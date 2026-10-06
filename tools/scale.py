"""Suite O (scale): many placeholders on a given storage, measured.

Writes synthetic catalog releases (invented names, realistic track counts, lengths and
covers) through Shijhon's own placeholder engine, release by release, as commits write
them: silent FLAC files, a targeted scan and the mandatory check each. Then records

- Navidrome's full-scan time (and a library-wide quick scan), and its targeted-scan
  latency for one added release;
- search3, getAlbumList2 and getAlbum latency through Shijhon, with the catalog off and
  replayed (the sanitized fixtures), next to Navidrome's own;
- Navidrome's and Shijhon's database sizes, and the placeholder folder's size.

    uv run python tools/scale.py --storage /path/on/the/target/storage [--count 20000]

Everything the run writes (the library, Navidrome's and Shijhon's databases) goes into
``<storage>/shijhon-scale``, removed at the end unless ``--keep`` (kept after a failure:
``--resume`` continues it). Results: ``var/scale/<time>-<count>/results.json`` and
``summary.md``. Outside the default test suite; see docs/development.md.
"""

# Synthetic data from seeded generators: nothing here is secret.
# ruff: noqa: S311

from __future__ import annotations

import argparse
import base64
import dataclasses
import fcntl
import json
import logging
import math
import os
import platform
import random
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import statistics
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.harness.navidrome import (
    ADMIN_PASSWORD,
    ADMIN_USER,
    PINNED_VERSION,
    NavidromeInstance,
    binary_version,
    navidrome_binary,
    resolve_version,
)
from tests.harness.replay import Replay, fixtures
from tests.harness.running import RunningServer
from tests.harness.subsonic import SubsonicClient

from shijhon.app import Services, ShijhonApp
from shijhon.catalog.base import Catalog
from shijhon.catalog.model import CatalogRef, CatalogRelease, CatalogTrack, ReleaseKind
from shijhon.config import load_settings
from shijhon.log import RedactingFilter, RedactingFormatter, quiet_libraries
from shijhon.placeholders.engine import MaterializeError, MaterializeResult

log = logging.getLogger("scale")

REPOSITORY = Path(__file__).resolve().parents[1]
RUN_FOLDER = "shijhon-scale"  # inside --storage
PLACEHOLDER_FOLDER = "_shijhon"  # Shijhon's default
DEFAULT_COVER_KB = 320  # a typical 1200 px catalog cover (the default artwork_size)
SEARCH_PAUSE = 0.5  # seconds before each new search
# The environment the runner and its harness read; other SHIJHON_ variables are settings.
OWN_ENV = {
    "SHIJHON_TEST_CACHE",
    "SHIJHON_NAVIDROME_VERSION",
    "SHIJHON_NAVIDROME_BIN",
    "SHIJHON_COMMIT",
}

# --- the synthetic catalog ------------------------------------------------------------

_SYLLABLES = (
    "ka", "lo", "mi", "ren", "dor", "vel", "sa", "tu", "bri", "nox", "el", "ya", "quo",
    "zen", "po", "ri", "an", "te", "vo", "li", "ser", "mar", "dun", "fe", "gal", "hu",
    "is", "jo", "kel", "ny", "or", "pra", "rus", "sol", "tam", "ur", "vin", "wex", "yl",
    "zor",
)  # fmt: skip
_GENRES = ("Rock", "Pop", "Electronic", "Jazz", "Hip-Hop", "Folk", "Classical", "Soul")
# Invented IDs outside the fixtures' range (900000001 and up), ISRCs with an invented prefix.
_RELEASE_IDS = 800_000_000
_TRACK_IDS = 810_000_000


