"""Tests for the roadmap read-model (typed evidence rules + API)."""

import sqlite3
from pathlib import Path

import httpx
import pytest
from conftest import make_arbiter, make_atp, make_maestro, make_maestro_home

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.models import ProjectSnapshot, TaskInfo
from dispatcher.core.roadmap import build_roadmap, default_roadmap_dirs
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio

_ROADMAP = """
version: 1
roadmap: test-v1
title: Test roadmap
items:
  - id: RD-A
    title: Implemented and verified item
    phase: "1"
    owner_project: arbiter
    evidence_rules:
      - rule: project_detected
        kind: implementation
        project: arbiter
      - rule: work_item_chain
        kind: verification
        work_item_id: T-9
        min_links: 2

  - id: RD-B
    title: Planned item (evidence missing)
    phase: "2"
    owner_project: arbiter
    evidence_rules:
      - rule: file_exists
        kind: implementation
        project: arbiter
        path: contracts/nope/schema.json

  - id: RD-C
    title: Blocked item (planned + unfinished dependency)
    phase: "3"
    depends_on: [RD-B]
    evidence_rules:
      - rule: file_exists
        kind: implementation
        project: arbiter
        path: contracts/also-nope.json

  - id: RD-D
    title: No machine rules yet
    phase: "9"
    expected_evidence:
      - prose only
    evidence_rules: []

  - id: RD-E
    title: Unknown rule name stays failed, not crashed
    phase: "9"
    evidence_rules:
      - rule: teleport_check
        kind: implementation
"""


def _snapshots() -> list[ProjectSnapshot]:
    def task(task_id: str, status: str) -> TaskInfo:
        return TaskInfo(task_id=task_id, status=status, source="test")

    return [
        ProjectSnapshot(name="arbiter", path="/tmp/x", tasks=[task("T-9", "assign")]),
        ProjectSnapshot(name="Maestro", path="", tasks=[task("T-9", "done")]),
    ]


def _write_roadmap(tmp_path: Path) -> Path:
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "test-v1.yaml").write_text(_ROADMAP)
    return d


def test_status_ladder(tmp_path: Path) -> None:
    result = build_roadmap((_write_roadmap(tmp_path),), _snapshots())
    status = {i.id: i.computed_status for i in result.items}
    assert status == {
        "RD-A": "verified",
        "RD-B": "planned",
        "RD-C": "blocked",
        "RD-D": "unknown",
        "RD-E": "planned",
    }
    blocked = next(i for i in result.items if i.id == "RD-C")
    assert blocked.blockers == ["RD-B"]
    unknown_rule = next(i for i in result.items if i.id == "RD-E")
    assert "unknown rule" in unknown_rule.evidence[0].detail


def test_missing_dir_warns(tmp_path: Path) -> None:
    result = build_roadmap((tmp_path / "absent",), _snapshots())
    assert result.items == []
    assert any("no roadmap directory" in w for w in result.warnings)


def test_duplicate_ids_warn(tmp_path: Path) -> None:
    d = tmp_path / "roadmaps"
    d.mkdir()
    dup = "items:\n  - id: RD-X\n    title: a\n  - id: RD-X\n    title: b\n"
    (d / "dup.yaml").write_text(dup)
    result = build_roadmap((d,), _snapshots())
    assert len(result.items) == 1
    assert any("duplicate" in w for w in result.warnings)


def test_path_traversal_rejected(tmp_path: Path) -> None:
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "evil.yaml").write_text(
        "items:\n"
        "  - id: RD-EVIL\n"
        "    title: traversal\n"
        "    evidence_rules:\n"
        "      - rule: file_exists\n"
        "        project: arbiter\n"
        "        path: /etc/passwd\n"
        "      - rule: file_exists\n"
        "        project: arbiter\n"
        "        path: ../../etc/passwd\n"
        "      - rule: sqlite_has_row\n"
        "        project: arbiter\n"
        "        db: ../outside.db\n"
        "        query: SELECT 1\n"
    )
    snaps = [ProjectSnapshot(name="arbiter", path=str(tmp_path / "arbiter"))]
    (tmp_path / "arbiter").mkdir()
    item = build_roadmap((d,), snaps).items[0]
    assert all(not e.passed for e in item.evidence)
    assert all("escapes project root" in e.detail for e in item.evidence)


