"""Generated tones in native music containers; no downloaded recordings."""

from __future__ import annotations

import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path

import mutagen
from mutagen.aac import AAC
from mutagen.ac3 import AC3
from mutagen.asf import ASF
from mutagen.dsf import DSF
from mutagen.id3 import ID3, TALB, TIT2, TPE1
from mutagen.mp4 import MP4


@dataclass(frozen=True)
class ImportFormat:
    key: str
    suffix: str
    args: tuple[str, ...]


IMPORT_FORMATS = tuple(
    ImportFormat(*row)
    for row in [
        ("mp3", "mp3", ("-c:a", "libmp3lame")),
        ("flac", "flac", ("-c:a", "flac")),
        ("aac-m4a", "m4a", ("-c:a", "aac")),
        ("alac-m4a", "m4a", ("-c:a", "alac")),
        ("alac", "alac", ("-c:a", "alac", "-f", "ipod")),
        ("aac-m4b", "m4b", ("-c:a", "aac", "-f", "ipod")),
        ("aac-adts", "aac", ("-c:a", "aac", "-f", "adts")),
        ("opus", "opus", ("-c:a", "libopus")),
        ("ogg-opus", "ogg", ("-c:a", "libopus", "-f", "ogg")),
        ("vorbis", "ogg", ("-c:a", "vorbis", "-strict", "-2")),
        ("ogg-flac", "oga", ("-c:a", "flac", "-f", "ogg")),
        ("wav-pcm", "wav", ("-c:a", "pcm_s16le")),
        ("wav-float", "wav", ("-c:a", "pcm_f32le")),
        ("aiff", "aiff", ("-c:a", "pcm_s16be")),
        ("aif", "aif", ("-c:a", "pcm_s16be", "-f", "aiff")),
        ("wma", "wma", ("-c:a", "wmav2")),
        ("wavpack", "wv", ("-c:a", "wavpack")),
        ("wavpack-wvp", "wvp", ("-c:a", "wavpack", "-f", "wv")),
        ("matroska-flac", "mka", ("-c:a", "flac")),
        ("mp2", "mp2", ("-c:a", "mp2")),
        ("ac3", "ac3", ("-c:a", "ac3")),
        ("trueaudio", "tta", ("-c:a", "tta")),
        ("dsf", "dsf", ()),
        ("monkeys-audio", "ape", ()),
        ("musepack", "mpc", ()),
        ("shorten", "shn", ()),
        ("tak", "tak", ()),
        ("speex-ogg", "ogg", ()),
        ("speex-spx", "spx", ()),
        ("aifc", "aifc", ("-c:a", "pcm_s16le", "-f", "aiff")),
    ]
)


def import_tone(folder: Path, fmt: ImportFormat, *, tagged: bool) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"01 First Light {fmt.key}.{fmt.suffix}"
    if fmt.suffix in ("ape", "mpc", "shn", "tak", "spx") or fmt.key == "speex-ogg":
        suffix = "spx" if fmt.key == "speex-ogg" else fmt.suffix
        shutil.copyfile(
            Path(__file__).parent / "fixtures" / "music-import-formats" / ("tone." + suffix),
            path,
        )
    elif fmt.key == "dsf":
        # A complete two-channel DSD64 stream with deterministic synthetic samples.
        samples, block = 2822400 // 4, 4096
        blocks = ((samples + 7) // 8 + block - 1) // block
        payload = bytes([0x69]) * (blocks * block * 2)
        header = b"DSD " + struct.pack("<QQQ", 28, 28 + 52 + 12 + len(payload), 0)
        header += b"fmt " + struct.pack(
            "<QIIIIIIQII", 52, 1, 0, 2, 2, 2822400, 1, samples, block, 0
        )
        header += b"data" + struct.pack("<Q", 12 + len(payload))
        path.write_bytes(header + payload)
    else:
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=523:sample_rate=44100",
                "-ac",
                "2",
                "-t",
                "0.3",
                "-map_metadata",
                "-1",
                *fmt.args,
                str(path),
            ],
            check=True,
            capture_output=True,
        )
    if not tagged:
        return path
    title = "First Light " + fmt.key
    if fmt.suffix == "mka":
        temporary = path.with_name("tagged.mka")
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(path),
                "-c",
                "copy",
                "-metadata",
                "title=" + title,
                "-metadata",
                "artist=Glass Orchard",
                "-metadata",
                "album=Paper Garden",
                str(temporary),
            ],
            check=True,
            capture_output=True,
        )
        temporary.replace(path)
        return path
    parsed = DSF(path) if fmt.key == "dsf" else mutagen.File(path)
    assert parsed is not None
    if isinstance(parsed, (AAC, AC3)):
        tags = ID3()
        tags.add(TIT2(encoding=3, text=[title]))
        tags.add(TPE1(encoding=3, text=["Glass Orchard"]))
        tags.add(TALB(encoding=3, text=["Paper Garden"]))
        tags.save(path)
    else:
        if parsed.tags is None:
            parsed.add_tags()
        if isinstance(parsed.tags, ID3):
            parsed.tags.add(TIT2(encoding=3, text=[title]))
            parsed.tags.add(TPE1(encoding=3, text=["Glass Orchard"]))
            parsed.tags.add(TALB(encoding=3, text=["Paper Garden"]))
        else:
            keys = ("title", "artist", "album")
            if isinstance(parsed, MP4):
                keys = ("\xa9nam", "\xa9ART", "\xa9alb")
            elif isinstance(parsed, ASF):
                keys = ("Title", "Author", "WM/AlbumTitle")
            for key, value in zip(keys, (title, "Glass Orchard", "Paper Garden"), strict=True):
                parsed[key] = [value]
        parsed.save()
    return path
