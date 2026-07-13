"""Integration tests for the HTTP API over a fixtures root."""

from pathlib import Path

import httpx
import pytest
from conftest import make_arbiter, make_atp, make_maestro_home, make_spec_runner

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio


def _client(tmp_path: Path) -> httpx.AsyncClient:
    make_atp(tmp_path)
    make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    db = make_maestro_home(tmp_path)
    config = DispatcherConfig(roots=(tmp_path,), maestro_db=db)
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_overview(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/overview")
    assert resp.status_code == 200
    data = resp.json()
    by_name = {p["name"]: p for p in data["projects"]}
    assert by_name["arbiter"]["detected"] is True
    assert by_name["arbiter"]["counts"]["tasks"] == 1
    assert by_name["Maestro"]["detected"] is False  # no project dir in root
    assert by_name["proctor"]["detected"] is False


async def test_project_detail_and_404(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        ok = await client.get("/api/projects/arbiter")
        missing = await client.get("/api/projects/unknown")
    assert ok.status_code == 200
    assert ok.json()["tasks"][0]["task_id"] == "T-9"
    assert missing.status_code == 404


async def test_errors_feed(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/errors", params={"limit": 5})
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) <= 5
    assert any(e["body"] == "subprocess failed" for e in events)


async def test_errors_negative_limit_rejected(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/errors", params={"limit": -1})
    assert resp.status_code == 422


async def test_errors_sorted_newest_first(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        events = (await client.get("/api/errors")).json()
    stamps = [e["timestamp"] or "" for e in events]
    assert stamps == sorted(stamps, reverse=True)


async def test_errors_project_filter(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        all_events = (await client.get("/api/errors")).json()
        arbiter_only = (
            await client.get("/api/errors", params={"project": "arbiter"})
        ).json()
        unknown = (await client.get("/api/errors", params={"project": "nope"})).json()
    assert 0 < len(arbiter_only) < len(all_events)
    # spec-runner fixture errors must not leak into the arbiter view
    assert not any("lint failed" in e["body"] for e in arbiter_only)
    assert unknown == []


async def test_errors_service_filter(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        all_events = (await client.get("/api/errors")).json()
        svc_only = (await client.get("/api/errors", params={"service": "svc"})).json()
        unknown = (await client.get("/api/errors", params={"service": "nope"})).json()
    assert 0 < len(svc_only) < len(all_events)
    assert all(e["service"] == "svc" for e in svc_only)
    assert unknown == []


async def test_errors_days_filter(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        all_events = (await client.get("/api/errors")).json()
        recent = (await client.get("/api/errors", params={"days": 1})).json()
        huge = (await client.get("/api/errors", params={"days": 36500})).json()
        bad = await client.get("/api/errors", params={"days": 0})
    assert len(recent) <= len(all_events)
    assert len(huge) == len(all_events)
    assert bad.status_code == 422


def test_recent_errors_helper() -> None:
    from datetime import UTC, datetime

    from dispatcher.core.models import ErrorEvent
    from dispatcher.server.app import recent_errors

    now = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
    events = [
        ErrorEvent(timestamp="2026-07-02T10:00:00+00:00", body="new", source="s"),
        ErrorEvent(timestamp="2026-02-01T10:00:00", body="old-naive", source="s"),
        ErrorEvent(timestamp=None, body="undated", source="s"),
    ]
    kept = {e.body for e in recent_errors(events, days=14, now=now)}
    assert kept == {"new", "undated"}  # undated events are never dropped


async def test_models_and_contracts(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        models = (await client.get("/api/models")).json()
        contracts = (await client.get("/api/contracts")).json()
    assert any(m["project"] == "arbiter" and m["role"] == "routable" for m in models)
    catalog = next(c for c in contracts if c["name"] == "agents-catalog")
    assert catalog["in_sync"] is False  # fixture vendored copy differs


async def test_index_served(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert 'id="projects"' in resp.text
    assert 'id="errors-toggle"' in resp.text
    # Errors live in a collapsible box, collapsed by default (no `open` attr)
    assert '<details id="errors-box">' in resp.text
    assert 'id="errors-service"' in resp.text
    # Regression guard: cards use data-name + a delegated listener; inline
    # onclick would be XSS-prone (project names reach a JS-string context).
    assert "data-name=" in resp.text
    assert "onclick=" not in resp.text
    # Roadmap table carries Contract + Freshness columns; empty row spans all 8
    assert "<th>Contract</th>" in resp.text
    assert "<th>Freshness</th>" in resp.text
    assert 'colspan="8"' in resp.text
