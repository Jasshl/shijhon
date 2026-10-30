"""Missing-song browsing and frozen bulk selections use effective personal membership."""

from __future__ import annotations

import json
import secrets
from dataclasses import replace
from typing import get_args

import pytest

from shijhon.collection.contracts import MODES, CollectionError, Mode, Query
from tests.unit.test_collection import finish, setup_collection, song

pytestmark = pytest.mark.anyio


async def apply(service, query, *, owner="one", profile=None, selected=None, action="save"):
    ident = await service.prepare(
        owner,
        "1",
        query,
        action=action,
        request_key=secrets.token_hex(16),
        selected=selected,
        profile_id=profile,
    )
    assert (await finish(service, ident, owner))["state"] == "review"
    await service.approve(owner, ident)
    assert (await finish(service, ident, owner))["state"] == "done"
    return ident


@pytest.fixture
async def library(tmp_path):
    store, access, service = await setup_collection(
        tmp_path,
        [
            song(1),
            song(2, path="_shijhon/2.flac"),
            song(3, path="_shijhon/3.flac"),
            song(4, album="Briar Vale", albumId="a2"),
            song(5, album="Briar Vale", albumId="a2", path="_shijhon/5.flac"),
            song(
                6,
                album="Distant Shore",
                albumId="a3",
                artist="Paper Field",
                artistId="r2",
                path="_shijhon/6.flac",
            ),
            song(7, album="Evening", albumId="a4", artist="Silver Tree", artistId="r3"),
            song(
                8,
                album="Fable",
                albumId="a5",
                artist="White Lake",
                artistId="r4",
                path="_shijhon/8.flac",
            ),
        ],
    )
    try:
        await apply(service, Query(view="all", scope="song"), selected=["catalog:s2", "catalog:s8"])
        # All holds every song of an album with an original (a reason): the catalog songs
        # of Cloud House and Briar Vale are out of it only when explicitly removed.
        await apply(
            service,
            Query(view="all", scope="song"),
            selected=["catalog:s3", "catalog:s5"],
            action="remove",
        )
        yield store, access, service
    finally:
        await store.close()


@pytest.mark.parametrize("profile", ["all", "shijhon", "original"])
@pytest.mark.parametrize("scope", ["song", "album", "artist"])
async def test_builtin_missing_tracks_and_full_group_membership(library, profile, scope):
    _store, _access, service = library
    result = await service.browse(
        "one", "1", Query(view="unsaved", scope=scope), profile_id=profile
    )
    assert result["query"]["view"] == "unsaved"
    # Original holds the whole albums of the originals (shared: everyone's imports);
    # All holds them as All's own choices left them; Shijhon library holds nothing yet
    # (no activity, and All's choices are All's alone).
    every = {
        "song": {f"{'native' if n in (1, 4, 7) else 'catalog'}:s{n}" for n in range(1, 9)},
        "album": {"a1", "a2", "a3", "a4", "a5"},
        "artist": {"r1", "r2", "r3", "r4"},
    }[scope]
    expected = {
        "all": {
            "song": {"catalog:s3", "catalog:s5", "catalog:s6"},
            "album": {"a1", "a2", "a3"},
            "artist": {"r1", "r2"},
        }[scope],
        "original": {
            "song": {"catalog:s6", "catalog:s8"},
            "album": {"a3", "a5"},
            "artist": {"r2", "r4"},
        }[scope],
        "shijhon": every,
    }[profile]
    rows = {row["item_key"]: row for row in result["rows"]}
    assert rows.keys() == expected and result["total"] == len(expected)
    missing = {"all": 3, "original": 2, "shijhon": 8}[profile]
    assert sum(row["tracks"] for row in rows.values()) == missing
    assert all(row["saved"] == 0 for row in rows.values())
    if profile != "shijhon":
        assert all(row["originals"] == 0 for row in rows.values())
    if scope in ("album", "artist") and profile == "all":
        partial = rows["a1" if scope == "album" else "r1"]
        imported = 1 if scope == "album" else 2
        assert partial["tracks"] == (1 if scope == "album" else 2)
        assert partial["all_tracks"] == (3 if scope == "album" else 5)
        assert partial["all_imported"] == imported
        assert partial["full_saved"] == imported + 1
        assert partial["editable"] == 1
    if scope in ("album", "artist") and profile == "shijhon":
        assert all(row["full_saved"] == 0 for row in rows.values())
    saved = {"all": 5, "original": 6, "shijhon": 0}[profile]
    assert result["counts"] == {"all_count": 8, "saved_count": saved, "unsaved_count": 8 - saved}
    assert result["sources"] == {"imported_count": 3, "catalog_count": 5}
    assert result["query"]["origin"] == "all"


