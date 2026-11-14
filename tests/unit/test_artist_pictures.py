"""Taking a picture back when Navidrome's external services name one (``ArtistPictures``),
with a stand-in for Navidrome's native API: the test Navidrome runs without external
services, so it never names one (the uploads themselves: ``test_G_artist_pictures``)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from shijhon.artist_pictures import ArtistPictures, same_name
from shijhon.catalog.base import CatalogError, SearchResults
from shijhon.catalog.model import CatalogArtist, CatalogRef, CatalogRelease, release_data
from shijhon.delivery import pacing
from shijhon.navidrome.client import NavidromeError
from shijhon.store import Store
from tests.harness.engine import catalog_release

pytestmark = pytest.mark.anyio


JPEG = b"\xff\xd8\xff\xe0" + b"0" * 16


@pytest.fixture(autouse=True)
def no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shijhon.artist_pictures.PAUSE_SECONDS", 0.0)


class FakeNavidrome:
    def __init__(self) -> None:
        self.state: dict[str, dict[str, Any]] = {
            "a1": {"id": "a1", "name": "Wren Hollow", "imageAbsent": True}
        }
        self.uploads = 0
        self.hash_later = False  # Navidrome reads an upload only after Shijhon stops waiting

    async def artists(self) -> list[dict[str, Any]]:
        return [dict(a) for a in self.state.values()]

    async def artist(self, ident: str) -> dict[str, Any] | None:
        return dict(self.state[ident]) if ident in self.state else None

    async def artist_songs(self, ident: str, limit: int = 500) -> list[dict[str, Any]]:
        return [{"id": "s1", "title": "Quiet Engines", "album": "Quiet Engines"}]

    async def set_artist_image(self, ident: str, image: bytes, content_type: str) -> None:
        self.uploads += 1
        artist = self.state[ident]
        artist.update(
            uploadedImage=f"{ident}_x.png", imageAbsent=False, updatedAt=f"u{self.uploads}"
        )
        if not self.hash_later:
            artist["imageHash"] = f"h{self.uploads}"

    async def remove_artist_image(self, ident: str) -> None:
        artist = self.state[ident]
        artist.pop("uploadedImage", None)
        artist.pop("imageHash", None)
        artist["imageAbsent"] = True  # (until Navidrome resolves its own)


class OneArtist:
    key = "test"
    region = ""

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        return SearchResults(
            artists=(
                CatalogArtist(
                    CatalogRef("test", "w"), "Wren Hollow", "https://p.invalid/{w}x{h}.jpg"
                ),
            )
        )

    async def artist_releases(self, artist_id: str) -> tuple[CatalogRelease, ...]:
        from tests.harness.engine import catalog_release

        return (catalog_release("w1", "Quiet Engines", "Wren Hollow", 1),)

    async def artwork(self, url: str) -> tuple[bytes, str]:
        return JPEG, "image/jpeg"


@pytest.fixture
async def store(tmp_path: Path) -> Any:
    opened = await Store.open(tmp_path / "shijhon.sqlite3")
    try:
        yield opened
    finally:
        await opened.close()


async def state_of(store: Store) -> str:
    [row] = await store.fetchall("SELECT state FROM artist_pictures")
    return str(row["state"])


async def test_navidromes_own_picture_takes_over_and_an_unusable_one_does_not(store: Store) -> None:
    nd = FakeNavidrome()
    pictures = ArtistPictures(nd, OneArtist(), store)  # type: ignore[arg-type]
    assert await pictures.once() == 1
    assert await state_of(store) == "set"

    nd.state["a1"]["largeImageUrl"] = "https://external.invalid/wren.jpg"  # found later
    assert await pictures.once() == 0
    assert "uploadedImage" not in nd.state["a1"]
    assert await state_of(store) == "withdrawn"

    # Navidrome could not use it after all: Shijhon's again, and it stays for that address.
    assert await pictures.once() == 1
    assert await pictures.once() == 0
    assert nd.state["a1"].get("uploadedImage") and await state_of(store) == "set"

    nd.state["a1"]["largeImageUrl"] = "https://external.invalid/another.jpg"  # a new one
    await pictures.once()
    assert await state_of(store) == "withdrawn"


async def test_a_picture_navidrome_could_not_use_when_set_is_kept(store: Store) -> None:
    nd = FakeNavidrome()
    nd.state["a1"]["largeImageUrl"] = "https://external.invalid/broken.jpg"
    pictures = ArtistPictures(nd, OneArtist(), store)  # type: ignore[arg-type]
    assert await pictures.once() == 1
    assert await pictures.once() == 0
    assert nd.state["a1"].get("uploadedImage") and await state_of(store) == "set"


async def test_without_a_catalog_the_pictures_set_stay(store: Store) -> None:
    nd = FakeNavidrome()
    assert await ArtistPictures(nd, OneArtist(), store).once() == 1  # type: ignore[arg-type]
    assert await ArtistPictures(nd, None, store).once() == 0  # type: ignore[arg-type]
    assert nd.state["a1"].get("uploadedImage") and await state_of(store) == "set"


class TwoArtists(OneArtist):
    """Two artists of the catalog; the first one's picture address always fails."""

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        ref = "bad" if term == "Ansel Grey" else "w"
        return SearchResults(
            artists=(
                CatalogArtist(
                    CatalogRef("test", ref), term, f"https://p.invalid/{ref}/{{w}}x{{h}}.jpg"
                ),
            )
        )

    async def artwork(self, url: str) -> tuple[bytes, str]:
        if "/bad/" in url:
            raise CatalogError("unavailable", "the image host answered 500")
        return JPEG, "image/jpeg"


