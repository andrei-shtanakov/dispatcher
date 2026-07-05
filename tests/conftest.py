"""Builders for miniature fake project trees used by collector tests."""

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _db(path: Path, script: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(script)
    conn.commit()
    conn.close()


def write_otel_error_log(project_root: Path) -> None:
    """One run dir with one ERROR record.

    The timestamp is computed relative to "now" (rather than a fixed
    epoch) so this fixture stays inside default recency windows (e.g. the
    TUI's 14-day errors filter) no matter when the suite runs.
    """
    run = project_root / "logs" / "01BBBBBBBBBBBBBBBBBBBBBBBB"
    run.mkdir(parents=True, exist_ok=True)
    rec = {
        "Timestamp": str(time.time_ns() - 3_600_000_000_000),  # ~1h ago
        "SeverityNumber": 17,
        "SeverityText": "ERROR",
        "Body": "subprocess failed",
        "Attributes": {"pipeline_id": "01BBB"},
        "Resource": {"service.name": "svc"},
    }
    (run / "svc-1.jsonl").write_text(json.dumps(rec) + "\n")


def make_spec_runner(root: Path) -> Path:
    p = root / "spec-runner"
    (p / "src" / "spec_runner").mkdir(parents=True)
    # This attempts-row timestamp feeds the TUI/web errors pipeline, whose
    # default freshness filter is a 14-day now-relative window. Keep it
    # now-relative (rather than a fixed epoch) so it doesn't age out of that
    # window and silently drop from recency-sensitive assertions.
    attempt_ts = (datetime.now(tz=UTC) - timedelta(days=1)).isoformat(
        timespec="seconds"
    )
    _db(
        p / "spec" / ".executor-state.db",
        f"""
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            started_at TEXT, completed_at TEXT);
        CREATE TABLE attempts (
            id INTEGER PRIMARY KEY, task_id TEXT, timestamp TEXT,
            success INTEGER, error TEXT, error_kind TEXT, error_stage TEXT);
        CREATE TABLE executor_meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO tasks VALUES
            ('T-1', 'completed', '2026-07-01T10:00:00', '2026-07-01T10:30:00'),
            ('T-2', 'in_progress', '2026-07-03T09:00:00', NULL);
        INSERT INTO attempts (task_id, timestamp, success, error, error_kind,
            error_stage)
            VALUES ('T-1', '{attempt_ts}', 0, 'lint failed', 'lint',
            'verify');
        INSERT INTO executor_meta VALUES ('total_completed', '1');
        """,
    )
    (p / "executor.config.yaml").write_text(
        "review_model: gpt-5-codex\nmax_retries: 2\napi_key: supersecret\n"
    )
    (p / "schemas").mkdir()
    (p / "schemas" / "status.schema.json").write_text("{}")
    write_otel_error_log(p)
    return p


def make_arbiter(root: Path) -> Path:
    p = root / "arbiter"
    (p / "arbiter-core").mkdir(parents=True)
    (p / "config").mkdir()
    (p / "config" / "agents.toml").write_text(
        '["claude_code@claude-sonnet-4-6"]\ndisplay_name = "CC"\n\n'
        '["codex_cli@gpt-5.5"]\ndisplay_name = "CX"\n\n'
        "[aider]\ndisplay_name = 'Aider'\n"
    )
    (p / "config" / "invariants.toml").write_text("[budget]\nmax_usd = 10\n")
    (p / "config" / "agents-catalog.toml").write_text("# vendored copy\nx = 1\n")
    _db(
        p / "arbiter.db",
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY, applied_at TEXT);
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY, task_id TEXT, timestamp TEXT,
            chosen_agent TEXT, action TEXT, confidence REAL);
        CREATE TABLE benchmark_runs (
            run_id TEXT PRIMARY KEY, benchmark_id TEXT, agent_id TEXT,
            ts TEXT, score REAL, total_cost_usd REAL);
        INSERT INTO schema_version VALUES (1, '2026-04-06');
        INSERT INTO decisions (task_id, timestamp, chosen_agent, action,
            confidence)
            VALUES ('T-9', '2026-07-02T10:00:00',
            'claude_code@claude-sonnet-4-6', 'assign', 0.9);
        INSERT INTO benchmark_runs VALUES
            ('R-1', 'code-review', 'codex_cli@gpt-5.5',
             '2026-07-02T09:00:00', 0.83, 1.2);
        """,
    )
    write_otel_error_log(p)
    return p


def make_maestro(root: Path) -> Path:
    p = root / "Maestro"
    (p / "maestro").mkdir(parents=True)
    (p / "pyproject.toml").write_text("[project]\nname = 'maestro'\n")
    (p / "executor.config.yaml").write_text("review_model: gpt-5-codex\n")
    write_otel_error_log(p)
    return p


def make_maestro_home(root: Path) -> Path:
    """Fake ~/.maestro/maestro.db; returns the db path."""
    db = root / "home" / ".maestro" / "maestro.db"
    _db(
        db,
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, status TEXT, agent_type TEXT,
            created_at TEXT, started_at TEXT, completed_at TEXT);
        CREATE TABLE task_costs (
            id INTEGER PRIMARY KEY, task_id TEXT,
            estimated_cost_usd REAL);
        INSERT INTO schema_migrations VALUES (2, 'v2', '2026-04-19');
        INSERT INTO tasks VALUES
            ('M-1', 'Build feature', 'completed', 'claude_code',
             '2026-04-19T10:00:00', '2026-04-19T10:01:00',
             '2026-04-19T10:30:00');
        INSERT INTO task_costs (task_id, estimated_cost_usd)
            VALUES ('M-1', 0.42);
        """,
    )
    return db


