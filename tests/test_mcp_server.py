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
