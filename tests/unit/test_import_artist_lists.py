"""Artist lists for music imports' files (``music_imports/lists.py``): given when a file is
published, or later as an import operation - a copy of the recorded file takes the
published name, the item's records name it and the file it was stays retained -, and
taken back. The import's records stay valid throughout (``Storage.holds``)."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from mutagen.flac import FLAC

from shijhon.artist_lists.files import ListError
from shijhon.music_imports import tagmap
from shijhon.music_imports.lists import ImportLists, _staging_name
from shijhon.placeholders.tags import ARTIST_ALIASES, ARTISTS_ALIASES, mapped
from tests.harness.library import tone
from tests.unit.test_music_imports import reviewed

pytest_plugins = ("tests.unit.test_music_imports",)
pytestmark = pytest.mark.anyio

JOINED = "Ash Vane, Bell Orrin"
NAMES = ["Ash Vane", "Bell Orrin"]


def tagged(tmp_path: Path, artist: str = JOINED) -> bytes:
    path = tmp_path / "tagged.flac"
    shutil.copyfile(tone(523, 0.3, "flac"), path)
    audio = FLAC(path)
    audio["title"], audio["artist"], audio["album"] = ["Night"], [artist], ["Night"]
    audio.save()
    return path.read_bytes()


def raw_of(data: bytes) -> dict[str, list[str]]:
    handle = io.BytesIO(data)
    audio = tagmap.parse(handle, "flac")
    return tagmap.raw(audio, tagmap.tags_of(audio, handle))


class Navidrome:
    """Navidrome's song as these tests need it: read from the published file."""

    def __init__(self, h: Any) -> None:
        self.h = h

    async def song(self, song_id: str) -> dict[str, Any] | None:
        row = next((r for r in self.h.access.rows if r["id"] == song_id), None)
        if row is None:
            return None
        path = self.h.root / row["path"]
        raw = raw_of(path.read_bytes())
        artists = mapped(raw, ARTISTS_ALIASES) or mapped(raw, ARTIST_ALIASES)
        album = mapped(raw, tagmap.LISTED["albumartists"]) or mapped(
            raw, tagmap.LISTED["albumartist"]
        )
        return {
            **row,
            "size": path.stat().st_size,
            "participants": {
                "artist": [{"name": n} for n in artists],
                "albumartist": [{"name": n} for n in album or artists[:1]],
            },
        }


def listing(h: Any) -> ImportLists:
    lists = ImportLists(h.service, Navidrome(h))  # type: ignore[arg-type]
    h.service.lists = lists
    return lists


def shows(names: list[str]) -> Any:
    def settled(song: dict[str, Any] | None, size: int) -> bool:
        return (
            song is not None
            and song["size"] == size
            and [p["name"] for p in song["participants"]["artist"]] == names
        )

    return settled


async def published(h: Any, data: bytes) -> dict[str, Any]:
    job = await reviewed(h, data)
    await h.service.approve("a", job["id"], job["revision"], "approve")
    await h.service.tick()
    item = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    assert item["state"] == "complete"
    return item


def incoming(h: Any, name: str) -> Path:
    return h.root / "_imports" / ".incoming" / name


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def test_a_file_is_published_with_its_list_and_its_original_retained(harness, tmp_path):
    h = harness
    lists = listing(h)
    asked: list[dict[str, Any]] = []

    async def names(facts, skips=None):
        asked.append(dict(facts))
        return (NAMES, "isrc") if facts.get("artist") == JOINED else None

    lists.publish_names = names
    data = tagged(tmp_path)
    item = await published(h, data)
    assert [f["artist"] for f in asked] == [JOINED]
    file = h.root / "_imports" / item["final_name"]
    raw = raw_of(file.read_bytes())
    assert raw["artists"] == NAMES
    assert raw["artist"] == [JOINED]  # (the text it shows)
    assert raw["albumartist"] == [JOINED]  # (its album as it was)
    assert raw["albumartists"] == NAMES  # (an album of one song, of one list)
    row = await h.store.fetchone("SELECT * FROM artist_lists")
    assert row["key"] == "import:" + item["id"] and row["state"] == "written"
    assert json.loads(row["previous"]) == dict.fromkeys(["artists", "albumartists", "albumartist"])
    assert row["staging_after"] == item["staging_name"] != row["staging_before"]
    # The received original stays as it came; the item's records name the copy.
    assert incoming(h, row["staging_before"]).read_bytes() == data
    assert row["sha_before"] == hashlib.sha256(data).hexdigest()
    assert item["digest"] == sha(file) == row["sha_after"]
    assert h.service.storage.holds(
        item["final_name"], item["staging_name"], item["expected_size"], item["digest"]
    )
    assert item["reserved_size"] == len(data) + item["expected_size"]
    # A publication that goes on after a pause leaves a file given its list as it is.
    job = dict(await h.store.fetchone("SELECT * FROM music_import_jobs"))
    assert await lists.at_publish_batch(job, [dict(item, state="eligible")]) == 0
    assert len(asked) == 1
    assert (await h.store.fetchone("SELECT state FROM artist_lists"))["state"] == "written"


