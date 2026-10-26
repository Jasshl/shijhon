"""Suite K — each listener's minimum audio quality (``delivery/quality_gate.py``).

A song whose file is below the listener's minimum plays from the add-ons under its own ID
when they have the recording in a quality not known to be lower - an add-on whose audio is
below it too is passed over for the next one; otherwise its file plays. So it does through
the transcoding extension, when the app asks at original quality for audio it plays as it
is. A file below someone's minimum does not play for a placeholder they play. Accounts
that get real audio only always get the file. Invented music throughout.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from shijhon.quality import DEFAULT, Minimum
from tests.conftest import NavidromeFactory
from tests.harness.dashboard import Browser
from tests.harness.delivery import DeliveryWorld, delivery_world
from tests.harness.engine import catalog_release
from tests.harness.fake_addon import FakeAddon, FakeTrack
from tests.harness.library import Album, Track, write_album
from tests.harness.navidrome import ADMIN_USER
from tests.harness.subsonic import SubsonicClient

LISTENER = ("quiet-listener", "invented-password-q")  # chooses any quality
ANALYZER = ("sound-analyzer", "invented-password-a")  # gets real audio only
# OpenSubsonic client profiles (getTranscodeDecision): one that plays MP3 and FLAC as they
# are, at original quality; the same with a bitrate limit; one that plays only MP3.
PLAYS_ALL: dict[str, Any] = {
    "name": "test",
    "platform": "test",
    "directPlayProfiles": [
        {"containers": ["mp3"], "audioCodecs": ["mp3"], "protocols": ["http"]},
        {"containers": ["flac"], "audioCodecs": ["flac"], "protocols": ["http"]},
    ],
    "transcodingProfiles": [{"container": "mp3", "audioCodec": "mp3", "protocol": "http"}],
}
LIMITED = {**PLAYS_ALL, "maxAudioBitrate": 320_000}
MP3_ONLY = {**PLAYS_ALL, "directPlayProfiles": PLAYS_ALL["directPlayProfiles"][:1]}


@dataclass
class Gated:
    world: DeliveryWorld
    addon: FakeAddon
    second: FakeAddon  # the next add-on in the routing's order
    songs: dict[str, str]  # title -> native song ID
    better: bytes  # the add-ons' FLAC of "Low Ember"
    placeholder: str  # a catalog song backed by "Low Ember"
    placeholder_audio: bytes  # the add-ons' FLAC of that catalog song
    fetched: str  # a catalog song backed by "Worn Ember", which the add-ons have in FLAC
    unfound: str  # a catalog song backed by "Lone Ember", which no add-on has
    late: str  # a catalog song backed by "Late Ember", which no add-on has (yet)
    late_isrc: str
    isrcs: dict[str, str]  # title -> ISRC
    seconds: dict[str, bytes] = field(default_factory=dict)  # title -> the second's FLAC


# Owned 128 kbit/s MP3s the first add-on has so too, and the second in FLAC.
ISRCS = {
    "Pale Ember": "XXQ0L2000005",
    "Dim Ember": "XXQ0L2000006",
    "Faint Ember": "XXQ0L2000007",
    "Hazy Ember": "XXQ0L2000008",
}


@pytest.fixture(scope="module")
def gated(
    navidrome_factory: NavidromeFactory, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Gated]:
    nd = navidrome_factory()
    for album in (
        Album(
            "Quiet Hollow",
            "Ember Road",
            (Track("Low Ember", 1, seconds=4, isrc="XXQ0L2000001"),),
            "mp3",
        ),
        Album(
            "Quiet Hollow", "Bright Road", (Track("High Ember", 1, seconds=4, isrc="XXQ0L2000002"),)
        ),
        Album(
            "Quiet Hollow",
            "Lone Road",
            (Track("Lone Ember", 1, seconds=4, isrc="XXQ0L2000003"),),
            "mp3",
        ),
        Album(
            "Quiet Hollow",
            "Worn Road",
            (Track("Worn Ember", 1, seconds=4, isrc="XXQ0L2000004"),),
            "mp3",
        ),
        *(
            Album("Quiet Hollow", f"{title} Road", (Track(title, 1, seconds=4, isrc=isrc),), "mp3")
            for title, isrc in {**ISRCS, "Late Ember": "XXQ0L2000009"}.items()
        ),
    ):
        write_album(nd.music, album)
    nd.scan(full=True)
    songs = {
        s["title"]: s["id"]
        for s in nd.client().ok("search3", {"query": "Ember", "songCount": 20})["searchResult3"][
            "song"
        ]
    }
    listener_id = nd.create_user(*LISTENER)
    nd.create_user(*ANALYZER)
    tmp = tmp_path_factory.mktemp("quality")
    with delivery_world(nd, tmp, quality_default="192", real_audio_accounts=[ANALYZER[0]]) as world:
        addon = world.addon("Source")
        world.add_source(addon)
        second = world.addon("Second")
        world.add_source(second)
        better = world.audio("better-ember", "flac", 4)
        addon.add(FakeTrack(isrc="XXQ0L2000001", audio=better))
        worse = world.audio("worse-ember", "mp3", 4)
        # Both add-ons have "Worn Ember" as a 128 kbit/s MP3 only; the others the first has
        # so, the second in FLAC.
        for isrc in ("XXQ0L2000004", *ISRCS.values()):
            addon.add(FakeTrack(isrc=isrc, audio=worse, content_type="audio/mpeg"))
        second.add(FakeTrack(isrc="XXQ0L2000004", audio=worse, content_type="audio/mpeg"))
        seconds = {}
        for title, isrc in ISRCS.items():
            flac = world.audio(f"second-{title.split()[0].lower()}", "flac", 4)
            second.add(FakeTrack(isrc=isrc, audio=flac))
            seconds[title] = flac.read_bytes()
        # A catalog release whose songs are played by owned files below the minimum.
        release = catalog_release("q-album", "Ember Album", "Quiet Hollow", 4, seconds=4)
        created = world.materialize(release).created
        placeholder, fetched, unfound, late = (created[t.ref] for t in release.tracks[:4])
        engine = world.services.engine
        for song, owned in (
            (placeholder, "Low Ember"),
            (fetched, "Worn Ember"),
            (unfound, "Lone Ember"),
            (late, "Late Ember"),
        ):
            world.server.call(lambda song=song, owned=owned: engine.set_backing(song, songs[owned]))
        catalog_audio = world.audio("catalog-ember", "flac", 4)
        for track, audio in ((release.tracks[0], catalog_audio), (release.tracks[1], better)):
            assert track.isrc is not None
            addon.add(FakeTrack(isrc=track.isrc, audio=audio))
        minimums = world.services.quality
        assert minimums is not None
        world.server.call(lambda: minimums.set(listener_id, "any"))
        late_isrc = release.tracks[3].isrc
        assert late_isrc is not None
        yield Gated(
            world,
            addon,
            second,
            songs,
            better.read_bytes(),
            placeholder,
            catalog_audio.read_bytes(),
            fetched,
            unfound,
            late,
            late_isrc,
            ISRCS,
            seconds,
        )


def file_of(gated: Gated, title: str) -> bytes:
    """The song's own file, as Navidrome serves it directly."""
    return gated.world.nd.client().request("stream", {"id": gated.songs[title]}).content


