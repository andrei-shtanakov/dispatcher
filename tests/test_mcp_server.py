"""FR-05: the MCP server over the read facade (DESIGN-703..706)."""

from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import make_arbiter, make_atp, make_maestro_home, make_spec_runner
from fastapi.encoders import jsonable_encoder
from fastmcp import Client
from fastmcp.client.client import CallToolResult

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.service import SnapshotService
from dispatcher.core.sync_service import SyncService
from dispatcher.mcp_server import build_server
from dispatcher.server.app import create_app

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

# A minimal roadmap item so the fixture workspace's /api/roadmap is
# non-empty: without it test_roadmap_item_parity_found would always hit
# its skip branch and never exercise the found-id path (silently gutting
# the test). Any evidence-free item is fine — parity only needs a real id.
_ROADMAP_FIXTURE = """
version: 1
roadmap: mcp-parity-fixture
title: Minimal roadmap for MCP/HTTP parity coverage
items:
  - id: RD-MCP-1
    title: Minimal item for parity coverage
    phase: "1"
    evidence_rules: []
"""


def _config(tmp_path: Path) -> DispatcherConfig:
    make_atp(tmp_path)
    make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    db = make_maestro_home(tmp_path)
    vault = tmp_path / "prograph-vault" / "authored" / "roadmaps"
    vault.mkdir(parents=True)
    (vault / "fixture.yaml").write_text(_ROADMAP_FIXTURE)
    # one project.yaml so spec_runner_configs is POPULATED — otherwise its
    # parity row is [] == [] and its serializer-guard entry is vacuous
    steward = tmp_path / "steward"
    steward.mkdir()
    (steward / "project.yaml").write_text(
        "project: steward\nspec_runner:\n  max_retries: 5\nworkstreams: []\n"
    )
    return DispatcherConfig(roots=(tmp_path,), maestro_db=db)


def _tool_json(result: CallToolResult) -> Any:
    """The JSON payload a tool call produced, list- or dict-shaped.

    fastmcp 2.14.7 rebuilds `.data` from the tool's output schema; for a
    `dict[str, Any]` return it equals `structured_content` verbatim, but
    for a `list[...]` return `.data` comes back as opaque wrapper model
    instances while the plain JSON list lives under
    `structured_content["result"]`. Route on the declared shape so both
    tool families compare as plain JSON against the HTTP response.
    """
    if isinstance(result.data, list):
        assert result.structured_content is not None
        return result.structured_content["result"]
    return result.data


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
    server = build_server(config, snapshot_service=cache, sync_service=sync_cache)
    app = create_app(config, snapshot_service=cache, sync_service=sync_cache)
    transport = httpx.ASGITransport(app=app)
    async with (
        Client(server) as mcp_client,
        httpx.AsyncClient(transport=transport, base_url="http://test") as http_client,
    ):
        for tool_name, tool_args, http_path in PARITY:
            tool_result = await mcp_client.call_tool(tool_name, tool_args)
            http_json = (await http_client.get(http_path)).json()
            assert _tool_json(tool_result) == http_json, (tool_name, http_path)


async def test_roadmap_item_parity_found(tmp_path: Path) -> None:
    """Lookup tool parity for an EXISTING id (drawn from the live data)."""
    config = _config(tmp_path)
    cache = SnapshotService(config)
    sync_cache = SyncService(config)
    server = build_server(config, snapshot_service=cache, sync_service=sync_cache)
    app = create_app(config, snapshot_service=cache, sync_service=sync_cache)
    transport = httpx.ASGITransport(app=app)
    async with (
        Client(server) as mcp_client,
        httpx.AsyncClient(transport=transport, base_url="http://test") as http_client,
    ):
        roadmap_json = (await http_client.get("/api/roadmap")).json()
        if not roadmap_json["items"]:
            pytest.skip("fixture workspace has no roadmap items")
        item_id = roadmap_json["items"][0]["id"]
        tool_result = await mcp_client.call_tool("roadmap_item", {"item_id": item_id})
        http_json = (await http_client.get(f"/api/roadmap/{item_id}")).json()
        assert _tool_json(tool_result) == http_json


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
    server = build_server(config, snapshot_service=cache, sync_service=sync_cache)
    async with Client(server) as mcp_client:
        tool_result = await mcp_client.call_tool("sync_status", {})
    assert _tool_json(tool_result)["fetch_in_flight"] is False
    assert fetch_calls == []  # the no-fetch pin: MCP never fetches
    # report payload parity against the same service, no fetch triggered
    # by the comparison either:
    direct = sync_cache.get(start_fetch=False)
    assert _tool_json(tool_result)["report"] == direct.model_dump(mode="json")["report"]


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
