# FR-05 MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `dispatcher mcp --config` — a FastMCP stdio server exposing the 14 read tools over the same implementations the HTTP API uses, read-only with teeth (exact whitelist test, `start_fetch=False`), JSON-parity-tested against the HTTP surface.

**Architecture:** Two stacked PRs per spec §7. **PR A (prep):** `core/read_api.py` facade extracted from the route bodies + `create_app` service injection + `ModelUsageRow` — behavior frozen by the existing suite. **PR B (MCP):** `dispatcher/mcp_server.py` (FastMCP + 14 tools), CLI wiring, the full DESIGN-706 test matrix, docs.

**Tech Stack:** Python 3.12, FastMCP (`fastmcp>=2.14.5,<3` — new dependency), existing pydantic models, pytest+anyio with the in-memory `fastmcp.Client`.

**Spec:** `docs/superpowers/specs/2026-07-18-mcp-server-design.md` (DESIGN-701..707) — read it first.

## Global Constraints

- Line length 88 (ruff), type hints, `uv run pyrefly check` + `uv run ruff format --check .` clean before every commit; full suite green (baseline 256 passed + 1 skipped).
- PR A must be behavior-frozen: NO test assertion changes except the `models()` typing additions — the existing suite is the refactor guard.
- The MCP surface NEVER fetches: every `SyncService.get` call in MCP code paths passes `start_fetch=False`, pinned by a spy test.
- Lookup errors: `ToolError` with the byte-exact HTTP detail texts (`unknown project: {name}`, `unknown roadmap item: {item_id}`).
- Every tool has a non-empty description; every tool parameter carries a `Field(description=...)` — enforced by test, not convention.
- Branches: Task 1 starts on `feat/read-facade-prep` off master; Task 3 starts on `feat/mcp-server` off the prep branch (stacked — do NOT wait for the prep merge). The controller opens both PRs.

---

## File Structure

- Create: `dispatcher/core/read_api.py` — the facade + `ReadLookupError` (PR A).
- Modify: `dispatcher/server/app.py` — read routes become delegations; `create_app` gains service injection (PR A).
- Modify: `dispatcher/core/models.py` — `ModelUsageRow` (PR A).
- Modify: `tests/test_api.py` — only the models-endpoint assertions extend (PR A).
- Create: `dispatcher/mcp_server.py` — `build_server(...)` + 14 tools (PR B).
- Modify: `dispatcher/cli.py` — `mcp` subcommand (PR B).
- Modify: `pyproject.toml` — fastmcp dependency (PR B).
- Create: `tests/test_mcp_server.py` (PR B).
- Modify (docs): `README.md`, `COWORK_CONTEXT.md`, `spec/discovery-brief-customer.md` (PR B).

---

### Task 1: Read facade + `create_app` service injection (DESIGN-701, PR A)

**Files:**
- Create: `dispatcher/core/read_api.py`
- Modify: `dispatcher/server/app.py`

**Interfaces:**
- Produces (used by Tasks 2-4): module `dispatcher.core.read_api` with `class ReadLookupError(Exception)` (message == the HTTP detail text) and functions:
  - `overview(cache: SnapshotService) -> OverviewResponse`
  - `project(cache: SnapshotService, name: str) -> ProjectSnapshot` — raises `ReadLookupError`
  - `errors(cache, *, limit: int = 100, days: int | None = None, project: str | None = None, service: str | None = None) -> list[ErrorEvent]`
  - `models(cache) -> list[dict[str, Any]]` (typed properly in Task 2)
  - `contracts(cache) -> list[ContractStatus]`
  - `work_items(cache, *, cross_only: bool = False, limit: int = 100) -> WorkItemsResponse`
  - `roadmap(cache, roadmap_dirs) -> RoadmapResponse`
  - `roadmap_drift(cache, roadmap_dirs) -> DriftResponse`
  - `roadmap_phases(cache, roadmap_dirs) -> PhasesResponse`
  - `roadmap_blockers(cache, roadmap_dirs) -> BlockersResponse`
  - `roadmap_summary(cache, roadmap_dirs) -> SummaryResponse`
  - `roadmap_item(cache, roadmap_dirs, item_id: str) -> RoadmapItemView` — raises `ReadLookupError`
  - `sync_status(sync_cache: SyncService, *, start_fetch: bool = True) -> SyncStatus`
  - `spec_runner_configs(config: DispatcherConfig) -> list[ProjectSpecRunnerConfig]`
