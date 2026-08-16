"""Runs the Orchestration-runs panel client JS under Node (tests/web/).

Same discipline as tests/test_product_proposals_js.py: the harness parses
the shipped index.html, runs its WHOLE <script> in a VM over the
dependency-free DOM (tests/web/dom.js) and drives the real detail()/
renderRuns code — nothing is sliced or simulated.

Node is a HARD prerequisite: a missing `node` FAILS this test, it does not
skip — a skip is how a suite goes green while covering nothing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

WEB = Path(__file__).parent / "web"
HARNESS = WEB / "runs_harness.js"
INDEX_HTML = (
    Path(__file__).parent.parent / "dispatcher" / "server" / "static" / "index.html"
)

_MISSING_NODE = (
    "node is a required prerequisite of this test suite for verifying the "
    "Orchestration-runs panel's client JS — install Node (CI pins Node 22 "
    "via actions/setup-node in ci.yml's `test` job). Without it the panel "
    "acceptance is UNVERIFIED, and that must FAIL, not skip."
)


def test_runs_panel_js() -> None:
    node = shutil.which("node")
    assert node is not None, _MISSING_NODE
    result = subprocess.run(
        [node, str(HARNESS), str(INDEX_HTML)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"harness failed\n--- stdout ---\n{result.stdout}"
        f"\n--- stderr ---\n{result.stderr}"
    )
