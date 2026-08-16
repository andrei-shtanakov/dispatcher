"""Tests for the Maestro collector."""

import os
import sqlite3
import subprocess
from pathlib import Path

from conftest import (
    make_atp,
    make_maestro,
    make_maestro_home,
    make_maestro_run,
    write_holder,
)

from dispatcher.core.collectors.base import CollectContext
from dispatcher.core.collectors.maestro import MaestroCollector

_ACME = ("github.com", "acme", "app")


def test_detect(tmp_path: Path) -> None:
    p = make_maestro(tmp_path)
    assert MaestroCollector().detect(p) is True
    assert MaestroCollector().detect(tmp_path) is False


def test_collect_happy_path(tmp_path: Path) -> None:
    p = make_maestro(tmp_path)
    db = make_maestro_home(tmp_path)
    atp = make_atp(tmp_path)
    ctx = CollectContext(
        home=tmp_path / "home",
        maestro_db=db,
        catalog_path=atp / "method" / "agents-catalog.toml",
    )
    snap = MaestroCollector().collect(p, ctx)
    ver = snap.schema_versions[0]
    assert (ver.found, ver.expected, ver.ok) == ("2", "2", True)
    task = snap.tasks[0]
    assert task.task_id == "M-1"
    assert task.cost_usd == 0.42
    routable = {(m.harness, m.model_id) for m in snap.models}
    assert ("claude_code", "claude-sonnet-4-6") in routable
    assert ("deepseek", "deepseek-chat") not in routable  # routable=false
    running = [c for c in snap.configs if c.format == "pid"]
    assert running[0].summary == {"running": False}
    assert any(e.body == "subprocess failed" for e in snap.errors)
    assert snap.warnings == []


def test_collect_without_home_db(tmp_path: Path) -> None:
    p = make_maestro(tmp_path)
    ctx = CollectContext(home=tmp_path / "home", maestro_db=None)
    snap = MaestroCollector().collect(p, ctx)
    assert snap.tasks == []
    assert any("maestro.db" in w for w in snap.warnings)


