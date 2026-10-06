"""A scriptable fake add-on (the add-on protocol).

It runs on two loopback ports — the add-on origin and a "CDN" origin — so redirects are
cross-origin. Tests reach it through host names (``*.test``) that a fake resolver maps to
addresses, which exercises the network policy. Behavior is scripted per ISRC:

ranged or non-ranged files, strong ETag or none, cross-origin redirects, numeric IDs,
``null``/empty optional fields, wrapped stream objects, URLs that expire after N requests
or after a time they declare (``expiresAt``; 403/410), slow resolution or first byte,
rate limits, unavailable tracks, ``/resolve`` items. Every request is logged for
assertions.

A track with ``dash`` (a folder from ``tests.harness.dash_fixtures``) links to a DASH
manifest on the add-on's origin, whose segments are on the CDN origin (or another host):
said by the stream answer's ``manifest``, by the link's ``.mpd`` path alone, or by the
answer's content type alone; the manifest and the init segment can be edited, a
segment's first answers scripted, one segment made slow, and the segments' ETags, ranges
and lengths taken away or given.

With a catalog (``catalog``: ``tests.harness.addon_catalog.FakeCatalog``) it
also answers the catalog requests - ``/search?q=``, ``/album/{id}``, ``/artist/{id}`` -
from invented items, and serves a cover for every address under ``/img/``. Without one,
``/search?q=`` answers with ``search_tracks`` when the test set them (or
``search_status``).
"""

from __future__ import annotations

import asyncio
import hashlib
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from itertools import count
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

from tests.harness.addon_catalog import FakeCatalog
from tests.harness.library import cover_image
from tests.harness.running import RunningServer

ADDON_HOST = "addon.fake.test"
CDN_HOST = "cdn.fake.test"


@dataclass
class FakeTrack:
    isrc: str
    audio: Path
    track_id: str | int = ""
    content_type: str = "audio/flac"
    etag: bool = True
    ranges: bool = True
    redirect: bool = False  # the stream URL answers 307 to the CDN origin
    redirect_host: str | None = None  # host name the redirect points to (default: the CDN)
    expire_after: int | None = None  # audio requests allowed per URL generation
    expire_status: int = 410
    link_seconds: float | None = None  # links declare expiresAt and stop working then
    resolve_delay: float = 0.0  # delay of /stream/{id}
    # A worker preparing the file: /stream/{id} answers once this long has passed since its
    # first request (the preparation goes on when a request is given up).
    prepare_seconds: float = 0.0
    lookup_delay: float = 0.0  # delay of /resolve-isrc
    first_byte_delay: float = 0.0
    body_delay: float = 0.0  # the answer's headers come at once, its first bytes this late
    # After that delay the audio answers this status instead (a link that fails late).
    first_byte_status: int | None = None
    chunk_delay: float = 0.0  # delay between 16 KiB body chunks
    truncate_after: int | None = None  # stop sending the body after this many bytes
    rate_limit_streams: int = 0  # first N /stream calls answer 429
    rate_limit_audio: int = 0  # first N audio requests answer 429
    stream_status: int | None = None  # /stream answers this status (e.g. 503: not ready)
    gone_streams: int = 0  # first N /stream calls answer 404 (an ID that is not there yet)
    available: bool = True  # resolve-isrc finds it
    stream_extra: dict[str, Any] = field(default_factory=dict)
    wrap: str | None = None  # "stream" or "streams"
    url_host: str | None = None  # host name to put in the stream URL
    resolve_item: dict[str, Any] | None = None  # answer of /resolve
    ready: bool | None = None  # answer of /availability (the add-on must declare it)
    availability_delay: float = 0.0
    # A DASH link: the folder of its manifest and segments (``dash_fixtures.dash_audio``).
    dash: Path | None = None
    dash_link: str = "field"  # said by "field" (manifest: dash), "path" (.mpd) or "type"
    dash_base: bool = False  # the segments' host as a BaseURL (else in their addresses)
    dash_host: str | None = None  # the segments' host name (default: the CDN)
    dash_edit: Callable[[str], str] | None = None  # the manifest as served
    dash_init_edit: Callable[[bytes], bytes] | None = None  # every init segment as served
    # A segment's (file name's) first answers: these statuses, in order - and its first
    # answers to a probe of its size (one byte: "bytes=0-0").
    segment_faults: dict[str, list[int]] = field(default_factory=dict)
    probe_faults: dict[str, list[int]] = field(default_factory=dict)
    segment_delay: float = 0.0  # (not for probes)
    segment_delays: dict[str, float] = field(default_factory=dict)  # one segment's, by name
    probe_delay: float = 0.0
    segment_etag: bool = False  # segments answer with a strong ETag of their bytes
    segment_ranges: bool = True  # False: a range is not answered (the whole segment, 200)
    segment_length: bool = True  # False: a whole segment's answer says no length

    def key(self) -> str:
        return str(self.track_id or self.isrc)