def streams(addon: FakeAddon) -> int:
    """The add-on's lookups, links and audio requests so far."""
    return sum(len(addon.requests(e)) for e in ("resolve-isrc", "resolve", "stream", "audio"))


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def asked(addon: FakeAddon, isrc: str) -> int:
    """The add-on's audio requests for the recording so far."""
    return sum(1 for r in addon.requests("audio") if f"/{isrc}/" in r["path"])


def decide(client: SubsonicClient, song: str, info: dict[str, Any], fmt: str = "json") -> Any:
    """getTranscodeDecision: parameters in the query, the client profile as JSON body."""
    return client.http.post(
        f"{client.base_url}/rest/getTranscodeDecision",
        params=[*client.auth_params(), ("f", fmt), ("mediaId", song), ("mediaType", "song")],
        content=json.dumps(info),
        headers={"content-type": "application/json"},
    )


def decision(answer: httpx.Response) -> tuple[bool, str]:
    """A decision's direct play, and its token (JSON or XML)."""
    if answer.headers["content-type"].startswith("application/json"):
        found = answer.json()["subsonic-response"]["transcodeDecision"]
        return found["canDirectPlay"], found.get("transcodeParams", "")
    token = re.search(rb'transcodeParams="([^"]+)"', answer.content)
    return b'canDirectPlay="true"' in answer.content, token.group(1).decode() if token else ""


