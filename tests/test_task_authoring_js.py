"""Runs the shipped task-authoring client JS under Node (tests/web/).

`dispatcher/server/static/index.html` is a vanilla-JS SPA with no browser
test runner wired into this repo. `tests/web/task_authoring_harness.js`
line-slices the actual shipped <script> content and drives it under a stub
DOM with a scripted fetch queue (see that file's header comment).

Node is a HARD prerequisite of this gate, not an optional nicety: a missing
`node` FAILS this test, it does not skip it. A skip is exactly how a suite
goes green while covering nothing, which is the blindness this harness
exists to remove. CI pins Node 22 via actions/setup-node in every job that
runs `uv run pytest` (see .github/workflows/ci.yml's `test` job).
"""

import shutil
import subprocess
from pathlib import Path

HARNESS = Path(__file__).parent / "web" / "task_authoring_harness.js"
INDEX_HTML = (
    Path(__file__).parent.parent / "dispatcher" / "server" / "static" / "index.html"
)


def test_task_authoring_js_harness() -> None:
    """The task-authoring screen's client-side rules, exercised in Node.

    If this test cannot run, the following are UNVERIFIED: null-vs-absent-
    vs-wrong-type handling for `matches`/`malformed` (a taken slug that
    could not be read exhaustively must not render as "free"); per-item
    IssueRef validation, including URL scheme, on every render sink (single
    match, multi-match conflict, and the create-result link); the
    three-valued `created` handling (null must not fold into false); the
    4xx-vs-5xx distinction on a failed create (a 5xx must not re-enable
    Create); and the data-dir-not-display-name entry point.
    """
    node = shutil.which("node")
    assert node is not None, (
        "node is a required prerequisite of this test suite for verifying "
        f"the task-authoring screen's client JS ({HARNESS}) — install "
        "Node (CI pins Node 22 via actions/setup-node in ci.yml's `test` "
        "job). Without it, the rules documented in this test's docstring "
        "are UNVERIFIED — this must FAIL, not skip."
    )
    result = subprocess.run(
        [node, str(HARNESS), str(INDEX_HTML)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"task-authoring JS harness failed (exit {result.returncode}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )


def test_task_authoring_js_harness_reports_a_thrown_case_as_failure() -> None:
    """Regression guard for a defect the harness itself once had.

    An uncaught exception inside one case's `drive()` used to propagate out
    of the whole run, skipping the harness's own summary/exit-code lines
    entirely — and because the harness installs an `unhandledRejection`
    listener (to COUNT rejections), that same listener suppresses Node's
    default crash-on-unhandled-rejection behaviour, so the process just
    drained its event loop and exited 0. The harness printed the rejection
    and even counted it, but nothing read that counter before the process
    ended: a run that crashed mid-case reported success.

    `--self-check-throw` appends one case whose `drive()` deliberately
    throws, run through the exact same per-case loop machinery as the 20
    real cases (not a reimplementation elsewhere). This test is the
    black-box half of that regression guard: it does not care how the
    harness catches the throw, only that the PROCESS exit code is non-zero
    when a case throws — the same signal CI reads.
    """
    node = shutil.which("node")
    assert node is not None, (
        "node is a required prerequisite of this test suite — see "
        "test_task_authoring_js_harness's assertion message for the "
        "full list of what goes unverified without it."
    )
    result = subprocess.run(
        [node, str(HARNESS), str(INDEX_HTML), "--self-check-throw"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0, (
        "the harness must exit non-zero when a case's drive() throws — it "
        f"did not (exit {result.returncode}), which is the exact defect "
        "this test guards against:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
