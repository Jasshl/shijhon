"""Refreshes learn from the owner's edits of the published playlist: invented music, a
scripted Navidrome playlist and a scripted model; no accounts, inference or network."""

from __future__ import annotations

import copy
import json
import sqlite3
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest

from shijhon.playlists import edits as owner_edits
from shijhon.playlists.model import Connection, Definition, PlaylistError, Track, fingerprint
from shijhon.playlists.prompt import Ordered, Proposals
from shijhon.playlists.requests import PlaylistRequest, digest
from shijhon.playlists.service import Playlists
from shijhon.store.db import apply_migrations, load_migrations
from tests.unit.test_playlist_requests import connected, service_at

# The owner's library, as Navidrome answers it.
LIBRARY = {
    "s-amber": ("Amber Coast", "Low Tide"),
    "s-birch": ("Birch Signal", "Paper Kites"),
    "s-cedar": ("Cedar Union", "Slow Engines"),
    "s-dune": ("Dune Parade", "Glass Market"),
    "s-elm": ("Elm Circuit", "Northern Wires"),
    "s-fern": ("Fern Atlas", "Copper Rain"),
    "s-gale": ("Gale Ribbon", "Open Windows"),
    "s-heron": ("Heron Static", "Field Notes"),
}
# Paper Kites is also in the catalog, under a catalog song ID.
CATALOG_KITES = Track(id="sh.tr.demo.kites", artist="Birch Signal", title="Paper Kites")
PUBLISHED = ["s-amber", "s-birch", "s-cedar"]


def song(ident: str) -> dict[str, Any]:
    artist, title = LIBRARY[ident]
    return {"id": ident, "artist": artist, "title": title, "album": "Invented album"}


def track(ident: str) -> Track:
    return Track.model_validate(song(ident))


class Native:
    """Navidrome's playlists and songs as the owner reads them; edits as the owner's app
    makes them."""

    def __init__(self) -> None:
        self.library = dict(LIBRARY)
        self.playlists: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.changes = 0

    def create(self, ident: str, name: str, songs: Sequence[str], owner: str = "owner") -> None:
        self.playlists[ident] = {
            "id": ident,
            "name": name,
            "owner": owner,
            "comment": "",
            "public": False,
            "changed": "2026-10-01T00:00:00Z",
            "entry": [song(i) for i in songs],
        }

    def edit(self, ident: str, *, songs: Sequence[str] | None = None, **details: Any) -> None:
        playlist = self.playlists[ident]
        if songs is not None:
            playlist["entry"] = [song(i) for i in songs]
        playlist.update(details)
        self.changes += 1
        playlist["changed"] = f"2026-10-01T00:{self.changes // 60:02d}:{self.changes % 60:02d}Z"

    async def call(self, connection: Connection, method: str, params: Any = ()) -> Any:
        values = dict(params)
        self.calls.append((method, values))
        if method == "getPlaylists":
            return {
                "playlists": {
                    "playlist": [
                        {k: v for k, v in p.items() if k != "entry"}
                        for p in self.playlists.values()
                    ]
                }
            }
        if method == "getPlaylist":
            found = self.playlists.get(values["id"])
            if found is None or found["owner"] != connection.username:
                raise PlaylistError("Navidrome could not complete the request.")
            return {"playlist": copy.deepcopy(found)}
        if method == "getSong":
            if values["id"] not in self.library:
                raise PlaylistError("Navidrome could not complete the request.")
            return {"song": song(values["id"])}
        raise AssertionError(method)


class Model:
    """Proposes titles, orders the first candidates and records what it was shown."""

    def __init__(self, titles: Sequence[str]) -> None:
        self.titles = list(titles)
        self.seen: list[tuple[type, dict[str, Any], str]] = []

    async def __call__(
        self, connection: Connection, prompt: str, *, schema: Any, instructions: str, **_: Any
    ) -> Any:
        payload = json.loads(prompt)
        self.seen.append((schema, payload, instructions))
        if schema is Proposals:
            by_title = {title: artist for artist, title in LIBRARY.values()}
            return {
                "tracks": [
                    {"artist": by_title[t], "title": t, "album": "", "version": ""}
                    for t in self.titles
                ]
            }
        assert schema is Ordered
        return {
            "goal": "Bright songs for a walk",
            "constraints_satisfied": True,
            "selections": [
                {"id": c["id"], "reason": "Fits", "fit": "meets"}
                for c in payload["candidates"][: payload["count"]]
            ],
        }

    def shown(self, schema: type) -> list[dict[str, Any]]:
        return [payload.get("user_edits", {}) for s, payload, _ in self.seen if s is schema]