def without_token(body: bytes) -> bytes:
    """A decision's answer but its token (signed anew for each answer)."""
    return re.sub(rb'(transcodeParams"?\s*[:=]\s*")[^"]*"', rb'\1"', body)


def transcoded(client: SubsonicClient, song: str, token: str, **headers: str) -> httpx.Response:
    return client.request(
        "getTranscodeStream",
        {"mediaId": song, "mediaType": "song", "transcodeParams": token},
        headers=headers,
    )


def routing(gated: Gated, mode: str, primary: str = "") -> None:
    settings = gated.world.services.deliverer.settings

    async def change() -> None:
        settings.routing, settings.primary_source = mode, primary  # type: ignore[assignment]

    gated.world.server.call(change)


def test_a_file_below_the_minimum_plays_from_the_addons(gated: Gated) -> None:
    client = gated.world.client()  # the administrator: the default, 192 kbit/s
    answer = client.request("stream", {"id": gated.songs["Low Ember"]})
    assert answer.status_code == 200
    assert digest(answer.content) == digest(gated.better)
    assert answer.content[:4] == b"fLaC"
    # A later range of that play stays on the add-ons' file.
    part = client.request(
        "stream", {"id": gated.songs["Low Ember"]}, headers={"range": "bytes=10-99"}
    )
    assert part.status_code == 206 and part.content == gated.better[10:100]
    # A download (an app's offline copy) is the add-ons' audio too.
    download = client.request("download", {"id": gated.songs["Low Ember"]})
    assert digest(download.content) == digest(gated.better)


def test_a_listener_with_a_lower_minimum_gets_the_file(gated: Gated) -> None:
    before = streams(gated.addon)
    listener = SubsonicClient(gated.world.server.base_url, *LISTENER)
    try:
        answer = listener.request("stream", {"id": gated.songs["Low Ember"]})
    finally:
        listener.close()
    assert digest(answer.content) == digest(file_of(gated, "Low Ember"))
    assert answer.content[:4] != b"fLaC"
    assert streams(gated.addon) == before


def test_a_file_meeting_the_minimum_plays_with_nothing_asked(gated: Gated) -> None:
    before = streams(gated.addon)
    answer = gated.world.client().request("stream", {"id": gated.songs["High Ember"]})
    assert digest(answer.content) == digest(file_of(gated, "High Ember"))
    assert streams(gated.addon) == before


def test_the_file_plays_when_the_addons_have_nothing_better(gated: Gated) -> None:
    client = gated.world.client()
    # No add-on has "Lone Ember".
    lone = client.request("stream", {"id": gated.songs["Lone Ember"]})
    assert lone.status_code == 200
    assert digest(lone.content) == digest(file_of(gated, "Lone Ember"))
    # Each add-on's "Worn Ember" is a 128 kbit/s MP3 too: below the minimum - the first's
    # is passed over for the second's, then the file plays.
    worn = client.request("stream", {"id": gated.songs["Worn Ember"]})
    assert worn.status_code == 200
    assert digest(worn.content) == digest(file_of(gated, "Worn Ember"))
    assert asked(gated.addon, "XXQ0L2000004") and asked(gated.second, "XXQ0L2000004")


