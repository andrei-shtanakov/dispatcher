"""The steward gate-verdicts re-vendor script, exercised offline via --from.

A deliberately smaller suite than tests/test_revendor_script.py: the script
is an adapted copy of the github-checker one (that suite pins its script's
constants by content, so the body could not be shared), and the copy-specific
risks are what is asserted here — the pin/manifest/bytes actually land, a
file upstream deleted does not survive, and a failed run restores the tree.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT_NAME = "revendor_steward_gate_verdicts.sh"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    """Run one git command in `repo`, identity supplied by env, not config."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_GIT_ENV},
    )
    return result.stdout.strip()


@pytest.fixture
def producer(tmp_path: Path) -> dict[str, object]:
    """A miniature steward: two commits over contracts/gate-verdicts/v1.

    The second commit drops a fixture the first had — the file-deleted-
    upstream case is what a copy-over-the-top re-vendor gets wrong.
    """
    repo = tmp_path / "steward"
    src = repo / "contracts" / "gate-verdicts" / "v1"
    (src / "fixtures").mkdir(parents=True)
    (src / "SCHEMA.json").write_text('{"title": "v1"}\n')
    (src / "fixtures" / "clean.jsonl").write_text('{"kind": "header"}\n')
    (src / "fixtures" / "dropped.jsonl").write_text('{"kind": "header"}\n')
    _git(repo, "init", "--quiet")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "one")
    first = _git(repo, "rev-parse", "HEAD")
    (src / "fixtures" / "dropped.jsonl").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "two")
    second = _git(repo, "rev-parse", "HEAD")
    return {"repo": repo, "first": first, "second": second}


@pytest.fixture
def skeleton(tmp_path: Path) -> Path:
    """A copy of the minimal dispatcher layout the script needs."""
    root = tmp_path / "dispatcher"
    (root / "scripts").mkdir(parents=True)
    (root / "contracts").mkdir()
    for name in (SCRIPT_NAME, "vendor_manifest.py"):
        shutil.copy(REPO_ROOT / "scripts" / name, root / "scripts" / name)
    return root


def _run(skeleton: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(skeleton / "scripts" / SCRIPT_NAME), *args],
        capture_output=True,
        text=True,
    )


def test_happy_path_writes_pin_manifest_and_bytes(
    producer: dict[str, object], skeleton: Path
) -> None:
    pin = str(producer["second"])
    result = _run(skeleton, pin, "--from", str(producer["repo"]))
    assert result.returncode == 0, result.stderr
    dst = skeleton / "contracts" / "steward-gate-verdicts" / "v1"
    manifest = json.loads((dst / "manifest.json").read_text())
    assert manifest["contract"] == "steward-gate-verdicts"
    assert manifest["producer_commit"] == pin
    assert f"commit: {pin}" in (dst / "PINNED.txt").read_text()
    assert (dst / "SCHEMA.json").exists()
    assert (dst / "fixtures" / "clean.jsonl").exists()


def test_a_file_upstream_deleted_does_not_survive(
    producer: dict[str, object], skeleton: Path
) -> None:
    repo = str(producer["repo"])
    assert _run(skeleton, str(producer["first"]), "--from", repo).returncode == 0
    dst = skeleton / "contracts" / "steward-gate-verdicts" / "v1"
    assert (dst / "fixtures" / "dropped.jsonl").exists()
    assert _run(skeleton, str(producer["second"]), "--from", repo).returncode == 0
    assert not (dst / "fixtures" / "dropped.jsonl").exists()


def test_a_failed_run_leaves_the_previous_copy_intact(
    producer: dict[str, object], skeleton: Path
) -> None:
    repo = str(producer["repo"])
    assert _run(skeleton, str(producer["first"]), "--from", repo).returncode == 0
    dst = skeleton / "contracts" / "steward-gate-verdicts" / "v1"
    before = sorted(p.name for p in dst.rglob("*") if p.is_file())
    result = _run(skeleton, "0" * 40, "--from", repo)  # commit that does not exist
    assert result.returncode == 2
    assert sorted(p.name for p in dst.rglob("*") if p.is_file()) == before
