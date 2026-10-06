"""``setup.sh``, the quick start as a script: its checks, its answers without a terminal and
at one, what it writes, how its passwords travel, where the data goes, and that a second run
overwrites nothing.

Docker is a stand-in on the ``PATH`` here: it records every call (arguments and standard
input), keeps the project's volumes and plays Navidrome's accounts. The real thing is
rehearsed by hand (``docs/development.md``, "The setup script").
"""

from __future__ import annotations

import json
import os
import pty
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
OWN_PASSWORD = 'pä55 "quoted" \\ $HOME `x` it\'s'

# The stand-in for ``docker``: what the script asks of Docker and of Navidrome's interface.
DOCKER = r"""
import json, os, re, sys
from pathlib import Path

state_file = Path(os.environ["STUB_STATE"])
state = json.loads(state_file.read_text())
args = sys.argv[1:]
reads_input = args[:4] == ["compose", "exec", "-T", "navidrome"] and "-K" in args
stdin = sys.stdin.read() if reads_input else ""
state["calls"].append({"args": args, "stdin": stdin})


def finish(code=0, out=""):
    state_file.write_text(json.dumps(state))
    sys.stdout.write(out)
    sys.exit(code)


def unquoted(text):
    return re.sub(r"\\(.)", r"\1", text)


def env(name):
    found = ""
    for line in Path(".env").read_text().splitlines():
        match = re.fullmatch(r"\s*(?:export\s+)?" + name + r"\s*=\s*(.*?)\s*(?:#.*)?", line)
        if match:
            value = match.group(1)
            found = value[1:-1].replace("\\'", "'") if value.startswith("'") else value
    return found


def folder(value):
    if value.startswith("/"):
        return Path(value)
    if value.startswith("."):
        return Path.cwd() / value
    return None


# Compose's volumes for the data, each unless .env names a folder in its place: the first
# start (of the service that makes them writable) creates them.
DATA = {"NAVIDROME_DATA": "navidrome-data", "SHIJHON_DATA": "shijhon-data",
        "USAGE_EXPORT_DATA": "usage-export"}

if args == ["compose", "version"]:
    finish()
if args == ["info", "--format", "{{.OperatingSystem}}"]:
    finish(0 if state["daemon"] else 1, state["system"] + "\n")
if args[:2] == ["ps", "-a"]:
    project = args[3].removeprefix("label=com.docker.compose.project=")
    finish(0, "".join(d + "\n" for d in state["projects"].get(project, [])))
if args == ["volume", "ls", "-q"]:
    finish(0, "".join(v + "\n" for v in state["volumes"]))
if args == ["compose", "up", "-d", "navidrome"]:
    project = env("COMPOSE_PROJECT_NAME") or "shijhon"
    for name, volume in DATA.items():
        if folder(env(name)) is None and f"{project}_{volume}" not in state["volumes"]:
            state["volumes"].append(f"{project}_{volume}")
    if folder(env("NAVIDROME_DATA")) is not None:
        (folder(env("NAVIDROME_DATA")) / "navidrome.db").touch()
    if state["navidrome_fails"]:
        sys.stderr.write("the start failed\n")
        finish(1)
    state["navidrome"] = True
    finish()
if args == ["compose", "build"]:
    if state["build_fails"]:
        sys.stderr.write("the build failed\n")
        finish(1)
    state["built"] = True
    finish()
if args == ["compose", "up", "-d"]:
    state["shijhon"] = state["built"]
    finish(0 if state["built"] else 1)
if args[:4] == ["compose", "exec", "-T", "shijhon"]:
    finish(0 if state["shijhon"] else 1)
if args[:4] == ["compose", "exec", "-T", "navidrome"] and not reads_input:
    finish(0 if state["navidrome"] else 1)  # the readiness check
if reads_input:
    config = {}
    for line in stdin.splitlines():
        key, value = re.fullmatch(r'(\w+) = "(.*)"', line).groups()
        config.setdefault(key, []).append(unquoted(value))
    path = config["url"][0].removeprefix("http://127.0.0.1:4533")
    body = json.loads(config["data"][0]) if "data" in config else None
    token = next((h.split("Bearer ", 1)[1] for h in config.get("header", []) if "Bearer" in h), "")
    users = state["users"]

    def answer(status, value):
        finish(0, json.dumps(value, separators=(",", ":")) + "\n" + str(status))

    if path == "/auth/createAdmin":
        if state["raced"]:  # another run made this very account a moment ago
            users[body["username"]] = body["password"]
        if users:
            answer(403, {"error": "Cannot create another first admin"})
        users[body["username"]] = body["password"]
        answer(200, {"id": "1", "isAdmin": True, "token": "token-of-" + body["username"]})
    if path == "/auth/login":
        if users.get(body["username"]) == body["password"]:
            answer(200, {"id": "1", "token": "token-of-" + body["username"]})
        answer(401, {"error": "Invalid username or password"})
    if path == "/api/user":
        if token != "token-of-shijhon":
            answer(401, {"error": "Not authenticated"})
        if body is None:
            answer(200, [{"id": str(n), "userName": name} for n, name in enumerate(users)])
        if body["userName"] in users:
            answer(400, {"errors": {"userName": "ra.validation.unique"}})
        users[body["userName"]] = body["password"]
        answer(200, {"id": "2"})
sys.stderr.write(f"the stand-in does not know: {args}\n")
finish(64)
"""


