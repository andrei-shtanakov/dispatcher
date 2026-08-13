"""FR-05: the MCP server over the read facade (DESIGN-703..706)."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import (
    make_arbiter,
    make_atp,
    make_impresario,
    make_maestro_home,
    make_spec_runner,
    seed_impresario_wait,
)
from fastapi.encoders import jsonable_encoder
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.exceptions import ToolError

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
    "onboarding",
    "product_proposals",
}

# A minimal roadmap item so the fixture workspace's /api/roadmap is
# non-empty: without it test_roadmap_item_parity_found would always hit
# its skip branch and never exercise the found-id path (silently gutting
# the test). Any evidence-free item is fine — parity only needs a real id.
#
# RD-MCP-DONE/NEXT/BLOCKED exercise the onboarding tool (DESIGN-806): a
# dep resolved via `project_detected` ALONE reaches computed_status
# "implemented" (in DONE_STATUSES) without any work_item_chain/maestro
# plumbing — the simpler shape found while implementing Task 3's API
# test. RD-MCP-NEXT becomes actionable, RD-MCP-BLOCKED stays blocked by
# a ghost id, so the fixture exercises both next_items verdicts.
_ROADMAP_FIXTURE = """
version: 1
roadmap: mcp-parity-fixture
title: Minimal roadmap for MCP/HTTP parity coverage
items:
  - id: RD-MCP-1
    title: Minimal item for parity coverage
    phase: "1"
    evidence_rules: []
  - id: RD-MCP-DONE
    title: Done dep
    phase: "1"
    owner_project: arbiter
    evidence_rules:
      - rule: project_detected
        kind: implementation
        project: arbiter
  - id: RD-MCP-NEXT
    title: Actionable next
    phase: "2"
    owner_project: arbiter
    depends_on: [RD-MCP-DONE]
    evidence_rules:
      - rule: file_exists
        kind: implementation
        project: arbiter
        path: contracts/nope.json
  - id: RD-MCP-BLOCKED
    title: Blocked by ghost
    phase: "2"
    owner_project: arbiter
    depends_on: [RD-MCP-GHOST]
    evidence_rules:
      - rule: file_exists
        kind: implementation
        project: arbiter
        path: contracts/also-nope.json
