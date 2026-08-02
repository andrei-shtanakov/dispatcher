"""Runs the governance-panel client JS under Node (tests/web/).

Same discipline as tests/test_task_authoring_js.py: the harness
(`tests/web/governance_harness.js`) parses the shipped index.html, runs its
WHOLE `<script>` in a VM over the dependency-free DOM (tests/web/dom.js) and
drives the real `renderGovernance` / `detail()` code — nothing is sliced or
simulated. M-01 (no damaged class renders as pass) and M-02 (the blocker is
readable off one screen) are asserted there, client-side.

Node is a HARD prerequisite: a missing `node` FAILS this test, it does not
skip — a skip is how a suite goes green while covering nothing.
"""

import shutil
import subprocess
from pathlib import Path

WEB = Path(__file__).parent / "web"
HARNESS = WEB / "governance_harness.js"
INDEX_HTML = (
    Path(__file__).parent.parent / "dispatcher" / "server" / "static" / "index.html"
)

_MISSING_NODE = (
    "node is a required prerequisite of this test suite for verifying the "
    "governance panel's client JS — install Node (CI pins Node 22 via "
    "actions/setup-node in ci.yml's `test` job). Without it M-01/M-02 are "
    "UNVERIFIED, and that must FAIL, not skip."
)


def test_governance_panel_js_suite_passes() -> None:
    node = shutil.which("node")
    assert node is not None, _MISSING_NODE
    result = subprocess.run(
        [node, str(HARNESS), str(INDEX_HTML)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"governance harness failed\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
