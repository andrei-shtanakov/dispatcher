"""FR-05: FastMCP stdio server over the read facade (DESIGN-703).

Read-only by construction: every tool delegates to core.read_api and
returns model_dump(mode="json"); no action tools (NFR-01/X-02 — a tool
call is an agent action, not a human click); sync never fetches
(start_fetch=False). Tool/parameter descriptions are the agent-facing
selection surface — keep them precise when editing.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from dispatcher.core import read_api
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.roadmap import default_roadmap_dirs
from dispatcher.core.service import SnapshotService
from dispatcher.core.sync_service import SyncService


def build_server(
    config: DispatcherConfig,
    *,
    snapshot_service: SnapshotService | None = None,
    sync_service: SyncService | None = None,
) -> FastMCP:
    """The dispatcher MCP server; service injection mirrors create_app."""
    cache = snapshot_service or SnapshotService(config)
    sync_cache = sync_service or SyncService(config)
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
            Field(description="Collector name, e.g. 'maestro' or 'arbiter'"),
        ],
    ) -> dict[str, Any]:
        """Full snapshot of ONE project: schema checks, models, tasks,
        test results, configs, errors, warnings. Errors with
        'unknown project: <name>' if the name is not monitored."""
        try:
            return read_api.project(cache, name).model_dump(mode="json")
        except read_api.ReadLookupError as err:
            raise ToolError(str(err)) from err

    @mcp.tool
    def errors(
        limit: Annotated[
            int, Field(description="Max events returned (newest first)")
        ] = 100,
        days: Annotated[
            int | None,
            Field(description="Only events from the last N days; None = all"),
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
        """Cross-repo contract sync state: canonical file vs each vendored
        copy, in_sync true/false/null (null = cannot compare)."""
        return [c.model_dump(mode="json") for c in read_api.contracts(cache)]

    @mcp.tool
    def work_items(
        cross_only: Annotated[
            bool,
            Field(description="Only items spanning more than one project"),
        ] = False,
        limit: Annotated[int, Field(description="Max items returned")] = 100,
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
        """Per-phase status counts and which items block each phase."""
        return read_api.roadmap_phases(cache, roadmap_dirs).model_dump(mode="json")

    @mcp.tool
    def roadmap_blockers() -> dict[str, Any]:
        """Reverse dependency view: which items block which others."""
        return read_api.roadmap_blockers(cache, roadmap_dirs).model_dump(mode="json")

    @mcp.tool
    def sync_status() -> dict[str, Any]:
        """Machine sync verdicts per host/repo (ok / pull-first / no-data
        / unknown) with snapshot ages and discovery proposals. Never
        triggers a network fetch — reports the cached state."""
        return read_api.sync_status(sync_cache, start_fetch=False).model_dump(
            mode="json"
        )

    @mcp.tool
    def spec_runner_configs() -> list[dict[str, Any]]:
        """Every discovered Maestro project.yaml with its spec_runner
        block: typed fields (value + explicit/default provenance) and the
        extra_executor_config overlay. Read-only — editing goes through
        the dispatcher UI's PR flow, never through MCP."""
        return [c.model_dump(mode="json") for c in read_api.spec_runner_configs(config)]

    return mcp
