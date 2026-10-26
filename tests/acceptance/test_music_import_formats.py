"""Real native scanning, original-byte playback and names across music formats."""

import hashlib
import shutil
import subprocess
import sys
from dataclasses import replace

import anyio
import pytest

from shijhon.collection.origins import Origins
from shijhon.collection.service import Collection
from shijhon.music_imports import (
    AssetResolver,
    ImportError,
    ImportLimits,
    ManifestEntry,
    MusicImports,
)
from shijhon.navidrome.client import NavidromeService
from shijhon.navidrome.scans import ScanCoordinator
from shijhon.store import Store
from tests.acceptance.test_music_imports import Caller
from tests.harness.music_import_formats import IMPORT_FORMATS, import_tone
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER, _platform_key

pytestmark = pytest.mark.anyio

VERIFIED = tuple(fmt for fmt in IMPORT_FORMATS if fmt.key not in ("ac3", "trueaudio", "tak"))


async def assert_native_import_matrix(navidrome_factory, tmp_path, formats, *, native_indexed=True):
    nd = navidrome_factory()
    owner = nd.create_user("format-importer", "invented-format-password")
    login = nd.login("format-importer", "invented-format-password")
    native = NavidromeService(nd.base_url, ADMIN_USER, ADMIN_PASSWORD)
    caller = Caller(native, [login])
    store = await Store.open(tmp_path / "state.db")
    collection = Collection(store, caller)
    coordinator = ScanCoordinator(native)
    (nd.music / "_imports").mkdir()
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
    selected = {}
    for fmt in formats:
        for tagged in (False,) if fmt.suffix == "shn" else (False, True):
            key = fmt.key + ("-tagged" if tagged else "-untagged")
            source = import_tone(tmp_path / "sources" / key, fmt, tagged=tagged)
            selected[key] = (source, fmt, tagged)
    try:
        await Origins(store).initialize_personal("1")
        await service.initialize()
        job = await service.create(owner, "format-matrix")
        job = await service.declare(
            owner,
            job["id"],
            "all-formats",
            [
                ManifestEntry(
                    key,
                    "Café Garden: Volume 1/" + key + "/" + file.name,
                    file.stat().st_size,
                    hashlib.sha256(file.read_bytes()).hexdigest(),
                )
                for key, (file, _, _) in selected.items()
            ],
        )
        await service.seal(owner, job["id"])
        for item in job["items"]:
            data = selected[item["client_key"]][0].read_bytes()

            async def stream(data=data):
                yield data

            await service.receive(owner, job["id"], item["id"], 0, stream())
        await coordinator.scan(["_imports"])
        assert await caller.songs(owner, "1", 0, 500) == []
        await service.review(owner, job["id"])
        for _ in selected:
            assert await service.tick()
        reviewed = await service.status(owner, job["id"], limit=500)
        assert reviewed["state"] == "review"
        assert reviewed["eligible_files"] == len(selected), reviewed
        for item in reviewed["items"]:
            _, _, tagged = selected[item["client_key"]]
            assert bool(item["warnings"]) is not tagged, item
        await service.approve(owner, job["id"], reviewed["revision"], "approved-formats")
        assert await service.tick()
        done = await service.status(owner, job["id"], limit=500)
        if not native_indexed:
            # A valid original can pass the parser yet be refused by this native
            # scanner build. Keep the approved bytes; never claim native success.
            assert done["state"] == "paused" and done["error"] == "indexing", done
            assert done["complete_files"] == 0
            assert await caller.songs(owner, "1", 0, 500) == []
            assert not await store.fetchone("SELECT 1 FROM collection_originals")
            for item in done["items"]:
                source = selected[item["client_key"]][0]
                final = nd.music / "_imports" / ("batch-" + job["id"]) / item["path"]
                assert final.read_bytes() == source.read_bytes()
                assert item["state"] == "placed" and item["song_id"] == ""
            await service.cancel(owner, job["id"])
            assert all(
                (nd.music / "_imports" / ("batch-" + job["id"]) / item["path"]).exists()
                for item in done["items"]
            )
            return
        assert done["state"] == "done", done
        assert done["complete_files"] == len(selected)
        rows = await caller.songs(owner, "1", 0, 500)
        indexed = {row["id"]: row for row in rows}
        assert len(indexed) == len(selected)
        client = nd.client("format-importer", "invented-format-password")
        try:
            for item in done["items"]:
                source, fmt, tagged = selected[item["client_key"]]
                row = indexed[item["song_id"]]
                original = source.read_bytes()
                expected_path = "_imports/batch-" + job["id"] + "/" + item["path"]
                assert row["path"] == expected_path
                assert row["title"] == ("First Light " + fmt.key if tagged else source.stem)
                if tagged:
                    assert row["artist"] == "Glass Orchard" and row["album"] == "Paper Garden"
                final = nd.music / expected_path
                assert final.read_bytes() == original
                assert source.read_bytes() == original
                for endpoint, params in [
                    ("download", {"id": row["id"]}),
                    ("stream", {"id": row["id"], "format": "raw"}),
                ]:
                    response = await anyio.to_thread.run_sync(
                        lambda endpoint=endpoint, params=params: client.request(endpoint, params)
                    )
                    assert response.status_code == 200
                    assert response.content == original
                    # Full decode of the actual native response, beyond header checks.
                    decoded = await anyio.to_thread.run_sync(
                        lambda data=response.content: subprocess.run(
                            ["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "null", "-"],
                            input=data,
                            capture_output=True,
                            timeout=10,
                        )
                    )
                    assert decoded.returncode == 0, (fmt.key, decoded.stderr)
        finally:
            client.close()
        await service.aclose()
        resolver = AssetResolver(store, library_id="1", library_path=nd.music)
        assets = await resolver("1", rows)
        assert len(assets) == len(selected)
        assert len(set(assets.values())) == len(selected)
    finally:
        await service.aclose()
        await caller.http.aclose()
        await native.aclose()
        await store.close()


async def test_tagged_and_untagged_native_format_matrix(navidrome_factory, tmp_path):
    await assert_native_import_matrix(navidrome_factory, tmp_path, VERIFIED)


async def test_trueaudio_native_platform_matrix(navidrome_factory, tmp_path):
    # This harness starts a SHA-pinned native binary on this test machine. This
    # platform distinction records that binary's observed scanner limitation;
    # application capabilities never infer a remote native OS from Python's OS.
    native_platform = _platform_key().split("_", 1)[0]
    assert native_platform in ("linux", "darwin")
    formats = tuple(fmt for fmt in IMPORT_FORMATS if fmt.key == "trueaudio")
    await assert_native_import_matrix(
        navidrome_factory, tmp_path, formats, native_indexed=native_platform == "linux"
    )


@pytest.mark.parametrize("extractor", ["taglib", "ffmpeg"])
async def test_valid_tak_native_rejection_never_uploads_publishes_or_credits(
    navidrome_factory, tmp_path, extractor
):
    nd = navidrome_factory({"ND_SCANNER_EXTRACTOR": extractor})
    owner = nd.create_user("tak-importer", "invented-tak-password")
    login = nd.login("tak-importer", "invented-tak-password")
    native = NavidromeService(nd.base_url, ADMIN_USER, ADMIN_PASSWORD)
    caller = Caller(native, [login])
    store = await Store.open(tmp_path / "state.db")
    (nd.music / "_imports").mkdir()
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
    fmt = next(fmt for fmt in IMPORT_FORMATS if fmt.key == "tak")
    sources = [import_tone(tmp_path / str(tagged), fmt, tagged=tagged) for tagged in (False, True)]
    try:
        for n, source in enumerate(sources):
            # Establish that the complete payload decodes and the mature parser
            # reads it; this is a native scanner refusal, not a malformed fixture.
            decoded = await anyio.to_thread.run_sync(
                lambda source=source: subprocess.run(
                    ["ffmpeg", "-v", "error", "-i", str(source), "-f", "null", "-"],
                    capture_output=True,
                    timeout=10,
                )
            )
            assert decoded.returncode == 0
            with source.open("rb") as descriptor:
                checked = await anyio.to_thread.run_sync(
                    lambda: subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "shijhon.music_imports.probe",
                            str(descriptor.fileno()),
                            "tak",
                        ],
                        pass_fds=(descriptor.fileno(),),
                        capture_output=True,
                        timeout=15,
                    )
                )
            assert checked.returncode == 0
            shutil.copyfile(source, nd.music / (str(n) + ".tak"))
        await coordinator.scan(["."])
        assert await caller.songs(owner, "1", 0, 500) == []
        log = nd.log_path.read_text()
        assert 'error="invalid file"' in log and "gotaglib" in log
        if extractor == "ffmpeg":
            assert "Extractor not found, using default" in log
        await Origins(store).initialize_personal("1")
        await service.initialize()
        caps = await service.capabilities(owner)
        assert "tak" not in caps["formats"]
        assert "tak" in caps["native_rejected_formats"]
        job = await service.create(owner, "native-tak-gap")
        job = await service.declare(
            owner,
            job["id"],
            "valid-tak",
            [
                ManifestEntry(str(n), str(n) + "/" + source.name, source.stat().st_size)
                for n, source in enumerate(sources)
            ],
        )
        # Equal source filenames are intentionally separate folders for this
        # evidence; no duplicate path is smuggled through the declaration.
        assert job["rejected_files"] == 2
        for item in job["items"]:
            assert item["error"] == "native_unsupported"
            assert "No original was uploaded or published" in item["warnings"][0]

            async def forbidden():
                pytest.fail("native-incompatible input consumed upload bytes")
                yield b""

            with pytest.raises(ImportError):
                await service.receive(owner, job["id"], item["id"], 0, forbidden())
        assert not await store.fetchone("SELECT 1 FROM collection_originals")
        assert list((nd.music / "_imports").rglob("*.tak")) == []
        assert list((nd.music / "_imports" / ".incoming").glob("*.part")) == []
    finally:
        await service.aclose()
        await caller.http.aclose()
        await native.aclose()
        await store.close()