class Setup:
    """A checkout with the script, a stand-in Docker and a music folder."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "check out"  # a space: every path is quoted
        (self.root / "packaging").mkdir(parents=True)
        shutil.copy(REPO / "setup.sh", self.root / "setup.sh")
        for name in ("compose.yaml", "shijhon.example.toml"):
            shutil.copy(REPO / "packaging" / name, self.root / "packaging" / name)
        self.packaging = self.root / "packaging"
        self.music = tmp_path / "my music"
        self.music.mkdir()
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(f"#!{sys.executable}\n{DOCKER}")
        docker.chmod(0o755)
        self.state_file = tmp_path / "docker-state.json"
        self.set_state(
            calls=[],
            daemon=True,
            system="Ubuntu 24.04 LTS",
            projects={},
            volumes=[],
            navidrome_fails=False,
            raced=False,
            navidrome=False,
            built=False,
            shijhon=False,
            build_fails=False,
            users={},
        )
        self.password_file = tmp_path / "own-password"
        self.password_file.write_text(OWN_PASSWORD + "\n")
        self.port = free_port()

    def set_state(self, **changes: object) -> None:
        state = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}
        self.state_file.write_text(json.dumps({**state, **changes}))

    @property
    def state(self) -> dict:
        return json.loads(self.state_file.read_text())

    def env(self, path: str | None = None, more: dict[str, str] | None = None) -> dict[str, str]:
        return {
            "PATH": path or f"{self.bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.home),
            "STUB_STATE": str(self.state_file),
            **(more or {}),
        }

    def answers(self) -> list[str]:
        return [
            *("--music", str(self.music), "--user", "yourname"),
            *("--password-file", str(self.password_file), "--port", str(self.port)),
        ]

    def run(
        self,
        *args: str,
        shell: str = "sh",
        stdin: str | None = None,
        path: str | None = None,
        more: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [shutil.which(shell) or shell, str(self.root / "setup.sh"), *args],
            input=stdin if stdin is not None else "",
            capture_output=True,
            text=True,
            env=self.env(path, more),
            cwd=self.root,
            timeout=60,
        )

    def written(self) -> dict[str, tuple[bytes, int]]:
        files = [
            self.packaging / ".env",
            self.packaging / "config" / "shijhon.toml",
            self.packaging / "config" / "navidrome-password",
        ]
        return {str(f): (f.read_bytes(), f.stat().st_mtime_ns) for f in files if f.exists()}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path)


def shells() -> list[str]:
    return [s for s in ("sh", "dash", "bash") if shutil.which(s)]


@pytest.mark.parametrize("shell", shells())
def test_a_run_without_questions_sets_everything_up(setup: Setup, shell: str) -> None:
    done = setup.run(*setup.answers(), shell=shell)
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""

    env = (setup.packaging / ".env").read_text().splitlines()
    assert env == [
        f"MUSIC_DIR='{setup.music.resolve()}'",
        f"PUID={os.getuid()}",
        f"PGID={os.getgid()}",
        f"SHIJHON_PORT={setup.port}",
    ]
    # The data is in the project's volumes, made by Navidrome's first start; on this
    # machine only the placeholder folder.
    assert setup.state["volumes"] == [
        "shijhon_navidrome-data",
        "shijhon_shijhon-data",
        "shijhon_usage-export",
    ]
    assert (setup.music / "_shijhon").is_dir()
    assert sorted(p.name for p in setup.root.parent.iterdir()) == sorted(
        ["check out", "my music", "home", "bin", "docker-state.json", "own-password"]
    )
    config = setup.packaging / "config" / "shijhon.toml"
    assert config.read_bytes() == (REPO / "packaging" / "shijhon.example.toml").read_bytes()
    password_file = setup.packaging / "config" / "navidrome-password"
    for private in (config, password_file):
        assert stat.S_IMODE(private.stat().st_mode) == 0o600

    # Navidrome has the two accounts: Shijhon's with the password in the file, yours with
    # the one given - whatever characters it has.
    service_password = password_file.read_text().rstrip("\n")
    assert len(service_password) == 48
    assert setup.state["users"] == {"shijhon": service_password, "yourname": OWN_PASSWORD}

    # No password is ever an argument of a command, or printed.
    for call in setup.state["calls"]:
        for secret in (OWN_PASSWORD, service_password):
            assert all(secret not in arg for arg in call["args"])
    assert OWN_PASSWORD not in done.stdout and service_password not in done.stdout

    assert "Shijhon is running" in done.stdout
    assert f"http://127.0.0.1:{setup.port}" in done.stdout
    assert f"http://127.0.0.1:{setup.port}/shijhon/" in done.stdout
    assert "from your network" in done.stdout and "not encrypted" in done.stdout
    assert 'The databases are in Docker volumes. Backups: docs/deployment.md, "Backups".' in (
        done.stdout
    )
    assert "Warning" not in done.stdout


def test_a_second_run_asks_nothing_and_overwrites_nothing(setup: Setup) -> None:
    assert setup.run(*setup.answers()).returncode == 0
    before = setup.written()
    calls = len(setup.state["calls"])

    again = setup.run()
    assert again.returncode == 0, again.stderr
    assert setup.written() == before
    assert "packaging/.env exists: used as it is" in again.stdout
    assert "packaging/config/shijhon.toml exists: kept." in again.stdout
    assert "Navidrome has its accounts: kept." in again.stdout
    assert "Shijhon is running" in again.stdout
    posted = [c["stdin"] for c in setup.state["calls"][calls:] if "data = " in c["stdin"]]
    assert len(posted) == 1 and "/auth/login" in posted[0]  # nothing created again


def test_options_that_would_change_an_existing_env_are_refused(setup: Setup) -> None:
    assert setup.run(*setup.answers()).returncode == 0
    before = setup.written()
    refused = setup.run("--music", str(setup.music))
    assert refused.returncode == 1
    assert "packaging/.env exists and is used as it is" in refused.stderr
    assert setup.written() == before


def test_the_password_can_come_on_the_standard_input(setup: Setup) -> None:
    answers = [a for a in setup.answers() if a != str(setup.password_file)]
    answers.insert(answers.index("--password-file") + 1, "-")
    done = setup.run(*answers, stdin=OWN_PASSWORD + "\n")
    assert done.returncode == 0, done.stderr
    assert setup.state["users"]["yourname"] == OWN_PASSWORD


def test_paths_with_a_space_and_a_quote(setup: Setup, tmp_path: Path) -> None:
    music = tmp_path / "Jan's music folder"
    music.mkdir()
    answers = setup.answers()
    answers[1] = str(music)
    assert setup.run(*answers).returncode == 0
    line = (setup.packaging / ".env").read_text().splitlines()[0]
    assert line == "MUSIC_DIR='" + str(music.resolve()).replace("'", "\\'") + "'"
    again = setup.run()  # ... and it reads its own line back
    assert again.returncode == 0, again.stderr
    assert f"music in {music.resolve()}," in again.stdout


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ("no docker", "Docker is not installed"),
        ("daemon down", "Docker is not running"),
        ("no music", "there is no folder"),
        ("music unreadable", "cannot be read by this user"),
        ("port in use", "is in use on this machine"),
        ("bad port", "the port is a number from 1 to 65535"),
        ("service name", "is the name of Shijhon's own account"),
        ("service name, other case", "is the name of Shijhon's own account"),
        ("set in the shell", "MUSIC_DIR is set in this shell"),
        ("data set in the shell", "SHIJHON_DATA is set in this shell"),
        ("no password", "the password file"),
        ("other installation", "already has containers"),
        ("earlier volumes", 'Docker has volumes of the Compose project "shijhon"'),
        ("colon", "a path with a colon"),
    ],
)
def test_checks_come_first_and_nothing_is_written(
    setup: Setup, tmp_path: Path, change: str, message: str
) -> None:
    answers = setup.answers()
    path = None
    listener = None
    more = {}
    if change == "no docker":
        tools = tmp_path / "tools"
        tools.mkdir()
        (tools / "dirname").symlink_to(shutil.which("dirname") or "/usr/bin/dirname")
        path = str(tools)
    elif change == "daemon down":
        setup.set_state(daemon=False)
    elif change == "no music":
        answers[1] = str(tmp_path / "nowhere")
    elif change == "music unreadable":
        if os.getuid() == 0:
            pytest.skip("root reads everything")
        setup.music.chmod(0o000)
    elif change == "port in use":
        if shutil.which("nc") is None:
            pytest.skip("the script checks the port with nc")
        listener = socket.socket()
        listener.bind(("127.0.0.1", setup.port))
        listener.listen()
    elif change == "bad port":
        answers[answers.index("--port") + 1] = "70000"
    elif change == "service name":
        answers[answers.index("--user") + 1] = "shijhon"
    elif change == "service name, other case":
        answers[answers.index("--user") + 1] = "Shijhon"
    elif change == "set in the shell":
        more = {"MUSIC_DIR": str(setup.music)}
    elif change == "data set in the shell":
        more = {"SHIJHON_DATA": str(tmp_path / "elsewhere")}
    elif change == "no password":
        answers[answers.index("--password-file") + 1] = str(tmp_path / "missing")
    elif change == "other installation":
        setup.set_state(projects={"shijhon": [str(tmp_path / "elsewhere" / "packaging")]})
    elif change == "earlier volumes":
        setup.set_state(volumes=["shijhon_shijhon-data", "other_navidrome-data", "shijhon_x"])
    elif change == "colon":
        answers[1] = str(tmp_path / "mu:sic")
        (tmp_path / "mu:sic").mkdir()
    try:
        stopped = setup.run(*answers, path=path, more=more)
    finally:
        if listener is not None:
            listener.close()
        setup.music.chmod(0o755)
    assert stopped.returncode == 1
    assert message in stopped.stderr
    assert setup.written() == {}
    assert not (setup.music / "_shijhon").exists()
    started = [c["args"] for c in setup.state["calls"] if c["args"][:2] == ["compose", "up"]]
    assert started == []
    if change == "earlier volumes":
        assert stopped.stderr.rstrip().endswith("docker volume rm shijhon_shijhon-data")
        assert "other_navidrome-data" not in stopped.stderr


def test_without_a_terminal_a_missing_answer_stops_the_run(setup: Setup) -> None:
    stopped = setup.run("--port", str(setup.port))
    assert stopped.returncode == 1
    assert "where is your music? --music DIR" in stopped.stderr
    stopped = setup.run("--music", str(setup.music))
    assert stopped.returncode == 1
    assert "--user NAME --password-file FILE" in stopped.stderr
    assert setup.written() == {}


def test_a_run_that_stopped_halfway_says_what_is_done_and_goes_on(setup: Setup) -> None:
    setup.set_state(build_fails=True)
    stopped = setup.run(*setup.answers())
    assert stopped.returncode == 1
    assert "Shijhon's image could not be built" in stopped.stderr
    assert "The setup did not finish. Done so far:" in stopped.stderr
    for step in (
        "packaging/.env written",
        "packaging/config/shijhon.toml written",
        "a password for Shijhon's own Navidrome account written",
        "Navidrome started",
        'Navidrome\'s account "shijhon" created',
        'your account "yourname" created',
    ):
        assert step in stopped.stderr
    assert "Run ./setup.sh again: it goes on from there." in stopped.stderr
    before = setup.written()
    users = setup.state["users"]

    setup.set_state(build_fails=False)
    again = setup.run()
    assert again.returncode == 0, again.stderr
    assert "Shijhon is running" in again.stdout
    assert setup.written() == before and setup.state["users"] == users


def test_a_password_file_left_without_its_account_is_used(setup: Setup) -> None:
    """A stop between writing Shijhon's password and creating its account: the next run
    creates the account with the password that is there."""
    config = setup.packaging / "config"
    config.mkdir()
    (config / "navidrome-password").write_text("left-behind\n")
    assert setup.run(*setup.answers()).returncode == 0
    assert (config / "navidrome-password").read_text() == "left-behind\n"
    assert setup.state["users"]["shijhon"] == "left-behind"


def test_a_navidrome_with_other_accounts_is_left_alone(setup: Setup) -> None:
    """The database appears with accounts this setup does not know (the first start found
    an existing one): the run stops, and the password it had just made is gone again."""
    setup.set_state(users={"someone": "else"})
    stopped = setup.run(*setup.answers())
    assert stopped.returncode == 1
    assert "already has accounts" in stopped.stderr
    assert not (setup.packaging / "config" / "navidrome-password").exists()
    assert setup.state["users"] == {"someone": "else"}


def test_an_existing_account_of_that_name_is_kept(setup: Setup) -> None:
    assert setup.run(*setup.answers()).returncode == 0
    setup.password_file.write_text("another password\n")
    again = setup.run("--user", "YourName", "--password-file", str(setup.password_file))
    assert again.returncode == 0, again.stderr
    assert 'Navidrome has the account "YourName": kept, with the password it has.' in again.stdout
    assert setup.state["users"] == {**setup.state["users"], "yourname": OWN_PASSWORD}
    assert len(setup.state["users"]) == 2


def test_the_service_accounts_name_is_refused_on_a_later_run_too(setup: Setup) -> None:
    assert setup.run(*setup.answers()).returncode == 0
    calls = len(setup.state["calls"])
    refused = setup.run("--user", "Shijhon", "--password-file", str(setup.password_file))
    assert refused.returncode == 1
    assert "is the name of Shijhon's own account" in refused.stderr
    assert [c for c in setup.state["calls"][calls:] if c["args"][:2] == ["compose", "up"]] == []


def test_a_file_is_never_written_over_what_is_there(setup: Setup) -> None:
    """Something that is no file sits where the password file goes: nothing is put in its
    place or inside it, and no copy of the password stays behind."""
    config = setup.packaging / "config"
    (config / "navidrome-password").mkdir(parents=True)
    stopped = setup.run(*setup.answers())
    assert stopped.returncode == 1
    assert "navidrome-password appeared while the setup ran: it is kept as it is" in stopped.stderr
    assert sorted(f.name for f in config.iterdir()) == ["navidrome-password", "shijhon.toml"]
    assert list((config / "navidrome-password").iterdir()) == []
    assert setup.state["users"] == {}


def test_an_account_made_by_another_run_meanwhile_is_used(setup: Setup) -> None:
    """Two runs at once: the other one created Shijhon's account with the password both
    read from the file. This run signs in with it; the password file stays."""
    setup.set_state(raced=True)
    done = setup.run(*setup.answers())
    assert done.returncode == 0, done.stderr
    service_password = (setup.packaging / "config" / "navidrome-password").read_text().rstrip()
    assert setup.state["users"] == {"shijhon": service_password, "yourname": OWN_PASSWORD}


def test_a_stop_after_navidromes_first_start_can_be_continued(setup: Setup) -> None:
    """Navidrome made its database and the run stopped there: Shijhon's password was written
    before, so the next run knows the database as its own and goes on."""
    setup.set_state(navidrome_fails=True)
    stopped = setup.run(*setup.answers())
    assert stopped.returncode == 1
    assert "Navidrome could not be started" in stopped.stderr
    assert "shijhon_navidrome-data" in setup.state["volumes"]
    before = setup.written()
    assert len(before) == 3

    setup.set_state(navidrome_fails=False)
    again = setup.run("--user", "yourname", "--password-file", str(setup.password_file))
    assert again.returncode == 0, again.stderr
    assert setup.written() == before
    service_password = (setup.packaging / "config" / "navidrome-password").read_text().rstrip()
    assert setup.state["users"] == {"shijhon": service_password, "yourname": OWN_PASSWORD}


def test_a_database_without_its_env_file_is_not_started(setup: Setup) -> None:
    """A finished setup whose .env is gone: its volumes are not taken for a new one."""
    assert setup.run(*setup.answers()).returncode == 0
    (setup.packaging / ".env").unlink()
    calls = len(setup.state["calls"])
    stopped = setup.run(*setup.answers())
    assert stopped.returncode == 1
    assert 'Docker has volumes of the Compose project "shijhon"' in stopped.stderr
    assert "put its packaging/.env and packaging/config back" in stopped.stderr
    assert not (setup.packaging / ".env").exists()
    started = [c for c in setup.state["calls"][calls:] if c["args"][:2] == ["compose", "up"]]
    assert started == []


def test_an_env_file_written_by_hand_is_read_as_compose_reads_it(setup: Setup) -> None:
    """Bare values, paths relative to packaging/, and a project name of its own: the script
    checks the project Compose will use, creates the folders where Compose will look, and
    leaves the data it keeps in volumes to them."""
    (setup.packaging / ".env").write_text(
        "MUSIC_DIR=../music by hand\nNAVIDROME_DATA=../data by hand/navidrome\n"
        "SHIJHON_DATA=shijhon-data\n"
        f"PUID={os.getuid()}\nPGID={os.getgid()}\n"
        "COMPOSE_PROJECT_NAME = mine\nexport SHIJHON_PORT=5123  # the port\n"
    )
    (setup.root / "music by hand").mkdir()
    setup.set_state(projects={"mine": [str(setup.root / "elsewhere")]})
    stopped = setup.run("--user", "yourname", "--password-file", str(setup.password_file))
    assert stopped.returncode == 1
    assert 'the Compose project "mine" already has containers' in stopped.stderr

    setup.set_state(projects={"shijhon": [str(setup.root / "elsewhere")]})
    done = setup.run("--user", "yourname", "--password-file", str(setup.password_file))
    assert done.returncode == 0, done.stderr
    assert (setup.root / "data by hand" / "navidrome" / "navidrome.db").exists()
    assert (setup.root / "music by hand" / "_shijhon").is_dir()
    assert setup.state["volumes"] == ["mine_shijhon-data", "mine_usage-export"]
    assert "http://127.0.0.1:5123/shijhon/" in done.stdout
    assert "The databases are in Docker volumes" not in done.stdout


def an_installation_with_data_folders(setup: Setup, tmp_path: Path) -> Path:
    """An installation whose .env names a folder for Navidrome's data, Shijhon's and the
    usage export, with Navidrome's database and both accounts in it."""
    data = tmp_path / "the data"
    for folder in ("navidrome", "shijhon", "usage"):
        (data / folder).mkdir(parents=True)
    (data / "navidrome" / "navidrome.db").write_text("the database")
    (setup.music / "_shijhon").mkdir()
    config = setup.packaging / "config"
    config.mkdir()
    shutil.copy(setup.packaging / "shijhon.example.toml", config / "shijhon.toml")
    (config / "navidrome-password").write_text("the service password\n")
    (setup.packaging / ".env").write_text(
        f"MUSIC_DIR='{setup.music}'\nNAVIDROME_DATA='{data}/navidrome'\n"
        f"SHIJHON_DATA='{data}/shijhon'\nUSAGE_EXPORT_DATA='{data}/usage'\n"
        f"PUID={os.getuid()}\nPGID={os.getgid()}\nSHIJHON_PORT={setup.port}\n"
    )
    setup.set_state(users={"shijhon": "the service password", "yourname": OWN_PASSWORD})
    return data


