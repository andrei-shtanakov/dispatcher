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
    """With the control plane off by default, `ControlPlaneOff` would fire
    before `RunRejectedError` ever could — masking exactly the path this
    test means to exercise (the same trap the malformed-id tests below
    already had to route around), so this needs it ON."""
    async with _client(tmp_path, control_plane=True) as client:
        resp = await client.get("/api/runs/nope")
        assert resp.status_code == 404


async def test_control_plane_off_reads_409_not_404(tmp_path: Path) -> None:
    """`ControlPlaneOff` is not "no such request" — it must match the 409
    `/resolve` and `/verb` already report for the same condition."""
    async with _client(tmp_path) as client:
        resp = await client.get("/api/runs/nope")
        assert resp.status_code == 409


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


def test_view_joins_the_request_to_maestros_own_run_row(tmp_path: Path) -> None:
    """dispatcher renders maestro's FSM; it does not restate it (spec §3.2)."""
    from conftest import make_maestro_run

    from dispatcher.core.run_controller import RunController
    from dispatcher.core.run_identity import RepoKey
    from dispatcher.core.run_store import RunStore

    home = tmp_path / "mhome"
    key = RepoKey(host="github.com", owner="owner", repo="deployer")
    make_maestro_run(
        home,
        key.as_path_parts(),
        "01AAA",
        started_at="2026-08-22T00:00:00Z",
        outcome="completed",
    )
    config = DispatcherConfig(
        roots=(tmp_path / "ws",),
        maestro_home=home,
        run_state_dir=tmp_path / "state",
        maestro_cli=tmp_path / "unused-maestro",
    )
    (tmp_path / "ws").mkdir()
    store = RunStore(tmp_path / "state")
    store.reserve("req-1", key, known_runs=[], window_start="t")
    store.mark_materialized("req-1", "01AAA")

    view = RunController(config).view("req-1")
    assert view.record.run_id == "01AAA"
    assert view.run is not None
    assert view.run.status == "completed"


def test_view_resolves_home_from_maestro_db_when_maestro_home_is_unset(
    tmp_path: Path,
) -> None:
    """`maestro_home=None` means "derive from `maestro_db.parent`"
    (`DispatcherConfig.effective_maestro_home`), not "control plane off" —
    the same shape `_require_on()` and `runs_dir()` already honor. `view()`
    must resolve through that fallback too, or a config that only sets
    `maestro_db` would see every run as absent while the dashboard
    collector, which does use the resolved home, finds it fine."""
    from conftest import make_maestro_run

    from dispatcher.core.run_controller import RunController
    from dispatcher.core.run_identity import RepoKey
    from dispatcher.core.run_store import RunStore

    home = tmp_path / "mhome"
    key = RepoKey(host="github.com", owner="owner", repo="deployer")
    make_maestro_run(
        home,
        key.as_path_parts(),
        "01AAA",
        started_at="2026-08-22T00:00:00Z",
        outcome="completed",
    )
    config = DispatcherConfig(
        roots=(tmp_path / "ws",),
        maestro_db=home / "maestro.db",  # maestro_home left unset on purpose
        run_state_dir=tmp_path / "state",
        maestro_cli=tmp_path / "unused-maestro",
    )
    (tmp_path / "ws").mkdir()
    store = RunStore(tmp_path / "state")
    store.reserve("req-1", key, known_runs=[], window_start="t")
    store.mark_materialized("req-1", "01AAA")

    view = RunController(config).view("req-1")
    assert view.run is not None
    assert view.run.status == "completed"


def test_view_surfaces_warnings_for_an_unreadable_run_source(tmp_path: Path) -> None:
    """An UNREADABLE `runs/` and a genuinely ABSENT one both used to surface
    as `run=None` with nothing to tell them apart — `view()` now carries
    `classified_runs`' warnings so they no longer read the same."""
    from dispatcher.core.run_controller import RunController
    from dispatcher.core.run_identity import RepoKey
    from dispatcher.core.run_store import RunStore

    home = tmp_path / "mhome"
    key = RepoKey(host="github.com", owner="owner", repo="deployer")
    runs_dir = home / "projects" / "github.com" / "owner" / "deployer" / "runs"
    runs_dir.mkdir(parents=True)
    runs_dir.chmod(0o000)
    try:
        config = DispatcherConfig(
            roots=(tmp_path / "ws",),
            maestro_home=home,
            run_state_dir=tmp_path / "state",
            maestro_cli=tmp_path / "unused-maestro",
        )
        (tmp_path / "ws").mkdir()
        store = RunStore(tmp_path / "state")
        store.reserve("req-1", key, known_runs=[], window_start="t")
        store.mark_materialized("req-1", "01AAA")

        view = RunController(config).view("req-1")
        assert view.run is None
        assert view.warnings, "an unreadable runs/ must not read the same as absent"
    finally:
        runs_dir.chmod(0o755)
