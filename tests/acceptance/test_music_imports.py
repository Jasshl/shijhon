"""Retained bytes/native-ID canary against a disposable unmodified Navidrome."""

from __future__ import annotations

import hashlib
from dataclasses import replace

import anyio
import httpx
import pytest

from shijhon.access import AccessService
from shijhon.collection.contracts import Actor
from shijhon.collection.origins import Origins
from shijhon.collection.service import Collection
from shijhon.music_imports import (
    AssetResolver,
    ImportLimits,
    ManifestEntry,
    MusicImports,
    NativeFile,
)
from shijhon.navidrome.client import NavidromeService
from shijhon.navidrome.scans import ScanCoordinator
from shijhon.store import Store
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER

pytestmark = pytest.mark.anyio


class Caller:
    """Independent exact native lookup using the actual importing user's bearer."""

    def __init__(self, native, logins):
        self.native = native
        self.access = AccessService(native)
        self.tokens = {login["id"]: login["token"] for login in logins}
        self.http = httpx.AsyncClient(base_url=native.base_url, trust_env=False)

    async def authorize(self, owner):
        principal = await self.access.verify_native(self.tokens[owner])
        assert principal.user_id == owner
        return Actor(owner, frozenset(str(i) for i in principal.library_ids))

    async def page(self, owner, params):
        await self.authorize(owner)
        response = await self.http.get(
            "/api/song",
            params=params,
            headers={"x-nd-authorization": "Bearer " + self.tokens[owner]},
        )
        response.raise_for_status()
        assert len(response.content) < 1024**2
        return response.json()

    async def songs(self, owner, library, offset, limit):
        return await self.page(
            owner,
            {
                "library_id": library,
                "_start": offset,
                "_end": offset + limit,
                "_sort": "id",
                "_order": "ASC",
                "missing": "false",
            },
        )

    async def exact(self, owner, library, path):
        rows = await self.page(
            owner, {"library_id": library, "path": path, "_start": 0, "_end": 2, "missing": "false"}
        )
        exact = [row for row in rows if row["path"] == path and str(row["libraryId"]) == library]
        if len(exact) != 1:
            return None
        return NativeFile(exact[0]["id"], exact[0]["path"], exact[0]["size"])