- `create_app(config, *, snapshot_service: SnapshotService | None = None, sync_service: SyncService | None = None)` — keyword-only injection, defaults construct from config as today (the `DispatcherApp` DI precedent).

- [ ] **Step 1: Write `dispatcher/core/read_api.py`**

The bodies are MOVES of the current route bodies (`dispatcher/server/app.py:123-247, 309-321` — read them side by side while writing; behavior must be identical):

```python
"""Read facade: one implementation for the HTTP API and the MCP tools.

DESIGN-701: endpoint bodies live here; `server/app.py` routes and
`dispatcher/mcp_server.py` tools are thin delegations, so the two
surfaces cannot drift. Lookup misses raise ReadLookupError whose message
IS the HTTP detail text (the API maps it to 404, MCP to ToolError).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


def models(cache: SnapshotService) -> list[dict[str, Any]]:
    """Every model referenced by any project's configs/catalogs."""
    snapshots, _ = cache.get()
    return [
        {"project": s.name, **m.model_dump()} for s in snapshots for m in s.models
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


def roadmap(
    cache: SnapshotService, roadmap_dirs: tuple[Path, ...]
) -> RoadmapResponse:
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


def sync_status(
    sync_cache: SyncService, *, start_fetch: bool = True
) -> SyncStatus:
    """Sync verdicts + freshness; start_fetch=False for agent surfaces."""
    return sync_cache.get(start_fetch=start_fetch)


def spec_runner_configs(
    config: DispatcherConfig,
) -> list[ProjectSpecRunnerConfig]:
    """Every discovered project.yaml across all roots (basename-keyed)."""
    configs, _ = discover_project_configs(config.roots)
    return configs
```

- [ ] **Step 2: Delegate in `server/app.py`**

`create_app` signature becomes:

```python
def create_app(
    config: DispatcherConfig,
    *,
    snapshot_service: SnapshotService | None = None,
    sync_service: SyncService | None = None,
) -> FastAPI:
    """Build the API app for the given configuration."""
    app = FastAPI(title="Dispatcher", version="0.1.0")
    cache = snapshot_service or SnapshotService(config)
    sync_cache = sync_service or SyncService(config)
```

Each read route body becomes a delegation, e.g.:

```python
    @app.get("/api/overview", response_model=OverviewResponse)
    def overview() -> OverviewResponse:
        return read_api.overview(cache)

    @app.get("/api/projects/{name}", response_model=ProjectSnapshot)
    def project_detail(name: str) -> ProjectSnapshot:
        try:
            return read_api.project(cache, name)
        except read_api.ReadLookupError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
```

Apply the same mechanical pattern to ALL read routes listed in Task 1's interface (errors passes its Query params through as keywords; the roadmap routes pass `roadmap_dirs`; `roadmap_item` maps `ReadLookupError` → 404; `sync()` returns `read_api.sync_status(sync_cache)`; `spec_runner_configs_list` delegates with its docstring kept on the ROUTE). Add `from dispatcher.core import read_api` to the imports and REMOVE the now-unused direct imports from app.py (`check_contracts`, `build_work_items`, `build_*` roadmap builders, `recent_errors`, `discover_project_configs` — verify each with ruff's unused-import check; `default_roadmap_dirs` STAYS, `roadmap_dirs` is still computed in `create_app`). The write routes (`sync_track`, actions) and `sync_hosts` (a projection built from `read_api.sync_status(sync_cache)`) are untouched in behavior.

