"""Acceptance from inbox #129, run on the pinned copy of the real PP-101.

1. ready_for_business copy with GD-001 REMOVED (deleted — not marked, not
   corrupted; the other decision retained) -> exactly one gate_waiting
   record (Gate A, business_owner, proposal://PP-101).
2. The true approved copy -> ok, zero waits.
3. A copy with an unreadable decision file -> unknown, NOT «zero waits».
"""

from __future__ import annotations

import json
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


def _mutate_loop(bundle: Path, **changes: object) -> None:
    state = json.loads((bundle / "loop.state").read_text())
    stop = state["stop"]
    if changes.get("stop", "keep") is None:
        state["stop"] = None
    else:
        stop.update({k: v for k, v in changes.items() if k != "stop"})
    (bundle / "loop.state").write_text(json.dumps(state))


def test_acceptance_p2_needs_human_mutation_yields_one_wait(
    tmp_path: Path,
) -> None:
    """#136: stop.verdict -> needs_human => exactly one record with identity
    (LOOP-101, 2) and freshness from stop.at."""
    mirror, bundle = _mirror_with_pp101(tmp_path)
    _mutate_loop(bundle, verdict="needs_human", reason="ждём человека")
    report = collect_product_proposals(mirror)
    assert [b.loop_status for b in report.bundles] == ["needs_human"]
    assert [
        (w.loop_id, w.iteration, w.reason, w.stopped_at) for w in report.needs_human
    ] == [("LOOP-101", 2, "ждём человека", "2026-08-12T04:01:21Z")]
    assert report.attention is False


def test_acceptance_p2_true_pp101_is_terminal_with_zero_loop_waits(
    tmp_path: Path,
) -> None:
    mirror, _ = _mirror_with_pp101(tmp_path)
    report = collect_product_proposals(mirror)
    assert [b.loop_status for b in report.bundles] == ["ready_for_business"]
    assert report.needs_human == [] and report.attention is False


def test_acceptance_p2_stop_null_is_running_no_wait(tmp_path: Path) -> None:
    mirror, bundle = _mirror_with_pp101(tmp_path)
    _mutate_loop(bundle, stop=None)
    report = collect_product_proposals(mirror)
    assert [b.loop_status for b in report.bundles] == ["running"]
    assert report.needs_human == []


def test_acceptance_p2_deleted_loop_state_keeps_phase1_working(
    tmp_path: Path,
) -> None:
    mirror, bundle = _mirror_with_pp101(tmp_path)
    (bundle / "loop.state").unlink()
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.bundles] == ["ok"]
    assert [b.loop_status for b in report.bundles] == ["absent"]
    assert report.needs_human == [] and report.attention is False


def test_acceptance_p2_invalid_loop_state_suppresses_both(
    tmp_path: Path,
) -> None:
    mirror, bundle = _mirror_with_pp101(tmp_path)
    (bundle / "loop.state").write_bytes(b"\xff\xfe")
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.bundles] == ["unknown"]
    assert [b.loop_status for b in report.bundles] == ["unknown"]
    assert report.bundles[0].waits == [] and report.bundles[0].loop_waits == []
    assert report.needs_human == [] and report.attention is True


def test_acceptance_p2_mismatched_loop_state_suppresses_both(
    tmp_path: Path,
) -> None:
    mirror, bundle = _mirror_with_pp101(tmp_path)
    state = json.loads((bundle / "loop.state").read_text())
    state["proposal_id"] = "PP-999"
    (bundle / "loop.state").write_text(json.dumps(state))
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.bundles] == ["unknown"]
    codes = [d.code for d in report.bundles[0].diagnostics]
    assert codes == ["loop-state-proposal-mismatch"]
    assert report.needs_human == [] and report.bundles[0].waits == []