def test_an_installation_with_data_folders_keeps_them(setup: Setup, tmp_path: Path) -> None:
    """Folders named in .env stay in use, and .env as it is: nothing is put into a volume,
    on this run or the next."""
    data = an_installation_with_data_folders(setup, tmp_path)
    before = (setup.packaging / ".env").read_text()
    for _ in range(2):
        done = setup.run()
        assert done.returncode == 0, done.stderr
        assert (setup.packaging / ".env").read_text() == before
        assert setup.state["volumes"] == []
        assert (data / "navidrome" / "navidrome.db").read_text() == "the database"
        assert "Navidrome has its accounts: kept." in done.stdout
        assert "The databases are in Docker volumes" not in done.stdout
        assert "Warning" not in done.stdout
    assert sorted(p.name for p in setup.packaging.iterdir()) == [
        ".env",
        "compose.yaml",
        "config",
        "shijhon.example.toml",
    ]


@pytest.mark.parametrize("problem", ["double quotes", "a failing tool"])
def test_a_data_variable_that_cannot_be_read_stops_the_run(
    setup: Setup, tmp_path: Path, problem: str
) -> None:
    """A data variable the script cannot read is not taken for absent, which would be the
    volume: the run stops before anything is started, and .env is as it was."""
    an_installation_with_data_folders(setup, tmp_path)
    path = None
    if problem == "double quotes":
        with (setup.packaging / ".env").open("a") as env:
            env.write(f'NAVIDROME_DATA="{tmp_path / "the data" / "navidrome"}"\n')
    else:
        failing = tmp_path / "failing"
        failing.mkdir()
        sed = failing / "sed"
        sed.write_text(
            "#!/bin/sh\n"
            'case "$*" in *NAVIDROME_DATA*) exit 2 ;; esac\n'
            f'exec {shutil.which("sed")} "$@"\n'
        )
        sed.chmod(0o755)
        path = f"{failing}:{setup.bin}:/usr/bin:/bin:/usr/sbin:/sbin"
    before = (setup.packaging / ".env").read_text()
    stopped = setup.run(path=path)
    assert stopped.returncode == 1
    assert "USAGE_EXPORT_DATA in packaging/.env is not a folder's path, or cannot be read" in (
        stopped.stderr
    )
    assert (setup.packaging / ".env").read_text() == before
    assert setup.state["volumes"] == []
    assert [c for c in setup.state["calls"] if c["args"][:2] == ["compose", "up"]] == []


