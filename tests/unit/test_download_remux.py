"""Local remux failures stay private, and cancellation removes their temporary files."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

import shijhon.delivery.download_first as download_module
from shijhon.delivery.download_first import DownloadFirst
from shijhon.delivery.playback import PlaybackSettings


def remuxer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> tuple[DownloadFirst, Path, str]:
    fetcher = DownloadFirst(SimpleNamespace(settings=PlaybackSettings()), None, None, tmp_path)
    partial = tmp_path / "input.part"
    partial.write_bytes(b"invented audio")
    monkeypatch.setattr(download_module, "audio_suffix", lambda *args, **kwargs: suffix)
    monkeypatch.setattr(download_module, "kept_suffix", lambda *args, **kwargs: suffix)
    monkeypatch.setattr(download_module.tagmap, "matroska_carries", lambda tags: False)
    monkeypatch.setattr(download_module, "_fragmented", lambda path: True)
    monkeypatch.setattr(download_module, "_kind_of", lambda path: None)
    name = {".aac": "_adts_in_m4a", ".mka": "_out_of_matroska",
            ".m4a": "_every_track", ".oga": "_out_of_ogg"}[suffix]  # fmt: skip
    return fetcher, partial, name


async def convert(fetcher: DownloadFirst, partial: Path, suffix: str) -> str | None:
    if suffix in (".aac", ".mka"):
        return await fetcher._put_in_place(
            {"duration_ms": 3000, "tags": "{}"}, partial, None, every=True
        )
    return (await fetcher._one_piece(partial, suffix, 3000))[1]


@pytest.mark.anyio
@pytest.mark.parametrize("suffix", [".aac", ".mka", ".m4a", ".oga"])
@pytest.mark.parametrize("error", ["exit", "timeout", "os"])
async def test_remux_failure_does_not_include_command_or_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str, error: str
) -> None:
    fetcher, partial, name = remuxer(tmp_path, monkeypatch, suffix)
    marker = "private-remux-state"
    command = ["ffmpeg", "-i", str(tmp_path / marker)]
    failure = {
        "exit": subprocess.CalledProcessError(1, command),
        "timeout": subprocess.TimeoutExpired(command, 1),
        "os": OSError(marker),
    }[error]
    copies: list[Path] = []

    def failed(source: Path, out: Path) -> None:
        out.write_bytes(b"unfinished copy")
        copies.append(out)
        raise failure

    monkeypatch.setattr(download_module, name, failed)
    reason = await convert(fetcher, partial, suffix)
    assert reason is not None and type(failure).__name__ in reason
    assert marker not in reason and str(tmp_path) not in reason and "ffmpeg" not in reason
    assert copies and all(not copy.exists() for copy in copies)


@pytest.mark.anyio
@pytest.mark.parametrize("suffix", [".aac", ".mka", ".m4a", ".oga"])
async def test_cancelled_remux_removes_its_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    fetcher, partial, name = remuxer(tmp_path, monkeypatch, suffix)
    copies: list[Path] = []

    with anyio.CancelScope() as scope:

        def cancelled(source: Path, out: Path) -> float:
            out.write_bytes(b"unfinished copy")
            copies.append(out)
            anyio.from_thread.run_sync(scope.cancel)
            return 3.0

        monkeypatch.setattr(download_module, name, cancelled)
        await convert(fetcher, partial, suffix)
        raise AssertionError("the cancelled conversion must stop")
    assert scope.cancelled_caught and copies
    assert all(not copy.exists() for copy in copies)
