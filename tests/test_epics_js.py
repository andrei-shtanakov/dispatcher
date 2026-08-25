"""Runs the epics-panel client JS under Node (tests/web/epics_harness.js).

The server-side fix gave the planes a third state, `partial`, so that a
half-observed fleet stops reporting itself as measured. That fix is only worth
as much as the surface that renders it: a client mapping `partial` onto the
`unavailable` branch throws away a real count, and one mapping it onto `read`
undoes the fix entirely. Both are asserted against the SHIPPED index.html.

Node is a HARD prerequisite: a missing `node` FAILS this test, it does not skip.
"""

import shutil
import subprocess
from pathlib import Path

WEB = Path(__file__).parent / "web"
HARNESS = WEB / "epics_harness.js"
INDEX_HTML = (
    Path(__file__).parent.parent / "dispatcher" / "server" / "static" / "index.html"
)

_MISSING_NODE = (
    "node is a required prerequisite for verifying the epics panel's client JS — "
    "install Node (CI pins Node 22 via actions/setup-node in ci.yml's `test` job). "
    "Without it the partial-state rendering is UNVERIFIED, and that must FAIL."
)


def test_epics_panel_js_suite_passes() -> None:
    node = shutil.which("node")
    assert node is not None, _MISSING_NODE
    result = subprocess.run(
        [node, str(HARNESS), str(INDEX_HTML)], capture_output=True, text=True
    )
    assert result.returncode == 0, (
        f"epics harness failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
