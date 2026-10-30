"""Exercise review, source-refresh and reorder races with the shipped controllers."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_dashboard_controllers_preserve_edits_and_serialize_moves():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the browser controller check.")
    root = Path(__file__).parents[2]
    subprocess.run(
        [
            node,
            str(Path(__file__).with_name("fixtures") / "dashboard_review_browser.cjs"),
            str(root / "src/shijhon/dashboard/static"),
        ],
        check=True,
        timeout=30,
        capture_output=True,
        text=True,
    )
