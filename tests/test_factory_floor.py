"""Factory floor C1 (spec 2026-09-30-factory-floor-c1-design)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from conftest import make_maestro_run, write_holder

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.factory_floor import build_factory_floor
from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_store import RunStore
from dispatcher.server.app import create_app

_ACME = ("github.com", "acme", "app")
_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
_REQ = "11111111-1111-4111-8111-111111111111"


def _config(tmp_path: Path, *, control_plane: bool = True) -> DispatcherConfig:
    return DispatcherConfig(
        roots=(tmp_path / "ws",),
        maestro_home=tmp_path / "mhome",
        run_state_dir=tmp_path / "state" if control_plane else None,
        maestro_cli=tmp_path / "bin" / "maestro",
    )


def test_only_unfinished_runs_stale_first_with_a_prepared_run_end(
    tmp_path: Path,
) -> None:
    home = tmp_path / "mhome"
    make_maestro_run(home, _ACME, "01OLD", started_at="2026-08-24T07:29:18+00:00")
    make_maestro_run(home, _ACME, "01NEW", started_at="2026-09-30T11:00:00+00:00")
    make_maestro_run(
        home,
        _ACME,
        "01DONE",
        started_at="2026-08-01T00:00:00+00:00",
        outcome="completed",
        ended_at="2026-08-01T01:00:00+00:00",
    )
    view = build_factory_floor(_config(tmp_path), now=_NOW)
    assert view.complete is True
    assert [r.run_id for r in view.in_flight] == ["01OLD", "01NEW"]
    old, new = view.in_flight
    assert (old.status, old.stale) == ("interrupted", True)
    assert old.act is not None
    assert old.act.model_dump() == {
        "kind": "maestro_run_end",
        "run_id": "01OLD",
        "repo_key": "github.com/acme/app",
        "maestro_home": str(home),
        "maestro_cli": str(tmp_path / "bin" / "maestro"),
    }
    assert (new.stale, new.act) == (False, None)  # interrupted, but only 1h old


def test_a_live_run_is_never_stale(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    make_maestro_run(home, _ACME, "01LIVE", started_at="2026-08-01T00:00:00+00:00")
    import os

    write_holder(home, _ACME, "01LIVE", os.getpid())
    [run] = build_factory_floor(_config(tmp_path), now=_NOW).in_flight
    assert (run.status, run.stale, run.act) == ("running", False, None)


def test_an_unknown_start_is_never_called_stale(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    make_maestro_run(home, _ACME, "01NAIVE", started_at="2026-08-01T00:00:00")
    [run] = build_factory_floor(_config(tmp_path), now=_NOW).in_flight
    assert (run.started_at, run.stale) == (None, False)


def test_the_dispatcher_record_is_joined(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    make_maestro_run(
        tmp_path / "mhome", _ACME, "01RUN", started_at="2026-09-30T11:00:00+00:00"
    )
    store = RunStore(config.run_state_dir)
    store.reserve(
        _REQ,
        RepoKey(host="github.com", owner="acme", repo="app"),
        known_runs=[],
        window_start="t",
        work_id="todo://app/x",
    )
    store.mark_materialized(_REQ, "01RUN")
    [run] = build_factory_floor(config, now=_NOW).in_flight
    assert (run.request_id, run.work_id) == (_REQ, "todo://app/x")


def test_an_unreadable_run_makes_maestro_partial(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    make_maestro_run(home, _ACME, "01GOOD", started_at="2026-09-30T11:00:00+00:00")
    home.joinpath("projects", *_ACME, "runs", "01BARE").mkdir()
    view = build_factory_floor(_config(tmp_path), now=_NOW)
    assert view.sources["maestro"].state == "partial"
    assert "01BARE" in (view.sources["maestro"].detail or "")
    assert view.complete is False
    assert [r.run_id for r in view.in_flight] == ["01GOOD"]


def test_control_plane_off_is_not_configured(tmp_path: Path) -> None:
    view = build_factory_floor(_config(tmp_path, control_plane=False), now=_NOW)
    assert view.sources["dispatcher_runs"].state == "not_configured"
    assert view.complete is True


@pytest.mark.anyio
async def test_the_endpoint_answers_200(tmp_path: Path) -> None:
    (tmp_path / "ws").mkdir()
    transport = httpx.ASGITransport(app=create_app(_config(tmp_path)))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/factory-floor")
    assert resp.status_code == 200
    assert resp.json()["sources"]["maestro"]["state"] == "ok"
