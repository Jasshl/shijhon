"""Standalone creation against disposable pinned native accounts and synthetic audio."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from mutagen.flac import FLAC

from shijhon.catalog.base import SearchResults
from shijhon.playlists.model import Connection, PlaylistError
from shijhon.playlists.prompt import NamedOrdered, Ordered, Proposals
from tests.acceptance.test_R_playlists import RELEASE, SONGS, wait_state
from tests.conftest import NavidromeFactory
from tests.harness.agents import connect_agent
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[DeliveryWorld, Browser]]:
    nd = navidrome_factory()
    write_album(
        nd.music,
        Album(
            "Inkwell & Reed",
            "Quiet Room",
            (Track("Still Water", 1), Track("Still Water (Live)", 2)),
        ),
    )
    write_album(
        nd.music,
        Album(
            "Aster & Vale",
            "Remaster evidence",
            (Track("Song (2011 Remaster)", 1), Track("Song (2024 Remaster)", 2)),
        ),
    )
    nd.scan()
    catalog = Replay().catalog()

    async def search(term: str, limit: int = 20) -> SearchResults:
        return SearchResults(songs=tuple(s for s in SONGS if s.title in term))

    catalog.search = search  # type: ignore[method-assign]
    with delivery_world(
        nd, tmp_path_factory.mktemp("standalone"), catalog=catalog, agents=True
    ) as w:
        browser = Browser(w.server.base_url)
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        # No history profile/key, API key, runner or real inference is needed.
        response = browser.submit(
            "playlists",
            {"password": ADMIN_PASSWORD, "history_user": "", "backend": "none"},
            "playlists/connection",
        )
        assert response.status_code == 303
        yield w, browser
        browser.close()


def mint(browser: Browser) -> str:
    return connect_agent(
        browser.http,
        str(browser.http.base_url).rstrip("/"),
        ADMIN_USER,
        ADMIN_PASSWORD,
    )["access_token"]


def test_no_history_explicit_submission_publishes_exact_native_order_and_recovers(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    service = w.services.playlists
    assert service
    key = mint(browser)
    auth = {"Authorization": "Bearer " + key, "Idempotency-Key": "native-explicit-1"}
    payload = {
        "name": "Exact explicit playlist",
        "count": 2,
        "tracks": [
            {"artist": "Inkwell & Reed", "title": "Still Water", "version": "Live"},
            {"artist": "Inkwell & Reed", "title": "Still Water"},
        ],
    }
    response = browser.http.post("/shijhon/api/playlists/requests", headers=auth, json=payload)
    assert response.status_code == 202
    ident, run = response.json()["id"], response.json()["run"]
    finished = wait_state(w, run, ("published", "failed", "uncertain"))
    assert finished["state"] == "published", finished["error"]
    status = browser.http.get(f"/shijhon/api/playlists/requests/{ident}", headers=auth).json()
    assert [s["title"] for s in status["selected"]] == ["Still Water (Live)", "Still Water"]
    assert not status["refreshes"]

    async def verify() -> None:
        row = await service.repo.store.fetchone(
            "SELECT owner FROM playlist_requests WHERE id=?", [ident]
        )
        connection = await service.connection(row["owner"])
        assert not connection.history_user and not connection.history_key.get_secret_value()
        assert connection.backend == "none"
        mix = await service.repo.mix(ident, connection.user_id)
        native = await service.client.playlist(connection, mix["playlist_id"])
        assert [s["id"] for s in native["entry"]] == finished["data"]["native_ids"]
        assert native["owner"] == ADMIN_USER and native["name"] == payload["name"]
        # Simulate a crash after native completion but before recording success.
        await service.repo.update(run, "uncertain", finished["data"])
        await service.requests.retry(connection.user_id, ident, creator=None)

    w.server.call(verify)
    recovered = wait_state(w, run, ("published", "failed", "uncertain"))
    assert recovered["state"] == "published", recovered["error"]
    assert recovered["data"]["created_id"] == finished["data"]["created_id"]
    again = browser.http.post("/shijhon/api/playlists/requests", headers=auth, json=payload)
    assert again.status_code == 200 and again.json()["id"] == ident
    assert len(w.server.call(lambda: service.repo.runs(ident))) == 1


@pytest.mark.parametrize("preview", [False, True])
@pytest.mark.parametrize("name", ["", "  Prompt playlist  ", "Described playlist"])
def test_prompt_without_history_resolves_catalog_and_uses_direct_or_preview_publication(
    world: tuple[DeliveryWorld, Browser],
    preview: bool,
    name: str,
) -> None:
    w, browser = world
    service = w.services.playlists
    assert service
    captured: list[dict[str, Any]] = []

    async def ai(connection: Connection, prompt: str, *, schema: Any, **kwargs: Any) -> Any:
        import json

        payload = json.loads(prompt)
        captured.append(payload)
        assert "history" not in payload and "library" not in payload
        if schema is Proposals:
            return {
                "tracks": [
                    {"artist": s.artist, "title": s.title, "album": RELEASE.title, "version": ""}
                    for s in SONGS
                ]
            }
        assert schema is (NamedOrdered if name == "" else Ordered)
        return {
            **({"name": "Lanterns After Rain"} if name == "" else {}),
            "goal": "A synthetic night walk",
            "constraints_satisfied": True,
            "selections": [
                {"id": c["id"], "reason": "Fits the requested synthetic playlist", "fit": "meets"}
                for c in reversed(payload["candidates"])
            ],
        }

    async def install() -> None:
        service.curator.api = ai

    w.server.call(install)
    form = browser.get("playlists/describe")
    name_input = re.search(r'<input name="name"[^>]*>', form.text)
    assert name_input and "Leave blank to choose automatically" in name_input[0]
    assert "required" not in name_input[0] and "value=" not in name_input[0]
    response = browser.submit(
        "playlists",
        {
            "backend": "api",
            "endpoint": "https://model.invalid/v1",
            "model": "synthetic",
        },
        "playlists/connection",
    )
    assert response.status_code == 303
    response = browser.submit(
        "playlists/describe",
        {
            "name": name,
            "prompt": "An ordered synthetic night walk",
            "count": "3",
            "max_per_artist": "3",
            "preview": "true" if preview else "false",
        },
    )
    assert response.status_code == 303
    ident = response.headers["location"].rsplit("/", 1)[1]
    run = w.server.call(lambda: service.repo.runs(ident))[0]
    result = wait_state(w, run["id"], ("preview", "published", "failed", "uncertain"))
    assert result["state"] == ("preview" if preview else "published"), result["error"]
    assert [s["title"] for s in result["data"]["selected"]] == [s.title for s in reversed(SONGS)]
    assert len(captured) == 2
    expected_name = name or "Lanterns After Rain"
    if preview:
        assert browser.post(f"playlists/describe/{ident}", {"action": "publish"}).status_code == 403
        response = browser.post(
            f"playlists/describe/{ident}",
            {"csrf": browser.csrf(), "action": "publish", "run": run["id"]},
        )
        assert response.status_code == 303
        result = wait_state(w, run["id"], ("published", "failed", "uncertain"))
        assert result["state"] == "published", result["error"]
    page = browser.get(f"playlists/describe/{ident}")
    assert page.status_code == 200 and "One-off; no regular refresh" in page.text
    assert "<dt>Listening history</dt><dd>Not used</dd>" in page.text
    assert "<dt>Library as context</dt><dd>Not used</dd>" in page.text

    async def verify_name() -> None:
        row = await service.repo.store.fetchone(
            "SELECT owner FROM playlist_requests WHERE id=?", [ident]
        )
        connection = await service.connection(row["owner"])
        assert (await service.requests.status(connection.user_id, ident))["name"] == expected_name
        mix = await service.repo.mix(ident, connection.user_id)
        native = await service.client.playlist(connection, mix["playlist_id"])
        assert native["name"] == expected_name
        assert not result["data"]["definition"]["auto_name"]

    w.server.call(verify_name)


def test_agent_oauth_revoke_needs_csrf_and_credentials_are_not_shown(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    assert (
        browser.post("playlists/agents", {"action": "mint", "name": "no-csrf"}).status_code == 403
    )
    key = mint(browser)
    page = browser.get("playlists/describe")
    assert key not in page.text
    service = w.services.playlists
    assert service
    assert service.oauth
    token = w.server.call(lambda: service.oauth.verify_token(key))
    assert token and token.claims

    response = browser.post(
        "playlists/agents",
        {"csrf": browser.csrf(), "action": "revoke", "agent": token.claims["grant_id"]},
    )
    assert response.status_code == 303
    response = browser.http.post(
        "/shijhon/api/playlists/context", headers={"authorization": "Bearer " + key}, json={}
    )
    assert response.status_code == 401


def test_explicit_recurrence_keeps_native_id_and_respects_pause_during_refresh(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    import json

    w, browser = world
    service = w.services.playlists
    assert service
    ordered_calls = 0
    ident = ""

    async def ai(connection: Connection, prompt: str, *, schema: Any, **kwargs: Any) -> Any:
        nonlocal ordered_calls
        payload = json.loads(prompt)
        if schema is Proposals:
            return {
                "tracks": [
                    {
                        "artist": "Inkwell & Reed",
                        "title": "Still Water",
                        "album": "Quiet Room",
                        "version": "",
                    }
                ]
            }
        ordered_calls += 1
        if ordered_calls == 2:
            # The owner pauses after this refresh started, while its model is working.
            await service.repo.store.execute(
                "UPDATE playlist_mixes SET enabled=0 WHERE id=?", [ident]
            )
        return {
            "goal": "One quiet recording",
            "constraints_satisfied": True,
            "selections": [
                {"id": payload["candidates"][0]["id"], "reason": "Fits", "fit": "meets"}
            ],
        }

    async def install() -> None:
        service.curator.api = ai

    w.server.call(install)
    assert (
        browser.submit(
            "playlists",
            {"backend": "api", "endpoint": "https://model.invalid/v1", "model": "synthetic"},
            "playlists/connection",
        ).status_code
        == 303
    )
    response = browser.submit(
        "playlists/describe",
        {
            "name": "Saved instructions",
            "prompt": "One quiet recording",
            "count": "1",
            "save_instructions": "true",
            "cadence": "daily",
        },
    )
    assert response.status_code == 303
    ident = response.headers["location"].rsplit("/", 1)[1]
    first = w.server.call(lambda: service.repo.runs(ident))[0]
    result = wait_state(w, first["id"], ("published", "failed", "uncertain"))
    assert result["state"] == "published", result["error"]

    async def refresh() -> tuple[str, str]:
        row = await service.repo.store.fetchone("SELECT * FROM playlist_mixes WHERE id=?", [ident])
        assert row["enabled"] and row["due"] > first["created"] + 86000
        run = await service.repo.enqueue(ident, automatic=True)
        service.notify()
        return run, row["playlist_id"]

    next_run, native_id = w.server.call(refresh)
    result = wait_state(w, next_run, ("published", "discarded", "failed", "uncertain"))
    assert result["state"] == "discarded", result["error"]
    row = w.server.call(
        lambda: service.repo.store.fetchone("SELECT * FROM playlist_mixes WHERE id=?", [ident])
    )
    assert row["playlist_id"] == native_id and not row["enabled"]


@pytest.mark.parametrize("dash", ["\u2013", "\u2014"])
def test_title_only_unicode_edition_survives_native_publication_and_recovery(
    world: tuple[DeliveryWorld, Browser],
    dash: str,
) -> None:
    w, browser = world
    service = w.services.playlists
    assert service
    key = mint(browser)
    auth = {"Authorization": "Bearer " + key, "Idempotency-Key": f"unicode-{ord(dash)}"}
    title = f"Song {dash} 2011 Remaster"
    payload = {
        "name": f"Unicode edition {ord(dash)}",
        "count": 1,
        "tracks": [{"artist": "Aster & Vale", "title": title}],
    }
    response = browser.http.post("/shijhon/api/playlists/requests", headers=auth, json=payload)
    assert response.status_code == 202, response.text
    ident, run_id = response.json()["id"], response.json()["run"]
    published = wait_state(w, run_id, ("published", "failed", "uncertain"))
    assert published["state"] == "published", published["error"]
    assert published["data"]["selected"][0]["title"] == "Song (2011 Remaster)"
    assert published["data"]["selected"][0]["required_title"] == title

    async def uncertain() -> None:
        await service.repo.update(run_id, "uncertain", published["data"])

    w.server.call(uncertain)
    retried = browser.http.post(
        f"/shijhon/api/playlists/requests/{ident}/retry",
        headers=auth,
        json={},
    )
    assert retried.status_code == 202
    recovered = wait_state(w, run_id, ("published", "failed", "uncertain"))
    assert recovered["state"] == "published", recovered["error"]
    assert recovered["data"] == published["data"]
    # The same native ID cannot become a different remaster during reconciliation.
    path = w.nd.music / "Aster & Vale/Remaster evidence/1-01 Song (2011 Remaster).flac"
    try:
        audio = FLAC(path)
        audio["title"] = ["Song (2024 Remaster)"]
        audio.save()
        w.nd.scan()
        w.server.call(uncertain)
        retried = browser.http.post(
            f"/shijhon/api/playlists/requests/{ident}/retry",
            headers=auth,
            json={},
        )
        assert retried.status_code == 202
        refused = wait_state(w, run_id, ("published", "failed", "uncertain"))
        assert refused["state"] == "uncertain" and "selected recording" in refused["error"]
        assert refused["data"]["created_id"] == published["data"]["created_id"]
        assert refused["data"]["selected"] == published["data"]["selected"]
        assert refused["data"]["native_ids"] == published["data"]["native_ids"]
    finally:
        audio = FLAC(path)
        audio["title"] = ["Song (2011 Remaster)"]
        audio.save()
        w.nd.scan()


def test_failed_scheduled_selection_retries_once_and_keeps_native_schedule_paused(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    import json

    w, browser = world
    service = w.services.playlists
    assert service
    generations = 0
    fail_next = False

    async def ai(connection: Connection, prompt: str, *, schema: Any, **kwargs: Any) -> Any:
        nonlocal generations, fail_next
        data = json.loads(prompt)
        if schema is Proposals:
            generations += 1
            if fail_next:
                fail_next = False
                raise PlaylistError("Synthetic scheduled selection failure.")
            return {
                "tracks": [
                    {
                        "artist": "Inkwell & Reed",
                        "title": "Still Water",
                        "album": "Quiet Room",
                        "version": "",
                    }
                ]
            }
        return {
            "goal": "One quiet recording",
            "constraints_satisfied": True,
            "selections": [{"id": data["candidates"][0]["id"], "reason": "Fits", "fit": "meets"}],
        }

    async def install() -> None:
        service.curator.api = ai

    w.server.call(install)
    assert (
        browser.submit(
            "playlists",
            {"backend": "api", "endpoint": "https://model.invalid/v1", "model": "synthetic"},
            "playlists/connection",
        ).status_code
        == 303
    )
    key = mint(browser)
    auth = {"Authorization": "Bearer " + key, "Idempotency-Key": "scheduled-retry-native"}
    payload = {
        "name": "Retry paused instructions",
        "prompt": "One quiet recording",
        "count": 1,
        "save_instructions": True,
        "cadence": "daily",
    }
    response = browser.http.post("/shijhon/api/playlists/requests", headers=auth, json=payload)
    assert response.status_code == 202
    ident, first_run = response.json()["id"], response.json()["run"]
    first = wait_state(w, first_run, ("published", "failed", "uncertain"))
    assert first["state"] == "published", first["error"]

    async def schedule() -> str:
        nonlocal fail_next
        fail_next = True
        run_id = await service.repo.enqueue(ident, automatic=True)
        service.notify()
        return run_id

    scheduled = w.server.call(schedule)
    failed = wait_state(w, scheduled, ("failed", "uncertain", "published", "discarded"))
    assert failed["state"] == "failed" and generations == 2
    status_path = f"/shijhon/api/playlists/requests/{ident}"
    assert not browser.http.get(status_path, headers=auth).json()["refreshes"]
    response = browser.http.post(status_path + "/retry", headers=auth, json={})
    assert response.status_code == 202, response.text
    result = wait_state(w, scheduled, ("published", "failed", "uncertain", "discarded"))
    assert result["state"] == "published", result["error"]
    assert not result["automatic"] and result["data"]["enable_after"] is False
    assert generations == 3
    status = browser.http.get(status_path, headers=auth).json()
    assert status["playlist_id"] == first["data"]["created_id"]
    assert not status["refreshes"]
    duplicate = browser.http.post("/shijhon/api/playlists/requests", headers=auth, json=payload)
    assert duplicate.status_code == 200 and duplicate.json()["id"] == ident
    assert len(w.server.call(lambda: service.repo.runs(ident))) == 2
    assert browser.http.post(status_path + "/retry", headers=auth, json={}).status_code == 409

    async def check_no_background_retry() -> None:
        await service.tick()
        assert len(await service.repo.runs(ident)) == 2

    w.server.call(check_no_background_retry)
    assert generations == 3


def test_references_are_read_from_native_playlists_and_shown_with_facts(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    import json

    w, browser = world
    service = w.services.playlists
    assert service
    prompts: list[tuple[Any, dict[str, Any]]] = []

    async def ai(connection: Connection, prompt: str, *, schema: Any, **kwargs: Any) -> Any:
        payload = json.loads(prompt)
        prompts.append((schema, payload))
        if schema is Proposals:
            return {
                "tracks": [
                    {"artist": "Inkwell & Reed", "title": t, "album": "Quiet Room", "version": ""}
                    for t in ("Still Water", "Still Water (Live)")
                ]
                + [
                    {"artist": s.artist, "title": s.title, "album": RELEASE.title, "version": ""}
                    for s in SONGS[:1]
                ]
            }
        if "previous_selection" in payload:  # the second look at a flagged pick: keep it
            return {
                "goal": "Brighter than the reference",
                "constraints_satisfied": True,
                "selections": [
                    {"id": p["id"], "reason": p["reason"], "fit": p["fit"]}
                    for p in payload["previous_selection"]
                ],
            }
        titles = {c["title"]: c["id"] for c in payload["candidates"]}
        return {
            "goal": "Brighter than the reference",
            "constraints_satisfied": True,
            "selections": [
                {"id": titles["Still Water (Live)"], "reason": "Live energy", "fit": "meets"},
                {"id": titles[SONGS[0].title], "reason": "Brighter", "fit": "partly"},
            ],
        }

    async def prepare() -> str:
        service.curator.api = ai
        row = await service.repo.store.fetchone(
            "SELECT id FROM playlist_owners WHERE username=?", [ADMIN_USER]
        )
        connection = await service.connection(row["id"])
        found = await service.client.call(
            connection, "search3", [("query", "Still Water"), ("songCount", "10")]
        )
        studio = next(
            s["id"] for s in found["searchResult3"]["song"] if s["title"] == "Still Water"
        )
        created = await service.client.call(
            connection,
            "createPlaylist",
            [("name", "Paper Lantern Evenings"), ("songId", studio)],
        )
        return str(created["playlist"]["id"])

    reference = w.server.call(prepare)
    assert (
        browser.submit(
            "playlists",
            {"backend": "api", "endpoint": "https://model.invalid/v1", "model": "synthetic"},
            "playlists/connection",
        ).status_code
        == 303
    )
    key = mint(browser)
    library_key = connect_agent(
        browser.http,
        str(browser.http.base_url).rstrip("/"),
        ADMIN_USER,
        ADMIN_PASSWORD,
        "create status library",
    )["access_token"]
    path = "/shijhon/api/playlists/requests"
    payload = {
        "name": "Brighter lanterns",
        "prompt": "A brighter companion to paper lantern evenings",
        "count": 2,
        "save_instructions": True,
        "cadence": "daily",
    }
    # Without the library grant, names in the instructions are not looked up.
    auth = {"Authorization": "Bearer " + key, "Idempotency-Key": "reference-name-no-library"}
    response = browser.http.post(path, headers=auth, json={**payload, "preview": True})
    assert response.status_code == 202, response.text
    unread = wait_state(w, response.json()["run"], ("preview", "failed", "uncertain"))
    assert unread["state"] == "preview", unread["error"]
    assert all("reference" not in payload for _, payload in prompts)
    assert unread["data"]["curation"]["references"]["playlists"] == []
    status = browser.http.get(f"{path}/{response.json()['id']}", headers=auth).json()
    assert status["references"]["playlists"] == []
    prompts.clear()
    # With it, the name written in the instructions is found among the caller's playlists.
    auth = {"Authorization": "Bearer " + library_key, "Idempotency-Key": "reference-by-name"}
    response = browser.http.post(path, headers=auth, json=payload)
    assert response.status_code == 202, response.text
    ident, run = response.json()["id"], response.json()["run"]
    published = wait_state(w, run, ("published", "failed", "uncertain"))
    assert published["state"] == "published", published["error"]
    (_, propose), (_, order), (_, second_look) = prompts
    assert propose["reference"]["playlists"] == [
        {"name": "Paper Lantern Evenings", "detected": True}
    ]
    assert propose["reference"]["songs"] == [
        {
            "artist": "Inkwell & Reed",
            "title": "Still Water",
            "album": "Quiet Room",
            "year": 2020,
            "source": "own",
        }
    ]
    candidates = {c["title"]: c for c in order["candidates"]}
    # Only named in the instructions: shown to the AI, not removed mechanically.
    assert "Still Water" in candidates
    assert [c["title"] for c in second_look["unused_candidates"]] == ["Still Water"]
    assert candidates["Still Water (Live)"]["source"] == "own"
    catalog = candidates[SONGS[0].title]
    # An earlier test of this module may have added it: then Navidrome's split genres.
    assert catalog["source"] in ("catalog", "added") and catalog["year"] == 2010
    assert catalog["genres"] == (
        ["Hip-Hop/Rap"] if catalog["source"] == "catalog" else ["Hip-Hop", "Rap", "West Coast Rap"]
    )
    status = browser.http.get(f"{path}/{ident}", headers=auth).json()
    assert status["references"]["playlists"] == [
        {"id": reference, "name": "Paper Lantern Evenings", "songs": 1, "detected": True}
    ]
    assert status["goal"] == "Brighter than the reference" and status["left_out"] == 0
    assert status["revision"] == "revised"
    assert [(s["title"], s["fit"]) for s in status["selected"]] == [
        ("Still Water (Live)", "meets"),
        (SONGS[0].title, "partly"),
    ]
    assert status["coverage"]["sound"] == "off" and status["coverage"]["tags"] == "off"

    named = ident

    # An explicit reference by ID, keeping its songs available. Choosing one is a library
    # read: the agent needs the library grant.
    prompts.clear()
    explicit = {
        "name": "Lanterns again",
        "prompt": "More of the same",
        "count": 2,
        "reference_playlists": [reference],
        "exclude_reference": False,
        "preview": True,
    }
    refused = {"Authorization": "Bearer " + key, "Idempotency-Key": "reference-by-id"}
    response = browser.http.post(path, headers=refused, json=explicit)
    assert response.status_code == 403, response.text
    auth["Idempotency-Key"] = "reference-by-id"
    response = browser.http.post(path, headers=auth, json=explicit)
    assert response.status_code == 202, response.text
    previewed = wait_state(w, response.json()["run"], ("preview", "failed", "uncertain"))
    assert previewed["state"] == "preview", previewed["error"]
    assert prompts[0][1]["reference"]["leave_out_reference_songs"] is False
    assert "Still Water" in {c["title"] for c in prompts[1][1]["candidates"]}
    playlists = previewed["data"]["curation"]["references"]["playlists"]
    assert playlists == [
        {"id": reference, "name": "Paper Lantern Evenings", "songs": 1, "detected": False}
    ]
    # A playlist the caller cannot read fails the request clearly.
    auth["Idempotency-Key"] = "reference-missing"
    payload = {"name": "Missing", "prompt": "x", "count": 1, "reference_playlists": ["absent"]}
    response = browser.http.post(path, headers=auth, json=payload)
    assert response.status_code == 202, response.text
    failed = wait_state(w, response.json()["run"], ("published", "failed", "uncertain"))
    assert failed["state"] == "failed"
    assert failed["error"] == 'The reference playlist "absent" is unavailable to this account.'

    # The agent's grant loses library: the next scheduled refresh looks nothing up.
    async def reduce_and_refresh() -> str:
        assert service.oauth
        token = await service.oauth.verify_token(library_key)
        assert token and token.claims
        await service.repo.store.execute(
            "UPDATE playlist_agent_tokens SET scopes=? WHERE id=?",
            [json.dumps(["create", "status"]), token.claims["grant_id"]],
        )
        run_id = await service.repo.enqueue(named, automatic=True)
        service.notify()
        return run_id

    prompts.clear()
    refresh = w.server.call(reduce_and_refresh)
    refreshed = wait_state(w, refresh, ("published", "failed", "uncertain", "discarded"))
    assert refreshed["state"] == "published", refreshed["error"]
    assert prompts and all("reference" not in payload for _, payload in prompts)
    assert refreshed["data"]["curation"]["references"]["playlists"] == []


def test_a_named_playlist_needs_the_library_grant_until_the_selection_is_saved(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    import json

    from shijhon.playlists.model import LIBRARY_LOST

    w, browser = world
    service = w.services.playlists
    assert service
    prompts: list[dict[str, Any]] = []

    async def ai(connection: Connection, prompt: str, *, schema: Any, **kwargs: Any) -> Any:
        payload = json.loads(prompt)
        prompts.append(payload)
        if schema is Proposals:
            return {
                "tracks": [
                    {
                        "artist": "Inkwell & Reed",
                        "title": "Still Water (Live)",
                        "album": "Quiet Room",
                        "version": "",
                    }
                ]
            }
        return {
            "goal": "Livelier",
            "constraints_satisfied": True,
            "selections": [{"id": payload["candidates"][0]["id"], "reason": "", "fit": "meets"}],
        }

    assert (
        browser.submit(
            "playlists",
            {"backend": "api", "endpoint": "https://model.invalid/v1", "model": "synthetic"},
            "playlists/connection",
        ).status_code
        == 303
    )
    key = connect_agent(
        browser.http,
        str(browser.http.base_url).rstrip("/"),
        ADMIN_USER,
        ADMIN_PASSWORD,
        "create status library",
    )["access_token"]
    generate = service.generate

    async def prepare() -> None:
        service.curator.api = ai
        assert service.oauth
        token = await service.oauth.verify_token(key)
        assert token and token.claims
        row = await service.repo.store.fetchone(
            "SELECT id FROM playlist_owners WHERE username=?", [ADMIN_USER]
        )
        connection = await service.connection(row["id"])
        found = await service.client.call(
            connection, "search3", [("query", "Still Water"), ("songCount", "10")]
        )
        studio = next(
            s["id"] for s in found["searchResult3"]["song"] if s["title"] == "Still Water"
        )
        await service.client.call(
            connection, "createPlaylist", [("name", "Harbor Glass Evenings"), ("songId", studio)]
        )

        async def generated_then_reduced(connection: Connection, mix: Any) -> Any:
            data = await generate(connection, mix)
            # The agent's grant loses library after selection, before it is saved.
            await service.repo.store.execute(
                "UPDATE playlist_agent_tokens SET scopes=? WHERE id=?",
                [json.dumps(["create", "status"]), token.claims["grant_id"]],
            )
            return data

        service.generate = generated_then_reduced  # type: ignore[method-assign]

    async def restore() -> None:
        service.generate = generate  # type: ignore[method-assign]

    w.server.call(prepare)
    try:
        path = "/shijhon/api/playlists/requests"
        auth = {"Authorization": "Bearer " + key, "Idempotency-Key": "named-grant-lost"}
        payload = {"name": "Harbor glow", "prompt": "Livelier than Harbor Glass Evenings"}
        response = browser.http.post(path, headers=auth, json={**payload, "count": 1})
        assert response.status_code == 202, response.text
        failed = wait_state(w, response.json()["run"], ("preview", "published", "failed"))
    finally:
        w.server.call(restore)
    # The model saw the playlist while the grant held; nothing of it was saved or shown.
    assert prompts[0]["reference"]["playlists"] == [
        {"name": "Harbor Glass Evenings", "detected": True}
    ]
    assert failed["state"] == "failed" and failed["error"] == LIBRARY_LOST
    assert "curation" not in failed["data"]
    status = browser.http.get(f"{path}/{response.json()['id']}", headers=auth)
    assert status.status_code == 200 and "Harbor Glass" not in status.text
    assert status.json()["references"] == {}
