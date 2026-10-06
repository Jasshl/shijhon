"""Made-up DASH audio for the fake add-on: ffmpeg's dash muxer over a synthetic tone, three
representations as a real manifest offers them (AAC at 96 kbit/s and 22.05 kHz, AAC at
320 kbit/s, FLAC in MP4), in the standard layouts. Built once and cached, like the tones.

Layouts: ``timeline`` (SegmentTemplate and SegmentTimeline), ``duration`` (SegmentTemplate
with @duration), ``list`` (SegmentList of files), ``ranges`` (SegmentList of byte ranges
of one file per representation, under a BaseURL), ``base`` (SegmentBase: one file each,
edited from ``ranges``). ``sets``: 2 puts the FLAC representation into an AdaptationSet of
its own. ``mix="mp3"``: MP3 in MP4 at 192 kbit/s and AAC at 128 kbit/s instead.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path

from tests.harness.library import frequency_for, tone
from tests.harness.paths import cache_dir, file_lock

LAYOUTS = {
    "timeline": ["-use_template", "1", "-use_timeline", "1"],
    "duration": ["-use_template", "1", "-use_timeline", "0"],
    "list": ["-use_template", "0"],
    "ranges": ["-single_file", "1"],
    "base": ["-single_file", "1"],
}
# The representations' IDs in the manifests (and in the segments' names).
AAC_96, AAC_320, FLAC = "0", "1", "2"
MP3_192, AAC_128 = "0", "1"  # (mix="mp3")
_MIXES = {
    "flac": [
        "-map", "0:a", "-map", "0:a", "-map", "0:a",
        "-c:a:0", "aac", "-b:a:0", "96k", "-ar:a:0", "22050",
        "-c:a:1", "aac", "-b:a:1", "320k",
        "-c:a:2", "flac", "-strict", "experimental",
    ],
    "mp3": [
        "-map", "0:a", "-map", "0:a",
        "-c:a:0", "libmp3lame", "-b:a:0", "192k",
        "-c:a:1", "aac", "-b:a:1", "128k",
    ],
}  # fmt: skip


def dash_audio(
    seed: str, seconds: float = 6, layout: str = "timeline", sets: int = 1, mix: str = "flac"
) -> Path:
    """A folder with ``out.mpd`` and its segments: ``seed``'s tone (as ``tone`` makes it,
    so its FLAC is the same audio), in 2-second segments."""
    frequency = frequency_for(seed)
    name = f"{frequency}-{seconds:g}-{layout}-{sets}" + ("" if mix == "flac" else f"-{mix}")
    folder = cache_dir() / "test-dash" / name
    if (folder / "out.mpd").exists():
        return folder
    source = tone(frequency, seconds, "flac")
    with file_lock(cache_dir() / "test-dash" / ".lock"):
        if (folder / "out.mpd").exists():
            return folder
        partial = folder.with_name(f"partial-{uuid.uuid4().hex}")
        partial.mkdir(parents=True)
        sets_arg = "id=0,streams=a" if sets == 1 else "id=0,streams=0,1 id=1,streams=2"
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-i", str(source), *_MIXES[mix],
                "-f", "dash", "-seg_duration", "2", *LAYOUTS[layout],
                "-adaptation_sets", sets_arg, str(partial / "out.mpd"),
            ],
            check=True,
        )  # fmt: skip
        if layout == "base":
            manifest = partial / "out.mpd"
            manifest.write_text(_segment_base(manifest.read_text()))
        if folder.exists():
            shutil.rmtree(folder)
        partial.rename(folder)
    return folder


def _segment_base(text: str) -> str:
    """Each representation's SegmentList of byte ranges as the SegmentBase of its file."""

    def one(match: re.Match[str]) -> str:
        init = re.search(r'<Initialization range="([0-9-]+)"', match[0])
        assert init is not None
        return f'<SegmentBase><Initialization range="{init[1]}" /></SegmentBase>'

    return re.sub(r"<SegmentList.*?</SegmentList>", one, text, flags=re.S)


def segment_names(folder: Path, representation: str) -> list[str]:
    """The media segments' file names of one representation (``timeline``, ``duration`` and
    ``list`` layouts), in order."""
    return sorted(p.name for p in folder.glob(f"chunk-stream{representation}-*.m4s"))
