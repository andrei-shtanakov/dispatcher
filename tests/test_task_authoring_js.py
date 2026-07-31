"""Runs the shipped task-authoring client JS under Node (tests/web/).

`dispatcher/server/static/index.html` is a vanilla-JS SPA with no browser test
runner wired into this repo. `tests/web/task_authoring_harness.js` loads that
file, parses its markup and runs its WHOLE `<script>` inside a VM over a
dependency-free DOM (`tests/web/dom.js`), then drives the real handlers with
real events and asserts on real state. Nothing is sliced by string markers and
no handler is simulated — an earlier version did both, and neither survives a
refactor nor catches a real break.

`tests/web/runner_selftest.js` tests the runner itself: it spawns one child
process per deliberate fault (rejected promise, throwing callback, plain
assertion mismatch, a case that throws, a crash outside every case) and
asserts each exits non-zero while a clean run exits 0. Without it, "the suite
is green" is an unverified claim about the suite.

Node is a HARD prerequisite of this gate, not an optional nicety: a missing
`node` FAILS these tests, it does not skip them. A skip is exactly how a suite
goes green while covering nothing, which is the blindness this harness exists
to remove. CI pins Node 22 via actions/setup-node in every job that runs
`uv run pytest` (see .github/workflows/ci.yml's `test` job).
"""

import shutil
import subprocess
from pathlib import Path

WEB = Path(__file__).parent / "web"
HARNESS = WEB / "task_authoring_harness.js"
SELFTEST = WEB / "runner_selftest.js"
INDEX_HTML = (
    Path(__file__).parent.parent / "dispatcher" / "server" / "static" / "index.html"
)

_MISSING_NODE = (
    "node is a required prerequisite of this test suite for verifying the "
    "task-authoring screen's client JS ({script}) — install Node (CI pins "
    "Node 22 via actions/setup-node in ci.yml's `test` job). Without it the "
    "rules named in this module's docstring are UNVERIFIED, and that must "
    "FAIL, not skip."
)


def _run(script: Path) -> subprocess.CompletedProcess[str]:
    """Run a Node script over the shipped index.html; fail if node is absent."""
    node = shutil.which("node")
    assert node is not None, _MISSING_NODE.format(script=script)
    return subprocess.run(
        [node, str(script), str(INDEX_HTML)],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _assert_clean(result: subprocess.CompletedProcess[str], label: str) -> None:
    assert result.returncode == 0, (
        f"{label} failed (exit {result.returncode}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_runner_can_actually_go_red() -> None:
    """The harness reports failure by EXIT CODE, for every kind of failure.

    This runs first on purpose: a green result from the case suite below only
    means something once the runner has been shown to fail on a rejected
    promise, a throwing callback, an assertion mismatch and a crash — and to
    pass a clean run. The version this replaced printed an unhandled
    rejection, counted it, and exited 0 anyway.
    """
    _assert_clean(_run(SELFTEST), "task-authoring runner self-test")


def test_task_authoring_js_harness() -> None:
    """The task-authoring screen's client-side rules, exercised in Node.

    If this test cannot run, the following are UNVERIFIED: null-vs-absent-vs-
    wrong-type handling for `matches`/`malformed` (a slug whose inbox could
    not be read exhaustively must not render as "free"); per-item IssueRef
    validation, including the URL scheme, on every render sink (single match,
    multi-match conflict, and the create-result link); the three-valued
    `created` handling (null must not fold into false); the 4xx-vs-5xx
    distinction on a failed create (a 5xx must not re-enable Create); and the
    entry point carrying the card's `data-dir` clone directory — never the
    project's display name — into `taRepo` and onto the wire.
    """
    _assert_clean(_run(HARNESS), "task-authoring JS harness")
