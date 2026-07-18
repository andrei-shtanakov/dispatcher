"""Read facade: one implementation for the HTTP API and the MCP tools.

DESIGN-701: endpoint bodies live here; `server/app.py` routes and
`dispatcher/mcp_server.py` tools are thin delegations, so the two
surfaces cannot drift. Lookup misses raise ReadLookupError whose message
IS the HTTP detail text (the API maps it to 404, MCP to ToolError).
"""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.contracts import check_contracts
from dispatcher.core.correlation import WorkItemsResponse, build_work_items
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.models import (
    ContractStatus,
    ErrorEvent,
    ModelUsageRow,
    OverviewEntry,
    OverviewResponse,
    ProjectSnapshot,
)
from dispatcher.core.onboarding import OnboardingView, build_onboarding
from dispatcher.core.roadmap import (
    BlockersResponse,
    DriftResponse,
    PhasesResponse,
    RoadmapItemView,
    RoadmapResponse,
    SummaryResponse,
    build_blockers,
    build_drift,
    build_phases,
    build_roadmap,
    build_summary,
)
from dispatcher.core.service import SnapshotService, recent_errors
from dispatcher.core.spec_runner_config import (
    ProjectSpecRunnerConfig,
    discover_project_configs,
)
from dispatcher.core.sync_service import SyncService, SyncStatus


class ReadLookupError(Exception):
    """A read lookup missed; the message is the surface-agnostic detail."""


def overview(cache: SnapshotService) -> OverviewResponse:
    """One row per monitored project: freshness, counts, warnings."""
    snapshots, warnings = cache.get()
    entries = [
        OverviewEntry(
            name=s.name,
            path=s.path or None,
            detected=s.detected,
            freshness=s.freshness,
            counts={
                "tasks": len(s.tasks),
                "models": len(s.models),
                "test_results": len(s.test_results),
                "errors": len(s.errors),
            },
            warnings=s.warnings,
        )
        for s in snapshots
    ]
    return OverviewResponse(projects=entries, warnings=warnings)


def project(cache: SnapshotService, name: str) -> ProjectSnapshot:
    """Full snapshot of one project by its collector name."""
    snapshots, _ = cache.get()
    for snap in snapshots:
        if snap.name == name:
            return snap
    raise ReadLookupError(f"unknown project: {name}")


def errors(
    cache: SnapshotService,
    *,
    limit: int = 100,
    days: int | None = None,
    project: str | None = None,
    service: str | None = None,
) -> list[ErrorEvent]:
    """Merged error feed, newest first, with the HTTP defaults."""
    snapshots, _ = cache.get()
    if project is not None:
        snapshots = [s for s in snapshots if s.name == project]
    merged = [e for s in snapshots for e in s.errors]
    if service is not None:
        merged = [e for e in merged if e.service == service]
    if days is not None:
        merged = recent_errors(merged, days)
    merged.sort(key=lambda e: e.timestamp or "", reverse=True)
    return merged[:limit]


def models(cache: SnapshotService) -> list[ModelUsageRow]:
    """Every model referenced by any project's configs/catalogs."""
    snapshots, _ = cache.get()
    return [
        ModelUsageRow(project=s.name, **m.model_dump())
        for s in snapshots
        for m in s.models
    ]


def contracts(cache: SnapshotService) -> list[ContractStatus]:
    """Cross-repo contract sync state (canon vs vendored copies)."""
    snapshots, _ = cache.get()
    projects = {s.name: Path(s.path) for s in snapshots if s.detected and s.path}
    return check_contracts(projects)


def work_items(
    cache: SnapshotService, *, cross_only: bool = False, limit: int = 100
) -> WorkItemsResponse:
    """Tasks correlated across projects by shared task id."""
    snapshots, _ = cache.get()
    result = build_work_items(snapshots)
    items = result.items
    if cross_only:
        items = [c for c in items if c.cross_project]
    return WorkItemsResponse(
        items=items[:limit], total=result.total, cross_project=result.cross_project
    )


def roadmap(cache: SnapshotService, roadmap_dirs: tuple[Path, ...]) -> RoadmapResponse:
    """Roadmap items with computed status and evidence."""
    snapshots, _ = cache.get()
    return build_roadmap(roadmap_dirs, snapshots)


def roadmap_drift(
    cache: SnapshotService, roadmap_dirs: tuple[Path, ...]
) -> DriftResponse:
    """Roadmap items joined with live contract sync state (canonical join)."""
    snapshots, _ = cache.get()
    projects = {s.name: Path(s.path) for s in snapshots if s.detected and s.path}
    # One checker run feeds both the status projection and the join,
    # so computed_status and contract_in_sync cannot disagree (ADR-R5).
    contracts_state = check_contracts(projects)
    roadmap_state = build_roadmap(roadmap_dirs, snapshots, contracts_state)
    return build_drift(roadmap_state, contracts_state)


def roadmap_phases(
    cache: SnapshotService, roadmap_dirs: tuple[Path, ...]
) -> PhasesResponse:
    """Per-phase status counts and blocked lists."""
    snapshots, _ = cache.get()
    return build_phases(build_roadmap(roadmap_dirs, snapshots))


def roadmap_blockers(
    cache: SnapshotService, roadmap_dirs: tuple[Path, ...]
) -> BlockersResponse:
    """Reverse dependency view: what blocks what."""
    snapshots, _ = cache.get()
    return build_blockers(build_roadmap(roadmap_dirs, snapshots))


def roadmap_summary(
    cache: SnapshotService, roadmap_dirs: tuple[Path, ...]
) -> SummaryResponse:
    """Один экран FR-03: проекты × готовность × флаги lagging/drift."""
    snapshots, _ = cache.get()
    projects = {s.name: Path(s.path) for s in snapshots if s.detected and s.path}
    contracts_state = check_contracts(projects)
    roadmap_state = build_roadmap(roadmap_dirs, snapshots, contracts=contracts_state)
    return build_summary(roadmap_state, contracts_state)


def roadmap_item(
    cache: SnapshotService, roadmap_dirs: tuple[Path, ...], item_id: str
) -> RoadmapItemView:
    """One roadmap item by id."""
    snapshots, _ = cache.get()
    for item in build_roadmap(roadmap_dirs, snapshots).items:
        if item.id == item_id:
            return item
    raise ReadLookupError(f"unknown roadmap item: {item_id}")


def onboarding(
    cache: SnapshotService, roadmap_dirs: tuple[Path, ...], name: str
) -> OnboardingView:
    """FR-04 one-screen join: description, roadmap position, next items."""
    snapshots, _ = cache.get()
    snap = next((s for s in snapshots if s.name == name), None)
    if snap is None:
        raise ReadLookupError(f"unknown project: {name}")
    projects = {s.name: Path(s.path) for s in snapshots if s.detected and s.path}
    # One checker run feeds the roadmap projection AND the summary join,
    # same as roadmap_summary (ADR-R5).
    contracts_state = check_contracts(projects)
    roadmap_state = build_roadmap(roadmap_dirs, snapshots, contracts=contracts_state)
    return build_onboarding(snap, roadmap_state, contracts_state)


def sync_status(sync_cache: SyncService, *, start_fetch: bool = True) -> SyncStatus:
    """Sync verdicts + freshness; start_fetch=False for agent surfaces."""
    return sync_cache.get(start_fetch=start_fetch)


def spec_runner_configs(
    config: DispatcherConfig,
) -> list[ProjectSpecRunnerConfig]:
    """Every discovered project.yaml across all roots (basename-keyed)."""
    configs, _ = discover_project_configs(config.roots)
    return configs
