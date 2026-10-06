"""The demo catalog's records (``shijhon_demo_catalog``: invented data in Shijhon's own
catalog model) with what the suites need around them.

Requests are answered through the catalog interface from the records, by their paths
(``albums/<id>``, ``songs/<id>``, ``artists/<id>``, ``artists/<id>/releases``,
``artists/<id>/top-songs``, searches by their term; ``songs`` and ``albums``: several
items at once - songs by ISRC, and the artist items of what a search showed). Every
request is counted and logged, and a test can make one fail (an HTTP status, as an HTTP
catalog would report it) or take its time, answer a search for its own names with a
recorded one, hide albums from search answers, or change a record: the next answer has it.
"""

from __future__ import annotations

import copy
import json
import time
from importlib import resources

import anyio

from shijhon_demo_catalog import DemoCatalog, Record, failure, load_records
from tests.harness.library import cover_image

REGION = "xx"  # the scope of what the suites save is "demo.xx"


def fixtures() -> list[Record]:
    return load_records()


def fixture(name: str) -> Record:
    """One record by its name, e.g. ``album-twins-clean``: ``path`` (``albums/<id>``),
    ``body`` (the release as ``catalog.model.release_data`` writes it), a search's
    ``params["term"]``."""
    file = resources.files("shijhon_demo_catalog") / "records" / f"{name}.json"
    record: Record = json.loads(file.read_text(encoding="utf-8"))
    return record


class Replay:
    def __init__(self, *, delay: float = 0.0) -> None:
        records = fixtures()
        self.by_path = {r["path"]: r for r in records if r["path"] != "search"}
        self.searches = {r["params"]["term"].lower(): r for r in records if r["path"] == "search"}
        self.delay = delay
        self.log: list[str] = []  # the requests (a search with its term, an ISRC lookup too)
        self.times: list[float] = []  # when each of them was made (monotonic)
        self.artwork_requests = 0
        self.artwork_urls: list[str] = []  # the image addresses asked for, in order
        # What the catalog answers an image request with (None: a JPEG, named so).
        self.artwork_answer: tuple[bytes, str] | None = None
        self.failing: dict[str, int] = {}  # request -> HTTP status to fail with
        self.slow: dict[str, float] = {}  # request -> extra delay in seconds
        # Search term -> recorded term to answer with (the recordings' terms are invented).
        self.aliases: dict[str, str] = {}
        self.hidden: set[str] = set()  # album IDs left out of recorded search answers

    @property
    def api_requests(self) -> int:
        return len(self.log)

    def catalog(self) -> ReplayCatalog:
        return ReplayCatalog(self)


class ReplayCatalog(DemoCatalog):
    """The demo catalog over a replay's records, counted and scripted by it. Only the
    recorded searches answer (``names`` off); covers are the harness's test image."""

    def __init__(self, replay: Replay) -> None:
        super().__init__((), region=REGION, names=False)
        self.replay = replay
        self.by_path = replay.by_path  # shared: a test changes a record
        self.searches = replay.searches

    async def asked(self, key: str, entry: str) -> None:
        replay = self.replay
        if replay.delay:
            await anyio.sleep(replay.delay)
        replay.times.append(time.monotonic())
        replay.log.append(entry)
        if key in replay.slow:
            await anyio.sleep(replay.slow[key])
        if key in replay.failing:
            raise failure(replay.failing[key])

    def search_record(self, term: str) -> Record | None:
        wanted = term.lower()
        record = self.searches.get(self.replay.aliases.get(wanted, wanted))
        if record is None or not self.replay.hidden or record.get("status", 200) != 200:
            return record
        record = copy.deepcopy(record)
        albums = record["body"].get("albums", [])
        record["body"]["albums"] = [a for a in albums if a["ref"]["id"] not in self.replay.hidden]
        return record

    async def artwork_asked(self, url: str) -> None:
        if self.replay.delay:
            await anyio.sleep(self.replay.delay)
        self.replay.artwork_requests += 1
        self.replay.artwork_urls.append(url)

    async def artwork(self, url: str) -> tuple[bytes, str]:
        await super().artwork(url)  # the address is checked, the request counted
        if self.replay.artwork_answer is not None:
            return self.replay.artwork_answer
        return cover_image("blue").read_bytes(), "image/jpeg"