@pytest.mark.parametrize("mode,title", [("ordered", "Pale Ember"), ("primary_first", "Dim Ember")])
def test_audio_below_the_minimum_is_passed_over_for_the_next_addon(
    gated: Gated, mode: str, title: str
) -> None:
    """The first add-on's MP3 is below the minimum: the routing goes on to the second,
    whose FLAC plays - also its later ranges."""
    routing(gated, mode, "Source" if mode == "primary_first" else "")
    try:
        client = gated.world.client()
        song, isrc = gated.songs[title], gated.isrcs[title]
        answer = client.request("stream", {"id": song})
        assert answer.status_code == 200
        assert digest(answer.content) == digest(gated.seconds[title])
        assert asked(gated.addon, isrc) == 1  # asked, and passed over
        part = client.request("stream", {"id": song}, headers={"range": "bytes=10-99"})
        assert part.status_code == 206 and part.content == gated.seconds[title][10:100]
        assert asked(gated.addon, isrc) == 1
    finally:
        routing(gated, "ordered")


def test_only_a_play_the_addons_served_may_upgrade_an_import(gated: Gated) -> None:
    """After a play from the start that the add-ons served, a person's own imported song
    may be upgraded (``upgrade_after``) - and the upgrade's fetch passes audio below their
    minimum over as the play did."""
    calls: list[tuple[str, str]] = []
    interceptor = gated.world.services.interceptor
    interceptor.upgrade_after = lambda name, song: calls.append((name, song.song_id))
    try:
        client = gated.world.client()
        faint = gated.songs["Faint Ember"]
        assert digest(client.request("stream", {"id": faint}).content) == digest(
            gated.seconds["Faint Ember"]
        )
        client.request("stream", {"id": faint}, headers={"range": "bytes=10-99"})
        client.request("stream", {"id": faint}, http_method="HEAD")
        client.request("stream", {"id": gated.songs["Worn Ember"]})  # the file
        client.request("stream", {"id": gated.songs["High Ember"]})  # meets the minimum
        client.request("download", {"id": faint})  # no play
        assert calls == [(ADMIN_USER, faint)]
        # A transcode stream whose decision is direct play is a play too.
        token = decision(decide(client, faint, PLAYS_ALL))[1]
        assert digest(transcoded(client, faint, token).content) == digest(
            gated.seconds["Faint Ember"]
        )
        assert calls == [(ADMIN_USER, faint)] * 2
        # A format the add-ons' audio is in already: served as it is, a play too; one it
        # needs converting for: the file, converted by Navidrome.
        flac = client.request("stream", {"id": faint, "format": "flac"})
        assert digest(flac.content) == digest(gated.seconds["Faint Ember"])
        converted = client.request("stream", {"id": faint, "format": "mp3", "maxBitRate": "96"})
        assert converted.status_code == 200 and converted.content[:4] != b"fLaC"
        assert calls == [(ADMIN_USER, faint)] * 3
    finally:
        interceptor.upgrade_after = None

    services = gated.world.services
    hazy = gated.songs["Hazy Ember"]

    async def fetched(minimum: Minimum | None) -> bytes:
        assert services.interceptor.quality is not None
        song = await services.interceptor.quality.song(hazy)
        assert song is not None
        services.deliverer.forget(hazy)  # routed anew: the first add-on first
        path = await services.download_first.fetch(song.track(), minimum)
        assert not isinstance(path, str), path
        try:
            return path.read_bytes()
        finally:
            path.unlink()

    assert gated.world.server.call(lambda: fetched(DEFAULT))[:4] == b"fLaC"
    assert gated.world.server.call(lambda: fetched(None))[:4] != b"fLaC"  # the first's MP3


