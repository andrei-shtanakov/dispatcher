"""PF-4: plan_item_declared_closed rule + attested-only projection (ADR-ECO-005 D3)."""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.roadmap import build_roadmap


def _repo(tmp_path: Path, name: str, todo: str) -> ProjectSnapshot:
    proj = tmp_path / name
    proj.mkdir()
    (proj / "TODO.md").write_text(todo)
    return ProjectSnapshot(name=name, path=str(proj))


def _roadmap(tmp_path: Path, body: str) -> tuple[Path, ...]:
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "r.yaml").write_text(body)
    return (d,)


def _only(tmp_path: Path, body: str, snaps: list[ProjectSnapshot]):
    return build_roadmap(_roadmap(tmp_path, body), snaps).items[0]


_CLOSED = "# atp-platform\n\n- [x] Second benchmark @id:benchmark-2 @owner:tech-lead\n"
_OPEN = "# atp-platform\n\n- [ ] Second benchmark @id:benchmark-2 @owner:tech-lead\n"

_RULE = """
items:
  - id: RD-1
    title: attested
    evidence_rules:
      - rule: plan_item_declared_closed
        kind: implementation
        ref: todo://atp-platform/benchmark-2
"""


def test_closed_item_is_implemented_and_attested_only(tmp_path: Path) -> None:
    item = _only(tmp_path, _RULE, [_repo(tmp_path, "atp-platform", _CLOSED)])
    assert item.computed_status == "implemented"
    assert item.implementation_is_attested_only is True
    ev = item.evidence[0]
    assert ev.passed and ev.evidence_grade == "attestation"
    assert ev.last_seen is not None  # TODO.md mtime


def test_open_item_stays_planned_not_attested(tmp_path: Path) -> None:
    item = _only(tmp_path, _RULE, [_repo(tmp_path, "atp-platform", _OPEN)])
    assert item.computed_status == "planned"
    assert item.implementation_is_attested_only is False
    assert not item.evidence[0].passed


def test_missing_id_fails(tmp_path: Path) -> None:
    todo = "# atp-platform\n\n- [x] Other @id:elsewhere @owner:tech-lead\n"
    item = _only(tmp_path, _RULE, [_repo(tmp_path, "atp-platform", todo)])
    assert item.computed_status == "planned"
    assert "no such @id" in item.evidence[0].detail


def test_invalid_ref_fails(tmp_path: Path) -> None:
    body = _RULE.replace("todo://atp-platform/benchmark-2", "atp-platform#benchmark-2")
    item = _only(tmp_path, body, [_repo(tmp_path, "atp-platform", _CLOSED)])
    assert item.computed_status == "planned"
    assert "invalid ref" in item.evidence[0].detail


def test_missing_project_fails(tmp_path: Path) -> None:
    item = _only(tmp_path, _RULE, [])  # no atp-platform snapshot
    assert item.computed_status == "planned"
    assert "not detected" in item.evidence[0].detail


_MIXED = """
items:
  - id: RD-2
    title: mixed impl evidence
    evidence_rules:
      - rule: plan_item_declared_closed
        kind: implementation
        ref: todo://atp-platform/benchmark-2
      - rule: file_exists
        kind: implementation
        project: atp-platform
        path: marker.txt
"""


def test_mixed_machine_and_attestation_is_not_attested_only(tmp_path: Path) -> None:
    snap = _repo(tmp_path, "atp-platform", _CLOSED)
    (Path(snap.path) / "marker.txt").write_text("x")
    item = _only(tmp_path, _MIXED, [snap])
    assert item.computed_status == "implemented"
    # one impl rule is machine-grade -> not attested-only
    assert item.implementation_is_attested_only is False


_VERIFIED = """
items:
  - id: RD-3
    title: verified over attested impl
    evidence_rules:
      - rule: plan_item_declared_closed
        kind: implementation
        ref: todo://atp-platform/benchmark-2
      - rule: file_exists
        kind: verification
        project: atp-platform
        path: marker.txt
"""


def test_verified_over_attested_impl_stays_attested_only(tmp_path: Path) -> None:
    snap = _repo(tmp_path, "atp-platform", _CLOSED)
    (Path(snap.path) / "marker.txt").write_text("x")
    item = _only(tmp_path, _VERIFIED, [snap])
    assert item.computed_status == "verified"
    # implementation evidence is all attestation, so provenance is preserved
    assert item.implementation_is_attested_only is True
