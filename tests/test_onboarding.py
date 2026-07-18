"""DESIGN-802: build_onboarding — the canonical 'what next' join (S-1..S-4)."""

from dispatcher.core.models import ProjectSnapshot, TaskInfo
from dispatcher.core.onboarding import build_onboarding
from dispatcher.core.roadmap import RoadmapItemView, RoadmapResponse


def _item(
    item_id: str,
    status: str,
    *,
    owner: str | None = "proj",
    phase: str | None = "1",
    deps: list[str] | None = None,
) -> RoadmapItemView:
    return RoadmapItemView(
        id=item_id,
        title=f"title {item_id}",
        phase=phase,
        owner_project=owner,
        depends_on=deps or [],
        computed_status=status,
        source="fixture.yaml",
    )


def _snap(**kwargs: object) -> ProjectSnapshot:
    return ProjectSnapshot(name="proj", path="/w/proj", **kwargs)  # type: ignore[arg-type]


def _view(items: list[RoadmapItemView], snap: ProjectSnapshot | None = None):
    roadmap = RoadmapResponse(roadmaps=["r"], items=items, warnings=[])
    return build_onboarding(snap or _snap(), roadmap, contracts=[])


def test_s1_verified_dep_makes_item_actionable() -> None:
    view = _view([_item("A", "planned", deps=["B"]), _item("B", "verified")])
    (next_a,) = [n for n in view.next_items if n.id == "A"]
    assert next_a.actionable is True
    assert next_a.blocked_by == []


def test_s1_planned_dep_blocks() -> None:
    view = _view([_item("A", "planned", deps=["B"]), _item("B", "planned")])
    (next_a,) = [n for n in view.next_items if n.id == "A"]
    assert next_a.actionable is False
    assert next_a.blocked_by == ["B"]


def test_s2_drift_dep_blocks() -> None:
    view = _view([_item("A", "planned", deps=["B"]), _item("B", "drift")])
    (next_a,) = [n for n in view.next_items if n.id == "A"]
    assert next_a.actionable is False
    assert next_a.blocked_by == ["B"]


def test_s3_unknown_dep_blocks_and_warns() -> None:
    view = _view([_item("A", "planned", deps=["GHOST"])])
    (next_a,) = view.next_items
    assert next_a.actionable is False
    assert next_a.blocked_by == ["GHOST"]
    assert "unknown dependency id: GHOST (item A)" in view.warnings


def test_non_planned_item_still_reports_blockers() -> None:
    # _apply_blocked only paints planned items; onboarding must see through
    view = _view([_item("A", "unknown", deps=["B"]), _item("B", "planned")])
    (next_a,) = [n for n in view.next_items if n.id == "A"]
    assert next_a.blocked_by == ["B"]


def test_done_items_are_not_next() -> None:
    view = _view([_item("A", "implemented"), _item("B", "verified")])
    assert view.next_items == []


def test_order_actionable_first_then_phase_then_id() -> None:
    view = _view(
        [
            _item("Z1", "planned", phase="1", deps=["GHOST"]),  # blocked ph1
            _item("A2", "planned", phase="2"),  # actionable ph2
            _item("A1", "planned", phase="1"),  # actionable ph1
            _item("A1B", "planned", phase="1"),  # actionable ph1, id after A1
        ]
    )
    assert [n.id for n in view.next_items] == ["A1", "A1B", "A2", "Z1"]


def test_foreign_items_are_excluded() -> None:
    view = _view([_item("X", "planned", owner="other")])
    assert view.next_items == []
    assert view.roadmap_position is None


def test_no_items_position_is_none_but_view_lives() -> None:
    snap = _snap(
        description="Desc.",
        description_source="readme",
        tasks=[TaskInfo(task_id="T-1", status="in_progress", source="db")],
    )
    view = _view([], snap)
    assert view.roadmap_position is None
    assert view.project.description == "Desc."
    assert [t.task_id for t in view.live_tasks] == ["T-1"]


def test_position_reuses_build_summary_row() -> None:
    view = _view([_item("A", "verified"), _item("B", "planned")])
    pos = view.roadmap_position
    assert pos is not None
    assert pos.summary.project == "proj"
    assert pos.summary.total == 2 and pos.summary.done == 1
    assert pos.median_readiness == 0.5
    assert [p.phase for p in pos.phases] == ["1"]


def test_live_tasks_filter_and_warning_merge_dedup() -> None:
    snap = _snap(
        tasks=[
            TaskInfo(task_id="T-1", status="pending", source="db"),
            TaskInfo(task_id="T-2", status="completed", source="db"),
            TaskInfo(task_id="T-3", status="in_progress", source="db"),
        ],
        warnings=["dup", "snap-only"],
    )
    roadmap = RoadmapResponse(roadmaps=["r"], items=[], warnings=["dup", "rm-only"])
    view = build_onboarding(snap, roadmap, contracts=[])
    assert [t.task_id for t in view.live_tasks] == ["T-1", "T-3"]
    assert view.warnings == ["dup", "snap-only", "rm-only"]
