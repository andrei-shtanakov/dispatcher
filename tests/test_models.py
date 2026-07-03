"""Tests for core pydantic models."""

from dispatcher.core.models import (
    ConfigSummary,
    ErrorEvent,
    ModelInUse,
    OverviewEntry,
    ProjectSnapshot,
    SchemaVersionCheck,
    TaskInfo,
    TestRunSummary,
)


def test_snapshot_defaults_are_empty() -> None:
    snap = ProjectSnapshot(name="x", path="/tmp/x")
    assert snap.detected is True
    assert snap.tasks == []
    assert snap.models == []
    assert snap.errors == []
    assert snap.warnings == []
    assert snap.freshness is None
    assert snap.collected_at is not None


def test_snapshot_serializes_to_json() -> None:
    snap = ProjectSnapshot(
        name="arbiter",
        path="/x",
        models=[ModelInUse(model_id="gpt-5.5", role="routable", source="a.toml")],
        tasks=[TaskInfo(task_id="t1", status="assign", source="db")],
        test_results=[TestRunSummary(run_id="r1", name="bench", source="db")],
        configs=[ConfigSummary(path="c.toml", format="toml")],
        errors=[ErrorEvent(body="boom", source="log")],
        schema_versions=[SchemaVersionCheck(database="d.db")],
        warnings=["w"],
    )
    data = snap.model_dump(mode="json")
    assert data["models"][0]["model_id"] == "gpt-5.5"
    assert data["errors"][0]["severity"] == "ERROR"


def test_overview_entry() -> None:
    entry = OverviewEntry(name="atp-platform", detected=False)
    assert entry.counts == {}
    assert entry.path is None
