"""checkout_pinned_impresario.sh: the five-way pin-agreement gate.

Sandbox discipline of tests/test_revendor_impresario_script.py: the script
and the five vendored manifests are COPIED into tmp_path, so the real
contracts tree is never touched. Only the disagreement direction lives
here — the agreement-pass direction is proven by the live smoke, which
uses the pin only after this check succeeds.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
CONTRACT_DIRS = (
    "contracts/impresario-product-proposal/v1",
    "contracts/impresario-gate-decision/v1",
    "contracts/impresario-loop-state/v1",
    "contracts/impresario-ranked-backlog/v1",
    "contracts/impresario-loop-resume-decision/v1",
)
OTHER_PIN = "a" * 40  # valid 40-hex, guaranteed different


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    box = tmp_path / "dispatcher"
    (box / "scripts").mkdir(parents=True)
    script = box / "scripts" / "checkout_pinned_impresario.sh"
    script.write_bytes(
        (REPO_ROOT / "scripts" / "checkout_pinned_impresario.sh").read_bytes()
    )
    script.chmod(0o755)
    for rel in CONTRACT_DIRS:
        (box / rel).mkdir(parents=True)
        manifest = json.loads((REPO_ROOT / rel / "manifest.json").read_text())
        (box / rel / "manifest.json").write_text(json.dumps(manifest))
    return box


def _run(box: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(box / "scripts" / "checkout_pinned_impresario.sh")],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_loop_state_pin_disagreement_fails_before_any_checkout(
    sandbox: Path,
) -> None:
    """ONLY the loop-state manifest names another pin: exit 3 with the
    provenance message, nothing fetched, nothing printed on stdout."""
    manifest_path = sandbox / CONTRACT_DIRS[2] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["producer_commit"] = OTHER_PIN
    manifest_path.write_text(json.dumps(manifest))
    result = _run(sandbox)
    assert result.returncode == 3
    assert "disagree" in result.stderr
    assert result.stdout == ""


def test_non_hex_pin_fails_closed(sandbox: Path) -> None:
    manifest_path = sandbox / CONTRACT_DIRS[2] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["producer_commit"] = "not-a-sha"
    manifest_path.write_text(json.dumps(manifest))
    result = _run(sandbox)
    assert result.returncode == 3
    assert "40-hex" in result.stderr


def test_missing_loop_state_manifest_fails_closed(sandbox: Path) -> None:
    (sandbox / CONTRACT_DIRS[2] / "manifest.json").unlink()
    result = _run(sandbox)
    assert result.returncode == 3
    assert "could not read producer_commit" in result.stderr
