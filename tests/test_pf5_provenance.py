"""PF-5: attested provenance reaches every surface (ADR-ECO-005 D3).

The status engine is unchanged (attestation still promotes to implemented);
PF-5 only guarantees the `(attested)` marker travels through the one shared
rendering (`display_status` / `status_label`) so no surface can silently show
an owner-attested item as machine-backed.
"""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.roadmap import build_roadmap, display_status

_CLOSED = "# atp-platform\n\n- [x] Second benchmark @id:benchmark-2 @owner:tech-lead\n"

_RULE = """
items:
  - id: RD-1
    title: attested
    evidence_rules:
      - rule: plan_item_declared_closed
        kind: implementation
        ref: todo://atp-platform/benchmark-2
"""

_MACHINE = """
items:
  - id: RD-2
    title: machine
    evidence_rules:
      - rule: file_exists
        kind: implementation
        project: atp-platform
        path: marker.txt
"""


def _repo(tmp_path: Path, todo: str) -> ProjectSnapshot:
    proj = tmp_path / "atp-platform"
    proj.mkdir()
    (proj / "TODO.md").write_text(todo)
    return ProjectSnapshot(name="atp-platform", path=str(proj))


def _only(tmp_path: Path, body: str, snap: ProjectSnapshot):
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "r.yaml").write_text(body)
    return build_roadmap((d,), [snap]).items[0]


def test_display_status_appends_marker_only_when_attested() -> None:
    assert display_status("implemented", True) == "implemented (attested)"
    assert display_status("verified", True) == "verified (attested)"
    assert display_status("implemented", False) == "implemented"
    assert display_status("planned", False) == "planned"


def test_status_label_folds_marker_for_attested_item(tmp_path: Path) -> None:
    item = _only(tmp_path, _RULE, _repo(tmp_path, _CLOSED))
    assert item.computed_status == "implemented"
    assert item.implementation_is_attested_only is True
    assert item.status_label == "implemented (attested)"


def test_status_label_is_plain_for_machine_item(tmp_path: Path) -> None:
    snap = _repo(tmp_path, _CLOSED)
    (Path(snap.path) / "marker.txt").write_text("x")
    item = _only(tmp_path, _MACHINE, snap)
    assert item.computed_status == "implemented"
    assert item.implementation_is_attested_only is False
    assert item.status_label == "implemented"


def test_provenance_survives_serialization(tmp_path: Path) -> None:
    """The API (pydantic response_model) must carry both the flag and the
    rendered label so web/VSCode/KB/Robin consume one authoritative value."""
    item = _only(tmp_path, _RULE, _repo(tmp_path, _CLOSED))
    dumped = item.model_dump()
    assert dumped["status_label"] == "implemented (attested)"
    assert dumped["implementation_is_attested_only"] is True
    # computed_status is preserved verbatim next to the rendered label
    assert dumped["computed_status"] == "implemented"
