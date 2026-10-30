#!/usr/bin/env python3
"""Small standard-library client for owner-scoped playlist requests.

Credentials come only from the environment. Errors never include remote bodies,
request input, URLs or exception details. No operation follows redirects or retries
automatically; polling reads status only.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import os
import re
import sys
import time
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_INPUT_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
API = "/shijhon/api/playlists"
ACTIVE_STATES = frozenset({"queued", "pending", "running", "generating", "preparing", "publishing"})
KEY_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{8,128}")
ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,128}")


class ClientError(Exception):
    """A fixed message safe to display, without request or remote details."""


def _text(value: object, maximum: int, *, required: bool = False) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= maximum
        and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)
        and (not required or bool(value.strip()))
    )


def _integer(value: object, minimum: int, maximum: int) -> bool:
    return type(value) is int and minimum <= value <= maximum


def validate_create(value: object) -> dict:
    """Validate the public contract without changing wording, order or constraints."""
    defaults = {
        "name": "Described playlist",
        "prompt": "",
        "count": 30,
        "max_per_artist": 2,
        "use_history": False,
        "use_library": False,
        "tracks": [],
        "save_instructions": False,
        "cadence": "manual",
        "preview": False,
    }
    # Optional: sent only when supplied, so earlier input files send the same body.
    references = {"reference_playlists": 3, "reference_songs": 50}
    if not isinstance(value, dict) or value.keys() - defaults.keys() - references.keys() - {
        "exclude_reference"
    }:
        raise ClientError("Invalid creation fields; see references/api.md.")
    for field, limit in references.items():
        ids = value.get(field, [])
        if (
            not isinstance(ids, list)
            or len(ids) > limit
            or len(set(map(str, ids))) != len(ids)
            or not all(
                isinstance(i, str) and len(i) <= 100 and ID_PATTERN.fullmatch(i) for i in ids
            )
        ):
            raise ClientError("References are distinct Navidrome IDs; see references/api.md.")
    if type(value.get("exclude_reference", True)) is not bool:
        raise ClientError("Invalid creation values; see references/api.md.")
    body = defaults | value
    if (
        not _text(body["name"], 100, required=body["name"] != "")
        or not _text(body["prompt"], 2000)
        or not _integer(body["count"], 1, 100)
        or not _integer(body["max_per_artist"], 1, 10)
        or any(
            type(body[field]) is not bool
            for field in ("use_history", "use_library", "save_instructions", "preview")
        )
        or body["cadence"] not in ("manual", "daily", "weekly")
    ):
        raise ClientError("Invalid creation values; see references/api.md.")
    tracks = body["tracks"]
    if not isinstance(tracks, list) or len(tracks) > 100:
        raise ClientError("Tracks must be an ordered list of at most 100 entries.")
    checked = []
    for track in tracks:
        if (
            not isinstance(track, dict)
            or track.keys() - {"artist", "title", "album", "version"}
            or not _text(track.get("artist"), 300, required=True)
            or not _text(track.get("title"), 300, required=True)
            or not _text(track.get("album", ""), 300)
            or not _text(track.get("version", ""), 100)
        ):
            raise ClientError("Invalid track; only artist, title, album and version are accepted.")
        checked.append({"album": "", "version": ""} | track)
    body["tracks"] = checked
    if tracks:
        if body.get("reference_playlists") or body.get("reference_songs"):
            raise ClientError("References work with a prompt, not with explicit tracks.")
        if (
            not body["name"]
            or len(tracks) != body["count"]
            or body["cadence"] != "manual"
            or body["use_history"]
            or body["use_library"]
            or body["save_instructions"]
        ):
            raise ClientError(
                "Explicit tracks require count matching their length, manual cadence "
                "both contexts off and no saved refresh instructions."
            )
    elif not body["prompt"].strip():
        raise ClientError("Supply a nonempty prompt or an explicit selection matching count.")
    if body["cadence"] != "manual" and (
        not body["save_instructions"] or not body["prompt"].strip()
    ):
        raise ClientError("Recurrence requires a prompt and save_instructions=true.")
    return body


def validate_context(
    *, use_history: bool = False, use_library: bool = False, limit: int = 30
) -> dict:
    if (
        type(use_history) is not bool
        or type(use_library) is not bool
        or not _integer(limit, 1, 100)
    ):
        raise ClientError("Context switches must be booleans and limit must be 1..100.")
    return {"use_history": use_history, "use_library": use_library, "limit": limit}


def validate_url(value: str) -> str:
    """Accept HTTPS, or HTTP to a literal loopback address / localhost only."""
    try:
        if not value or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value:
            raise ValueError
        url = urlsplit(value)
        host = url.hostname
        if (
            url.scheme not in ("https", "http")
            or not host
            or url.username is not None
            or url.password is not None
            or "?" in value
            or "#" in value
            or "%" in url.netloc
            or (url.port is not None and not 1 <= url.port <= 65535)
        ):
            raise ValueError
        if (
            url.scheme == "http"
            and host != "localhost"
            and not ipaddress.ip_address(host).is_loopback
        ):
            raise ValueError
        return urlunsplit((url.scheme, url.netloc, url.path.rstrip("/"), "", ""))
    except ValueError:
        raise ClientError(
            "SHIJHON_URL must use HTTPS (HTTP loopback only), without credentials, "
            "query or fragment."
        ) from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise ClientError(
            "Redirect refused; check SHIJHON_URL without sending credentials elsewhere."
        )


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _no_constant(value: str) -> None:
    raise ValueError


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return number


def _decode(raw: bytes) -> object:
    return json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_no_constant,
        parse_float=_finite_float,
    )


def load_input(path: Path) -> dict:
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError
        return validate_create(_decode(raw))
    except (OSError, ValueError, RecursionError):
        raise ClientError(
            "Input must be a UTF-8 JSON object within 256 KiB, without duplicate "
            "fields or non-finite numbers."
        ) from None


class Client:
    def __init__(self, base_url: str, token: str):
        self._base_url = validate_url(base_url)
        if not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
            raise ClientError(
                "Set SHIJHON_PLAYLIST_TOKEN to a valid bearer token in the environment."
            )
        self._token = token
        if token in unquote(self._base_url):
            raise ClientError("SHIJHON_URL must not contain the bearer token.")
        # Avoid implicit proxy settings that could send loopback HTTP credentials elsewhere.
        self._opener = build_opener(ProxyHandler({}), NoRedirect())

    @classmethod
    def from_environment(cls) -> Client:
        return cls(os.environ.get("SHIJHON_URL", ""), os.environ.get("SHIJHON_PLAYLIST_TOKEN", ""))

    def _redact(self, value: object) -> object:
        if isinstance(value, str):
            return value.replace(self._token, "[redacted]")
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        return value

    def _request(
        self, method: str, path: str, body: dict | None = None, *, key: str | None = None
    ) -> dict:
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        if self._token in path:
            raise ClientError("Request IDs must not contain the bearer token.")
        data = None
        if body is not None:
            data = json.dumps(
                body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
            headers["Content-Type"] = "application/json"
        # validate_url restricts the scheme; path is constructed only by this client.
        request = Request(  # noqa: S310
            self._base_url + API + path, data=data, headers=headers, method=method
        )
        try:
            with self._opener.open(request, timeout=15) as response:
                status = response.status
                if 300 <= status < 400:
                    raise ClientError("Redirect refused; check SHIJHON_URL.")
                if status not in (200, 202):
                    raise ClientError("Unexpected HTTP status; keep the original request and key.")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError
            result = _decode(raw)
            if not isinstance(result, dict):
                raise ValueError
            return self._redact(result)
        except HTTPError as exc:
            status = exc.code
            exc.close()
            messages = {
                400: "Server rejected the input; check the API contract.",
                401: "Invalid playlist token; configure it in the environment.",
                403: "Token does not permit this request or the requested context.",
                404: "Request was not found or is not accessible to this token.",
                409: "Idempotency-Key conflicts with earlier input; retain the original request.",
                413: "Request body is too large; reduce the input.",
                415: "Unsupported media type; send application/json.",
                424: "Required playlist setup is missing; check the connection settings.",
                429: "Too many playlist requests; try again later with the same input and key.",
            }
            if 300 <= status < 400:
                raise ClientError("Redirect refused; check SHIJHON_URL.") from None
            raise ClientError(
                messages.get(
                    status,
                    "Server unavailable; outcome may be unknown. "
                    "Keep the exact input and key; check status or replay that request.",
                )
            ) from None
        except (URLError, OSError, HTTPException):
            raise ClientError(
                "Connection failed; outcome may be unknown. Keep the exact input and "
                "key; check status or replay that request."
            ) from None
        except (ValueError, RecursionError):
            raise ClientError(
                "Invalid or oversized JSON response; outcome may be unknown. "
                "Keep the exact input and key and check status."
            ) from None

    def create(self, value: object, *, key: str) -> dict:
        if not isinstance(key, str) or not KEY_PATTERN.fullmatch(key) or self._token in key:
            raise ClientError("Idempotency-Key must be 8..128 letters, digits or _.:-.")
        return self._request("POST", "/requests", validate_create(value), key=key)

    def _request_path(self, ident: str) -> str:
        if not isinstance(ident, str) or not ID_PATTERN.fullmatch(ident) or ident in (".", ".."):
            raise ClientError(
                "Request ID must be a single safe path segment of at most 128 characters."
            )
        return "/requests/" + quote(ident, safe="")

    def status(self, ident: str, *, attempts: int = 1, interval: int = 2) -> dict:
        if not _integer(attempts, 1, 30) or not _integer(interval, 1, 30):
            raise ClientError("Status attempts and interval must each be 1..30.")
        path = self._request_path(ident)
        for attempt in range(attempts):
            result = self._request("GET", path)
            state = result.get("state")
            if not isinstance(state, str):
                raise ClientError(
                    "Invalid status response; retain the request ID and check status."
                )
            if state not in ACTIVE_STATES or attempt == attempts - 1:
                return result
            time.sleep(interval)
        raise AssertionError("unreachable")

    def context(
        self, *, use_history: bool = False, use_library: bool = False, limit: int = 30
    ) -> dict:
        return self._request(
            "POST",
            "/context",
            validate_context(use_history=use_history, use_library=use_library, limit=limit),
        )

    def retry(self, ident: str) -> dict:
        return self._request("POST", self._request_path(ident) + "/retry", {})


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's normal errors echo argument values, which may contain secrets.
        self.print_usage(sys.stderr)
        self.exit(2, "Invalid arguments; use --help.\n")


def main(argv: list[str] | None = None) -> int:
    parser = SafeParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create", help="Create from a saved JSON input file")
    create.add_argument("input", type=Path)
    create.add_argument("--idempotency-key", required=True)
    status = commands.add_parser("status", help="Read status, with optional bounded polling")
    status.add_argument("id")
    status.add_argument("--attempts", type=int, default=1)
    status.add_argument("--interval", type=int, default=2)
    context = commands.add_parser("context", help="Read only explicitly requested context")
    context.add_argument("--history", action="store_true")
    context.add_argument("--library", action="store_true")
    context.add_argument("--limit", type=int, default=30)
    retry = commands.add_parser("retry", help="Retry the same saved request, never create another")
    retry.add_argument("id")
    args = parser.parse_args(argv)
    try:
        client = Client.from_environment()
        if args.command == "create":
            result = client.create(load_input(args.input), key=args.idempotency_key)
        elif args.command == "status":
            result = client.status(args.id, attempts=args.attempts, interval=args.interval)
        elif args.command == "context":
            result = client.context(
                use_history=args.history, use_library=args.library, limit=args.limit
            )
        else:
            result = client.retry(args.id)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except ClientError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Interrupted; keep the exact input and key and check status before retrying.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
