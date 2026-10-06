"""Add-ons in the dashboard: the stored list, their manifests (version, declared settings)
and health.

A manifest is fetched through the network policy with the add-on's own reach, at most once
a minute per add-on (the page's health check). Its declared settings become the add-on's
settings form: a select for a choice (e.g. quality), a checkbox for an on/off, a number
field, and write-only fields for the rest - a setting declared secret, one whose key looks
like a key or a password whatever type it is declared with, and free text the manifest does
not mark ``"secret": false``. A stored value is never shown unless it cannot be a secret
(``shown``): what a manifest declares may change, what was stored under it does not - so
a number or a text is shown only when it was saved through the form's own visible field
(its mark), never one from the configuration file or typed into a write-only field.
Errors are short reasons, never URLs.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import anyio
import httpx

from shijhon.delivery import pacing
from shijhon.delivery.addon import addon_base, allows_downloads, sendable
from shijhon.delivery.netpolicy import Reach, Resolver, denied, policy_client, system_resolver
from shijhon.delivery.pacing import AddonPace
from shijhon.delivery.sources import StoredSource

MAX_MANIFEST_BYTES = 256 * 1024
CHECK_SECONDS = 60.0
CHECK_TIMEOUT = 4.0
# How long a check waits for its turn at an add-on's request limit when the dashboard
# already has a manifest of it to show (a page is not held up for a newer one).
KNOWN_WAIT = 0.5
# An address's limits: where a request to it takes its turn (None: none).
PaceAt = Callable[[str], Awaitable[AddonPace | None]]
REACHES = {
    "public": "Internet",
    "private": "Local network",
    "loopback": "This machine",
}
# A setting whose key looks like one of these is write-only, whatever type its manifest
# declares (add-on URLs often carry keys) ...
_SECRET_KEY = re.compile(
    r"(?i)(key|token|secret|pass(word|wd)?|auth|cookie|session|credential|url|uri|endpoint"
    r"|webhook)"
)
# ... or one of whose words (``userId``, ``pin_code``) is one of these.
_SECRET_WORDS = frozenset(
    {"pin", "otp", "code", "id", "user", "username", "login", "email", "account", "serial"}
    | {"license", "licence"}
)
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")
_SECRET_TYPES = {"password", "secret", "token", "url", "uri"}
_SWITCH_TYPES = {"toggle", "boolean", "bool", "switch", "checkbox"}
_NUMBER_TYPES = {"number", "integer", "int", "float", "decimal"}
_RESERVED = {"q", "isrc"}  # query parameters of the protocol itself
# A select's entry for a stored value that is none of its options: kept, never shown.
KEPT = "__kept__"


@dataclass(frozen=True)
class Declared:
    """A setting an add-on's manifest declares."""

    key: str
    label: str
    kind: str  # select | switch | number | secret | text
    default: str | None
    options: tuple[tuple[str, str], ...] = ()
    help: str = ""


@dataclass(frozen=True)
class ManifestInfo:
    name: str
    version: str
    declared: tuple[Declared, ...]
    downloads: bool = True  # False: it asks not to be used for downloads (``allowDownloads``)


@dataclass(frozen=True)
class Check:
    at: float  # wall clock
    manifest: ManifestInfo | None
    error: str | None  # a short reason when the manifest could not be read


class Busy(ValueError):
    """The manifest was not asked for: its request got no turn at the add-on's request
    limit in time - which says nothing about the add-on."""


def host_of(base_url: str) -> str:
    """An add-on's host: all of its address the dashboard shows (the rest may hold keys)."""
    try:
        return httpx.URL(base_url.strip()).host or "?"
    except (httpx.InvalidURL, ValueError, TypeError):
        return "?"


def representable(value: Any) -> bool:
    """Whether a stored setting fits the settings form's controls (text, a number, on/off);
    a list or a table from the configuration file does not: it is named, never changed -
    and never sent to the add-on (``addon.sendable``: the same rule)."""
    return sendable(value)


def looks_secret(key: str) -> bool:
    """Whether a setting's key names a credential or the like (a heuristic: it errs toward
    write-only)."""
    words = {word.lower() for word in _WORD.findall(key)}
    return _SECRET_KEY.search(key) is not None or not words.isdisjoint(_SECRET_WORDS)


def numeric(value: Any) -> bool:
    """A stored value that is a number (also one written as text in the file)."""
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    return isinstance(value, str) and re.fullmatch(r"\s*-?\d+([.,]\d+)?\s*", value) is not None


def mark(value: Any) -> str:
    """A mark of a value saved through a visible field: the value is shown again only
    while it is still that one (whoever wrote the setting since)."""
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def hidden_mark(value: Any) -> str:
    """The mark of a value typed into a write-only field: never shown, whatever a later
    manifest declares the setting - a choice that happens to list it, an on/off."""
    return "hidden:" + mark(value)


