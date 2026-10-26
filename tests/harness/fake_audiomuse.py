"""A scriptable fake AudioMuse-AI (v3.6.4).

It answers the requests Shijhon makes with the shapes AudioMuse-AI's source gives them
(https://github.com/NeptuneHub/AudioMuse-AI at tag v3.6.4; the file of each shape is named
where it is made), over invented songs:

- every route but ``/api/health`` needs ``Authorization: Bearer <token>`` (``app_auth.py``,
  ``check_auth_needed``); a path under ``/api/`` answers 401 as JSON;
- ``server`` (a query parameter, or a key of a JSON body) names one of its media servers
  by name or ID and an unknown one answers 400 (``app_server_context.py``,
  ``resolve_request_server_id``); the songs it lists and the IDs it answers with are that
  server's, and a song the server does not have is left out (``scope_results``). A song has
  an internal canonical ID that is never answered;
- the request line is limited to 4094 bytes, as gunicorn's default is;
- ``/api/sync`` (``app_sync.py``): the manifest (``fields=index``, 1000 a page), the payload of
  ``ids=`` (500 at most) and the paged payload (500 a page); the fingerprint ``fp`` changes
  with the song's analysis;
- ``/api/similar_tracks`` (``app_ivf.py``), ``/api/alchemy`` (``app_alchemy.py``,
  ``tasks/song_alchemy.py``: a ``temperature`` above 0 samples the results), ``/api/clap/search``
  and ``/api/clap/concepts`` (``app_clap_search.py``, ``tasks/clap_steering.py``),
  ``/api/find_path`` (``app_path.py``), ``/api/version`` and ``/api/health`` (``app.py``).

Tests script it through ``songs``, the ranking of a search (``search_results``), failures
(``statuses``: an endpoint answers that status), slowness (``delays``) and ``available`` /
``loaded`` / ``vocabulary``. Every request is logged.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
import threading
import uuid
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from tests.harness.running import RunningServer

REQUEST_LINE_LIMIT = 4094  # gunicorn's default limit_request_line

# error/error_dictionary.py: the codes a body carries (with the status they answer with)
ERR_INVALID_REQUEST, ERR_NOT_FOUND, ERR_UNAUTHORIZED = 1003, 1004, 1005
ERR_INDEX_EMPTY, ERR_SEARCH_FAILED = 3002, 3003

# dclap_sae_concepts.json: the category order and some of its terms.
CONCEPTS = {
    "genre": ("jazz", "rock", "hip hop", "ambient music", "House", "classical music"),
    "instrument": ("piano", "violin", "drums", "electric guitar", "saxophone"),
    "vocals": ("female vocals", "male vocals", "instrumental music with no vocals"),
    "mood": ("sad", "relaxed", "epic", "chillout"),
    "production": ("acoustic", "heavy bass", "live recording"),
    "tempo": ("slow tempo", "fast tempo", "danceable beat"),
    "era": ("1980s music", "90s"),
}
LABELS = {
    "genre": "Genre",
    "instrument": "Instrument",
    "vocals": "Vocals",
    "mood": "Mood",
    "production": "Production",
    "tempo": "Tempo",
    "era": "Era",
}
FEATURE_NAMES = ("danceable", "aggressive", "happy", "party", "relaxed", "sad")
ALPHA_STEPS = [1.0, 2.0, 3.0, 5.0, 10.0]  # config.py: CLAP_SAE_ALPHA_STEPS


@dataclass
class FakeSong:
    """A song AudioMuse-AI analyzed. ``ids``: its ID on each media server (by server name)
    - Navidrome's, for the server that is Shijhon's. ``vector``: where it lies, to rank
    neighbours by distance."""

    ids: dict[str, str]
    title: str = "Song"
    artist: str = "Artist"
    album: str = "Album"
    vector: tuple[float, float] = (0.0, 0.0)
    tempo: float = 120.0
    key: str = "A"
    scale: str = "minor"
    energy: float | None = 0.5
    moods: dict[str, float] = field(default_factory=lambda: {"rock": 0.6, "pop": 0.3})
    # The six scores; None: not scored yet (all 0.00, until its CLAP step has run).
    features: dict[str, float] | None = None
    revision: int = 0  # raised when it is analyzed again: the fingerprint changes
    canonical: str = field(default_factory=lambda: "fp_" + uuid.uuid4().hex[:20])

    def mood_vector(self) -> str:
        """database.py: save_track_analysis_and_embedding: ``"label:0.612,..."``."""
        return ",".join(f"{k}:{v:.3f}" for k, v in self.moods.items())

    def other_features(self) -> str:
        """tasks/analysis/song.py: zero_other_features, then compute_other_features_str."""
        scores = self.features or {}
        return ",".join(f"{name}:{scores.get(name, 0.0):.2f}" for name in FEATURE_NAMES)

    def fingerprint(self) -> str:
        """app_sync.py: _FP_SQL - the first 16 characters of an md5 of the analysis."""
        text = "|".join(
            [
                self.mood_vector(),
                "" if self.energy is None else str(self.energy),
                self.other_features(),
                str(self.tempo),
                self.key,
                self.scale,
                str(self.revision),
            ]
        )
        return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()[:16]


class FakeAudioMuse:
    def __init__(
        self,
        token: str,
        *,
        servers: dict[str, str] | None = None,
        default: str = "Shijhon",
    ) -> None:
        self.token = token
        self.servers = servers or {"Shijhon": "srv-1"}  # name -> ID
        self.default = default
        self.songs: list[FakeSong] = []
        self.version = "v3.6.4"
        self.available = True  # /api/clap/concepts: the steering models are loaded
        self.loaded = True  # the CLAP cache is loaded (/api/clap/search: 503 when not)
        self.clap = True  # CLAP_ENABLED
        self.vocabulary = {k: list(v) for k, v in CONCEPTS.items()}
        # The ranking /api/clap/search answers with, by query (lower case): provider IDs on
        # the default server. A steered search uses ``steered`` first, keyed by the query and
        # the sorted (term, direction) pairs. Without either, songs by distance from the
        # query's first word read as a number (a dummy: tests script their rankings).
        self.search_results: dict[str, list[str]] = {}
        self.steered: dict[tuple[str, tuple[tuple[str, str], ...]], list[str]] = {}
        # Statuses and delays by endpoint (``sync``, ``similar_tracks``, ``alchemy``,
        # ``search``, ``concepts``, ``find_path``, ``version``).
        self.statuses: dict[str, int] = {}
        self.delays: dict[str, float] = {}
        self.log: list[dict[str, Any]] = []
        self.at_once = 0  # requests under way now, and at most
        self.most_at_once = 0
        self.lock = threading.Lock()
        self.main = RunningServer(self._app(), lifespan="off")

    # --- control ---------------------------------------------------------------------

    def add(self, *songs: FakeSong) -> None:
        self.songs.extend(songs)

    def start(self) -> None:
        self.main.start()

    def stop(self) -> None:
        self.main.stop()

    @property
    def base_url(self) -> str:
        return self.main.base_url

    def requests(self, endpoint: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.log if endpoint is None or r["endpoint"] == endpoint]

    def clear(self) -> None:
        with self.lock:
            self.log.clear()

    # --- shapes ----------------------------------------------------------------------

    def _server(self, wanted: Any) -> str | None:
        """The name of the server a request means (``resolve_request_server_id``): none named
        is the default; one that is not known raises ValueError."""
        if wanted is None or wanted == "":
            return self.default
        if not isinstance(wanted, str):
            wanted = str(wanted)
        for name, ident in self.servers.items():
            if wanted in (name, ident):
                return name
        raise ValueError("unknown server")

    def _on(self, server: str) -> list[FakeSong]:
        return [s for s in self.songs if server in s.ids]

    def _provider(self, ident: str, server: str) -> FakeSong | None:
        """The song a provider ID of ``server`` means (``resolve_input_item_ids``)."""
        for song in self.songs:
            if song.ids.get(server) == ident or song.canonical == ident:
                return song
        return None

    def _row(self, song: FakeSong, server: str, **more: Any) -> dict[str, Any]:
        """database.py / app_helper.py: serialize_neighbor_results."""
        return {
            "item_id": song.ids[server],
            "title": song.title,
            "author": song.artist,
            "album": song.album,
            "album_artist": song.artist,
            "mood_vector": song.mood_vector(),
            "other_features": song.other_features(),
            "top_genre": next(iter(song.moods), None),
            "top_mood": None,
            **more,
        }

    def _payload(self, song: FakeSong, server: str) -> dict[str, Any]:
        """app_sync.py: _payload_page, without embeddings."""
        return {
            "id": song.ids[server],
            "title": song.title,
            "artist": song.artist,
            "album_artist": song.artist,
            "album": song.album,
            "year": 2001,
            "tempo": song.tempo,
            "key": song.key,
            "scale": song.scale,
            "mood_vector": song.mood_vector(),
            "energy": song.energy,
            "other_features": song.other_features(),
            "rating": 0,
            "umap_x": None,
            "umap_y": None,
            "fp": song.fingerprint(),
        }

    @staticmethod
    def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
        return math.dist(a, b)

    def _nearest(
        self, point: tuple[float, float], server: str, *, skip: Collection[str] = ()
    ) -> list[tuple[FakeSong, float]]:
        ranked = [
            (s, self._distance(point, s.vector))
            for s in self._on(server)
            if s.canonical not in skip
        ]
        return sorted(ranked, key=lambda pair: pair[1])

    # --- server ----------------------------------------------------------------------

    def _app(self) -> Starlette:
        def error(code: int, message: str, status: int, **extra: Any) -> Response:
            """error/responses.py: json_error."""
            return JSONResponse(
                {
                    "error_code": code,
                    "error_class": "Error",
                    "error_message": message,
                    "error": message,
                    **extra,
                },
                status_code=status,
            )

        async def gate(request: Request, endpoint: str) -> Response | None:
            """The log, the request line, the token, and the scripted slowness and status."""
            self._record(request, endpoint)
            line = request.scope.get("raw_path", b"") + b"?" + request.scope["query_string"]
            if len(line) > REQUEST_LINE_LIMIT:
                return Response(status_code=414)
            if request.headers.get("authorization") != f"Bearer {self.token}":
                return error(ERR_UNAUTHORIZED, "Unauthorized", 401)
            if endpoint in self.delays:
                await asyncio.sleep(self.delays[endpoint])
            if endpoint in self.statuses:
                return error(ERR_SEARCH_FAILED, "Failed", self.statuses[endpoint])
            return None

        async def body(request: Request) -> dict[str, Any]:
            data = await request.json()
            with self.lock:
                self.log[-1]["body"] = data
            return data if isinstance(data, dict) else {}

        async def health(request: Request) -> Response:
            self._record(request, "health")
            return JSONResponse({"status": "ok"})

        async def version(request: Request) -> Response:
            if (refused := await gate(request, "version")) is not None:
                return refused
            return JSONResponse(
                {"app_version": self.version}, headers={"cache-control": "no-store"}
            )

        async def sync(request: Request) -> Response:
            if (refused := await gate(request, "sync")) is not None:
                return refused
            try:
                server = self._server(request.query_params.get("server"))
            except ValueError:
                return error(ERR_INVALID_REQUEST, "Invalid server selection.", 400)
            assert server is not None
            args = request.query_params
            manifest = args.get("fields") == "index"
            page = max(1, int(args.get("page", "1")))
            top = 1000 if manifest else 500
            limit = min(max(1, int(args.get("limit", "500"))), top)
            songs = sorted(self._on(server), key=lambda s: s.canonical)
            if "ids" in args:
                wanted = [i for i in args["ids"].split(",") if i][:500]
                songs = [s for s in songs if s.ids[server] in wanted]
                has_more, total = False, len(songs)
            else:
                total = len(songs)
                songs = songs[(page - 1) * limit : page * limit]
                has_more = page * limit < total
            if manifest:
                tracks = [{"id": s.ids[server], "fp": s.fingerprint()} for s in songs]
            else:
                tracks = [self._payload(s, server) for s in songs]
            return JSONResponse(
                {
                    "tracks": tracks,
                    "total_tracks": total,
                    "provider_type": "navidrome",
                    "has_more": has_more,
                    "next_page": page + 1 if has_more else None,
                }
            )

        async def similar_tracks(request: Request) -> Response:
            if (refused := await gate(request, "similar_tracks")) is not None:
                return refused
            try:
                server = self._server(request.query_params.get("server"))
            except ValueError:
                return error(ERR_INVALID_REQUEST, "Invalid server selection.", 400)
            assert server is not None
            ident = request.query_params.get("item_id")
            count = max(1, int(request.query_params.get("n", "50")))
            target = self._provider(ident, server) if ident else None
            if target is None:
                return error(ERR_NOT_FOUND, "Target track not found.", 404)
            ranked = self._nearest(target.vector, server, skip={target.canonical})
            return JSONResponse([self._row(s, server, distance=d) for s, d in ranked[:count]])

        async def alchemy(request: Request) -> Response:
            if (refused := await gate(request, "alchemy")) is not None:
                return refused
            data = await body(request)
            try:
                server = self._server(data.get("server") or request.query_params.get("server"))
            except ValueError:
                return error(ERR_INVALID_REQUEST, "Invalid server selection.", 400)
            assert server is not None
            items = data.get("items", [])
            count = max(1, int(data.get("n", 50)))
            add = [i for i in items if str(i.get("op", "")).upper() == "ADD"]
            sub = [i for i in items if str(i.get("op", "")).upper() == "SUBTRACT"]
            if not add:
                return error(ERR_INVALID_REQUEST, "Invalid request", 400)
            found = [self._provider(str(i["id"]), server) for i in add]
            seeds = [s for s in found if s is not None]
            if not seeds:
                return JSONResponse({"results": [], "filtered_out": []})
            away = [s for s in (self._provider(str(i["id"]), server) for i in sub) if s]
            centre = _centroid([s.vector for s in seeds])
            avoid = _centroid([s.vector for s in away]) if away else None
            skip = {s.canonical for s in seeds + away}
            kept, filtered = [], []
            for song, distance in self._nearest(centre, server, skip=skip):
                near = avoid is not None and self._distance(song.vector, avoid) < 0.2
                (filtered if near else kept).append((song, distance))
            if float(data.get("temperature", 1.0)) > 0:
                random.shuffle(kept)  # tasks/song_alchemy.py: sampled with a softmax
            rows = [self._row(s, server, distance=d, embedding_2d=list(s.vector)) for s, d in kept]
            return JSONResponse(
                {
                    "results": rows[:count],
                    "filtered_out": [self._row(s, server) for s, _ in filtered],
                    "add_points": [],
                    "sub_points": [],
                    "centroid_2d": list(centre),
                    "ignored_anchors": [],
                }
            )

        async def concepts(request: Request) -> Response:
            if (refused := await gate(request, "concepts")) is not None:
                return refused
            categories = [
                {
                    "category": name,
                    "label": LABELS[name],
                    "terms": [{"term": t, "grounding": 0.1} for t in terms],
                }
                for name, terms in self.vocabulary.items()
            ]
            return JSONResponse(
                {
                    "available": self.available,
                    "reason": None if self.available else "the steering models are not loaded",
                    "categories": categories if self.available else [],
                    "alpha_steps": ALPHA_STEPS,
                    "default_alpha": 3.0,
                    "max_terms": 10,
                }
            )

        async def search(request: Request) -> Response:
            if (refused := await gate(request, "search")) is not None:
                return refused
            data = await body(request)
            if not self.clap:
                return error(ERR_INVALID_REQUEST, "CLAP text search is disabled.", 400, results=[])
            try:
                server = self._server(data.get("server") or request.query_params.get("server"))
            except ValueError:
                return error(ERR_INVALID_REQUEST, "Invalid server selection.", 400)
            assert server is not None
            query = data.get("query")
            if not isinstance(query, str) or not query.strip():
                return error(ERR_INVALID_REQUEST, "Missing query", 400)
            if not self.loaded:
                return error(ERR_INDEX_EMPTY, "CLAP cache not loaded.", 503, results=[])
            limit = max(1, int(data.get("limit") or 50))
            # tasks/clap_steering.py: normalize_terms - a term must be in the vocabulary;
            # a list with none valid answers 400, otherwise the valid ones steer.
            known = {t for terms in self.vocabulary.values() for t in terms}
            steering = []
            for item in data.get("steering") or []:
                term = item.get("term") if isinstance(item, dict) else item
                direction = (item.get("direction") if isinstance(item, dict) else None) or "more"
                if term in known and (term, direction) not in steering:
                    steering.append((term, direction))
            if data.get("steering") and not steering:
                return error(ERR_INVALID_REQUEST, "not a validated concept", 400, results=[])
            key = (query.strip().lower(), tuple(sorted(steering)))
            ids = self.steered.get(key) or self.search_results.get(key[0])
            if ids is None:
                ranked = self._nearest((0.0, 0.0), server)
                found = [s for s, _ in ranked]
            else:
                found = [s for s in (self._provider(i, self.default) for i in ids) if s]
            rows = [
                {"item_id": s.ids[server], "title": s.title, "author": s.artist, "similarity": 0.9}
                for s in found[:limit]
                if server in s.ids
            ]
            answer: dict[str, Any] = {"query": query.strip(), "results": rows, "count": len(rows)}
            if steering:
                answer["steering"] = [
                    {"term": t, "weight": 3.0, "direction": d} for t, d in steering
                ]
            return JSONResponse(answer)

        async def find_path(request: Request) -> Response:
            if (refused := await gate(request, "find_path")) is not None:
                return refused
            args = request.query_params
            try:
                server = self._server(args.get("server"))
            except ValueError:
                return error(ERR_INVALID_REQUEST, "Invalid server selection.", 400)
            assert server is not None
            start = self._provider(args.get("start_song_id", ""), server)
            end = self._provider(args.get("end_song_id", ""), server)
            if args.get("start_song_id") == args.get("end_song_id"):
                return error(ERR_INVALID_REQUEST, "Start and end songs cannot be the same.", 400)
            if start is None or end is None:
                return error(ERR_NOT_FOUND, "No path found between the selected songs.", 404)
            steps = min(max(1, int(args.get("max_steps", "25"))), 200)
            path = [start]
            for k in range(1, max(steps - 1, 1)):
                t = k / max(steps - 1, 1)
                point = (
                    start.vector[0] + t * (end.vector[0] - start.vector[0]),
                    start.vector[1] + t * (end.vector[1] - start.vector[1]),
                )
                taken = {s.canonical for s in path} | {end.canonical}
                nearest = self._nearest(point, server, skip=taken)
                if nearest:
                    path.append(nearest[0][0])
            path.append(end)
            # app_path.py: each song carries its (empty) embedding_vector.
            rows = [self._row(s, server, embedding_vector=[]) for s in path]
            return JSONResponse({"path": rows, "total_distance": float(len(rows))})

        def counted(handler: Callable[[Request], Awaitable[Response]]) -> Any:
            async def counting(request: Request) -> Response:
                with self.lock:
                    self.at_once += 1
                    self.most_at_once = max(self.most_at_once, self.at_once)
                try:
                    return await handler(request)
                finally:
                    with self.lock:
                        self.at_once -= 1

            return counting

        routes = [
            Route("/api/health", counted(health)),
            Route("/api/version", counted(version)),
            Route("/api/sync", counted(sync)),
            Route("/api/similar_tracks", counted(similar_tracks)),
            Route("/api/alchemy", counted(alchemy), methods=["POST"]),
            Route("/api/clap/concepts", counted(concepts)),
            Route("/api/clap/search", counted(search), methods=["POST"]),
            Route("/api/find_path", counted(find_path)),
        ]
        return Starlette(routes=routes)

    def _record(self, request: Request, endpoint: str) -> None:
        with self.lock:
            self.log.append(
                {
                    "endpoint": endpoint,
                    "method": request.method,
                    "path": request.url.path,
                    "params": dict(request.query_params),
                    "authorization": request.headers.get("authorization"),
                    "user_agent": request.headers.get("user-agent"),
                    "body": None,
                }
            )


def _centroid(vectors: list[tuple[float, float]]) -> tuple[float, float]:
    return (
        sum(v[0] for v in vectors) / len(vectors),
        sum(v[1] for v in vectors) / len(vectors),
    )
