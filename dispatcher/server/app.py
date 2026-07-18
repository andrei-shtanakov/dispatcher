"""FastAPI application: read-only JSON API over collector snapshots."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dispatcher.core import read_api
from dispatcher.core.actions import (
    Action,
    ActionBusyError,
    ActionOutcome,
    ActionRejectedError,
    ActionRunner,
)
from dispatcher.core.correlation import WorkItemsResponse
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.models import (
    ContractStatus,
    ErrorEvent,
    ModelUsageRow,
    OverviewResponse,
    ProjectSnapshot,
)
from dispatcher.core.onboarding import OnboardingView
from dispatcher.core.roadmap import (
    BlockersResponse,
    DriftResponse,
    PhasesResponse,
    RoadmapItemView,
    RoadmapResponse,
    SummaryResponse,
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
from dispatcher.core.suggest_bundle import build_suggest_bundle
from dispatcher.core.suggest_cli import (
    SuggestCancelledError,
    SuggestInvalidError,
    SuggestOutcome,
    SuggestRunner,
    SuggestRunnerBusyError,
    SuggestTimeoutError,
    SuggestUnavailableError,
)
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
    # Tri-state: None (omitted) preserves the current file's overlay;
    # {} is an intentional clear; non-empty replaces it (X-02 Copilot
    # round 1 on PR #40).
    extra_executor_config: dict[str, Any] | None = None
    base_mtime: float


class SuggestRequest(BaseModel):
    """POST .../suggest body: mtime of the config the form was built from."""

    base_mtime: float


class CancelResponse(BaseModel):
    """POST .../suggest/cancel result."""

    cancelled: bool


class SuggestAvailability(BaseModel):
    """GET /api/spec-runner-config/suggest-availability result."""

    available: bool
    detail: str | None = None


def create_app(
    config: DispatcherConfig,
    *,
    snapshot_service: SnapshotService | None = None,
    sync_service: SyncService | None = None,
    suggest_runner: SuggestRunner | None = None,
) -> FastAPI:
    """Build the API app for the given configuration."""
    app = FastAPI(title="Dispatcher", version="0.1.0")
    # explicit is-None: a falsey mock/service must not be silently replaced
    cache = (
        snapshot_service if snapshot_service is not None else SnapshotService(config)
    )
    sync_cache = sync_service if sync_service is not None else SyncService(config)
    actions = ActionRunner(config)
    spec_runner_config_actions = SpecRunnerConfigActionRunner(config)
    suggest = suggest_runner if suggest_runner is not None else SuggestRunner(config)
    _suggest_audit = logging.getLogger("dispatcher.actions.spec_runner_config")
    # CSRF-токен на процесс: SOP не даст чужой странице его прочитать,
    # значит POST с токеном мог отправить только наш UI (DESIGN-204)
    action_token = secrets.token_hex(16)

    @app.get("/api/overview", response_model=OverviewResponse)
    def overview() -> OverviewResponse:
        return read_api.overview(cache)

    @app.get("/api/projects/{name}", response_model=ProjectSnapshot)
    def project_detail(name: str) -> ProjectSnapshot:
        try:
            return read_api.project(cache, name)
        except read_api.ReadLookupError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    @app.get("/api/errors", response_model=list[ErrorEvent])
    def errors(
        limit: int = Query(100, ge=0),
        days: int | None = Query(None, ge=1),
        project: str | None = Query(None),
        service: str | None = Query(None),
    ) -> list[ErrorEvent]:
        return read_api.errors(
            cache, limit=limit, days=days, project=project, service=service
        )

    @app.get("/api/models", response_model=list[ModelUsageRow])
    def models() -> list[ModelUsageRow]:
        return read_api.models(cache)

    @app.get("/api/contracts", response_model=list[ContractStatus])
    def contracts() -> list[ContractStatus]:
        return read_api.contracts(cache)

    @app.get("/api/work-items", response_model=WorkItemsResponse)
    def work_items(
        cross_only: bool = Query(False),
        limit: int = Query(100, ge=0),
    ) -> WorkItemsResponse:
        return read_api.work_items(cache, cross_only=cross_only, limit=limit)

    roadmap_dirs = config.roadmap_dirs or default_roadmap_dirs(config.roots)

    @app.get("/api/roadmap", response_model=RoadmapResponse)
    def roadmap() -> RoadmapResponse:
        return read_api.roadmap(cache, roadmap_dirs)

    # Registered before /{item_id} so "drift" is not matched as an item id.
    @app.get("/api/roadmap/drift", response_model=DriftResponse)
    def roadmap_drift() -> DriftResponse:
        return read_api.roadmap_drift(cache, roadmap_dirs)

    # Registered before /{item_id} so "phases" is not matched as an item id.
    @app.get("/api/roadmap/phases", response_model=PhasesResponse)
    def roadmap_phases() -> PhasesResponse:
        return read_api.roadmap_phases(cache, roadmap_dirs)

    # Registered before /{item_id} so "blockers" is not matched as an item id.
    @app.get("/api/roadmap/blockers", response_model=BlockersResponse)
    def roadmap_blockers() -> BlockersResponse:
        return read_api.roadmap_blockers(cache, roadmap_dirs)

    @app.get("/api/roadmap/summary", response_model=SummaryResponse)
    def roadmap_summary() -> SummaryResponse:
        """Один экран FR-03: проекты × готовность × флаги lagging/drift."""
        return read_api.roadmap_summary(cache, roadmap_dirs)

    @app.get("/api/projects/{name}/onboarding", response_model=OnboardingView)
    def project_onboarding(name: str) -> OnboardingView:
        """FR-04: описание + позиция в roadmap + предстоящие задачи."""
        try:
            return read_api.onboarding(cache, roadmap_dirs, name)
        except read_api.ReadLookupError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    @app.get("/api/roadmap/{item_id}", response_model=RoadmapItemView)
    def roadmap_item(item_id: str) -> RoadmapItemView:
        try:
            return read_api.roadmap_item(cache, roadmap_dirs, item_id)
        except read_api.ReadLookupError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    @app.get("/api/sync", response_model=SyncStatus)
    def sync() -> SyncStatus:
        """Verdict table + top line + freshness metadata (corner spinner)."""
        return read_api.sync_status(sync_cache)

    @app.get("/api/sync/hosts", response_model=SyncHostsResponse)
    def sync_hosts() -> SyncHostsResponse:
        status = read_api.sync_status(sync_cache)
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

    @app.get("/api/spec-runner-configs", response_model=list[ProjectSpecRunnerConfig])
    def spec_runner_configs_list() -> list[ProjectSpecRunnerConfig]:
        """Enumerate every discovered project.yaml across all roots.

        Basename-keyed action contract: the action key is the directory
        NAME. Same-named dirs in two roots appear twice here and BOTH
        resolve to the first root at action time — fail-closed via the
        base_mtime conflict (409), but visible as duplicates. Closes the
        DISCOVERY gap (no other endpoint lists names); fetching a known
        name was already possible via the per-name GET.
        """
        return read_api.spec_runner_configs(config)

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

    @app.get(
        "/api/spec-runner-config/suggest-availability",
        response_model=SuggestAvailability,
    )
    def spec_runner_config_suggest_availability() -> SuggestAvailability:
        detail = suggest.availability()
        return SuggestAvailability(available=detail is None, detail=detail)

    @app.post(
        "/api/projects/{name}/spec-runner-config/suggest",
        response_model=SuggestOutcome,
        response_model_exclude={"cli_version"},
    )
    def spec_runner_config_suggest(
        name: str,
        request: SuggestRequest,
        x_action_token: str | None = Header(default=None),
    ) -> SuggestOutcome:
        """Явный клик человека: CLI-вызов ТРАТИТ ДЕНЬГИ — токен обязателен."""
        if x_action_token != action_token:
            raise HTTPException(status_code=403, detail="bad or missing action token")
        configs, _ = discover_project_configs(config.roots)
        target = next(
            (c for c in configs if Path(c.project_yaml_path).parent.name == name),
            None,
        )
        if target is None:
            raise HTTPException(status_code=404, detail=f"no project.yaml for: {name}")
        if target.base_mtime != request.base_mtime:
            raise HTTPException(
                status_code=409, detail="config changed — reload the form"
            )
        peers = [c for c in configs if c is not target]
        snapshots, _w = cache.get()
        target_dir = str(Path(target.project_yaml_path).parent)
        snap = next((s for s in snapshots if s.path == target_dir), None)
        bundle = build_suggest_bundle(target, peers, snap)
        requested = set(bundle["requested_fields"])
        try:
            outcome = suggest.run(name, bundle, requested)
        except SuggestUnavailableError as err:
            _suggest_audit.info("action=suggest project=%s outcome=unavailable", name)
            raise HTTPException(status_code=503, detail=str(err)) from err
        except SuggestRunnerBusyError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        except SuggestTimeoutError as err:
            _suggest_audit.info("action=suggest project=%s outcome=timeout", name)
            raise HTTPException(status_code=409, detail=str(err)) from err
        except SuggestCancelledError as err:
            _suggest_audit.info("action=suggest project=%s outcome=cancelled", name)
            raise HTTPException(status_code=409, detail="cancelled") from err
        except SuggestInvalidError as err:
            _suggest_audit.info("action=suggest project=%s outcome=invalid", name)
            raise HTTPException(status_code=422, detail=str(err)) from err
        _suggest_audit.info(
            "action=suggest project=%s outcome=ok duration=%.1fs fields=%s "
            "dropped=%s cost=%s cli=%s",
            name,
            outcome.duration_s,
            sorted(outcome.suggestions),
            outcome.dropped,
            outcome.cost_usd,
            outcome.cli_version,
        )
        return outcome

    @app.post(
        "/api/projects/{name}/spec-runner-config/suggest/cancel",
        response_model=CancelResponse,
    )
    def spec_runner_config_suggest_cancel(
        name: str,
        x_action_token: str | None = Header(default=None),
    ) -> CancelResponse:
        if x_action_token != action_token:
            raise HTTPException(status_code=403, detail="bad or missing action token")
        try:
            return CancelResponse(cancelled=suggest.cancel(name))
        except SuggestRunnerBusyError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err

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
