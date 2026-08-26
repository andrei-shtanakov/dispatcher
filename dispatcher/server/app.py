"""FastAPI application: read-only JSON API over collector snapshots."""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi import Path as FastapiPath
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
from dispatcher.core.benchmark_service import BenchmarkService
from dispatcher.core.benchmarks import BenchmarksStatus, RunStatusReport
from dispatcher.core.correlation import WorkItemsResponse
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.epics import (
    DefectRow,
    EpicDetail,
    EpicsView,
    build_detail,
    build_view,
)
from dispatcher.core.governance import BundleGovernance
from dispatcher.core.models import (
    ContractStatus,
    ErrorEvent,
    ModelUsageRow,
    OverviewResponse,
    ProjectSnapshot,
)
from dispatcher.core.onboarding import OnboardingView
from dispatcher.core.product_proposals import ProductProposalsReport
from dispatcher.core.roadmap import (
    BlockersResponse,
    DriftResponse,
    PhasesResponse,
    RoadmapItemView,
    RoadmapResponse,
    SummaryResponse,
    default_roadmap_dirs,
)
from dispatcher.core.run_controller import (
    ControlPlaneOff,
    LaunchReceipt,
    RunController,
    RunLogs,
    RunView,
    TaskLog,
    UnknownResolution,
    VerbOutcome,
)
from dispatcher.core.run_identity import IdentityError, RepoKey, identity_from_checkout
from dispatcher.core.run_request import RunRejectedError, RunRequest
from dispatcher.core.run_store import (
    GuardBusyError,
    LaunchRecord,
    RunStore,
    RunStoreError,
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
from dispatcher.core.sync import HostPanel, kb_snapshot_dirs, load_kb_snapshots
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


class TaskRequest(BaseModel):
    """POST /api/actions/request-task body.

    `from` is deliberately absent: the server supplies it. It is written
    into the issue's structural block, so a client-settable value would be
    a way to forge the sender.
    """

    dir: str
    slug: str
    title: str
    prose: str


class MergeRequest(BaseModel):
    """POST /api/actions/merge-and-sync body."""

    dir: str
    pr: int
    if_head: str


class ActionSession(BaseModel):
    """GET /api/actions/session: per-process CSRF token for action POSTs."""

    token: str


class ResolveRequest(BaseModel):
    """POST /api/runs/{id}/resolve — optional named orphan to end."""

    run_id: str | None = None
    outcome: str | None = None


class AcknowledgeVanishedRequest(BaseModel):
    """POST /api/runs/{id}/acknowledge-vanished — the audited escape
    (spec §8.3). `display_name` is self-reported and never the actor of
    record — the base actor string is server-assigned."""

    confirm_run_id: str
    reason: str
    display_name: str | None = None


class ReleaseMalformedLockRequest(BaseModel):
    """POST /api/locks/release-malformed — the audited escape (spec §8.3).
    The lock PATH is computed server-side from a `repo_key` verified
    against a real checkout under a configured root — a client never
    names a file. `display_name` is self-reported, same rule as
    `AcknowledgeVanishedRequest`."""

    repo_key: str
    confirm_repo_key: str
    reason: str
    display_name: str | None = None


class VerbRequest(BaseModel):
    """POST /api/runs/{id}/verb — one Mode-1 control verb."""

    verb: str
    task_id: str | None = None
    outcome: str | None = None


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
    benchmark_service: BenchmarkService | None = None,
) -> FastAPI:
    """Build the API app for the given configuration."""
    app = FastAPI(title="Dispatcher", version="0.1.0")
    # explicit is-None: a falsey mock/service must not be silently replaced
    cache = (
        snapshot_service if snapshot_service is not None else SnapshotService(config)
    )
    sync_cache = sync_service if sync_service is not None else SyncService(config)
    actions = ActionRunner(config)
    runs = RunController(config)
    spec_runner_config_actions = SpecRunnerConfigActionRunner(config)
    suggest = suggest_runner if suggest_runner is not None else SuggestRunner(config)
    benchmarks_service = (
        benchmark_service
        if benchmark_service is not None
        else (
            BenchmarkService(
                config.benchmarks_url, token_file=config.benchmarks_token_file
            )
            if config.benchmarks_url
            else None
        )
    )
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

    @app.get("/api/projects/{name}/governance", response_model=BundleGovernance)
    def project_governance(name: str) -> BundleGovernance:
        """WS-005 WS-C: read-only bundle state (ARCH-C2: GET only)."""
        try:
            return read_api.governance(cache, name)
        except read_api.ReadLookupError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    @app.get(
        "/api/projects/{name}/product-proposals",
        response_model=ProductProposalsReport,
    )
    def project_product_proposals(name: str) -> ProductProposalsReport:
        """Inbox #129: read-only gate_waiting (GET only; producer decides —
        dispatcher renders). 404 codes are structured so the panel can tell
        «not this kind of project» from «unknown project»."""
        try:
            return read_api.product_proposals(cache, name)
        except read_api.ReadLookupError as err:
            raise HTTPException(
                status_code=404,
                detail={"code": "project-not-found", "message": str(err)},
            ) from err
        except read_api.NotImpresarioMirrorError as err:
            raise HTTPException(
                status_code=404,
                detail={"code": "not-impresario-mirror", "message": str(err)},
            ) from err

    @app.get("/api/roadmap/{item_id}", response_model=RoadmapItemView)
    def roadmap_item(item_id: str) -> RoadmapItemView:
        try:
            return read_api.roadmap_item(cache, roadmap_dirs, item_id)
        except read_api.ReadLookupError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    def _epic_snapshots() -> tuple[list[Any], list[tuple[str, str]]]:
        """Published snapshots for the GitHub planes — never a live GitHub call.

        Dispatcher does not talk to the GitHub API (ADR-ECO-004 D1). If nothing is
        published, the planes come back `unavailable`, which is the honest answer and
        deliberately not zero. The load ERRORS travel with the snapshots: "nothing was
        published" and "what was published could not be read" are different facts, and
        dropping the second one would leave `unavailable` without its reason.
        """
        return load_kb_snapshots(kb_snapshot_dirs(config.roots))

    @app.get("/api/epics", response_model=EpicsView)
    def epics(
        kind: Annotated[str | None, Query(pattern="^(ecosystem|external)$")] = None,
    ) -> EpicsView:
        """Programs → epics, per-plane counts, and the always-present unclassified bucket.

        `kind` filters EPICS only. The bucket survives every filter: an unmarked
        artifact belongs to no program, so filtering it away would hand back a partial
        aggregate with nothing marking it partial.
        """
        snapshots, errors = _epic_snapshots()
        return build_view(config, snapshots, kind=kind, snapshot_errors=errors)

    @app.get("/api/epics/{epic_id}", response_model=EpicDetail)
    def epic_detail(epic_id: str) -> EpicDetail:
        snapshots, _ = _epic_snapshots()
        detail = build_detail(config, epic_id, snapshots)
        if detail is None:
            raise HTTPException(status_code=404, detail=f"unknown epic {epic_id!r}")
        return detail

    @app.get("/api/defects", response_model=list[DefectRow])
    def defects() -> list[DefectRow]:
        """The reverse cut: defect class × epic — where the fleet breaks most."""
        snapshots, errors = _epic_snapshots()
        return build_view(config, snapshots, snapshot_errors=errors).defects

    @app.get("/api/sync", response_model=SyncStatus)
    def sync() -> SyncStatus:
        """Verdict table + top line + freshness metadata (corner spinner)."""
        return read_api.sync_status(sync_cache)

    @app.get("/api/benchmarks", response_model=BenchmarksStatus)
    def benchmarks_view() -> BenchmarksStatus:
        """Spec §7: global read-only report; state lives in the body (200 always)."""
        return read_api.benchmarks(benchmarks_service)

    @app.get("/api/benchmarks/runs/{run_id}", response_model=RunStatusReport)
    def benchmark_run_status_view(
        run_id: Annotated[int, FastapiPath(ge=1)],
    ) -> RunStatusReport:
        """Phase-2 §5: token-gated run status, ONE outbound GET on explicit
        human action (precedent: GET /api/pr-detail). Every valid run_id
        (ge=1; below that FastAPI answers 422) gets a 200 whose state —
        including every token-file failure mode — lives in the body."""
        return read_api.benchmark_run_status(benchmarks_service, run_id)

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

    @app.get("/api/pr-detail", response_model=ActionOutcome)
    def pr_detail(dir: str, pr: int) -> ActionOutcome:
        """Read-through to github-checker; no mutation, so no token."""
        try:
            return actions.pr_detail(dir.strip(), pr)
        except ActionRejectedError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    @app.post("/api/actions/merge-and-sync", response_model=ActionOutcome)
    def action_merge_and_sync(
        request: MergeRequest,
        x_action_token: str | None = Header(default=None),
    ) -> ActionOutcome:
        """Явный клик человека: squash-merge + локальная синхронизация одним локом."""
        if x_action_token != action_token:
            raise HTTPException(status_code=403, detail="bad or missing action token")
        try:
            outcome = actions.merge_and_sync(
                request.dir.strip(), request.pr, request.if_head.strip()
            )
        except ActionRejectedError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        except ActionBusyError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        if outcome.ok:
            sync_cache.invalidate()
        return outcome

    @app.post("/api/actions/post-merge-sync", response_model=ActionOutcome)
    def action_post_merge_sync(
        request: ActionRequest,
        x_action_token: str | None = Header(default=None),
    ) -> ActionOutcome:
        """Добор локальной половины, когда merge прошёл, а sync — нет."""
        return _run_action("post-merge-sync", request, x_action_token)

    @app.get("/api/issue-lookup", response_model=ActionOutcome)
    def issue_lookup(dir: str, slug: str) -> ActionOutcome:
        """Read-through to github-checker; no mutation, so no token."""
        try:
            return actions.issue_lookup(dir.strip(), slug.strip())
        except ActionRejectedError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    @app.post("/api/actions/request-task", response_model=ActionOutcome)
    def action_request_task(
        request: TaskRequest,
        x_action_token: str | None = Header(default=None),
    ) -> ActionOutcome:
        """Явный клик человека: завести inbox-issue в целевом репо."""
        if x_action_token != action_token:
            raise HTTPException(status_code=403, detail="bad or missing action token")
        try:
            outcome = actions.request_task(
                request.dir.strip(),
                slug=request.slug.strip(),
                sender="dispatcher",  # never from the client — see TaskRequest
                title=request.title.strip(),
                prose=request.prose,
            )
        except ActionRejectedError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        except ActionBusyError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        return outcome

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
        "/api/projects/{dir_name}/spec-runner-config",
        response_model=ProjectSpecRunnerConfig,
    )
    def spec_runner_config_view(dir_name: str) -> ProjectSpecRunnerConfig:
        # Directory-keyed (matches the ON-DISK clone dirname), unlike the
        # sibling /api/projects/{name}/onboarding and /governance routes,
        # which key on the collector's display name. The path parameter is
        # named `dir_name` (not `name`) specifically so a caller can't
        # mistake this for a display-name lookup — see the client-side
        # data-dir comment in server/static/index.html's detail().
        configs, _ = discover_project_configs(config.roots)
        for cfg in configs:
            if Path(cfg.project_yaml_path).parent.name == dir_name:
                return cfg
        raise HTTPException(status_code=404, detail=f"no project.yaml for: {dir_name}")

    @app.get(
        "/api/spec-runner-config/suggest-availability",
        response_model=SuggestAvailability,
    )
    def spec_runner_config_suggest_availability() -> SuggestAvailability:
        detail = suggest.availability()
        return SuggestAvailability(available=detail is None, detail=detail)

    @app.post(
        "/api/projects/{dir_name}/spec-runner-config/suggest",
        response_model=SuggestOutcome,
        response_model_exclude={"cli_version"},
    )
    def spec_runner_config_suggest(
        dir_name: str,
        request: SuggestRequest,
        x_action_token: str | None = Header(default=None),
    ) -> SuggestOutcome:
        """Явный клик человека: CLI-вызов ТРАТИТ ДЕНЬГИ — токен обязателен."""
        if x_action_token != action_token:
            raise HTTPException(status_code=403, detail="bad or missing action token")
        configs, _ = discover_project_configs(config.roots)
        target = next(
            (c for c in configs if Path(c.project_yaml_path).parent.name == dir_name),
            None,
        )
        if target is None:
            raise HTTPException(
                status_code=404, detail=f"no project.yaml for: {dir_name}"
            )
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
            outcome = suggest.run(dir_name, bundle, requested)
        except SuggestUnavailableError as err:
            _suggest_audit.info(
                "action=suggest project=%s outcome=unavailable", dir_name
            )
            raise HTTPException(status_code=503, detail=str(err)) from err
        except SuggestTimeoutError as err:
            _suggest_audit.info("action=suggest project=%s outcome=timeout", dir_name)
            raise HTTPException(status_code=409, detail=str(err)) from err
        except SuggestCancelledError as err:
            _suggest_audit.info("action=suggest project=%s outcome=cancelled", dir_name)
            raise HTTPException(status_code=409, detail="cancelled") from err
        except SuggestInvalidError as err:
            _suggest_audit.info("action=suggest project=%s outcome=invalid", dir_name)
            raise HTTPException(status_code=422, detail=str(err)) from err
        except SuggestRunnerBusyError as err:
            _suggest_audit.info("action=suggest project=%s outcome=busy", dir_name)
            raise HTTPException(status_code=409, detail=str(err)) from err
        _suggest_audit.info(
            "action=suggest project=%s outcome=ok duration=%.1fs fields=%s "
            "dropped=%s cost=%s cli=%s",
            dir_name,
            outcome.duration_s,
            sorted(outcome.suggestions),
            outcome.dropped,
            outcome.cost_usd,
            outcome.cli_version,
        )
        return outcome

    @app.post(
        "/api/projects/{dir_name}/spec-runner-config/suggest/cancel",
        response_model=CancelResponse,
    )
    def spec_runner_config_suggest_cancel(
        dir_name: str,
        x_action_token: str | None = Header(default=None),
    ) -> CancelResponse:
        if x_action_token != action_token:
            raise HTTPException(status_code=403, detail="bad or missing action token")
        try:
            return CancelResponse(cancelled=suggest.cancel(dir_name))
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

    def _require_token(token: str | None) -> None:
        if token != action_token:
            raise HTTPException(status_code=403, detail="bad or missing action token")

    def _resolve_known_repo_key(repo_key: str) -> RepoKey | None:
        """The `RepoKey` a real checkout under a configured root actually
        reports, matching `repo_key` — never a `RepoKey` built directly
        from the client's string, which would let an arbitrary value
        reach `safe_path_parts` for a repo dispatcher never checked out
        (spec §8.3: the lock path is computed server-side, from identity
        dispatcher can verify, not named by the caller).
        """
        for root in config.roots:
            if not root.is_dir():
                continue
            for candidate in root.iterdir():
                if not candidate.is_dir():
                    continue
                try:
                    found = identity_from_checkout(candidate)
                except IdentityError:
                    continue
                if found.as_text() == repo_key:
                    return found
        return None

    @app.post("/api/runs/submit", response_model=LaunchReceipt)
    def submit_run(
        request: RunRequest,
        x_action_token: str | None = Header(default=None),
    ) -> LaunchReceipt:
        """Explicit human click: start one Mode-1 run (spec §5.3).

        Every outcome is a receipt, including a refusal: `accepted` is
        three-valued and `null` (launch_unknown) is not an error.
        """
        _require_token(x_action_token)
        return runs.submit(request)

    @app.get("/api/runs/{request_id}", response_model=RunView)
    def read_run(request_id: str) -> RunView:
        """Read-through to the launch record, joined to maestro's own run
        row (spec §3.2); no mutation, no token."""
        try:
            return runs.view(request_id)
        except RunRejectedError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        except ControlPlaneOff as err:
            # Matches `/resolve` and `/verb`: "the feature is off" is not
            # the same fact as "no such request", and 404 told an operator
            # the wrong one of the two.
            raise HTTPException(status_code=409, detail=str(err)) from err

    @app.get("/api/runs/{request_id}/logs", response_model=RunLogs)
    def run_logs(request_id: str) -> RunLogs:
        """maestro's event timeline for this run, plus its task-log names.

        A READ, so no action token: it changes nothing, exactly like
        `GET /api/runs/{request_id}`. Separate from that view on purpose —
        the console polls the view every five seconds, and re-reading a
        growing log file on every tick would be waste the operator pays for.
        """
        try:
            return runs.logs(request_id)
        except RunRejectedError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        except ControlPlaneOff as err:
            raise HTTPException(status_code=409, detail=str(err)) from err

    @app.get("/api/runs/{request_id}/logs/{task_id}", response_model=TaskLog)
    def run_task_log(request_id: str, task_id: str) -> TaskLog:
        """The tail of one task's log."""
        try:
            return runs.task_log(request_id, task_id)
        except RunRejectedError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        except ControlPlaneOff as err:
            raise HTTPException(status_code=409, detail=str(err)) from err

    @app.post("/api/runs/{request_id}/resolve", response_model=UnknownResolution)
    def resolve_run(
        request_id: str,
        request: ResolveRequest,
        x_action_token: str | None = Header(default=None),
    ) -> UnknownResolution:
        """Adopt an unambiguous orphan, or end the one the operator names."""
        _require_token(x_action_token)
        if (request.run_id is None) != (request.outcome is None):
            # `run_id` and `outcome` are one operator decision — "end THIS
            # run THIS way" — so the endpoint enforces the pairing in both
            # directions. Half of it is a malformed request, not an empty
            # one, and the two halves fail differently if waved through:
            # a run_id alone reaches `end_orphan(..., "")`, which reports
            # "outcome must be cancelled|superseded, got ''" as though the
            # CALLER sent that value; an outcome alone is silently dropped
            # and the request falls through to `resolve_unknown`, so the
            # caller asks to END a run and gets an ADOPTION attempt with no
            # sign anything was ignored. The controller's own check stays —
            # `end_orphan` is also reachable from the TUI, MCP and VSCode
            # surfaces, which do not go through this endpoint.
            raise HTTPException(
                status_code=422,
                detail=(
                    "run_id and outcome must be given together: both to end "
                    "a named orphan (outcome cancelled|superseded), or "
                    "neither to attempt adoption"
                ),
            )
        try:
            if request.run_id is not None:
                return runs.end_orphan(
                    request_id, request.run_id, request.outcome or ""
                )
            return runs.resolve_unknown(request_id)
        except RunRejectedError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        except ControlPlaneOff as err:
            raise HTTPException(status_code=409, detail=str(err)) from err

    @app.post(
        "/api/runs/{request_id}/acknowledge-vanished", response_model=LaunchRecord
    )
    def acknowledge_vanished_run(
        request_id: str,
        request: AcknowledgeVanishedRequest,
        x_action_token: str | None = Header(default=None),
    ) -> LaunchRecord:
        """Audited administrative release of a fail-closed `run_vanished`
        block (spec §8.3): the record keeps who attested the run was gone,
        not just that someone did."""
        _require_token(x_action_token)
        try:
            return runs.acknowledge_vanished(
                request_id,
                request.confirm_run_id,
                request.reason,
                request.display_name,
            )
        except RunRejectedError as err:
            # 409, not 422 (unlike `/resolve` above): every refusal here
            # is "the current state doesn't allow this" — still exists,
            # wrong confirm_run_id, terminal already, unreadable — a
            # conflict with what the caller asserted, not a malformed
            # request body (PR-C restructures `/resolve` to match).
            raise HTTPException(status_code=409, detail=str(err)) from err
        except GuardBusyError as err:
            raise HTTPException(status_code=409, detail=f"guard_busy: {err}") from err
        except ControlPlaneOff as err:
            raise HTTPException(status_code=409, detail=str(err)) from err

    @app.post("/api/locks/release-malformed")
    def release_malformed_lock_route(
        request: ReleaseMalformedLockRequest,
        x_action_token: str | None = Header(default=None),
    ) -> dict[str, object]:
        """Audited administrative quarantine of a MALFORMED lock file
        (spec §8.3) — never a healthy one; `RunStore.release_malformed_lock`
        proves that itself, under the guard, before anything is moved."""
        _require_token(x_action_token)
        if request.confirm_repo_key != request.repo_key:
            raise HTTPException(
                status_code=409,
                detail="confirm_repo_key does not match repo_key — retype it",
            )
        key = _resolve_known_repo_key(request.repo_key)
        if key is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"unknown repo_key: {request.repo_key!r} — not a checkout "
                    "under any configured root"
                ),
            )
        try:
            state_dir, _, _ = runs._require_on()
        except ControlPlaneOff as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        store = RunStore(state_dir)
        # Same normalization as `acknowledge_vanished` (Task 7) — the brief
        # omits it here, but the two audited escapes are meant to agree.
        reason_norm = " ".join(request.reason.split())[: RunController._REASON_CAP]
        actor = "local-unauthenticated"  # server-assigned; spec §8.3
        if request.display_name:
            actor += f" (self_reported: {request.display_name[:64]})"
        try:
            return store.release_malformed_lock(key, actor=actor, reason=reason_norm)
        except RunStoreError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        except GuardBusyError as err:
            raise HTTPException(status_code=409, detail=f"guard_busy: {err}") from err

    @app.post("/api/runs/{request_id}/verb", response_model=VerbOutcome)
    def run_verb(
        request_id: str,
        request: VerbRequest,
        x_action_token: str | None = Header(default=None),
    ) -> VerbOutcome:
        """Explicit human click: one allowlisted Mode-1 control verb (spec §6)."""
        _require_token(x_action_token)
        try:
            return runs.control(
                request_id,
                request.verb,
                task_id=request.task_id,
                outcome=request.outcome,
            )
        except RunRejectedError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        except ControlPlaneOff as err:
            raise HTTPException(status_code=409, detail=str(err)) from err

    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
    return app