async def test_owner_custom_profile_isolation_and_browsing_has_no_membership_effect(library):
    store, _access, service = library
    custom = await service.create_profile("one", "Personal", start="original")
    empty = await service.create_profile("one", "Empty", start="empty")
    await apply(service, Query(view="all", scope="song"), profile=custom, selected=["catalog:s3"])
    await apply(
        service,
        Query(view="all", scope="song"),
        profile=custom,
        selected=["native:s1"],
        action="remove",
    )
    await service.profile("two")
    before = [dict(row) for row in await store.fetchall("SELECT * FROM collection_membership")]
    preferences = [
        dict(row) for row in await store.fetchall("SELECT * FROM collection_preferences")
    ]
    query = Query(view="unsaved", scope="song")
    for owner, profile, expected in (
        ("one", "all", {"catalog:s3", "catalog:s5", "catalog:s6"}),
        # The shared originals of Cloud House and Briar Vale bring them whole into the
        # second account's All; it chose nothing explicitly.
        ("two", "all", {"catalog:s6", "catalog:s8"}),
        # A copy of Original imported library: its whole albums, less the removed import.
        ("one", custom, {"native:s1", "catalog:s6", "catalog:s8"}),
        (
            "one",
            empty,
            {
                "native:s1",
                "catalog:s2",
                "catalog:s3",
                "native:s4",
                "catalog:s5",
                "catalog:s6",
                "native:s7",
                "catalog:s8",
            },
        ),
    ):
        result = await service.browse(owner, "1", query, profile_id=profile)
        assert {row["item_key"] for row in result["rows"]} == expected
    assert [
        dict(row) for row in await store.fetchall("SELECT * FROM collection_membership")
    ] == before
    assert [dict(row) for row in await store.fetchall("SELECT * FROM collection_preferences")] == (
        preferences
    )
    await service.activate_profile("one", custom)
    assert (await service.browse("one", "1", query))["rows"] == (
        await service.browse("one", "1", query, profile_id=custom)
    )["rows"]
    with pytest.raises(CollectionError, match="unavailable"):
        await service.browse("two", "1", query, profile_id=custom)
    await service.archive_profile("one", empty)
    with pytest.raises(CollectionError, match="unavailable"):
        await service.browse("one", "1", query, profile_id=empty)


@pytest.mark.parametrize("scope", ["song", "album", "artist"])
@pytest.mark.parametrize("sort", ["title", "artist"])
async def test_missing_filter_precedes_group_counts_order_and_paging(library, scope, sort):
    _store, _access, service = library
    query = Query(view="unsaved", scope=scope, sort=sort)
    full = await service.browse("one", "1", query)
    pages = [
        await service.browse("one", "1", replace(query, limit=1, offset=offset))
        for offset in range(full["total"] + 1)
    ]
    assert all(page["total"] == full["total"] for page in pages)
    assert [row for page in pages for row in page["rows"]] == full["rows"]
    assert pages[-1]["rows"] == []


