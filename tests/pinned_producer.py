"""Resolve the `github-checker` on PATH and prove it is the vendored pin.

Test-only. Shipped code never reaches for the producer's repository or its
install metadata — the whole point of vendoring is that the running
dispatcher needs neither.

The producer publishes no `--version` flag and no commit anywhere in its
output, so "which github-checker is this?" cannot be asked of the binary.
It can be asked of the *install*: PEP 610 makes a VCS install record the
commit it resolved to, in `direct_url.json`. That record is written by the
installer, not by the package, so a checkout renamed to look like the pin
cannot forge it.

Every failure here raises. Nothing skips: a level-3 smoke that quietly
declines to run is the defect this module exists to remove.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

VENDORED_ROOT = (
    Path(__file__).parent.parent / "contracts" / "github-checker-actions" / "v1"
)

# Read from the manifest rather than PINNED.txt: the manifest's copy is
# already asserted against the literal in test_contract_ingest.py, so the
# pin has one checked source. test_pinned_producer.py holds PINNED.txt to
# the same value so the human-readable record cannot drift away from it.
PRODUCER_COMMIT: str = json.loads((VENDORED_ROOT / "manifest.json").read_text())[
    "producer_commit"
]

_PROBE = (
    "import importlib.metadata as m;"
    "print(m.distribution('github-checker').read_text('direct_url.json') or '', end='')"
)


class ProducerBinaryProblem(AssertionError):
    """The binary on PATH is absent, unidentifiable, or the wrong commit.

    An `AssertionError` on purpose: this is a test failure, never a reason
    to skip.
    """


def commit_of_direct_url(record: str) -> str:
    """The commit a PEP 610 record *proves*, or a refusal.

    Reads `commit_id` (what the installer resolved), never
    `requested_revision` (what someone asked for) — `@master` requests
    resolve to whatever master happened to be, and that difference is the
    drift this check exists to catch.
    """
    if not record.strip():
        raise ProducerBinaryProblem(
            "the installed github-checker carries no PEP 610 record, so the "
            "commit it was built from cannot be established"
        )
    try:
        parsed = json.loads(record)
    except ValueError as exc:
        raise ProducerBinaryProblem(
            f"the installed github-checker's PEP 610 record is unreadable: {exc.args[0]}"
        ) from None
    commit = None
    if isinstance(parsed, dict):
        vcs_info = parsed.get("vcs_info")
        if isinstance(vcs_info, dict):
            commit = vcs_info.get("commit_id")
    if not isinstance(commit, str) or not commit:
        raise ProducerBinaryProblem(
            "the installed github-checker is not a pinned VCS install "
            "(an editable checkout or a built wheel proves no commit); "
            "install it from a git revision instead"
        )
    return commit


def installed_commit(binary: Path) -> str:
    """Ask the interpreter beside `binary` which commit it installed."""
    interpreter = binary.parent / "python"
    if not interpreter.exists():
        raise ProducerBinaryProblem(
            f"no interpreter beside {binary}, so its install metadata cannot "
            "be read; install github-checker into a virtualenv and put that "
            "virtualenv's bin on PATH"
        )
    probe = subprocess.run(
        [str(interpreter), "-c", _PROBE], capture_output=True, text=True
    )
    if probe.returncode != 0:
        raise ProducerBinaryProblem(
            f"{interpreter} could not report github-checker's install metadata "
            f"(exit {probe.returncode})"
        )
    return commit_of_direct_url(probe.stdout)


def pinned_producer_binary() -> Path:
    """The `github-checker` on PATH, proven to be `PRODUCER_COMMIT`."""
    found = shutil.which("github-checker")
    if found is None:
        raise ProducerBinaryProblem(
            "no `github-checker` on PATH: run scripts/install_pinned_checker.sh "
            "and put the directory it prints on PATH. This is a failure, not a "
            "skip — level 3 is the only step that exercises the real binary."
        )
    binary = Path(found).resolve()
    commit = installed_commit(binary)
    if commit != PRODUCER_COMMIT:
        raise ProducerBinaryProblem(
            f"{binary} was installed from {commit}, but the vendored contract "
            f"is pinned to {PRODUCER_COMMIT}. Re-install at the pin, or "
            "re-vendor the contract at the newer commit — running level 3 "
            "against a different producer proves nothing about this copy."
        )
    return binary