async def test_a_file_without_a_list_is_published_as_it_is(harness, tmp_path):
    h = harness
    lists = listing(h)

    async def none(facts, skips=None):
        return None

    lists.publish_names = none
    data = tagged(tmp_path)
    item = await published(h, data)
    assert (h.root / "_imports" / item["final_name"]).read_bytes() == data
    assert await h.store.fetchall("SELECT * FROM artist_lists") == []


async def test_a_published_file_is_given_its_list_and_back_with_valid_records(harness, tmp_path):
    h = harness
    lists = listing(h)
    data = tagged(tmp_path)
    before = await published(h, data)
    file = h.root / "_imports" / before["final_name"]
    request = {"values": {"artists": NAMES}, "pin": True, "artist": JOINED}
    record = {"artist": JOINED, "source": "link", "artists": NAMES, "album_id": "a1"}
    made = await lists.write(before["song_id"], request, shows(NAMES), record)
    item = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    assert item["upgrade"] == "" and item["upgraded_from"] == ""  # (not an upgrade)
    assert item["staging_name"] != before["staging_name"]
    assert (item["digest"], item["expected_size"]) == (made.sha256, made.size)
    assert item["song_id"] == before["song_id"]
    assert raw_of(file.read_bytes())["artists"] == NAMES
    assert h.service.storage.holds(
        item["final_name"], item["staging_name"], item["expected_size"], item["digest"]
    )
    assert incoming(h, before["staging_name"]).read_bytes() == data
    assert item["reserved_size"] == before["reserved_size"] + made.size
    row = dict(await h.store.fetchone("SELECT * FROM artist_lists"))
    assert (row["state"], row["song_id"], row["album_id"]) == ("written", before["song_id"], "a1")
    assert row["staging_before"] == before["staging_name"]
    # Undone: the original takes the name back, the copy and its reservation go.
    assert await lists.undo(row, shows([JOINED])) == ""
    item = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    for name in ("staging_name", "digest", "expected_size", "reserved_size", "final_name"):
        assert item[name] == before[name], name
    assert file.read_bytes() == data
    assert not incoming(h, row["staging_after"]).exists()
    assert await h.store.fetchall("SELECT * FROM artist_lists") == []


async def test_an_undo_leaves_a_file_that_changed_since(harness, tmp_path):
    h = harness
    lists = listing(h)
    before = await published(h, tagged(tmp_path))
    request = {"values": {"artists": NAMES}, "pin": True, "artist": JOINED}
    await lists.write(before["song_id"], request, shows(NAMES), {"artist": JOINED})
    row = dict(await h.store.fetchone("SELECT * FROM artist_lists"))
    item = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    await h.store.execute(  # (another swap of it since: an upgrade's records)
        "UPDATE music_import_items SET digest=? WHERE id=?", ["0" * 64, item["id"]]
    )
    assert await lists.undo(row, shows([JOINED])) == "declined"
    assert incoming(h, row["staging_after"]).exists()


