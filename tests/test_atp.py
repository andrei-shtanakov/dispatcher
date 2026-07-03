"""Tests for the atp-platform collector."""

from pathlib import Path

from conftest import make_atp

from dispatcher.core.collectors.atp import AtpCollector
from dispatcher.core.collectors.base import CollectContext


def _ctx(tmp_path: Path) -> CollectContext:
    return CollectContext(home=tmp_path / "home")


def test_detect(tmp_path: Path) -> None:
    p = make_atp(tmp_path)
    assert AtpCollector().detect(p) is True
    assert AtpCollector().detect(tmp_path) is False


def test_collect_happy_path(tmp_path: Path) -> None:
    p = make_atp(tmp_path)
    snap = AtpCollector().collect(p, _ctx(tmp_path))
    ver = snap.schema_versions[0]
    assert (ver.found, ver.expected, ver.ok) == (
        "f1a2b3c4d5e6",
        "f1a2b3c4d5e6",
        True,
    )
    names = {t.name for t in snap.test_results}
    assert "suite/smoke" in names
    assert any(n.startswith("benchmark:") for n in names)
    assert "experiment_results.json" in names
    assert "_bench_output/r07/sweep.db" in names
    smoke = next(t for t in snap.test_results if t.name == "suite/smoke")
    assert (smoke.passed, smoke.failed, smoke.total) == (3, 0, 3)
    catalog_models = {m.model_id for m in snap.models if m.role == "catalog"}
    assert catalog_models == {"claude-sonnet-4-6", "gpt-5.5"}
    roles = {(m.model_id, m.role) for m in snap.models}
    assert ("claude-sonnet-4-6", "routable") in roles
    assert ("deepseek-chat", "enrolled") in roles
    assert ("gpt-4o-mini", "default") in roles
    cfg = snap.configs[0]
    assert cfg.summary["dashboard_secret_key"] == "***"
    assert snap.warnings == []


def test_collect_without_dashboard_db(tmp_path: Path) -> None:
    p = make_atp(tmp_path)
    (p / ".atp-dashboard.db").unlink()
    snap = AtpCollector().collect(p, _ctx(tmp_path))
    assert any("atp-dashboard" in w for w in snap.warnings)
    assert any(m.role == "catalog" for m in snap.models)


def test_collect_with_unstatable_bench_output(tmp_path: Path) -> None:
    p = make_atp(tmp_path)
    db = p / "_bench_output" / "r07" / "sweep.db"
    db.unlink()
    db.symlink_to(p / "nonexistent-target")
    snap = AtpCollector().collect(p, _ctx(tmp_path))
    names = {t.name for t in snap.test_results}
    assert "_bench_output/r07/sweep.db" not in names
    assert any("sweep.db" in w for w in snap.warnings)


def test_collect_catalog_skips_malformed_agent_entry(tmp_path: Path) -> None:
    p = make_atp(tmp_path)
    (p / "method" / "agents-catalog.toml").write_text(
        'agents = ["notadict"]\n\n'
        '[models."claude-sonnet-4-6"]\nvendor = "anthropic"\n'
        'status = "active"\n'
    )
    snap = AtpCollector().collect(p, _ctx(tmp_path))
    catalog_models = {m.model_id for m in snap.models if m.role == "catalog"}
    assert catalog_models == {"claude-sonnet-4-6"}
    assert not any(m.role in ("routable", "enrolled") for m in snap.models)