class FakeAddon:
    def __init__(
        self,
        name: str = "Fake",
        *,
        resources: tuple[str, ...] = ("stream", "isrc"),
        settings: list[dict[str, Any]] | None = None,
        catalog: FakeCatalog | None = None,
    ) -> None:
        self.name = name
        # An add-on with a catalog declares it (unless the test names its resources).
        self.catalog = catalog
        if catalog is not None and resources == ("stream", "isrc"):
            resources = ("stream", "isrc", "search", "catalog")
        self.resources = resources
        self.settings = settings or []
        self.refuse_lookups = False  # resolve-isrc answers 403 (a refusal, not a miss)
        self.manifest_delay = 0.0  # seconds before /manifest.json answers
        self.manifest_status: int | None = None  # /manifest.json answers this status
        self.retry_after: str | None = "1"  # the Retry-After of every 429 (None: none)
        self.rate_limit_availability = 0  # first N /availability calls answer 429
        self.rate_limit_prepare = False  # /availability with prepare=true answers 429
        self.api_redirects = 0  # every API request is redirected this many times first
        self.manifest_extra: dict[str, Any] = {}  # more fields of its manifest
        # Without a catalog: the tracks /search answers with (None: HTTP 404), or the
        # status it answers with instead.
        self.search_tracks: list[dict[str, Any]] | None = None
        self.search_status: int | None = None
        self.tracks: dict[str, FakeTrack] = {}
        self.by_key: dict[str, FakeTrack] = {}
        self.log: list[dict[str, Any]] = []
        self.generation: dict[str, int] = {}
        self.served: dict[tuple[str, int], int] = {}
        self.expiry: dict[tuple[str, int], float] = {}  # link (ISRC, generation) -> time
        self.bytes_sent: dict[str, int] = {}
        self.stream_calls: dict[str, int] = {}
        self.preparing: dict[str, float] = {}  # track key -> when its preparation started
        self.segments_at_once = 0  # DASH segment requests under way now, and at most
        self.most_segments_at_once = 0
        self.probes_at_once = 0  # ... and probes of their sizes (one byte each)
        self.most_probes_at_once = 0
        self._signatures = count(1)
        self.lock = threading.Lock()
        self.main = RunningServer(self._app("main"), lifespan="off")
        self.cdn = RunningServer(self._app("cdn"), lifespan="off")

    # --- control ---------------------------------------------------------------------

    def add(self, track: FakeTrack) -> FakeTrack:
        self.tracks[track.isrc] = track
        self.by_key[track.key()] = track
        return track

    def start(self) -> None:
        self.main.start()
        self.cdn.start()

    def stop(self) -> None:
        self.main.stop()
        self.cdn.stop()

    @property
    def base_url(self) -> str:
        return f"http://{ADDON_HOST}:{self.main.port}"

    def requests(self, endpoint: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.log if endpoint is None or r["endpoint"] == endpoint]

    def clear(self) -> None:
        with self.lock:
            self.log.clear()

    # --- server ----------------------------------------------------------------------

    def _record(self, request: Request, endpoint: str, **extra: Any) -> None:
        with self.lock:
            self.log.append(
                {
                    "endpoint": endpoint,
                    "path": request.url.path,
                    "params": dict(request.query_params),
                    "range": request.headers.get("range"),
                    "if_match": request.headers.get("if-match"),
                    "user_agent": request.headers.get("user-agent"),
                    "at": time.monotonic(),
                    **extra,
                }
            )

    def _app(self, origin: str) -> Starlette:
        async def manifest(request: Request) -> Response:
            self._record(request, "manifest")
            if self.manifest_delay:
                await asyncio.sleep(self.manifest_delay)
            if self.manifest_status is not None:
                return (
                    self._limited()
                    if self.manifest_status == 429
                    else Response(status_code=self.manifest_status)
                )
            return JSONResponse(
                {
                    "id": f"test.fake.{self.name.lower()}",
                    "name": self.name,
                    "version": "1.0.0",
                    "resources": list(self.resources),
                    "settings": self.settings,
                    **self.manifest_extra,
                }
            )

        async def resolve_isrc(request: Request) -> Response:
            isrc = request.query_params.get("isrc", "")
            self._record(request, "resolve-isrc", isrc=isrc)
            if self.refuse_lookups:
                return JSONResponse({"error": "forbidden"}, status_code=403)
            track = self.tracks.get(isrc)
            if track is not None and track.lookup_delay:
                await asyncio.sleep(track.lookup_delay)
            if track is None or not track.available:
                return JSONResponse({"trackId": None})
            return JSONResponse({"trackId": track.track_id or track.isrc})

        async def resolve(request: Request) -> Response:
            self._record(request, "resolve")
            for track in self.tracks.values():
                if track.resolve_item is not None:
                    return JSONResponse({"item": track.resolve_item})
            return JSONResponse({"item": None})

        async def availability(request: Request) -> Response:
            isrc = request.query_params.get("isrc", "")
            prepare = request.query_params.get("prepare") == "true"
            self._record(request, "availability", isrc=isrc, prepare=prepare)
            with self.lock:
                limited = self.rate_limit_availability > 0
                self.rate_limit_availability -= int(limited)
            if limited or (prepare and self.rate_limit_prepare):
                return self._limited()
            track = self.tracks.get(isrc)
            if track is not None and track.availability_delay:
                await asyncio.sleep(track.availability_delay)
            if track is None or not track.available:
                return JSONResponse({"available": False})
            return JSONResponse(
                {
                    "available": track.ready,
                    "id": track.key(),
                    "preparing": prepare and track.ready is False,
                }
            )

        async def catalog(request: Request) -> Response:
            """``/search``, ``/album/{id}``, ``/artist/{id}``: the catalog's answer - one
            the test set for this path, else made from its items; 404 for an item it does
            not have (and from an add-on without a catalog)."""
            endpoint = request.url.path.strip("/").split("/", 1)[0]
            ident = request.path_params.get("id", "")
            term = request.query_params.get("q", "")
            self._record(request, endpoint, key=ident, term=term)
            held = self.catalog
            if held is None and endpoint == "search" and self.search_status == 429:
                return self._limited()
            if held is None and endpoint == "search" and self.search_status is not None:
                return JSONResponse({"error": "unavailable"}, status_code=self.search_status)
            if held is None and endpoint == "search" and self.search_tracks is not None:
                return JSONResponse({"tracks": self.search_tracks, "albums": [], "artists": []})
            if held is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            if endpoint in held.delay:
                await asyncio.sleep(held.delay[endpoint])
            status = held.status.get(endpoint)
            if status == 429:
                return self._limited()
            if status is not None:
                return JSONResponse({"error": "unavailable"}, status_code=status)
            path = endpoint if endpoint == "search" else f"{endpoint}/{ident}"
            if path in held.answers:
                return JSONResponse(held.answers[path])
            if endpoint == "search":
                return JSONResponse(held.search(term))
            answer = held.album(ident) if endpoint == "album" else held.artist(ident)
            if answer is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            return JSONResponse(answer)

        async def image(request: Request) -> Response:
            self._record(request, "image")
            data = cover_image("blue").read_bytes()
            return Response(data, media_type="image/jpeg")

        async def stream(request: Request) -> Response:
            key = request.path_params["id"]
            self._record(request, "stream", key=key)
            track = self.by_key.get(key)
            if track is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            with self.lock:
                calls = self.stream_calls[key] = self.stream_calls.get(key, 0) + 1
                generation = self.generation[track.isrc] = self.generation.get(track.isrc, 0) + 1
            if track.stream_status is not None:
                return JSONResponse({"error": "unavailable"}, status_code=track.stream_status)
            if calls <= track.rate_limit_streams:
                return self._limited()
            if calls <= track.gone_streams:
                return JSONResponse({"error": "not found"}, status_code=404)
            if track.resolve_delay:
                await asyncio.sleep(track.resolve_delay)
            if track.prepare_seconds:
                with self.lock:
                    begun = self.preparing.setdefault(key, time.monotonic())
                await asyncio.sleep(max(0.0, begun + track.prepare_seconds - time.monotonic()))
            host = track.url_host or (ADDON_HOST if track.redirect else CDN_HOST)
            port = self.main.port if host == ADDON_HOST else self.cdn.port
            prefix = "r" if track.redirect else "audio"
            body: dict[str, Any] = {
                "url": f"http://{host}:{port}/{prefix}/{track.isrc}/{generation}",
                **track.stream_extra,
            }
            if track.dash is not None:
                # A signed manifest address on the add-on's origin (a made-up signature).
                name = "play" if track.dash_link == "type" else "out.mpd"
                signed = f"sig=made-up-{next(self._signatures)}"
                body["url"] = (
                    f"http://{ADDON_HOST}:{self.main.port}/dash/{track.isrc}/{generation}/{name}?{signed}"
                )
                if track.dash_link == "field":
                    body.update(
                        manifest="dash", format="dash", codec="flac", container="mp4",
                        encrypted=False,
                    )  # fmt: skip
                body.update(track.stream_extra)
            if track.link_seconds is not None:
                expires = self.expiry[(track.isrc, generation)] = time.time() + track.link_seconds
                body["expiresAt"] = round(expires)
            if track.wrap == "stream":
                body = {"stream": body}
            elif track.wrap == "streams":
                body = {"streams": [body]}
            return JSONResponse(body)

        async def redirect(request: Request) -> Response:
            self._record(request, "redirect")
            isrc, generation = request.path_params["isrc"], request.path_params["gen"]
            host = self.tracks[isrc].redirect_host or CDN_HOST
            return RedirectResponse(
                f"http://{host}:{self.cdn.port}/audio/{isrc}/{generation}", status_code=307
            )

        async def audio(request: Request) -> Response:
            isrc, generation = request.path_params["isrc"], int(request.path_params["gen"])
            track = self.tracks[isrc]
            self._record(request, "audio", isrc=isrc, origin=origin, generation=generation)
            with self.lock:
                count = self.served[(isrc, generation)] = self.served.get((isrc, generation), 0) + 1
            if count <= track.rate_limit_audio:
                return self._limited()
            expired = time.time() > self.expiry.get((isrc, generation), float("inf"))
            if (
                expired
                or generation != self.generation.get(isrc)
                or (track.expire_after is not None and count > track.expire_after)
            ):
                return Response(status_code=track.expire_status)
            data = track.audio.read_bytes()
            etag = f'"fake-{isrc}-{len(data)}"'
            if track.etag and request.headers.get("if-match") not in (None, etag):
                return Response(status_code=412)
            headers = {"content-type": track.content_type}
            if track.etag:
                headers["etag"] = etag
            start, end, status = 0, len(data) - 1, 200
            wanted = request.headers.get("range")
            if track.ranges:
                headers["accept-ranges"] = "bytes"
                if wanted and wanted.startswith("bytes="):
                    first, _, last = wanted[6:].partition("-")
                    if first == "":
                        start = max(0, len(data) - int(last))
                    else:
                        start = int(first)
                        end = min(int(last), len(data) - 1) if last else len(data) - 1
                    if start >= len(data):
                        return Response(
                            status_code=416, headers={"content-range": f"bytes */{len(data)}"}
                        )
                    status = 206
                    headers["content-range"] = f"bytes {start}-{end}/{len(data)}"
            payload = data[start : end + 1]
            headers["content-length"] = str(len(payload))
            if request.method == "HEAD":
                return Response(status_code=status, headers=headers)
            if track.first_byte_delay:
                await asyncio.sleep(track.first_byte_delay)
            if track.first_byte_status is not None:
                return Response(status_code=track.first_byte_status)
            if track.truncate_after is not None:
                payload_sent = payload[: track.truncate_after]  # Content-Length still promises all
            else:
                payload_sent = payload
            return StreamingResponse(
                self._chunks(isrc, payload_sent, track.chunk_delay, track.body_delay),
                status_code=status,
                headers=headers,
            )

        async def dash_manifest(request: Request) -> Response:
            isrc, generation = request.path_params["isrc"], int(request.path_params["gen"])
            track = self.tracks[isrc]
            self._record(request, "mpd", isrc=isrc, origin=origin, generation=generation)
            if generation != self.generation.get(isrc):
                return Response(status_code=track.expire_status)
            assert track.dash is not None
            text = (track.dash / "out.mpd").read_text()
            host = track.dash_host or CDN_HOST
            base = f"http://{host}:{self.cdn.port}/seg/{isrc}/{generation}/"
            if track.dash_base:
                text = text.replace("<Period", f"<BaseURL>{base}</BaseURL><Period", 1)
            else:
                for attribute in ('initialization="', 'media="', 'sourceURL="'):
                    text = text.replace(attribute, attribute + base)
                text = text.replace("<BaseURL>", "<BaseURL>" + base)
            if track.dash_edit is not None:
                text = track.dash_edit(text)
            return Response(text, media_type="application/dash+xml")

        async def dash_segment(request: Request) -> Response:
            isrc, generation = request.path_params["isrc"], int(request.path_params["gen"])
            name = request.path_params["name"]
            track = self.tracks[isrc]
            probe = request.headers.get("range") == "bytes=0-0"  # (a size asked for)
            self._record(
                request, "segment", isrc=isrc, origin=origin, generation=generation, name=name,
                probe=probe, method=request.method,
            )  # fmt: skip
            with self.lock:
                if probe:
                    self.probes_at_once += 1
                    self.most_probes_at_once = max(self.most_probes_at_once, self.probes_at_once)
                else:
                    self.segments_at_once += 1
                    self.most_segments_at_once = max(
                        self.most_segments_at_once, self.segments_at_once
                    )
                faults = (track.probe_faults if probe else track.segment_faults).get(name)
                fault = faults.pop(0) if faults else None
            try:
                delay = (
                    track.probe_delay
                    if probe
                    else track.segment_delays.get(name, track.segment_delay)
                )
                if delay:
                    await asyncio.sleep(delay)
                if fault == 429:
                    return self._limited()
                if fault == 0:  # an answer that breaks off: its length promised, half sent
                    data = (track.dash / name).read_bytes()
                    return StreamingResponse(
                        iter([data[: len(data) // 2]]),
                        headers={"content-length": str(len(data))},
                        media_type="video/mp4",
                    )
                if fault is not None:
                    return Response(status_code=fault)
                if generation != self.generation.get(isrc):
                    return Response(status_code=track.expire_status)
                assert track.dash is not None
                data = (track.dash / name).read_bytes()
                if name.startswith("init") and track.dash_init_edit is not None:
                    data = track.dash_init_edit(data)
                headers = {}
                if track.segment_etag:
                    headers["etag"] = f'"seg-{hashlib.sha256(data).hexdigest()[:16]}"'
                wanted = request.headers.get("range")
                if wanted and wanted.startswith("bytes=") and track.segment_ranges:
                    first, _, last = wanted[6:].partition("-")
                    start, end = int(first), min(int(last), len(data) - 1)
                    headers["content-range"] = f"bytes {start}-{end}/{len(data)}"
                    payload = b"" if request.method == "HEAD" else data[start : end + 1]
                    return Response(
                        payload, status_code=206, headers=headers, media_type="video/mp4"
                    )
                if not track.segment_length:  # (streamed: no length said)
                    return StreamingResponse(iter([data]), headers=headers, media_type="video/mp4")
                if request.method == "HEAD":
                    headers["content-length"] = str(len(data))
                    return Response(b"", headers=headers, media_type="video/mp4")
                return Response(data, headers=headers, media_type="video/mp4")
            finally:
                with self.lock:
                    if probe:
                        self.probes_at_once -= 1
                    else:
                        self.segments_at_once -= 1

        def api(endpoint: Any) -> Any:
            """``endpoint`` behind the redirects every API request gets first."""

            async def hop(request: Request) -> Response:
                done = int(request.query_params.get("_hop", "0"))
                if done < self.api_redirects:
                    self._record(request, "api-redirect")
                    onward = request.url.include_query_params(_hop=done + 1)
                    return RedirectResponse(str(onward), status_code=307)
                return await endpoint(request)

            return hop

        routes = [
            Route("/manifest.json", api(manifest)),
            Route("/resolve-isrc", api(resolve_isrc)),
            Route("/resolve", api(resolve)),
            Route("/availability", api(availability)),
            Route("/stream/{id}", api(stream)),
            Route("/search", api(catalog)),
            Route("/album/{id:path}", api(catalog)),
            Route("/artist/{id:path}", api(catalog)),
            Route("/img/{rest:path}", image),
            Route("/r/{isrc}/{gen}", redirect, methods=["GET", "HEAD"]),
            Route("/audio/{isrc}/{gen}", audio, methods=["GET", "HEAD"]),
            Route("/dash/{isrc}/{gen}/{name}", dash_manifest),
            Route("/seg/{isrc}/{gen}/{name}", dash_segment, methods=["GET", "HEAD"]),
        ]
        return Starlette(routes=routes)

    def _limited(self) -> Response:
        """ "Too many requests", with the Retry-After the test set."""
        headers = {} if self.retry_after is None else {"retry-after": self.retry_after}
        return JSONResponse({"error": "RateLimited"}, status_code=429, headers=headers)

    async def _chunks(self, isrc: str, payload: bytes, delay: float, first: float = 0.0):  # type: ignore[no-untyped-def]
        if first:
            await asyncio.sleep(first)
        for offset in range(0, len(payload), 16384):
            chunk = payload[offset : offset + 16384]
            with self.lock:
                self.bytes_sent[isrc] = self.bytes_sent.get(isrc, 0) + len(chunk)
            yield chunk
            if delay:
                await asyncio.sleep(delay)


def fake_resolver(mapping: dict[str, str]) -> Callable[[str, int], Awaitable[list[str]]]:
    """Resolve ``*.test`` names from ``mapping`` (default 127.0.0.1); others normally."""

    async def resolve(host: str, port: int) -> list[str]:
        if host in mapping:
            return [mapping[host]]
        if host.endswith(".test"):
            return ["127.0.0.1"]
        if host.replace(".", "").isdigit() or ":" in host:
            return [host]
        raise OSError("unknown test host")

    return resolve
