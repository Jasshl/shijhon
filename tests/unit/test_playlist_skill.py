"""Reusable skill client: synthetic input and HTTP responses; never network or inference."""

from __future__ import annotations

import importlib.util
import json
import socket
from email.message import Message
from http.client import BadStatusLine, IncompleteRead
from io import BytesIO
from pathlib import Path
from types import ModuleType
from urllib.error import HTTPError, URLError
from urllib.request import BaseHandler, ProxyHandler
from urllib.response import addinfourl

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "skills/shijhon-playlists/scripts/client.py"
TOKEN = "synthetic-only-bearer-secret"
KEY = "request-test-001"


@pytest.fixture(scope="module")
def skill() -> ModuleType:
    spec = importlib.util.spec_from_file_location("playlist_skill_client", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("The playlist skill tests must not open a real connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)


class Response(BytesIO):
    def __init__(self, value: object, *, status: int = 200, raw: bytes | None = None):
        super().__init__(json.dumps(value).encode() if raw is None else raw)
        self.status = status
        self.read_limits: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_limits.append(size)
        return super().read(size)


class Transport:
    def __init__(self):
        self.calls = []
        self.responses = []

    def open(self, request, *, timeout):
        self.calls.append(request)
        assert timeout == 15
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def transport(skill: ModuleType, monkeypatch: pytest.MonkeyPatch):
    fake = Transport()

    def opener(*handlers):
        assert any(isinstance(handler, skill.NoRedirect) for handler in handlers)
        assert (
            next(handler for handler in handlers if isinstance(handler, ProxyHandler)).proxies == {}
        )
        return fake

    monkeypatch.setattr(skill, "build_opener", opener)
    monkeypatch.setenv("SHIJHON_URL", "https://music.example.test")
    monkeypatch.setenv("SHIJHON_PLAYLIST_TOKEN", TOKEN)
    return fake


def test_defaults_and_exact_constraints_are_preserved(skill: ModuleType) -> None:
    body = skill.validate_create({"prompt": "  Quiet studio recordings only\n"})
    assert body == {
        "name": "Described playlist",
        "prompt": "  Quiet studio recordings only\n",
        "count": 30,
        "max_per_artist": 2,
        "use_history": False,
        "use_library": False,
        "tracks": [],
        "save_instructions": False,
        "cadence": "manual",
        "preview": False,
    }
    tracks = [
        {"artist": "Glass Orchard", "title": "First Lantern", "version": "Original studio"},
        {"artist": "Quill & Marrow", "title": "Night Light", "album": "  Paper Sky  "},
    ]
    value = {"count": 2, "max_per_artist": 1, "tracks": tracks, "prompt": "No live versions"}
    explicit = skill.validate_create(value)
    assert explicit["tracks"] == [
        {"album": "", "version": "Original studio", **tracks[0]},
        {"album": "  Paper Sky  ", "version": "", **tracks[1]},
    ]
    assert explicit["prompt"] == "No live versions"
    assert explicit["max_per_artist"] == 1
    assert "album" not in tracks[0]  # validation does not mutate the caller's input


def test_references_are_sent_only_when_supplied(skill: ModuleType) -> None:
    assert "reference_playlists" not in skill.validate_create({"prompt": "Livelier"})
    value = {
        "prompt": "Livelier than this playlist",
        "reference_playlists": ["pl-1", "pl-2", "pl-3"],
        "reference_songs": [f"song:{i}" for i in range(50)],
        "exclude_reference": False,
    }
    body = skill.validate_create(value)
    assert {k: body[k] for k in value} == value


def test_automatic_prompt_name_preserves_defaults_and_explicit_names(skill: ModuleType) -> None:
    assert skill.validate_create({"name": "", "prompt": "quiet"})["name"] == ""
    for name in ("Described playlist", "  Evening lights  "):
        assert skill.validate_create({"name": name, "prompt": "quiet"})["name"] == name
    with pytest.raises(skill.ClientError):
        skill.validate_create({"name": "", "count": 1, "tracks": [{"artist": "A", "title": "B"}]})


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        {"prompt": ""},
        {"prompt": " \n"},
        {"prompt": "x" * 2001},
        {"prompt": 4},
        {"prompt": "x", "count": True},
        {"prompt": "x", "count": 0},
        {"prompt": "x", "count": 101},
        {"prompt": "x", "count": 3.0},
        {"prompt": "x", "max_per_artist": 0},
        {"prompt": "x", "max_per_artist": 11},
        {"prompt": "x", "max_per_artist": True},
        {"prompt": "x", "use_history": "false"},
        {"prompt": "x", "use_library": 1},
        {"prompt": "x", "save_instructions": 0},
        {"prompt": "x", "preview": "true"},
        {"prompt": "x", "name": " "},
        {"prompt": "x", "name": "x" * 101},
        {"prompt": "x", "cadence": "hourly"},
        {"prompt": "x", "cadence": "daily"},
        {"prompt": "x", "tracks": {}},
        {"prompt": "x", "tracks": [None]},
        {"prompt": "x", "model": "unaccepted"},
        {"prompt": "x", "actions": []},
        {"prompt": "x\ud800"},
        {"count": 1, "tracks": [{"artist": "A", "title": "T"}], "use_history": True},
        {"count": 1, "tracks": [{"artist": "A", "title": "T"}], "use_library": True},
        {"count": 2, "tracks": [{"artist": "A", "title": "T"}]},
        {
            "count": 1,
            "tracks": [{"artist": "A", "title": "T"}],
            "cadence": "weekly",
            "prompt": "x",
            "save_instructions": True,
        },
        {"prompt": "x", "reference_playlists": ["a", "b", "c", "d"]},
        {"prompt": "x", "reference_playlists": "playlist"},
        {"prompt": "x", "reference_songs": ["s", "s"]},
        {"prompt": "x", "reference_songs": ["../s"]},
        {"prompt": "x", "reference_songs": ["s" * 101]},
        {"prompt": "x", "reference_songs": [7]},
        {"prompt": "x", "exclude_reference": "false"},
        {"count": 1, "tracks": [{"artist": "A", "title": "T"}], "reference_songs": ["s"]},
    ],
)
def test_invalid_creation_is_rejected_before_http(skill: ModuleType, transport, value) -> None:
    with pytest.raises(skill.ClientError):
        skill.Client.from_environment().create(value, key=KEY)
    assert not transport.calls


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "opaque"),
        ("url", "https://example.test/song"),
        ("model", "unaccepted"),
        ("action", "publish"),
        ("artist", ""),
        ("title", " "),
        ("title", "x" * 301),
        ("artist", "x" * 301),
        ("album", "x" * 301),
        ("version", "x" * 101),
        ("album", None),
    ],
)
def test_track_fields_are_bounded_text_only(skill: ModuleType, field: str, value: object) -> None:
    track = {"artist": "Glass Orchard", "title": "First Lantern", field: value}
    with pytest.raises(skill.ClientError):
        skill.validate_create({"count": 1, "tracks": [track]})


