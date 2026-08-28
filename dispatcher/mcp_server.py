"""FR-05: FastMCP stdio server over the read facade (DESIGN-703).

Read-only by construction: every tool delegates to core.read_api and
returns model_dump(mode="json"); no action tools (NFR-01/X-02 — a tool
call is an agent action, not a human click); sync never fetches
(start_fetch=False). Tool/parameter descriptions are the agent-facing
selection surface — keep them precise when editing.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from dispatcher.core import read_api
from dispatcher.core.benchmark_service import BenchmarkService
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.roadmap import default_roadmap_dirs
from dispatcher.core.service import SnapshotService
from dispatcher.core.sync_service import SyncService

_BENCHMARKS_WAIT_SECONDS = 20.0


def build_server(
    config: DispatcherConfig,
    *,
    snapshot_service: SnapshotService | None = None,
    sync_service: SyncService | None = None,
    benchmark_service: BenchmarkService | None = None,
) -> FastMCP:
    """The dispatcher MCP server; service injection mirrors create_app."""
    # explicit is-None: mirrors create_app's DI contract exactly
    cache = (
        snapshot_service if snapshot_service is not None else SnapshotService(config)
    )
    sync_cache = sync_service if sync_service is not None else SyncService(config)
    # Deliberately built WITHOUT token_file: no MCP tool may spend the
    # stored secret on an agent's initiative (phase-2 spec §2, X-02) — the
    # capability is absent by construction, not merely unused.
    benchmarks_service = (
        benchmark_service
        if benchmark_service is not None
        else (
            BenchmarkService(config.benchmarks_url) if config.benchmarks_url else None
        )
    )
    roadmap_dirs = config.roadmap_dirs or default_roadmap_dirs(config.roots)
    mcp: FastMCP = FastMCP(
        "dispatcher",
        instructions=(
            "Read-only view of the AI-orchestrators ecosystem: project "
            "health, errors, models, contracts, roadmap, machine sync "
            "state and spec-runner configs. No tool here mutates anything."
        ),
    )

    @mcp.tool
    def overview() -> dict[str, Any]:
        """Ecosystem overview: one row per monitored project with
        freshness, task/model/test/error counts and warnings. Start here
        to see what exists and what looks unhealthy."""
        return read_api.overview(cache).model_dump(mode="json")

    @mcp.tool
    def project(
        name: Annotated[
            str,
            Field(description="Collector name, e.g. 'Maestro' or 'arbiter'"),
        ],
    ) -> dict[str, Any]:
        """Full snapshot of ONE project: schema checks, models, tasks,
        orchestration runs (Maestro per-run state, #147 — `runs[].status`
        is fail-closed: no terminal record reads as 'interrupted', never
        as in-progress; empty `runs` is only a confident zero when no
        warning starts with 'run '/'runs '), test results, configs,
        errors, warnings. Errors with 'unknown project: <name>' if the
        name is not monitored."""
        try:
            return read_api.project(cache, name).model_dump(mode="json")
        except read_api.ReadLookupError as err:
            raise ToolError(str(err)) from err

    @mcp.tool
    def errors(
        limit: Annotated[
            int,
            Field(ge=0, description="Max events returned (newest first)"),
        ] = 100,
        days: Annotated[
            int | None,
            Field(
                ge=1,
                description="Only events from the last N days; None = all",
            ),
        ] = None,
        project: Annotated[
            str | None,
            Field(description="Filter to one project's events; None = all"),
        ] = None,
        service: Annotated[
            str | None,
            Field(description="Filter to one service name; None = all"),
        ] = None,
    ) -> list[dict[str, Any]]:
        """Merged error/failure feed across all projects, newest first —
        the same feed the dashboard's Errors panel shows."""
        rows = read_api.errors(
            cache, limit=limit, days=days, project=project, service=service
        )
        return [e.model_dump(mode="json") for e in rows]

    @mcp.tool
    def models() -> list[dict[str, Any]]:
        """Every LLM referenced by any project's configs and catalogs,
        with role/vendor/status — who uses which model where."""
        return [m.model_dump(mode="json") for m in read_api.models(cache)]

    @mcp.tool
    def contracts() -> list[dict[str, Any]]:
        """Cross-repo contract check results, one row per check.

        `kind` says which question a row answers, and the two must not be
        read as one: `vendored_integrity` = our vendored copy matches the
        manifest travelling with it (offline, always available);
        `upstream_drift` = canon differs, or does not, from that copy — an
        observation whose `in_sync=null` means NO canon was available to
        compare, i.e. unknown, not in sync. `listing` is not a comparison."""
        return [c.model_dump(mode="json") for c in read_api.contracts(cache)]

    @mcp.tool
    def epics(
        kind: Annotated[
            str | None,
            Field(
                description=(
                    "Filter epics by their program's kind: 'ecosystem' (the platform "
                    "itself) or 'external' (a third-party product built with it). "
                    "The unclassified bucket is returned regardless."
                )
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Stream axis: programs, epics, per-plane counts, unclassified bucket.

        `kind` is `ecosystem` or `external` and filters EPICS only — the
        unclassified bucket is always present, because an unmarked artifact
        belongs to no program and filtering it away would return a partial
        aggregate that looks complete. Counts are per plane: a plane reported
        `unavailable` was not read, which is not the same as zero.
        """
        from dispatcher.core.epics import build_view
        from dispatcher.core.sync import load_kb_snapshots

        # The HTTP surface constrains `kind` with a pattern; without the same check
        # here an unknown value would filter every epic out and return just the
        # bucket — an empty-looking answer to a malformed question, which reads as
        # "there are no epics" rather than "you asked wrongly".
        if kind is not None and kind not in ("ecosystem", "external"):
            raise ValueError(
                f"unknown kind {kind!r}; expected 'ecosystem' or 'external'"
            )
        load = load_kb_snapshots(config.roots)
        return build_view(
            config, load.snapshots, kind=kind, snapshot_errors=load.errors
        ).model_dump()

    @mcp.tool
    def epic(
        epic_id: Annotated[
            str,
            Field(description="Canonical epic id, '<program>.<epic>' (e.g. 'eco.ops')"),
        ],
    ) -> dict[str, Any]:
        """One epic with every artifact behind it, across the planes that were read."""
        from dispatcher.core.epics import build_detail
        from dispatcher.core.sync import load_kb_snapshots

        load = load_kb_snapshots(config.roots)
        detail = build_detail(config, epic_id, load.snapshots)
        if detail is None:
            raise ValueError(f"unknown epic {epic_id!r}")
        return detail.model_dump()

    @mcp.tool
    def work_items(
        cross_only: Annotated[
            bool,
            Field(description="Only items spanning more than one project"),
        ] = False,
        limit: Annotated[int, Field(ge=0, description="Max items returned")] = 100,
    ) -> dict[str, Any]:
        """Tasks correlated across projects by shared task id — the
        read-side view of Maestro→spec-runner/arbiter handoffs."""
        return read_api.work_items(
            cache, cross_only=cross_only, limit=limit
        ).model_dump(mode="json")

    @mcp.tool
    def roadmap() -> dict[str, Any]:
        """All roadmap items with computed status (planned/implemented/
        verified/unknown/blocked) and their evidence. Prefer
        roadmap_summary for a per-project readiness digest."""
        return read_api.roadmap(cache, roadmap_dirs).model_dump(mode="json")

    @mcp.tool
    def roadmap_item(
        item_id: Annotated[str, Field(description="Roadmap item id, e.g. 'RD-001'")],
    ) -> dict[str, Any]:
        """ONE roadmap item by id, with evidence and blockers. Errors with
        'unknown roadmap item: <id>' if absent."""
        try:
            return read_api.roadmap_item(cache, roadmap_dirs, item_id).model_dump(
                mode="json"
            )
        except read_api.ReadLookupError as err:
            raise ToolError(str(err)) from err

    @mcp.tool
    def roadmap_summary() -> dict[str, Any]:
        """Per-project roadmap digest: done/total, readiness share,
        lagging flag, contract-drift flag. The one-screen answer to
        'how is the ecosystem doing'."""
        return read_api.roadmap_summary(cache, roadmap_dirs).model_dump(mode="json")

    @mcp.tool
    def roadmap_drift() -> dict[str, Any]:
        """Roadmap items joined with LIVE contract sync state — the
        canonical drift join; do not recompute this from roadmap() +
        contracts() yourself."""
        return read_api.roadmap_drift(cache, roadmap_dirs).model_dump(mode="json")

    @mcp.tool
    def roadmap_phases() -> dict[str, Any]:
        """Phase-level rollup: per-phase status counts plus which items
        block each phase from completing."""
        return read_api.roadmap_phases(cache, roadmap_dirs).model_dump(mode="json")

    @mcp.tool
    def roadmap_blockers() -> dict[str, Any]:
        """Item-to-item dependency edges: for each blocking item, the list
        of items it blocks (the reverse of depends_on)."""
        return read_api.roadmap_blockers(cache, roadmap_dirs).model_dump(mode="json")

    @mcp.tool
    def sync_status() -> dict[str, Any]:
        """Machine sync verdicts per host/repo (ok / sync-first / no-data
        / unknown) with snapshot ages and discovery proposals. sync-first
        names a state, not a remedy: work should not start until local
        changes and upstream divergence are reconciled — check the row's
        `reason` for which of behind/ahead/dirty applies. Never triggers
        a network fetch — reports the cached state."""
        return read_api.sync_status(sync_cache, start_fetch=False).model_dump(
            mode="json"
        )

    @mcp.tool
    def benchmarks() -> dict[str, Any]:
        """ATP benchmark list + per-benchmark leaderboards of the ONE
        configured eco server, with fail-closed states: `unconfigured`
        (feature off), `unavailable`/`unreadable` = unknown, NEVER an
        empty-but-ok answer; an empty list is only trustworthy when
        `report.status` is 'ok'. Divergence from sync_status, on purpose:
        this tool MAY start one throttled (60s) read-only GET of the eco
        server's PUBLIC surface and wait briefly for it — the cache is
        in-memory per process, so a never-fetching stdio tool would always
        answer 'not fetched yet'. It never touches the token-gated
        run-status surface (the service here is built without a token by
        construction)."""
        status = read_api.benchmarks(benchmarks_service)
        if benchmarks_service is not None and status.fetch_in_flight:
            benchmarks_service.wait_for_fetch(timeout=_BENCHMARKS_WAIT_SECONDS)
            status = read_api.benchmarks(benchmarks_service, start_fetch=False)
        return status.model_dump(mode="json")

    @mcp.tool
    def spec_runner_configs() -> list[dict[str, Any]]:
        """Every discovered Maestro project.yaml with its spec_runner
        block: typed fields (value + explicit/default provenance) and the
        extra_executor_config overlay. Read-only — editing goes through
        the dispatcher UI's PR flow, never through MCP."""
        return [c.model_dump(mode="json") for c in read_api.spec_runner_configs(config)]

    @mcp.tool
    def onboarding(
        project: Annotated[
            str,
            Field(description="Collector name, e.g. 'Maestro' or 'arbiter'"),
        ],
    ) -> dict[str, Any]:
        """One-screen onboarding join for ONE project: description,
        roadmap position (readiness vs median, own-phase cuts), next_items
        with actionable/blocked_by verdicts, and live pending/in_progress
        tasks. For 'what should I do next in project X' prefer THIS over
        combining project() + roadmap_summary(). Errors with
        'unknown project: <name>' if the name is not monitored."""
        try:
            return read_api.onboarding(cache, roadmap_dirs, project).model_dump(
                mode="json"
            )
        except read_api.ReadLookupError as err:
            raise ToolError(str(err)) from err

    @mcp.tool
    def product_proposals(
        project: Annotated[
            str,
            Field(description="The impresario mirror's collector name"),
        ],
    ) -> dict[str, Any]:
        """Product-governance waits for the impresario mirror: which
        proposals wait at a human gate (gate_waiting), which
        researcher-creator loops wait for a human (needs_human), and
        whether the current RankedBacklog version waits for its QG-4
        selection (backlog_waits), plus every discovered bundle with its
        classification state. A non-ok bundle means classification
        suppressed — unknown, not zero waits.
        Errors carry a JSON detail whose stable `code` is
        'project-not-found' or 'not-impresario-mirror' (the message text
        is not a contract)."""
        try:
            return read_api.product_proposals(cache, project).model_dump(mode="json")
        except read_api.ReadLookupError as err:
            raise ToolError(
                json.dumps({"code": "project-not-found", "message": str(err)})
            ) from err
        except read_api.NotImpresarioMirrorError as err:
            raise ToolError(
                json.dumps({"code": "not-impresario-mirror", "message": str(err)})
            ) from err

    return mcp
