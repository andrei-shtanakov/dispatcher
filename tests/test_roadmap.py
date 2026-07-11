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


def test_default_roadmap_dirs() -> None:
    dirs = default_roadmap_dirs((Path("/r1"), Path("/r2")))
    assert dirs == (
        Path("/r1/prograph-vault/authored/roadmaps"),
        Path("/r2/prograph-vault/authored/roadmaps"),
    )


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