def test_a_data_folder_on_docker_desktop_is_warned_of(setup: Setup, tmp_path: Path) -> None:
    an_installation_with_data_folders(setup, tmp_path)
    setup.set_state(system="Docker Desktop")
    done = setup.run()
    assert done.returncode == 0, done.stderr
    assert "Warning: packaging/.env keeps data in a folder of this computer." in done.stdout
    assert "keep them in Docker volumes" in done.stdout


def test_a_data_folder_without_its_password_file_stops_the_run(
    setup: Setup, tmp_path: Path
) -> None:
    data = an_installation_with_data_folders(setup, tmp_path)
    (setup.packaging / "config" / "navidrome-password").unlink()
    calls = len(setup.state["calls"])
    stopped = setup.run()
    assert stopped.returncode == 1
    assert (
        f"Navidrome's data is there already ({data}/navidrome), and there is no "
        "packaging/config/navidrome-password" in stopped.stderr
    )
    assert [c for c in setup.state["calls"][calls:] if c["args"][:2] == ["compose", "up"]] == []


def test_a_volume_without_its_password_file_stops_the_run(setup: Setup) -> None:
    assert setup.run(*setup.answers()).returncode == 0
    (setup.packaging / "config" / "navidrome-password").unlink()
    calls = len(setup.state["calls"])
    stopped = setup.run()
    assert stopped.returncode == 1
    assert (
        "Navidrome's data is there already (the Docker volume shijhon_navidrome-data), and "
        "there is no packaging/config/navidrome-password" in stopped.stderr
    )
    assert [c for c in setup.state["calls"][calls:] if c["args"][:2] == ["compose", "up"]] == []
    assert not (setup.packaging / "config" / "navidrome-password").exists()