def test_contract_boundaries_and_recurring_prompt(skill: ModuleType) -> None:
    body = skill.validate_create(
        {
            "name": "n" * 100,
            "prompt": "p" * 2000,
            "count": 100,
            "max_per_artist": 10,
            "cadence": "weekly",
            "save_instructions": True,
            "preview": True,
        }
    )
    assert body["cadence"] == "weekly" and body["save_instructions"] is True
    track = {"artist": "a" * 300, "title": "t" * 300, "album": "l" * 300, "version": "v" * 100}
    assert len(skill.validate_create({"count": 100, "tracks": [track] * 100})["tracks"]) == 100


@pytest.mark.parametrize(
    "url",
    [
        "http://music.example.test",
        "http://192.168.1.2",
        "http://169.254.169.254",
        "http://localhost.example.test",
        "http://[::ffff:192.168.1.2]",
        "ftp://example.test",
        "https://user:secret@example.test",
        "https://user@example.test",
        "https://example.test?token=x",
        "https://example.test#x",
        "https://example.test?",
        "https://example.test#",
        "https://",
        "https://example.test:bad",
        "https://example.test:0",
        "https://example.test:65536",
        "https://example.test\n/",
        " https://example.test",
        "https://example.test\\@other.test",
        "https://%65xample.test",
        "http://127.0.0.1.example.test",
        "https://[broken",
    ],
)
def test_unsafe_urls_are_rejected_without_echo(skill: ModuleType, url: str) -> None:
    with pytest.raises(skill.ClientError) as caught:
        skill.Client(url, TOKEN)
    assert url not in str(caught.value)
    assert "user:secret" not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://music.example.test",
        "https://music.example.test/prefix/",
        "http://localhost:8080",
        "http://127.0.0.1:8080",
        "http://127.2.3.4",
        "http://[::1]:8080",
    ],
)
def test_https_and_loopback_urls(skill: ModuleType, url: str) -> None:
    assert skill.validate_url(url) == url.rstrip("/")


