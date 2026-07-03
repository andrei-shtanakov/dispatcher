"""Tests for the proctor-a collector."""

import sqlite3
from pathlib import Path

from conftest import make_proctor

from dispatcher.core.collectors.base import CollectContext
from dispatcher.core.collectors.proctor import ProctorCollector


def _ctx(tmp_path: Path) -> CollectContext:
    return CollectContext(home=tmp_path / "home")


def test_detect(tmp_path: Path) -> None:
    p = make_proctor(tmp_path)
    assert ProctorCollector().detect(p) is True
    assert ProctorCollector().detect(tmp_path) is False


def test_collect_happy_path(tmp_path: Path) -> None:
    p = make_proctor(tmp_path)
    snap = ProctorCollector().collect(p, _ctx(tmp_path))
    ids = {t.task_id for t in snap.tasks}
    assert ids == {"P-1", "S-1"}
    sched = next(t for t in snap.tasks if t.task_id == "S-1")
    assert sched.status == "enabled"
    assert "cron 0 9 * * *" in (sched.title or "")
    roles = {(m.model_id, m.role) for m in snap.models}
    assert ("claude-sonnet-4-20250514", "default") in roles
    assert ("ollama/llama3.2", "fallback") in roles
    cfg = snap.configs[0]
    assert cfg.summary["telegram"] == "<1 items>"
    assert any("trigger failed" in e.body for e in snap.errors)
    assert snap.schema_versions[0].ok is True
    assert snap.warnings == []


def test_collect_without_state_db(tmp_path: Path) -> None:
    p = make_proctor(tmp_path)
    (p / "data" / "state.db").unlink()
    snap = ProctorCollector().collect(p, _ctx(tmp_path))
    assert snap.tasks == []
    assert any("state.db" in w for w in snap.warnings)


def test_collect_masks_secrets_in_log_errors(tmp_path: Path) -> None:
    p = make_proctor(tmp_path)
    (p / "logs" / "scheduler-trigger.log").write_text(
        "2026-07-01 INFO started\n"
        "2026-07-02 ERROR conn nats://admin:hunter2@host:4222 "
        "auth Bearer sk-live-abc123456\n"
    )
    snap = ProctorCollector().collect(p, _ctx(tmp_path))
    bodies = [e.body for e in snap.errors]
    assert bodies  # log line was collected
    assert not any("hunter2" in b for b in bodies)
    assert not any("sk-live-abc123456" in b for b in bodies)


def test_collect_null_status_task_does_not_raise(tmp_path: Path) -> None:
    p = make_proctor(tmp_path)
    with sqlite3.connect(p / "data" / "state.db") as conn:
        conn.execute(
            "INSERT INTO tasks (id, status, created_at, updated_at) "
            "VALUES ('P-null', NULL, '2026-07-03T00:00:00', "
            "'2026-07-03T00:00:00')"
        )
    snap = ProctorCollector().collect(p, _ctx(tmp_path))
    task = next(t for t in snap.tasks if t.task_id == "P-null")
    assert task.status == "unknown"


def test_collect_config_llm_not_dict_does_not_raise(tmp_path: Path) -> None:
    p = make_proctor(tmp_path)
    (p / "config" / "proctor.yaml").write_text("llm: oops\n")
    snap = ProctorCollector().collect(p, _ctx(tmp_path))
    assert snap.models == []
    assert snap.configs
    assert snap.configs[0].summary["llm"] == "oops"