- [ ] **Step 3: Full suite — the refactor guard**

Run: `uv run pytest -q && uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: EXACTLY 256 passed + 1 skipped — any assertion change means the move changed behavior; stop and fix rather than adapting tests.

- [ ] **Step 4: Commit**

```bash
git add dispatcher/core/read_api.py dispatcher/server/app.py
git commit -m "refactor: extract the read facade — one implementation for HTTP and MCP (DESIGN-701)"
```

---

### Task 2: `ModelUsageRow` (DESIGN-702, PR A)

**Files:**
- Modify: `dispatcher/core/models.py`, `dispatcher/core/read_api.py`, `dispatcher/server/app.py`
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing test** (extend the existing models-endpoint test in `tests/test_api.py` — find it first; add assertions, do not weaken):

```python
    # DESIGN-702: the endpoint now carries a response model; the JSON
    # shape is unchanged (same keys as the old ad-hoc dict)
    row = resp.json()[0]
    assert set(row) == {
        "project", "model_id", "vendor", "harness", "role", "status", "source",
    }
```

(Adapt to the actual test's variable names; if no models test exists, add a minimal one following the file's sibling pattern.)

- [ ] **Step 2: Implement**

`dispatcher/core/models.py`, after `ModelInUse`:

```python
class ModelUsageRow(ModelInUse):
    """`ModelInUse` + its owning project — the /api/models row shape."""

    project: str
```

`read_api.models` becomes typed:

```python
def models(cache: SnapshotService) -> list[ModelUsageRow]:
    """Every model referenced by any project's configs/catalogs."""
    snapshots, _ = cache.get()
    return [
        ModelUsageRow(project=s.name, **m.model_dump())
        for s in snapshots
        for m in s.models
    ]
```

`app.py`: `@app.get("/api/models", response_model=list[ModelUsageRow])` delegating to the facade; import `ModelUsageRow`.

NOTE: field ORDER in JSON changes (`project` last vs first) — key SETS are what the tests assert; if any existing test asserts order, that is a real question to raise, not silently adapt.

- [ ] **Step 3: Full suite, format, lint, type-check; commit; push; PR A**

```bash
uv run pytest -q && uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/models.py dispatcher/core/read_api.py dispatcher/server/app.py tests/test_api.py
git commit -m "feat: ModelUsageRow — /api/models gains its response model (DESIGN-702)"
git push -u origin feat/read-facade-prep
```

(The controller opens PR A; execution continues immediately on the stacked branch.)

---

### Task 3: MCP server + CLI + whitelist/description tests (DESIGN-703/705, PR B)

**Files:**
- Create: `dispatcher/mcp_server.py`
- Modify: `dispatcher/cli.py`, `pyproject.toml`
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Produces: `build_server(config: DispatcherConfig, *, snapshot_service: SnapshotService | None = None, sync_service: SyncService | None = None) -> FastMCP` (injection mirrors `create_app` — the parity tests share instances across both).

- [ ] **Step 1: Add the dependency**

Run: `uv add "fastmcp>=2.14.5,<3"`

- [ ] **Step 2: Write the failing tests**

`tests/test_mcp_server.py` (async, `pytestmark = pytest.mark.anyio`; reuse the conftest workspace builders):

```python
"""FR-05: the MCP server over the read facade (DESIGN-703..706)."""

from pathlib import Path

import pytest
from conftest import make_arbiter, make_atp, make_maestro_home, make_spec_runner
from fastmcp import Client

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.mcp_server import build_server

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = {
    "overview",
    "project",
    "errors",
    "models",
    "contracts",
    "work_items",
    "roadmap",
    "roadmap_item",
    "roadmap_summary",
    "roadmap_drift",
    "roadmap_phases",
    "roadmap_blockers",
    "sync_status",
    "spec_runner_configs",
}


