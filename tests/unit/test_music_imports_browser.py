"""Run the shipped upload controller against durable synthetic network failures."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("crypto_surface", ["secure", "insecure"])
def test_bounded_browser_upload_and_uncertain_response_recovery(crypto_surface):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the browser controller check.")
    root = Path(__file__).parents[2]
    subprocess.run(
        [
            node,
            str(Path(__file__).with_name("fixtures") / "music_imports_browser.cjs"),
            str(root / "src/shijhon/dashboard/static/music_imports.js"),
            crypto_surface,
        ],
        check=True,
        timeout=30,
        capture_output=True,
        text=True,
    )
