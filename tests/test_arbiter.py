"""Tests for the arbiter collector."""

from pathlib import Path

from conftest import make_arbiter

from dispatcher.core.collectors.arbiter import ArbiterCollector
from dispatcher.core.collectors.base import CollectContext


def _ctx(tmp_path: Path) -> CollectContext:
    return CollectContext(home=tmp_path / "home")


def test_detect(tmp_path: Path) -> None:
    p = make_arbiter(tmp_path)
    assert ArbiterCollector().detect(p) is True
    assert ArbiterCollector().detect(tmp_path) is False


def test_collect_happy_path(tmp_path: Path) -> None:
    p = make_arbiter(tmp_path)
    snap = ArbiterCollector().collect(p, _ctx(tmp_path))
    ver = snap.schema_versions[0]
    assert (ver.found, ver.expected, ver.ok) == ("1", "1", True)
    assert snap.tasks[0].task_id == "T-9"
    assert snap.tasks[0].status == "assign"
    assert snap.test_results[0].run_id == "R-1"
    assert snap.test_results[0].score == 0.83
    routable = {(m.harness, m.model_id) for m in snap.models}
    assert ("claude_code", "claude-sonnet-4-6") in routable
    assert ("codex_cli", "gpt-5.5") in routable
    assert ("aider", "aider") in routable
    assert any(e.body == "subprocess failed" for e in snap.errors)
    assert snap.warnings == []


def test_collect_without_db(tmp_path: Path) -> None:
    p = make_arbiter(tmp_path)
    (p / "arbiter.db").unlink()
    snap = ArbiterCollector().collect(p, _ctx(tmp_path))
    assert snap.tasks == []
    assert any("arbiter.db" in w for w in snap.warnings)
    assert len(snap.models) == 3  # config still readable
