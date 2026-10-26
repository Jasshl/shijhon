"""Personal uploads through real dashboard admission and production startup."""

from __future__ import annotations

import hashlib
import time

import anyio

from shijhon.app import create_app
from shijhon.collection.origins import Origins
from shijhon.config import load_settings
from shijhon.store import Store
from tests.harness.dashboard import Browser
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.running import running


def test_upload_routes_publish_only_reviewed_own_originals_and_keep_active_custom(
    navidrome_factory, tmp_path
):
    nd = navidrome_factory()
    owner = nd.create_user("importer", "invented-import-password")
    other = nd.create_user("neighbour", "invented-neighbour-password")
    (nd.music / "_imports").mkdir()
    state = tmp_path.resolve() / "state"
    settings = load_settings(
        None,
        state_dir=state,
        navidrome={
            "url": nd.base_url,
            "user": ADMIN_USER,
            "password": ADMIN_PASSWORD,
            "library_path": nd.music,
        },
        music_imports={"enabled": True, "min_free_bytes": 0},
    )

    async def new_installation():
        store = await Store.open(settings.database_path)
        try:
            await Origins(store).initialize_personal("1")
        finally:
            await store.close()

    anyio.run(new_installation)
    source = write_album(
        tmp_path / "source",
        Album("Glass Orchard", "Paper Garden", (Track("First Light", 1, seconds=0.3),)),
    )[0]
    data = source.read_bytes()
    app = create_app(settings)
    with running(app) as server:
        browser, neighbour = Browser(server.base_url), Browser(server.base_url)
        try:
            assert browser.sign_in("importer", "invented-import-password").status_code == 303
            assert neighbour.sign_in("neighbour", "invented-neighbour-password").status_code == 303
            headers = {"X-CSRF-Token": browser.csrf(), "Origin": server.base_url}
            page = browser.get("collection/import")
            assert page.status_code == 200 and "Choose your music" in page.text
            assert "script-src 'self'" in page.headers["content-security-policy"]
            assert "connect-src 'self'" in page.headers["content-security-policy"]
            collection = app.services.collection
            assert collection is not None and app.services.music_imports is not None
            removals = app.services.music_import_removals
            assert removals is not None and removals.native_absent is not None

            async def native_absence():
                assert await removals.native_absent(owner, "1", "_imports/not-published.flac")

            server.call(native_absence)

            async def custom():
                ident = await collection.create_profile(owner, "Quiet", start="empty")
                await collection.activate_profile(owner, ident)
                return ident

            profile_id = server.call(custom)
            base = "/shijhon/collection/import/api"

            def post(path, body):
                response = browser.http.post(path, json=body, headers=headers)
                assert response.status_code == 200, response.text
                return response.json()

            job = post(base, {"request_key": "one-import"})
            path = base + "/" + job["id"]
            assert neighbour.get("collection/import/manage").status_code == 403
            administrator = Browser(server.base_url)
            try:
                assert administrator.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
                management = administrator.get("collection/import/manage?job=" + job["id"])
                assert management.status_code == 200 and "Review removal" in management.text
                policy = management.headers["content-security-policy"]
                assert "default-src 'none'" in policy
                assert "script-src 'self'" in policy and "connect-src 'self'" in policy
                assert "music_import_removals.js" in management.text
                assert administrator.get("static/music_import_removals.js").status_code == 200
            finally:
                administrator.close()
            job = post(
                path + "/manifest",
                {
                    "page_key": "first",
                    "entries": [
                        {"client_key": "file", "path": "Folder/First Light.flac", "size": len(data)}
                    ],
                },
            )
            post(path + "/seal", {})
            item = job["items"][0]
            response = browser.http.post(
                path + "/files/" + item["id"] + "?attempt=0",
                content=data,
                headers={**headers, "Content-Type": "application/octet-stream"},
            )
            assert response.status_code == 200, response.text
            assert neighbour.http.get(path).status_code == 404
            assert list((nd.music / "_imports").rglob("*.flac")) == []
            assert post(path + "/review", {})["state"] == "checking"

            def await_state(expected):
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    answer = browser.http.get(path)
                    assert answer.status_code == 200, answer.text
                    value = answer.json()
                    if value["state"] in expected:
                        return value
                    assert value["state"] != "paused", value
                    time.sleep(0.05)
                raise AssertionError(value)

            reviewed = await_state({"review"})
            assert reviewed["eligible_files"] == 1
            assert list((nd.music / "_imports").rglob("*.flac")) == []
            post(path + "/approve", {"revision": reviewed["revision"], "request_key": "approve"})
            done = await_state({"done"})
            native_id = done["items"][0]["song_id"]
            downloaded = nd.client("importer", "invented-import-password").request(
                "download", {"id": native_id}
            )
            assert hashlib.sha256(downloaded.content).digest() == hashlib.sha256(data).digest()

            async def selections():
                assert (await collection.profile(owner))["id"] == profile_id
                assert not await collection.visible_song_ids(owner, "1", [native_id])
                for builtin in ("original", "all"):
                    assert await collection.visible_song_ids(
                        owner, "1", [native_id], profile_id=builtin
                    ) == {native_id}
                assert not await collection.visible_song_ids(
                    other, "1", [native_id], profile_id="all"
                )

            server.call(selections)
        finally:
            browser.close()
            neighbour.close()