@pytest.mark.parametrize("token", ["", "space secret", "secret\r\nHeader: injected", "secret\x00"])
def test_invalid_tokens_never_echo(skill: ModuleType, token: str) -> None:
    with pytest.raises(skill.ClientError) as caught:
        skill.Client("https://music.example.test", token)
    assert token == "" or token not in str(caught.value)


@pytest.mark.parametrize(
    "key", [None, 12345678, "short", "x" * 129, "contains space", "bad\r\nheader", TOKEN]
)
def test_invalid_keys_do_not_send(skill: ModuleType, transport, key: object) -> None:
    with pytest.raises(skill.ClientError):
        skill.Client.from_environment().create({"prompt": "Quiet"}, key=key)
    assert not transport.calls


def test_create_replay_sends_identical_body_and_header(skill: ModuleType, transport) -> None:
    transport.responses = [
        Response({"id": "request-1", "state": "queued"}, status=202),
        Response({"id": "request-1", "state": "queued"}),
    ]
    client = skill.Client.from_environment()
    value = {"prompt": "Quiet\nNo live versions", "count": 12, "max_per_artist": 1}
    assert client.create(value, key=KEY) == client.create(value, key=KEY)
    first, replay = transport.calls
    assert first.data == replay.data
    assert first.get_header("Idempotency-key") == replay.get_header("Idempotency-key") == KEY
    assert first.get_method() == "POST"
    assert first.full_url == "https://music.example.test/shijhon/api/playlists/requests"
    assert first.get_header("Authorization") == f"Bearer {TOKEN}"
    assert TOKEN not in first.full_url and TOKEN.encode() not in first.data
    assert json.loads(first.data)["count"] == 12


@pytest.mark.parametrize(
    ("code", "location"),
    [
        (301, "/new"),
        (302, "https://other.example.test/"),
        (303, "/new"),
        (307, "https://other.example.test/"),
        (308, "/new"),
        (300, "/new"),
        (304, "/new"),
    ],
)
def test_real_opener_rejects_all_redirects_without_a_second_request(
    skill: ModuleType,
    code: int,
    location: str,
) -> None:
    seen = []
    stream = BytesIO(TOKEN.encode())

    class SyntheticHTTPS(BaseHandler):
        handler_order = 100

        def https_open(self, request):
            seen.append(request)
            headers = Message()
            headers["Location"] = location
            response = addinfourl(stream, headers, request.full_url, code)
            response.msg = "Synthetic redirect"
            return response

    client = skill.Client("https://music.example.test", TOKEN)
    client._opener.add_handler(SyntheticHTTPS())
    with pytest.raises(skill.ClientError, match="Redirect refused") as caught:
        client.create({"prompt": "Quiet"}, key=KEY)
    assert len(seen) == 1
    assert seen[0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert TOKEN not in str(caught.value) and location not in str(caught.value)
    assert stream.closed


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (400, "rejected the input"),
        (401, "Invalid playlist token"),
        (403, "does not permit"),
        (404, "not found"),
        (409, "conflicts"),
        (413, "body is too large"),
        (415, "Unsupported media type"),
        (424, "setup is missing"),
        (429, "Too many playlist requests"),
        (500, "outcome may be unknown"),
    ],
)
def test_http_errors_hide_remote_body_url_and_headers(
    skill: ModuleType,
    transport,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    code: int,
    message: str,
) -> None:
    stream = BytesIO(f"private-body {TOKEN}".encode())
    transport.responses = [
        HTTPError(
            f"https://private.test/?token={TOKEN}",
            code,
            f"private-reason {TOKEN}",
            {"Secret": TOKEN},
            stream,
        )
    ]
    path = tmp_path / "input.json"
    path.write_text('{"prompt":"Quiet"}')
    assert skill.main(["create", str(path), "--idempotency-key", KEY]) == 1
    captured = capsys.readouterr()
    assert message in captured.err
    assert TOKEN not in captured.out + captured.err
    assert "private-" not in captured.err
    assert stream.closed and len(transport.calls) == 1