@asynccontextmanager
async def refreshing(path: Path) -> AsyncIterator[tuple[Playlists, Native]]:
    async with service_at(path) as service:
        native = Native()
        service.client.call = native.call

        async def resolve(connection: Connection, wanted: dict[str, Any]) -> Track | None:
            if wanted["title"] == CATALOG_KITES.title:
                return CATALOG_KITES
            ident = next((i for i, (_, t) in LIBRARY.items() if t == wanted["title"]), None)
            return track(ident) if ident else None

        service.resolver.resolve = resolve
        yield service, native


REQUEST = PlaylistRequest(
    name="Walking mix",
    prompt="Bright songs for a walk",
    count=3,
    max_per_artist=1,
    save_instructions=True,
    cadence="weekly",
)


def described() -> Definition:
    return REQUEST.definition()


async def publish(
    service: Playlists,
    native: Native,
    mix: str,
    ids: Sequence[str],
    *,
    owner: str = "owner",
    definition: Definition | None = None,
    playlist: str = "",
) -> None:
    """What a verified publication leaves: the native playlist, the mix's fingerprint of
    it and a published run with the frozen selection."""
    definition = definition or described()
    playlist = playlist or f"pl-{mix}"
    if playlist in native.playlists:
        native.edit(playlist, songs=ids)
    else:
        native.create(playlist, definition.name, ids, owner)
    store = service.repo.store
    await store.execute(
        "INSERT INTO playlist_mixes(id,owner,definition) VALUES (?,?,?) ON CONFLICT(id) DO NOTHING",
        [mix, owner, definition.model_dump_json()],
    )
    await store.execute(
        "UPDATE playlist_mixes SET playlist_id=?,published=? WHERE id=?",
        [playlist, fingerprint(native.playlists[playlist]), mix],
    )
    data = {
        "definition": definition.model_dump(),
        "selected": [track(i).model_dump() for i in ids],
        "native_ids": list(ids),
    }
    runs = await store.fetchone("SELECT count(*) AS n FROM playlist_runs WHERE mix=?", [mix])
    run = f"{mix}-{runs['n']}"
    await store.execute(
        "INSERT INTO playlist_runs(id,mix,created,state,data) VALUES (?,?,?,'published',?)",
        [run, mix, time.time() + runs["n"], json.dumps(data)],
    )
    if definition.mode == "prompt":
        # The saved request a described playlist's refreshes check.
        canonical = REQUEST.canonical()
        await store.execute(
            "INSERT INTO playlist_requests"
            "(id,owner,creator,library,request_key,input_hash,input,first_run)"
            " VALUES (?,?,'','1',?,?,?,?) ON CONFLICT(id) DO NOTHING",
            [mix, owner, f"key-{mix}", digest(canonical), canonical, run],
        )


async def refresh(service: Playlists, mix: str, owner: str = "owner") -> dict[str, Any]:
    return await service.generate(
        await service.connection(owner), await service.repo.mix(mix, owner)
    )


def titles(data: dict[str, Any]) -> list[str]:
    return [s["title"] for s in data["selected"]]


