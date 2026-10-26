"""Describe a playlist in the dashboard against a disposable Navidrome: the account's own
playlists are offered as references, the chosen ones and the exclusion are sent, and the
request's page shows the references, goal, fit and reasons the run recorded."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import pytest

from shijhon.catalog.base import SearchResults
from shijhon.playlists.model import Connection
from shijhon.playlists.prompt import Ordered, Proposals
from tests.acceptance.test_R_playlists import wait_state
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import Browser, forms
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.replay import Replay

ARTIST, ALBUM = "Copper Wren", "Kitchen Radio"
TITLES = ("Morning Static", "Kettle Song", "Window Light", "Late Bus")
HOSTILE_NAME = 'Late <em>Lanterns</em> & "Rain"'
HOSTILE_REASON = "<script>alert(1)</script> keeps the pace up"


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[DeliveryWorld, Browser]]:
    nd = navidrome_factory()
    write_album(
        nd.music, Album(ARTIST, ALBUM, tuple(Track(t, n + 1) for n, t in enumerate(TITLES)))
    )
    nd.scan()
    catalog = Replay().catalog()

    async def search(term: str, limit: int = 20) -> SearchResults:
        return SearchResults()

    catalog.search = search  # type: ignore[method-assign]
    with delivery_world(nd, tmp_path_factory.mktemp("describe"), catalog=catalog) as w:
        browser = Browser(w.server.base_url)
        assert browser.sign_in(ADMIN_USER, ADMIN_PASSWORD).status_code == 303
        for fields in (
            {"password": ADMIN_PASSWORD, "history_user": "", "backend": "none"},
            {"backend": "api", "endpoint": "https://model.invalid/v1", "model": "synthetic"},
        ):
            response = browser.submit("playlists", fields, "playlists/connection")
            assert response.status_code == 303
        yield w, browser
        browser.close()


def test_chosen_reference_is_sent_and_the_result_page_shows_the_curation(
    world: tuple[DeliveryWorld, Browser],
) -> None:
    w, browser = world
    service = w.services.playlists
    assert service

    async def ai(connection: Connection, prompt: str, *, schema: Any, **kwargs: Any) -> Any:
        payload = json.loads(prompt)
        if schema is Proposals:
            return {
                "tracks": [
                    {"artist": ARTIST, "title": t, "album": ALBUM, "version": ""} for t in TITLES
                ]
            }
        assert schema is Ordered
        ids = {c["title"]: c["id"] for c in payload["candidates"]}
        return {
            "goal": "Brighter than the kitchen mornings",
            "constraints_satisfied": True,
            "selections": [
                {"id": ids["Late Bus"], "reason": HOSTILE_REASON, "fit": "meets"},
                {"id": ids["Window Light"], "reason": "A calmer close", "fit": "partly"},
            ],
        }

    async def prepare() -> dict[str, str]:
        service.curator.api = ai
        row = await service.repo.store.fetchone(
            "SELECT id FROM playlist_owners WHERE username=?", [ADMIN_USER]
        )
        connection = await service.connection(row["id"])
        found = await service.client.call(
            connection, "search3", [("query", ARTIST), ("songCount", "10")]
        )
        songs = {s["title"]: s["id"] for s in found["searchResult3"]["song"]}
        made = {}
        for name, titles in (
            ("Sunday Kitchen", ("Morning Static", "Kettle Song")),
            (HOSTILE_NAME, ("Kettle Song",)),
            ("another Mix", ()),
        ):
            created = await service.client.call(
                connection,
                "createPlaylist",
                [("name", name), *(("songId", songs[t]) for t in titles)],
            )
            made[name] = str(created["playlist"]["id"])
        return made

    made = w.server.call(prepare)
    page = browser.get("playlists/describe")
    assert page.status_code == 200
    picker = page.text.split('name="reference_playlist_1"')[1].split("</select>")[0]
    offered = re.findall(r'<option value="([^"]*)"', picker)
    # The account's own playlists, by name without case, after "None".
    assert offered == [
        "",
        made["another Mix"],
        made[HOSTILE_NAME],
        made["Sunday Kitchen"],
    ]
    assert "Late &lt;em&gt;Lanterns&lt;/em&gt; &amp; &#34;Rain&#34; (1 song)" in picker
    assert HOSTILE_NAME not in page.text
    assert forms(page.text)["/shijhon/playlists/describe"]["exclude_reference"] == "true"

    response = browser.submit(
        "playlists/describe",
        {
            "name": "Brighter kitchen",
            "prompt": "Like my kitchen playlist but brighter",
            "count": "2",
            "max_per_artist": "2",
            "reference_playlist_1": made["Sunday Kitchen"],
            "reference_playlist_2": made[HOSTILE_NAME],
            "preview": "true",
        },
    )
    assert response.status_code == 303, response.text
    ident = response.headers["location"].rsplit("/", 1)[1]

    async def saved() -> dict[str, Any]:
        row = await service.repo.store.fetchone(
            "SELECT input FROM playlist_requests WHERE id=?", [ident]
        )
        return dict(json.loads(row["input"]))

    sent = w.server.call(saved)
    assert sent["reference_playlists"] == [made["Sunday Kitchen"], made[HOSTILE_NAME]]
    assert "exclude_reference" not in sent  # the default: leave reference songs out
    run = w.server.call(lambda: service.repo.runs(ident))[0]
    result = wait_state(w, run["id"], ("preview", "failed", "uncertain"))
    assert result["state"] == "preview", result["error"]
    assert result["data"]["curation"]["left_out"] == 2  # Morning Static, Kettle Song

    page = browser.get(f"playlists/describe/{ident}")
    assert page.status_code == 200
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page.text))
    assert "Compared with" in text and "Sunday Kitchen · 2 songs · chosen" in text
    assert "Late &lt;em&gt;Lanterns&lt;/em&gt; &amp; &#34;Rain&#34; · 1 song · chosen" in text
    assert "left out: 2 suggested songs skipped" in text
    assert "Goal: Brighter than the kitchen mornings" in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt; keeps the pace up" in text
    assert "<script>" not in page.text and "<em>Lanterns" not in page.text
    assert '<span class="tag tag-meets">Fits</span>' in page.text
    assert '<span class="tag tag-partly">Partly fits</span>' in page.text
    assert page.text.count('<span class="tag">Your files</span>') == 2
    # No sound analysis or Last.fm here: nothing about them is shown.
    assert "sound facts" not in text and "Last.fm tags" not in text
    assert "energy-bar" not in page.text

    # Unchecking the box sends only its hidden "false".
    data = forms(browser.get("playlists/describe").text)["/shijhon/playlists/describe"]
    data.update(
        name="Kitchen again",
        prompt="More of the kitchen",
        count="2",
        reference_playlist_1=made["Sunday Kitchen"],
        exclude_reference="false",
        preview="true",
    )
    response = browser.post("playlists/describe", data)
    assert response.status_code == 303
    ident = response.headers["location"].rsplit("/", 1)[1]
    sent = w.server.call(saved)
    assert sent["reference_playlists"] == [made["Sunday Kitchen"]]
    assert sent["exclude_reference"] is False
    run = w.server.call(lambda: service.repo.runs(ident))[0]
    assert wait_state(w, run["id"], ("preview", "failed", "uncertain"))["state"] == "preview"
    text = re.sub(
        r"\s+", " ", re.sub(r"<[^>]+>", " ", browser.get(f"playlists/describe/{ident}").text)
    )
    assert "Songs already in the reference could be chosen again." in text
