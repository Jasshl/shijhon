"""Suite C — interception (placeholder audio; the commit half follows with catalog IDs).

Placeholder audio is intercepted on every Navidrome 0.64.2 route that serves audio:
``stream`` and ``download`` (GET, POST, HEAD, ``.view``), ``getTranscodeDecision`` /
``getTranscodeStream``, share links and jukebox. Requests where Shijhon does no work are
forwarded without a credential check. With wrong credentials a placeholder request
causes no add-on request, no file change and no scan (suite B).
"""

from __future__ import annotations

import json
import re
import socket
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest

from shijhon.delivery import pacing
from tests.conftest import NavidromeFactory
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.fake_addon import FakeAddon
from tests.harness.library import simple_album, write_album
from tests.harness.navidrome import ADMIN_PASSWORD, ADMIN_USER
from tests.harness.subsonic import SubsonicClient

# A client that plays FLAC as it is, at original quality (no bitrate limit).
CLIENT_INFO: dict[str, Any] = {
    "name": "test",
    "platform": "test",
    "directPlayProfiles": [
        {"containers": ["flac"], "audioCodecs": ["flac"], "protocols": ["http"]}
    ],
    "transcodingProfiles": [{"container": "mp3", "audioCodec": "mp3", "protocol": "http"}],
}
# The same with a bitrate limit (bits per second): Navidrome decides from the real file.
LIMITED = {**CLIENT_INFO, "maxAudioBitrate": 320_000}
# A client that cannot play FLAC as it is (the silent placeholder is FLAC).
MP3_ONLY = {
    **CLIENT_INFO,
    "directPlayProfiles": [{"containers": ["mp3"], "audioCodecs": ["mp3"], "protocols": ["http"]}],
}


@pytest.fixture(scope="module")
def world(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[DeliveryWorld]:
    nd = navidrome_factory({"ND_ENABLESHARING": "true", "ND_JUKEBOX_ENABLED": "true"})
    owned = simple_album("Owned Band", "Owned Record", 2)
    write_album(nd.music, owned)
    nd.scan(full=True)
    with delivery_world(nd, tmp_path_factory.mktemp("c"), budget_seconds=8.0) as w:
        addon = w.addon("Source")
        w.add_source(addon)
        yield w


@pytest.fixture
def addon(world: DeliveryWorld) -> FakeAddon:
    return world.addons[0]


def state(world: DeliveryWorld, song: str) -> str:
    row = world.server.call(
        lambda: world.services.store.fetchone(
            "SELECT state FROM placeholders WHERE song_id = ?", [song]
        )
    )
    assert row is not None
    return str(row["state"])


def library_bytes(world: DeliveryWorld, song: str) -> bytes:
    path = world.nd.native("GET", f"song/{song}").json()["path"]
    return (world.nd.music / path).read_bytes()


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"http_method": "POST"},
        {"view_suffix": True},
        {"http_method": "POST", "view_suffix": True},
    ],
    ids=["get", "post", "view", "post-view"],
)
def test_stream_is_intercepted(
    world: DeliveryWorld, addon: FakeAddon, kwargs: dict[str, Any]
) -> None:
    key = "c-stream-" + "-".join(f"{k}{v}" for k, v in kwargs.items())
    song, _, audio = world.placeholder_track(key, [addon])
    response = world.client().request("stream", {"id": song}, **kwargs)
    assert response.content == audio.read_bytes()
    head = world.client().request("stream", {"id": song}, http_method="HEAD")
    assert head.headers["content-length"] == str(audio.stat().st_size)


def _pieces(data: bytes, size: int = 64 * 1024) -> Iterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


FORM_TYPE = "application/x-www-form-urlencoded"


