"""Runs the run-console panel's client JS under Node (tests/web/).

Same discipline as tests/test_governance_js.py: the harness
(`tests/web/run_console_harness.js`) parses the shipped index.html, runs its
WHOLE `<script>` in a VM over the dependency-free DOM (tests/web/dom.js) and
drives the real `renderReceipt()` / submit-handler code — nothing is sliced
or simulated. The three-valued `accepted` (true | false | null) is asserted
there, client-side: `false` and `null` are both falsy in JS, so this is the
one place that verifies a truthiness branch has not quietly merged "refused"
with "unknown".

Node is a HARD prerequisite: a missing `node` FAILS this test, it does not
skip — a skip is how a suite goes green while covering nothing.
"""

import shutil
import subprocess
from pathlib import Path

WEB = Path(__file__).parent / "web"
HARNESS = WEB / "run_console_harness.js"
INDEX_HTML = (
    Path(__file__).parent.parent / "dispatcher" / "server" / "static" / "index.html"
)

_MISSING_NODE = (
    "node is a required prerequisite of this test suite for verifying the "
    "run console's client JS — install Node (CI pins Node 22 via "
    "actions/setup-node in ci.yml's `test` job). Without it the three-valued "
    "accepted rendering is UNVERIFIED, and that must FAIL, not skip."
)


def test_run_console_js_suite_passes() -> None:
    node = shutil.which("node")
    assert node is not None, _MISSING_NODE
    result = subprocess.run(
        [node, str(HARNESS), str(INDEX_HTML)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"run console harness failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