class Generator:
    """Deterministic invented releases: about 70 % albums (9-18 tracks, some on two
    discs), 15 % EPs (4-6) and 15 % singles (1-2); 2-5.5 minutes a track; a few releases
    per artist."""

    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.index = 0  # releases made
        self.tracks = 0  # tracks made
        self.artists: list[str] = []
        self.words: list[str] = []  # title words, for search queries

    def word(self) -> str:
        return "".join(self.rng.choice(_SYLLABLES) for _ in range(self.rng.randint(2, 3))).title()

    def phrase(self, low: int, high: int) -> str:
        words = [self.word() for _ in range(self.rng.randint(low, high))]
        self.words.extend(words)
        return " ".join(words)

    def artist(self) -> str:
        # A new artist for about every fourth release, else one of the earlier ones.
        if not self.artists or self.rng.random() < 0.25:
            name = f"{self.word()} {self.word()}"
            while name in self.artists:
                name = f"{self.word()} {self.word()}"
            self.artists.append(name)
            return name
        return self.rng.choice(self.artists)

    def release(self, tracks: int | None = None) -> CatalogRelease:
        roll = self.rng.random()
        if roll < 0.15:
            kind, count = ReleaseKind.SINGLE, self.rng.randint(1, 2)
        elif roll < 0.30:
            kind, count = ReleaseKind.EP, self.rng.randint(4, 6)
        else:
            kind, count = ReleaseKind.ALBUM, self.rng.randint(9, 18)
        if tracks is not None:
            count = tracks
            kind = ReleaseKind.ALBUM if tracks > 6 else kind
        self.index += 1
        ref = CatalogRef("demo", str(_RELEASE_IDS + self.index))
        artist = self.artist()
        title = self.phrase(1, 3)
        discs = 2 if count >= 12 and self.rng.random() < 0.1 else 1
        per_disc = -(-count // discs)
        date = (
            f"{self.rng.randint(1965, 2026)}-{self.rng.randint(1, 12):02d}"
            f"-{self.rng.randint(1, 28):02d}"
        )
        made = []
        for n in range(count):
            self.tracks += 1
            made.append(
                CatalogTrack(
                    ref=CatalogRef("demo", str(_TRACK_IDS + self.tracks)),
                    title=self.phrase(1, 4),
                    artist=artist,
                    duration_ms=self.rng.randint(120_000, 330_000),
                    disc=n // per_disc + 1,
                    number=n % per_disc + 1,
                    isrc=f"ZZSCL{self.tracks:07d}",
                    album=ref,
                    album_title=title,
                    release_date=date,
                )
            )
        return CatalogRelease(
            ref=ref,
            title=title,
            artist=artist,
            kind=kind,
            release_date=date,
            tracks=tuple(made),
            genres=(self.rng.choice(_GENRES),),
            label=f"{self.word()} Records",
            track_count=count,
        )


def plan(count: int, seed: int) -> tuple[list[CatalogRelease], Generator]:
    """Releases with exactly ``count`` tracks in all (the last one cut to fit)."""
    generator = Generator(seed)
    releases: list[CatalogRelease] = []
    left = count
    while left > 0:
        release = generator.release()
        if len(release.tracks) > left:
            kept = release.tracks[:left]
            release = dataclasses.replace(release, tracks=kept, track_count=len(kept))
        releases.append(release)
        left -= len(release.tracks)
    return releases, generator


# A 16x16 gray baseline JPEG; covers are padded to their size with comment segments of
# random bytes (as incompressible as real image data, for file systems that compress).
_TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAgAAAQABAAD/2wBDAAgKCgsKCw0NDQ0NDRAPEBAQEBAQEBAQEBASEhIVFRUSEhIQEBIS"
    "FBQVFRcXFxUVFRUXFxkZGR4eHBwjIyQrKzP/xABKAAEAAAAAAAAAAAAAAAAAAAAAAQEAAAAAAAAAAAAAAAAA"
    "AAAAEAEAAAAAAAAAAAAAAAAAAAAAEQEAAAAAAAAAAAAAAAAAAAAA/8AAEQgAEAAQAwEiAAIRAAMRAP/aAAwD"
    "AQACEQMRAD8AAA//2Q=="
)


def cover(size: int, seed: int) -> bytes:
    """A valid JPEG of ``size`` bytes (up to 3 fewer; at least the tiny image's own)."""
    padding = max(0, size - len(_TINY_JPEG))
    rng = random.Random(seed)
    segments = bytearray()
    while padding >= 4:
        # A comment segment: FF FE, a length that counts itself (2 bytes), the payload.
        payload = min(65533, padding - 4)
        rest = padding - 4 - payload
        if 0 < rest < 4:
            payload -= 4 - rest  # the last segment then takes exactly 4 bytes
        segments += b"\xff\xfe" + (payload + 2).to_bytes(2, "big") + rng.randbytes(payload)
        padding -= payload + 4
    return _TINY_JPEG[:2] + bytes(segments) + _TINY_JPEG[2:]


def queries(generator: Generator, count: int, seed: int) -> list[str]:
    """Search terms like a client's: artist names, title words, their first letters,
    two-word phrases and terms the library does not have. Distinct, in a fixed order."""
    rng = random.Random(seed)
    words = sorted(set(generator.words))
    kinds: list[Callable[[], str]] = [
        lambda: rng.choice(generator.artists),
        lambda: rng.choice(words),
        lambda: rng.choice(words)[:3].lower(),
        lambda: f"{rng.choice(words)} {rng.choice(words)}",
        lambda: f"{rng.choice(words)}qx",
    ]
    found: list[str] = []
    seen: set[str] = set()
    for attempt in range(count * 50):
        if len(found) == count:
            break
        term = kinds[attempt % len(kinds)]()
        if term.lower() not in seen:
            seen.add(term.lower())
            found.append(term)
    return found


# --- measurements -------------------------------------------------------------------------


def summary(values: Sequence[float], scale: float = 1.0) -> dict[str, Any]:
    """n, min, p50, p95, max and mean (nearest rank), times ``scale``."""
    if not values:
        return {"n": 0}
    ordered = sorted(values)

    def rank(p: float) -> float:
        return ordered[max(0, math.ceil(p * len(ordered)) - 1)]

    return {
        "n": len(ordered),
        "min": round(ordered[0] * scale, 4),
        "p50": round(rank(0.50) * scale, 4),
        "p95": round(rank(0.95) * scale, 4),
        "max": round(ordered[-1] * scale, 4),
        "mean": round(statistics.fmean(ordered) * scale, 4),
    }


