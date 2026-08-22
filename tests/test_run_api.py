"""HTTP surface of the control plane (spec §5.3, §6)."""

from pathlib import Path

import httpx
import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio


def _client(tmp_path: Path, *, control_plane: bool = False) -> httpx.AsyncClient:
    """The control plane is off by default — matching production, where an
    unconfigured `run_state_dir`/`maestro_cli` is the common case. Pass
    `control_plane=True` for cases that must be proven with it ON: the
    off-by-default config makes `_require_on()` raise `ControlPlaneOff`
    before anything else runs, which can hide a bug the store layer would
    otherwise surface (fix round 1, Critical)."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    config = DispatcherConfig(
        roots=(ws,),
        run_state_dir=tmp_path / "state" if control_plane else None,
        maestro_cli=tmp_path / "maestro" if control_plane else None,
    )
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _body() -> dict:
    return {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "work_id": "todo://deployer/entrypoint-token-boundary-match",
        "repository": "deployer",
        "revision": "a" * 40,
        "tasks": "tasks.yaml",
    }


async def test_submit_requires_the_action_token(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.post("/api/runs/submit", json=_body())
        assert resp.status_code == 403


async def test_submit_with_the_control_plane_off_is_a_refusal_not_a_crash(
    tmp_path: Path,
) -> None:
    async with _client(tmp_path) as client:
        token = (await client.get("/api/actions/session")).json()["token"]
        resp = await client.post(
            "/api/runs/submit",
            json=_body(),
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["accepted"] is False
        assert "control plane is off" in payload["reason"]


async def test_unknown_request_reads_404(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/runs/nope")
        assert resp.status_code == 404


async def test_verb_outside_the_allowlist_is_422(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        token = (await client.get("/api/actions/session")).json()["token"]
        resp = await client.post(
            "/api/runs/11111111-1111-4111-8111-111111111111/verb",
            json={"verb": "workstream-continue"},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 422


async def test_stop_is_deliberately_not_a_reachable_verb(tmp_path: Path) -> None:
    """`maestro stop` kills the scheduler process, not one run (spec §6)."""
    async with _client(tmp_path) as client:
        token = (await client.get("/api/actions/session")).json()["token"]
        resp = await client.post(
            "/api/runs/11111111-1111-4111-8111-111111111111/verb",
            json={"verb": "stop"},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 422


# -- fix round 1, Critical: a malformed request_id in the URL must not 500 --
#
# `RunStore._record_path` raises a bare `RunStoreError` for any id outside
# `[A-Za-z0-9_-]`. `RunRequest.request_id` is pydantic-constrained, so
# `/api/runs/submit` never sees this — but a path parameter carries no such
# constraint, so `read_run`, `resolve_run` and `run_verb` all could. These
# three run with the control plane ON: with it off, `ControlPlaneOff` fires
# first and the store is never reached, which is exactly what hid the bug.

_BAD_ID = "bad.id"


async def test_read_with_a_malformed_request_id_is_404_not_500(
    tmp_path: Path,
) -> None:
    async with _client(tmp_path, control_plane=True) as client:
        resp = await client.get(f"/api/runs/{_BAD_ID}")
        assert resp.status_code == 404


async def test_resolve_with_a_malformed_request_id_is_422_not_500(
    tmp_path: Path,
) -> None:
    async with _client(tmp_path, control_plane=True) as client:
        token = (await client.get("/api/actions/session")).json()["token"]
        resp = await client.post(
            f"/api/runs/{_BAD_ID}/resolve",
            json={},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 422


async def test_verb_with_a_malformed_request_id_is_422_not_500(
    tmp_path: Path,
) -> None:
    async with _client(tmp_path, control_plane=True) as client:
        token = (await client.get("/api/actions/session")).json()["token"]
        resp = await client.post(
            f"/api/runs/{_BAD_ID}/verb",
            json={"verb": "status"},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 422