def test_a_folder_compose_takes_is_taken(setup: Setup) -> None:
    """A path that starts with "." is a folder for Compose, from packaging/: made there."""
    assert setup.run(*setup.answers()).returncode == 0
    with (setup.packaging / ".env").open("a") as env:
        env.write("SHIJHON_DATA=.state\n")
    again = setup.run()
    assert again.returncode == 0, again.stderr
    assert (setup.packaging / ".state").is_dir()


def test_a_placeholder_folder_that_cannot_be_made_stops_the_run(setup: Setup) -> None:
    if os.getuid() == 0:
        pytest.skip("root writes everywhere")
    assert setup.run(*setup.answers()).returncode == 0
    (setup.music / "_shijhon").rmdir()
    calls = len(setup.state["calls"])
    setup.music.chmod(0o555)
    try:
        stopped = setup.run()
    finally:
        setup.music.chmod(0o755)
    assert stopped.returncode == 1
    assert "/_shijhon, or a data folder that packaging/.env names, could not be created" in (
        stopped.stderr
    )
    assert [c for c in setup.state["calls"][calls:] if c["args"][:2] == ["compose", "up"]] == []


@pytest.mark.parametrize("value", ["another-volume", "relative/folder"])
def test_a_data_value_compose_would_refuse_stops_the_run(setup: Setup, value: str) -> None:
    """Compose takes a folder only by a path that starts with / or ./, and no volume but its
    own: anything else is refused before anything is started."""
    assert setup.run(*setup.answers()).returncode == 0
    with (setup.packaging / ".env").open("a") as env:
        env.write(f"SHIJHON_DATA={value}\n")
    calls = len(setup.state["calls"])
    stopped = setup.run()
    assert stopped.returncode == 1
    assert "USAGE_EXPORT_DATA in packaging/.env is not a folder's path" in stopped.stderr
    assert [c for c in setup.state["calls"][calls:] if c["args"][:2] == ["compose", "up"]] == []


