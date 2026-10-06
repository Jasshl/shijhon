"""Transcode decisions at original quality: which client profiles ask for the file as
it is, Navidrome's answer read as direct play (JSON and XML), and a transcode stream's
decision read from its token."""

from __future__ import annotations

import base64
import json
import time
from typing import Any

from shijhon.delivery.intercept import (
    _direct_play_decision,
    converted,
    direct_play,
    original_quality,
)
from shijhon.proxy.params import RestCall


def decision(body: Any) -> RestCall:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return RestCall.build(
        "getTranscodeDecision",
        "POST",
        b"/rest/getTranscodeDecision",
        b"mediaId=a&mediaType=song",
        [(b"content-type", b"application/json")],
        raw,
    )


def test_no_bitrate_limit_and_no_required_limitation_is_original_quality() -> None:
    profiles = {"directPlayProfiles": [{"containers": ["flac"], "protocols": ["http"]}]}
    assert original_quality(decision({"name": "x", "platform": "y", **profiles}))
    assert original_quality(decision({"name": "x", "maxAudioBitrate": 0, **profiles}))
    assert original_quality(
        decision(
            {
                "codecProfiles": [
                    {"type": "AudioCodec", "name": "flac", "limitations": [
                        {"name": "audioSamplerate", "comparison": "LessThanEqual",
                         "values": ["48000"], "required": False},
                    ]},
                ]
            }
        )
    )  # fmt: skip


def test_a_limit_or_an_unreadable_profile_is_not() -> None:
    assert not original_quality(decision({"maxAudioBitrate": 320_000}))
    assert not original_quality(decision({"maxAudioBitrate": "320000"}))
    assert not original_quality(
        decision(
            {"codecProfiles": [{"name": "flac", "limitations": [{"name": "x", "required": True}]}]}
        )
    )
    assert not original_quality(decision(b"not json"))
    assert not original_quality(decision([1, 2]))
    assert not original_quality(decision({"codecProfiles": [{"limitations": "x"}]}))


def test_navidrome_s_answer_is_read_as_direct_play() -> None:
    ok = {"subsonic-response": {"status": "ok", "transcodeDecision": {"canDirectPlay": True}}}
    no = {"subsonic-response": {"status": "ok", "transcodeDecision": {"canDirectPlay": False}}}
    assert _direct_play_decision(json.dumps(ok).encode())
    assert not _direct_play_decision(json.dumps(no).encode())
    assert not _direct_play_decision(
        json.dumps({"subsonic-response": {"status": "failed"}}).encode()
    )
    xml = b'<subsonic-response status="ok"><transcodeDecision canDirectPlay="true"'
    assert _direct_play_decision(xml + b"></transcodeDecision></subsonic-response>")
    assert not _direct_play_decision(xml.replace(b'"true"', b'"false"') + b"/>")


def token(claims: dict[str, Any]) -> str:
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return f"eyJhbGciOiJIUzI1NiJ9.{payload}.signature"


def stream(transcode_params: str, media: str = "song-1") -> RestCall:
    query = f"mediaId={media}&mediaType=song&transcodeParams={transcode_params}".encode()
    return RestCall.build("getTranscodeStream", "GET", b"/rest/getTranscodeStream", query, [], None)


def test_a_direct_play_token_for_this_song_is_a_plain_stream() -> None:
    later = time.time() + 3600
    assert direct_play(
        stream(token({"mid": "song-1", "ua": 1, "dp": True, "exp": later})), "song-1"
    )
    assert direct_play(stream(token({"mid": "song-1", "ua": 1})), "song-1")  # no target format


def test_a_transcode_token_another_song_s_or_an_expired_one_is_not() -> None:
    later = time.time() + 3600
    assert not direct_play(stream(token({"mid": "song-1", "f": "mp3", "b": 192})), "song-1")
    assert not direct_play(stream(token({"mid": "song-2", "dp": True})), "song-1")
    assert not direct_play(
        stream(token({"mid": "song-1", "dp": True, "exp": time.time() - 5})), "song-1"
    )
    assert direct_play(stream(token({"mid": "song-1", "dp": True, "exp": later})), "song-1")
    assert not direct_play(stream("garbage"), "song-1")


def test_what_navidrome_would_convert() -> None:
    """Navidrome 0.64.2's rules for a file: its own format at or below the bitrate is
    served as it is, another format or a lower bitrate is converted; a bitrate alone is
    converted only below the file's and only with a downsampling format set; what the first
    bytes did not tell counts as converted."""
    assert not converted(("mp3", 320), "mp3", 320)
    assert not converted(("mp3", 0), "mp3", None)
    assert converted(("mp3", 128), "mp3", 320)
    assert converted(("mp3", 320), "flac", 900)
    assert converted(("aac", 256), "m4a", None)  # Navidrome's "aac" is not the file's "m4a"
    assert not converted(("m4a", 0), "m4a", None)
    assert converted(("m4a", 256), "m4a", None)  # its bitrate unread
    assert not converted(("", 320), "mp3", 320)
    assert converted(("", 320), "flac", 900)
    assert converted(("", 320), "opus", None)
    assert not converted(("", 128), "flac", 900, downsampling=False)
    assert converted(("mp3", 128), "mp3", 320, downsampling=False)