def test_a_refresh_learns_removed_and_added_songs_and_leaves_removed_ones_out(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with refreshing(tmp_path / "db") as (service, native):
            await publish(service, native, "walk", PUBLISHED)
            # The owner removes Paper Kites and adds Glass Market in their app.
            native.edit("pl-walk", songs=["s-amber", "s-cedar", "s-dune"])
            model = Model(["Paper Kites", "Northern Wires", "Copper Rain", "Open Windows"])
            service.curator.api = model
            data = await refresh(service, "walk")
            # Paper Kites came back under a catalog ID: the same song, still left out.
            assert "Paper Kites" not in titles(data)
            assert titles(data) == ["Northern Wires", "Copper Rain", "Open Windows"]
            removed = {"artist": "Birch Signal", "title": "Paper Kites", "album": "Invented album"}
            added = {"artist": "Dune Parade", "title": "Glass Market", "album": "Invented album"}
            assert data["edits"] == {
                "learned": {
                    "removed": [{"id": "s-birch", **removed}],
                    "added": [{"id": "s-dune", **added}],
                },
                "remembered": {"removed": 1, "added": 1},
                "left_out": 1,
            }
            # Both the suggestions and the final choice see the edits, with the note.
            shown = {"removed": [removed], "added": [added]}
            assert model.shown(Proposals) == [shown] and model.shown(Ordered) == [shown]
            assert all(owner_edits.NOTE in instructions for *_, instructions in model.seen)
            assert all(
                c["title"] != "Paper Kites"
                for _, payload, _ in model.seen
                for c in payload.get("candidates", [])
            )

    anyio.run(run)


def test_edits_accumulate_between_refreshes_and_the_latest_edit_of_a_song_counts(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with refreshing(tmp_path / "db") as (service, native):
            service.curator.api = Model(
                ["Northern Wires", "Copper Rain", "Open Windows", "Field Notes", "Low Tide"]
            )
            await publish(service, native, "walk", PUBLISHED)
            native.edit("pl-walk", songs=["s-amber", "s-cedar", "s-dune"])
            first = await refresh(service, "walk")
            await publish(service, native, "walk", [s["id"] for s in first["selected"]])
            # Second refresh: Copper Rain removed, and Paper Kites added back.
            native.edit("pl-walk", songs=["s-elm", "s-gale", "s-birch"])
            second = await refresh(service, "walk")
            assert [s["title"] for s in second["edits"]["learned"]["removed"]] == ["Copper Rain"]
            assert [s["title"] for s in second["edits"]["learned"]["added"]] == ["Paper Kites"]
            assert second["edits"]["remembered"] == {"removed": 1, "added": 2}
            remembered = await owner_edits.remembered(service.repo.store, "walk")
            assert [t.title for t in remembered.removed] == ["Copper Rain"]
            assert {t.title for t in remembered.added} == {"Glass Market", "Paper Kites"}
            assert "Copper Rain" not in titles(second)

    anyio.run(run)


def test_no_learning_on_the_first_run_or_without_edits(tmp_path: Path) -> None:
    async def run() -> None:
        async with refreshing(tmp_path / "db") as (service, native):
            model = Model(["Low Tide", "Paper Kites", "Slow Engines"])
            service.curator.api = model
            store = service.repo.store
            new, _ = await service.requests.create("owner", "first-run", REQUEST)
            first = await refresh(service, new)
            assert "edits" not in first
            await publish(service, native, "walk", PUBLISHED)
            unchanged = await refresh(service, "walk")
            assert unchanged["edits"] == {
                "learned": {"removed": [], "added": []},
                "remembered": {"removed": 0, "added": 0},
                "left_out": 0,
            }
            assert model.shown(Proposals) == [{}, {}]
            assert all(owner_edits.NOTE not in instructions for *_, instructions in model.seen)
            assert not native.calls.count(("getSong", {"id": "s-birch"}))
            # Reordering is not an edit of the songs.
            native.edit("pl-walk", songs=list(reversed(PUBLISHED)))
            reordered = await refresh(service, "walk")
            assert reordered["edits"]["learned"] == {"removed": [], "added": []}
            row = await store.fetchone("SELECT count(*) AS n FROM playlist_edits")
            assert row["n"] == 0

    anyio.run(run)


def test_a_song_that_left_the_library_was_not_removed_by_the_owner(tmp_path: Path) -> None:
    async def run() -> None:
        async with refreshing(tmp_path / "db") as (service, native):
            service.curator.api = Model(["Low Tide", "Slow Engines", "Northern Wires"])
            await publish(service, native, "walk", PUBLISHED)
            del native.library["s-birch"]
            native.edit("pl-walk", songs=["s-amber", "s-cedar"])
            data = await refresh(service, "walk")
            assert data["edits"]["learned"] == {"removed": [], "added": []}
            assert ("getSong", {"id": "s-birch"}) in native.calls

    anyio.run(run)


def test_a_rename_stops_the_refresh_and_a_comment_or_visibility_stays(tmp_path: Path) -> None:
    async def run() -> None:
        async with refreshing(tmp_path / "db") as (service, native):
            model = Model(["Northern Wires", "Copper Rain", "Open Windows"])
            service.curator.api = model
            await publish(service, native, "walk", PUBLISHED)
            native.edit("pl-walk", comment="Mine", public=True)
            kept = await refresh(service, "walk")
            assert kept["edits"]["learned"] == {"removed": [], "added": []}
            assert kept["original_name"] == "Walking mix"
            native.edit("pl-walk", name="My walk", songs=["s-amber"])
            calls = len(model.seen)
            with pytest.raises(PlaylistError, match="renamed outside Shijhon"):
                await refresh(service, "walk")
            assert len(model.seen) == calls
            row = await service.repo.store.fetchone("SELECT count(*) AS n FROM playlist_edits")
            assert row["n"] == 0

    anyio.run(run)


def test_a_retry_from_the_same_publication_drops_an_edit_the_owner_undid(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with refreshing(tmp_path / "db") as (service, native):
            service.curator.api = Model(
                ["Paper Kites", "Northern Wires", "Copper Rain", "Open Windows"]
            )
            await publish(service, native, "walk", PUBLISHED)
            native.edit("pl-walk", songs=["s-amber", "s-cedar"])
            failed = await refresh(service, "walk")  # say its publication then failed
            assert failed["edits"]["remembered"] == {"removed": 1, "added": 0}
            native.edit("pl-walk", songs=["s-amber", "s-cedar", "s-birch"])
            again = await refresh(service, "walk")
            assert again["edits"]["remembered"] == {"removed": 0, "added": 0}
            assert "Paper Kites" in titles(again)

    anyio.run(run)


def test_edits_belong_to_one_playlist_and_one_owner(tmp_path: Path) -> None:
    async def run() -> None:
        async with refreshing(tmp_path / "db") as (service, native):
            await service.repo.connect(
                connected(
                    "neighbour", backend="api", endpoint="https://model.invalid/v1", model="m"
                )
            )
            model = Model(["Paper Kites", "Northern Wires", "Copper Rain", "Open Windows"])
            service.curator.api = model
            await publish(service, native, "walk", PUBLISHED)
            await publish(service, native, "other", PUBLISHED)
            await publish(service, native, "theirs", PUBLISHED, owner="neighbour")
            native.edit("pl-walk", songs=["s-amber", "s-cedar"])
            assert (await refresh(service, "walk"))["edits"]["remembered"]["removed"] == 1
            for mix, owner in (("other", "owner"), ("theirs", "neighbour")):
                model.seen.clear()
                data = await refresh(service, mix, owner)
                assert data["edits"]["remembered"] == {"removed": 0, "added": 0}
                assert "Paper Kites" in titles(data)
                assert model.shown(Proposals) == [{}]
            # A disconnect forgets the owner's playlists and their edits.
            await service.repo.disconnect("owner")
            row = await service.repo.store.fetchone("SELECT count(*) AS n FROM playlist_edits")
            assert row["n"] == 0

    anyio.run(run)


def test_remembered_edits_are_bounded_and_the_ai_sees_the_latest(tmp_path: Path) -> None:
    async def run() -> None:
        async with refreshing(tmp_path / "db") as (service, _):
            store = service.repo.store
            await store.execute(
                "INSERT INTO playlist_mixes(id,owner,definition) VALUES ('walk','owner',?)",
                [described().model_dump_json()],
            )

            def many(prefix: str, n: int) -> list[Track]:
                return [
                    Track(id=f"{prefix}-{i}", artist=f"Invented {prefix}", title=f"Song {i}")
                    for i in range(n)
                ]

            await owner_edits.remember(store, "walk", "run-1", many("old", 150), many("kept", 3))
            await owner_edits.remember(store, "walk", "run-2", many("new", 120), [])
            remembered = await owner_edits.remembered(store, "walk")
            assert len(remembered.removed) == owner_edits.KEPT == 200
            assert {t.id for t in remembered.removed[:120]} == {f"new-{i}" for i in range(120)}
            assert len(remembered.added) == 3
            shown = remembered.shown()
            assert len(shown["removed"]) == owner_edits.SHOWN == 100
            assert {s["artist"] for s in shown["removed"]} == {"Invented new"}
            # Every remembered removal still leaves its song out, by ID or by music key.
            assert remembered.leaves_out("", "old-149")
            assert remembered.leaves_out(Track(id="x", artist="invented OLD", title="Song 140").key)
            assert not remembered.leaves_out(
                Track(id="x", artist="Invented old", title="Song 1").key
            )
            assert await owner_edits.forget(store, "walk") == 203
            assert await owner_edits.forget(store, "walk") == 0
            assert (await owner_edits.remembered(store, "walk")).shown() == {}

    anyio.run(run)


def test_discovery_refreshes_leave_removed_songs_out_and_show_edits(tmp_path: Path) -> None:
    async def run() -> None:
        async with refreshing(tmp_path / "db") as (service, native):
            definition = Definition(
                name="Discovery walk",
                count=2,
                unfamiliar_percent=0,
                max_per_artist=1,
                exclude_days=0,
                repeat_days=0,
                brief="Bright songs for a walk",
                ai=True,
            )
            await publish(service, native, "disco", PUBLISHED, definition=definition)
            native.edit("pl-disco", songs=["s-amber", "s-cedar", "s-dune"])
            service.history.sync = AsyncMock(return_value=([], 0))
            wanted = [
                {"artist": LIBRARY[i][0], "title": LIBRARY[i][1], "score": 1.0}
                for i in ("s-birch", "s-elm", "s-fern")
            ]
            service.history.candidates = AsyncMock(return_value=wanted)
            service.history.info = AsyncMock(side_effect=lambda c, w: w)
            seen: list[tuple[dict[str, Any], str]] = []

            async def ai(connection: Connection, prompt: str, **kwargs: Any) -> Any:
                payload = json.loads(prompt)
                seen.append((payload, kwargs["instructions"]))
                return {
                    "selections": [{"id": c["id"], "reason": "x"} for c in payload["candidates"]]
                }

            service.curator.api = ai
            data = await refresh(service, "disco")
            assert titles(data) == ["Northern Wires", "Copper Rain"]
            assert data["edits"]["left_out"] == 1
            ((payload, instructions),) = seen
            assert [s["title"] for s in payload["user_edits"]["removed"]] == ["Paper Kites"]
            assert [s["title"] for s in payload["user_edits"]["added"]] == ["Glass Market"]
            assert owner_edits.NOTE in instructions

    anyio.run(run)


def test_the_request_status_reports_what_a_refresh_learned(tmp_path: Path) -> None:
    async def run() -> None:
        async with refreshing(tmp_path / "db") as (service, native):
            service.curator.api = Model(["Northern Wires", "Copper Rain", "Open Windows"])
            await publish(service, native, "walk", PUBLISHED)
            assert (await service.requests.status("owner", "walk"))["edits"] == {}
            native.edit("pl-walk", songs=["s-amber", "s-cedar"])
            run = await service.repo.enqueue("walk")
            await service.repo.update(run, "preview", await refresh(service, "walk"))
            status = await service.requests.status("owner", "walk")
            assert status["edits"]["learned"]["removed"][0]["title"] == "Paper Kites"

    anyio.run(run)


def test_the_edits_migration_applies_to_an_existing_database() -> None:
    migrations = load_migrations(
        Path(__file__).parents[2] / "src" / "shijhon" / "store" / "migrations"
    )
    before_edits = next(
        i for i, (_, name, _) in enumerate(migrations) if name == "0019_playlist_edits.sql"
    )
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    assert apply_migrations(conn, migrations[:before_edits]) == before_edits
    conn.execute(
        "INSERT INTO playlist_owners(id,username,connection,generation)"
        " VALUES ('owner','admin','{}','g')"
    )
    conn.execute(
        "INSERT INTO playlist_mixes(id,owner,definition,playlist_id,published)"
        " VALUES ('mix','owner','{}','native','print')"
    )
    conn.execute(
        "INSERT INTO playlist_runs(id,mix,created,state,data) VALUES"
        " ('run','mix',1,'published','{\"native_ids\": [\"a\"]}')"
    )
    before = conn.execute("SELECT * FROM playlist_mixes").fetchall()
    runs = conn.execute("SELECT * FROM playlist_runs").fetchall()
    assert apply_migrations(conn, migrations) == migrations[-1][0]
    assert conn.execute("SELECT * FROM playlist_mixes").fetchall() == before
    assert conn.execute("SELECT * FROM playlist_runs").fetchall() == runs
    conn.execute(
        "INSERT INTO playlist_edits(mix,song_key,song_id,kind,artist,title,publication,at)"
        " VALUES ('mix','a:b:','a','removed','A','B','run',1)"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO playlist_edits(mix,song_key,song_id,kind,artist,title,publication,at)"
            " VALUES ('mix','c:d:','c','skipped','C','D','run',1)"
        )
    conn.execute("DELETE FROM playlist_owners WHERE id='owner'")
    assert conn.execute("SELECT count(*) FROM playlist_edits").fetchone()[0] == 0