def _config(tmp_path: Path) -> DispatcherConfig:
    make_atp(tmp_path)
    make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    db = make_maestro_home(tmp_path)
    return DispatcherConfig(roots=(tmp_path,), maestro_db=db)


async def test_tool_set_is_exactly_the_whitelist(tmp_path: Path) -> None:
    """Read-only with teeth: equality BOTH ways — a future action tool
    cannot leak in, a dropped read tool cannot vanish silently."""
    async with Client(build_server(_config(tmp_path))) as client:
        tools = await client.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS


async def test_every_tool_and_param_described(tmp_path: Path) -> None:
    """DESIGN-703 enforced: descriptions are the agent-facing contract."""
    async with Client(build_server(_config(tmp_path))) as client:
        tools = await client.list_tools()
    for tool in tools:
        assert tool.description, f"{tool.name}: empty description"
        props = (tool.inputSchema or {}).get("properties", {})
        for pname, schema in props.items():
            assert schema.get("description"), (
                f"{tool.name}.{pname}: parameter without a description"
            )
```

(Read-first note: check how `conftest` exposes the builders and how fastmcp's `Client` returns tool metadata — `inputSchema` attribute naming may be `input_schema` depending on the fastmcp version; adapt the accessor, keep the assertions' meaning.)

- [ ] **Step 3: Run to verify failure** (`ModuleNotFoundError: dispatcher.mcp_server`), then implement `dispatcher/mcp_server.py`:

```python
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
        item_id: Annotated[
            str, Field(description="Roadmap item id, e.g. 'RD-001'")
        ],
    ) -> dict[str, Any]:
        """ONE roadmap item by id, with evidence and blockers. Errors with
        'unknown roadmap item: <id>' if absent."""
        try:
            return read_api.roadmap_item(
                cache, roadmap_dirs, item_id
            ).model_dump(mode="json")
        except read_api.ReadLookupError as err:
            raise ToolError(str(err)) from err

    @mcp.tool
    def roadmap_summary() -> dict[str, Any]:
        """Per-project roadmap digest: done/total, readiness share,
        lagging flag, contract-drift flag. The one-screen answer to
        'how is the ecosystem doing'."""
        return read_api.roadmap_summary(cache, roadmap_dirs).model_dump(
            mode="json"
        )

    @mcp.tool
    def roadmap_drift() -> dict[str, Any]:
        """Roadmap items joined with LIVE contract sync state — the
        canonical drift join; do not recompute this from roadmap() +
        contracts() yourself."""
        return read_api.roadmap_drift(cache, roadmap_dirs).model_dump(
            mode="json"
        )

    @mcp.tool
    def roadmap_phases() -> dict[str, Any]:
        """Per-phase status counts and which items block each phase."""
        return read_api.roadmap_phases(cache, roadmap_dirs).model_dump(
            mode="json"
        )

    @mcp.tool
    def roadmap_blockers() -> dict[str, Any]:
        """Reverse dependency view: which items block which others."""
        return read_api.roadmap_blockers(cache, roadmap_dirs).model_dump(
            mode="json"
        )

    @mcp.tool
    def sync_status() -> dict[str, Any]:
        """Machine sync verdicts per host/repo (ok / pull-first / no-data
        / unknown) with snapshot ages and discovery proposals. Never
        triggers a network fetch — reports the cached state."""
        return read_api.sync_status(
            sync_cache, start_fetch=False
        ).model_dump(mode="json")

    @mcp.tool
    def spec_runner_configs() -> list[dict[str, Any]]:
        """Every discovered Maestro project.yaml with its spec_runner
        block: typed fields (value + explicit/default provenance) and the
        extra_executor_config overlay. Read-only — editing goes through
        the dispatcher UI's PR flow, never through MCP."""
        return [
            c.model_dump(mode="json")
            for c in read_api.spec_runner_configs(config)
        ]

    return mcp
