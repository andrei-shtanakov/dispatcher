"""FR-04 onboarding read-model (DESIGN-802): one canonical 'what next' join.

Dependency semantics S-1..S-4 (design spec §2): a dep is satisfied iff
its computed_status is in DONE_STATUSES; drift and unknown ids block
(pessimism under uncertainty, unknown ids also warn); only DIRECT deps —
an unmet transitive dep keeps the direct dep out of DONE_STATUSES, so
the effect cascades without recursion.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from dispatcher.core.models import (
    ContractStatus,
    DescriptionSource,
    ProjectSnapshot,
    TaskInfo,
)
from dispatcher.core.roadmap import (
    DONE_STATUSES,
    PhaseSummary,
    ProjectSummary,
    RoadmapResponse,
    build_phases,
    build_summary,
)

_LIVE_STATUSES = ("pending", "in_progress")


class OnboardingProject(BaseModel):
    """Identity block of the onboarding screen."""

    name: str
    path: str
    description: str | None = None
    description_source: DescriptionSource | None = None
    freshness: str | None = None


class OnboardingRoadmapPosition(BaseModel):
    """Where the project stands: its build_summary row + own-phase cuts."""

    summary: ProjectSummary
    median_readiness: float | None = None
    phases: list[PhaseSummary] = Field(default_factory=list)


class OnboardingNextItem(BaseModel):
    """One not-done roadmap item with the actionable/blocked verdict."""

    id: str
    title: str
    phase: str | None = None
    computed_status: str
    actionable: bool  # named actionable, NOT ready — see spec §2 naming
    blocked_by: list[str] = Field(default_factory=list)


class OnboardingView(BaseModel):
    """Response of GET /api/projects/{name}/onboarding (FR-04)."""

    project: OnboardingProject
    roadmap_position: OnboardingRoadmapPosition | None = None
    next_items: list[OnboardingNextItem] = Field(default_factory=list)
    live_tasks: list[TaskInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def build_onboarding(
    snapshot: ProjectSnapshot,
    roadmap: RoadmapResponse,
    contracts: list[ContractStatus],
) -> OnboardingView:
    """Pure join of one snapshot with the roadmap (style of build_summary)."""
    own = [i for i in roadmap.items if i.owner_project == snapshot.name]
    by_id = {i.id: i for i in roadmap.items}
    warnings = [*snapshot.warnings, *roadmap.warnings]

    next_items: list[OnboardingNextItem] = []
    for item in own:
        if item.computed_status in DONE_STATUSES:
            continue
        blocked_by = [
            dep
            for dep in item.depends_on
            if dep not in by_id or by_id[dep].computed_status not in DONE_STATUSES
        ]
        warnings.extend(
            f"unknown dependency id: {dep} (item {item.id})"
            for dep in item.depends_on
            if dep not in by_id
        )
        next_items.append(
            OnboardingNextItem(
                id=item.id,
                title=item.title,
                phase=item.phase,
                computed_status=item.computed_status,
                actionable=not blocked_by,
                blocked_by=blocked_by,
            )
        )
    next_items.sort(key=lambda n: (not n.actionable, n.phase or "", n.id))

    position: OnboardingRoadmapPosition | None = None
    if own:
        summary = build_summary(roadmap, contracts)
        row = next(p for p in summary.projects if p.project == snapshot.name)
        own_view = RoadmapResponse(roadmaps=roadmap.roadmaps, items=own, warnings=[])
        position = OnboardingRoadmapPosition(
            summary=row,
            median_readiness=summary.median_readiness,
            phases=build_phases(own_view).phases,
        )

    return OnboardingView(
        project=OnboardingProject(
            name=snapshot.name,
            path=snapshot.path,
            description=snapshot.description,
            description_source=snapshot.description_source,
            freshness=snapshot.freshness,
        ),
        roadmap_position=position,
        next_items=next_items,
        live_tasks=[t for t in snapshot.tasks if t.status in _LIVE_STATUSES],
        warnings=list(dict.fromkeys(warnings)),
    )
