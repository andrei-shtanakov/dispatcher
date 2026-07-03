"""Tests for the Maestro collector."""

from pathlib import Path

from conftest import make_atp, make_maestro, make_maestro_home

from dispatcher.core.collectors.base import CollectContext
from dispatcher.core.collectors.maestro import MaestroCollector


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