def test_trusted_proxies_with_the_port_open_stop_a_run(setup: Setup) -> None:
    """A configuration that believes a reverse proxy while the port is open to every
    address: the .env has to say which is meant before anything is started again."""
    assert setup.run(*setup.answers()).returncode == 0
    config = setup.packaging / "config" / "shijhon.toml"
    config.write_text(config.read_text().replace("# trusted_proxies = [", "trusted_proxies = ["))
    assert "\ntrusted_proxies = [" in config.read_text()
    calls = len(setup.state["calls"])
    stopped = setup.run()
    assert stopped.returncode == 1
    assert "names trusted proxies" in stopped.stderr and "SHIJHON_BIND=127.0.0.1" in stopped.stderr
    assert [c for c in setup.state["calls"][calls:] if c["args"][:1] == ["compose"]] == [
        {"args": ["compose", "version"], "stdin": ""}
    ]

    with (setup.packaging / ".env").open("a") as env:
        env.write("SHIJHON_BIND=127.0.0.1\n")
    done = setup.run()
    assert done.returncode == 0, done.stderr
    assert "from your network" not in done.stdout and "not encrypted" not in done.stdout
    assert f"http://127.0.0.1:{setup.port}/shijhon/" in done.stdout


def test_no_command_is_given_a_secret_as_an_argument_or_in_its_environment(
    setup: Setup, tmp_path: Path
) -> None:
    """Every program the script runs is a recording stand-in here (the script finds nothing
    else on its PATH): what each is started with - its arguments and its environment - never
    holds a password or Navidrome's token, also when the caller's environment had variables
    of the script's names, exported."""
    tools = tmp_path / "recorded"
    tools.mkdir()
    log = tmp_path / "started.log"
    names = (
        "dirname cat sed tr tail sort grep od id mkdir rm ln mv nc uname route ipconfig ip sleep"
    )
    real = {name: shutil.which(name) for name in names.split()}
    real["docker"] = str(setup.bin / "docker")
    for name, path in real.items():
        if path is None:
            continue  # not on this system: the script does without (ip, route, ipconfig, nc)
        wrapper = tools / name
        wrapper.write_text(
            "#!/bin/sh\n"
            'printf \'%s\\n\' "started: $0 $*" >>"$STARTED_LOG"\n'
            '/usr/bin/env >>"$STARTED_LOG"\n'
            f'exec "{path}" "$@"\n'
        )
        wrapper.chmod(0o755)
    inherited = dict.fromkeys(
        ("own_password", "service_password", "token", "answer", "nd_answer", "nd_body"), "inherited"
    )
    done = setup.run(*setup.answers(), path=str(tools), more={"STARTED_LOG": str(log), **inherited})
    assert done.returncode == 0, done.stderr
    started = log.read_text(errors="replace")
    assert started.count("started: ") > 40
    service_password = (setup.packaging / "config" / "navidrome-password").read_text().rstrip()
    assert setup.state["users"] == {"shijhon": service_password, "yourname": OWN_PASSWORD}
    for secret in (OWN_PASSWORD, service_password, "token-of-"):
        assert secret not in started
    assert "inherited" not in started