async def test_a_removed_imports_list_is_not_given_back_while_it_stays_removed(harness, tmp_path):
    h = harness
    lists = listing(h)
    before = await published(h, tagged(tmp_path))
    request = {"values": {"artists": NAMES}, "pin": True, "artist": JOINED}
    await lists.write(before["song_id"], request, shows(NAMES), {"artist": JOINED})
    row = dict(await h.store.fetchone("SELECT * FROM artist_lists"))
    await h.store.execute(
        "INSERT INTO music_import_removals (id, job_id, owner_id, state, include_used, created)"
        " VALUES ('r1', ?, 'a', 'removed', 0, 0)",
        [before["job_id"]],
    )
    item = await lists._item(before["id"])
    assert await lists._undoable(row, item) == "import_removed"
    await h.store.execute("UPDATE music_import_removals SET state = 'removing'")
    assert await lists._undoable(row, item) == "import_busy"


@pytest.mark.parametrize("same_size", [True, False])
async def test_an_import_edited_in_place_since_is_not_given_its_list(harness, tmp_path, same_size):
    h = harness
    lists = listing(h)
    before = await published(h, tagged(tmp_path))
    path = h.root / "_imports" / before["final_name"]
    audio = FLAC(path)  # (a tag editor, through the link to the retained file)
    audio["comment"] = ["edited" if same_size else "edited after the import " * 1000]
    audio.save()
    # (Within the padding only the bytes differ; past it the size too.)
    assert (path.stat().st_size == before["expected_size"]) is same_size
    edited = path.read_bytes()
    request = {"values": {"artists": NAMES}, "pin": True, "artist": JOINED}
    with pytest.raises(ListError) as refused:
        await lists.write(before["song_id"], request, shows(NAMES), {"artist": JOINED})
    assert refused.value.reason == "edited"
    assert path.read_bytes() == edited
    item = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    for name in ("staging_name", "digest", "expected_size", "reserved_size", "upgrade"):
        assert item[name] == before[name], name
    assert await h.store.fetchall("SELECT * FROM artist_lists WHERE state='written'") == []


async def test_a_swap_navidrome_does_not_confirm_is_rolled_back(harness, tmp_path):
    h = harness
    lists = listing(h)
    data = tagged(tmp_path)
    before = await published(h, data)
    request = {"values": {"artists": NAMES}, "pin": True, "artist": JOINED}
    published_file = h.root / "_imports" / before["final_name"]
    old_time = published_file.stat().st_mtime_ns
    os.utime(published_file, ns=(old_time - 10**10, old_time - 10**10))  # (an older file)
    old_time = published_file.stat().st_mtime_ns
    with pytest.raises(Exception, match="not_confirmed"):
        await lists.write(before["song_id"], request, shows(["Someone Else"]), {})
    # Put back with the time of now: Navidrome reads only a file newer than its record.
    assert published_file.stat().st_mtime_ns > old_time
    item = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    for name in ("staging_name", "digest", "expected_size", "reserved_size", "upgrade"):
        assert item[name] == before[name], name
    assert (h.root / "_imports" / item["final_name"]).read_bytes() == data
    assert not incoming(h, _staging_name(before["id"], before["digest"])).exists()
    assert await h.store.fetchall("SELECT * FROM artist_lists WHERE state='written'") == []


async def test_an_undo_put_back_stays_due_until_a_scan_has_run(harness, tmp_path):
    h = harness
    lists = listing(h)
    before = await published(h, tagged(tmp_path))
    request = {"values": {"artists": NAMES}, "pin": True, "artist": JOINED}
    await lists.write(before["song_id"], request, shows(NAMES), {"artist": JOINED})
    row = dict(await h.store.fetchone("SELECT * FROM artist_lists"))
    file = h.root / "_imports" / before["final_name"]
    listed = file.read_bytes()

    async def down():
        raise RuntimeError("scanner down")

    h.before_scan = down  # (no scan confirms the undo, nor reads the copy put back)
    outcome = await lists.undo_many([(row, shows([JOINED]))])
    assert getattr(outcome[row["key"]], "reason", None) == "not_confirmed"
    assert file.read_bytes() == listed  # (the copy back under the published name)
    kept = await h.store.fetchone("SELECT state, phase FROM artist_lists")
    assert (kept["state"], kept["phase"]) == ("written", "undo")  # (a scan still owed)
    h.before_scan = None
    scans = h.scans
    assert await lists.recover() == 1
    assert h.scans == scans + 1
    kept = await h.store.fetchone("SELECT state, phase FROM artist_lists")
    assert (kept["state"], kept["phase"]) == ("written", "")
    assert file.read_bytes() == listed


