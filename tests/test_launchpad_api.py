"""HTTP surface of the launchpad snapshot (Task 3, spec §4.2)."""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from conftest import make_maestro_run

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_store import RunStore
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio

_OWNER = "andrei-shtanakov"


def _config(tmp_path: Path, *, control_plane: bool = True) -> DispatcherConfig:
    """Mirrors `test_run_api.py`'s `_client` helper: the control plane is
    off unless a test explicitly needs it on. Here it defaults ON because
    every scenario except the dedicated off-case needs `assemble_snapshot`
    to actually run."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return DispatcherConfig(
        roots=(ws,),
        maestro_home=tmp_path / "mhome" if control_plane else None,
        run_state_dir=tmp_path / "state" if control_plane else None,
        maestro_cli=tmp_path / "fake-maestro" if control_plane else None,
    )


def _client(config: DispatcherConfig) -> httpx.AsyncClient:
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _key(name: str) -> RepoKey:
    return RepoKey(host="github.com", owner=_OWNER, repo=name)


def _set_mtime(store: RunStore, request_id: str, when: float) -> None:
    path = store._record_path(request_id)  # noqa: SLF001 — test-only, mirrors assembler tests
    os.utime(path, (when, when))


async def test_round_trip_over_an_empty_workspace(tmp_path: Path) -> None:
    config = _config(tmp_path)
    async with _client(config) as client:
        resp = await client.get("/api/launchpad")
        assert resp.status_code == 200
        body = resp.json()
        assert body["repositories"] == []
        assert body["snapshot_id"]
        assert body["next_cursor"] is None


async def test_pagination_via_cursor(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    key = _key("widget")
    for i, req in enumerate(["t1", "t2", "t3"]):
        store.reserve(
            req,
            key,
            known_runs=[],
            window_start="t",
            work_id=f"w{i}",
            revision="a" * 40,
            repository="widget",
        )
        store.mark_terminal(req, outcome="completed")
        _set_mtime(store, req, 1_700_000_000 + i)

    async with _client(config) as client:
        resp = await client.get("/api/launchpad", params={"recent_limit": 2})
        assert resp.status_code == 200
        page1 = resp.json()
        assert [r["request_id"] for r in page1["recent_completed"]] == ["t3", "t2"]
        assert page1["completed_total"] == 3
        assert page1["next_cursor"] is not None

        resp2 = await client.get(
            "/api/launchpad",
            params={"recent_limit": 2, "cursor": page1["next_cursor"]},
        )
        assert resp2.status_code == 200
        page2 = resp2.json()
        assert [r["request_id"] for r in page2["recent_completed"]] == ["t1"]
        assert page2["next_cursor"] is None


async def test_recent_limit_over_100_is_422_invalid_request(tmp_path: Path) -> None:
    config = _config(tmp_path)
    async with _client(config) as client:
        resp = await client.get("/api/launchpad", params={"recent_limit": 101})
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "invalid_request"


async def test_recent_limit_zero_is_422_invalid_request(tmp_path: Path) -> None:
    config = _config(tmp_path)
    async with _client(config) as client:
        resp = await client.get("/api/launchpad", params={"recent_limit": 0})
        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_request"


async def test_recent_limit_negative_is_422_invalid_request(tmp_path: Path) -> None:
    config = _config(tmp_path)
    async with _client(config) as client:
        resp = await client.get("/api/launchpad", params={"recent_limit": -1})
        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_request"


async def test_recent_limit_non_int_is_422_invalid_request(tmp_path: Path) -> None:
    config = _config(tmp_path)
    async with _client(config) as client:
        resp = await client.get("/api/launchpad", params={"recent_limit": "abc"})
        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_request"


async def test_garbage_cursor_is_422_invalid_request(tmp_path: Path) -> None:
    config = _config(tmp_path)
    async with _client(config) as client:
        resp = await client.get(
            "/api/launchpad", params={"cursor": "not*a*valid*cursor"}
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_request"


async def test_control_plane_off_is_409(tmp_path: Path) -> None:
    config = _config(tmp_path, control_plane=False)
    async with _client(config) as client:
        resp = await client.get("/api/launchpad")
        assert resp.status_code == 409
        assert resp.json()["code"] == "control_plane_off"


async def test_active_row_reflects_a_real_run(tmp_path: Path) -> None:
    """One more shape check beyond the empty-workspace round trip: a
    materialized, in-flight run shows up in `active` with maestro's own
    run status joined in."""
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    key = _key("widget")
    store.reserve(
        "req-a",
        key,
        known_runs=[],
        window_start="t",
        work_id="w1",
        revision="a" * 40,
        repository="widget",
    )
    store.mark_launching("req-a")
    store.mark_materialized("req-a", "01AAA")
    assert config.maestro_home is not None
    make_maestro_run(
        config.maestro_home,
        key.as_path_parts(),
        "01AAA",
        started_at="2026-01-01T00:00:00Z",
    )

    async with _client(config) as client:
        resp = await client.get("/api/launchpad")
        assert resp.status_code == 200
        body = resp.json()
        assert [r["request_id"] for r in body["active"]] == ["req-a"]


async def test_structurally_empty_cursor_is_422(tmp_path: Path) -> None:
    """base64 of a lone NUL parses but was never server-issued — it must be
    a 422, not a fake empty page (review-pr minor on #209)."""
    config = _config(tmp_path)
    async with _client(config) as client:
        resp = await client.get("/api/launchpad", params={"cursor": "AA=="})
        assert resp.status_code == 422
        assert resp.json()["code"] == "invalid_request"