@pytest.mark.parametrize(
    "failure",
    [
        URLError(f"private transport {TOKEN}"),
        TimeoutError(TOKEN),
        OSError(TOKEN),
        BadStatusLine(TOKEN),
        IncompleteRead(TOKEN.encode()),
    ],
)
def test_transport_errors_do_not_retry_or_echo(
    skill: ModuleType, transport, failure: Exception
) -> None:
    transport.responses = [failure]
    with pytest.raises(skill.ClientError, match="outcome may be unknown") as caught:
        skill.Client.from_environment().create({"prompt": "Quiet"}, key=KEY)
    assert TOKEN not in str(caught.value)
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        b"not JSON private-body",
        b"[]",
        b"null",
        b'{"state":NaN}',
        b'{"state":1,"state":2}',
        b'{"value":1e9999}',
        b"\xff",
        b"x" * (1024 * 1024 + 1),
    ],
)
def test_invalid_or_oversized_response_body_is_bounded_and_hidden(
    skill: ModuleType,
    transport,
    body: bytes,
) -> None:
    response = Response(None, raw=body)
    transport.responses = [response]
    with pytest.raises(skill.ClientError, match="Invalid or oversized") as caught:
        skill.Client.from_environment().create({"prompt": "Quiet"}, key=KEY)
    assert "private-body" not in str(caught.value)
    assert response.closed and response.read_limits == [1024 * 1024 + 1]


def test_cli_redacts_token_even_in_successful_nested_metadata(
    skill: ModuleType,
    transport,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport.responses = [
        Response(
            {
                "state": "failed",
                "error": TOKEN,
                "selected": [{"title": f"prefix {TOKEN}"}],
                TOKEN: "key",
            }
        )
    ]
    assert skill.main(["status", "request-1"]) == 0
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert "[redacted]" in captured.out
    assert json.loads(captured.out)["state"] == "failed"


@pytest.mark.parametrize(
    ("args", "history", "library"),
    [
        ([], False, False),
        (["--history"], True, False),
        (["--library"], False, True),
        (["--history", "--library"], True, True),
    ],
)
def test_context_sources_need_separate_explicit_switches(
    skill: ModuleType,
    transport,
    capsys,
    args: list[str],
    history: bool,
    library: bool,
) -> None:
    transport.responses = [Response({"history": [], "library": []})]
    assert skill.main(["context", "--limit", "12", *args]) == 0
    request = transport.calls[0]
    assert request.full_url.endswith("/context") and request.get_method() == "POST"
    assert json.loads(request.data) == {"use_history": history, "use_library": library, "limit": 12}
    assert request.get_header("Idempotency-key") is None


@pytest.mark.parametrize("value", [0, 101, True, 1.0, "30"])
def test_context_limit_rejects_invalid_values(skill: ModuleType, value: object) -> None:
    with pytest.raises(skill.ClientError):
        skill.validate_context(limit=value)


def test_status_polling_is_bounded_read_only_and_retry_is_same_request(
    skill: ModuleType,
    transport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps = []
    monkeypatch.setattr(skill.time, "sleep", sleeps.append)
    client = skill.Client.from_environment()
    transport.responses = [Response({"state": "queued"}) for _ in range(3)]
    assert client.status("request-1", attempts=3, interval=2) == {"state": "queued"}
    assert sleeps == [2, 2]
    assert [request.get_method() for request in transport.calls] == ["GET"] * 3
    assert all(request.data is None for request in transport.calls)
    transport.responses = [Response({"id": "request-1", "state": "queued"}, status=202)]
    assert client.retry("request-1")["id"] == "request-1"
    request = transport.calls[-1]
    assert request.full_url.endswith("/requests/request-1/retry")
    assert request.get_method() == "POST" and request.data == b"{}"


@pytest.mark.parametrize("state", ["published", "failed", "uncertain", "preview", "future-state"])
def test_polling_stops_on_terminal_and_unknown_states(
    skill: ModuleType, transport, state: str
) -> None:
    transport.responses = [Response({"state": state})]
    assert skill.Client.from_environment().status("request-1", attempts=30)["state"] == state
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "ident", ["../other", "..", ".", "a/b", "a?token=x", "https://other.test", TOKEN]
)
def test_ids_cannot_change_endpoint_or_include_token(
    skill: ModuleType, transport, ident: str
) -> None:
    with pytest.raises(skill.ClientError):
        skill.Client.from_environment().status(ident)
    assert not transport.calls


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"[]",
        b'{"prompt":"a","prompt":"b"}',
        b'{"prompt":NaN}',
        b"\xff",
        b"x" * (256 * 1024 + 1),
    ],
)
def test_input_file_is_strict_and_bounded(skill: ModuleType, tmp_path: Path, body: bytes) -> None:
    path = tmp_path / "input.json"
    path.write_bytes(body)
    with pytest.raises(skill.ClientError):
        skill.load_input(path)