async def test_a_copy_a_stop_left_at_publication_goes_with_its_reservation(harness, tmp_path):
    h = harness
    lists = listing(h)
    item = await published(h, tagged(tmp_path))
    name = "f" * 32 + ".part"
    incoming(h, name).write_bytes(b"a copy a stop left")
    await h.store.execute(
        "UPDATE music_import_items SET reserved_size=reserved_size+100 WHERE id=?", [item["id"]]
    )
    await h.store.execute(
        "INSERT INTO artist_lists (key, kind, path, item_id, artist, state, reason, phase,"
        " work, updated_at) VALUES (?, 'import', '', ?, ?, 'failed', 'interrupted', 'write',"
        " ?, 0)",
        [
            "import:" + item["id"],
            item["id"],
            JOINED,
            json.dumps({"staging": name, "reserved": 100}),
        ],
    )
    assert await lists.recover() == 1
    assert not incoming(h, name).exists()
    after = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    assert after["reserved_size"] == item["reserved_size"]
    row = await h.store.fetchone("SELECT * FROM artist_lists")
    assert (row["phase"], row["state"]) == ("", "failed")


async def test_the_same_original_imported_again_is_known_by_its_bytes(harness, tmp_path):
    h = harness
    lists = listing(h)

    async def names(facts, skips=None):
        return (NAMES, "isrc")

    lists.publish_names = names
    data = tagged(tmp_path)
    first = await published(h, data)
    lists.publish_names = None
    job = await reviewed(h, data, key="again")
    item = dict(
        await h.store.fetchone(
            "SELECT * FROM music_import_items WHERE job_id=? AND derived_from=''", [job["id"]]
        )
    )
    # (Not published a second time: the earlier import's song, by the received bytes.)
    assert json.loads(item["match"]).get("song_id") == first["song_id"], item["outcome"]


async def test_a_publication_asks_failing_catalogs_a_few_times_only(harness):
    h = harness
    lists = listing(h)
    asked = 0

    async def failing(facts, skips=None):
        nonlocal asked
        asked += 1
        raise TimeoutError

    lists.publish_names = failing
    facts = json.dumps({"artist": JOINED, "taggable": True})
    batch = [{"id": f"i{n}", "state": "eligible", "facts": facts} for n in range(6)]
    assert await lists.at_publish_batch({"id": "j", "owner_id": "a"}, batch) == 0
    assert asked == 3


async def test_a_listed_original_goes_with_a_copy_that_was_never_published(harness, tmp_path):
    """(``_discard_listed``, as the cleanup of a cancelled import calls it.)"""
    h = harness
    lists = listing(h)

    async def names(facts, skips=None):
        return (NAMES, "isrc")

    lists.publish_names = names
    item = await published(h, tagged(tmp_path))
    row = dict(await h.store.fetchone("SELECT * FROM artist_lists"))
    await h.service._discard_listed(item["id"], "another.part")  # (not its copy: kept)
    assert incoming(h, row["staging_before"]).exists()
    final = h.root / "_imports" / item["final_name"]
    final.unlink()  # (as if never placed: the copy has no other link)
    await h.service._discard_listed(item["id"], item["staging_name"])
    assert not incoming(h, row["staging_before"]).exists()
    assert await h.store.fetchall("SELECT * FROM artist_lists") == []


