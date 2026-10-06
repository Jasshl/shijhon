"""The harness itself: the Navidrome asked for (the pinned one, or a canary candidate),
synthetic library, every login style."""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from tests.harness.library import Album, Track, simple_album, write_album
from tests.harness.navidrome import NavidromeInstance, binary_version, wanted_version
from tests.harness.subsonic import SubsonicClient


def test_the_navidrome_asked_for_answers(navidrome: NavidromeInstance) -> None:
    assert binary_version(navidrome.binary) == wanted_version()
    ping = navidrome.client().ok("ping")
    assert ping["serverVersion"].startswith(wanted_version())


@pytest.mark.parametrize("fmt", ["flac", "mp3", "m4a"])
def test_synthetic_album_is_indexed(navidrome: NavidromeInstance, fmt: str) -> None:
    album = simple_album("Harness Artist", f"Harness {fmt}", 3, fmt=fmt)  # type: ignore[arg-type]
    write_album(navidrome.music, album)
    navidrome.scan(targets=[album.relative_folder])
    found = navidrome.client().ok("search3", {"query": album.title, "albumCount": 5})
    albums = [a for a in found["searchResult3"].get("album", []) if a["name"] == album.title]
    assert len(albums) == 1
    detail = navidrome.client().ok("getAlbum", {"id": albums[0]["id"]})["album"]
    assert [s["track"] for s in detail["song"]] == [1, 2, 3]
    assert {s["suffix"] for s in detail["song"]} == {fmt}


@pytest.mark.parametrize("auth", ["token", "password", "hex"])
def test_login_styles(navidrome: NavidromeInstance, auth: str) -> None:
    navidrome.client(auth=auth).ok("ping")  # type: ignore[arg-type]


def test_normal_user_and_wrong_password(navidrome: NavidromeInstance) -> None:
    navidrome.create_user("listener", "listener-password")
    SubsonicClient(navidrome.base_url, "listener", "listener-password").ok("getMusicFolders")
    wrong = SubsonicClient(navidrome.base_url, "listener", "nope")
    assert wrong.error_code("ping") == 40


def test_distinct_tones_per_track(navidrome: NavidromeInstance) -> None:
    album = Album("Tone Artist", "Tones", (Track("A", 1), Track("B", 2)))
    first, second = write_album(navidrome.music, album)
    assert first.read_bytes() != second.read_bytes()


def test_xml_and_form_post(navidrome: NavidromeInstance) -> None:
    xml = navidrome.client(fmt="xml").request("ping")
    assert xml.headers["content-type"].startswith("application/xml")
    assert b'status="ok"' in xml.content
    navidrome.client().ok("ping", http_method="POST")


def test_a_test_navidrome_ends_with_the_process_that_started_it(tmp_path: Path) -> None:
    """A pytest worker killed outright cannot stop its Navidromes; they used to stay behind
    for good. Started through the tether, a Navidrome ends when its starter is gone."""
    repo = Path(__file__).resolve().parents[2]
    script = tmp_path / "starter.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import sys, time
            from pathlib import Path
            sys.path.insert(0, {str(repo)!r})
            from tests.harness.navidrome import NavidromeInstance
            nd = NavidromeInstance(Path({str(tmp_path / "nd")!r}))
            nd.start()
            print(nd.port, nd.process.pid, flush=True)
            time.sleep(600)
            """
        )
    )
    starter = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE)
    group = 0
    try:
        assert starter.stdout is not None
        port, group = (int(n) for n in starter.stdout.readline().split())

        def listening() -> bool:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            except OSError:
                return False
            return True

        assert listening()
        starter.kill()  # nothing of its own runs any more
        starter.wait()
        deadline = time.monotonic() + 30
        while listening():
            assert time.monotonic() < deadline, "the Navidrome outlived its starter"
            time.sleep(0.1)
    finally:
        starter.kill()
        starter.wait()
        if group:  # (whatever the test found: nothing of it stays behind)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(group, signal.SIGKILL)