def test_collect_null_status_task_does_not_raise(tmp_path: Path) -> None:
    p = make_maestro(tmp_path)
    db = make_maestro_home(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO tasks (id, title, status, agent_type, created_at, "
            "started_at, completed_at) VALUES ('M-null', 'x', NULL, "
            "'claude_code', '2026-07-03T00:00:00', NULL, NULL)"
        )
    ctx = CollectContext(home=tmp_path / "home", maestro_db=db)
    snap = MaestroCollector().collect(p, ctx)
    task = next(t for t in snap.tasks if t.task_id == "M-null")
    assert task.status == "unknown"


def _maestro_home(tmp_path: Path) -> Path:
    return tmp_path / "home" / ".maestro"


def _collect_runs(tmp_path: Path, maestro_db: Path | None = None):
    p = make_maestro(tmp_path)
    ctx = CollectContext(
        home=tmp_path / "home",
        maestro_db=maestro_db,
        maestro_home=_maestro_home(tmp_path),
    )
    return MaestroCollector().collect(p, ctx)


def test_enumerates_runs_newest_first_tasks_from_newest_only(tmp_path: Path) -> None:
    home = _maestro_home(tmp_path)
    make_maestro_run(
        home,
        _ACME,
        "01OLD",
        started_at="2026-08-10T00:00:00",
        outcome="completed",
        ended_at="2026-08-10T01:00:00",
        task_id="T-old",
    )
    make_maestro_run(
        home, _ACME, "01NEW", started_at="2026-08-12T00:00:00", task_id="T-new"
    )
    snap = _collect_runs(tmp_path)
    assert [(r.run_id, r.status) for r in snap.runs] == [
        ("01NEW", "interrupted"),
        ("01OLD", "completed"),
    ]
    assert snap.runs[0].repo_key == "github.com/acme/app"
    assert [t.task_id for t in snap.tasks] == ["T-new"]
    assert not [w for w in snap.warnings if "run " in w]


def test_local_namespace_is_two_segments(tmp_path: Path) -> None:
    home = _maestro_home(tmp_path)
    make_maestro_run(
        home, ("_local", "proj-abc123"), "01L", started_at="2026-08-12T00:00:00"
    )
    snap = _collect_runs(tmp_path)
    assert [(r.repo_key, r.run_id) for r in snap.runs] == [
        ("_local/proj-abc123", "01L")
    ]


def test_no_terminal_record_is_never_in_progress(tmp_path: Path) -> None:
    home = _maestro_home(tmp_path)
    make_maestro_run(home, _ACME, "01X", started_at="2026-08-12T00:00:00")
    snap = _collect_runs(tmp_path)
    assert snap.runs[0].status == "interrupted"


def test_holder_with_live_pid_reports_running(tmp_path: Path) -> None:
    home = _maestro_home(tmp_path)
    make_maestro_run(home, _ACME, "01R", started_at="2026-08-12T00:00:00")
    write_holder(home, _ACME, "01R", os.getpid())
    snap = _collect_runs(tmp_path)
    assert snap.runs[0].status == "running"


def test_holder_with_dead_pid_stays_interrupted(tmp_path: Path) -> None:
    home = _maestro_home(tmp_path)
    make_maestro_run(home, _ACME, "01D", started_at="2026-08-12T00:00:00")
    proc = subprocess.Popen(["sleep", "0"])
    proc.wait()
    write_holder(home, _ACME, "01D", proc.pid)
    snap = _collect_runs(tmp_path)
    assert snap.runs[0].status == "interrupted"


def test_holder_for_other_run_stays_interrupted(tmp_path: Path) -> None:
    home = _maestro_home(tmp_path)
    make_maestro_run(home, _ACME, "01A", started_at="2026-08-12T00:00:00")
    write_holder(home, _ACME, "01B-other", os.getpid())
    snap = _collect_runs(tmp_path)
    assert snap.runs[0].status == "interrupted"


def test_malformed_holder_stays_interrupted(tmp_path: Path) -> None:
    home = _maestro_home(tmp_path)
    make_maestro_run(home, _ACME, "01M", started_at="2026-08-12T00:00:00")
    path = write_holder(home, _ACME, "01M", os.getpid())
    path.write_text("not json")
    snap = _collect_runs(tmp_path)
    assert snap.runs[0].status == "interrupted"


def test_suspended_run_reports_suspended(tmp_path: Path) -> None:
    home = _maestro_home(tmp_path)
    make_maestro_run(
        home,
        _ACME,
        "01S",
        started_at="2026-08-12T00:00:00",
        suspended_at="2026-08-12T02:00:00",
    )
    snap = _collect_runs(tmp_path)
    assert snap.runs[0].status == "suspended"


def test_running_wins_over_suspended(tmp_path: Path) -> None:
    home = _maestro_home(tmp_path)
    make_maestro_run(
        home,
        _ACME,
        "01RS",
        started_at="2026-08-12T00:00:00",
        suspended_at="2026-08-12T02:00:00",
    )
    write_holder(home, _ACME, "01RS", os.getpid())
    snap = _collect_runs(tmp_path)
    assert snap.runs[0].status == "running"


def test_missing_run_row_is_unreadable_not_legacy(tmp_path: Path) -> None:
    home = _maestro_home(tmp_path)
    make_maestro_run(
        home, _ACME, "01U", started_at="2026-08-12T00:00:00", with_run_row=False
    )
    snap = _collect_runs(tmp_path)
    assert snap.runs[0].status == "unreadable"
    assert any("01U" in w for w in snap.warnings)


def test_garbage_db_is_unreadable(tmp_path: Path) -> None:
    home = _maestro_home(tmp_path)
    db = home.joinpath("projects", *_ACME, "runs", "01G", "state.db")
    db.parent.mkdir(parents=True)
    db.write_bytes(b"not a sqlite database")
    snap = _collect_runs(tmp_path)
    assert snap.runs[0].status == "unreadable"


def test_freshness_covers_newest_run_logs(tmp_path: Path) -> None:
    home = _maestro_home(tmp_path)
    db = make_maestro_run(home, _ACME, "01F", started_at="2026-08-12T00:00:00")
    logs = db.parent / "logs"
    logs.mkdir()
    # Telemetry can land under the run's logs/ after the last state.db
    # write; stamp the dir ahead of every other source to prove it feeds
    # freshness (2_000_000_000 = 2033-05-18T03:33:20Z).
    os.utime(logs, (2_000_000_000, 2_000_000_000))
    snap = _collect_runs(tmp_path)
    assert snap.freshness is not None
    assert snap.freshness.startswith("2033-05-18")


def test_legacy_db_labeled_legacy_and_tasks_kept(tmp_path: Path) -> None:
    legacy = make_maestro_home(tmp_path)
    snap = _collect_runs(tmp_path, maestro_db=legacy)
    legacy_runs = [r for r in snap.runs if r.repo_key == "legacy"]
    assert [(r.run_id, r.status) for r in legacy_runs] == [(None, "legacy")]
    assert any(t.task_id == "M-1" for t in snap.tasks)


def test_unreadable_runs_dir_warns_never_silent_zero(tmp_path: Path) -> None:
    home = _maestro_home(tmp_path)
    make_maestro_run(home, _ACME, "01P", started_at="2026-08-12T00:00:00")
    runs_dir = home.joinpath("projects", *_ACME, "runs")
    runs_dir.chmod(0o000)
    try:
        snap = _collect_runs(tmp_path)
    finally:
        runs_dir.chmod(0o700)
    assert snap.runs == []
    assert any(w.startswith("runs enumeration:") for w in snap.warnings)


def test_run_warning_prefixes_are_pinned(tmp_path: Path) -> None:
    """Surfaces distinguish «degraded» from «clean zero» by the `run `/
    `runs ` warning prefixes — every enumeration/classification warning
    must carry one, or a broken tree renders as a confident «0 runs»."""
    home = _maestro_home(tmp_path)
    make_maestro_run(
        home, _ACME, "01W", started_at="2026-08-12T00:00:00", with_run_row=False
    )
    bad = home.joinpath("projects", *_ACME, "runs", "01V", "state.db")
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"garbage")
    locks = home.joinpath("projects", *_ACME, "locks")
    locks.mkdir()
    snap = _collect_runs(tmp_path)
    run_warnings = [
        w
        for w in snap.warnings
        if w not in ("agents catalog not available (atp-platform?)",)
        and "maestro.db not found" not in w
    ]
    assert run_warnings, "expected classification warnings"
    assert all(w.startswith(("run ", "runs ")) for w in run_warnings)


def test_no_maestro_home_skips_enumeration(tmp_path: Path) -> None:
    home = _maestro_home(tmp_path)
    make_maestro_run(home, _ACME, "01Z", started_at="2026-08-12T00:00:00")
    p = make_maestro(tmp_path)
    ctx = CollectContext(home=tmp_path / "home", maestro_db=None)
    snap = MaestroCollector().collect(p, ctx)
    assert snap.runs == []