"""


def _config(tmp_path: Path) -> DispatcherConfig:
    make_atp(tmp_path)
    make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    # impresario mirror with one Gate-A wait, one needs_human loop and one
    # unreadable bundle — the product_proposals parity row must compare a
    # POPULATED report (waits + needs_human + a suppressed bundle), not {}.
    mirror = make_impresario(tmp_path)
    bundle = seed_impresario_wait(mirror)
    (bundle / "loop.state").write_text(
        json.dumps(
            {
                "loop_id": "LOOP-101",
                "idea_ref": "idea://IDEA-101",
                "idea_input_hash": "sha256:" + "f" * 64,
                "proposal_id": "PP-101",
                "exchange_log_id": "XL-101",
                "max_iterations": 3,
                "stop": {
                    "verdict": "needs_human",
                    "reason": "ждём человека",
                    "iteration": 1,
                    "at": "2026-08-12T05:00:00Z",
                },
            }
        )
    )
    broken = mirror / "pilot" / "pp-999"
    broken.mkdir(parents=True)
    (broken / "proposal.yaml").write_bytes(b"\xff\xfe")
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
    ("onboarding", {"project": "arbiter"}, "/api/projects/arbiter/onboarding"),
    (
        "product_proposals",
        {"project": "impresario"},
        "/api/projects/impresario/product-proposals",
    ),
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
            tool_json = _tool_json(tool_result)
            assert tool_json == http_json, (tool_name, http_path)
            if tool_name == "roadmap":
                # fixture precondition: the dep resolves via
                # project_detected ALONE, landing on "implemented" (not
                # "verified") — no work_item_chain/maestro plumbing here.
                statuses = {i["id"]: i["computed_status"] for i in tool_json["items"]}
                assert statuses["RD-MCP-DONE"] == "implemented"
            if tool_name == "onboarding":
                # fixture must exercise both verdicts, not compare empty lists
                flags = {n["actionable"] for n in tool_json["next_items"]}
                assert flags == {True, False}
            if tool_name == "product_proposals":
                # fixture must exercise waits, needs_human AND a suppressed
                # bundle — an empty report would compare vacuously
                assert [w["gate_id"] for w in tool_json["waits"]] == ["qg5_business"]
                assert [w["loop_id"] for w in tool_json["needs_human"]] == ["LOOP-101"]
                assert "unreadable" in {b["state"] for b in tool_json["bundles"]}


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


async def test_numeric_constraints_mirror_http(tmp_path: Path) -> None:
    """HTTP has Query(ge=0/ge=1); the tools carry the same Field bounds —
    a negative limit is a validation error, never a negative slice."""
    async with Client(build_server(_config(tmp_path))) as client:
        with pytest.raises(ToolError):
            await client.call_tool("errors", {"limit": -1})
        with pytest.raises(ToolError):
            await client.call_tool("errors", {"days": 0})
        with pytest.raises(ToolError):
            await client.call_tool("work_items", {"limit": -1})


async def test_lookup_errors_carry_http_detail_text(tmp_path: Path) -> None:
    async with Client(build_server(_config(tmp_path))) as client:
        with pytest.raises(ToolError, match="unknown project: nope"):
            await client.call_tool("project", {"name": "nope"})
        with pytest.raises(ToolError, match="unknown roadmap item: RD-404"):
            await client.call_tool("roadmap_item", {"item_id": "RD-404"})
        with pytest.raises(ToolError, match="unknown project: no-such"):
            await client.call_tool("onboarding", {"project": "no-such"})


async def test_product_proposals_errors_carry_stable_codes(tmp_path: Path) -> None:
    """The CODE is the contract (the human message is not): both 404
    families surface as ToolError carrying the same structured code the
    HTTP route sends in its detail object."""
    async with Client(build_server(_config(tmp_path))) as client:
        with pytest.raises(ToolError, match='"code": "project-not-found"'):
            await client.call_tool("product_proposals", {"project": "nonesuch"})
        with pytest.raises(ToolError, match='"code": "not-impresario-mirror"'):
            await client.call_tool("product_proposals", {"project": "arbiter"})


async def test_product_proposals_mirror_not_detected_is_a_report(
    tmp_path: Path,
) -> None:
    """No impresario under the roots → a SUCCESSFUL report carrying the
    report-level diagnostic and attention=true — never a tool error."""
    make_arbiter(tmp_path)  # some OTHER project, so discovery runs fine
    config = DispatcherConfig(roots=(tmp_path,))
    async with Client(build_server(config)) as client:
        result = await client.call_tool("product_proposals", {"project": "impresario"})
    data = _tool_json(result)
    assert [d["code"] for d in data["diagnostics"]] == ["mirror-not-detected"]
    assert data["attention"] is True


async def test_product_proposals_anchors_missing_is_a_report(
    tmp_path: Path,
) -> None:
    """Mirror degrades within the cache TTL → mirror-anchors-missing as a
    successful attention report, not an MCP error."""
    make_arbiter(tmp_path)
    mirror = make_impresario(tmp_path)
    config = DispatcherConfig(roots=(tmp_path,))
    async with Client(build_server(config)) as client:
        first = await client.call_tool("product_proposals", {"project": "impresario"})
        assert _tool_json(first)["diagnostics"] == []
        (mirror / "docs" / "semantics.md").unlink()
        result = await client.call_tool("product_proposals", {"project": "impresario"})
    data = _tool_json(result)
    assert [d["code"] for d in data["diagnostics"]] == ["mirror-anchors-missing"]
    assert data["attention"] is True


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
        read_api.onboarding(cache, dirs, "arbiter"),
        read_api.product_proposals(cache, "impresario"),
    ]
    for obj in objects:
        assert jsonable_encoder(obj) == obj.model_dump(mode="json"), type(obj)