async def test_complete_bytes_tags_ids_private_staging_and_same_bytes_linked_canary(
    navidrome_factory, tmp_path
):
    nd = navidrome_factory()
    first_id = nd.create_user("importer-a", "invented-password-a")
    second_id = nd.create_user("importer-b", "invented-password-b")
    first = nd.login("importer-a", "invented-password-a")
    second = nd.login("importer-b", "invented-password-b")
    source = tmp_path / "source"
    files = [
        write_album(
            source,
            Album(
                "Glass Orchard",
                "Paper Garden " + fmt,
                (Track("First Light", 1, seconds=0.5),),
                fmt=fmt,
                extra={"COMMENT": ["Preserve this invented tag"]} if fmt == "flac" else {},
            ),
        )[0]
        for fmt in ("flac", "mp3")
    ]
    (nd.music / "_imports").mkdir()
    native = NavidromeService(nd.base_url, ADMIN_USER, ADMIN_PASSWORD)
    caller = Caller(native, [first, second])
    store = await Store.open(tmp_path / "state.db")
    collection = Collection(store, caller)
    coordinator = ScanCoordinator(native)
    service = MusicImports(
        store,
        caller,
        library_id="1",
        library_path=nd.music,
        limits=replace(ImportLimits(), min_free_bytes=0),
        scan=coordinator.scan,
        native_lookup=caller.exact,
        refresh=collection.refresh,
    )
    collection.index.asset_lookup = service.asset_lookup
    try:
        await Origins(store).initialize_personal("1")
        await service.initialize()
        receipts = []
        for owner, selected in [(first_id, files), (second_id, files[:1])]:
            job = await service.create(owner, "import")
            job = await service.declare(
                owner,
                job["id"],
                "page",
                [
                    ManifestEntry(
                        str(n),
                        file.name,
                        file.stat().st_size,
                        hashlib.sha256(file.read_bytes()).hexdigest(),
                    )
                    for n, file in enumerate(selected)
                ],
            )
            await service.seal(owner, job["id"])
            for item in job["items"]:
                data = selected[int(item["client_key"])].read_bytes()

                async def stream(data=data):
                    yield data

                await service.receive(owner, job["id"], item["id"], 0, stream())
            # An explicit native scan must ignore all staged/unapproved bytes.
            before = await caller.songs(owner, "1", 0, 500)
            await coordinator.scan(["_imports"])
            assert await caller.songs(owner, "1", 0, 500) == before
            assert (await service.review(owner, job["id"]))["state"] == "checking"
            for _ in selected:
                assert await service.tick()
            reviewed = await service.status(owner, job["id"])
            assert (await caller.songs(owner, "1", 0, 500)) == before
            if owner == second_id:
                # Exactly the bytes the first account imported: linked, not published again.
                assert reviewed["state"] == "review" and reviewed["eligible_files"] == 0
                assert reviewed["linked_files"] == 1
                [linked] = reviewed["items"]
                assert linked["outcome"] == "linked" and linked["match"]["title"] == "First Light"
                await service.approve(owner, job["id"], reviewed["revision"], "explicit-approval")
                assert await service.tick()  # links the file
                assert await service.tick()  # nothing left to publish
                assert not await service.tick()
                done = await service.status(owner, job["id"])
                assert done["state"] == "done" and done["linked_files"] == 1, done
                assert done["complete_files"] == 0
                linked_song = linked["match"]["song_id"]
                continue
            assert reviewed["state"] == "review" and reviewed["eligible_files"] == len(selected)
            await service.approve(owner, job["id"], reviewed["revision"], "explicit-approval")
            assert await service.tick()  # one bounded publication phase for all selected files
            assert not await service.tick()
            done = await service.status(owner, job["id"])
            assert done["state"] == "done", done
            assert done["complete_files"] == len(selected)
            for item in done["items"]:
                original = selected[int(item["client_key"])].read_bytes()
                song_id = item["song_id"]
                # Verify actual native playback/download bytes, not ledger/fake IDs.
                client = nd.client(
                    "importer-a" if owner == first_id else "importer-b",
                    "invented-password-a" if owner == first_id else "invented-password-b",
                )
                try:
                    downloaded = await anyio.to_thread.run_sync(
                        lambda client=client, song_id=song_id: (
                            client.request("download", {"id": song_id}).content
                        )
                    )
                    detail = await anyio.to_thread.run_sync(
                        lambda client=client, song_id=song_id: client.ok(
                            "getSong", {"id": song_id}
                        )["song"]
                    )
                finally:
                    client.close()
                assert hashlib.sha256(downloaded).digest() == hashlib.sha256(original).digest()
                assert detail["title"] == "First Light" and detail["artist"] == "Glass Orchard"
                assert not song_id.startswith("sh.")
                receipts.append((owner, song_id))
        assert len({ident for _, ident in receipts}) == 2  # the same bytes are published once
        assert len(list((nd.music / "_imports").rglob("*.flac"))) == 1
        assert len(list((nd.music / "_imports").rglob("*.mp3"))) == 1
        assert sorted(
            row["owner_id"] for row in await store.fetchall("SELECT * FROM collection_originals")
        ) == sorted([first_id, first_id, second_id])
        all_ids = [ident for _, ident in receipts]
        assert linked_song in all_ids
        # Import credit stays personal: each account's Original holds its own imports,
        # the second account's being the song its import was linked to...
        assert await collection.visible_song_ids(first_id, "1", all_ids, mode="original") == set(
            all_ids
        )
        assert await collection.visible_song_ids(second_id, "1", all_ids, mode="original") == {
            linked_song
        }
        # ... and each All the whole albums of its imports.
        album_of = {
            str(row["id"]): str(row["albumId"]) for row in await caller.songs(first_id, "1", 0, 500)
        }
        assert await collection.visible_song_ids(second_id, "1", all_ids) == {
            ident for ident in all_ids if album_of[ident] == album_of[linked_song]
        }
        third_id = nd.create_user("new-account", "invented-password-c")
        third = nd.login("new-account", "invented-password-c")
        caller.tokens[third_id] = third["token"]
        assert await collection.visible_song_ids(third_id, "1", all_ids) == set()
        # Full rescan/structural refresh keeps every exact ID and uploader relation.
        await native.start_scan(["_imports"], full=True)
        await coordinator.scan(["_imports"])
        await collection.refresh(second_id, "1")
        assert {row["id"] for row in await caller.songs(second_id, "1", 0, 500)} == set(all_ids)
        assert not await service.tick()
        # Intake is now stopped; native structural refresh still restores asset aliases
        # and keeps personal originals through the independent read-only resolver.
        await service.aclose()
        collection.index.asset_lookup = AssetResolver(store, library_id="1", library_path=nd.music)
        await collection.refresh(third_id, "1")
        assert await collection.visible_song_ids(first_id, "1", all_ids, mode="original") == set(
            all_ids
        )
        assert await collection.visible_song_ids(second_id, "1", all_ids, mode="original") == {
            linked_song
        }
        assert await collection.visible_song_ids(third_id, "1", all_ids) == set()
    finally:
        await service.aclose()
        await caller.http.aclose()
        await native.aclose()
        await store.close()
