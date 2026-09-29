"""HTTP surface of the human queue (spec 2026-09-29-human-queue-a1 §4.1, §6)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
from conftest import make_maestro_run

from dispatcher.core import human_queue_sources
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_store import RunStore
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio

_REQ = "11111111-1111-4111-8111-111111111111"
_ACME = ("github.com", "acme", "app")


def _config(tmp_path: Path) -> DispatcherConfig:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return DispatcherConfig(
        roots=(ws,),
        maestro_home=tmp_path / "mhome",
        run_state_dir=tmp_path / "state",
    )


def _client(config: DispatcherConfig) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(config))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_queue_over_local_sources_is_partial_by_construction(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    store.reserve(
        _REQ,
        RepoKey(host="github.com", owner="acme", repo="app"),
        known_runs=[],
        window_start="t",
    )
    store.mark_unknown(_REQ, "no run appeared")
    make_maestro_run(
        tmp_path / "mhome", _ACME, "01RUN", started_at="2026-09-01T00:00:00"
    )

    async with _client(config) as client:
        resp = await client.get("/api/human-queue")

    assert resp.status_code == 200
    body = resp.json()
    assert [w["key"] for w in body["waits"]] == [f"dispatcher-run:{_REQ}"]
    assert body["sources"]["dispatcher_runs"]["state"] == "ok"
    assert body["sources"]["maestro"]["state"] == "ok"
    assert body["sources"]["impresario"]["state"] == "not_configured"
    assert body["sources"]["forge_labelled_prs"]["state"] == "not_connected"
    assert body["sources"]["forge_candidate_prs"]["state"] == "not_connected"
    assert body["complete"] is False


async def test_a_raising_adapter_is_unavailable_not_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The healthy sources carry real waits, so a guard that dropped them
    alongside the failing one would fail here."""
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    store.reserve(
        _REQ,
        RepoKey(host="github.com", owner="acme", repo="app"),
        known_runs=[],
        window_start="t",
    )
    store.mark_unknown(_REQ, "no run appeared")
    db = make_maestro_run(
        tmp_path / "mhome", _ACME, "01RUN", started_at="2026-09-01T00:00:00"
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("T-1", "t", "needs_review", "claude_code", "2026-09-01", None, None),
        )
        conn.commit()
    finally:
        conn.close()

    def boom(_cache: object) -> object:
        raise RuntimeError("mirror exploded")

    monkeypatch.setattr(human_queue_sources, "from_impresario", boom)
    async with _client(config) as client:
        resp = await client.get("/api/human-queue")

    assert resp.status_code == 200
    body = resp.json()
    sources = body["sources"]
    assert sources["impresario"]["state"] == "unavailable"
    assert "mirror exploded" in sources["impresario"]["detail"]
    assert sources["dispatcher_runs"]["state"] == "ok"
    assert sources["maestro"]["state"] == "ok"
    assert sorted(w["source"] for w in body["waits"]) == [
        "dispatcher_runs",
        "maestro",
    ]


async def test_a_failing_run_store_does_not_hide_maestro_waits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store read is an enrichment for maestro, not its precondition."""
    config = _config(tmp_path)
    db = make_maestro_run(
        tmp_path / "mhome", _ACME, "01RUN", started_at="2026-09-01T00:00:00"
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("T-1", "t", "needs_review", "claude_code", "2026-09-01", None, None),
        )
        conn.commit()
    finally:
        conn.close()

    def boom(_self: RunStore) -> object:
        raise RuntimeError("store exploded")

    monkeypatch.setattr(RunStore, "list", boom)
    async with _client(config) as client:
        resp = await client.get("/api/human-queue")

    assert resp.status_code == 200
    body = resp.json()
    assert body["sources"]["dispatcher_runs"]["state"] == "unavailable"
    assert "store exploded" in body["sources"]["dispatcher_runs"]["detail"]
    assert body["sources"]["maestro"]["state"] == "ok"
    [wait] = body["waits"]
    assert wait["source"] == "maestro"
    assert wait["act"]["kind"] == "maestro_verb"
