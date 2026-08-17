"""Phase 3 (inbox #154): the QG-4 backlog-level wait.

Acceptance runs on the pinned copy of the real pilot backlog (BL-ecosystem
v4 + gd-001/gd-002) — the exact live state the issue cites: selectable items
and no active QG-4 decision on the current version. Scenario mutations are
built in tmp_path from the pinned copy, so acceptance reads literally like
issue #154.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from dispatcher.core.product_proposals import (
    ANCHOR_FILES,
    collect_product_proposals,
)

BACKLOG_FIXTURE = Path(__file__).parent / "fixtures" / "product_proposals"
BACKLOG_FIXTURE = BACKLOG_FIXTURE / "pilot-backlog"


def _mirror_with_backlog(tmp_path: Path) -> tuple[Path, Path]:
    mirror = tmp_path / "impresario"
    for rel in ANCHOR_FILES:
        p = mirror / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    root = mirror / "pilot"
    shutil.copytree(BACKLOG_FIXTURE, root)
    (root / "PROVENANCE.txt").unlink()  # fixture metadata, not mirror content
    return mirror, root


def _gd(
    decision_id: str = "GD-003",
    version: int = 4,
    decision: str = "select",
    ref: str = "backlog://BL-ecosystem",
    supersedes: str | None = None,
) -> str:
    selected = "selected_idea_ref: idea://IDEA-103\n" if decision == "select" else ""
    supersede = f"supersedes: gate-decision://{supersedes}\n" if supersedes else ""
    return (
        f"decision_id: {decision_id}\n"
        "gate_id: qg4_backlog\n"
        "subject:\n"
        "  kind: ranked_backlog\n"
        f"  ref: {ref}\n"
        f"  version: {version}\n"
        f"decision: {decision}\n"
        f"{selected}"
        f"{supersede}"
        "decided_by:\n"
        "  kind: human\n"
        "  id: andrei\n"
        "  role: qg4_selector\n"
        "decided_at: '2026-08-17T10:00:00Z'\n"
        "reason: test decision\n"
    )


# --- Acceptance (verbatim from issue #154) ---------------------------------


def test_acceptance_1_live_v4_yields_exactly_one_wait(tmp_path: Path) -> None:
    """Current mirror state: v4 selectable, no QG-4 decision on v4 → exactly
    one wait with identity (BL-ecosystem, 4) and freshness = updated_at."""
    mirror, _ = _mirror_with_backlog(tmp_path)
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.backlog_bundles] == ["ok"]
    assert [
        (w.backlog_id, w.version, w.gate_id, w.gate_label, w.authority)
        for w in report.backlog_waits
    ] == [("BL-ecosystem", 4, "qg4_backlog", "QG-4", "qg4_selector")]
    wait = report.backlog_waits[0]
    assert wait.artifact_ref == "backlog://BL-ecosystem"
    assert wait.artifact_path == "pilot/backlog.yaml"
    assert wait.backlog_updated_at == "2026-08-17T02:42:23Z"
    # ranks 3-7 of the live v4 are status: new — context, not identity
    assert wait.selectable_idea_refs == [
        "idea://IDEA-103",
        "idea://IDEA-106",
        "idea://IDEA-107",
        "idea://IDEA-102",
        "idea://IDEA-108",
    ]
    assert report.attention is False  # waiting is business work, not a defect


@pytest.mark.parametrize("outcome", ["select", "defer", "park", "reject"])
def test_acceptance_2_any_active_v4_outcome_yields_zero_waits(
    tmp_path: Path, outcome: str
) -> None:
    """The wait is extinguished by ANY QG-4 outcome, not only select."""
    mirror, root = _mirror_with_backlog(tmp_path)
    (root / "decisions" / "gd-003.yaml").write_text(_gd(decision=outcome))
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.backlog_bundles] == ["ok"]
    assert report.backlog_waits == []


def test_acceptance_3_version_bump_reopens_the_wait(tmp_path: Path) -> None:
    """v5 published with selectable items and no v5 decision → one wait with
    the NEW identity (BL-ecosystem, 5), even though v4 was decided."""
    mirror, root = _mirror_with_backlog(tmp_path)
    (root / "decisions" / "gd-003.yaml").write_text(_gd(version=4))
    backlog = (root / "backlog.yaml").read_text()
    (root / "backlog.yaml").write_text(backlog.replace("version: 4", "version: 5", 1))
    report = collect_product_proposals(mirror)
    assert [(w.backlog_id, w.version) for w in report.backlog_waits] == [
        ("BL-ecosystem", 5)
    ]


def test_acceptance_4_unreadable_backlog_is_unknown_not_zero_waits(
    tmp_path: Path,
) -> None:
    mirror, root = _mirror_with_backlog(tmp_path)
    (root / "backlog.yaml").write_bytes(b"\xff\xfe not utf-8")
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.backlog_bundles] == ["unreadable"]
    assert [d.code for d in report.backlog_bundles[0].diagnostics] == [
        "backlog-unreadable"
    ]
    assert report.backlog_bundles[0].waits == []  # suppressed, not «no wait»
    assert report.attention is True


# --- Classification semantics ----------------------------------------------


def test_no_selectable_items_means_no_wait_without_any_decision(
    tmp_path: Path,
) -> None:
    mirror, root = _mirror_with_backlog(tmp_path)
    backlog = (root / "backlog.yaml").read_text()
    (root / "backlog.yaml").write_text(backlog.replace("status: new", "status: parked"))
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.backlog_bundles] == ["ok"]
    assert report.backlog_waits == []


def test_under_review_is_still_selectable(tmp_path: Path) -> None:
    """Owner-fixed policy: an item a human is looking at is still undecided —
    the wait stays open."""
    mirror, root = _mirror_with_backlog(tmp_path)
    backlog = (root / "backlog.yaml").read_text()
    (root / "backlog.yaml").write_text(
        backlog.replace("status: new", "status: under_review")
    )
    report = collect_product_proposals(mirror)
    assert [w.version for w in report.backlog_waits] == [4]


def test_superseded_v4_decision_does_not_extinguish(tmp_path: Path) -> None:
    """gd-003 decides v4 but gd-004 (deciding v3) supersedes it: v4 has no
    active decision again, the wait is back."""
    mirror, root = _mirror_with_backlog(tmp_path)
    (root / "decisions" / "gd-003.yaml").write_text(_gd(version=4))
    (root / "decisions" / "gd-004.yaml").write_text(
        _gd(decision_id="GD-004", version=3, supersedes="GD-003")
    )
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.backlog_bundles] == ["ok"]
    assert [w.version for w in report.backlog_waits] == [4]


def test_decision_for_another_backlog_does_not_extinguish(tmp_path: Path) -> None:
    mirror, root = _mirror_with_backlog(tmp_path)
    (root / "decisions" / "gd-003.yaml").write_text(_gd(ref="backlog://BL-other"))
    report = collect_product_proposals(mirror)
    assert [w.version for w in report.backlog_waits] == [4]


def test_unreadable_decision_suppresses_the_backlog_wait(tmp_path: Path) -> None:
    mirror, root = _mirror_with_backlog(tmp_path)
    (root / "decisions" / "gd-003.yaml").write_bytes(b"\xff\xfe not utf-8")
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.backlog_bundles] == ["unknown"]
    assert report.backlog_bundles[0].waits == []
    assert report.backlog_waits == []
    assert report.attention is True


def test_invalid_backlog_schema_is_unreadable(tmp_path: Path) -> None:
    mirror, root = _mirror_with_backlog(tmp_path)
    (root / "backlog.yaml").write_text("id: BL-ecosystem\n")  # misses required
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.backlog_bundles] == ["unreadable"]
    assert [d.code for d in report.backlog_bundles[0].diagnostics] == [
        "backlog-schema-invalid"
    ]


def test_backlog_id_conflict_marks_all_roots(tmp_path: Path) -> None:
    mirror, root = _mirror_with_backlog(tmp_path)
    other = mirror / "elsewhere"
    other.mkdir()
    shutil.copy(root / "backlog.yaml", other / "backlog.yaml")
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.backlog_bundles] == ["conflict", "conflict"]
    assert report.backlog_waits == []
    for bundle in report.backlog_bundles:
        assert [d.code for d in bundle.diagnostics] == ["backlog-id-conflict"]
        assert "elsewhere, pilot" in bundle.diagnostics[0].message
    assert report.attention is True


# --- loop-resume-decision awareness ----------------------------------------


def test_lrd_record_in_decisions_is_recognized_and_ignored(tmp_path: Path) -> None:
    """The live pp-101 grew decisions/lrd-001.yaml — a loop-resume-decision.
    It must NOT push the bundle into unknown (the pre-fix behaviour)."""
    mirror, root = _mirror_with_backlog(tmp_path)
    (root / "decisions" / "lrd-001.yaml").write_text(
        "decision_id: LRD-001\n"
        "subject:\n"
        "  loop_id: LOOP-101\n"
        "  iteration: 1\n"
        "new_max_iterations: 3\n"
        "decided_by:\n"
        "  kind: human\n"
        "  id: andrei\n"
        "decided_at: '2026-08-12T04:01:21Z'\n"
        "reason: resume authorization\n"
    )
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.backlog_bundles] == ["ok"]
    assert [w.version for w in report.backlog_waits] == [4]  # still waiting


def test_record_matching_neither_schema_is_still_invalid(tmp_path: Path) -> None:
    mirror, root = _mirror_with_backlog(tmp_path)
    (root / "decisions" / "gd-003.yaml").write_text("decision_id: NOPE-1\n")
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.backlog_bundles] == ["unknown"]
    diag = report.backlog_bundles[0].diagnostics[0]
    assert diag.code == "decision-schema-invalid"
    assert "gate-decision/v1" in diag.message
    assert "loop-resume-decision/v1" in diag.message