async def album_of_two(h: Any, tmp_path: Path, *, identified: bool = False) -> dict[str, Any]:
    """An approved import of a new album of two songs credited to the joined text (the
    first with MusicBrainz artist IDs, when ``identified``), not published yet."""
    from shijhon.music_imports import ManifestEntry
    from tests.unit.test_music_imports import chunks, declared, freeze

    data = []
    for number in (1, 2):
        path = tmp_path / f"{number}.flac"
        shutil.copyfile(tone(523 + number, 0.3, "flac"), path)
        audio = FLAC(path)
        audio.update(
            title=[f"Night {number}"], artist=[JOINED], album=["Night"], tracknumber=[str(number)]
        )
        if identified and number == 1:
            audio["musicbrainz_artistid"] = ["00000000-0000-4000-8000-000000000001"]
        audio.save()
        data.append(path.read_bytes())
    job = await declared(
        h, [ManifestEntry(str(n), f"{n}.flac", len(b)) for n, b in enumerate(data)]
    )
    for item in job["items"]:
        received = data[int(item["client_key"])]
        await h.service.receive("a", job["id"], item["id"], item["attempt"], chunks(received))
    job = await freeze(h, "a", job["id"])
    await h.service.approve("a", job["id"], job["revision"], "approve")
    return dict(await h.store.fetchone("SELECT * FROM music_import_jobs"))


async def published_album(h: Any) -> list[dict[str, Any] | None]:
    items = await h.store.fetchall("SELECT * FROM music_import_items ORDER BY client_key")
    assert [i["state"] for i in items] == ["complete", "complete"]
    raws = [raw_of((h.root / "_imports" / i["final_name"]).read_bytes()) for i in items]
    for item in items:  # (the records name the files published)
        assert h.service.storage.holds(
            item["final_name"], item["staging_name"], item["expected_size"], item["digest"]
        )
    return [{"artists": r.get("artists"), "albumartists": r.get("albumartists")} for r in raws]


class Stop(BaseException):
    """A stop at a chosen point."""


@pytest.mark.parametrize("given", [1, 2])
async def test_a_publication_stopped_after_a_files_list_completes_the_album(
    harness, tmp_path, monkeypatch, given
):
    """(After one file: the album is decided again. After both: kept, not asked again.)"""
    h = harness
    lists = listing(h)
    asked = 0

    async def names(facts, skips=None):
        nonlocal asked
        asked += 1
        return (NAMES, "isrc")

    lists.publish_names = names
    job = await album_of_two(h, tmp_path)
    batch = [dict(r) for r in await h.store.fetchall("SELECT * FROM music_import_items")]
    publish = lists.at_publish
    done = 0

    async def stop_after(*args, **kwargs):
        nonlocal done
        assert await publish(*args, **kwargs)
        done += 1
        if done == given:
            raise Stop()
        return True

    monkeypatch.setattr(lists, "at_publish", stop_after)
    with pytest.raises(Stop):
        await lists.at_publish_batch(job, batch)
    monkeypatch.undo()
    await h.service.tick()  # (resumed)
    assert await published_album(h) == [{"artists": NAMES, "albumartists": NAMES}] * 2
    assert asked == (2 if given == 2 else 4)  # (the batch asks both files first)


async def test_a_stop_before_the_album_take_back_leaves_no_album_split(
    harness, tmp_path, monkeypatch
):
    from shijhon.artist_lists.files import ListError

    h = harness
    lists = listing(h)

    async def names(facts, skips=None):
        return (NAMES, "isrc")

    lists.publish_names = names
    job = await album_of_two(h, tmp_path)
    batch = [dict(r) for r in await h.store.fetchall("SELECT * FROM music_import_items")]
    refused_id = batch[1]["id"]
    copy = lists._copy
    refused = False

    async def refuse(item, name, request):
        nonlocal refused
        if item["id"] == refused_id:
            refused = True
            raise ListError("identified")
        return await copy(item, name, request)

    async def still():
        if refused:
            raise Stop()  # (right before the take-back)

    monkeypatch.setattr(lists, "_copy", refuse)
    with pytest.raises(Stop):
        await lists.at_publish_batch(job, batch, still)
    await h.service.tick()  # (resumed, the refusal lasting)
    album = await published_album(h)
    assert [a["albumartists"] for a in album] == [None, None]
    assert sorted(bool(a["artists"]) for a in album) == [False, True]
    kept = [p for p in (h.root / "_imports" / ".incoming").iterdir() if p.suffix == ".part"]
    assert len(kept) == 3  # (two originals and one copy: none left behind)


