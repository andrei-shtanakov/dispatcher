"""Tests for read-side work-item correlation (core + API)."""

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from conftest import (
    make_arbiter,
    make_atp,
    make_maestro,
    make_maestro_home,
    make_spec_runner,
)

from dispatcher.core.correlation import build_work_items, scan_task_pipelines
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.models import ProjectSnapshot, TaskInfo
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio


def _task(task_id: str, status: str, started_at: str | None = None) -> TaskInfo:
    return TaskInfo(
        task_id=task_id, status=status, started_at=started_at, source="test"
    )


def _snapshots() -> list[ProjectSnapshot]:
    return [
        ProjectSnapshot(
            name="Maestro",
            path="",
            tasks=[_task("T-9", "done", "2026-07-02T09:59:00")],
        ),
        ProjectSnapshot(
            name="arbiter",
            path="",
            tasks=[
                _task("T-9", "assign", "2026-07-02T10:00:00"),
                _task("T-9", "success", "2026-07-02T10:05:00"),
            ],
        ),
        ProjectSnapshot(
            name="spec-runner",
            path="",
            tasks=[_task("TASK-1", "completed", "2026-07-01T10:00:00")],
        ),
    ]


def test_build_work_items_groups_by_task_id() -> None:
    result = build_work_items(_snapshots())
    assert result.total == 2
    assert result.cross_project == 1
    chain = result.items[0]  # cross-project chains sort first
    assert chain.work_item_id == "T-9"
    assert chain.cross_project is True
    assert chain.projects == ["Maestro", "arbiter"]
    # links ordered chronologically: task -> decision -> outcome
    assert [link.status for link in chain.links] == ["done", "assign", "success"]


def test_build_work_items_single_project_chain() -> None:
    result = build_work_items(_snapshots())
    solo = result.items[1]
    assert solo.work_item_id == "TASK-1"
    assert solo.cross_project is False
    assert solo.pipeline_ids == []


def _write_log(logs_dir: Path, run: str, records: list[dict]) -> None:
    run_dir = logs_dir / run
    run_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r) for r in records]
    (run_dir / "arbiter-1.jsonl").write_text("\n".join(lines) + "\n")


def test_scan_task_pipelines(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    _write_log(
        logs,
        "01AAAAAAAAAAAAAAAAAAAAAAAA",
        [
            {"Attributes": {"event": "mcp.ready", "pipeline_id": "01AAA"}},
            {
                "Attributes": {
                    "event": "outcome.recorded",
                    "task_id": "T-9",
                    "pipeline_id": "01AAA",
                }
            },
        ],
    )
    # corrupt line must be skipped silently
    (logs / "01AAAAAAAAAAAAAAAAAAAAAAAA" / "broken.jsonl").write_text("{oops\n")
    assert scan_task_pipelines(logs) == {"T-9": {"01AAA"}}


def test_scan_task_pipelines_missing_dir(tmp_path: Path) -> None:
    assert scan_task_pipelines(tmp_path / "absent") == {}


def _client(tmp_path: Path) -> httpx.AsyncClient:
    make_atp(tmp_path)
    arb = make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    maestro_root = make_maestro(tmp_path)
    db = make_maestro_home(tmp_path)
    # Shared work item: Maestro task T-9 routed and reported in arbiter.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tasks VALUES ('T-9', 'Route me', 'done', 'auto', "
            "'2026-07-02T09:58:00', '2026-07-02T09:59:00', "
            "'2026-07-02T10:06:00')"
        )
    with sqlite3.connect(arb / "arbiter.db") as conn:
        conn.execute(
            "INSERT INTO outcomes (task_id, decision_id, agent_id, timestamp,"
            " status, cost_usd) VALUES ('T-9', 1, "
            "'claude_code@claude-sonnet-4-6', '2026-07-02T10:05:00', "
            "'success', 0.08)"
        )
    _write_log(
        maestro_root / "logs",
        "01CCCCCCCCCCCCCCCCCCCCCCCC",
        [
            {
                "Attributes": {
                    "event": "outcome.recorded",
                    "task_id": "T-9",
                    "pipeline_id": "01CCC",
                }
            }
        ],
    )
    config = DispatcherConfig(roots=(tmp_path,), maestro_db=db)
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_work_items_endpoint(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/work-items")
    assert resp.status_code == 200
    data = resp.json()
    chains = {c["work_item_id"]: c for c in data["items"]}
    chain = chains["T-9"]
    assert chain["cross_project"] is True
    assert chain["projects"] == ["Maestro", "arbiter"]
    # Maestro task + arbiter decision + arbiter outcome
    assert [link["status"] for link in chain["links"]] == [
        "done",
        "assign",
        "success",
    ]
    assert chain["pipeline_ids"] == ["01CCC"]
    assert data["cross_project"] >= 1


async def test_work_items_cross_only_filter(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/work-items", params={"cross_only": "true"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"]
    assert all(c["cross_project"] for c in data["items"])
    # totals describe the unfiltered population
    assert data["total"] >= len(data["items"])