@pytest.mark.parametrize("scope", ["song", "album", "artist"])
async def test_missing_search_source_and_group_constraints_intersect(library, scope):
    _store, _access, service = library
    custom = await service.create_profile("one", "Omit originals", start="empty")
    base = Query(view="unsaved", scope=scope, album_id="a1", artist_id="r1")
    for profile in ("all", "shijhon", custom):
        for origin, search, expected in (
            ("catalog", "Song 3", 1),
            ("catalog", "Song 1", 0),
            ("imported", "Song 1", int(profile != "all")),
            ("original", "Song 1", int(profile != "all")),
            ("catalog", "%", 0),
            ("catalog", "_", 0),
        ):
            result = await service.browse(
                "one", "1", replace(base, origin=origin, search=search), profile_id=profile
            )
            assert result["total"] == expected
            assert sum(row["tracks"] for row in result["rows"]) == expected
        for constrained in (replace(base, album_id="a3"), replace(base, artist_id="r2")):
            assert (await service.browse("one", "1", constrained, profile_id=profile))["total"] == 0


@pytest.mark.parametrize("scope", ["album", "artist"])
@pytest.mark.parametrize("selected", [False, True])
async def test_bulk_freezes_only_matching_missing_tracks_and_undo_preserves_saved_neighbor(
    library, scope, selected
):
    store, access, service = library
    query = Query(view="unsaved", scope=scope, album_id="a1", search="Song", offset=99, limit=1)
    neighbor = dict(
        await store.fetchone("SELECT * FROM collection_membership WHERE track_key='catalog:s2'")
    )
    ident = await service.prepare(
        "one",
        "1",
        query,
        action="save",
        request_key="only-missing",
        selected=(["a1"] if scope == "album" else ["r1"]) if selected else None,
    )
    op = await finish(service, ident)
    assert op["state"] == "review" and op["approved_at"] is None
    assert op["total"] == op["affected"] == op["group_count"] == 1
    assert json.loads(op["query"])["view"] == "unsaved"
    assert [
        row["track_key"]
        for row in await store.fetchall(
            "SELECT track_key FROM collection_operation_items WHERE operation_id=?", [ident]
        )
    ] == ["catalog:s3"]
    assert not await service.visible_song_ids("one", "1", ["s3"])
    # A later discovered missing song in the same group must not enter the frozen set.
    access.rows.append(song(9, path="_shijhon/9.flac"))
    await service.refresh("one", "1")
    await service.approve("one", ident)
    assert (await finish(service, ident))["changed"] == 1
    # s9 is in All by its album's original, not by this change: it was not frozen.
    assert await service.visible_song_ids("one", "1", ["s1", "s2", "s3", "s9"]) == {
        "s1",
        "s2",
        "s3",
        "s9",
    }
    assert not await store.fetchone(
        "SELECT 1 FROM collection_membership WHERE track_key='catalog:s9'"
    )
    assert (
        dict(
            await store.fetchone("SELECT * FROM collection_membership WHERE track_key='catalog:s2'")
        )
        == neighbor
    )
    # A later explicit choice on the saved neighbor survives Undo of this missing set.
    await apply(service, Query(view="all", scope="song"), selected=["catalog:s2"])
    newer_neighbor = dict(
        await store.fetchone("SELECT * FROM collection_membership WHERE track_key='catalog:s2'")
    )
    undone = await finish(service, await service.undo("one", ident))
    assert undone["changed"] == 1 and undone["skipped"] == 0
    assert await service.visible_song_ids("one", "1", ["s2", "s3", "s9"]) == {"s2", "s9"}
    assert (
        dict(
            await store.fetchone("SELECT * FROM collection_membership WHERE track_key='catalog:s2'")
        )
        == newer_neighbor
    )