def make_atp(root: Path) -> Path:
    p = root / "atp-platform"
    (p / "atp").mkdir(parents=True)
    (p / "method").mkdir()
    (p / "method" / "agents-catalog.toml").write_text(
        '[models."claude-sonnet-4-6"]\nvendor = "anthropic"\n'
        'status = "active"\n\n'
        '[models."gpt-5.5"]\nvendor = "openai"\nstatus = "active"\n\n'
        "[[agents]]\nharness = 'claude_code'\nmodel = 'claude-sonnet-4-6'\n"
        "routable = true\n\n"
        "[[agents]]\nharness = 'deepseek'\nmodel = 'deepseek-chat'\n"
        "routable = false\n"
    )
    (p / "atp.config.yaml").write_text(
        "default_llm_model: gpt-4o-mini\nparallel_workers: 4\n"
        "dashboard_secret_key: hushhush\n"
    )
    _db(
        p / ".atp-dashboard.db",
        """
        CREATE TABLE alembic_version (version_num VARCHAR PRIMARY KEY);
        CREATE TABLE test_executions (
            id INTEGER PRIMARY KEY, test_name TEXT, started_at TEXT,
            success BOOLEAN, score FLOAT, status TEXT,
            total_runs INTEGER, successful_runs INTEGER);
        CREATE TABLE benchmark_runs (
            id INTEGER PRIMARY KEY, agent_name TEXT, status TEXT,
            total_score FLOAT, started_at TEXT);
        INSERT INTO alembic_version VALUES ('f1a2b3c4d5e6');
        INSERT INTO test_executions (test_name, started_at, success, score,
            status, total_runs, successful_runs)
            VALUES ('suite/smoke', '2026-07-01T08:00:00', 1, 0.95,
            'completed', 3, 3);
        INSERT INTO benchmark_runs (agent_name, status, total_score,
            started_at)
            VALUES ('codex_cli@gpt-5.5', 'completed', 0.83,
            '2026-07-02T09:00:00');
        """,
    )
    results = p / "results" / "experiment"
    results.mkdir(parents=True)
    (results / "experiment_results.json").write_text("{}")
    bench = p / "_bench_output" / "r07"
    bench.mkdir(parents=True)
    (bench / "sweep.db").write_bytes(b"")
    return p


def make_proctor(root: Path) -> Path:
    p = root / "proctor-a"
    (p / "config").mkdir(parents=True)
    (p / "config" / "proctor.yaml").write_text(
        "llm:\n  default_model: claude-sonnet-4-20250514\n"
        "  fallback_model: ollama/llama3.2\n"
        "telegram:\n  bot_token: tg-secret\n"
    )
    _db(
        p / "data" / "state.db",
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, status TEXT, created_at TEXT,
            updated_at TEXT);
        CREATE TABLE schedules (
            id TEXT PRIMARY KEY, type TEXT, expression TEXT,
            enabled INTEGER, next_run TEXT, last_run TEXT);
        INSERT INTO tasks VALUES
            ('P-1', 'pending', '2026-07-01T07:00:00', '2026-07-01T07:00:00');
        INSERT INTO schedules VALUES
            ('S-1', 'cron', '0 9 * * *', 1, '2026-07-04T09:00:00',
             '2026-07-03T09:00:00');
        """,
    )
    logs = p / "logs"
    logs.mkdir()
    (logs / "scheduler-trigger.log").write_text(
        "2026-07-01 INFO started\n2026-07-02 ERROR trigger failed: boom\n"
    )
    return p
