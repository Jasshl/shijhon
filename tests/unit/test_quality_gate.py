from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from shijhon.delivery.quality_gate import (
    LibrarySong,
    Minimums,
    QualityGate,
    addon_quality,
    library_song,
    minimum_of,
    native_quality,
    refusal,
)
from shijhon.navidrome.client import NavidromeError
from shijhon.quality import ANY, DEFAULT, Minimum
from shijhon.store import Store


def test_choices_name_minimums() -> None:
    assert minimum_of("any") == ANY
    assert minimum_of("192") == DEFAULT
    assert minimum_of("320") == Minimum(kbps=320)
    assert minimum_of("lossless") == Minimum(kbps=0, lossless_only=True)
    assert minimum_of("999") == ANY and minimum_of("") == ANY


def test_qualities_of_native_songs_and_addon_audio() -> None:
    mp3 = native_quality({"suffix": "mp3", "bitRate": 128, "sampleRate": 44100})
    flac = native_quality({"suffix": "flac", "bitRate": 900, "sampleRate": 48000, "bitDepth": 16})
    alac = native_quality({"suffix": "m4a", "codec": "alac", "bitRate": 700})
    assert not mp3.lossless and mp3.kbps == 128 and mp3.sample_rate == 44100
    assert flac.lossless and alac.lossless
    assert not DEFAULT.met_by(mp3) and DEFAULT.met_by(flac)
    assert addon_quality("flac", None).lossless
    assert not addon_quality("aac", 128).lossless
    assert not addon_quality(None, None).known()  # nothing told: meets every minimum
    assert DEFAULT.met_by(addon_quality(None, None))


def test_addon_audio_below_the_minimum_is_refused_with_what_it_told() -> None:
    assert refusal(DEFAULT, "mp3", 128) == "below the listener's minimum (MP3 at 128 kbit/s)"
    assert refusal(DEFAULT, "mp3", 320) is None and refusal(DEFAULT, "flac", None) is None
    assert refusal(Minimum(kbps=0, lossless_only=True), "mp3", 320) is not None
    assert refusal(ANY, "mp3", 96) is None
    # Nothing told (an M4A's bitrate is not read): it does.
    assert refusal(DEFAULT, "m4a", None) is None and refusal(DEFAULT, None, None) is None


def test_library_song_is_the_recording_for_the_addons() -> None:
    song = library_song(
        "s1",
        {
            "title": "Lantern",
            "artist": "Tide Paper",
            "duration": 201.4,
            "discNumber": 2,
            "trackNumber": 3,
            "tags": {"isrc": ["XXA0L2000001"]},
            "explicitStatus": "c",
            "suffix": "mp3",
            "bitRate": 128,
        },
    )
    track = song.track()
    assert (track.song_id, track.isrc, track.title, track.artist) == (
        "s1",
        "XXA0L2000001",
        "Lantern",
        "Tide Paper",
    )
    assert (track.duration_ms, track.disc, track.number, track.version) == (201400, 2, 3, "clean")
    assert track.ref is None and track.key == "s1"


class FakeNavidrome:
    def __init__(self, songs: dict[str, dict[str, Any]]) -> None:
        self.songs = songs
        self.asked: list[str] = []
        self.fail = False

    async def song(self, song_id: str) -> dict[str, Any] | None:
        self.asked.append(song_id)
        if self.fail:
            raise NavidromeError("down")
        return self.songs.get(song_id)


class FixedMinimums:
    def __init__(self, minimum: Minimum) -> None:
        self.minimum = minimum

    async def minimum_for(self, **kw: Any) -> Minimum:
        return self.minimum

    async def anyone(self) -> bool:
        return self.minimum != ANY


def gate(minimum: Minimum, navidrome: FakeNavidrome, **kw: Any) -> QualityGate:
    return QualityGate(FixedMinimums(minimum), navidrome, library_id=1, **kw)  # type: ignore[arg-type]


SONGS = {
    "low": {"suffix": "mp3", "bitRate": 128, "sampleRate": 44100, "libraryId": 1},
    "high": {"suffix": "flac", "bitRate": 900, "sampleRate": 44100, "libraryId": 1},
    "elsewhere": {"suffix": "mp3", "bitRate": 96, "sampleRate": 44100, "libraryId": 2},
    "missing": {"suffix": "mp3", "bitRate": 96, "libraryId": 1, "missing": True},
    "unknown": {"suffix": "aac", "bitRate": 0, "libraryId": 1},
}


def test_gate_decides_by_the_listener_minimum() -> None:
    async def main() -> None:
        navidrome = FakeNavidrome(SONGS)
        strict = gate(DEFAULT, navidrome)
        assert isinstance(await strict.below("ana", "low"), LibrarySong)
        assert await strict.below("ana", "high") is None
        assert await strict.below("ana", "elsewhere") is None  # another library: Navidrome's
        assert await strict.below("ana", "missing") is None
        assert await strict.below("ana", "unknown") is None  # never turned away for no data
        assert await strict.below("ana", "nothing") is None
        assert not await strict.stands_in("ana", "low")
        assert await strict.stands_in("ana", "high") and await strict.stands_in(None, "low")
        # Remembered: the same song is not asked again ...
        asked = len(navidrome.asked)
        await strict.below("ben", "low")
        assert len(navidrome.asked) == asked
        # ... until it is forgotten (its file changed).
        strict.forget("low")
        await strict.below("ben", "low")
        assert len(navidrome.asked) == asked + 1

        # Any quality: nothing is asked at all (no work for files that play as they are).
        relaxed_navidrome = FakeNavidrome(SONGS)
        relaxed = gate(ANY, relaxed_navidrome)
        assert await relaxed.below("ana", "low") is None
        assert relaxed_navidrome.asked == []
        # An account that gets real audio only is never sent to the add-ons.
        analyzer = gate(DEFAULT, FakeNavidrome(SONGS), exempt=lambda name: name == "analyzer")
        assert await analyzer.below("analyzer", "low") is None
        # Navidrome cannot tell: the file plays.
        broken = FakeNavidrome(SONGS)
        broken.fail = True
        assert await gate(DEFAULT, broken).below("ana", "low") is None

    anyio.run(main)


