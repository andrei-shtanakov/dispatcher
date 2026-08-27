"""Runs the launchpad root panel's client JS under Node (tests/web/).

Same discipline as tests/test_run_console_js.py: the harness
(`tests/web/launchpad_harness.js`) parses the shipped index.html, runs its
WHOLE `<script>` in a VM over the dependency-free DOM (tests/web/dom.js) and
drives the real `lpRender()`/`lpFetchSnapshot()`/`lpRefetchAfterAction()`
code — nothing is sliced or simulated. This is the one place that verifies
the sequence guard (spec §10): a superseded snapshot response must never
apply, not even temporarily, through both resolution orders.

Node is a HARD prerequisite: a missing `node` FAILS this test, it does not
skip — a skip is how a suite goes green while covering nothing.
"""

import shutil
import subprocess
from pathlib import Path

WEB = Path(__file__).parent / "web"
HARNESS = WEB / "launchpad_harness.js"
INDEX_HTML = (
    Path(__file__).parent.parent / "dispatcher" / "server" / "static" / "index.html"
)

_MISSING_NODE = (
    "node is a required prerequisite of this test suite for verifying the "
    "launchpad panel's client JS — install Node (CI pins Node 22 via "
    "actions/setup-node in ci.yml's `test` job). Without it the sequence "
    "guard is UNVERIFIED, and that must FAIL, not skip."
)


def test_launchpad_js_suite_passes() -> None:
    node = shutil.which("node")
    assert node is not None, _MISSING_NODE
    # Same timeout discipline as test_run_console_js.py: this harness also
    # installs a controllable fake timer (`makeIntervalRecorder`,
    # tests/web/launchpad_harness.js) — a regression that lets a real timer
    # loop, or a callback that never settles its stalled promise, hang
    # rather than fail must not hang CI instead of failing the suite.
    result = subprocess.run(
        [node, str(HARNESS), str(INDEX_HTML)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"launchpad harness failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