@pytest.mark.parametrize("fmt", ["json", "xml"])
def test_a_transcode_stream_at_original_quality_plays_from_the_addons(
    gated: Gated, fmt: str
) -> None:
    """Navidrome decides from the file (its answer passed on as it is); its direct play,
    at original quality, of a file below the minimum plays the add-ons' audio the app
    plays as it is - its later ranges too."""
    client, song = gated.world.client(), gated.songs["Low Ember"]
    answer = decide(client, song, PLAYS_ALL, fmt)
    native = decide(gated.world.nd.client(), song, PLAYS_ALL, fmt)
    assert answer.status_code == native.status_code == 200
    assert without_token(answer.content) == without_token(native.content)
    direct, token = decision(answer)
    assert direct and token
    streamed = transcoded(client, song, token)
    assert streamed.status_code == 200
    assert digest(streamed.content) == digest(gated.better)
    part = transcoded(client, song, token, range="bytes=10-99")
    assert part.status_code == 206 and part.content == gated.better[10:100]


def test_a_transcode_streams_later_ranges_go_on_only_with_an_accepted_decision(
    gated: Gated,
) -> None:
    """A later range of a transcode stream played from the add-ons goes on there with the
    decision it began with, and with another decision Navidrome accepts for the same
    client profile - never with a token Navidrome refuses (its own answer), nor with
    another decision (a bitrate limit: the file)."""
    client, song = gated.world.client(), gated.songs["Low Ember"]
    _, token = decision(decide(client, song, PLAYS_ALL))
    assert digest(transcoded(client, song, token).content) == digest(gated.better)
    forged = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    refused = transcoded(client, song, forged, range="bytes=10-99")
    native = transcoded(gated.world.nd.client(), song, forged, range="bytes=10-99")
    assert refused.status_code == native.status_code != 206
    assert refused.content != gated.better[10:100]
    part = transcoded(client, song, token, range="bytes=10-99")
    assert part.status_code == 206 and part.content == gated.better[10:100]
    time.sleep(1.1)  # (a decision's token is signed anew each second)
    _, limited = decision(decide(client, song, LIMITED))
    assert limited != token
    part = transcoded(client, song, limited, range="bytes=10-99")
    assert part.status_code == 206 and part.content == file_of(gated, "Low Ember")[10:100]
    time.sleep(1.1)
    _, again = decision(decide(client, song, PLAYS_ALL))
    assert again not in (token, limited)
    part = transcoded(client, song, again, range="bytes=100-199")
    assert part.status_code == 206 and part.content == gated.better[100:200]


def started_then_decided(gated: Gated, client: SubsonicClient, song: str, then: Any) -> str:
    """A transcode stream's play begun from the add-ons with a decision at original quality,
    then the client's next decision (``then``: its profile; None: none) - answered with the
    same token, signed in the same second. Returns that token."""
    for _ in range(10):
        time.sleep(1.02 - time.time() % 1)  # (at the start of a second)
        _, token = decision(decide(client, song, PLAYS_ALL))
        begun = transcoded(client, song, token, range="bytes=0-99")
        assert begun.status_code == 206 and begun.content == gated.better[:100]
        if then is None:
            return token
        _, again = decision(decide(client, song, then))
        if again == token:
            return token
    pytest.fail("no two decisions in the same second")


@pytest.mark.parametrize("then", ["another profile", "a bitrate limit", "forgotten"])
def test_a_transcode_streams_later_decisions_decide_only_its_next_plays(
    gated: Gated, then: str
) -> None:
    """A decision after a transcode stream's play began from the add-ons - of another app
    profile or with a bitrate limit, answered with the play's own token (signed in the same
    second), or the play's decision forgotten among later ones - decides the client's next
    plays only: the play's later ranges stay on the add-ons' audio, and a new play from
    byte zero with that token is the file."""
    client, song = gated.world.client(), gated.songs["Low Ember"]
    profile = {"another profile": MP3_ONLY, "a bitrate limit": LIMITED, "forgotten": None}[then]
    token = started_then_decided(gated, client, song, profile)
    if then == "forgotten":
        interceptor = gated.world.services.interceptor

        async def forget() -> None:
            interceptor._direct.clear()

        gated.world.server.call(forget)
    part = transcoded(client, song, token, range="bytes=10-99")
    assert part.status_code == 206 and part.content == gated.better[10:100]
    part = transcoded(client, song, token, range="bytes=100-199")
    assert part.status_code == 206 and part.content == gated.better[100:200]
    again = transcoded(client, song, token)
    assert again.status_code == 200
    assert digest(again.content) == digest(file_of(gated, "Low Ember"))


