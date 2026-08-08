"""The steward gate-catalog re-vendor script, exercised offline via --from.

A deliberately small suite in the mould of tests/test_revendor_steward_script.py
(the script is an adapted copy of that one); the copy-specific risks asserted
here are the single-file surface — the pin/manifest/bytes actually land, a
commit without the catalog file is refused rather than vendored empty, and a
failed run restores the tree.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT_NAME = "revendor_steward_gate_catalog.sh"
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
    """A miniature steward: two commits over profiles/gate-catalog.yaml,
    plus a commit that predates the catalog file entirely."""
    repo = tmp_path / "steward"
    profiles = repo / "profiles"
    profiles.mkdir(parents=True)
    (repo / "README.md").write_text("steward\n")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "zero")
    before_catalog = _git(repo, "rev-parse", "HEAD")
    (profiles / "gate-catalog.yaml").write_text(
        "version: 1\nobligation_vocabulary:\n  - quality\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "one")
    first = _git(repo, "rev-parse", "HEAD")
    (profiles / "gate-catalog.yaml").write_text(
        "version: 1\nobligation_vocabulary:\n  - quality\n  - approval\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "two")
    second = _git(repo, "rev-parse", "HEAD")
    return {
        "repo": repo,
        "before_catalog": before_catalog,
        "first": first,
        "second": second,
    }


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
    dst = skeleton / "contracts" / "steward-gate-catalog" / "v1"
    manifest = json.loads((dst / "manifest.json").read_text())
    assert manifest["contract"] == "steward-gate-catalog"
    assert manifest["producer_commit"] == pin
    assert f"commit: {pin}" in (dst / "PINNED.txt").read_text()
    assert "approval" in (dst / "gate-catalog.yaml").read_text()


def test_a_commit_without_the_catalog_is_refused(
    producer: dict[str, object], skeleton: Path
) -> None:
    """`git archive` of a path the commit does not have must land on the
    documented exit 2, never on an empty-but-certified vendored copy."""
    result = _run(
        skeleton, str(producer["before_catalog"]), "--from", str(producer["repo"])
    )
    assert result.returncode == 2
    # The staging parent may remain as an empty directory; what must not
    # exist is a certified vendored copy.
    assert not (skeleton / "contracts" / "steward-gate-catalog" / "v1").exists()


def test_a_failed_run_leaves_the_previous_copy_intact(
    producer: dict[str, object], skeleton: Path
) -> None:
    repo = str(producer["repo"])
    assert _run(skeleton, str(producer["first"]), "--from", repo).returncode == 0
    dst = skeleton / "contracts" / "steward-gate-catalog" / "v1"
    before = {p.name: p.read_bytes() for p in dst.rglob("*") if p.is_file()}
    result = _run(skeleton, "0" * 40, "--from", repo)  # commit that does not exist
    assert result.returncode == 2
    after = {p.name: p.read_bytes() for p in dst.rglob("*") if p.is_file()}
    assert after == before


def test_re_vendor_moves_the_copy_to_the_new_pin(
    producer: dict[str, object], skeleton: Path
) -> None:
    repo = str(producer["repo"])
    assert _run(skeleton, str(producer["first"]), "--from", repo).returncode == 0
    dst = skeleton / "contracts" / "steward-gate-catalog" / "v1"
    assert "approval" not in (dst / "gate-catalog.yaml").read_text()
    assert _run(skeleton, str(producer["second"]), "--from", repo).returncode == 0
    assert "approval" in (dst / "gate-catalog.yaml").read_text()
    manifest = json.loads((dst / "manifest.json").read_text())
    assert manifest["producer_commit"] == str(producer["second"])
