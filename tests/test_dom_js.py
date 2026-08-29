"""The browser model under tests/web/dom.js is itself under test.

A router built on location/history/hashchange is only as trustworthy as the
stand-in it is exercised against, so the stand-in gets its own red/green.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

WEB = Path(__file__).parent / "web"
SELFTEST = WEB / "dom_selftest.js"


def test_browser_model_behaves_like_a_browser() -> None:
    node = shutil.which("node")
    # A skip is how a suite goes green while covering nothing.
    assert node is not None, "node is required for the web harnesses"
    result = subprocess.run(
        [node, str(SELFTEST)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