def test_a_play_from_the_addons_keeps_their_audio_after_the_last_minimum_goes(
    gated: Gated,
) -> None:
    """The installation's default set to "any" while no one chose another: no one has a
    minimum. A play that began at the add-ons keeps their audio for its later ranges - a
    stream's and a transcode stream's -; the next play from byte zero is the file."""
    client, song = gated.world.client(), gated.songs["Low Ember"]
    services = gated.world.services
    minimums = services.quality
    assert minimums is not None
    assert digest(client.request("stream", {"id": song}).content) == digest(gated.better)
    token = started_then_decided(gated, client, song, None)

    async def default(value: str) -> bool:
        minimums.default = value
        return await minimums.anyone()

    assert not gated.world.server.call(lambda: default("any"))
    try:
        part = client.request("stream", {"id": song}, headers={"range": "bytes=10-99"})
        assert part.status_code == 206 and part.content == gated.better[10:100]
        part = transcoded(client, song, token, range="bytes=10-99")
        assert part.status_code == 206 and part.content == gated.better[10:100]
        before = streams(gated.addon) + streams(gated.second)
        file = file_of(gated, "Low Ember")
        again = client.request("stream", {"id": song})
        assert digest(again.content) == digest(file)
        part = client.request("stream", {"id": song}, headers={"range": "bytes=10-99"})
        assert part.status_code == 206 and part.content == file[10:100]
        assert digest(transcoded(client, song, token).content) == digest(file)
        assert streams(gated.addon) + streams(gated.second) == before
    finally:
        gated.world.server.call(lambda: default("192"))


def test_a_transcode_stream_the_addons_cannot_serve_as_asked_plays_the_file(
    gated: Gated,
) -> None:
    """The file plays for a decision with a bitrate limit (Navidrome's direct play of the
    file, nothing asked of the add-ons), and for one whose app does not play the add-ons'
    audio as it is (FLAC for an app that plays only MP3)."""
    client, song = gated.world.client(), gated.songs["Low Ember"]
    before = streams(gated.addon) + streams(gated.second)
    direct, token = decision(decide(client, song, LIMITED))
    assert direct  # (the 128 kbit/s file is within the limit)
    assert digest(transcoded(client, song, token).content) == digest(file_of(gated, "Low Ember"))
    assert streams(gated.addon) + streams(gated.second) == before
    direct, token = decision(decide(client, song, MP3_ONLY))
    assert direct
    streamed = transcoded(client, song, token)
    assert streamed.status_code == 200
    assert digest(streamed.content) == digest(file_of(gated, "Low Ember"))


def test_a_transcode_stream_of_a_file_meeting_the_minimum_is_navidromes(gated: Gated) -> None:
    before = streams(gated.addon) + streams(gated.second)
    for (user, password), title in (
        (("admin", ""), "High Ember"),  # meets the minimum
        (LISTENER, "Low Ember"),  # any quality
    ):
        client = (
            gated.world.client()
            if user == "admin"
            else SubsonicClient(gated.world.server.base_url, user, password)
        )
        try:
            song = gated.songs[title]
            answer = decide(client, song, PLAYS_ALL)
            direct, token = decision(answer)
            assert direct
            assert without_token(answer.content) == without_token(
                decide(gated.world.nd.client(), song, PLAYS_ALL).content
            )
            streamed = transcoded(client, song, token)
            assert digest(streamed.content) == digest(file_of(gated, title))
        finally:
            client.close()
    assert streams(gated.addon) + streams(gated.second) == before