async def test_one_failing_artist_does_not_hold_up_the_others(store: Store) -> None:
    nd = FakeNavidrome()
    nd.state = {
        "a0": {"id": "a0", "name": "Ansel Grey", "imageAbsent": True},
        "a1": {"id": "a1", "name": "Wren Hollow", "imageAbsent": True},
    }
    pictures = ArtistPictures(nd, TwoArtists(), store)  # type: ignore[arg-type]
    assert await pictures.once() == 1
    assert nd.state["a1"].get("uploadedImage") and not nd.state["a0"].get("uploadedImage")
    [row] = await store.fetchall("SELECT * FROM artist_pictures WHERE artist_id = 'a0'")
    assert row["state"] == "unmatched" and row["retry_at"] - row["updated_at"] == pytest.approx(
        86400, abs=1
    )


async def test_what_is_not_an_image_is_not_uploaded(store: Store) -> None:
    class Script(OneArtist):
        async def artwork(self, url: str) -> tuple[bytes, str]:
            return b"<svg onload='x'/>", "image/jpeg"  # (the claim is not believed)

    nd = FakeNavidrome()
    assert await ArtistPictures(nd, Script(), store).once() == 0  # type: ignore[arg-type]
    assert nd.uploads == 0


async def test_a_refusal_outlasts_turning_the_setting_off_and_on(store: Store) -> None:
    nd = FakeNavidrome()
    pictures = ArtistPictures(nd, OneArtist(), store)  # type: ignore[arg-type]
    assert await pictures.once() == 1
    nd.state["a1"].update(imageHash="the-persons", updatedAt="later")  # replaced by a person
    await pictures.once()
    assert await state_of(store) == "declined"
    pictures.enabled = False
    await pictures.once()
    pictures.enabled = True
    await nd.remove_artist_image("a1")  # the person removes theirs too
    assert await pictures.once() == 0
    assert await state_of(store) == "declined"