def test_cli_create_reads_saved_json_and_preserves_preview_and_recurrence(
    skill: ModuleType,
    transport,
    tmp_path: Path,
    capsys,
) -> None:
    path = tmp_path / "playlist.json"
    path.write_text(
        json.dumps(
            {
                "prompt": "Quiet",
                "count": 17,
                "cadence": "daily",
                "save_instructions": True,
                "preview": True,
            }
        )
    )
    transport.responses = [Response({"id": "request-1", "state": "queued"}, status=202)]
    assert skill.main(["create", str(path), "--idempotency-key", KEY]) == 0
    body = json.loads(transport.calls[0].data)
    assert body["count"] == 17 and body["preview"] is True
    assert body["cadence"] == "daily" and body["save_instructions"] is True
    assert json.loads(capsys.readouterr().out)["id"] == "request-1"


def test_argument_errors_do_not_echo_secret_values(skill: ModuleType, capsys) -> None:
    with pytest.raises(SystemExit) as caught:
        skill.main(["status", "request-1", "--attempts", TOKEN])
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err


@pytest.mark.parametrize("state", [None, [], {}])
def test_malformed_status_does_not_crash_or_continue_polling(
    skill: ModuleType,
    transport,
    capsys,
    state: object,
) -> None:
    transport.responses = [Response({"state": state})]
    assert skill.main(["status", "request-1", "--attempts", "3"]) == 1
    assert "Invalid status response" in capsys.readouterr().err
    assert len(transport.calls) == 1


@pytest.mark.parametrize("setting", ["SHIJHON_URL", "SHIJHON_PLAYLIST_TOKEN"])
def test_missing_environment_is_safe_and_does_not_send(
    skill: ModuleType,
    transport,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    setting: str,
) -> None:
    monkeypatch.delenv(setting)
    assert skill.main(["status", "request-1"]) == 1
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert not transport.calls


def test_retry_cli_and_prefix_keep_the_same_endpoint(
    skill: ModuleType, transport, monkeypatch, capsys
):
    monkeypatch.setenv("SHIJHON_URL", "http://[::1]:8080/prefix/")
    transport.responses = [Response({"id": "request-1", "state": "queued"}, status=202)]
    assert skill.main(["retry", "request-1"]) == 0
    assert transport.calls[0].full_url == (
        "http://[::1]:8080/prefix/shijhon/api/playlists/requests/request-1/retry"
    )
    assert transport.calls[0].data == b"{}"
    assert json.loads(capsys.readouterr().out)["id"] == "request-1"


def test_missing_private_input_does_not_echo_path_or_secret(skill: ModuleType, transport, capsys):
    assert skill.main(["create", f"/missing/{TOKEN}.json", "--idempotency-key", KEY]) == 1
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert not transport.calls


@pytest.mark.parametrize(("attempts", "interval"), [(0, 2), (31, 2), (True, 2), (1, 0), (1, 31)])
def test_poll_budget_is_validated_before_http(skill: ModuleType, transport, attempts, interval):
    with pytest.raises(skill.ClientError):
        skill.Client.from_environment().status("request-1", attempts=attempts, interval=interval)
    assert not transport.calls


@pytest.mark.parametrize(
    "url",
    [
        f"https://music.example.test/{TOKEN}",
        "https://music.example.test/" + "".join(f"%{ord(char):02x}" for char in TOKEN),
    ],
)
def test_bearer_token_cannot_be_embedded_in_base_url(skill: ModuleType, transport, url: str):
    with pytest.raises(skill.ClientError) as caught:
        skill.Client(url, TOKEN)
    assert TOKEN not in str(caught.value)
    assert not transport.calls


def test_uncertain_create_only_replays_when_explicitly_called_with_same_input(
    skill: ModuleType,
    transport,
) -> None:
    transport.responses = [URLError(TOKEN), Response({"id": "request-1", "state": "queued"})]
    client = skill.Client.from_environment()
    value = {"prompt": "Quiet", "count": 12, "max_per_artist": 1}
    with pytest.raises(skill.ClientError, match="outcome may be unknown"):
        client.create(value, key=KEY)
    assert len(transport.calls) == 1
    assert client.create(value, key=KEY)["id"] == "request-1"
    original, replay = transport.calls
    assert original.data == replay.data
    assert original.get_header("Idempotency-key") == replay.get_header("Idempotency-key") == KEY