@pytest.mark.parametrize("method", ["stream", "download"])
@pytest.mark.parametrize(
    ("http_method", "content_type", "in_body", "pad", "whole"),
    [
        ("POST", FORM_TYPE, True, 3 << 20, False),  # the ID after 3 MiB of the form
        ("POST", FORM_TYPE, True, 3 << 20, True),  # ... sent with its length
        ("POST", "Application/X-WWW-Form-Urlencoded", True, 0, True),
        ("POST", "APPLICATION/X-Www-Form-UrlEncoded ; Charset=UTF-8", True, 2 << 20, False),
        ("PUT", FORM_TYPE, True, 2 << 20, False),
        ("PATCH", "application/X-www-form-urlencoded", True, 0, True),
        ("POST", "application/octet-stream", False, 3 << 20, False),  # no parameters in it
        ("POST", "application/json", False, 3 << 20, True),
        ("DELETE", FORM_TYPE, False, 2 << 20, False),  # not a form for this method
    ],
    ids=[
        "long-form",
        "long-form-whole",
        "shouted",
        "shouted-long",
        "put",
        "patch",
        "long-other-body",
        "long-json-body",
        "delete",
    ],
)
def test_interception_does_not_depend_on_the_body_or_its_header(
    world: DeliveryWorld,
    addon: FakeAddon,
    request: pytest.FixtureRequest,
    method: str,
    http_method: str,
    content_type: str,
    in_body: bool,
    pad: int,
    whole: bool,
) -> None:
    """A placeholder's stream or download never reaches Navidrome's silent file, also when
    its form is longer than 1 MiB or its Content-Type capitalized: Shijhon reads what
    Navidrome reads as parameters, whatever the body's size, the header's letter case or the
    method."""
    song, _, audio = world.placeholder_track(f"c-body-{request.node.callspec.id}", [addon])
    client = world.client()
    padding = [("pad", "x" * pad)] if pad else []
    named = [*client.auth_params(), ("f", "json"), ("id", song)]
    form = urlencode([*padding, *named]).encode() if in_body else b"x" * pad
    response = httpx.request(
        http_method,
        f"{client.base_url}/rest/{method}",
        params=[] if in_body else named,
        content=form if whole else _pieces(form),
        headers={"content-type": content_type},
        timeout=60,
    )
    if method == "stream":
        assert response.status_code == 200 and response.content == audio.read_bytes()
        return
    assert state(world, song) == "delivered"  # fetched first, whatever became of the body
    if in_body or response.status_code == 200:
        assert response.status_code == 200
        assert response.content == library_bytes(world, song)
        assert response.content[:4] == b"fLaC" and len(response.content) > 20_000
    else:
        # A long body Navidrome does not read: it may answer and close before the body is
        # through (the proxy's 502, as for any such forward) - never the silent file.
        assert response.status_code == 502


