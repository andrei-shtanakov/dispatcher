"""Runs the tab shell's client JS under Node (tests/web/).

Same discipline as tests/test_launchpad_js.py: the harness
(`tests/web/tabs_harness.js`) parses the shipped index.html, runs its WHOLE
`<script>` in a VM over the dependency-free DOM (tests/web/dom.js) plus the
browser model (`makeBrowser`: location, history, hashchange) and drives the
real router — nothing is sliced or simulated. This is the one place that
verifies design §3.3 (exactly one tabpanel visible, `#ta-outcomes` outside
every panel), §4 (ARIA + keyboard) and §4.1 (hash routing, closed grammar,
safe fallback, Back/Forward).

Node is a HARD prerequisite: a missing `node` FAILS this test, it does not
skip — a skip is how a suite goes green while covering nothing.
"""

import shutil
import subprocess
from pathlib import Path

WEB = Path(__file__).parent / "web"
HARNESS = WEB / "tabs_harness.js"
INDEX_HTML = (
    Path(__file__).parent.parent / "dispatcher" / "server" / "static" / "index.html"
)

_MISSING_NODE = (
    "node is a required prerequisite of this test suite for verifying the "
    "tab shell's client JS — install Node (CI pins Node 22 via "
    "actions/setup-node in ci.yml's `test` job). Without it the routing and "
    "one-panel-at-a-time contract is UNVERIFIED, and that must FAIL, not skip."
)


def test_tabs_js_suite_passes() -> None:
    node = shutil.which("node")
    assert node is not None, _MISSING_NODE
    # Same timeout discipline as test_launchpad_js.py: this harness installs a
    # controllable fake timer (`makeIntervalRecorder`) — a regression that lets
    # a real timer loop, or a hashchange handler that re-enters forever, must
    # fail rather than hang CI.
    result = subprocess.run(
        [node, str(HARNESS), str(INDEX_HTML)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"tabs harness failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
