"""A song the library's album lacks, in every format whose tags Shijhon can write: its
copy carries the album's tags and Navidrome puts it into that album (one album of the
name). Matroska's copy is made by ffmpeg as a stream copy. Against a real, disposable
Navidrome with invented tones.

TrueAudio is left out here: the pinned Darwin build does not index valid TrueAudio files
(``docs/music-imports.md``); its tags are written as for the other ID3 formats. Shorten
has no tags, so it never joins an album."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from shijhon.music_imports import tagmap
from tests.acceptance.test_music_import_overlap import LIBRARY, album_songs, approve, upload, world
from tests.harness.library import write_album
from tests.harness.music_import_formats import IMPORT_FORMATS, ImportFormat, import_tone

pytestmark = [pytest.mark.anyio, pytest.mark.canary]

FORMATS = [f for f in IMPORT_FORMATS if f.key not in ("shorten", "tak", "ac3", "trueaudio")]


def _tagged(source: Path, fmt: ImportFormat, n: int) -> Path:
    path = import_tone(source / fmt.key, fmt, tagged=False)
    if fmt.suffix == "mka":
        tagged = path.with_name("tagged.mka")
        metadata = {"title": f"Ember {fmt.key}", "artist": "Tide Paper", "album": "Night Letters",
                    "album_artist": "Tide Paper", "track": str(20 + n),
                    "DATE_RELEASED": "2020-03-01"}  # fmt: skip
        subprocess.run(
            ["ffmpeg", "-v", "error", "-nostdin", "-y", "-i", str(path), "-c", "copy",
             *(a for k, v in metadata.items() for a in ("-metadata", f"{k}={v}")), str(tagged)],
            check=True,
        )  # fmt: skip
        tagged.replace(path)
        return path
    with open(path, "rb+") as data:
        parsed = tagmap.parse(data, fmt.suffix)
        tags = tagmap.ensure_tags(parsed, data)
        tagmap.write(parsed, tags, tagmap.family(parsed), {
            "title": [f"Ember {fmt.key}"],
            "artist": ["Tide Paper"],
            "album": ["Night Letters"],
            "albumartist": ["Tide Paper"],
            "tracknumber": [str(20 + n)],
            # Only in these files: on their own they would make a second album.
            "releasedate": ["2020-03-01"],
        })  # fmt: skip
        tagmap.save(parsed, tags, data)
    return path


async def test_a_copy_in_every_writable_format_joins_the_library_album(navidrome_factory, tmp_path):
    nd = navidrome_factory()
    write_album(nd.music, LIBRARY)
    w = await world(nd, tmp_path, ("ben",))
    source = tmp_path / "ben"
    files = [_tagged(source, fmt, n) for n, fmt in enumerate(FORMATS)]
    review = await upload(w, "ben", files, source, "formats")
    outcomes = {i["path"].split("/")[0]: i["outcome"] for i in review["items"]}
    assert outcomes == {f.key: "copy" for f in FORMATS}, outcomes
    done = await approve(w, "ben", review)
    assert done["complete_files"] == len(FORMATS), done
    songs = await album_songs(w, "ben")  # one "Night Letters"
    assert sorted(s["title"] for s in songs) == sorted(
        [f"Lantern {n}" for n in range(1, 5)] + [f"Ember {f.key}" for f in FORMATS]
    )
    await w.caller.aclose()
    await w.imports.aclose()
    await w.store.close()