def test_a_catalog_song_whose_downloaded_audio_is_below_the_minimum(gated: Gated) -> None:
    """A catalog song downloaded when no add-on had it holds a copy of the owned file it was
    played by; once an add-on has it, its transcode stream at original quality plays the
    add-on's audio."""
    client = gated.world.client()
    download = client.request("download", {"id": gated.late})
    assert download.status_code == 200 and download.content[:4] != b"fLaC"
    flac = gated.world.audio("late-ember", "flac", 4)
    gated.second.add(FakeTrack(isrc=gated.late_isrc, audio=flac))
    deliverer = gated.world.services.deliverer

    async def forget_failures() -> None:  # (a retry within a minute skips them)
        deliverer._failed_at.clear()
        deliverer._failed.clear()
        deliverer._leftovers.clear()

    gated.world.server.call(forget_failures)
    direct, token = decision(decide(client, gated.late, PLAYS_ALL))
    assert direct
    streamed = transcoded(client, gated.late, token)
    assert streamed.status_code == 200
    assert digest(streamed.content) == digest(flac.read_bytes())


def test_a_file_below_the_minimum_does_not_stand_in_for_a_catalog_song(gated: Gated) -> None:
    # The administrator (192): the add-ons' audio of the catalog song, not "Low Ember".
    answer = gated.world.client().request("stream", {"id": gated.placeholder})
    assert digest(answer.content) == digest(gated.placeholder_audio)
    # The listener (any quality): the owned file plays for it, as before.
    listener = SubsonicClient(gated.world.server.base_url, *LISTENER)
    try:
        theirs = listener.request("stream", {"id": gated.placeholder})
    finally:
        listener.close()
    assert digest(theirs.content) == digest(file_of(gated, "Low Ember"))


def test_an_account_that_gets_real_audio_only_gets_the_file(gated: Gated) -> None:
    before = streams(gated.addon)
    analyzer = SubsonicClient(gated.world.server.base_url, *ANALYZER)
    try:
        answer = analyzer.request("stream", {"id": gated.songs["Low Ember"]})
    finally:
        analyzer.close()
    assert digest(answer.content) == digest(file_of(gated, "Low Ember"))
    assert streams(gated.addon) == before


def test_each_person_chooses_on_the_audio_quality_page(gated: Gated) -> None:
    browser = Browser(gated.world.server.base_url)
    try:
        browser.sign_in(*LISTENER)
        page = browser.get("quality")
        assert page.status_code == 200
        assert "lossless audio (FLAC, ALAC…) is accepted at any bitrate" in page.text
        assert "Your choice: Any quality." in page.text
        browser.submit("quality", {"minimum": "320"})
        assert "Lowest quality saved." in browser.get("quality").text
        listener = SubsonicClient(gated.world.server.base_url, *LISTENER)
        try:
            # Now the listener's minimum is above the file: the add-ons' FLAC.
            answer = listener.request("stream", {"id": gated.songs["Low Ember"]})
            assert digest(answer.content) == digest(gated.better)
        finally:
            listener.close()
        browser.submit("quality", {"minimum": "any"})
        refused = browser.post("quality", {"minimum": "320", "csrf": "forged"})
        assert refused.status_code == 403
        assert "Your choice: Any quality." in browser.get("quality").text
    finally:
        browser.close()


def test_downloads_of_catalog_songs_take_the_addons_audio_over_a_low_file(gated: Gated) -> None:
    """A download puts the song's audio in the library (download-first): the add-ons' when
    the owned file that plays for it is below the listener's minimum, and that file only
    when the add-ons have nothing."""
    client = gated.world.client()
    fetched = client.request("download", {"id": gated.fetched})
    assert fetched.status_code == 200 and fetched.content[:4] == b"fLaC"
    unfound = client.request("download", {"id": gated.unfound})
    assert unfound.status_code == 200 and unfound.content[:4] != b"fLaC"

    async def states() -> dict[str, str]:
        rows = await gated.world.services.store.fetchall(
            "SELECT song_id, state FROM placeholders WHERE song_id IN (?, ?)",
            [gated.fetched, gated.unfound],
        )
        return {row["song_id"]: row["state"] for row in rows}

    assert gated.world.server.call(states) == {
        gated.fetched: "delivered",
        gated.unfound: "delivered",
    }
