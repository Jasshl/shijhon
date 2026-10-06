"""A real, disposable Navidrome at the pinned version.

The official release archive is downloaded once, verified against the SHA-256 values
published with the release, and cached. ``SHIJHON_NAVIDROME_BIN`` may point at another
binary; it must report the pinned version unless ``SHIJHON_NAVIDROME_VERSION`` names a
canary candidate (a release, or ``latest``), which is downloaded and checked the same way
against the checksums published with that release.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import io
import os
import platform
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from tests.harness.paths import cache_dir, file_lock
from tests.harness.subsonic import SubsonicClient

PINNED_VERSION = "0.64.2"
# From navidrome_checksums.txt of the v0.64.2 release.
PINNED_SHA256 = {
    "darwin_amd64": "bc872847dcd1c0d760bb9b8730f2a3c876e0ff0679e5440755c37697f2804a23",
    "darwin_arm64": "8b0a7798001453719ad50c12f7025a6044546c4a3568efe51feb296681f29956",
    "linux_amd64": "fdd87fd107818667c2c50bc24dc6ce856962c795b3822a91f95c066cfad2c53e",
    "linux_arm64": "6d1683428cb6d99cdabc3da815b94cc161de5904820130c5b9e2e42c34dcdb15",
}
RELEASES_PAGE = "https://github.com/navidrome/navidrome/releases"
RELEASES = f"{RELEASES_PAGE}/download"

# Navidrome is started through this: it ends with the process that started it, also when
# that process is killed outright (a pytest worker, the development instance's tool).
TETHER = Path(__file__).with_name("tether.py")

ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin-password-for-tests"


@functools.cache
def resolve_version(version: str) -> str:
    """A version as given (without a leading "v"), or for ``latest`` the newest Navidrome
    release (where GitHub's "latest release" address leads)."""
    if version != "latest":
        return version.removeprefix("v")
    response = httpx.get(f"{RELEASES_PAGE}/latest", follow_redirects=False, timeout=60)
    tag = response.headers.get("location", "").rstrip("/").rsplit("/", 1)[-1]
    if not tag.startswith("v"):
        raise RuntimeError(f"cannot tell Navidrome's latest release (HTTP {response.status_code})")
    return tag.removeprefix("v")


def wanted_version() -> str:
    """The pinned version, or the candidate ``SHIJHON_NAVIDROME_VERSION`` names (suite P)."""
    return resolve_version(os.environ.get("SHIJHON_NAVIDROME_VERSION") or PINNED_VERSION)


def _platform_key() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "arm64": "arm64", "aarch64": "arm64"}[machine]
    return f"{system}_{arch}"


def binary_version(path: Path) -> str:
    out = subprocess.run([str(path), "--version"], capture_output=True, text=True, check=True)
    return out.stdout.split()[0]


def _expected_sha256(version: str, archive: str) -> str:
    if version == PINNED_VERSION:
        return PINNED_SHA256[_platform_key()]
    # Canary candidates: trust the checksum file published with that release.
    sums = httpx.get(
        f"{RELEASES}/v{version}/navidrome_checksums.txt", follow_redirects=True, timeout=60
    )
    sums.raise_for_status()
    for line in sums.text.splitlines():
        digest, _, name = line.partition("  ")
        if name.strip() == archive:
            return digest
    raise RuntimeError(f"no checksum for {archive}")


def navidrome_binary(version: str | None = None) -> Path:
    version = resolve_version(version) if version else wanted_version()
    override = os.environ.get("SHIJHON_NAVIDROME_BIN")
    if override:
        path = Path(override)
        found = binary_version(path)
        if found != version:
            raise RuntimeError(f"{path} is Navidrome {found}, tests want {version}")
        return path

    target = cache_dir() / "navidrome" / version / _platform_key() / "navidrome"
    if target.exists():
        return target
    with file_lock(target.parent.parent / ".lock"):
        if target.exists():
            return target
        archive = f"navidrome_{version}_{_platform_key()}.tar.gz"
        response = httpx.get(f"{RELEASES}/v{version}/{archive}", follow_redirects=True, timeout=300)
        response.raise_for_status()
        digest = hashlib.sha256(response.content).hexdigest()
        if digest != _expected_sha256(version, archive):
            raise RuntimeError(f"checksum mismatch for {archive}")
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
            member = tar.extractfile("navidrome")
            if member is None:
                raise RuntimeError(f"{archive} has no navidrome binary")
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(".partial")
            partial.write_bytes(member.read())
        partial.chmod(0o755)
        partial.replace(target)
    return target