```

- [ ] **Step 4: CLI wiring** (`dispatcher/cli.py`): in `build_parser`, after `tui`:

```python
    mcp = sub.add_parser(
        "mcp",
        help="run the MCP stdio server over the read API (for agents)",
    )
    mcp.add_argument("--config", type=Path, default=None)
```

In `main()`, before the `tui` branch:

```python
    if args.command == "mcp":
        # Imported lazily: serve/tui should not pay fastmcp's import cost.
        from dispatcher.mcp_server import build_server

        build_server(config).run()
        return
```

- [ ] **Step 5: Run the two tests (green), full suite, format/lint/pyrefly; commit**

```bash
uv run pytest tests/test_mcp_server.py -v && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add dispatcher/mcp_server.py dispatcher/cli.py pyproject.toml uv.lock tests/test_mcp_server.py
git commit -m "feat: dispatcher mcp — FastMCP stdio server over the read facade (DESIGN-703/705)"
```

---

### Task 4: Parity, lookup-error, no-fetch and serializer-guard tests (DESIGN-704/706, PR B)

**Files:**
- Test: `tests/test_mcp_server.py`
- Modify: `dispatcher/mcp_server.py` / `dispatcher/core/read_api.py` ONLY if a test exposes a real gap (report it — do not weaken tests).

- [ ] **Step 1: Parity tests with shared services**

Append (read fastmcp's `Client.call_tool` result API first — the parsed JSON payload accessor is `result.data` in fastmcp v2 for object results and `result.structured_content` in some versions; also `httpx`/`ASGITransport` usage per `tests/test_api.py` conventions — mirror them):

```python
import httpx
from fastapi.encoders import jsonable_encoder

from dispatcher.core.service import SnapshotService
from dispatcher.core.sync_service import SyncService
from dispatcher.server.app import create_app

PARITY: list[tuple[str, dict, str]] = [
    ("overview", {}, "/api/overview"),
    ("project", {"name": "arbiter"}, "/api/projects/arbiter"),
    ("errors", {}, "/api/errors?limit=100"),
    (
        "errors",
        {"limit": 5, "days": 14},
        "/api/errors?limit=5&days=14",
    ),
    ("models", {}, "/api/models"),
    ("contracts", {}, "/api/contracts"),
    ("work_items", {}, "/api/work-items"),
    ("work_items", {"cross_only": True}, "/api/work-items?cross_only=true"),
    ("roadmap", {}, "/api/roadmap"),
    ("roadmap_summary", {}, "/api/roadmap/summary"),
    ("roadmap_drift", {}, "/api/roadmap/drift"),
    ("roadmap_phases", {}, "/api/roadmap/phases"),
    ("roadmap_blockers", {}, "/api/roadmap/blockers"),
    ("spec_runner_configs", {}, "/api/spec-runner-configs"),
]


async def test_tool_json_equals_http_json(tmp_path: Path) -> None:
    """DESIGN-706 parity: same services, same JSON — both surfaces."""
    config = _config(tmp_path)
    cache = SnapshotService(config)
    sync_cache = SyncService(config)
    server = build_server(
        config, snapshot_service=cache, sync_service=sync_cache
    )
    app = create_app(config, snapshot_service=cache, sync_service=sync_cache)
    transport = httpx.ASGITransport(app=app)
    async with (
        Client(server) as mcp_client,
        httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as http_client,
    ):
        for tool_name, tool_args, http_path in PARITY:
            tool_result = await mcp_client.call_tool(tool_name, tool_args)
            http_json = (await http_client.get(http_path)).json()
            assert tool_result.data == http_json, (tool_name, http_path)