async def test_an_unread_upload_of_a_changed_artist_is_left_until_it_can_be_told(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shijhon.artist_pictures.HASH_WAIT_SECONDS", 0.05)
    nd = FakeNavidrome()
    nd.hash_later = True  # Navidrome does not read the image in time
    pictures = ArtistPictures(nd, OneArtist(), store)  # type: ignore[arg-type]
    assert await pictures.once() == 1
    [row] = await store.fetchall("SELECT * FROM artist_pictures")
    assert row["image_hash"] is None
    nd.state["a1"]["updatedAt"] = "a person saved something"  # same name, unread content
    nd.state["a1"]["largeImageUrl"] = "https://external.invalid/w.jpg"
    pictures.enabled = False
    await pictures.once()
    assert nd.state["a1"].get("uploadedImage")  # not removed: it may be the person's


class ByName(OneArtist):
    """Every artist asked about is found by its name, with the library's album."""

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        ref = CatalogRef("test", term)
        return SearchResults(artists=(CatalogArtist(ref, term, "https://p.invalid/{w}x{h}.jpg"),))


async def test_every_artist_due_is_asked_in_one_pass_with_a_pause_between(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    nd = FakeNavidrome()
    nd.state = {
        f"a{n}": {"id": f"a{n}", "name": f"Lumen Archer {n}", "imageAbsent": True}
        for n in range(250)
    }
    pauses: list[float] = []
    real_sleep = anyio.sleep

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr("shijhon.artist_pictures.anyio.sleep", sleep)
    pictures = ArtistPictures(nd, ByName(), store, pause=0.5)  # type: ignore[arg-type]
    assert await pictures.once() == 250  # (no cap on a pass)
    assert pauses == [0.5] * 249  # one between two artists, none before the first
    assert await pictures.once() == 0


async def test_turning_the_setting_off_ends_a_pass(store: Store) -> None:
    nd = FakeNavidrome()
    nd.state = {
        f"a{n}": {"id": f"a{n}", "name": f"Lumen Archer {n}", "imageAbsent": True} for n in range(5)
    }

    class Off(ByName):
        async def artwork(self, url: str) -> tuple[bytes, str]:
            pictures.enabled = False  # turned off while the first is given
            return JPEG, "image/jpeg"

    pictures = ArtistPictures(nd, Off(), store)  # type: ignore[arg-type]
    assert await pictures.once() == 1
    assert nd.uploads == 1


def test_a_name_with_its_words_in_another_order_is_the_same() -> None:
    assert same_name("Ilka Moreno", "Moreno Ilka")
    assert same_name("Ilka  Moréno", "ilka moreno")
    assert not same_name("Ilka Moreno", "Ilka")
    assert not same_name("Ilka Moreno", "Ilka Moreno Trio")
    assert not same_name("", "")


async def linked(store: Store, song: str, release: CatalogRelease) -> None:
    """A library song linked to the first track of a catalog release."""
    await store.execute(
        "INSERT INTO releases (ref, folder, album_id, title, artist, album_tags, data,"
        " created_at) VALUES (?, ?, ?, ?, ?, '{}', ?, 0)",
        [
            str(release.ref),
            f"folder-{release.ref.id}",
            f"al-{release.ref.id}",
            release.title,
            release.artist,
            json.dumps(release_data(release)),
        ],
    )
    await store.execute(
        "INSERT INTO track_links (track_ref, song_id, release_ref, owned) VALUES (?, ?, ?, 1)",
        [str(release.tracks[0].ref), song, str(release.ref)],
    )


class Items(OneArtist):
    """Catalog artists by item; a search finds nothing."""

    def __init__(self, *artists: CatalogArtist) -> None:
        self.artists = {a.ref.id: a for a in artists}

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        return SearchResults()

    async def artist(self, artist_id: str) -> CatalogArtist:
        if artist_id not in self.artists:
            raise CatalogError("not_found", "no such artist")
        return self.artists[artist_id]


def one_credit(key: str, artist: str, ref: CatalogRef) -> CatalogRelease:
    release = catalog_release(key, "Glass Tide", artist, 1)
    track = replace(release.tracks[0], artist_refs=(ref,))
    return replace(release, artist_refs=(ref,), tracks=(track,))


def songs_credited(credit: str) -> Any:
    async def songs(ident: str, limit: int = 500) -> list[dict[str, Any]]:
        return [
            {
                "id": "s1",
                "title": "Glass Tide Song 1",
                "album": "Glass Tide",
                "artist": credit,
                "albumArtist": credit,
            }
        ]

    return songs


async def picture_from_a_link(store: Store, library: str, catalog_name: str) -> int:
    """The library artist ``library`` with one song, linked to a catalog track credited to
    ``catalog_name`` alone; a search finds nothing."""
    ref = CatalogRef("test", "km")
    nd = FakeNavidrome()
    nd.state = {"a1": {"id": "a1", "name": library, "imageAbsent": True}}
    nd.artist_songs = songs_credited(library)  # type: ignore[method-assign]
    await linked(store, "s1", one_credit("gt", catalog_name, ref))
    catalog = Items(CatalogArtist(ref, catalog_name, "https://p.invalid/{w}x{h}.jpg"))
    return await ArtistPictures(nd, catalog, store).once()  # type: ignore[arg-type]


async def test_a_linked_credit_with_the_names_words_in_another_order(store: Store) -> None:
    assert await picture_from_a_link(store, "Mirei Kaito", "Kaito Mirei") == 1
    [row] = await store.fetchall("SELECT state, catalog_ref FROM artist_pictures")
    assert (row["state"], row["catalog_ref"]) == ("set", "test:km")


async def test_a_joined_name_does_not_take_one_of_its_artists_picture(store: Store) -> None:
    assert await picture_from_a_link(store, "Mirei Kaito, Juno Vale", "Kaito Mirei") == 0


async def test_another_name_is_not_taken_from_a_link_alone(store: Store) -> None:
    # (another script, or a remix or cover linked to the original artist's track)
    assert await picture_from_a_link(store, "Kaito Mirei", "海斗 美玲") == 0


async def test_a_stop_right_after_the_upload_still_records_it(store: Store) -> None:
    nd = FakeNavidrome()
    upload = nd.set_artist_image
    with anyio.CancelScope() as scope:

        async def stopped_while_uploading(ident: str, image: bytes, content_type: str) -> None:
            scope.cancel()
            await upload(ident, image, content_type)

        nd.set_artist_image = stopped_while_uploading  # type: ignore[method-assign]
        await ArtistPictures(nd, OneArtist(), store).once()  # type: ignore[arg-type]
    assert nd.uploads == 1
    assert await state_of(store) == "set"  # Shijhon's, so taken back when turned off


class FailingAfterUpload(FakeNavidrome):
    """Navidrome fails once: its answer to the upload (after keeping it), or the
    ``fail_on``-th read of the artist after it."""

    def __init__(self, fail_on: int = 0, upload_fails: bool = False) -> None:
        super().__init__()
        self.hash_later = True
        self.fail_on, self.upload_fails = fail_on, upload_fails
        self.reads = 0
        self.armed = False

    async def set_artist_image(self, ident: str, image: bytes, content_type: str) -> None:
        await super().set_artist_image(ident, image, content_type)
        self.armed = True
        if self.upload_fails:
            raise NavidromeError("timed out")

    async def artist(self, ident: str) -> dict[str, Any] | None:
        if self.armed:
            self.reads += 1
            if self.reads == self.fail_on:
                raise NavidromeError("temporary")
        return await super().artist(ident)


@pytest.fixture
def quick_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shijhon.artist_pictures.HASH_WAIT_SECONDS", 0.05)
    monkeypatch.setattr("shijhon.artist_pictures.READ_PAUSES", (0.0, 0.0))


@pytest.mark.parametrize(
    ("fail_on", "upload_fails"), [(1, False), (0, True)], ids=["read", "upload-answer"]
)
async def test_an_upload_read_back_after_a_failure_is_recorded(
    store: Store, quick_reads: None, fail_on: int, upload_fails: bool
) -> None:
    # A read that failed once is tried again; an upload Navidrome kept without answering
    # is recorded as Shijhon's when the read shows it.
    nd = FailingAfterUpload(fail_on, upload_fails)
    pictures = ArtistPictures(nd, OneArtist(), store)  # type: ignore[arg-type]
    assert await pictures.once() == 1
    [row] = await store.fetchall("SELECT * FROM artist_pictures")
    assert (row["state"], row["image"]) == ("set", "a1_x.png")


NOW = 1_800_000_000.0  # the tests' clock, in seconds


def iso(seconds: float) -> str:
    """A time as Navidrome writes ``updatedAt``."""
    return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")


class Outage(FakeNavidrome):
    """Navidrome keeps the upload (unless ``kept`` is off; ``updatedAt`` the clock's time),
    then is unreachable for ``reads`` reads and, with ``removal_fails``, for the removal;
    ``answer``: the upload's own error."""

    def __init__(
        self,
        now: list[float],
        *,
        reads: int = 99,
        removal_fails: bool = True,
        kept: bool = True,
        answer: NavidromeError | None = None,
    ) -> None:
        super().__init__()
        self.now = now
        self.reads, self.removal_fails, self.kept, self.answer = reads, removal_fails, kept, answer
        self.down = False
        self.removals = 0

    async def set_artist_image(self, ident: str, image: bytes, content_type: str) -> None:
        if self.kept:
            await super().set_artist_image(ident, image, content_type)
            self.state[ident]["updatedAt"] = iso(self.now[0] + 1)
        else:
            self.uploads += 1
        self.down = True
        if self.answer is not None:
            raise self.answer

    async def artist(self, ident: str) -> dict[str, Any] | None:
        if self.down and self.reads > 0:
            self.reads -= 1
            raise NavidromeError("down")
        return await super().artist(ident)

    async def remove_artist_image(self, ident: str) -> None:
        self.removals += 1
        if self.down and self.removal_fails:
            raise NavidromeError("down")
        await super().remove_artist_image(ident)


def at_now(nd: FakeNavidrome, catalog: Any, store: Store, now: list[float]) -> ArtistPictures:
    return ArtistPictures(nd, catalog, store, clock=lambda: now[0])  # type: ignore[arg-type]


async def test_an_upload_that_cannot_be_read_back_is_removed(
    store: Store, quick_reads: None
) -> None:
    now = [NOW]
    nd = Outage(now, removal_fails=False)
    pictures = at_now(nd, OneArtist(), store, now)
    assert await pictures.once() == 0
    assert nd.uploads == 1 and nd.removals == 1 and not nd.state["a1"].get("uploadedImage")
    [row] = await store.fetchall("SELECT * FROM artist_pictures")
    assert (row["state"], row["uploaded_at"]) == ("unmatched", None)
    assert row["retry_at"] - row["updated_at"] == pytest.approx(86400, abs=1)


@pytest.mark.parametrize("later", ["kept", "gone"])
async def test_an_upload_neither_recorded_nor_removed_is_settled_by_the_next_pass(
    store: Store, quick_reads: None, later: str
) -> None:
    now = [NOW]
    nd = Outage(now)
    tries: list[str] = []

    class Counting(OneArtist):
        async def artwork(self, url: str) -> tuple[bytes, str]:
            tries.append(url)
            return await super().artwork(url)

    pictures = at_now(nd, Counting(), store, now)
    assert await pictures.once() == 0
    [row] = await store.fetchall("SELECT * FROM artist_pictures")
    assert row["state"] == "unmatched" and row["uploaded_at"] and row["catalog_ref"] == "test:w"
    nd.down = False  # Navidrome is back
    now[0] += 60
    if later == "gone":
        await nd.remove_artist_image("a1")  # (no upload stayed after all)
    await pictures.once()
    [row] = await store.fetchall("SELECT * FROM artist_pictures")
    if later == "kept":
        assert (row["state"], row["image"]) == ("set", "a1_x.png")
        pictures.enabled = False
        await pictures.once()
        assert not nd.state["a1"].get("uploadedImage")  # Shijhon's, so taken back
    else:
        # A plain failure with its one-day retry: not asked again in the next passes.
        assert (row["state"], row["uploaded_at"]) == ("unmatched", None)
        assert row["retry_at"] == pytest.approx(NOW + 86400, abs=1)
        await pictures.once()
        assert len(tries) == 1


@pytest.mark.parametrize("how", ["settled", "turned off"])
async def test_a_persons_upload_after_an_unknown_one_is_left_alone(
    store: Store, quick_reads: None, how: str
) -> None:
    now = [NOW]
    nd = Outage(now, kept=False, reads=0, answer=NavidromeError("timed out"))
    pictures = at_now(nd, OneArtist(), store, now)
    await pictures.once()  # (no answer, and none shown: not known yet)
    nd.down = False
    now[0] += 5 * 3600  # hours later a person sets a picture
    nd.state["a1"].update(
        uploadedImage="a1_mine.png", imageAbsent=False, updatedAt=iso(now[0]), imageHash="hp"
    )
    if how == "settled":
        await pictures.once()
        [row] = await store.fetchall("SELECT state FROM artist_pictures")
        assert row["state"] == "declined"
    pictures.enabled = False
    await pictures.once()
    assert nd.state["a1"].get("uploadedImage") == "a1_mine.png" and nd.removals == 0
    [row] = await store.fetchall("SELECT state FROM artist_pictures")
    assert row["state"] == "declined"


async def test_turned_off_an_upload_not_told_yet_is_removed(
    store: Store, quick_reads: None
) -> None:
    now = [NOW]
    nd = Outage(now)
    pictures = at_now(nd, OneArtist(), store, now)
    await pictures.once()
    nd.down = False
    pictures.enabled = False
    await pictures.once()
    assert not nd.state["a1"].get("uploadedImage")
    assert await store.fetchall("SELECT * FROM artist_pictures") == []


async def test_an_unanswered_upload_shown_only_later_is_taken_by_the_next_pass(
    store: Store, quick_reads: None
) -> None:
    now = [NOW]
    nd = Outage(now, reads=0, kept=False, answer=NavidromeError("timed out"))
    pictures = at_now(nd, OneArtist(), store, now)
    assert await pictures.once() == 0
    assert nd.removals == 0
    [row] = await store.fetchall("SELECT * FROM artist_pictures")
    assert row["state"] == "unmatched" and row["uploaded_at"]
    nd.down = False
    stored = iso(NOW + 30)  # (Navidrome stored it after all, half a minute on)
    nd.state["a1"].update(uploadedImage="a1_x.png", imageAbsent=False, updatedAt=stored)
    now[0] += 120
    await pictures.once()
    [row] = await store.fetchall("SELECT * FROM artist_pictures")
    assert (row["state"], row["image"], row["uploaded_at"]) == ("set", "a1_x.png", stored)


@pytest.mark.parametrize(
    "answer",
    [NavidromeError("HTTP 415", status=415), NavidromeError("ConnectError", unsent=True)],
    ids=["refused", "never sent"],
)
async def test_an_upload_navidrome_refused_or_never_got_leaves_nothing_to_tell(
    store: Store, quick_reads: None, answer: NavidromeError
) -> None:
    now = [NOW]
    nd = Outage(now, reads=0, kept=False, answer=answer)
    pictures = at_now(nd, OneArtist(), store, now)
    assert await pictures.once() == 0
    assert nd.removals == 0
    [row] = await store.fetchall("SELECT * FROM artist_pictures")
    assert (row["state"], row["uploaded_at"]) == ("unmatched", None)


async def test_a_hanging_upload_holds_no_longer_than_its_bound(
    store: Store, quick_reads: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shijhon.artist_pictures.UPLOAD_SECONDS", 0.2)
    now = [NOW]
    nd = Outage(now)

    async def hanging(ident: str, image: bytes, content_type: str) -> None:
        await anyio.sleep(30)

    nd.set_artist_image = hanging  # type: ignore[method-assign]
    pictures = at_now(nd, OneArtist(), store, now)
    with anyio.fail_after(5):
        assert await pictures.once() == 0
    [row] = await store.fetchall("SELECT * FROM artist_pictures")
    assert row["state"] == "unmatched" and row["uploaded_at"]  # told later


async def test_an_upload_not_known_is_told_minutes_later_not_hours(
    store: Store, quick_reads: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shijhon.artist_pictures.FIRST_PASS_SECONDS", 0.0)
    monkeypatch.setattr("shijhon.artist_pictures.SETTLE_SECONDS", 0.05)
    now = [NOW]
    nd = Outage(now)
    pictures = at_now(nd, OneArtist(), store, now)
    told = anyio.Event()
    settle = pictures.settle

    async def settling() -> None:
        nd.down = False  # (back by then)
        await settle()
        told.set()

    pictures.settle = settling  # type: ignore[method-assign]
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(pictures.run)
        with anyio.fail_after(5):
            await told.wait()
        tasks.cancel_scope.cancel()
    [row] = await store.fetchall("SELECT state, image FROM artist_pictures")
    assert (row["state"], row["image"]) == ("set", "a1_x.png")


async def test_an_upload_whose_record_fails_is_removed(store: Store, quick_reads: None) -> None:
    nd = FakeNavidrome()
    pictures = ArtistPictures(nd, OneArtist(), store)  # type: ignore[arg-type]

    async def broken(*args: Any, **kwargs: Any) -> None:
        raise OSError("disk full")

    pictures._note = broken  # type: ignore[method-assign]
    with pytest.raises(OSError):
        await pictures.once()
    assert nd.uploads == 1 and not nd.state["a1"].get("uploadedImage")


async def test_an_artist_failing_unexpectedly_holds_up_no_other(store: Store) -> None:
    nd = FakeNavidrome()
    nd.state = {
        "a0": {"id": "a0", "name": "Ansel Grey", "imageAbsent": True},
        "a1": nd.state["a1"],
    }

    class Breaks(OneArtist):
        async def search(self, term: str, limit: int = 20) -> SearchResults:
            if term == "Ansel Grey":
                raise RuntimeError("an adapter's own error")
            return await super().search(term, limit)

    pictures = ArtistPictures(nd, Breaks(), store)  # type: ignore[arg-type]
    assert await pictures.once() == 1
    assert nd.state["a1"].get("uploadedImage")
    [row] = await store.fetchall("SELECT * FROM artist_pictures WHERE artist_id = 'a0'")
    assert row["state"] == "unmatched" and row["retry_at"] - row["updated_at"] == pytest.approx(
        86400, abs=1
    )


async def test_a_failed_read_while_waiting_for_the_hash_keeps_the_record(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_sleep = anyio.sleep

    async def quick(seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("shijhon.artist_pictures.anyio.sleep", quick)
    nd = FailingAfterUpload(fail_on=2)  # (1: the read right after the upload)
    pictures = ArtistPictures(nd, OneArtist(), store)  # type: ignore[arg-type]
    assert await pictures.once() == 1
    [row] = await store.fetchall("SELECT * FROM artist_pictures")
    assert (row["state"], row["image"]) == ("set", "a1_x.png")
    pictures.enabled = False
    await pictures.once()
    assert not nd.state["a1"].get("uploadedImage")  # Shijhon's, so taken back


async def test_a_failure_after_the_picture_is_recorded_does_not_forget_it(store: Store) -> None:
    nd = FakeNavidrome()
    pictures = ArtistPictures(nd, OneArtist(), store)  # type: ignore[arg-type]
    give = pictures._give

    async def recorded_then_failed(artist: dict[str, Any]) -> bool:
        await give(artist)
        raise NavidromeError("temporary")

    pictures._give = recorded_then_failed  # type: ignore[method-assign]
    await pictures.once()
    assert await state_of(store) == "set"


async def test_the_worker_goes_on_after_an_unexpected_failure(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shijhon.artist_pictures.FIRST_PASS_SECONDS", 0.0)
    nd = FakeNavidrome()
    nd.state["a2"] = {"id": "a2", "name": "Ansel Grey", "imageAbsent": True}
    failed, removed = anyio.Event(), anyio.Event()
    remove = nd.remove_artist_image

    async def removing(ident: str) -> None:
        await remove(ident)
        removed.set()

    nd.remove_artist_image = removing  # type: ignore[method-assign]

    class Breaks(OneArtist):
        async def search(self, term: str, limit: int = 20) -> SearchResults:
            if term == "Ansel Grey":
                raise RuntimeError("an adapter's own error")
            return await super().search(term, limit)

    pictures = ArtistPictures(nd, Breaks(), store)  # type: ignore[arg-type]
    note = pictures._note

    async def noting(ident: str, *args: Any, **kwargs: Any) -> None:
        if ident == "a2":  # (its failure cannot be recorded either: the pass ends)
            failed.set()
            raise RuntimeError("the store's own error")
        await note(ident, *args, **kwargs)

    pictures._note = noting  # type: ignore[method-assign]
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(pictures.run)
        with anyio.fail_after(5):
            await failed.wait()
            assert nd.state["a1"].get("uploadedImage")
            await anyio.sleep(0.01)  # (the pass's failure reaches the worker)
            pictures.enabled = False  # applied at once, by the next pass
            await removed.wait()
        assert not nd.state["a1"].get("uploadedImage")
        tasks.cancel_scope.cancel()


async def test_the_catalog_requests_are_background_work(store: Store) -> None:
    seen: list[int | None] = []

    def note() -> None:
        urgency = pacing.current()
        seen.append(urgency.level if urgency else None)

    class Watched(OneArtist):
        async def search(self, term: str, limit: int = 20) -> SearchResults:
            note()
            return await super().search(term, limit)

        async def artist_releases(self, artist_id: str) -> tuple[CatalogRelease, ...]:
            note()
            return await super().artist_releases(artist_id)

        async def artwork(self, url: str) -> tuple[bytes, str]:
            note()
            return await super().artwork(url)

    pictures = ArtistPictures(FakeNavidrome(), Watched(), store)  # type: ignore[arg-type]
    assert await pictures.once() == 1
    assert seen == [pacing.WARM] * 3


class SameNames(OneArtist):
    """Two catalog artists of the library artist's name, each with a release of the
    library's album title (a self-titled album, say)."""

    async def search(self, term: str, limit: int = 20) -> SearchResults:
        return SearchResults(
            artists=tuple(
                CatalogArtist(CatalogRef("test", key), "Wren Hollow", "https://p.invalid/{w}.jpg")
                for key in ("w", "v")
            )
        )


async def test_two_qualifying_artists_of_the_name_give_no_picture(store: Store) -> None:
    nd = FakeNavidrome()
    assert await ArtistPictures(nd, SameNames(), store).once() == 0  # type: ignore[arg-type]
    assert nd.uploads == 0 and await state_of(store) == "unmatched"


async def test_one_qualifying_artist_among_others_of_the_name_gives_its_picture(
    store: Store,
) -> None:
    class OnlyOne(SameNames):
        async def artist_releases(self, artist_id: str) -> tuple[CatalogRelease, ...]:
            if artist_id == "v":
                return (catalog_release("v1", "Other Shores", "Wren Hollow", 1),)
            return await super().artist_releases(artist_id)

    nd = FakeNavidrome()
    assert await ArtistPictures(nd, OnlyOne(), store).once() == 1  # type: ignore[arg-type]
    [row] = await store.fetchall("SELECT catalog_ref FROM artist_pictures")
    assert row["catalog_ref"] == "test:w"


@pytest.mark.parametrize(
    ("failure", "unsent", "status"),
    [
        (httpx.ConnectError("refused"), True, None),
        (httpx.ConnectTimeout("no connection"), True, None),
        (httpx.ReadTimeout("no answer"), False, None),
        (None, False, 415),
    ],
    ids=["refused", "connect timeout", "no answer", "error answer"],
)
async def test_navidrome_errors_tell_a_request_never_sent(
    failure: Exception | None, unsent: bool, status: int | None
) -> None:
    from shijhon.navidrome.client import NavidromeService

    def answer(request: httpx.Request) -> httpx.Response:
        if failure is not None:
            raise failure
        return httpx.Response(415)

    service = NavidromeService("http://test.invalid", "admin", "synthetic")
    service._jwt = "token"
    await service.http.aclose()
    service.http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    try:
        with pytest.raises(NavidromeError) as caught:
            await service.set_artist_image("a1", JPEG, "image/jpeg")
        assert (caught.value.unsent, caught.value.status) == (unsent, status)
    finally:
        await service.http.aclose()