def start_tethered(
    command: list[str], *, grace: float | None = None, **popen: Any
) -> tuple[subprocess.Popen[bytes], int]:
    """Start ``command`` through the tether, in a session of its own. Returns the tether's
    process and the read end of a pipe that ends when the tether, the command and what it
    started are gone (as far as they kept the pipe: a program that closes inherited files
    for its children hides them; ``stop_group`` kills the group's rest all the same)."""
    alive, held = os.pipe()
    options = ["--alive-fd", str(held)] + (["--grace", str(grace)] if grace is not None else [])
    try:
        process = subprocess.Popen(
            [sys.executable, str(TETHER), *options, *command],
            stdin=subprocess.PIPE,  # held open, never written: its end stops the command
            pass_fds=[held],
            start_new_session=True,
            **popen,
        )
    except BaseException:
        os.close(alive)
        raise
    finally:
        os.close(held)
    return process, alive


def ended(alive: int, seconds: float = 0.0) -> bool:
    """Whether the pipe from ``start_tethered`` has ended (within ``seconds``): the tether,
    the command and what it started with that pipe are gone."""
    readable, _, _ = select.select([alive], [], [], max(0.0, seconds))
    return bool(readable) and os.read(alive, 1) == b""


def stop_group(process: subprocess.Popen[bytes], alive: int, grace: float = 20.0) -> None:
    """Stop a tethered command and return only once it is gone itself - its data folder may
    be used again at once: its whole process group gets SIGTERM (the tether waits for the
    command; a tether that died earlier is no excuse to leave the command), then - after
    the pipe ``alive`` has ended, or ``grace`` seconds - SIGKILL for whatever is left of
    it. The pipe's end is the proof that the command is gone; no process ID is trusted for
    that. The group is signaled only while the tether has not been waited for: until then
    its process ID, the group's ID, cannot be another process's. Raises (leaving the pipe
    open) when the command cannot be stopped."""

    def signal_group(number: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, number)

    if process.returncode is None:
        signal_group(signal.SIGTERM)
        ended(alive, grace)
        signal_group(signal.SIGKILL)  # what is left of the group; nobody, usually
        if not ended(alive, 10.0):
            raise RuntimeError("a tethered command could not be stopped")
        process.wait(timeout=10)
    elif not ended(alive):
        raise RuntimeError("a tethered command outlived its tether, which was waited for")
    os.close(alive)
    if process.stdin is not None:
        process.stdin.close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# Hermetic defaults: no external services, no background scans, deterministic scanning.
BASE_ENV = {
    "ND_LOGLEVEL": "warn",
    "ND_ENABLEINSIGHTSCOLLECTOR": "false",
    "ND_ENABLEEXTERNALSERVICES": "false",
    "ND_ENABLEARTWORKPRECACHE": "false",
    "ND_ENABLEGRAVATAR": "false",
    "ND_PLUGINS_ENABLED": "false",
    "ND_SCANNER_SCHEDULE": "0",
    "ND_SCANNER_SCANONSTARTUP": "false",
    "ND_SCANNER_WATCHERWAIT": "0",
    "ND_SCANNER_PURGEMISSING": "never",
    "ND_AUTHREQUESTLIMIT": "10000",
    "ND_ENABLESHARING": "false",
    "ND_JELLYFIN_ENABLED": "false",
    "ND_JUKEBOX_ENABLED": "false",
}


@dataclass
class ScanStatus:
    scanning: bool
    count: int
    folder_count: int
    last_scan: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