async def test_an_album_whose_file_is_refused_its_list_gets_no_album_list(harness, tmp_path):
    """(All of an album's files get the album artists list or none, also at publication:
    one refused - here for its MusicBrainz artist IDs - and the other is made again.)"""
    h = harness
    lists = listing(h)

    async def names(facts, skips=None):
        return (NAMES, "isrc")

    lists.publish_names = names
    await album_of_two(h, tmp_path, identified=True)
    await h.service.tick()
    items = await h.store.fetchall("SELECT * FROM music_import_items ORDER BY client_key")
    assert [i["state"] for i in items] == ["complete", "complete"]
    raws = [raw_of((h.root / "_imports" / i["final_name"]).read_bytes()) for i in items]
    assert [r.get("albumartists") for r in raws] == [None, None]
    assert [r.get("artists") for r in raws] == [None, NAMES]  # (the other keeps its list)
    for item in items:  # (the records name the files published)
        assert h.service.storage.holds(
            item["final_name"], item["staging_name"], item["expected_size"], item["digest"]
        )
    row = await h.store.fetchone("SELECT * FROM artist_lists WHERE state = 'written'")
    assert row["album_artists"] is None and json.loads(row["artists"]) == NAMES
    assert incoming(h, row["staging_before"]).exists()
    kept = [p for p in (h.root / "_imports" / ".incoming").iterdir() if p.suffix == ".part"]
    assert len(kept) == 3  # (the two originals and one copy: no copy left behind)


async def listed_before_placing(h: Any, tmp_path: Path) -> tuple[dict[str, Any], Path, Path]:
    """An approved file given its list (``at_publish``) and not placed yet: the job, the
    copy and the original the copy was made from."""
    lists = listing(h)
    job = await reviewed(h, tagged(tmp_path))
    await h.service.approve("a", job["id"], job["revision"], "approve")
    item = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    job = dict(await h.store.fetchone("SELECT * FROM music_import_jobs"))
    assert await lists.at_publish(job, item, (NAMES, "isrc"))
    row = dict(await h.store.fetchone("SELECT * FROM artist_lists WHERE state='written'"))
    return job, incoming(h, row["staging_after"]), incoming(h, row["staging_before"])


async def test_a_cancelled_imports_listed_original_stays_counted_until_deleted(
    harness, tmp_path, monkeypatch
):
    import errno

    h = harness
    job, copy, original = await listed_before_placing(h, tmp_path)
    discard = h.service.storage.discard_unpublished

    def full(name: str) -> None:
        if name == original.name:
            raise OSError(errno.ENOSPC, "disk full")
        discard(name)

    monkeypatch.setattr(h.service.storage, "discard_unpublished", full)
    await h.service.cancel("a", job["id"])
    await h.service._release_unpublished()
    item = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    assert original.exists() and not copy.exists()
    assert item["reserved_size"] >= original.stat().st_size  # (still counted)
    assert item["received_cleanup"] != "deleted" and item["received_cleanup_error"]
    assert await h.store.fetchone("SELECT * FROM artist_lists") is not None


async def test_a_cancelled_import_keeps_both_listed_files_counted(harness, tmp_path, monkeypatch):
    """(Cancelling releases an unplaced file's reservation down to its received bytes;
    a file given its list keeps two files, counted until the cleanup deletes them - also
    when the cleanup is refused, which it records and does not retry.)"""
    import errno

    h = harness
    job, copy, original = await listed_before_placing(h, tmp_path)
    both = copy.stat().st_size + original.stat().st_size
    await h.service.cancel("a", job["id"])
    item = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    assert item["reserved_size"] >= both

    def refused(name: str) -> None:
        raise OSError(errno.EIO, "unreadable")

    monkeypatch.setattr(h.service.storage, "discard_unpublished", refused)
    await h.service._release_unpublished()
    item = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    assert copy.exists() and original.exists()
    assert item["reserved_size"] >= both and item["received_cleanup_error"]


