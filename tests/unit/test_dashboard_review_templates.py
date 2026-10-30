"""Settings notices and import management use the dashboard's page vocabulary."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from jinja2 import Environment, PackageLoader, StrictUndefined

from shijhon.dashboard import sections
from shijhon.dashboard.app import DOWNLOAD_LIMITS


@pytest.fixture
def templates():
    env = Environment(
        loader=PackageLoader("shijhon.dashboard", "templates"),
        autoescape=True,
        undefined=StrictUndefined,
    )
    env.filters.update(when=lambda value: "Today")
    context = dict(
        prefix="/shijhon",
        accent="green",
        user="Listener",
        csrf="synthetic",
        current="sound",
        title="Sound analysis",
        nav=[],
        pending=[],
        can_restart=False,
        version="test",
        copyright="2026",
        license="AGPL-3.0",
        source_url="https://example.invalid",
    )
    return env, context


def section(env, context, **changes):
    return env.get_template("section.html").render(
        **{
            **context,
            "page": "sound",
            "summary": "Sound facts for playlists",
            "notices": [],
            "words": "",
            "problem": "",
            "groups": [],
            "fold": SimpleNamespace(groups=[], open=False),
            "shown_kind": None,
            "first_step": False,
            "form_status": "",
            **changes,
        }
    )


@pytest.mark.parametrize(
    ("running", "configured", "expected"),
    [
        (True, False, "Sound analysis is still running; it turns off after Shijhon restarts"),
        (False, False, "Off: no AudioMuse-AI address is set"),
        (False, True, "Not connected yet: sound analysis starts when Shijhon restarts"),
        (True, True, "Answering"),
    ],
)
def test_sound_status_distinguishes_saved_settings_from_running_service(
    templates, running, configured, expected
):
    env, context = templates
    html = section(
        env,
        context,
        extra=dict(
            running=running,
            configured=configured,
            status=dict(reachable=True, songs=0, synced=0, error="") if running else None,
        ),
    )
    assert expected in html
    if running:
        assert "Off: no AudioMuse-AI address is set" not in html


def test_restart_and_catalog_notices_preserve_product_names(templates):
    env, context = templates
    context["pending"] = [dict(label="AudioMuse-AI address", page="Sound analysis")]
    html = section(env, context, extra=dict(running=False, configured=False, status=None))
    assert "AudioMuse-AI address (Sound analysis)" in html and "audiomuse-ai" not in html
    item = dict(label="DASH token", address="DASH address", id="token")
    html = section(
        env,
        context,
        page="catalog",
        extra=dict(
            notice="",
            problem=dict(id="url", label="AudioMuse-AI address", notice="is needed"),
            running=False,
            withheld=[item],
            displaced=[item],
            kind="demo",
            wait=False,
        ),
    )
    assert "Not sent: DASH token" in html and "DASH address" in html
    assert "Not connected: AudioMuse-AI address is needed" in html
    assert "dash token" not in html and "audiomuse-ai" not in html


def test_download_timeout_lives_in_timing_and_saved_audio_group_is_empty_on_playback():
    groups = sections.grouped("delivery", sections.fields_of("delivery"))
    timeout_group = next(
        group
        for group, fields in groups
        if any(f.key == "download_timeout_seconds" for f in fields)
    )
    assert timeout_group.title == "Timing"
    saved_audio = next(fields for group, fields in groups if group.key == "downloads")
    assert {field.key for field in saved_audio} == set(DOWNLOAD_LIMITS)


def test_import_management_labels_describe_review_and_removal(templates):
    env, context = templates
    html = env.get_template("music_import_manage.html").render(
        **context,
        error="",
        asset_revision="test",
        batches=[dict(id="batch", created=0, files=2, state="review", removal_state="reviewing")],
        job=None,
        offset=0,
    )
    assert "Review your import" in html
    assert "Checking files and shared use…" in html
    assert "· review<" not in html and ">reviewing<" not in html


def test_quality_explains_lossless_and_catalog_file_fallback(templates):
    env, context = templates
    html = env.get_template("quality.html").render(
        **context,
        saved_now="",
        available=True,
        own=False,
        default_words="192 kb/s",
        choices=[("any", "Any")],
        chosen="any",
        upgrades=True,
    )
    assert "Lossless accepts only lossless audio" in html
    assert "the file plays only when they have nothing" in html
    assert "only accepts nothing else" not in html


def test_playlist_import_choices_and_columns_name_navidrome(templates):
    env, context = templates
    form = env.get_template("playlist_imports.html").render(
        **context,
        shown=None,
        error="",
        connected=True,
        request_key="synthetic-key",
        form={},
        formats=["auto"],
        columns=["native_id"],
        max_entries=100,
        max_bytes=1048576,
        listing=[],
    )
    assert "Navidrome song ID column" in form
    review = env.get_template("playlist_imports.html").render(
        **context,
        error="",
        shown=dict(
            id="synthetic-import",
            name="Evening paths",
            run=dict(id="run", state="preview", error="", data={}),
            only_missing=False,
            total=1,
            matched=0,
            missing=1,
            removed=0,
            unfinished=False,
            release_total=0,
            pages=1,
            page=0,
            review_key="synthetic-review",
            recheck_available=False,
            rows=[
                dict(
                    number=1,
                    input=dict(
                        artist="Glass Orchard",
                        title="Evening",
                        album="",
                        version="",
                        native_id="song",
                    ),
                    state="ambiguous",
                    track=None,
                    choices=[],
                    overflow=True,
                )
            ],
        ),
    )
    assert "Navidrome song ID: <code>song</code>" in review
    assert "Navidrome song ID for entry 1" in review
    assert "Exact Navidrome song ID" in review
    assert "native recording" not in review