def test_a_method_in_small_letters_is_the_method_navidrome_gets(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """A request line with "post" reaches Navidrome as POST (the upstream client sends
    methods in capitals), so it is a POST for Shijhon too: its form is read, the
    placeholder's audio intercepted."""
    song, _, audio = world.placeholder_track("c-small-method", [addon])
    form = urlencode([*world.client().auth_params(), ("id", song)]).encode()
    address = httpx.URL(world.server.base_url)
    head = (
        f"post /rest/stream HTTP/1.1\r\nHost: {address.host}\r\n"
        f"Content-Type: {FORM_TYPE}\r\nContent-Length: {len(form)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    answer = b""
    with socket.create_connection((address.host, address.port), timeout=30) as connection:
        connection.sendall(head + form)
        while chunk := connection.recv(65536):
            answer += chunk
    assert answer.startswith(b"HTTP/1.1 200")
    assert answer.endswith(audio.read_bytes())


@pytest.mark.parametrize("method", ["stream", "download"])
def test_a_form_is_read_as_far_as_navidrome_reads_it(
    world: DeliveryWorld, addon: FakeAddon, method: str
) -> None:
    """Navidrome 0.64.2 reads a form of up to 10 MiB and answers a longer one with an error
    before it looks at a parameter: up to that length Shijhon intercepts, a longer one gets
    Navidrome's own answer - never the placeholder's silence."""
    limit = 10 << 20
    song, _, audio = world.placeholder_track(f"c-limit-{method}", [addon])
    client = world.client()
    named = urlencode([*client.auth_params(), ("f", "json"), ("id", song)]).encode()

    def post(base: str, length: int, whole: bool, query: bytes = b"") -> httpx.Response:
        body = b"pad=" + b"x" * (length - len(named) - 5) + b"&" + named
        assert len(body) == length
        return httpx.post(
            f"{base}/rest/{method}?{query.decode()}",
            content=body if whole else _pieces(body),
            headers={"content-type": FORM_TYPE},
            timeout=60,
        )

    # (Also with the song and the credentials in the query, which Navidrome could read:
    # it answers the error before it looks at any parameter.)
    for whole, query in ((True, b""), (False, b""), (True, named), (False, named)):
        addon.clear()
        too_long = post(world.server.base_url, limit + 1, whole, query)
        direct = post(world.nd.base_url, limit + 1, whole, query)
        assert too_long.status_code == direct.status_code == 200
        assert too_long.content == direct.content  # (in XML when the form's "f" is not read)
        assert "failed" in too_long.text and "request body too large" in too_long.text
        assert len(too_long.content) < 2000
        assert addon.requests() == [] and state(world, song) == "placeholder"
    at_limit = post(world.server.base_url, limit, False)
    if method == "stream":
        assert at_limit.content == audio.read_bytes()
    else:
        assert state(world, song) == "delivered"
        assert at_limit.content == library_bytes(world, song)


def test_a_form_of_more_parameters_than_navidrome_reads_is_its_refusal(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """Navidrome reads 10,000 parameters of a form and answers one with more with an error
    before it looks at any: up to that many Shijhon intercepts, more get Navidrome's answer."""
    song, _, audio = world.placeholder_track("c-params", [addon])
    named = urlencode([*world.client().auth_params(), ("f", "json"), ("id", song)])
    pairs = len(named.split("&"))

    def post(base: str, count: int) -> httpx.Response:
        body = "&".join(["x=1"] * (count - pairs)) + "&" + named
        return httpx.post(
            f"{base}/rest/stream",
            content=body.encode(),
            headers={"content-type": FORM_TYPE},
            timeout=60,
        )

    addon.clear()
    refused, direct = post(world.server.base_url, 10_001), post(world.nd.base_url, 10_001)
    assert refused.status_code == direct.status_code == 200
    assert refused.content == direct.content and "exceeded limit" in refused.text
    assert addon.requests() == [] and state(world, song) == "placeholder"
    assert post(world.server.base_url, 10_000).content == audio.read_bytes()


def test_a_request_navidrome_refuses_as_a_whole_gets_no_work_done(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """Navidrome answers some requests with an error before it looks at any parameter - a
    Content-Type its parser refuses; a form and a query that hold, together, more
    parameters than it reads. Such a request for a placeholder's audio is Navidrome's to
    answer: nothing is fetched for it."""
    song, _, _ = world.placeholder_track("c-refused", [addon])
    credentials = urlencode([*world.client().auth_params(), ("f", "json")])
    named = f"{credentials}&id={song}"
    pairs = len(named.split("&"))
    over = "&".join(["x=1"] * (10_001 - pairs - 100)) + "&" + named  # 9,901 in the form
    for query, body, content_type in (
        ("", named, "application/x-www-form-urlencoded; charset"),
        (named, "pad=1", "application/x-www-form-urlencoded; a=1; a=2"),
        (named, "pad=1", "not a media type"),
        ("&".join(["y=1"] * 100), over, FORM_TYPE),  # 10,001 together
        # ... parameters without a name counted too.
        ("&".join(["y=1"] * 100), over.replace("x=1", "=1"), FORM_TYPE),
        # A parameter Navidrome's parser refuses, in the query or in the form.
        (f"{named}&pad=%zz", "", FORM_TYPE),
        (named, "pad=one;two", FORM_TYPE),
    ):
        addon.clear()
        answers = [
            httpx.post(
                f"{base}/rest/download?{query}",
                content=body.encode(),
                headers={"content-type": content_type},
                timeout=60,
            )
            for base in (world.nd.base_url, world.server.base_url)
        ]
        assert answers[0].status_code == answers[1].status_code == 200, content_type
        assert answers[0].content == answers[1].content, content_type
        assert "failed" in answers[1].text and len(answers[1].content) < 2000
        assert addon.requests() == [] and state(world, song) == "placeholder", content_type


def test_download_is_download_first(world: DeliveryWorld, addon: FakeAddon) -> None:
    song, _, _ = world.placeholder_track("c-download", [addon])
    response = world.client().request("download", {"id": song})
    assert state(world, song) == "delivered"
    assert response.content == library_bytes(world, song)
    assert response.content[:4] == b"fLaC" and len(response.content) > 20_000


def decide(
    client: SubsonicClient, song: str, info: dict[str, Any] | None = None, fmt: str = "json"
) -> httpx.Response:
    """getTranscodeDecision: parameters in the query, the client profile as JSON body."""
    return client.http.post(
        f"{client.base_url}/rest/getTranscodeDecision",
        params=[*client.auth_params(), ("f", fmt), ("mediaId", song), ("mediaType", "song")],
        content=json.dumps(CLIENT_INFO if info is None else info),
        headers={"content-type": "application/json"},
    )


def test_transcode_decision_and_stream(world: DeliveryWorld, addon: FakeAddon) -> None:
    # The decision goes through Shijhon: real audio is put in place first, so Navidrome
    # decides (and mints its token) for the real file, and the stream then works - for a
    # client that limits the bitrate (at original quality: the next tests).
    song, _, _ = world.placeholder_track("c-decision", [addon])
    decision = decide(world.client(), song, LIMITED)
    assert decision.json()["subsonic-response"]["status"] == "ok"
    assert state(world, song) == "delivered"
    token = _find(decision.json(), "transcodeParams")
    streamed = world.client().request(
        "getTranscodeStream", {"mediaId": song, "mediaType": "song", "transcodeParams": token}
    )
    assert streamed.status_code == 200 and len(streamed.content) > 20_000


def test_transcode_stream_with_a_token_for_the_silent_file(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    # A token to transcode the silent placeholder (e.g. minted before a cache expiry
    # reverted the song) still triggers download-first; Navidrome then calls the token stale
    # (410 Gone), which tells the client to ask for a new decision - that one plays.
    song, _, _ = world.placeholder_track("c-transcode-stream", [addon])
    stale = _find(decide(world.nd.client(), song, MP3_ONLY).json(), "transcodeParams")
    assert stale and state(world, song) == "placeholder"
    first = world.client().request(
        "getTranscodeStream", {"mediaId": song, "mediaType": "song", "transcodeParams": stale}
    )
    assert state(world, song) == "delivered"
    assert first.status_code in (200, 410)  # 200 only if both happened within one second
    fresh = _find(decide(world.client(), song).json(), "transcodeParams")
    streamed = world.client().request(
        "getTranscodeStream", {"mediaId": song, "mediaType": "song", "transcodeParams": fresh}
    )
    assert streamed.status_code == 200 and len(streamed.content) > 20_000


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_a_transcode_decision_at_original_quality_is_a_plain_stream(
    world: DeliveryWorld, addon: FakeAddon, fmt: str
) -> None:
    """A client that asks for no bitrate limit and plays the file as it is gets
    Navidrome's direct-play decision at once - no download-first, nothing fetched - and its
    stream, or a transcode stream with that decision, is a plain stream from the add-ons
    (like ``format=raw``)."""
    song, _, audio = world.placeholder_track(f"c-original-{fmt}", [addon])
    asked = len(addon.requests())
    decision = decide(world.client(), song, fmt=fmt)
    assert len(addon.requests()) == asked  # nothing fetched to decide
    assert decision.status_code == 200
    if fmt == "json":
        answer = decision.json()["subsonic-response"]["transcodeDecision"]
        assert answer["canDirectPlay"] is True
        token = answer["transcodeParams"]
    else:
        assert b'canDirectPlay="true"' in decision.content
        found = re.search(rb'transcodeParams="([^"]+)"', decision.content)
        assert found is not None
        token = found.group(1).decode()
    assert state(world, song) == "placeholder"
    assert world.client().request("stream", {"id": song}).content == audio.read_bytes()
    streamed = world.client().request(
        "getTranscodeStream",
        {"mediaId": song, "mediaType": "song", "transcodeParams": token, "offset": "0"},
    )
    assert streamed.status_code == 200 and streamed.content == audio.read_bytes()
    assert state(world, song) == "placeholder"


def test_an_original_quality_decision_the_placeholder_cannot_meet_is_download_first(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """At original quality, but Navidrome would not play the (FLAC) placeholder as it is
    for this client: the real file is put in place and decided from, as before."""
    song, _, _ = world.placeholder_track("c-original-mp3", [addon])
    decision = decide(world.client(), song, MP3_ONLY)
    assert decision.json()["subsonic-response"]["status"] == "ok"
    assert state(world, song) == "delivered"


def test_a_transcode_stream_with_another_song_s_token_is_navidrome_s_to_answer(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """A direct-play token of another song is no plain stream of this one: download-first,
    then Navidrome's own answer (410 Gone)."""
    song, _, _ = world.placeholder_track("c-original-other", [addon])
    other, _, _ = world.placeholder_track("c-original-other-2", [addon])
    token = _find(decide(world.client(), other).json(), "transcodeParams")
    streamed = world.client().request(
        "getTranscodeStream", {"mediaId": song, "mediaType": "song", "transcodeParams": token}
    )
    assert streamed.status_code == 410
    assert state(world, song) == "delivered"


def test_a_transcode_stream_with_its_parameters_in_a_form_is_a_plain_stream_too(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """Navidrome reads a form's parameters too: the decision is checked with them, and the
    add-ons' audio is served - never Navidrome's silent placeholder."""
    song, _, audio = world.placeholder_track("c-original-form", [addon])
    token = _find(decide(world.client(), song).json(), "transcodeParams")
    client = world.client()
    streamed = client.http.post(
        f"{client.base_url}/rest/getTranscodeStream",
        params=client.auth_params(),
        data={"mediaId": song, "mediaType": "song", "transcodeParams": token},
    )
    assert streamed.status_code == 200 and streamed.content == audio.read_bytes()
    assert state(world, song) == "placeholder"


def test_a_forged_or_stale_direct_play_token_is_navidrome_s_to_answer(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """A direct-play token for this song that Navidrome does not accept (forged: another
    signature, another file time) gets Navidrome's 410, not the add-ons' audio; one without
    ``mediaType``, Navidrome's 400."""
    import base64

    song, _, _ = world.placeholder_track("c-original-forged", [addon])
    token = _find(decide(world.client(), song).json(), "transcodeParams")
    header, payload, _ = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    for changed in ({**claims, "ua": claims["ua"] - 60}, claims):
        body = base64.urlsafe_b64encode(json.dumps(changed).encode()).rstrip(b"=").decode()
        forged = f"{header}.{body}.forged-signature"
        asked = len(addon.requests())
        streamed = world.client().request(
            "getTranscodeStream",
            {"mediaId": song, "mediaType": "song", "transcodeParams": forged},
        )
        assert streamed.status_code == 410
        assert len(addon.requests()) == asked and state(world, song) == "placeholder"
    missing = world.client().request(
        "getTranscodeStream", {"mediaId": song, "transcodeParams": token}
    )
    assert missing.status_code == 400  # Navidrome's own answer: mediaType is required


@pytest.mark.parametrize("missing,fmt", [("c", "json"), ("v", "json"), ("c", "xml")])
def test_a_transcode_stream_without_c_or_v_gets_navidrome_s_error(
    world: DeliveryWorld, addon: FakeAddon, missing: str, fmt: str
) -> None:
    """A direct-play token for this song, but no client name (or protocol version): the
    credential check passes, Navidrome refuses the request itself (error 10) - its answer
    is passed on as it gives it directly, never an HTTP 500 and never the add-ons' audio."""
    song, _, _ = world.placeholder_track(f"c-original-no-{missing}-{fmt}", [addon])
    token = _find(decide(world.client(), song).json(), "transcodeParams")
    asked = len(addon.requests())

    def without(client: SubsonicClient, method: str = "GET") -> httpx.Response:
        params = [(k, v) for k, v in client.auth_params() if k != missing]
        params += [("f", fmt), ("mediaId", song), ("mediaType", "song")]
        return client.http.request(
            method,
            f"{client.base_url}/rest/getTranscodeStream",
            params=[*params, ("transcodeParams", token)],
        )

    answer, direct = without(world.client()), without(world.nd.client())
    assert answer.status_code == direct.status_code == 200
    assert answer.content == direct.content
    assert answer.headers["content-type"] == direct.headers["content-type"]
    assert (b'"code":10' if fmt == "json" else b'code="10"') in answer.content
    head = without(world.client(), "HEAD")
    assert head.status_code == 200 and head.content == b""
    assert len(addon.requests()) == asked and state(world, song) == "placeholder"


def test_a_malformed_client_profile_gets_navidrome_s_answer(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    song, _, _ = world.placeholder_track("c-original-malformed", [addon])
    for profiles in (1, True, "x"):
        answer = decide(world.client(), song, {**CLIENT_INFO, "codecProfiles": profiles})
        assert answer.status_code == 200
        assert answer.json()["subsonic-response"]["status"] == "failed", profiles


def _find(value: Any, key: str) -> str:
    if isinstance(value, dict):
        if key in value and isinstance(value[key], str):
            return value[key]
        for child in value.values():
            found = _find(child, key)
            if found:
                return found
    if isinstance(value, list):
        for child in value:
            found = _find(child, key)
            if found:
                return found
    return ""


def test_jukebox_is_download_first(world: DeliveryWorld, addon: FakeAddon) -> None:
    song, _, _ = world.placeholder_track("c-jukebox", [addon])
    assert world.client().ok("jukeboxControl", {"action": "set", "id": song})
    assert state(world, song) == "delivered"
    status = world.client().ok("jukeboxControl", {"action": "get"})["jukeboxPlaylist"]
    assert [e["id"] for e in status["entry"]] == [song]


def test_only_a_set_jukebox_queue_s_first_song_is_the_one_being_played(
    world: DeliveryWorld, addon: FakeAddon, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the add-ons' limits the jukebox's upcoming songs are queued downloads, like
    an offline sync's - after every listener's play; only the first song of a queue that is
    set is the song being played. None of them waits for the user's download turns or the
    hour's allowance: Navidrome gets the request once they are all in place."""
    songs = [world.placeholder_track(f"c-jukebox-{n}", [addon])[0] for n in range(4)]
    download_first = world.services.download_first
    real = download_first._download
    asked: list[tuple[str, int]] = []

    async def noted(row: Any, **kwargs: Any) -> str | None:
        asked.append((row["song_id"], kwargs["urgency"]))
        return await real(row, **kwargs)

    monkeypatch.setattr(download_first, "_download", noted)
    limits = download_first.limits
    assert limits is not None
    monkeypatch.setattr(limits, "burst", 1)  # the hour's allowance: one in a row, then paced
    limits._users.clear()
    client = world.client()
    began = time.monotonic()
    assert client.ok("jukeboxControl", {"action": "set", "id": songs[:3]})
    assert time.monotonic() - began < 20  # (not paced: one every 30 s would be a minute)
    assert asked == [
        (songs[0], pacing.PLAY),
        (songs[1], pacing.QUEUED),
        (songs[2], pacing.QUEUED),
    ]
    assert all(state(world, song) == "delivered" for song in songs[:3])
    asked.clear()
    assert client.ok("jukeboxControl", {"action": "add", "id": songs[3]})
    assert asked == [(songs[3], pacing.QUEUED)]  # added behind what plays
    limits._users.clear()
    status = client.ok("jukeboxControl", {"action": "get"})["jukeboxPlaylist"]
    assert [e["id"] for e in status["entry"]] == songs


def test_share_links_are_download_first(world: DeliveryWorld, addon: FakeAddon) -> None:
    song, _, _ = world.placeholder_track("c-share", [addon])
    share = world.client().ok("createShare", {"id": song})["shares"]["share"][0]
    page = httpx.get(world.server.base_url + "/share/" + share["id"])
    info = re.search(r"__SHARE_INFO__\s*=\s*(\".*?\")\s*</script>", page.text, re.S)
    assert info is not None
    token = json.loads(json.loads(info.group(1)))["tracks"][0]["id"]
    assert state(world, song) == "placeholder"  # viewing the page does no work
    audio = httpx.get(f"{world.server.base_url}/share/s/{token}")
    assert state(world, song) == "delivered"
    assert audio.content == library_bytes(world, song)
    bad = httpx.get(f"{world.server.base_url}/share/s/{token[:-4]}AAAA")
    assert bad.status_code != 200 or bad.content != audio.content


def test_album_zip_with_placeholders_is_refused(world: DeliveryWorld, addon: FakeAddon) -> None:
    song, _, _ = world.placeholder_track("c-zip", [addon])
    album = world.client().ok("getSong", {"id": song})["song"]["albumId"]
    response = world.client().request("download", {"id": album})
    assert response.json()["subsonic-response"]["status"] == "failed"
    assert state(world, song) == "placeholder"


def test_plain_requests_are_forwarded_without_a_credential_check(world: DeliveryWorld) -> None:
    owned = world.client().ok("search3", {"query": "Owned Record"})["searchResult3"]["song"][0][
        "id"
    ]
    pings = world.app.checker.pings
    assert world.client().request("stream", {"id": owned}).content[:4] == b"fLaC"
    world.client().ok("getAlbumList2", {"type": "newest"})
    assert world.app.checker.pings == pings
    # A download of an unknown ID may be an archive (album, artist, playlist); inspecting it
    # is work, so it is checked first - and still served.
    assert world.client().request("download", {"id": owned}).content[:4] == b"fLaC"


# --- suite B: wrong credentials on placeholder paths do no work ------------------------


@pytest.mark.parametrize(
    "method,params",
    [
        ("stream", {"id": "{song}"}),
        ("stream", {"id": "{song}", "maxBitRate": "96", "format": "mp3"}),
        ("download", {"id": "{song}"}),
        ("getTranscodeStream", {"mediaId": "{song}", "mediaType": "song", "transcodeParams": "x"}),
        ("jukeboxControl", {"action": "set", "id": "{song}"}),
    ],
)
@pytest.mark.parametrize("password", ["wrong", ""])
def test_wrong_credentials_do_no_work(
    world: DeliveryWorld, addon: FakeAddon, method: str, params: dict[str, str], password: str
) -> None:
    key = f"c-noauth-{method}-{len(params)}-{password or 'none'}"
    song, _, _ = world.placeholder_track(key, [addon])
    addon.clear()
    scans = world.services.scans.scans_started
    client = SubsonicClient(
        world.server.base_url, ADMIN_USER, password or ADMIN_PASSWORD, auth="token"
    )
    if not password:
        client.auth = "none"
    response = client.request(method, {k: v.format(song=song) for k, v in params.items()})
    body = response.json()["subsonic-response"]
    assert body["status"] == "failed" and body["error"]["code"] in (10, 40)
    assert body["type"] == "navidrome"  # Navidrome's own error
    assert addon.requests() == []
    assert world.services.scans.scans_started == scans
    assert state(world, song) == "placeholder"


def test_a_shared_album_s_archive_is_navidromes_own_and_holds_no_silence(
    world: DeliveryWorld, addon: FakeAddon
) -> None:
    """``/share/d/<id>``: an owned album's archive is Navidrome's answer as it is; a
    catalog album's silent placeholders are fetched first (download-first), so the
    archive holds their audio (it is forwarded by Shijhon itself, with no new
    placeholders moving into the library meanwhile)."""

    def shared(album: str) -> str:
        made = world.nd.native(
            "POST",
            "share",
            json={"resourceIds": album, "resourceType": "album", "downloadable": True},
        )
        return str(made.json()["id"])

    albums = world.nd.client().ok("getAlbumList2", {"type": "alphabeticalByName", "size": 500})
    owned = next(a["id"] for a in albums["albumList2"]["album"] if a["name"] == "Owned Record")
    link = f"/share/d/{shared(owned)}"
    direct = httpx.get(world.nd.base_url + link)
    through = httpx.get(world.server.base_url + link)
    assert through.status_code == direct.status_code == 200
    assert through.content.startswith(b"PK") and len(through.content) == len(direct.content)
    for name in ("content-type", "content-disposition"):
        assert through.headers.get(name) == direct.headers.get(name)
    song, _, audio = world.placeholder_track("c-share-archive", [addon])
    album = world.nd.client().ok("getSong", {"id": song})["song"]["albumId"]
    archive = httpx.get(f"{world.server.base_url}/share/d/{shared(album)}", timeout=60)
    assert archive.status_code == 200 and archive.content.startswith(b"PK")
    assert state(world, song) == "delivered"
    assert len(archive.content) > len(audio.read_bytes()) // 2  # the audio, not 1 KB of silence


def test_share_with_bad_token_does_no_work(world: DeliveryWorld, addon: FakeAddon) -> None:
    song, _, _ = world.placeholder_track("c-badshare", [addon])
    # A forged token carrying the placeholder's ID: Navidrome rejects it, Shijhon does no work.
    forged_payload = json.dumps({"id": song, "iss": "ND"}).encode()
    import base64

    token = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        + base64.urlsafe_b64encode(forged_payload).decode().rstrip("=")
        + ".c2lnbmF0dXJl"
    )
    addon.clear()
    response = httpx.get(f"{world.server.base_url}/share/s/{token}")
    assert response.status_code != 200 or not response.content.startswith(b"fLaC")
    assert addon.requests() == []
    assert state(world, song) == "placeholder"


# --- regressions ------------------------------------------------------------------------


def _share_token(world: DeliveryWorld, song: str) -> str:
    share = world.client().ok("createShare", {"id": song})["shares"]["share"][0]
    page = httpx.get(world.server.base_url + "/share/" + share["id"])
    info = re.search(r"__SHARE_INFO__\s*=\s*(\".*?\")\s*</script>", page.text, re.S)
    assert info is not None
    return str(json.loads(json.loads(info.group(1)))["tracks"][0]["id"])


def test_share_link_played_by_a_browser_with_range(world: DeliveryWorld, addon: FakeAddon) -> None:
    song, _, _ = world.placeholder_track("c-share-range", [addon])
    token = _share_token(world, song)
    audio = httpx.get(f"{world.server.base_url}/share/s/{token}", headers={"range": "bytes=0-"})
    assert state(world, song) == "delivered"
    assert audio.status_code == 206 and audio.content == library_bytes(world, song)


def test_artist_zip_with_placeholders_is_refused(world: DeliveryWorld, addon: FakeAddon) -> None:
    song, _, _ = world.placeholder_track("c-artist-zip", [addon])
    artist = world.client().ok("getSong", {"id": song})["song"]["artistId"]
    response = world.client().request("download", {"id": artist})
    assert response.json()["subsonic-response"]["status"] == "failed"
    assert state(world, song) == "placeholder"


def test_unauthenticated_downloads_of_odd_ids_do_nothing(world: DeliveryWorld) -> None:
    bad = SubsonicClient(world.server.base_url, ADMIN_USER, "wrong")
    for ident in ("../song", "a/b", "%2e%2e"):
        body = bad.request("download", {"id": ident}).json()["subsonic-response"]
        assert body["status"] == "failed" and body["error"]["code"] == 40
