"""Tests for the spec-runner collector."""

from pathlib import Path

from conftest import make_spec_runner

from dispatcher.core.collectors.base import CollectContext
from dispatcher.core.collectors.spec_runner import SpecRunnerCollector


def _ctx(tmp_path: Path) -> CollectContext:
    return CollectContext(home=tmp_path / "home")


def test_detect(tmp_path: Path) -> None:
    p = make_spec_runner(tmp_path)
    c = SpecRunnerCollector()
    assert c.detect(p) is True
    assert c.detect(tmp_path) is False


def test_collect_happy_path(tmp_path: Path) -> None:
    p = make_spec_runner(tmp_path)
    snap = SpecRunnerCollector().collect(p, _ctx(tmp_path))
    assert snap.name == "spec-runner"
    assert {t.task_id for t in snap.tasks} == {"T-1", "T-2"}
    assert any("lint failed" in e.body for e in snap.errors)  # failed attempt
    assert any(e.body == "subprocess failed" for e in snap.errors)  # otel
    assert any(m.model_id == "gpt-5-codex" for m in snap.models)
    cfg = snap.configs[0]
    assert cfg.summary["api_key"] == "***"
    assert snap.schema_versions[0].ok is True
    assert snap.freshness is not None
    assert snap.warnings == []


def test_collect_without_db(tmp_path: Path) -> None:
    p = make_spec_runner(tmp_path)
    (p / "spec" / ".executor-state.db").unlink()
    snap = SpecRunnerCollector().collect(p, _ctx(tmp_path))
    assert snap.tasks == []
    assert any("executor-state" in w for w in snap.warnings)


def test_collect_with_unexpected_schema(tmp_path: Path) -> None:
    import sqlite3

    p = make_spec_runner(tmp_path)
    db = p / "spec" / ".executor-state.db"
    db.unlink()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE something_else (id INTEGER)")
    conn.commit()
    conn.close()
    snap = SpecRunnerCollector().collect(p, _ctx(tmp_path))
    assert snap.schema_versions[0].ok is False
    assert snap.tasks == []