def shown(item: Declared, value: Any, marks: Mapping[str, str] | None = None) -> bool:
    """Whether a stored value is plain under its declaration, so that the settings form may
    show it and a move to another host may take it along: nothing stored; a choice among
    the manifest's own options; on or off; a number, or text the manifest marks plain,
    that was saved through the form's own visible field (``marks``: by key, of the value
    saved so). Any other value may be a secret - from the configuration file, or stored
    under an earlier declaration (a token whose manifest now offers options, a key
    declared a number or plain text): never shown."""
    if item.kind == "secret" or not representable(value):
        return False
    if value is None:
        return True
    if (marks or {}).get(item.key) == hidden_mark(value):
        return False  # typed into a write-only field
    if item.kind == "select":
        return setting_text(value) in dict(item.options)
    if item.kind == "switch":
        return setting_text(value) in ("true", "false")
    if item.kind == "number" and not numeric(value):
        return False
    return (marks or {}).get(item.key) == mark(value)


def plain_keys(old: ManifestInfo | None, new: ManifestInfo | None = None) -> set[str]:
    """The settings that may go to another host: those the add-on's manifest (``old``,
    as last read) declares plain - a choice, an on/off, a number, free text not named like a
    key - and the ``new`` host's does not declare secret. The new manifest can only
    protect more; with the old one unread, nothing is known to be plain."""
    if old is None:
        return set()
    secret = {item.key for item in new.declared if item.kind == "secret"} if new else set()
    return {item.key for item in old.declared if item.kind != "secret"} - secret


def manifest_url(base_url: str) -> httpx.URL:
    """The manifest of an add-on given by its base or manifest URL (its query kept)."""
    base = addon_base(base_url)
    path = base.raw_path.split(b"?", 1)[0].rstrip(b"/") + b"/manifest.json"
    if base.query:
        path += b"?" + base.query
    return base.copy_with(raw_path=path)


def _text(value: Any) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    if isinstance(value, str):
        return value.strip() or None
    return None


