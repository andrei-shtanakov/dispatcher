"""Human queue A1 adapters (spec 2026-09-29-human-queue-a1-design §3)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from conftest import make_maestro_run

from dispatcher.core.collectors.maestro import classified_runs, waiting_tasks
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.human_queue_sources import (
    from_maestro,
    from_run_store,
    read_store,
)
from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_store import RunStore

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


_REQ = "11111111-1111-4111-8111-111111111111"
_OLD_REQ = "22222222-2222-4222-8222-222222222222"
_KEY = RepoKey(host="github.com", owner="acme", repo="app")


def _config(tmp_path: Path, *, control_plane: bool = True) -> DispatcherConfig:
    return DispatcherConfig(
        roots=(tmp_path / "ws",),
        maestro_home=tmp_path / "mhome",
        run_state_dir=tmp_path / "state" if control_plane else None,
    )


# -- dispatcher_runs ---------------------------------------------------------


def test_run_store_off_is_not_configured(tmp_path: Path) -> None:
    result = from_run_store(read_store(_config(tmp_path, control_plane=False)))
    assert result.status.state == "not_configured"
    assert result.waits == []


def test_launch_unknown_is_a_wait_aged_by_unknown_at(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t", work_id="w1")
    record = store.mark_unknown(_REQ, "no run appeared")
    result = from_run_store(read_store(config))
    assert result.status.state == "ok"
    [wait] = result.waits
    assert wait.key == f"dispatcher-run:{_REQ}"
    assert wait.reasons == ["launch_unknown"]
    assert wait.since == record.unknown_at
    assert wait.since_basis is not None
    assert wait.act.kind == "run_view"


def test_record_without_unknown_at_has_unknown_age(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    store.reserve(_OLD_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_unknown(_OLD_REQ, "no run appeared")
    path = store._record_path(_OLD_REQ)  # noqa: SLF001 — simulating an old record
    raw = json.loads(path.read_text())
    raw.pop("unknown_at")
    path.write_text(json.dumps(raw))
    [wait] = from_run_store(read_store(config)).waits
    assert (wait.since, wait.since_basis) == (None, None)


def test_unreadable_record_makes_source_partial(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_unknown(_REQ, "no run appeared")
    (config.run_state_dir / "requests" / "broken.json").write_text("{not json")
    result = from_run_store(read_store(config))
    assert result.status.state == "partial"
    assert "broken.json" in (result.status.detail or "")
    assert len(result.waits) == 1


# -- maestro -----------------------------------------------------------------


def test_waits_from_every_non_terminal_run_not_only_the_newest(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    old = make_maestro_run(home, _ACME, "01OLD", started_at="2026-09-01T00:00:00")
    _add_task(old, "T-old", "needs_review", "2026-09-01T00:00:00")
    new = make_maestro_run(home, _ACME, "01NEW", started_at="2026-09-10T00:00:00")
    _add_task(new, "T-000", "awaiting_approval", "2026-08-01T00:00:00")
    for i in range(1, 60):
        _add_task(new, f"T-{i:03d}", "in_progress", f"2026-09-10T00:{i:02d}:00")
    result = from_maestro(home, [])
    assert result.status.state == "ok"
    assert sorted(w.key for w in result.waits) == [
        "maestro:github.com/acme/app:01NEW:T-000:awaiting_approval",
        "maestro:github.com/acme/app:01OLD:T-old:needs_review",
    ]
    by_ref = {w.ref: w for w in result.waits}
    assert by_ref["01OLD/T-old"].reasons == ["run_needs_review"]
    assert by_ref["01OLD/T-old"].act.model_dump() == {
        "kind": "maestro_verb",
        "verb": "retry",
        "task_id": "T-old",
        "run_id": "01OLD",
        "repo_key": "github.com/acme/app",
    }
    assert by_ref["01NEW/T-000"].act.model_dump()["verb"] == "approve"
    assert all(w.since is None for w in result.waits)


def test_waiting_task_in_a_finished_run_is_not_a_wait(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    db = make_maestro_run(
        home,
        _ACME,
        "01DONE",
        started_at="2026-09-01T00:00:00",
        outcome="completed",
        ended_at="2026-09-01T01:00:00",
    )
    _add_task(db, "T-1", "needs_review", "2026-09-01T00:00:00")
    result = from_maestro(home, [])
    assert result.status.state == "ok"
    assert result.waits == []


def test_unreadable_run_db_is_partial_others_still_listed(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    good = make_maestro_run(home, _ACME, "01GOOD", started_at="2026-09-01T00:00:00")
    _add_task(good, "T-1", "needs_review", "2026-09-01T00:00:00")
    bad = home.joinpath("projects", *_ACME, "runs", "01BAD", "state.db")
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a database")
    result = from_maestro(home, [])
    assert result.status.state == "partial"
    assert "01BAD" in (result.status.detail or "")
    assert [w.ref for w in result.waits] == ["01GOOD/T-1"]


def test_failing_task_query_on_a_readable_run_is_partial(tmp_path: Path) -> None:
    """The `run` row reads (run classified), the task query does not."""
    home = tmp_path / "mhome"
    good = make_maestro_run(home, _ACME, "01GOOD", started_at="2026-09-01T00:00:00")
    _add_task(good, "T-1", "needs_review", "2026-09-01T00:00:00")
    broken = make_maestro_run(
        home, _ACME, "01NOTASKS", started_at="2026-09-02T00:00:00"
    )
    conn = sqlite3.connect(broken)
    try:
        conn.execute("DROP TABLE tasks")
        conn.commit()
    finally:
        conn.close()
    result = from_maestro(home, [])
    assert result.status.state == "partial"
    assert "01NOTASKS" in (result.status.detail or "")
    assert [w.ref for w in result.waits] == ["01GOOD/T-1"]


def test_run_dir_without_state_db_is_partial(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    good = make_maestro_run(home, _ACME, "01GOOD", started_at="2026-09-01T00:00:00")
    _add_task(good, "T-1", "needs_review", "2026-09-01T00:00:00")
    home.joinpath("projects", *_ACME, "runs", "01BARE").mkdir()
    result = from_maestro(home, [])
    assert result.status.state == "partial"
    assert "01BARE" in (result.status.detail or "")
    assert [w.ref for w in result.waits] == ["01GOOD/T-1"]


def test_unlistable_runs_dir_is_partial(tmp_path: Path) -> None:
    """An enumeration warning from classified_runs is a hole in the source."""
    home = tmp_path / "mhome"
    good = make_maestro_run(home, _ACME, "01GOOD", started_at="2026-09-01T00:00:00")
    _add_task(good, "T-1", "needs_review", "2026-09-01T00:00:00")
    other = ("github.com", "acme", "locked")
    make_maestro_run(home, other, "01HIDDEN", started_at="2026-09-01T00:00:00")
    runs_dir = home.joinpath("projects", *other, "runs")
    runs_dir.chmod(0)
    try:
        if os.access(runs_dir, os.R_OK):
            pytest.skip("running as a user that ignores directory permissions")
        result = from_maestro(home, [])
    finally:
        runs_dir.chmod(0o755)
    assert result.status.state == "partial"
    assert "cannot list" in (result.status.detail or "")
    assert [w.ref for w in result.waits] == ["01GOOD/T-1"]


def test_absent_maestro_home_is_a_clean_zero(tmp_path: Path) -> None:
    result = from_maestro(tmp_path / "nowhere", [])
    assert result.status.state == "ok"
    assert result.waits == []


def test_run_launched_by_dispatcher_routes_to_run_view(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    home = tmp_path / "mhome"
    db = make_maestro_run(home, _ACME, "01RUN", started_at="2026-09-01T00:00:00")
    _add_task(db, "T-1", "needs_review", "2026-09-01T00:00:00")
    store = RunStore(config.run_state_dir)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_materialized(_REQ, "01RUN")
    listing = read_store(config)
    assert listing is not None
    [wait] = from_maestro(home, listing[0]).waits
    assert wait.act.model_dump() == {"kind": "run_view", "request_id": _REQ}