def test_adapters_go_into_the_env_file(setup: Setup) -> None:
    spec = "example @ https://example.invalid/a.tar.gz#sha256=0 other==1.0"
    assert setup.run(*setup.answers(), "--adapters", spec).returncode == 0
    assert (setup.packaging / ".env").read_text().splitlines()[-1] == f"SHIJHON_ADAPTERS='{spec}'"


def test_at_a_terminal_it_asks_and_never_shows_the_password(setup: Setup) -> None:
    """The questions, answered at a pseudo-terminal: a wrong folder is asked again, the
    password is typed twice and echoed nowhere."""
    answers = [
        ("Where is your music?", str(setup.music / "nowhere")),
        ("Where is your music?", str(setup.music)),
        ("A username for your own account", "yourname"),
        ("A password for it", OWN_PASSWORD),
        ("The password again", "something else"),
        ("A password for it", OWN_PASSWORD),
        ("The password again", OWN_PASSWORD),
        ("The port your apps connect to", str(setup.port)),
        ("Catalog adapters to install", ""),
    ]
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [shutil.which("sh") or "sh", str(setup.root / "setup.sh")],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=setup.env(),
        cwd=setup.root,
        start_new_session=True,
    )
    os.close(slave)
    seen = b""  # bytes: a password must not hide behind a character split between reads

    def read_until(text: str, start: int) -> None:
        nonlocal seen
        deadline = time.monotonic() + 60
        while text.encode() not in seen[start:]:
            assert time.monotonic() < deadline, f"no {text!r} in {seen[start:]!r}"
            if select.select([master], [], [], 0.2)[0]:
                try:
                    seen += os.read(master, 4096)
                except OSError:  # the script ended (Linux reports it this way)
                    assert text.encode() in seen[start:], seen
                    return

    try:
        for question, reply in answers:
            start = len(seen)
            read_until(question, start)
            read_until(": ", seen.index(question.encode(), start))
            os.write(master, (reply + "\n").encode())
        read_until("Sign in with your Navidrome account (yourname).", 0)
        assert process.wait(timeout=30) == 0, seen
    finally:
        process.kill()
        os.close(master)
    assert b"there is no folder" in seen and b"The two differ." in seen
    assert OWN_PASSWORD.encode() not in seen and b"something else" not in seen
    assert setup.state["users"]["yourname"] == OWN_PASSWORD
    assert f"SHIJHON_PORT={setup.port}" in (setup.packaging / ".env").read_text()