class NavidromeInstance:
    """One Navidrome process with its own data folder, library and port."""

    def __init__(
        self,
        root: Path,
        *,
        env: Mapping[str, str | None] | None = None,
        binary: Path | None = None,
        admin: tuple[str, str] = (ADMIN_USER, ADMIN_PASSWORD),
    ) -> None:
        # Absolute: Navidrome runs with its own working directory (the root).
        self.root = root.resolve()
        self.admin_user, self.admin_password = admin
        self.music = self.root / "music"
        self.data = self.root / "data"
        self.port = free_port()
        self.binary = binary or navidrome_binary()
        self.extra_env = dict(env or {})
        self.process: subprocess.Popen[bytes] | None = None
        self._alive = -1  # (see start_tethered)
        self._jwt: str | None = None
        self._ran = False  # it has answered on its port (a restart keeps the port)
        self._port_tries = 0

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def log_path(self) -> Path:
        return self.root / "navidrome.log"

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("ND_")}
        env.update(BASE_ENV)
        env.update(
            ND_MUSICFOLDER=str(self.music),
            ND_DATAFOLDER=str(self.data),
            ND_CACHEFOLDER=str(self.root / "cache"),
            ND_ADDRESS="127.0.0.1",
            ND_PORT=str(self.port),
        )
        for key, value in self.extra_env.items():
            if value is None:
                env.pop(key, None)  # Navidrome's own default (e.g. its login limit)
            else:
                env[key] = value
        return env

    def start(self, timeout: float = 30.0) -> None:
        self.music.mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)
        log = self.log_path.open("ab")
        logged = log.tell()
        # cwd=root so a navidrome.toml elsewhere can never be picked up. Its own session (a
        # terminal's Ctrl-C is this process's to handle), and tethered: the tether sees the
        # end of its input - held open here, never written - when this process is gone,
        # however it ended, and stops Navidrome.
        self.process, self._alive = start_tethered(
            [str(self.binary)], cwd=self.root, env=self._env(), stdout=log, stderr=log
        )
        log.close()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ended(self._alive):  # (not waited for here: see stop_group)
                said = self.log_path.read_bytes()[logged:].decode(errors="replace")
                if "address already in use" in said and not self._ran and self._port_tries < 3:
                    # Its port, free when picked, was taken before it bound it (another
                    # test process): a first start takes another (a restart keeps its port).
                    stop_group(self.process, self._alive)
                    self._port_tries += 1
                    self.port = free_port()
                    self.start(timeout)
                    return
                raise RuntimeError(f"navidrome exited:\n{self.log_path.read_text()[-4000:]}")
            try:
                answered = httpx.get(f"{self.base_url}/ping", timeout=1).status_code == 200
                if answered and ended(self._alive):
                    continue  # another process answered on its port: the branch above retries
                if answered:
                    self._ran = True
                    self._wait_initial_scan(deadline)
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        raise TimeoutError("navidrome did not start")

    def _wait_initial_scan(self, deadline: float) -> None:
        """A new database asks for a full scan "after migration", which Navidrome starts
        about 2 s after startup whatever the scan settings: wait until it has run. Started
        later, in the middle of a test, its first phase rewrites the album rows of the
        folders it has reached so far - a filled album then counted only its owned songs
        until the scan's end (the suite D "stale songCount" flake)."""
        while time.monotonic() < deadline:
            try:
                db = sqlite3.connect(f"file:{self.data / 'navidrome.db'}?mode=ro", uri=True)
                try:
                    flag = db.execute(
                        "SELECT value FROM property WHERE id = 'FullScanAfterMigration'"
                    ).fetchone()
                    row = db.execute(
                        "SELECT last_scan_at, full_scan_in_progress FROM library WHERE id = 1"
                    ).fetchone()
                    last, running = row if row is not None else ("0000", 1)
                finally:
                    db.close()
            except sqlite3.OperationalError as exc:
                if "no such" in str(exc) and (self.data / "navidrome.db").exists():
                    # Another schema (a canary version): the scan's own timing, then on.
                    time.sleep(3.0)
                    return
                flag, last, running = ("1",), "", 1  # not created yet
            except sqlite3.Error:
                flag, last, running = ("1",), "", 1  # not created yet
            scanned = not str(last).startswith("0000") and not running
            if (flag is None or flag[0] != "1") and scanned:
                return
            time.sleep(0.1)
        raise TimeoutError("navidrome's initial scan did not finish")

    def stop(self) -> None:
        if self.process is None:
            return
        stop_group(self.process, self._alive)  # (raises: still running, as far as known)
        self.process = None
        self._jwt = None

    def restart(self, env: Mapping[str, str] | None = None) -> None:
        self.stop()
        if env is not None:
            self.extra_env.update(env)
        self.start()

    # --- users -----------------------------------------------------------------------

    def create_admin(self) -> None:
        response = httpx.post(
            f"{self.base_url}/auth/createAdmin",
            json={"username": self.admin_user, "password": self.admin_password},
            timeout=10,
        )
        response.raise_for_status()

    def login(self, user: str | None = None, password: str | None = None) -> dict[str, Any]:
        response = httpx.post(
            f"{self.base_url}/auth/login",
            json={
                "username": user or self.admin_user,
                "password": password or self.admin_password,
            },
            timeout=10,
        )
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        return body

    def native(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Navidrome's own (non-Subsonic) API, as the admin."""
        if self._jwt is None:
            self._jwt = self.login()["token"]
        headers = {"x-nd-authorization": f"Bearer {self._jwt}"}
        response = httpx.request(
            method, f"{self.base_url}/api/{path}", headers=headers, timeout=30, **kwargs
        )
        response.raise_for_status()
        return response

    def create_user(self, user: str, password: str, *, admin: bool = False) -> str:
        created = self.native(
            "POST",
            "user",
            json={"userName": user, "name": user, "password": password, "isAdmin": admin},
        ).json()
        user_id: str = created["id"]
        libraries = [lib["id"] for lib in self.native("GET", "library").json()]
        self.native("PUT", f"user/{user_id}/library", json={"libraryIds": libraries})
        return user_id

    def client(
        self, user: str | None = None, password: str | None = None, **kwargs: Any
    ) -> SubsonicClient:
        """A Subsonic client for this instance (``headers=`` sets default headers)."""
        return SubsonicClient(
            self.base_url, user or self.admin_user, password or self.admin_password, **kwargs
        )

    # --- scanning --------------------------------------------------------------------

    def scan_status(self) -> ScanStatus:
        raw = self.client().ok("getScanStatus")["scanStatus"]
        return ScanStatus(
            scanning=raw["scanning"],
            count=raw.get("count", 0),
            folder_count=raw.get("folderCount", 0),
            last_scan=raw.get("lastScan"),
            raw=raw,
        )

    def scan(
        self, *, full: bool = False, targets: Iterable[str] = (), timeout: float = 120.0
    ) -> float:
        """Run a scan and wait until the scanner is idle again. ``targets`` are folder paths
        relative to the library root. Returns the elapsed seconds."""
        started = time.monotonic()
        deadline = started + timeout  # for every wait and every attempt
        params: list[tuple[str, str]] = [("fullScan", "true" if full else "false")]
        params += [("target", f"1:{t}") for t in targets]
        # Navidrome silently ignores a request while another scan is still finishing, so
        # wait until this scan is recorded (lastScan changes), asking again if needed.
        for _ in range(5):
            while self.scan_status().scanning:
                if time.monotonic() > deadline:
                    raise TimeoutError("scan did not finish")
                time.sleep(0.05)
            before = self.scan_status().last_scan
            self.client().ok("startScan", params)
            settle = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                status = self.scan_status()
                if not status.scanning and status.last_scan != before:
                    return time.monotonic() - started
                if not status.scanning and time.monotonic() > settle:
                    break  # never started: ask again
                time.sleep(0.05)
            else:
                raise TimeoutError("scan did not finish")
        raise TimeoutError("scan did not finish")

    def remove(self) -> None:
        self.stop()
        shutil.rmtree(self.root, ignore_errors=True)
