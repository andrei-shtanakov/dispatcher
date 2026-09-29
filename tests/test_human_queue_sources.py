"""Human queue A1 adapters (spec 2026-09-29-human-queue-a1-design §3)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from conftest import make_maestro_run

from dispatcher.core.collectors.maestro import classified_runs, waiting_tasks
from dispatcher.core.models import ProjectSnapshot

_ACME = ("github.com", "acme", "app")


def _add_task(db: Path, task_id: str, status: str, created_at: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                f"title {task_id}",
                status,
                "claude_code",
                created_at,
                None,
                None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_waiting_tasks_has_no_limit_and_no_recency_window(tmp_path: Path) -> None:
    """The collector keeps the newest 50; the queue must keep every wait.

    60 waiting tasks, the oldest first, among 60 newer non-waiting ones: a
    LIMIT before OR after the status filter, or a recency order, fails this.
    """
    db = make_maestro_run(tmp_path, _ACME, "01RUN", started_at="2026-09-01T00:00:00")
    for i in range(60):
        status = "needs_review" if i % 2 == 0 else "awaiting_approval"
        _add_task(db, f"W-{i:03d}", status, f"2026-08-01T00:{i:02d}:00")
    for i in range(60):
        _add_task(db, f"N-{i:03d}", "in_progress", f"2026-09-02T00:{i:02d}:00")
    rows = waiting_tasks(db)
    assert [r["id"] for r in rows] == [f"W-{i:03d}" for i in range(60)]
    assert {r["status"] for r in rows} == {"needs_review", "awaiting_approval"}


def test_missing_state_is_reported_only_when_asked(tmp_path: Path) -> None:
    make_maestro_run(tmp_path, _ACME, "01GOOD", started_at="2026-09-01T00:00:00")
    tmp_path.joinpath("projects", *_ACME, "runs", "01BARE").mkdir(parents=True)

    silent = ProjectSnapshot(name="maestro", path="")
    runs = classified_runs(tmp_path, silent)
    assert [info.run_id for info, _ in runs] == ["01GOOD"]
    assert silent.warnings == []  # every existing caller: unchanged

    loud = ProjectSnapshot(name="maestro", path="")
    runs = classified_runs(tmp_path, loud, report_missing_state=True)
    assert [info.run_id for info, _ in runs] == ["01GOOD"]
    assert len(loud.warnings) == 1
    assert loud.warnings[0].startswith("run 01BARE: no state.db")