def test_an_interrupt_at_the_password_question_leaves_nothing(setup: Setup) -> None:
    """Ctrl-C while the password is asked for: the terminal echoes again, and nothing has
    been written."""
    master, slave = pty.openpty()
    process = subprocess.Popen(
        [shutil.which("sh") or "sh", str(setup.root / "setup.sh"), "--music", str(setup.music)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=setup.env(),
        cwd=setup.root,
    )
    seen = ""

    def read_until(text: str) -> None:
        nonlocal seen
        deadline = time.monotonic() + 30
        while text not in seen:
            assert time.monotonic() < deadline, seen
            if select.select([master], [], [], 0.2)[0]:
                seen += os.read(master, 4096).decode(errors="replace")

    try:
        read_until("A username for your own account: ")
        os.write(master, b"yourname\n")
        read_until("A password for it (not shown): ")
        assert not termios.tcgetattr(slave)[3] & termios.ECHO
        process.send_signal(signal.SIGINT)
        assert process.wait(timeout=30) == 130
        read_until("setup: interrupted")
        assert termios.tcgetattr(slave)[3] & termios.ECHO
    finally:
        process.kill()
        os.close(slave)
        os.close(master)
    assert setup.written() == {}
    assert not (setup.music / "_shijhon").exists() and setup.state["volumes"] == []
    assert [c for c in setup.state["calls"] if c["args"][:2] == ["compose", "up"]] == []


def test_the_script_is_clean_under_shellcheck() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not installed (uvx --from shellcheck-py shellcheck setup.sh)")
    checked = subprocess.run(
        [shellcheck, "-s", "sh", str(REPO / "setup.sh")], capture_output=True, text=True
    )
    assert checked.returncode == 0, checked.stdout