def test_malformed_rule_degrades_not_crashes(tmp_path: Path) -> None:
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "bad.yaml").write_text(
        "items:\n"
        "  - id: RD-BAD\n"
        "    title: bad min_links\n"
        "    evidence_rules:\n"
        "      - rule: work_item_chain\n"
        "        work_item_id: T-9\n"
        "        min_links: not-a-number\n"
    )
    item = build_roadmap((d,), _snapshots()).items[0]
    assert item.computed_status == "planned"
    assert "rule error" in item.evidence[0].detail


def test_roadmap_names_dedup_and_null(tmp_path: Path) -> None:
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "a.yaml").write_text("roadmap: null\nitems: []\n")
    (d / "b.yaml").write_text("roadmap: same\nitems: []\n")
    (d / "c.yaml").write_text("roadmap: same\nitems: []\n")
    result = build_roadmap((d,), [])
    assert result.roadmaps == ["a", "same"]


def test_default_roadmap_dirs() -> None:
    dirs = default_roadmap_dirs((Path("/r1"), Path("/r2")))
    assert dirs == (
        Path("/r1/prograph-vault/authored/roadmaps"),
        Path("/r2/prograph-vault/authored/roadmaps"),
    )


def test_vault_file_rules_resolve_from_roadmap_dirs(tmp_path: Path) -> None:
    """`prograph-vault` file rules resolve via the roadmap dirs (RD-000)."""
    d = tmp_path / "prograph-vault" / "authored" / "roadmaps"
    d.mkdir(parents=True)
    (tmp_path / "prograph-vault" / "authored" / "rules").mkdir()
    (tmp_path / "prograph-vault" / "authored" / "rules" / "checklist.md").write_text(
        "# rule\n"
    )
    (d / "kb.yaml").write_text(
        "items:\n"
        "  - id: RD-KB\n"
        "    title: authored rule exists\n"
        "    evidence_rules:\n"
        "      - rule: file_exists\n"
        "        kind: implementation\n"
        "        project: prograph-vault\n"
        "        path: authored/rules/checklist.md\n"
    )
    result = build_roadmap((d,), [])
    assert result.items[0].computed_status == "implemented"


def test_dispatcher_self_evidence(tmp_path: Path) -> None:
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "self.yaml").write_text(
        "items:\n"
        "  - id: RD-SELF\n"
        "    title: dashboard attests itself\n"
        "    evidence_rules:\n"
        "      - rule: file_exists\n"
        "        kind: implementation\n"
        "        project: dispatcher\n"
        "        path: dispatcher/core/roadmap.py\n"
    )
    result = build_roadmap((d,), [])
    assert result.items[0].computed_status == "implemented"


async def test_roadmap_endpoint(tmp_path: Path) -> None:
    make_atp(tmp_path)
    make_arbiter(tmp_path)
    make_maestro(tmp_path)
    db = make_maestro_home(tmp_path)
    with sqlite3.connect(db) as conn:
        # second T-9 link so the RD-A verification chain rule passes
        conn.execute(
            "INSERT INTO tasks VALUES ('T-9', 'Route me', 'done', 'auto', "
            "'2026-07-02T09:58:00', '2026-07-02T09:59:00', "
            "'2026-07-02T10:06:00')"
        )
    vault = tmp_path / "prograph-vault" / "authored" / "roadmaps"
    vault.mkdir(parents=True)
    (vault / "test-v1.yaml").write_text(_ROADMAP)
    config = DispatcherConfig(roots=(tmp_path,), maestro_db=db)
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        listing = await c.get("/api/roadmap")
        one = await c.get("/api/roadmap/RD-A")
        missing = await c.get("/api/roadmap/RD-ZZZ")
    assert listing.status_code == 200
    data = listing.json()
    assert data["roadmaps"] == ["test-v1"]
    assert {i["id"] for i in data["items"]} == {"RD-A", "RD-B", "RD-C", "RD-D", "RD-E"}
    assert one.status_code == 200
    assert one.json()["computed_status"] == "verified"
    assert missing.status_code == 404