class ScanTimes(logging.Handler):
    """Navidrome's targeted-scan times as Shijhon's scan coordinator logs them (a unit test
    holds the coordinator's wording to this pattern)."""

    PATTERN = re.compile(r"targeted scan of \d+ folder\(s\) took ([0-9.]+)s")

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.seconds: list[float] = []

    def emit(self, record: logging.LogRecord) -> None:
        if (match := self.PATTERN.search(record.getMessage())) is not None:
            self.seconds.append(float(match.group(1)))


@contextmanager
def scan_times() -> Iterator[ScanTimes]:
    handler = ScanTimes()
    logger = logging.getLogger("shijhon.navidrome.scans")
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def timed(client: SubsonicClient, method: str, params: dict[str, Any]) -> float:
    """Seconds until the whole answer is read; a failed request raises."""
    started = time.perf_counter()
    response = client.request(method, params)
    body = response.content
    elapsed = time.perf_counter() - started
    status = json.loads(body)["subsonic-response"]["status"] if response.status_code == 200 else ""
    if status != "ok":
        raise RuntimeError(f"{method} failed: HTTP {response.status_code}, {status}")
    return elapsed


@dataclass
class Requests:
    """The requests measured, the same for Navidrome and Shijhon."""

    searches: list[str]
    lists: list[dict[str, Any]]
    albums: list[str]
    warm_search: str  # asked once before the measured searches


def requests_for(generator: Generator, album_ids: list[str], args: argparse.Namespace) -> Requests:
    rng = random.Random(args.seed + 2)
    total = len(album_ids)
    lists: list[dict[str, Any]] = []
    for kind in ("newest", "alphabeticalByName", "random"):
        shapes = [(50, 0), (500, 0)]
        if kind != "random" and total > 500:
            shapes.append((500, total - 500))  # a sync paging through to the end
        lists += [{"type": kind, "size": s, "offset": o} for s, o in shapes]
    terms = queries(generator, args.searches + 1, args.seed + 1)
    return Requests(
        searches=terms[1:],
        lists=lists,
        albums=rng.sample(album_ids, min(args.albums, len(album_ids))),
        warm_search=terms[0],
    )


def list_name(params: dict[str, Any]) -> str:
    where = "end" if params["offset"] else "start"
    return f"getAlbumList2 {params['type']} {params['size']} at the {where}"


def latency(
    nd_client: SubsonicClient, sh_client: SubsonicClient, work: Requests, rounds: int
) -> dict[str, Any]:
    """Each request to Navidrome directly and through Shijhon, one after the other, which
    one first alternating (the second finds the first's warm caches). Searches twice: the
    first time (the catalog asked) and repeated (its answer cached)."""
    for _ in range(3):  # connections, Shijhon's credential cache (a fixed salt), the catalog
        for client in (nd_client, sh_client):
            timed(client, "ping", {})
            timed(client, "getAlbumList2", {"type": "newest", "size": 10})
    timed(sh_client, "search3", {"query": work.warm_search})
    out: dict[str, Any] = {}

    def run(name: str, method: str, calls: Iterable[dict[str, Any]], pause: float = 0) -> None:
        direct: list[float] = []
        through: list[float] = []
        for n, params in enumerate(calls):
            if pause:
                time.sleep(pause)
                for client in (nd_client, sh_client):  # awake again, before the timing
                    timed(client, "ping", {})
            if n % 2:
                through.append(timed(sh_client, method, params))
                direct.append(timed(nd_client, method, params))
            else:
                direct.append(timed(nd_client, method, params))
                through.append(timed(sh_client, method, params))
        out[name] = {"navidrome_ms": summary(direct, 1000), "shijhon_ms": summary(through, 1000)}

    # A pause before each new search, as between a person's searches: back to back, they
    # would wait for the catalog adapter's request pace (10 a second by default).
    run("search3 first", "search3", ({"query": q} for q in work.searches), SEARCH_PAUSE)
    run("search3 repeated", "search3", ({"query": q} for q in work.searches))
    for params in work.lists:
        run(list_name(params), "getAlbumList2", [params] * rounds)
    run("getAlbum", "getAlbum", ({"id": a} for a in work.albums))
    return out


def latency_worker() -> None:
    """``--latency-worker``: the timing client in a process of its own (Shijhon runs in
    the runner's, whose threads would otherwise share an interpreter lock with it). Reads
    its task as JSON on stdin, writes the results as JSON on stdout."""
    task = json.load(sys.stdin)
    salt = secrets.token_hex(6)  # one per session, as a client keeps it
    direct = SubsonicClient(task["navidrome"], task["user"], task["password"], salt=salt)
    through = SubsonicClient(task["shijhon"], task["user"], task["password"], salt=salt)
    try:
        out = latency(direct, through, Requests(**task["work"]), task["rounds"])
    finally:
        direct.close()
        through.close()
    json.dump(out, sys.stdout)


def measure(nd: NavidromeInstance, shijhon: Shijhon, work: Requests, rounds: int) -> Any:
    task = {
        "navidrome": nd.base_url,
        "shijhon": shijhon.server.base_url,
        "user": nd.admin_user,
        "password": nd.admin_password,
        "work": dataclasses.asdict(work),
        "rounds": rounds,
    }
    done = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--latency-worker"],
        input=json.dumps(task),
        capture_output=True,
        text=True,
        timeout=3600,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"the latency measurement failed:\n{done.stderr[-2000:]}")
    return json.loads(done.stdout)


