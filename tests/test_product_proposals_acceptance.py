"""Acceptance from inbox #129, run on the pinned copy of the real PP-101.

1. ready_for_business copy with GD-001 REMOVED (deleted — not marked, not
   corrupted; the other decision retained) -> exactly one gate_waiting
   record (Gate A, business_owner, proposal://PP-101).
2. The true approved copy -> ok, zero waits.
3. A copy with an unreadable decision file -> unknown, NOT «zero waits».
"""

from __future__ import annotations

import shutil
from pathlib import Path

from dispatcher.core.product_proposals import (
    ANCHOR_FILES,
    collect_product_proposals,
)

PP101 = Path(__file__).parent / "fixtures" / "product_proposals" / "pp-101"


def _mirror_with_pp101(tmp_path: Path) -> tuple[Path, Path]:
    mirror = tmp_path / "impresario"
    for rel in ANCHOR_FILES:
        p = mirror / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    bundle = mirror / "pilot" / "forconcept" / "pp-101"
    shutil.copytree(PP101, bundle)
    (bundle / "PROVENANCE.txt").unlink()  # fixture metadata, not bundle content
    return mirror, bundle


def test_acceptance_1_ready_for_business_without_gd001_waits_for_gate_a(
    tmp_path: Path,
) -> None:
    mirror, bundle = _mirror_with_pp101(tmp_path)
    proposal = (bundle / "proposal.yaml").read_text()
    (bundle / "proposal.yaml").write_text(
        proposal.replace("status: approved", "status: ready_for_business")
    )
    (bundle / "decisions" / "gd-001.yaml").unlink()  # removed, not corrupted
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.bundles] == ["ok"]
    assert [
        (w.gate_id, w.gate_label, w.authority, w.artifact_ref) for w in report.waits
    ] == [("qg5_business", "Gate A", "business_owner", "proposal://PP-101")]
    assert report.waits[0].bundle_path == "pilot/forconcept/pp-101"
    assert report.attention is False


def test_acceptance_2_true_approved_bundle_has_zero_waits(
    tmp_path: Path,
) -> None:
    mirror, _ = _mirror_with_pp101(tmp_path)
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.bundles] == ["ok"]
    assert [b.status for b in report.bundles] == ["approved"]
    assert report.waits == [] and report.attention is False


def test_acceptance_3_unreadable_decision_is_unknown_not_zero_waits(
    tmp_path: Path,
) -> None:
    mirror, bundle = _mirror_with_pp101(tmp_path)
    (bundle / "decisions" / "gd-001.yaml").write_bytes(b"\xff\xfe not utf-8")
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.bundles] == ["unknown"]
    assert [d.code for d in report.bundles[0].diagnostics] == ["decision-unreadable"]
    assert report.bundles[0].waits == []  # suppressed, not «nothing waits»
    assert report.attention is True