async def test_unsaved_freeze_retains_revision_protection_and_custom_original_support(library):
    _store, _access, service = library
    custom = await service.create_profile("one", "Subset", start="empty")
    query = Query(view="unsaved", scope="album", album_id="a1", origin="original")
    ident = await service.prepare(
        "one", "1", query, action="save", request_key="custom-original", profile_id=custom
    )
    assert (await finish(service, ident))["total"] == 1
    await apply(service, Query(view="all", scope="song"), profile=custom, selected=["native:s1"])
    await service.approve("one", ident)
    op = await finish(service, ident)
    assert op["changed"] == 0 and op["skipped"] == 1
    # The newer hand addition stands, and brings its album (s2 shares it).
    assert await service.visible_song_ids("one", "1", ["s1", "s2"], profile_id=custom) == {
        "s1",
        "s2",
    }
    protected = await service.prepare(
        "one", "1", query, action="save", request_key="protected", profile_id="all"
    )
    assert (await finish(service, protected))["total"] == 0
    # Original imported library holds the import's whole album: nothing is missing there.
    whole = await service.prepare(
        "one",
        "1",
        replace(query, origin="all"),
        action="save",
        request_key="whole-album",
        profile_id="original",
    )
    assert (await finish(service, whole))["total"] == 0


async def test_missing_view_rechecks_access_and_worker_freeze_and_apply_authority(library):
    store, access, service = library
    query = Query(view="unsaved", scope="song")
    ident = await service.prepare("one", "1", query, action="save", request_key="before-freeze")
    access.allowed = False
    with pytest.raises(CollectionError):
        await service.browse("one", "1", query)
    with pytest.raises(CollectionError):
        await service.prepare("one", "1", query, action="save", request_key="revoked")
    await service.tick()
    assert (await store.fetchone("SELECT state FROM collection_operations WHERE id=?", [ident]))[
        "state"
    ] == "failed"
    assert not await store.fetchone(
        "SELECT 1 FROM collection_operation_items WHERE operation_id=?", [ident]
    )
    access.allowed = True
    await service.retry("one", ident)
    assert (await finish(service, ident))["state"] == "review"
    await service.approve("one", ident)
    access.allowed = False
    await service.tick()
    access.allowed = True
    assert (await service.operation("one", ident))["state"] == "failed"
    assert not await service.visible_song_ids("one", "1", ["s3", "s5", "s6"])
    await service.retry("one", ident)
    assert (await finish(service, ident))["changed"] == 3


async def test_unknown_absent_and_unprepared_candidates_fail_closed(library):
    store, access, service = library
    query = Query(view="unsaved", scope="song")
    with pytest.raises(CollectionError):
        await service.browse("one", "2", query)
    with pytest.raises(CollectionError, match="unavailable"):
        await service.browse("one", "1", query, profile_id="unknown-profile")
    assert not await service.visible_song_ids("one", "1", ["never-indexed"])
    assert (await service.browse("one", "1", replace(query, album_id="never-indexed")))[
        "total"
    ] == 0
    access.rows = [row for row in access.rows if row["id"] != "s3"]
    await service.refresh("one", "1")
    assert {row["item_key"] for row in (await service.browse("one", "1", query))["rows"]} == {
        "catalog:s5",
        "catalog:s6",
    }
    await store.execute("UPDATE collection_index SET generation=''")
    with pytest.raises(CollectionError, match="still being prepared"):
        await service.browse("one", "1", query)
    with pytest.raises(CollectionError, match="still being prepared"):
        await service.prepare("one", "1", query, action="save", request_key="not-ready")


async def test_unsaved_is_a_browse_view_and_never_a_native_mode(library):
    _store, _access, service = library
    assert get_args(Mode) == MODES == ("saved", "original", "all")
    with pytest.raises(CollectionError):
        Query(view="unknown")
    with pytest.raises(CollectionError):
        await service.set_mode("one", "unsaved")
    with pytest.raises(CollectionError):
        await service.predicate("one", "1", mode="unsaved")
    assert (await service.profile("one"))["selection_id"] == "all"
    # Browsing Original imported library uses its rule: the imports' whole albums.
    for view, albums in (
        ("saved", {"a1", "a2", "a4"}),
        ("original", {"a1", "a2", "a4"}),  # physical originals, for recorded changes
        ("all", {"a1", "a2", "a3", "a4", "a5"}),
    ):
        listing = await service.browse("one", "1", Query(view=view), profile_id="original")
        assert listing["query"]["view"] == view
        assert {row["item_key"] for row in listing["rows"]} == albums