def folder_size(root: Path) -> dict[str, int]:
    """Files, bytes and allocated bytes under ``root`` (not the hidden staging folder)."""
    out = {
        "files": 0,
        "bytes": 0,
        "allocated_bytes": 0,
        "placeholders": 0,
        "placeholder_bytes": 0,
        "covers": 0,
        "cover_bytes": 0,
    }
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            stat = (Path(folder) / name).stat()
            out["files"] += 1
            out["bytes"] += stat.st_size
            out["allocated_bytes"] += getattr(stat, "st_blocks", 0) * 512
            if name.endswith(".flac"):
                out["placeholders"] += 1
                out["placeholder_bytes"] += stat.st_size
            elif name == "cover.jpg":
                out["covers"] += 1
                out["cover_bytes"] += stat.st_size
    return out


def database_size(path: Path) -> dict[str, int]:
    sizes = {
        suffix or "db": (path.with_name(path.name + suffix)).stat().st_size
        for suffix in ("", "-wal", "-shm")
        if path.with_name(path.name + suffix).exists()
    }
    return {**sizes, "total": sum(sizes.values())}


def library_counts(nd: NavidromeInstance) -> dict[str, int]:
    db = sqlite3.connect(f"file:{nd.data / 'navidrome.db'}?mode=ro", uri=True)
    try:
        return {
            table: int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
            for table in ("media_file", "album", "artist", "folder")
        }
    finally:
        db.close()


# --- the run ------------------------------------------------------------------------------


@dataclass
class Shijhon:
    app: ShijhonApp
    server: RunningServer

    @property
    def services(self) -> Services:
        assert self.app.services is not None
        return self.app.services

    def call(self, make: Callable[[], Any], timeout: float = 600) -> Any:
        return self.server.call(make, timeout)

    def rows(self, sql: str) -> list[Any]:
        return list(self.call(lambda: self.services.store.fetchall(sql)))

    def stop(self) -> None:
        self.server.stop()


def start_shijhon(nd: NavidromeInstance, state: Path, catalog: Catalog | None = None) -> Shijhon:
    settings = load_settings(
        None,
        state_dir=state,
        navidrome={
            "url": nd.base_url,
            "user": ADMIN_USER,
            "password": ADMIN_PASSWORD,
            "library_path": nd.music,
        },
        placeholders={"folder": PLACEHOLDER_FOLDER},
        # Measurement settings: no catalog but the one given (the replay), many different
        # searches in a row are no burst to guard against, and no background work (a
        # library pass, covers fetched ahead) runs meanwhile.
        catalog={"kind": "none", "prefetch_covers": 0},
        search={"guard_searches": 1_000_000},
        fill={"library_pass": "off"},
    )
    app = ShijhonApp(settings, catalog=catalog)
    server = RunningServer(app)
    server.start()
    return Shijhon(app, server)


@dataclass
class Written:
    seconds: list[float] = field(default_factory=list)  # a release's materialize
    engine_scan: list[float] = field(default_factory=list)  # write + scan + check
    tracks: list[int] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    scans: list[float] = field(default_factory=list)  # Navidrome's targeted scans
    scans_started: int = 0


