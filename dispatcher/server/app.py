"""FastAPI application: read-only JSON API over collector snapshots."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dispatcher.core.actions import (
    Action,
    ActionBusyError,
    ActionOutcome,
    ActionRejectedError,
    ActionRunner,
)
from dispatcher.core.contracts import check_contracts
from dispatcher.core.correlation import WorkItemsResponse, build_work_items
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.models import (
    ContractStatus,
    ErrorEvent,
    OverviewEntry,
    OverviewResponse,
    ProjectSnapshot,
)
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
    default_roadmap_dirs,
)
from dispatcher.core.service import SnapshotService, recent_errors
from dispatcher.core.spec_runner_config import (
    ProjectSpecRunnerConfig,
    discover_project_configs,
)
from dispatcher.core.spec_runner_config_actions import (
    ConfigCandidate,
    SpecRunnerConfigActionRunner,
    SpecRunnerConfigBusyError,
    SpecRunnerConfigConflictError,
    SpecRunnerConfigRejectedError,
)
from dispatcher.core.spec_runner_config_schema import ConfigValidationError
from dispatcher.core.sync import HostPanel
from dispatcher.core.sync_service import SyncService, SyncStatus
from dispatcher.core.tracking import TrackAction, decide

__all__ = ["create_app", "recent_errors"]  # re-export: old import path

_STATIC_DIR = Path(__file__).parent / "static"


class TrackDecision(BaseModel):
    """POST /api/sync/track body: one confirm/reject decision."""

    dir: str
    action: TrackAction


class TrackingView(BaseModel):
    """Resulting decision sets after a tracking update."""

    tracked: list[str]
    ignored: list[str]


class SyncHostsResponse(BaseModel):
    """GET /api/sync/hosts: host panels with snapshot ages (DESIGN-207)."""

    current_host: str
    fetch_in_flight: bool
    hosts: list[HostPanel]


class ActionRequest(BaseModel):
    """POST /api/actions/{pull|create-pr} body."""

    dir: str


class ActionSession(BaseModel):
    """GET /api/actions/session: per-process CSRF token for action POSTs."""

    token: str


class UpdateSpecRunnerConfigRequest(BaseModel):
    """POST /api/actions/update-spec-runner-config body."""

    dir: str
    typed: dict[str, Any]
    extra_executor_config: dict[str, Any] = {}
    base_mtime: float


def create_app(config: DispatcherConfig) -> FastAPI:
    """Build the API app for the given configuration."""
    app = FastAPI(title="Dispatcher", version="0.1.0")
    cache = SnapshotService(config)
    sync_cache = SyncService(config)
    actions = ActionRunner(config)
    spec_runner_config_actions = SpecRunnerConfigActionRunner(config)
    # CSRF-токен на процесс: SOP не даст чужой странице его прочитать,
    # значит POST с токеном мог отправить только наш UI (DESIGN-204)
    action_token = secrets.token_hex(16)

    @app.get("/api/overview", response_model=OverviewResponse)
    def overview() -> OverviewResponse:
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

    @app.get("/api/projects/{name}", response_model=ProjectSnapshot)
    def project_detail(name: str) -> ProjectSnapshot:
        snapshots, _ = cache.get()
        for snap in snapshots:
            if snap.name == name:
                return snap
        raise HTTPException(status_code=404, detail=f"unknown project: {name}")

    @app.get("/api/errors", response_model=list[ErrorEvent])
    def errors(
        limit: int = Query(100, ge=0),
        days: int | None = Query(None, ge=1),
        project: str | None = Query(None),
        service: str | None = Query(None),
    ) -> list[ErrorEvent]:
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

    @app.get("/api/models")
    def models() -> list[dict[str, Any]]:
        snapshots, _ = cache.get()
        return [
            {"project": s.name, **m.model_dump()} for s in snapshots for m in s.models
        ]

    @app.get("/api/contracts", response_model=list[ContractStatus])
    def contracts() -> list[ContractStatus]:
        snapshots, _ = cache.get()
        projects = {s.name: Path(s.path) for s in snapshots if s.detected and s.path}
        return check_contracts(projects)

    @app.get("/api/work-items", response_model=WorkItemsResponse)
    def work_items(
        cross_only: bool = Query(False),
        limit: int = Query(100, ge=0),
    ) -> WorkItemsResponse:
        snapshots, _ = cache.get()
        result = build_work_items(snapshots)
        items = result.items
        if cross_only:
            items = [c for c in items if c.cross_project]
        return WorkItemsResponse(
            items=items[:limit],
            total=result.total,
            cross_project=result.cross_project,
        )

    roadmap_dirs = config.roadmap_dirs or default_roadmap_dirs(config.roots)

    @app.get("/api/roadmap", response_model=RoadmapResponse)
    def roadmap() -> RoadmapResponse:
        snapshots, _ = cache.get()
        return build_roadmap(roadmap_dirs, snapshots)

    # Registered before /{item_id} so "drift" is not matched as an item id.
    @app.get("/api/roadmap/drift", response_model=DriftResponse)
    def roadmap_drift() -> DriftResponse:
        snapshots, _ = cache.get()
        projects = {s.name: Path(s.path) for s in snapshots if s.detected and s.path}
        # One checker run feeds both the status projection and the join,
        # so computed_status and contract_in_sync cannot disagree (ADR-R5).
        contracts = check_contracts(projects)
        roadmap = build_roadmap(roadmap_dirs, snapshots, contracts)
        return build_drift(roadmap, contracts)

    # Registered before /{item_id} so "phases" is not matched as an item id.
    @app.get("/api/roadmap/phases", response_model=PhasesResponse)
    def roadmap_phases() -> PhasesResponse:
        snapshots, _ = cache.get()
        return build_phases(build_roadmap(roadmap_dirs, snapshots))

    # Registered before /{item_id} so "blockers" is not matched as an item id.
    @app.get("/api/roadmap/blockers", response_model=BlockersResponse)
    def roadmap_blockers() -> BlockersResponse:
        snapshots, _ = cache.get()
        return build_blockers(build_roadmap(roadmap_dirs, snapshots))

    @app.get("/api/roadmap/summary", response_model=SummaryResponse)
    def roadmap_summary() -> SummaryResponse:
        """Один экран FR-03: проекты × готовность × флаги lagging/drift."""
        snapshots, _ = cache.get()
        projects = {s.name: Path(s.path) for s in snapshots if s.detected and s.path}
        contracts_state = check_contracts(projects)
        roadmap = build_roadmap(roadmap_dirs, snapshots, contracts=contracts_state)
        return build_summary(roadmap, contracts_state)

    @app.get("/api/roadmap/{item_id}", response_model=RoadmapItemView)
    def roadmap_item(item_id: str) -> RoadmapItemView:
        snapshots, _ = cache.get()
        for item in build_roadmap(roadmap_dirs, snapshots).items:
            if item.id == item_id:
                return item
        raise HTTPException(status_code=404, detail=f"unknown roadmap item: {item_id}")

    @app.get("/api/sync", response_model=SyncStatus)
    def sync() -> SyncStatus:
        """Verdict table + top line + freshness metadata (corner spinner)."""
        return sync_cache.get()

    @app.get("/api/sync/hosts", response_model=SyncHostsResponse)
    def sync_hosts() -> SyncHostsResponse:
        status = sync_cache.get()
        return SyncHostsResponse(
            current_host=status.report.current_host,
            fetch_in_flight=status.fetch_in_flight,
            hosts=status.report.hosts,
        )

    @app.post("/api/sync/track", response_model=TrackingView)
    def sync_track(decision: TrackDecision) -> TrackingView:
        """Confirm/reject one auto-discovery proposal (writes only the sidecar)."""
        if config.tracking_file is None:
            raise HTTPException(status_code=409, detail="sync tracking not configured")
        repo_dir = decision.dir.strip()
        if not repo_dir:
            raise HTTPException(status_code=422, detail="empty repo dir")
        state = decide(config.tracking_file, repo_dir, decision.action)
        sync_cache.invalidate()
        return TrackingView(
            tracked=sorted(state.tracked), ignored=sorted(state.ignored)
        )

    @app.get("/api/actions/session", response_model=ActionSession)
    def action_session() -> ActionSession:
        return ActionSession(token=action_token)

    def _run_action(
        action: Action, request: ActionRequest, token: str | None
    ) -> ActionOutcome:
        if token != action_token:
            raise HTTPException(status_code=403, detail="bad or missing action token")
        try:
            outcome = actions.run(action, request.dir.strip())
        except ActionRejectedError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        except ActionBusyError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        if outcome.ok:
            sync_cache.invalidate()  # состояние репо изменилось — вердикты пересчитать
        return outcome

    @app.post("/api/actions/pull", response_model=ActionOutcome)
    def action_pull(
        request: ActionRequest,
        x_action_token: str | None = Header(default=None),
    ) -> ActionOutcome:
        """Явный клик человека: ff-only pull через github-checker (NFR-01)."""
        return _run_action("pull", request, x_action_token)

    @app.post("/api/actions/create-pr", response_model=ActionOutcome)
    def action_create_pr(
        request: ActionRequest,
        x_action_token: str | None = Header(default=None),
    ) -> ActionOutcome:
        """Явный клик человека: gh pr create через github-checker (идемпотентно)."""
        return _run_action("open-pr", request, x_action_token)

    @app.get(
        "/api/projects/{name}/spec-runner-config",
        response_model=ProjectSpecRunnerConfig,
    )
    def spec_runner_config_view(name: str) -> ProjectSpecRunnerConfig:
        configs, _ = discover_project_configs(config.roots)
        for cfg in configs:
            if Path(cfg.project_yaml_path).parent.name == name:
                return cfg
        raise HTTPException(status_code=404, detail=f"no project.yaml for: {name}")

    @app.post("/api/actions/update-spec-runner-config", response_model=ActionOutcome)
    def action_update_spec_runner_config(
        request: UpdateSpecRunnerConfigRequest,
        x_action_token: str | None = Header(default=None),
    ) -> ActionOutcome:
        """Явный клик человека: PR в spec_runner: блок project.yaml (DESIGN-304)."""
        if x_action_token != action_token:
            raise HTTPException(status_code=403, detail="bad or missing action token")
        candidate = ConfigCandidate(
            typed=request.typed,
            extra_executor_config=request.extra_executor_config,
            base_mtime=request.base_mtime,
        )
        try:
            return spec_runner_config_actions.run(request.dir.strip(), candidate)
        except (SpecRunnerConfigRejectedError, ConfigValidationError) as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        except (SpecRunnerConfigBusyError, SpecRunnerConfigConflictError) as err:
            raise HTTPException(status_code=409, detail=str(err)) from err

    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
    return app
