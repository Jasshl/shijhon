"""Discovery creation retries reuse durable work and refused rules remain editable."""

from __future__ import annotations

from types import SimpleNamespace

import anyio
import pytest
from starlette.requests import Request

from shijhon.dashboard.auth import Session
from shijhon.dashboard.playlists import Pages, version
from shijhon.playlists.model import Definition, PlaylistError
from shijhon.playlists.repository import Repository
from shijhon.store import Store
from tests.harness.dashboard import forms
from tests.unit.test_collection_workspace import Dashboard
from tests.unit.test_playlists import connection

pytestmark = pytest.mark.anyio


@pytest.fixture
async def discovery(tmp_path):
    store = await Store.open(tmp_path / "state.db")
    repo = Repository(store)
    owner = connection()
    await repo.connect(owner)

    async def connected(owner_id):
        return await repo.connection(owner_id)

    async def choices(owner_id):
        return SimpleNamespace(model="", default_model="", allowed_models=[])

    service = SimpleNamespace(
        repo=repo,
        connection=connected,
        ai_choices=choices,
        changes=anyio.Lock(),
        notify=lambda: None,
        settings=SimpleNamespace(allow_cli=False, history_days=90),
    )
    dashboard = Dashboard(None)
    dashboard.services.playlists = service
    dashboard.env.filters.update(when=lambda value: "Today", day=lambda value: "Today")
    original_render = dashboard._render

    async def render(name, session, **context):
        return await original_render(name, session, is_admin=session.is_admin, **context)

    dashboard._render = render
    session = Session("test", "Listener", "csrf", 9999999999, owner.user_id)
    yield Pages(dashboard), session, repo
    await store.close()


def post(form, mix=""):
    request = Request({"type": "http", "method": "POST", "path_params": {"mix": mix}})
    request.state.form = form
    return request


async def test_creation_double_submit_and_later_retry_reuse_one_mix_and_run(discovery):
    pages, session, repo = discovery
    html = (await pages.render(session)).body.decode()
    form = forms(html)["/shijhon/playlists"]
    assert len(form["request_key"]) == 32
    form.update(name="Evening paths", ai="true", brief="Gentle invented songs")
    responses = []

    async def submit():
        responses.append(await pages.index(post(form), session))

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(submit)
        tasks.start_soon(submit)
    assert all(response.status_code == 303 for response in responses)
    assert responses[0].headers["location"] == responses[1].headers["location"]
    ident = responses[0].headers["location"].rsplit("/", 1)[1]
    runs = await repo.runs(ident)
    assert len(runs) == 1
    assert runs[0]["data"] == {"enable_after": False, "publish_after_generation": True}
    # Retrying after completion and rule edits still refers to the original creation.
    await repo.update(runs[0]["id"], "published", {})
    await repo.store.execute(
        "UPDATE playlist_mixes SET definition=? WHERE id=?",
        [Definition(name="Changed later").model_dump_json(), ident],
    )
    retry = await pages.index(post(form), session)
    assert retry.headers["location"] == responses[0].headers["location"]
    assert len(await repo.runs(ident)) == 1
    changed = await pages.index(post({**form, "name": "Different input"}), session)
    assert "already used with different input" in changed.body.decode()
    assert len(await repo.mixes(session.user_id)) == 1


@pytest.mark.parametrize("editing", [False, True])
async def test_refused_mix_rules_keep_brief_values_and_unchecked_choices(discovery, editing):
    pages, session, repo = discovery
    ident = ""
    original = Definition(
        name="Saved rules", ai=True, brief="Old brief", review_before_publishing=True
    )
    if editing:
        ident = await repo.create(session.user_id, original)
    form = {
        "request_key": "keep-this-request",
        "publication_mode": "direct",
        "name": "Unfinished & personal",
        "count": "0",
        "unfamiliar_percent": "25",
        "max_per_artist": "4",
        "exclude_days": "8",
        "repeat_days": "11",
        "cadence": "daily",
        "brief": "A quiet <evening> with strings",
        "enable": "true",
        "version": version(original.model_dump_json()),
    }
    response = await (
        pages.detail(post(form, ident), session) if editing else pages.index(post(form), session)
    )
    html = response.body.decode()
    submitted = forms(html)[f"/shijhon/playlists{('/' + ident) if editing else ''}"]
    for field in (
        "name",
        "count",
        "unfamiliar_percent",
        "max_per_artist",
        "exclude_days",
        "repeat_days",
        "cadence",
        "enable",
    ):
        assert submitted[field] == form[field]
    assert "A quiet &lt;evening&gt; with strings</textarea>" in html
    assert "ai" not in submitted and "review_before_publishing" not in submitted
    if not editing:
        assert submitted["request_key"] == form["request_key"]
    else:
        assert (await repo.mix(ident, session.user_id))["definition"] == original.model_dump_json()


async def test_discovery_keys_are_owner_scoped_and_failed_creation_rolls_back(discovery):
    _pages, session, repo = discovery
    for key in ("", "short", "x" * 129):
        with pytest.raises(PlaylistError, match="Reload"):
            await repo.create_discovery(session.user_id, key, Definition())
    spec = Definition(review_before_publishing=True)
    first = await repo.create_discovery(session.user_id, "same-key", spec, enable_after=True)
    other = connection(user_id="another-owner")
    await repo.connect(other)
    second = await repo.create_discovery(other.user_id, "same-key", spec, enable_after=True)
    assert first != second
    assert (await repo.runs(first))[0]["data"] == {
        "enable_after": True,
        "publish_after_generation": False,
    }
    # The durable key and run share the mix's transaction; a failed insert leaves neither.
    await repo.store.execute(
        "CREATE TRIGGER refuse_discovery_run BEFORE INSERT ON playlist_runs"
        " BEGIN SELECT RAISE(ABORT, 'injected run failure'); END"
    )
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError, match="injected run failure"):
        await repo.create_discovery(session.user_id, "failed-key", spec)
    assert len(await repo.mixes(session.user_id)) == 1
    assert not await repo.store.fetchone(
        "SELECT id FROM playlist_discovery_requests WHERE request_key='failed-key'"
    )