def write(
    shijhon: Shijhon, releases: Sequence[CatalogRelease], args: argparse.Namespace
) -> Written:
    """Materialize ``releases`` through the running Shijhon's placeholder engine."""
    engine = shijhon.services.engine
    written = Written()
    total = sum(len(r.tracks) for r in releases)
    started = time.monotonic()
    scans_before = shijhon.services.scans.scans_started
    done_tracks = 0
    step = max(1, total // 20)
    next_report = step

    def one(release: CatalogRelease) -> tuple[CatalogRelease, float, Any]:
        data = cover(args.cover_kb * 1000, int(release.ref.id)) if args.cover_kb else None
        began = time.monotonic()
        try:
            result: MaterializeResult = shijhon.call(
                lambda: engine.materialize(release, cover=data)
            )
        except MaterializeError as exc:
            return release, time.monotonic() - began, exc
        return release, time.monotonic() - began, result

    with scan_times() as scans, ThreadPoolExecutor(max_workers=args.parallel) as pool:
        for release, seconds, outcome in pool.map(one, releases):
            if isinstance(outcome, MaterializeError):
                written.failures.append(f"{release.ref}: {outcome.reason}")
                log.warning("release %s not written: %s", release.ref, outcome.reason)
                continue
            written.seconds.append(seconds)
            written.engine_scan.append(outcome.scan_seconds)
            written.tracks.append(len(release.tracks))
            done_tracks += len(release.tracks)
            if done_tracks >= next_report:
                next_report = (done_tracks // step + 1) * step
                elapsed = time.monotonic() - started
                recent = written.seconds[-50:]
                print(
                    f"  {done_tracks:>6}/{total} placeholders, {elapsed:6.0f}s,"
                    f" last releases {statistics.median(recent):.2f}s each",
                    flush=True,
                )
    written.scans = scans.seconds
    written.scans_started = shijhon.services.scans.scans_started - scans_before
    if written.seconds and not written.scans:
        raise RuntimeError("no targeted-scan times were logged (has the log line changed?)")
    return written


def tenths(values: list[float]) -> list[dict[str, Any]]:
    """p50 and p95 of each tenth of ``values`` (in writing order): growth with the library."""
    if len(values) < 10:
        return []
    size = len(values) / 10
    return [
        summary(values[round(i * size) : round((i + 1) * size)]) | {"tenth": i + 1}
        for i in range(10)
    ]


def git_head() -> str | None:
    """The checkout's commit; ``SHIJHON_COMMIT`` where git cannot tell (a container)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(REPOSITORY), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return os.environ.get("SHIJHON_COMMIT")
    return out.stdout.strip() or os.environ.get("SHIJHON_COMMIT")


def space_needed(count: int, cover_kb: int) -> float:
    """Bytes a run writes, estimated from small runs: about 6 KB a placeholder, 4 KB of
    Navidrome's and 2 KB of Shijhon's database a song, a cover per ten placeholders."""
    return count * (12_000 + cover_kb * 1000 / 10)


def choose_binary(args: argparse.Namespace) -> tuple[Path, str]:
    if args.navidrome_bin:
        path = Path(args.navidrome_bin).resolve()
        found = binary_version(path)
        if args.navidrome_version and resolve_version(args.navidrome_version) != found:
            raise SystemExit(f"{path} is Navidrome {found}, not {args.navidrome_version}")
        return path, found
    version = resolve_version(args.navidrome_version or PINNED_VERSION)
    return navidrome_binary(version), version


def scrub_environment(environ: dict[str, str] | os._Environ[str]) -> list[str]:
    """Remove Shijhon's settings from the environment (the runner sets its own); returns
    the names removed."""
    names = sorted(k for k in environ if k.startswith("SHIJHON_") and k not in OWN_ENV)
    for name in names:
        del environ[name]
    return names


@contextmanager
def run_folder(storage: Path, fingerprint: dict[str, Any], resume: bool) -> Iterator[Path]:
    """``<storage>/shijhon-scale``, new (or, with ``resume``, the kept one of the same
    plan), held exclusively while the run uses it and until it is removed. The lock is a
    file next to it (``.shijhon-scale.lock``, left in place), taken before anything else."""
    root = storage / RUN_FOLDER
    lock_path = storage / f".{RUN_FOLDER}.lock"
    try:  # a regular file, never through a link
        descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o644)
    except OSError as exc:
        raise SystemExit(f"cannot use {lock_path}: {exc.strerror}") from None
    with os.fdopen(descriptor, "w") as lock:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise SystemExit(f"{lock_path} is not a regular file")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"{root} is in use by another run") from None
        if not resume or not (root.exists() or root.is_symlink()):
            needed = space_needed(fingerprint["count"], fingerprint["cover_kb"])
            free = shutil.disk_usage(storage).free
            if free < 1.5 * needed:
                raise SystemExit(
                    f"{storage} has {free / 1e9:.1f} GB free; the run needs about"
                    f" {needed / 1e9:.1f} GB (and a margin)"
                )
            try:
                root.mkdir()
            except FileExistsError:
                raise SystemExit(f"{root} exists: --resume continues it, or remove it") from None
            (root / "plan.json").write_text(json.dumps(fingerprint))
        # Never through a link: everything the run writes and removes stays inside it.
        if (link := first_link(root)) is not None:
            raise SystemExit(f"{link} is a symbolic link")
        plan_file = root / "plan.json"
        saved = json.loads(plan_file.read_text()) if plan_file.is_file() else None
        if saved != fingerprint:
            raise SystemExit(f"{root} was started with {saved}, not {fingerprint}")
        yield root


def first_link(root: Path) -> Path | None:
    """A symbolic link at or anywhere below ``root``, if there is one; a folder that cannot
    be read fails (it could hide one)."""
    if root.is_symlink():
        return root

    def unreadable(error: OSError) -> None:
        raise SystemExit(f"cannot check {error.filename}: {error.strerror}")

    for folder, dirs, files in os.walk(root, onerror=unreadable):
        for name in (*dirs, *files):
            if (path := Path(folder) / name).is_symlink():
                return path
    return None


def drop_unrecorded(root: Path) -> list[str]:
    """Release folders an interrupted run moved into place without recording them (it
    stopped between the files and the record): removed, so that the release is written
    again. Only inside the run folder's placeholder folder."""
    database = root / "shijhon" / "shijhon.sqlite3"
    folder = root / "navidrome" / "music" / PLACEHOLDER_FOLDER
    if not database.is_file() or not folder.is_dir() or folder.is_symlink():
        return []
    db = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        recorded = {str(row[0]) for row in db.execute("SELECT folder FROM releases")}
    finally:
        db.close()
    dropped: list[str] = []
    for artist in sorted(folder.iterdir()):
        if artist.name.startswith(".") or artist.is_symlink() or not artist.is_dir():
            continue
        for release in sorted(artist.iterdir()):
            relative = f"{PLACEHOLDER_FOLDER}/{artist.name}/{release.name}"
            if release.is_dir() and not release.is_symlink() and relative not in recorded:
                shutil.rmtree(release)
                dropped.append(relative)
    return dropped


def run(args: argparse.Namespace, results: dict[str, Any], report: Callable[[], None]) -> None:
    """The measurement; ``results`` is filled as it goes (a failure leaves what was done).
    ``report`` writes them; then, still holding the run folder, a complete run's folder is
    removed unless kept."""
    storage = Path(args.storage).resolve()
    if not storage.is_dir():
        raise SystemExit(f"{storage} is not a folder")
    binary, version = choose_binary(args)
    fingerprint = {
        "count": args.count,
        "seed": args.seed,
        "cover_kb": args.cover_kb,
        "navidrome": version,
    }
    with run_folder(storage, fingerprint, args.resume) as root:
        complete = measure_in(root, storage, binary, version, args, results)
        report()
        if complete and not args.keep:
            try:
                shutil.rmtree(root)
            except OSError as exc:
                print(f"could not remove {root}: {exc}", file=sys.stderr)


def measure_in(
    root: Path,
    storage: Path,
    binary: Path,
    version: str,
    args: argparse.Namespace,
    results: dict[str, Any],
) -> bool:
    """The run in ``root``; False if releases could not be written."""
    releases, generator = plan(args.count, args.seed)
    dropped = drop_unrecorded(root)
    results |= {
        "suite": "O",
        "started": datetime.now(UTC).isoformat(timespec="seconds"),
        "parameters": {
            "count": args.count,
            "storage": str(storage),
            "navidrome_version": version,
            "navidrome_binary": "local" if args.navidrome_bin else "release download",
            "seed": args.seed,
            "cover_kb": args.cover_kb,
            "parallel": args.parallel,
            "searches": args.searches,
            "albums": args.albums,
            "list_rounds": args.list_rounds,
            "full_scans": args.full_scans,
            "added": args.added,
            "resumed": args.resume,
        },
        "environment": {
            "system": f"{platform.system()} {platform.machine()}",
            "cpus": os.cpu_count(),
            "python": platform.python_version(),
            "shijhon_commit": git_head(),
            "storage_free_bytes_before": shutil.disk_usage(storage).free,
        },
        "notes": [
            "Settings changed for the measurement: search.guard_searches 1000000 (the "
            "search guard would answer a run of different searches from the library), "
            "fill.library_pass off and catalog.prefetch_covers 0 (no background work "
            "while measuring); the environment's SHIJHON_ settings are ignored.",
            "Navidrome runs with the test harness's settings: no file watcher, no artwork "
            "precaching, no external services, scans only when asked. Its scans follow the "
            "writing (a warm page cache).",
            f"New searches {SEARCH_PAUSE} s apart (back to back they would wait for the "
            "catalog adapter's request pace); the replayed catalog answers at once, so "
            "its column is Shijhon's own work at this library size, not a catalog's.",
            "The timing client runs in a process of its own, with one salt a session (so "
            "Shijhon's credential cache applies); direct and proxied requests alternate "
            "which goes first.",
            "All releases are catalog albums with a cover; there are no owned albums, so "
            "owned albums shown complete are not measured: their getAlbum and "
            "list answers here are forwarded ones.",
        ],
    }
    if dropped:
        results["notes"].append(
            f"{len(dropped)} release folder(s) of the interrupted run removed and written again."
        )

    nd = NavidromeInstance(root / "navidrome", binary=binary)
    shijhon: Shijhon | None = None
    try:
        nd.start(timeout=600)
        try:
            nd.login()
        except httpx.HTTPStatusError:
            nd.create_admin()  # a new database (or one whose first start stopped early)
        shijhon = start_shijhon(nd, root / "shijhon")
        known = {row["ref"] for row in shijhon.rows("SELECT ref FROM releases")}
        todo = [r for r in releases if str(r.ref) not in known]
        # Releases added at the end: the same ones for a plan (a resumed run does not add
        # others); those an interrupted run added already are not measured again.
        probes = [generator.release(tracks=12) for _ in range(args.added)]
        extra = [r for r in probes if str(r.ref) not in known]
        if len(extra) < len(probes):
            results["notes"].append(
                f"{len(probes) - len(extra)} of the {len(probes)} releases added at the end "
                "were added by the earlier run of this plan and are not measured again."
            )
        print(
            f"Navidrome {version}; writing {sum(len(r.tracks) for r in todo)} placeholders"
            f" in {len(todo)} releases ({len(releases) - len(todo)} written before)",
            flush=True,
        )
        started = time.monotonic()
        written = write(shijhon, todo, args)
        results["write"] = {
            "seconds": round(time.monotonic() - started, 1),
            "releases": len(written.seconds),
            "placeholders": sum(written.tracks),
            "resumed_after_releases": len(releases) - len(todo),
            "failures": written.failures[:20],
            "failed": len(written.failures),
            "scans_started": written.scans_started,
            "release_seconds": summary(written.seconds),
            "engine_scan_seconds": summary(written.engine_scan),
            "navidrome_targeted_scan_seconds": summary(written.scans),
            "release_seconds_by_tenth": tenths(written.seconds),
            "navidrome_targeted_scan_seconds_by_tenth": tenths(written.scans),
        }

        print("scans: quick, full ...", flush=True)
        quick = nd.scan(full=False, timeout=3600)
        full = [nd.scan(full=True, timeout=3600) for _ in range(args.full_scans)]
        added = write(shijhon, extra, args)
        results["scans"] = {
            "quick_seconds": round(quick, 3),
            "full_seconds": [round(s, 3) for s in full],
            "one_added_release": {
                "tracks": 12,
                "release_seconds": summary(added.seconds),
                "engine_scan_seconds": summary(added.engine_scan),
                "navidrome_targeted_scan_seconds": summary(added.scans),
                "scans_started": added.scans_started,
                "failed": len(added.failures),
                "failures": added.failures,
            },
        }
        results["library"] = library_counts(nd)

        album_ids = [
            row["album_id"] for row in shijhon.rows("SELECT album_id FROM releases ORDER BY rowid")
        ]
        work = requests_for(generator, album_ids, args)
        results["latency"] = {}
        print("latency: catalog off ...", flush=True)
        results["latency"]["catalog_off"] = measure(nd, shijhon, work, args.list_rounds)
        shijhon.stop()
        shijhon = None

        replay = Replay()
        recorded = sorted(str(r["params"]["term"]) for r in fixtures() if r["path"] == "search")
        for n, term in enumerate([work.warm_search, *work.searches]):
            replay.aliases[term.lower()] = recorded[n % len(recorded)]
        shijhon = start_shijhon(nd, root / "shijhon", catalog=replay.catalog())
        print("latency: catalog replayed ...", flush=True)
        results["latency"]["catalog_replayed"] = measure(nd, shijhon, work, args.list_rounds)
        asked = Counter(line.split("?")[0].split("/")[0] for line in replay.log)
        results["latency"]["catalog_replayed_requests"] = dict(asked)
        if asked["search"] < len(work.searches) + 1:
            results["notes"].append(
                f"Only {asked['search']} of {len(work.searches) + 1} replayed searches asked "
                "the catalog: the replayed column is partly Navidrome's answer alone."
            )
        shijhon.stop()
        shijhon = None
    finally:
        if shijhon is not None:
            shijhon.stop()
        nd.stop()

    results["sizes"] = {
        "navidrome_db": database_size(nd.data / "navidrome.db"),
        "shijhon_db": database_size(root / "shijhon" / "shijhon.sqlite3"),
        "placeholder_folder": folder_size(nd.music / PLACEHOLDER_FOLDER),
    }
    results["environment"]["storage_free_bytes_after"] = shutil.disk_usage(storage).free
    results["finished"] = datetime.now(UTC).isoformat(timespec="seconds")
    results["navidrome_log"] = str(nd.log_path)
    failed = results["write"]["failed"] + results["scans"]["one_added_release"]["failed"]
    results["complete"] = not failed
    return not failed


# --- the report ---------------------------------------------------------------------------


def _mb(value: int) -> str:
    return f"{value / 1_000_000:,.1f} MB"


def _ms(stats: dict[str, Any], digits: int = 1) -> str:
    if not stats["n"]:
        return "-"
    return " / ".join(f"{stats[k]:.{digits}f}" for k in ("p50", "p95", "max"))


def markdown(results: dict[str, Any]) -> str:
    """The short summary (no paths: it may be quoted in the repository)."""
    p, w, s = results["parameters"], results["write"], results["scans"]
    sizes, lib, env = results["sizes"], results["library"], results["environment"]
    folder = sizes["placeholder_folder"]
    per = folder["placeholder_bytes"] / max(1, folder["placeholders"])
    tenth = w["navidrome_targeted_scan_seconds_by_tenth"]
    growth = f"{tenth[0]['p50']:.2f} s → {tenth[-1]['p50']:.2f} s" if tenth else "-"
    one = s["one_added_release"]
    resumed = (
        f" (resumed: {w['resumed_after_releases']:,} releases written before)"
        if w["resumed_after_releases"]
        else ""
    )
    lines = [
        f"# Suite O (scale): {p['count']:,} placeholders, then {p['added']} release(s) added",
        "",
        f"{results['started']}, Navidrome {p['navidrome_version']}, {env['system']} "
        f"({env['cpus']} CPUs), Shijhon {env['shijhon_commit']}.",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Library | {lib['media_file']:,} songs, {lib['album']:,} albums, "
        f"{lib['artist']:,} artists |",
        f"| Write phase | {w['placeholders']:,} placeholders in {w['releases']:,} releases, "
        f"{w['seconds']:,.0f} s ({w['failed']} failed){resumed} |",
        f"| A release while writing (p50 / p95 / max) | {_ms(w['release_seconds'], 2)} s |",
        f"| Targeted scan while writing, p50 first → last tenth | {growth} |",
        f"| Full scan | {', '.join(f'{x:.1f} s' for x in s['full_seconds'])} |",
        f"| Quick scan (no changes) | {s['quick_seconds']:.2f} s |",
        f"| One added release (12 tracks): whole write, p50 / p95 / max | "
        f"{_ms(one['release_seconds'], 2)} s |",
        f"| One added release: Navidrome's targeted scan, p50 / p95 / max | "
        f"{_ms(one['navidrome_targeted_scan_seconds'], 2)} s |",
        f"| Navidrome database | {_mb(sizes['navidrome_db']['total'])} |",
        f"| Shijhon database | {_mb(sizes['shijhon_db']['total'])} |",
        f"| Placeholder folder | {_mb(folder['bytes'])} ({_mb(folder['allocated_bytes'])} "
        f"allocated): {folder['placeholders']:,} placeholders, {per / 1000:.1f} KB each; "
        f"{folder['covers']:,} covers, {_mb(folder['cover_bytes'])} |",
        "",
        "## Latency, ms (p50 / p95 / max)",
        "",
        "| Request | n | Navidrome | Shijhon, catalog off | Shijhon, catalog replayed |",
        "|---|---|---|---|---|",
    ]
    off = results["latency"]["catalog_off"]
    replayed = results["latency"]["catalog_replayed"]
    for name, row in off.items():
        other = replayed.get(name, {}).get("shijhon_ms", {"n": 0})
        lines.append(
            f"| {name} | {row['navidrome_ms']['n']} | {_ms(row['navidrome_ms'])} | "
            f"{_ms(row['shijhon_ms'])} | {_ms(other)} |"
        )
    lines += ["", *(f"- {note}" for note in results["notes"]), ""]
    return "\n".join(lines)


def stop_on_signal(signum: int, _frame: Any) -> None:
    # Unwinds like Ctrl-C, so that Shijhon and Navidrome are stopped (the folder is kept).
    raise SystemExit(f"stopped by signal {signum}")


def results_folder(base: Path, count: int) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    for n in range(100):
        out = base / (f"{stamp}-{count}" + (f"-{n}" if n else ""))
        try:
            out.mkdir(parents=True)
        except FileExistsError:
            continue
        return out
    raise SystemExit(f"cannot create a results folder in {base}")


def main(argv: Sequence[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--latency-worker"]:
        latency_worker()
        return
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--storage", required=True, help="a folder on the storage to measure")
    parser.add_argument("--count", type=int, default=20_000, help="placeholders (20000)")
    parser.add_argument(
        "--navidrome-version",
        help=f"a Navidrome release, or 'latest' (default: the pinned {PINNED_VERSION})",
    )
    parser.add_argument("--navidrome-bin", help="a local Navidrome binary instead")
    parser.add_argument("--cover-kb", type=int, default=DEFAULT_COVER_KB, help="0: no covers")
    parser.add_argument("--parallel", type=int, default=1, help="releases written at once")
    parser.add_argument("--searches", type=int, default=40)
    parser.add_argument("--albums", type=int, default=100, help="getAlbum requests")
    parser.add_argument("--list-rounds", type=int, default=5, help="requests of each list")
    parser.add_argument("--full-scans", type=int, default=2)
    parser.add_argument("--added", type=int, default=5, help="single releases added at the end")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--results", type=Path, default=REPOSITORY / "var" / "scale")
    parser.add_argument("--keep", action="store_true", help="keep the library afterwards")
    parser.add_argument("--resume", action="store_true", help="continue a kept or failed run")
    args = parser.parse_args(argv)
    if min(args.count, args.parallel, args.searches, args.albums, args.list_rounds) < 1:
        parser.error("--count, --parallel and the sample sizes must be at least 1")
    if min(args.cover_kb, args.full_scans, args.added) < 0:
        parser.error("--cover-kb, --full-scans and --added cannot be negative")

    # Shijhon's own settings come from the runner alone, not from the environment.
    if ignored := scrub_environment(os.environ):
        print(f"ignored for this run: {', '.join(ignored)}", file=sys.stderr)
    signal.signal(signal.SIGTERM, stop_on_signal)
    if signal.getsignal(signal.SIGHUP) is not signal.SIG_IGN:  # (nohup: a hangup stays ignored)
        signal.signal(signal.SIGHUP, stop_on_signal)

    out = results_folder(args.results, args.count)
    evidence = logging.FileHandler(out / "shijhon.log")
    evidence.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    evidence.addFilter(RedactingFilter())
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.WARNING)
    console.setFormatter(RedactingFormatter("%(levelname)s %(name)s: %(message)s"))
    console.addFilter(RedactingFilter())
    logging.getLogger().handlers[:] = [evidence, console]
    logging.getLogger().setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").disabled = True
    quiet_libraries()

    results: dict[str, Any] = {}

    def report() -> None:
        navidrome_log = Path(results.pop("navidrome_log"))
        if navidrome_log.is_file():
            shutil.copyfile(navidrome_log, out / "navidrome.log")
        (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        (out / "summary.md").write_text(markdown(results))

    try:
        run(args, results, report)
    except BaseException:
        if results:
            (out / "results.partial.json").write_text(json.dumps(results, indent=2) + "\n")
            print(f"failed; what was measured: {out / 'results.partial.json'}", file=sys.stderr)
        else:  # nothing started
            logging.getLogger().handlers[:] = [console]
            evidence.close()
            shutil.rmtree(out, ignore_errors=True)
        raise
    print((out / "summary.md").read_text())
    print(f"results: {out}")
    if not results["complete"]:
        raise SystemExit("some releases were not written (see results.json); the run is kept")


if __name__ == "__main__":
    main()
