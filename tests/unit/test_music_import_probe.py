"""Private parser refusals and input budgets using actual local audio bytes."""

import json
import os
import subprocess
import sys

import pytest
from mutagen.id3 import ID3, TXXX

from shijhon.music_imports.probe import READ_LIMIT, HeaderReader
from tests.harness.library import tone
from tests.harness.music_import_formats import IMPORT_FORMATS, import_tone


def check(path, fmt):
    with path.open("rb") as source:
        return subprocess.run(
            [sys.executable, "-m", "shijhon.music_imports.probe", str(source.fileno()), fmt],
            pass_fds=(source.fileno(),),
            capture_output=True,
            timeout=15,
        )


@pytest.mark.parametrize("fmt", ["m4a", "wav", "ogg", "opus", "aac", "mka", "shn", "wma", "tta"])
def test_different_container_and_url_text_are_not_audio(fmt, tmp_path):
    source = tmp_path / ("invented." + fmt)
    source.write_bytes(tone(523, 0.3, "flac").read_bytes())
    assert check(source, fmt).returncode != 0
    source.write_bytes(b"#EXTM3U\nhttps://invalid.example/no-audio\n")
    result = check(source, fmt)
    assert result.returncode != 0 and result.stdout == b""
    assert b"invalid.example" not in result.stderr


@pytest.mark.parametrize("tagged", [False, True])
def test_trueaudio_reads_id3_without_changing_original(tmp_path, tagged):
    fmt = next(fmt for fmt in IMPORT_FORMATS if fmt.key == "trueaudio")
    source = import_tone(tmp_path, fmt, tagged=tagged)
    original, before = source.read_bytes(), source.stat()
    result = check(source, "tta")
    assert result.returncode == 0 and result.stderr == b""
    warnings = json.loads(result.stdout)["warnings"]
    assert len(warnings) == (0 if tagged else 2)
    assert source.read_bytes() == original
    assert source.stat().st_mtime_ns == before.st_mtime_ns


@pytest.mark.parametrize("tagged", [False, True])
def test_trueaudio_rejects_invalid_header_even_with_id3(tmp_path, tagged):
    fmt = next(fmt for fmt in IMPORT_FORMATS if fmt.key == "trueaudio")
    source = import_tone(tmp_path, fmt, tagged=tagged)
    invalid = source.read_bytes().replace(b"TTA1", b"NOPE", 1)
    source.write_bytes(invalid)
    result = check(source, "tta")
    assert result.returncode != 0 and result.stdout == result.stderr == b""
    assert source.read_bytes() == invalid


def test_trueaudio_inspects_large_original_within_cumulative_read_budget(tmp_path):
    fmt = next(fmt for fmt in IMPORT_FORMATS if fmt.key == "trueaudio")
    source = import_tone(tmp_path, fmt, tagged=True)
    # Header inspection must stay bounded when the payload exceeds the budget.
    # Padding is sufficient here: this test makes no complete-frame decode claim.
    with source.open("ab") as file:
        file.truncate(READ_LIMIT + 1)
    before = source.stat()
    result = check(source, "tta")
    assert result.returncode == 0 and json.loads(result.stdout)["warnings"] == []
    assert (source.stat().st_size, source.stat().st_mtime_ns) == (
        before.st_size,
        before.st_mtime_ns,
    )


@pytest.mark.parametrize("fmt", ["mp3", "tta"])
def test_oversized_id3_is_refused_without_rewriting_original(tmp_path, fmt):
    if fmt == "tta":
        source = import_tone(
            tmp_path, next(item for item in IMPORT_FORMATS if item.key == "trueaudio"), tagged=True
        )
    else:
        source = tmp_path / "oversized.mp3"
        source.write_bytes(tone(523, 0.3, "mp3").read_bytes())
    tags = ID3(source)
    tags.add(TXXX(encoding=3, desc="synthetic", text=["x" * (READ_LIMIT + 1)]))
    tags.save(source)
    before = source.stat()
    assert check(source, "mp3").returncode != 0
    after = source.stat()
    assert (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)


def test_reader_bounds_tail_reads_and_cumulative_reads(tmp_path):
    source = tmp_path / "sparse"
    with source.open("wb") as file:
        file.truncate(READ_LIMIT + 1)
    with HeaderReader(source.open("rb", buffering=0)) as reader:
        with pytest.raises(ValueError):
            reader.read()
        reader.seek(-4, os.SEEK_END)
        assert reader.read() == b"\0" * 4
        reader.remaining = 3
        reader.seek(0)
        assert reader.read(2) == b"\0" * 2
        with pytest.raises(ValueError):
            reader.read(2)