def setting_text(value: Any) -> str:
    """A setting's value as the add-on receives it (booleans as true/false)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return _text(value) or ""


def _options(raw: Any) -> tuple[tuple[str, str], ...]:
    options = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            value = _text(item.get("value", item.get("id", item.get("key"))))
            label = _text(item.get("label", item.get("name", item.get("title")))) or value
        else:
            value = label = _text(item)
        if value is not None and label is not None:
            options.append((value, label))
    return tuple(options)


def parse_manifest(data: Any) -> ManifestInfo:
    if not isinstance(data, dict):
        raise ValueError("manifest is not an object")
    declared = []
    raw = data.get("settings")
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        key = _text(item.get("key"))
        if not key or key in _RESERVED:
            continue
        kind_name = (_text(item.get("type")) or "text").lower()
        options = _options(item.get("options"))
        if kind_name in _SECRET_TYPES or item.get("secret") is True:
            kind = "secret"
        elif options:  # shows the manifest's own options only, never a stored value
            kind = "select"
        elif kind_name in _SWITCH_TYPES:  # shows on or off only
            kind = "switch"
        elif looks_secret(key):  # whatever its declared type: a number, text marked plain
            kind = "secret"
        elif kind_name in _NUMBER_TYPES:
            kind = "number"
        else:
            # Free text of any declared type: write-only unless the manifest marks it plain.
            kind = "text" if item.get("secret") is False else "secret"
        default = item.get("default")
        declared.append(
            Declared(
                key,
                _text(item.get("label", item.get("title", item.get("name")))) or key,
                kind,
                None if default is None else setting_text(default),
                options,
                _text(item.get("description", item.get("help"))) or "",
            )
        )
    return ManifestInfo(
        _text(data.get("name")) or "",
        _text(data.get("version")) or "",
        tuple(declared),
        allows_downloads(data.get("allowDownloads")),
    )


async def fetch_manifest(
    base_url: str,
    reach: Reach,
    *,
    resolver: Resolver = system_resolver,
    pace: PaceAt | None = None,
    wait: float | None = None,
) -> ManifestInfo:
    """Read an add-on's manifest; raises ValueError with a short reason (never a URL).
    ``pace``: the limits of the origin an address is at - the request, and each hop
    of a redirect, takes its turn there, after every request a listener or a background
    fetch waits for, and gives up after ``wait`` seconds (``Busy``); it is not sent while
    the add-on is left alone after a rate limit, and its own answer "too many requests"
    leaves the add-on alone like any other (its ``Retry-After``)."""
    try:
        url = manifest_url(base_url)
    except (httpx.InvalidURL, ValueError, TypeError):
        raise ValueError("not a valid URL") from None
    if url.scheme not in ("http", "https") or not url.host:
        raise ValueError("not an http(s) URL")
    asked: list[AddonPace] = []  # the limits of the origin each hop went to

    async def turn(hop: httpx.URL) -> None:
        limits = await pace(str(hop)) if pace is not None else None
        if limits is None:
            return
        try:
            with (
                pacing.urgent(pacing.CHECK),
                anyio.fail_after(CHECK_TIMEOUT if wait is None else wait),
            ):
                await limits.request()
        except TimeoutError:
            raise Busy("busy: " + pacing.REQUESTS) from None
        except pacing.Blocked:
            raise Busy(pacing.BLOCKED) from None
        asked.append(limits)

    async with policy_client(reach, timeout=CHECK_TIMEOUT, resolver=resolver) as http:
        try:
            get = pacing.get(http, url, turn=turn, headers={"accept": "application/json"})
            async with get as response:
                if response.status_code == 429 and asked:
                    asked[-1].limited(pacing.retry_after(response.headers.get("retry-after")))
                if response.status_code != 200:
                    raise ValueError(f"HTTP {response.status_code}")
                body = b""
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_MANIFEST_BYTES:
                        raise ValueError("manifest too large")
        except httpx.TimeoutException:
            raise ValueError("timeout") from None
        except httpx.HTTPError as exc:
            if denied(exc):
                raise ValueError("address not allowed for this network reach") from None
            raise ValueError(f"connection failed ({type(exc).__name__})") from None
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise ValueError("manifest is not JSON") from None
    return parse_manifest(data)


class ManifestChecks:
    """Manifests of the stored add-ons, read at most once a minute each - one request for
    pages loading at the same time, and none for a manifest the dashboard has just read
    (an add-on added, or moved to another address: ``learn``)."""

    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        every: float = CHECK_SECONDS,
        clock: Callable[[], float] = time.time,
        pace: PaceAt | None = None,
    ) -> None:
        self.pace = pace  # an address's limits
        self.resolver = resolver
        self.every = every
        self.clock = clock
        self._checks: dict[int, tuple[str, Check]] = {}
        self._reading: dict[tuple[int, str], anyio.Event] = {}  # reads under way

    def _key(self, addon: StoredSource) -> str:
        return f"{addon.reach}\n{addon.base_url}"

    def known(self, addon: StoredSource) -> Check | None:
        entry = self._checks.get(addon.id)
        return entry[1] if entry is not None and entry[0] == self._key(addon) else None

    def forget(self, addon_id: int) -> None:
        self._checks.pop(addon_id, None)

    def learn(self, addon: StoredSource, manifest: ManifestInfo) -> None:
        """The add-on's manifest, read just now at its address (not asked for again)."""
        self._checks[addon.id] = (self._key(addon), Check(self.clock(), manifest, None))

    async def check(self, addons: list[StoredSource]) -> None:
        """Read the manifests that are due, all together, each within 5 s: an enabled
        add-on's once a minute, a disabled one's until it was read once (for its settings
        form)."""
        now = self.clock()

        def due(addon: StoredSource) -> bool:
            known = self.known(addon)
            return known is None or (
                (addon.enabled or known.manifest is None)  # a disabled one until read once
                and now - known.at >= self.every
            )

        async def one(addon: StoredSource) -> None:
            reading = (addon.id, self._key(addon))
            under_way = self._reading.get(reading)
            if under_way is not None:  # another page's read of it: its result, no request
                await under_way.wait()
                return
            if not due(addon):  # read by another page since this one looked
                return
            done = self._reading[reading] = anyio.Event()
            try:
                await read(addon)
            finally:
                del self._reading[reading]
                done.set()

        async def read(addon: StoredSource) -> None:
            try:
                # (DNS and slow bodies included; and its turn at the add-on's request limit)
                with anyio.fail_after(2 * CHECK_TIMEOUT + 1):
                    info = await fetch_manifest(
                        addon.base_url,
                        Reach(addon.reach),
                        resolver=self.resolver,
                        pace=self.pace,
                        # (A page is not held up for a manifest it already has one of.)
                        wait=None if self.known(addon) is None else KNOWN_WAIT,
                    )
                result = Check(self.clock(), info, None)
            except TimeoutError:
                result = Check(self.clock(), None, "timeout")
            except Busy as exc:
                if self.known(addon) is not None:
                    return  # what was read before stands; the next look asks again
                result = Check(self.clock(), None, str(exc))
            except ValueError as exc:
                result = Check(self.clock(), None, str(exc))
            except Exception as exc:  # a broken add-on must not break the page
                result = Check(self.clock(), None, f"unreadable manifest ({type(exc).__name__})")
            self._checks[addon.id] = (self._key(addon), result)

        async with anyio.create_task_group() as group:
            for addon in addons:
                if due(addon):
                    group.start_soon(one, addon)
