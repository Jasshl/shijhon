"""Navidrome on a Unix socket (``[navidrome] socket``), as the NixOS service runs it: the
first accounts are made through it, and every way Shijhon reaches Navidrome takes it -
the credential check and Subsonic forwards, Navidrome's web interface, login page and own
API, the dashboard's sign-in and the service account's calls at startup. Navidrome has
no TCP port here at all."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest

from shijhon.app import create_app
from shijhon.config import load_settings
from shijhon.navidrome import accounts, connect
from tests.harness.collection import refresh_collection
from tests.harness.dashboard import Browser
from tests.harness.library import simple_album, write_album
from tests.harness.navidrome import BASE_ENV, navidrome_binary
from tests.harness.running import RunningServer
from tests.harness.subsonic import SubsonicClient

OWN, OWN_PASSWORD = "alex", "an invented password"
BASE = "http://navidrome.invalid"  # (resolves nowhere: only the socket is reached)


class SocketNavidrome:
    def __init__(self) -> None:
        # Short: a socket's path has a length limit (104 bytes on macOS).
        self.root = Path(tempfile.mkdtemp(prefix="nds", dir="/tmp"))
        self.socket = self.root / "nd.sock"
        self.music = self.root / "music"
        self.music.mkdir()
        # One owned song, long enough to be a body of some size.
        album = simple_album("Invented Artist", "Socket", 1, seconds=60)
        (self.song,) = write_album(self.music, album)
        env = {k: v for k, v in os.environ.items() if not k.startswith("ND_")}
        env.update(BASE_ENV)
        env.update(
            ND_MUSICFOLDER=str(self.music),
            ND_DATAFOLDER=str(self.root / "data"),
            ND_CACHEFOLDER=str(self.root / "cache"),
            ND_ADDRESS=f"unix:{self.socket}",
            ND_UNIXSOCKETPERM="0600",
            ND_EXTAUTH_TRUSTEDSOURCES="@",
            ND_ENABLESHARING="true",
        )
        self.log = (self.root / "navidrome.log").open("wb")
        self.process = subprocess.Popen(
            [str(navidrome_binary())], cwd=self.root, env=env, stdout=self.log, stderr=self.log
        )
        with httpx.Client(transport=httpx.HTTPTransport(uds=str(self.socket))) as http:
            for _ in range(300):
                try:
                    if http.get("http://navidrome.invalid/ping").status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        raise TimeoutError("navidrome did not start")

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=30)
        self.log.close()
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture(scope="module")
def nd() -> Iterator[SocketNavidrome]:
    instance = SocketNavidrome()
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture(scope="module")
def shijhon(
    nd: SocketNavidrome, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[RunningServer]:
    state = tmp_path_factory.mktemp("state")
    own = state / "own-password"
    own.write_text(OWN_PASSWORD + "\n")
    accounts.ensure(
        "http://navidrome.invalid",
        "shijhon",
        state / "navidrome-password",
        socket=nd.socket,
        account=OWN,
        account_password_file=own,
        wait=30,
        say=lambda _: None,
    )
    settings = load_settings(
        None,
        state_dir=state,
        navidrome={
            "url": "http://navidrome.invalid",
            "socket": nd.socket,
            "user": "shijhon",
            "password_file": state / "navidrome-password",
            "library_path": nd.music,
        },
    )
    server = RunningServer(create_app(settings))
    server.start()
    try:
        yield server
    finally:
        server.stop()


def test_the_transport_reaches_the_socket(nd: SocketNavidrome) -> None:
    with connect.client(nd.socket, base_url="http://navidrome.invalid") as http:
        assert http.get("/ping").status_code == 200

    async def ping() -> int:
        async with connect.async_client(nd.socket, base_url="http://navidrome.invalid") as http:
            return (await http.get("/ping")).status_code

    assert anyio.run(ping) == 200


def test_apps_sign_in_and_are_answered_through_the_socket(shijhon: RunningServer) -> None:
    client = SubsonicClient(shijhon.base_url, OWN, OWN_PASSWORD)
    client.ok("ping")
    assert client.ok("getUser", {"username": OWN})["user"]["adminRole"]
    wrong = SubsonicClient(shijhon.base_url, OWN, "not the password")
    assert wrong.error_code("ping") == 40


def test_navidromes_web_interface_login_and_api_through_the_socket(
    shijhon: RunningServer,
) -> None:
    with httpx.Client(base_url=shijhon.base_url, timeout=30) as http:
        assert http.get("/app/").status_code == 200
        login = http.post("/auth/login", json={"username": OWN, "password": OWN_PASSWORD})
        assert login.status_code == 200, login.text
        token = login.json()["token"]
        users: Any = http.get("/api/user", headers={"x-nd-authorization": f"Bearer {token}"})
        assert users.status_code == 200
        assert {u["userName"] for u in users.json()} == {"shijhon", OWN}


def test_the_dashboard_signs_in_through_the_socket(shijhon: RunningServer) -> None:
    browser = Browser(shijhon.base_url)
    try:
        signed = browser.sign_in(OWN, OWN_PASSWORD)
        assert signed.status_code == 303, signed.text
        # (Navidrome's state there is read with the service account.)
        diagnostics = browser.get("diagnostics")
        assert diagnostics.status_code == 200
        assert "(socket)" in diagnostics.text
        assert "Navidrome is not answering" not in diagnostics.text
    finally:
        browser.close()


def prepared(nd: SocketNavidrome, shijhon: RunningServer) -> str:
    """The owned song's ID, once Navidrome has scanned it and the collection - read with a
    caller's proof from the dashboard's sign-in - is ready for apps."""
    with connect.client(nd.socket, base_url=BASE, timeout=30) as http:
        login = http.post("/auth/login", json={"username": OWN, "password": OWN_PASSWORD})
        owner, token = str(login.json()["id"]), login.json()["subsonicToken"]
        salt = login.json()["subsonicSalt"]
        auth = {"u": OWN, "t": token, "s": salt, "v": "1.16.1", "c": "t", "f": "json"}
        http.get("/rest/startScan", params=auth)
        for _ in range(300):
            answer = http.get("/rest/search3", params={**auth, "query": "Socket"}).json()
            found = answer["subsonic-response"]["searchResult3"].get("song", [])
            status = http.get("/rest/getScanStatus", params=auth).json()
            if found and not status["subsonic-response"]["scanStatus"]["scanning"]:
                break
            time.sleep(0.2)
        else:
            raise TimeoutError("the song was not scanned")
    browser = Browser(shijhon.base_url)
    try:
        assert browser.sign_in(OWN, OWN_PASSWORD).status_code == 303
    finally:
        browser.close()
    refresh_collection(shijhon, owner)
    return str(found[0]["id"])


def test_audio_and_share_links_stream_through_the_socket(
    nd: SocketNavidrome, shijhon: RunningServer
) -> None:
    song = prepared(nd, shijhon)
    client = SubsonicClient(shijhon.base_url, OWN, OWN_PASSWORD)
    body = client.request("download", {"id": song}).content
    assert body == nd.song.read_bytes() and len(body) > 100_000
    share = client.ok("createShare", {"id": song})["shares"]["share"][0]
    page = httpx.get(f"{shijhon.base_url}/share/{share['id']}", timeout=30)
    found = re.search(r"__SHARE_INFO__\s*=\s*(\".*?\")\s*</script>", page.text, re.S)
    assert found is not None, page.text[:300]
    token = json.loads(json.loads(found.group(1)))["tracks"][0]["id"]
    audio = httpx.get(f"{shijhon.base_url}/share/s/{token}", timeout=30)
    assert audio.status_code == 200 and len(audio.content) > 0


def test_playlist_owner_and_collection_reads_through_the_socket(
    nd: SocketNavidrome, shijhon: RunningServer
) -> None:
    services = shijhon.config.app.services

    async def owner_login() -> dict[str, Any]:
        return await services.playlists.client.login(OWN, OWN_PASSWORD, "10.9.0.1")

    assert shijhon.call(owner_login)
    prepared(nd, shijhon)  # (the collection's reads, with the caller's own proof)