async def test_roadmap_item_parity_found(tmp_path: Path) -> None:
    """Lookup tool parity for an EXISTING id (drawn from the live data)."""
    config = _config(tmp_path)
    cache = SnapshotService(config)
    sync_cache = SyncService(config)
    server = build_server(
        config, snapshot_service=cache, sync_service=sync_cache
    )
    app = create_app(config, snapshot_service=cache, sync_service=sync_cache)
    transport = httpx.ASGITransport(app=app)
    async with (
        Client(server) as mcp_client,
        httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as http_client,
    ):
        roadmap_json = (await http_client.get("/api/roadmap")).json()
        if not roadmap_json["items"]:
            pytest.skip("fixture workspace has no roadmap items")
        item_id = roadmap_json["items"][0]["id"]
        tool_result = await mcp_client.call_tool(
            "roadmap_item", {"item_id": item_id}
        )
        http_json = (
            await http_client.get(f"/api/roadmap/{item_id}")
        ).json()
        assert tool_result.data == http_json


async def test_sync_status_parity_report_payload(tmp_path: Path) -> None:
    """§2 sync_status row: report payload equal on shared services; the
    fetch-lifecycle fields are the DESIGNED divergence; tool never
    fetches."""
    config = _config(tmp_path)
    cache = SnapshotService(config)
    fetch_calls: list[Path] = []

    def spy_fetcher(workspace: Path) -> list[str]:
        fetch_calls.append(workspace)
        return []

    sync_cache = SyncService(config, fetcher=spy_fetcher)
    server = build_server(
        config, snapshot_service=cache, sync_service=sync_cache
    )
    async with Client(server) as mcp_client:
        tool_result = await mcp_client.call_tool("sync_status", {})
    assert tool_result.data["fetch_in_flight"] is False
    assert fetch_calls == []  # the no-fetch pin: MCP never fetches
    # report payload parity against the same service, no fetch triggered
    # by the comparison either:
    direct = sync_cache.get(start_fetch=False)
    assert tool_result.data["report"] == direct.model_dump(mode="json")["report"]
```

(Read-first notes: `SyncService.__init__`'s injectable fetcher/collector parameter NAMES — `sync_service.py:81` — adapt `fetcher=` if the keyword differs. If the fixture workspace yields no sync report without github-checker, the collector may raise/degrade — read how existing sync tests construct SyncService and mirror; injecting a stub collector is acceptable for the payload-parity half, but the NO-FETCH assertions must stay against the real `get()` path.)

- [ ] **Step 2: Lookup-error and serializer-guard tests**

```python
async def test_lookup_errors_carry_http_detail_text(tmp_path: Path) -> None:
    from fastmcp.exceptions import ToolError

    async with Client(build_server(_config(tmp_path))) as client:
        with pytest.raises(ToolError, match="unknown project: nope"):
            await client.call_tool("project", {"name": "nope"})
        with pytest.raises(ToolError, match="unknown roadmap item: RD-404"):
            await client.call_tool("roadmap_item", {"item_id": "RD-404"})


async def test_serializers_agree_for_every_read_model(tmp_path: Path) -> None:
    """review 2's guard: jsonable_encoder == model_dump(mode='json') on
    POPULATED instances — datetimes are the sensitive spot."""
    config = _config(tmp_path)
    cache = SnapshotService(config)
    sync_cache = SyncService(config)
    from dispatcher.core import read_api
    from dispatcher.core.roadmap import default_roadmap_dirs

    dirs = config.roadmap_dirs or default_roadmap_dirs(config.roots)
    objects = [
        read_api.overview(cache),
        *read_api.errors(cache),
        *read_api.models(cache),
        *read_api.contracts(cache),
        read_api.work_items(cache),
        read_api.roadmap(cache, dirs),
        read_api.roadmap_summary(cache, dirs),
        read_api.roadmap_drift(cache, dirs),
        read_api.roadmap_phases(cache, dirs),
        read_api.roadmap_blockers(cache, dirs),
        read_api.sync_status(sync_cache, start_fetch=False),
        *read_api.spec_runner_configs(config),
    ]
    for obj in objects:
        assert jsonable_encoder(obj) == obj.model_dump(mode="json"), type(obj)
