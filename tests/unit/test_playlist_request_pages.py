"""The dashboard's described-playlist pages: choosing reference playlists, and a request's
curation (references, goal, fit, facts, energy) shown from run data, invented music only."""

from __future__ import annotations

import json
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import anyio
import httpx
from jinja2 import Environment, PackageLoader, StrictUndefined
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Mount

from shijhon.dashboard import app as dashboard_app
from shijhon.dashboard.auth import Session
from shijhon.dashboard.playlist_requests import Pages, curation, edits_view
from shijhon.dashboard.playlists import Pages as MixPages
from shijhon.playlists import edits as owner_edits
from shijhon.playlists.model import Definition, PlaylistError, Track
from shijhon.playlists.requests import PlaylistRequest
from shijhon.playlists.service import Playlists
from tests.harness.dashboard import forms
from tests.unit.test_playlist_edits import PUBLISHED, Model, publish, refresh, refreshing, titles
from tests.unit.test_playlist_requests import connected, service_at

SESSION = Session("hash", "owner", "csrf", 0, "owner")
HOSTILE = '<script>alert("x")</script>'


def dashboard(service: Playlists) -> SimpleNamespace:
    """The real templates, rendered as the dashboard renders them."""
    env = Environment(
        loader=PackageLoader("shijhon.dashboard", "templates"),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(
        hm=dashboard_app.hm, day=dashboard_app.day, when=dashboard_app.when, count=str
    )

    async def render(template: str, *, session: Session, current: str, **context: Any) -> Response:
        return HTMLResponse(
            env.get_template(template).render(
                prefix="/shijhon",
                accent="green",
                user=session.username,
                csrf=session.csrf,
                current=current,
                nav=[],
                is_admin=False,
                home="collection",
                version="test",
                copyright="2026",
                license="AGPL-3.0-or-later",
                source_url="https://example.invalid",
                pending=[],
                can_restart=False,
                **context,
            )
        )

    return SimpleNamespace(
        services=SimpleNamespace(playlists=service),
        _render=render,
        _see=lambda where: RedirectResponse(f"/shijhon{where}", 303),
    )


def listing(*playlists: dict[str, Any]) -> AsyncMock:
    async def call(connection: Any, method: str, params: Any = ()) -> dict[str, Any]:
        assert method == "getPlaylists"
        return {"playlists": {"playlist": list(playlists)}}

    return AsyncMock(side_effect=call)


def own(ident: str, name: str, songs: int = 3, owner: str = "owner") -> dict[str, Any]:
    return {"id": ident, "name": name, "songCount": songs, "owner": owner}


async def page(pages: Pages, method: str = "GET", form: dict[str, str] | None = None) -> str:
    request = Request({"type": "http", "method": method, "path_params": {}})
    request.state.form = form or {}
    response = await pages.index(request, SESSION)
    return bytes(response.body).decode()


def test_the_form_offers_the_accounts_own_playlists_by_name(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            service.client.call = listing(
                own("pl-b", "velvet Orbit", 1),
                own("pl-a", "Copper Wren Mornings", 41),
                own("pl-x", "Someone else's mix", owner="listener"),
                own("bad id!", "Unusable ID"),
                own("pl-h", HOSTILE),
            )
            html = await page(Pages(dashboard(service)))
            names = [
                re.sub(r" \(\d+ songs?\)$", "", n)
                for n in re.findall(
                    r'<option value="pl-[^"]+">([^<]*)</option>',
                    html.split('name="reference_playlist_1"')[1].split("</select>")[0],
                )
            ]
            # Sorted without case; another account's playlist and an unusable ID left out.
            assert names == [
                "&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;",
                "Copper Wren Mornings",
                "velvet Orbit",
            ]
            assert HOSTILE not in html and "Someone else" not in html
            assert "velvet Orbit (1 song)" in html
            fields = forms(html)["/shijhon/playlists/describe"]
            assert {fields[f"reference_playlist_{n}"] for n in (1, 2, 3)} == {""}
            assert fields["exclude_reference"] == "true"  # checked by default
            assert "named in the instructions" in html

    anyio.run(run)


def test_the_form_still_works_when_the_playlists_cannot_be_read(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            service.client.call = AsyncMock(side_effect=PlaylistError("Navidrome is down."))
            html = await page(Pages(dashboard(service)))
            assert "Your playlists couldn't be read just now" in html
            assert 'name="reference_playlist_1"' not in html
            assert 'name="exclude_reference"' in html and "Create playlist" in html
            service.client.call = listing()
            html = await page(Pages(dashboard(service)))
            assert "You have no playlists of your own yet." in html

    anyio.run(run)


def test_chosen_references_and_the_exclusion_are_submitted(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            service.client.call = listing(own("pl-a", "Copper Wren Mornings"))
            pages = Pages(dashboard(service))
            form = {
                "request_key": "form-references-1",
                "name": "Brighter",
                "prompt": "Like my Copper Wren Mornings but more energetic",
                "count": "5",
                "max_per_artist": "2",
                "cadence": "manual",
                "reference_playlist_1": "pl-a",
                "reference_playlist_2": "pl-b",
                "reference_playlist_3": "pl-a",  # the same one twice counts once
                "exclude_reference": "false",  # the box unchecked: only the hidden field
            }
            request = Request({"type": "http", "method": "POST", "path_params": {}})
            request.state.form = form
            response = await pages.index(request, SESSION)
            assert response.status_code == 303
            row = await service.repo.store.fetchone("SELECT input FROM playlist_requests")
            saved = json.loads(row["input"])
            assert saved["reference_playlists"] == ["pl-a", "pl-b"]
            assert saved["exclude_reference"] is False
            # The default (box checked) keeps the canonical input of earlier requests.
            form.update(
                request_key="form-references-2",
                reference_playlist_2="",
                reference_playlist_3="",
                exclude_reference="true",
            )
            await pages.index(request, SESSION)
            rows = await service.repo.store.fetchall(
                "SELECT input FROM playlist_requests ORDER BY rowid"
            )
            assert "exclude_reference" not in json.loads(rows[1]["input"])
            # A form without references is the request it always was.
            del form["reference_playlist_1"], form["exclude_reference"]
            form["request_key"] = "form-references-3"
            await pages.index(request, SESSION)
            rows = await service.repo.store.fetchall(
                "SELECT input FROM playlist_requests ORDER BY rowid"
            )
            assert json.loads(rows[2]["input"]) == json.loads(
                PlaylistRequest(
                    name="Brighter",
                    prompt="Like my Copper Wren Mornings but more energetic",
                    count=5,
                ).canonical()
            )

    anyio.run(run)


def test_a_refused_form_keeps_what_was_entered(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            service.client.call = listing(own("pl-a", "Copper Wren Mornings"))
            html = await page(
                Pages(dashboard(service)),
                "POST",
                {
                    "request_key": "form-refused-1",
                    "prompt": f"Weekly {HOSTILE}",
                    "count": "5",
                    "max_per_artist": "2",
                    "cadence": "weekly",  # without saved instructions: refused
                    "reference_playlist_1": "pl-a",
                    "exclude_reference": "false",
                },
            )
            assert "reference playlists. Refreshes need saved instructions." in html
            assert HOSTILE not in html and "Weekly &lt;script&gt;" in html
            fields = forms(html)["/shijhon/playlists/describe"]
            assert fields["reference_playlist_1"] == "pl-a"
            assert fields["cadence"] == "weekly"
            assert "exclude_reference" in fields and fields["exclude_reference"] == "false"
            assert await service.repo.store.fetchone("SELECT id FROM playlist_requests") is None

    anyio.run(run)


def selected(n: int) -> dict[str, Any]:
    return {
        "id": f"song-{n}",
        "artist": "Inkwell & Reed",
        "title": f"Lantern {n}",
        "album": "Quiet Room",
        "reason": f"Reason {n}",
    }


CURATION = {
    "references": {
        "playlists": [
            {"id": "pl-a", "name": f"Evenings {HOSTILE}", "songs": 24, "detected": False},
            {"id": "pl-b", "name": "Velvet Orbit", "songs": 1, "detected": True},
        ],
        "songs": 0,
        "shown": 25,
        "exclude": True,
    },
    "reference_profile": {
        "songs": 25,
        "with_sound": 19,
        "energy": 55,
        "tempo": 104,
        "tags": ["mellow", HOSTILE],
        "genres": ["folk"],
        "years": [2009, 2021],
    },
    "goal": f"Brighter than the evenings {HOSTILE}",
    "revision": "revised",
    "songs": {
        "song-1": {
            "fit": "meets",
            "source": "own",
            "facts": {
                "year": 2020,
                "genres": ["Folk"],
                "tags": ["warm", HOSTILE],
                "sound": {"tempo": 112.4, "key": "A minor", "energy": 71},
            },
        },
        "song-2": {
            "fit": "partly",
            "source": "catalog",
            "facts": {"sound": {"energy": 0}, "suggested_by": "sound_similar"},
        },
        "song-3": {"fit": "misses", "source": "added", "facts": {}},
    },
    "coverage": {"songs": 3, "with_sound": 2, "with_tags": 1, "sound": "used", "tags": "used"},
    "sound_pool": {"similar": 2, "search": 1},
    "left_out": 3,
}


async def request_page(service: Playlists, data: dict[str, Any]) -> str:
    ident, _ = await service.requests.create(
        "owner", f"page-{len(json.dumps(data))}", PlaylistRequest(prompt="quiet", count=3)
    )
    run = (await service.repo.runs(ident))[0]
    await service.repo.update(run["id"], "preview", {**run["data"], **data})
    response = await Pages(dashboard(service)).render(SESSION, ident=ident)
    return bytes(response.body).decode()


def test_the_request_page_shows_references_goal_fit_facts_and_energy(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            html = await request_page(
                service,
                {
                    "selected": [
                        {**selected(1), "reason": f"Lifts it {HOSTILE}"},
                        selected(2),
                        selected(3),
                    ],
                    "releases": [],
                    "curation": CURATION,
                },
            )
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
            assert HOSTILE not in html
            assert "Evenings &lt;script&gt;" in html and "Lifts it &lt;script&gt;" in html
            assert "Velvet Orbit · 1 song · found by name in your instructions" in text
            assert "24 songs · chosen" in text
            assert "Typical of the reference: energy 55 · 104 BPM · 2009\u20132021" in text
            assert "19 of 25 reference songs have sound facts" in text
            assert "left out: 3 suggested songs skipped" in text
            assert "The AI was asked not to repeat songs of playlists found by name" in text
            assert "Goal: Brighter than the evenings &lt;script&gt;" in text
            assert "Revised once" in text
            assert "2 of 3 songs have sound facts · 1 of 3 songs has Last.fm tags" in text
            assert "Sound analysis suggested 3 library songs as candidates" in text
            # Fit and source in words, each song's own facts, energy beside its reference.
            assert '<span class="tag tag-meets">Fits</span>' in html
            assert '<span class="tag tag-partly">Partly fits</span>' in html
            assert '<span class="tag tag-misses">Doesn&#39;t fit</span>' in html
            for words in ("Your files", "From the catalog", "Added by Shijhon"):
                assert f'<span class="tag">{words}</span>' in html
            assert "Sounds like the reference" in html
            assert "2020 · Folk · tags: warm, &lt;script&gt;" in text
            assert "A minor" in text
            assert "Energy 71 · reference 55" in text and "112 BPM · reference 104" in text
            assert "Energy 0 · reference 55" in text
            bars = re.findall(r'class="energy-fill"[^>]*width="([\d.]+)"', html)
            assert bars == ["70.3", "1.0"]
            assert html.count('class="energy-reference" x1="55.0"') == 2
            assert 'aria-hidden="true"' in html.split('class="energy-bar"')[1][:60]

    anyio.run(run)


def test_runs_without_curation_still_show_their_songs(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            html = await request_page(
                service, {"selected": [selected(1), selected(2)], "releases": []}
            )
            assert "Lantern 1" in html and "Reason 2" in html
            for absent in ("Compared with", "Goal:", "energy-bar", "tag-meets", "sound facts"):
                assert absent not in html
            # Data of an unexpected shape is left out, never an error.
            html = await request_page(
                service,
                {
                    "selected": [selected(1)],
                    "releases": [],
                    "curation": {
                        "references": {"playlists": "x", "songs": "many"},
                        "reference_profile": {"energy": 250, "years": [2020]},
                        "goal": 7,
                        "revision": ["revised"],
                        "songs": {"song-1": {"fit": "great", "facts": {"sound": "loud"}}},
                        "coverage": {"songs": "3", "sound": "used"},
                        "sound_pool": {"similar": "2"},
                    },
                },
            )
            assert "Lantern 1" in html
            songs = html.split('class="curated"')[1]  # the page head has its state's tag
            for absent in ("Compared with", "Goal:", "energy-bar", "sound facts"):
                assert absent not in html
            assert "tag-" not in songs

    anyio.run(run)


def test_curation_view_of_a_run_found_by_name_without_sound() -> None:
    view = curation(
        {
            "selected": [{**selected(1), "fit": "meets", "source": "own", "facts": {}}],
            "references": {
                "playlists": [{"id": "pl", "name": "Velvet Orbit", "songs": 9, "detected": True}],
                "songs": 0,
                "exclude": False,
            },
            "reference_profile": {"songs": 9, "with_sound": 0, "years": [2011, 2011]},
            "revision": "none",
            "coverage": {"songs": 1, "with_sound": 0, "with_tags": 0, "sound": "unavailable"},
        }
    )
    assert view["detected"] and not view["chosen"] and not view["exclude"]
    assert view["summary"] == ["2011"] and view["reference_sound"] == (0, 9)
    assert view["revision"] == "" and view["facts"] == ["0 of 1 songs have sound facts"]
    assert view["sound_unavailable"] and not view["tags_unavailable"] and not view["energy"]
    assert view["songs"][0]["fit"] == "Fits" and view["songs"][0]["energy"] is None


# --- What refreshes learned from the owner's edits, and forgetting it ---------------------

REFRESHING = PlaylistRequest(prompt="quiet", count=3, save_instructions=True, cadence="weekly")
WENT = {"id": "r-1", "artist": "Inkwell & Reed", "title": "Went", "album": ""}


def invented(prefix: str, n: int) -> list[Track]:
    return [
        Track(id=f"{prefix}-{i}", artist="Inkwell & Reed", title=f"Song {prefix} {i}")
        for i in range(n)
    ]


def plain(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


def remembered_count(service: Playlists, mix: str) -> Any:
    return service.repo.store.fetchone(
        "SELECT count(*) AS n FROM playlist_edits WHERE mix=?", [mix]
    )


@asynccontextmanager
async def web(service: Playlists, pages: type = Pages) -> AsyncIterator[httpx.AsyncClient]:
    """The real pages behind the dashboard's own form admission (its token check), signed in
    as the account the x-account header names."""
    host = dashboard(service)

    async def session(request: Request) -> Session:
        user = request.headers.get("x-account", "owner")
        return Session("hash", user, "csrf", 0, user)

    host._session = session
    host._form = MethodType(dashboard_app.Dashboard._form, host)
    host._personal_page = MethodType(dashboard_app.Dashboard._personal_page, host)
    app = Starlette(routes=[Mount("/shijhon", routes=pages(host).routes())])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
        yield c


def test_the_request_page_shows_what_the_refresh_learned_from_edits(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            ident, _ = await service.requests.create("owner", "edits-page-1", REFRESHING)
            first = (await service.repo.runs(ident))[0]
            hostile = Track(id="r-0", artist=HOSTILE, title=f"Lantern {HOSTILE}")
            await owner_edits.remember(
                service.repo.store,
                ident,
                "run-1",
                [*invented("old", 24), hostile],  # (the latest first: the hostile one leads)
                invented("new", 2),
            )
            learned = {
                "learned": {"removed": [WENT], "added": [{**WENT, "id": "r-2", "title": "Came"}]},
                "remembered": {"removed": 25, "added": 2},
                "left_out": 2,
            }
            data = {**first["data"], "selected": [selected(1)], "releases": [], "edits": learned}
            await service.repo.update(first["id"], "published", data)
            response = await Pages(dashboard(service)).render(SESSION, ident=ident)
            html = bytes(response.body).decode()
            text = plain(html)
            assert HOSTILE not in html
            assert (
                "Learned from your edits: 25 removed songs stay out, 2 added songs guide the AI."
                in text
            )
            assert (
                "Since the playlist was last published, you removed 1 song and added 1 song."
                in text
            )
            assert "2 suggested songs were left out of this run because you removed them." in text
            # The latest 20 of each kind are listed, then how many more.
            block = html.split('class="curation learned-edits"')[1].split("</details>")[0]
            items = re.findall(r"<li>([^<]*)</li>", block)
            assert len(items) == 20 + 1 + 2 and items[20] == "and 5 more"
            assert items[0].startswith("Lantern &lt;script&gt;") and "Song old 23" in items[1]
            assert "Song new 1" in items[21] and "Song old 5 " in items[19]
            # The question comes first; the button posts from this page with its token.
            assert "Forget what Shijhon learned from your edits to this playlist?" in text
            assert 'name="action" value="forget_edits">Forget learned edits</button>' in html
            fields = forms(html)[f"/shijhon/playlists/describe/{ident}"]
            assert fields["csrf"] == SESSION.csrf

    anyio.run(run)


def test_the_page_shows_nothing_about_edits_without_edits_data(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            first_refresh = {
                "learned": {"removed": [], "added": []},
                "remembered": {"removed": 0, "added": 0},
                "left_out": 0,
            }
            for edits in (
                None,  # an older run, or a one-off request
                {},
                first_refresh,
                "unexpected",
                {"learned": "x", "remembered": [1], "left_out": "many"},
                {"learned": {"removed": "a", "added": [1, "b"]}, "left_out": True},
            ):
                extra = {} if edits is None else {"edits": edits}
                html = await request_page(
                    service,
                    {
                        "selected": [selected(1)],
                        "releases": [],
                        "x": "y" * len(str(edits)),
                        **extra,
                    },
                )
                assert "Lantern 1" in html
                for absent in ("Learned from your edits", "Forget learned edits", "forget_edits"):
                    assert absent not in html

    anyio.run(run)


def test_edits_in_display_words() -> None:
    one = [Track(id="a", artist="Inkwell & Reed", title="Went")]
    view = edits_view({}, owner_edits.Edits(one, one))
    assert view
    assert view["summary"] == "1 removed song stays out, 1 added song guides the AI"
    assert view["removed"] == {"songs": ["Went · Inkwell & Reed"], "more": 0}
    assert not view["noticed"] and not view["left_out"]
    # Songs forgotten since a run left out a suggestion: the run's own record stays.
    view = edits_view(
        {"left_out": 1, "learned": {"removed": [WENT], "added": [WENT, WENT]}}, owner_edits.Edits()
    )
    assert view
    assert view["summary"] == "nothing is remembered for this playlist now"
    assert not view["remembered"]
    assert (
        view["noticed"]
        == "Since the playlist was last published, you removed 1 song and added 2 songs."
    )
    assert view["left_out"] == "1 suggested song was left out of this run because you removed them."
    assert edits_view(None, owner_edits.Edits()) is None
    assert edits_view({"left_out": 0}, owner_edits.Edits()) is None


def test_the_owner_forgets_learned_edits_with_the_page_button_only(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            neighbour = connected(
                "neighbour", backend="api", endpoint="https://model.invalid/v1", model="m"
            )
            await service.repo.connect(neighbour)
            mine, _ = await service.requests.create("owner", "forget-mine-1", REFRESHING)
            theirs, _ = await service.requests.create("neighbour", "forget-theirs", REFRESHING)
            for ident in (mine, theirs):
                await owner_edits.remember(
                    service.repo.store, ident, "run-1", invented("old", 2), invented("new", 1)
                )
            url = f"/shijhon/playlists/describe/{mine}"
            async with web(service) as client:
                shown = await client.get(url)
                assert (
                    "Learned from your edits: 2 removed songs stay out, 1 added song guides the AI."
                    in plain(shown.text)
                )
                # Not by a link, without the page's token, nor with another one.
                await client.get(url, params={"action": "forget_edits", "csrf": "csrf"})
                for form in (
                    {"action": "forget_edits"},
                    {"action": "forget_edits", "csrf": "other"},
                ):
                    assert (await client.post(url, data=form)).status_code == 403
                assert (await remembered_count(service, mine))["n"] == 3
                # Another account's session cannot forget this request's edits.
                form = {"action": "forget_edits", "csrf": "csrf"}
                refused = await client.post(url, data=form, headers={"x-account": "neighbour"})
                assert refused.status_code == 200 and "unavailable" in refused.text
                assert (await remembered_count(service, mine))["n"] == 3
                done = await client.post(url, data=form)
                assert done.status_code == 303 and done.headers["location"] == url
                assert (await remembered_count(service, mine))["n"] == 0
                assert (await remembered_count(service, theirs))["n"] == 3
                again = await client.get(url)
                for absent in ("Learned from your edits", "Forget learned edits"):
                    assert absent not in again.text
                # Forgetting nothing is harmless.
                assert (await client.post(url, data=form)).status_code == 303

    anyio.run(run)


def test_the_mix_page_shows_and_forgets_a_discovery_mixs_edits(tmp_path: Path) -> None:
    async def run() -> None:
        async with service_at(tmp_path / "db") as service:
            neighbour = connected(
                "neighbour", backend="api", endpoint="https://model.invalid/v1", model="m"
            )
            await service.repo.connect(neighbour)
            store = service.repo.store
            mixes = Definition(name="Evening discovery", count=3, brief="Bright songs", ai=True)
            for mix, owner in (("mine", "owner"), ("theirs", "neighbour"), ("plain", "owner")):
                await store.execute(
                    "INSERT INTO playlist_mixes(id,owner,definition) VALUES (?,?,?)",
                    [mix, owner, mixes.model_dump_json()],
                )
                learned = {"learned": {"removed": [WENT], "added": []}, "left_out": 2}
                await store.execute(
                    "INSERT INTO playlist_runs(id,mix,created,state,data)"
                    " VALUES (?,?,?,'published',?)",
                    [
                        f"run-{mix}",
                        mix,
                        time.time(),
                        json.dumps({} if mix == "plain" else {"edits": learned}),
                    ],
                )
                if mix != "plain":
                    await owner_edits.remember(
                        store,
                        mix,
                        f"run-{mix}",
                        [Track(**{**WENT, "title": f"Went {HOSTILE}"})],
                        invented("new", 1),
                    )
            url, action = "/shijhon/playlists/mine", "/shijhon/playlists/mine/action"
            async with web(service, MixPages) as client:
                html = (await client.get(url)).text
                text = plain(html)
                assert HOSTILE not in html and "Went &lt;script&gt;" in html
                assert (
                    "Learned from your edits: 1 removed song stays out, 1 added song guides the AI."
                    in text
                )
                assert (
                    "2 suggested songs were left out of this run because you removed them." in text
                )
                assert forms(html)[action]["csrf"] == "csrf"
                for absent in ("Learned from your edits", "Forget learned edits"):
                    assert absent not in (await client.get("/shijhon/playlists/plain")).text
                # Only with the token, only for the owner's mix.
                assert (
                    await client.post(action, data={"action": "forget_edits"})
                ).status_code == 403
                form = {"action": "forget_edits", "csrf": "csrf"}
                refused = await client.post(action, data=form, headers={"x-account": "neighbour"})
                assert "unavailable" in refused.text
                assert (await remembered_count(service, "mine"))["n"] == 2
                done = await client.post(action, data=form)
                assert done.status_code == 303 and done.headers["location"] == url
                assert (await remembered_count(service, "mine"))["n"] == 0
                assert (await remembered_count(service, "theirs"))["n"] == 2
                again = plain((await client.get(url)).text)
                assert "Learned from your edits: nothing is remembered" in again
                assert "Forget learned edits" not in again

    anyio.run(run)


def test_after_forgetting_the_next_refresh_no_longer_leaves_the_removed_song_out(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        async with refreshing(tmp_path / "db") as (service, native):
            service.curator.api = Model(
                ["Paper Kites", "Northern Wires", "Copper Rain", "Open Windows"]
            )
            await publish(service, native, "walk", PUBLISHED)
            native.edit("pl-walk", songs=["s-amber", "s-cedar", "s-dune"])
            first = await refresh(service, "walk")
            assert "Paper Kites" not in titles(first)
            await publish(service, native, "walk", [s["id"] for s in first["selected"]])
            latest = (await service.repo.runs("walk"))[0]
            await service.repo.update(latest["id"], "published", {**latest["data"], "releases": []})
            url = "/shijhon/playlists/describe/walk"
            async with web(service) as client:
                shown = plain((await client.get(url)).text)
                assert (
                    "Learned from your edits: 1 removed song stays out, 1 added song guides the AI."
                    in shown
                )
                form = {"action": "forget_edits", "csrf": "csrf"}
                assert (await client.post(url, data=form)).status_code == 303
            again = await refresh(service, "walk")
            assert "Paper Kites" in titles(again)
            assert again["edits"]["remembered"] == {"removed": 0, "added": 0}

    anyio.run(run)
