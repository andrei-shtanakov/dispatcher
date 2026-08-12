"""The impresario re-vendor script, exercised offline via --from.

Copy-specific risks asserted here: the pin/manifests/bytes land in BOTH
directories, a file upstream deleted does not survive, one bad source
subdir means NEITHER directory changes, and a failed run restores the tree.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
DSTS = (
    "contracts/impresario-product-proposal/v1",
    "contracts/impresario-gate-decision/v1",
)
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


@pytest.fixture
def producer(tmp_path: Path) -> dict[str, object]:
    """A miniature impresario: two commits over BOTH contract subdirs.

    The second commit drops a fixture the first had — the file-deleted-
    upstream case a copy-over-the-top re-vendor gets wrong.
    """
    repo = tmp_path / "impresario"
    pp = repo / "contracts" / "product-proposal" / "v1"
    gd = repo / "contracts" / "gate-decision" / "v1"
    (pp / "fixtures").mkdir(parents=True)
    (gd / "fixtures").mkdir(parents=True)
    (pp / "schema.json").write_text('{"title": "pp"}\n')
    (pp / "fixtures" / "ok.yaml").write_text("a: 1\n")
    (pp / "fixtures" / "dropped.yaml").write_text("b: 2\n")
    (gd / "schema.json").write_text('{"title": "gd"}\n')
    (gd / "fixtures" / "ok.yaml").write_text("c: 3\n")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "one")
    first = _git(repo, "rev-parse", "HEAD")
    (pp / "fixtures" / "dropped.yaml").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "two")
    second = _git(repo, "rev-parse", "HEAD")
    return {"repo": repo, "first": first, "second": second}


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A copy of the repo skeleton the script needs, so the REAL vendored
    directories are never touched by this test."""
    box = tmp_path / "dispatcher"
    (box / "scripts").mkdir(parents=True)
    for name in ("revendor_impresario_contracts.sh", "vendor_manifest.py"):
        src = REPO_ROOT / "scripts" / name
        dst = box / "scripts" / name
        dst.write_bytes(src.read_bytes())
        dst.chmod(0o755)
    for rel in DSTS:
        (box / rel).mkdir(parents=True)
        (box / rel / "stale.txt").write_text("previous copy\n")
    return box


def _run(box: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(box / "scripts" / "revendor_impresario_contracts.sh"), *args],
        capture_output=True,
        text=True,
    )


def test_both_directories_land_with_the_same_pin(
    producer: dict[str, object], sandbox: Path
) -> None:
    result = _run(sandbox, str(producer["second"]), "--from", str(producer["repo"]))
    assert result.returncode == 0, result.stderr
    pins = set()
    for rel in DSTS:
        manifest = json.loads((sandbox / rel / "manifest.json").read_text())
        pins.add(manifest["producer_commit"])
        assert not (sandbox / rel / "stale.txt").exists()
    assert pins == {producer["second"]}


def test_upstream_deleted_file_does_not_survive(
    producer: dict[str, object], sandbox: Path
) -> None:
    assert (
        _run(
            sandbox, str(producer["first"]), "--from", str(producer["repo"])
        ).returncode
        == 0
    )
    assert (
        _run(
            sandbox, str(producer["second"]), "--from", str(producer["repo"])
        ).returncode
        == 0
    )
    pp = sandbox / DSTS[0]
    assert not (pp / "fixtures" / "dropped.yaml").exists()


def test_failure_restores_both_previous_copies(
    producer: dict[str, object], sandbox: Path, tmp_path: Path
) -> None:
    """A commit that has product-proposal/v1 but NO gate-decision/v1: the
    second extraction fails, and neither live directory may have changed."""
    repo = tmp_path / "half"
    pp = repo / "contracts" / "product-proposal" / "v1"
    pp.mkdir(parents=True)
    (pp / "schema.json").write_text("{}\n")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "half")
    half = _git(repo, "rev-parse", "HEAD")
    result = _run(sandbox, half, "--from", str(repo))
    assert result.returncode != 0
    for rel in DSTS:
        assert (sandbox / rel / "stale.txt").read_text() == "previous copy\n"
        assert not (sandbox / rel).with_suffix(".staging").exists()


def test_rejects_a_non_full_sha(sandbox: Path) -> None:
    result = _run(sandbox, "main")
    assert result.returncode == 1