```

- [ ] **Step 3: Run everything, format/lint/pyrefly; commit**

```bash
uv run pytest tests/test_mcp_server.py -v && uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add tests/test_mcp_server.py dispatcher/mcp_server.py dispatcher/core/read_api.py
git commit -m "test: MCP parity, lookup errors, no-fetch pin, serializer guard (DESIGN-704/706)"
```

---

### Task 5: Documentation (DESIGN-707, PR B)

**Files:**
- Modify: `README.md`, `COWORK_CONTEXT.md`, `spec/discovery-brief-customer.md`

- [ ] **Step 1: README** — new "## MCP server" section after the API section: what it is (read-only tools over the same read API), the registration one-liner (`claude mcp add dispatcher -- uv run --project /path/to/dispatcher dispatcher mcp --config /path/dispatcher.toml`), the 14-tool list in one terse line, the read-only statement (no action tools by design; sync never fetches).

- [ ] **Step 2: COWORK_CONTEXT** — Стек section gains an «**MCP**: fastmcp stdio (`dispatcher mcp`), 14 read-тулзов поверх core/read_api — те же модели, что HTTP» line; API section mentions the MCP surface mirrors it.

- [ ] **Step 3: FR-05 resolution** — `spec/discovery-brief-customer.md`'s FR-05 entry gains the resolution line (same style as FR-06's): closed 2026-07-18, spec `2026-07-18-mcp-server-design.md`.

- [ ] **Step 4: Verify, commit, push**

```bash
uv run pytest -q && uv run ruff format --check .
git add README.md COWORK_CONTEXT.md spec/discovery-brief-customer.md
git commit -m "docs: record the MCP server — FR-05 closed (DESIGN-707)"
git push -u origin feat/mcp-server
```

(The controller opens PR B on top of PR A's branch.)

---

## Self-Review Notes

- **Spec coverage:** DESIGN-701 → Task 1 (facade code in full, delegation pattern with the two 404 mappings, service injection); DESIGN-702 → Task 2 (subclass ordering note: JSON key ORDER changes — flagged as a raise-don't-adapt point); DESIGN-703 → Task 3 (14 tools, every one with an agent-facing docstring, every param `Field(description=...)`, `fastmcp>=2.14.5,<3`); DESIGN-704 → Tasks 3 (ToolError raises) + 4 (exact-text tests); DESIGN-705 → Task 3 (subparser + lazy import, `serve`/`tui` cost note kept); DESIGN-706 → Tasks 3-4 (whitelist equality, description completeness, JSON parity on shared services incl. non-default `errors`/`work_items(cross_only=True)`/`roadmap_item` found+not-found, sync report-payload parity + `fetch_in_flight is False` + spy-fetcher `fetch_calls == []`, serializer guard on populated instances); DESIGN-707 → Task 5. §4 error rows: lookup (T4), collector degradation (inherent — warnings ride inside payloads), config errors (CLI reuses `load_config` before the loop).
- **Placeholder scan:** clean. Three read-first notes are explicit instructions (conftest builder names, fastmcp result/metadata accessors, SyncService injectable kwarg names) — version-sensitive surfaces where blind verbatim code would be the riskier choice.
- **Type consistency:** facade signatures in Task 1's interface block match Task 3's call sites and Task 4's direct calls; `build_server` injection kwargs mirror `create_app`'s exactly; `EXPECTED_TOOLS` matches the spec §2 table's 14 names.
- **Known judgment calls:** `sync_hosts` projection stays in app.py (HTTP-only UI shape — spec §2); route docstrings stay on routes (user-facing OpenAPI) while facade carries its own; `models()` typed in Task 2 rather than Task 1 so PR A's first commit is a pure move and the suite diff proves it.