def test_minimums_are_saved_per_person(tmp_path: Path) -> None:
    async def main() -> None:
        store = await Store.open(tmp_path / "state.db")
        try:
            minimums = Minimums(store, FakeNavidrome({}), default="192")  # type: ignore[arg-type]
            assert await minimums.choice("u1") is None
            assert await minimums.minimum_for(user_id="u1") == DEFAULT
            await minimums.set("u1", "320")
            await minimums.set("u2", "lossless")
            assert await minimums.minimum_for(user_id="u1") == Minimum(kbps=320)
            assert (await minimums.minimum_for(user_id="u2")).lossless_only
            # The installation's default changes for those without a choice of their own.
            minimums.default = "any"
            assert await minimums.minimum_for(user_id="u3") == ANY
            assert await minimums.minimum_for(user_id="u1") == Minimum(kbps=320)
            await minimums.set("u1", None)
            assert await minimums.choice("u1") is None
            # Saved durably: a new service reads them back.
            again = Minimums(store, FakeNavidrome({}), default="192")  # type: ignore[arg-type]
            assert await again.choice("u2") == "lossless" and await again.choice("u1") is None
            # No user: no minimum (a name that cannot be looked up gets plain behavior).
            assert await again.minimum_for() == ANY
        finally:
            await store.close()

    anyio.run(main)


def test_whether_anyone_can_have_a_minimum_is_kept_until_a_choice_changes(
    tmp_path: Path,
) -> None:
    async def main() -> None:
        store = await Store.open(tmp_path / "state.db")
        try:
            minimums = Minimums(store, FakeNavidrome({}), default="any")  # type: ignore[arg-type]
            assert not await minimums.anyone()
            await minimums.set("u1", "any")  # chosen, but no minimum
            assert not await minimums.anyone()
            await minimums.set("u2", "lossless")
            assert await minimums.anyone()
            await minimums.set("u2", None)
            assert not await minimums.anyone()
            minimums.default = "192"  # the installation's default, changed live
            assert await minimums.anyone()
            minimums.default = "any"
            assert not await minimums.anyone()
            await minimums.set("u3", "128")
            # Read once, then kept: a new service reads the saved choices.
            again = Minimums(store, FakeNavidrome({}), default="any")  # type: ignore[arg-type]
            assert await again.anyone()
            await store.execute("DELETE FROM saved_settings")
            assert await again.anyone()  # (kept until a choice or the default changes)
            # Without anyone who can have a minimum, nothing is asked about a song.
            navidrome = FakeNavidrome(SONGS)
            nobody = QualityGate(minimums, navidrome, library_id=1)  # type: ignore[arg-type]
            await minimums.set("u3", None)
            assert not await nobody.anyone()
        finally:
            await store.close()

    anyio.run(main)


@pytest.mark.parametrize("change", ["default", "choice"])
def test_a_reading_of_anyone_overtaken_by_a_change_is_never_kept(
    tmp_path: Path, change: str
) -> None:
    """The default or someone's choice changes while whether anyone can have a minimum is
    being read: that reading is neither answered nor kept - it is read again."""

    async def main() -> None:
        store = await Store.open(tmp_path / "state.db")
        fetchone = store.fetchone
        reading, resume = anyio.Event(), anyio.Event()

        async def slow(*args: Any, **kwargs: Any) -> Any:
            found = await fetchone(*args, **kwargs)
            if not reading.is_set():  # the first reading waits for the change
                reading.set()
                await resume.wait()
            return found

        results: list[bool] = []
        try:
            minimums = Minimums(store, FakeNavidrome({}), default="any")  # type: ignore[arg-type]
            store.fetchone = slow  # type: ignore[method-assign]

            async def ask() -> None:
                results.append(await minimums.anyone())

            with anyio.fail_after(10):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(ask)
                    await reading.wait()  # it read that no one has a minimum
                    if change == "default":
                        minimums.default = "192"
                    else:
                        await minimums.set("u1", "320")
                    resume.set()
            assert results == [True]
            assert await minimums.anyone()  # kept: the change's
            minimums.default = "any"
            await minimums.set("u1", None)
            assert not await minimums.anyone()
        finally:
            store.fetchone = fetchone  # type: ignore[method-assign]
            await store.close()

    anyio.run(main)


@pytest.mark.parametrize("codec", ["AAC", "mp3", "opus", "vorbis", "wma"])
def test_a_native_lossy_codec_with_a_bit_depth_stays_below_the_minimum(codec: str) -> None:
    quality = native_quality({"suffix": "m4a", "codec": codec, "bitDepth": 16, "bitRate": 128})
    assert not quality.lossless and not DEFAULT.met_by(quality)