async def test_a_copy_taken_back_stays_recorded_and_counted_until_deleted(
    harness, tmp_path, monkeypatch
):
    """(``_unlist_unplaced``: a copy made again without the album artists list.)"""
    import errno

    h = harness
    job, copy, original = await listed_before_placing(h, tmp_path)
    lists = h.service.lists
    item = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    discard = h.service.storage.discard_attempt

    def refused(name: str) -> None:
        if name == copy.name:
            raise OSError(errno.EIO, "unreadable")
        discard(name)

    monkeypatch.setattr(h.service.storage, "discard_attempt", refused)
    with pytest.raises(OSError):
        await lists._unlist_unplaced(item)
    after = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    assert after["staging_name"] == original.name == item["staging_name"]
    assert after["reserved_size"] == item["reserved_size"]  # (the copy still counted)
    row = await h.store.fetchone("SELECT phase, work FROM artist_lists")
    assert row["phase"] == "write" and json.loads(row["work"])["staging"] == copy.name
    monkeypatch.undo()
    await h.service.cancel("a", job["id"])  # (keeps it counted too)
    assert (await h.store.fetchone("SELECT reserved_size FROM music_import_items"))[
        "reserved_size"
    ] == item["reserved_size"]
    assert await lists.recover() == 1
    assert not copy.exists() and original.exists()
    after = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    assert after["reserved_size"] == item["reserved_size"] - json.loads(row["work"])["reserved"]
    assert (await h.store.fetchone("SELECT phase FROM artist_lists"))["phase"] == ""


@pytest.mark.parametrize("refused_again", [False, True])
async def test_a_cancelled_imports_cleanup_deletes_a_pending_taken_back_copy_first(
    harness, tmp_path, monkeypatch, refused_again
):
    """(A take-back whose copy's deletion was refused, then the import cancelled: its
    cleanup deletes that copy before the reservation goes - or, refused again, keeps it.)"""
    import errno

    h = harness
    job, copy, original = await listed_before_placing(h, tmp_path)
    lists = h.service.lists
    item = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    discard = h.service.storage.discard_attempt

    def refused(name: str) -> None:
        if name == copy.name:
            raise OSError(errno.EIO, "unreadable")
        discard(name)

    monkeypatch.setattr(h.service.storage, "discard_attempt", refused)
    with pytest.raises(OSError):
        await lists._unlist_unplaced(item)
    if not refused_again:
        monkeypatch.undo()
    await h.service.cancel("a", job["id"])
    await h.service._release_unpublished()
    after = dict(await h.store.fetchone("SELECT * FROM music_import_items"))
    row = await h.store.fetchone("SELECT phase FROM artist_lists")
    if refused_again:
        assert copy.exists() and after["reserved_size"] >= copy.stat().st_size
        assert after["received_cleanup_error"] and row["phase"] == "write"
    else:
        assert not copy.exists() and not original.exists()
        assert after["reserved_size"] == 0 and after["received_cleanup"] == "deleted"
        assert row["phase"] == ""
        assert await lists.recover() == 0


async def test_a_linked_files_listed_original_goes_before_its_reservation(harness, tmp_path):
    from shijhon.music_imports.overlap import LINKED

    h = harness
    job, copy, original = await listed_before_placing(h, tmp_path)
    # As a recheck leaves it when another import added the song meanwhile.
    await h.store.execute("UPDATE music_import_items SET state='linked', outcome=?", [LINKED])
    await h.service._release_linked(job)
    item = dict(await h.store.fetchone("SELECT reserved_size FROM music_import_items"))
    assert not copy.exists() and not original.exists() and item["reserved_size"] == 0
    assert await h.store.fetchall("SELECT * FROM artist_lists") == []


async def test_a_discarded_copys_listed_original_goes_with_it(harness, tmp_path):
    h = harness
    job, copy, original = await listed_before_placing(h, tmp_path)
    # As a new review leaves a derived copy it gives up (``_reset_overlap``).
    await h.store.execute("UPDATE music_import_items SET state='discarding'")
    await h.service._discard_reset(job["id"])
    assert not copy.exists() and not original.exists()
    assert await h.store.fetchall("SELECT * FROM music_import_items") == []
    assert await h.store.fetchall("SELECT * FROM artist_lists") == []
