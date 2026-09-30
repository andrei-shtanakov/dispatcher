"""Factory floor C1 (spec 2026-09-30-factory-floor-c1-design)."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from conftest import make_maestro_run, write_holder

from dispatcher.core.actions import ActionOutcome
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.factory_floor import (
    AgentMergesReader,
    build_factory_floor,
    from_merged_prs,
)
from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_store import RunStore
from dispatcher.server.app import create_app

_ACME = ("github.com", "acme", "app")
_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
_RECENT = datetime(2026, 9, 30, 11, 30, tzinfo=timezone.utc)
_REQ = "11111111-1111-4111-8111-111111111111"


def _config(tmp_path: Path, *, control_plane: bool = True) -> DispatcherConfig:
    return DispatcherConfig(
        roots=(tmp_path / "ws",),
        maestro_home=tmp_path / "mhome",
        run_state_dir=tmp_path / "state" if control_plane else None,
        maestro_cli=tmp_path / "bin" / "maestro",
    )


def _touch(path: Path, when: datetime) -> None:
    """Make a run file look last written at *when*."""
    ts = when.timestamp()
    os.utime(path, (ts, ts))


def _run(run_id: str, started: str, *, active: datetime, tmp_path: Path) -> Path:
    db = make_maestro_run(tmp_path / "mhome", _ACME, run_id, started_at=started)
    _touch(db, active)
    return db


def test_only_unfinished_runs_stale_first_with_a_prepared_run_end(
    tmp_path: Path,
) -> None:
    home = tmp_path / "mhome"
    _run(
        "01OLD",
        "2026-08-24T07:29:18+00:00",
        active=datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
        tmp_path=tmp_path,
    )
    _run("01NEW", "2026-09-30T11:00:00+00:00", active=_RECENT, tmp_path=tmp_path)
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
    assert old.last_activity_at == "2026-08-24T08:00:00+00:00"
    assert old.act is not None
    assert old.act.model_dump() == {
        "kind": "maestro_run_end",
        "run_id": "01OLD",
        "repo_key": "github.com/acme/app",
        "maestro_home": str(home),
        "maestro_cli": str(tmp_path / "bin" / "maestro"),
    }
    assert (new.stale, new.act) == (False, None)


def test_a_long_run_that_still_writes_is_not_stale(tmp_path: Path) -> None:
    """Review on #282: dispatcher-launched runs are never `running` (no
    holder outside maestro's service tick). A 3-day-old interrupted run that
    wrote its state.db ten minutes ago is alive, not abandoned."""
    _run(
        "01LONG",
        "2026-09-27T12:00:00+00:00",
        active=datetime(2026, 9, 30, 11, 50, tzinfo=timezone.utc),
        tmp_path=tmp_path,
    )
    [run] = build_factory_floor(_config(tmp_path), now=_NOW).in_flight
    assert (run.status, run.stale, run.act) == ("interrupted", False, None)


def test_recent_logs_count_as_activity(tmp_path: Path) -> None:
    db = _run(
        "01LOGS",
        "2026-09-01T00:00:00+00:00",
        active=datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc),
        tmp_path=tmp_path,
    )
    log = db.parent / "logs" / "task.log"
    log.parent.mkdir()
    log.write_text("still going\n")
    _touch(log, datetime(2026, 9, 30, 11, 55, tzinfo=timezone.utc))
    [run] = build_factory_floor(_config(tmp_path), now=_NOW).in_flight
    assert run.stale is False


def test_a_live_run_is_never_stale(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    _run(
        "01LIVE",
        "2026-08-01T00:00:00+00:00",
        active=datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc),
        tmp_path=tmp_path,
    )
    write_holder(home, _ACME, "01LIVE", os.getpid())
    [run] = build_factory_floor(_config(tmp_path), now=_NOW).in_flight
    assert (run.status, run.stale, run.act) == ("running", False, None)


def test_an_unknown_start_is_shown_as_unknown_and_sorts_last(tmp_path: Path) -> None:
    _run("01NAIVE", "2026-08-01T00:00:00", active=_RECENT, tmp_path=tmp_path)
    _run("01KNOWN", "2026-09-30T11:00:00+00:00", active=_RECENT, tmp_path=tmp_path)
    runs = build_factory_floor(_config(tmp_path), now=_NOW).in_flight
    assert [r.run_id for r in runs] == ["01KNOWN", "01NAIVE"]
    assert runs[1].started_at is None


def test_order_is_by_instant_not_text(tmp_path: Path) -> None:
    """'10:00+02:00' (08:00Z) is older than '09:00Z' though later as text."""
    _run("01Z", "2026-09-30T09:00:00+00:00", active=_RECENT, tmp_path=tmp_path)
    _run("01P2", "2026-09-30T10:00:00+02:00", active=_RECENT, tmp_path=tmp_path)
    runs = build_factory_floor(_config(tmp_path), now=_NOW).in_flight
    assert [r.run_id for r in runs] == ["01P2", "01Z"]


def test_the_dispatcher_record_is_joined(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    _run("01RUN", "2026-09-30T11:00:00+00:00", active=_RECENT, tmp_path=tmp_path)
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
    _run("01GOOD", "2026-09-30T11:00:00+00:00", active=_RECENT, tmp_path=tmp_path)
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


def test_a_suspended_run_is_waiting_not_abandoned(tmp_path: Path) -> None:
    """Review on #282: `suspended` is a run parked for a human. However long
    it has been idle, it must never be offered a run-end."""
    db = make_maestro_run(
        tmp_path / "mhome",
        _ACME,
        "01PARKED",
        started_at="2026-08-01T00:00:00+00:00",
        suspended_at="2026-08-01T01:00:00+00:00",
    )
    _touch(db, datetime(2026, 8, 1, 1, 0, tzinfo=timezone.utc))
    [run] = build_factory_floor(_config(tmp_path), now=_NOW).in_flight
    assert (run.status, run.stale, run.act) == ("suspended", False, None)


def test_a_dispatcher_launched_stale_run_still_gets_the_cli_run_end(
    tmp_path: Path,
) -> None:
    """Review round 3 on #282: the web run view has no run-end (it lives
    only in the launch_unknown flow), and the extension executes nothing —
    so the CLI line is the one reachable path for every stale run. The
    launch record is still named, so the human knows it stays open."""
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    _run(
        "01ORPH",
        "2026-08-24T07:00:00+00:00",
        active=datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
        tmp_path=tmp_path,
    )
    store = RunStore(config.run_state_dir)
    store.reserve(
        _REQ,
        RepoKey(host="github.com", owner="acme", repo="app"),
        known_runs=[],
        window_start="t",
        checkout=str(tmp_path / "ws" / "app"),
    )
    store.mark_materialized(_REQ, "01ORPH")
    [run] = build_factory_floor(config, now=_NOW).in_flight
    assert (run.stale, run.request_id) == (True, _REQ)
    assert run.act is not None
    assert "request_id" not in run.act.model_dump()


def test_unreadable_logs_make_activity_unknown_not_stale(tmp_path: Path) -> None:
    db = _run(
        "01LOCKED",
        "2026-08-24T07:00:00+00:00",
        active=datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
        tmp_path=tmp_path,
    )
    logs = db.parent / "logs"
    logs.mkdir()
    logs.chmod(0)
    try:
        if os.access(logs, os.R_OK):
            pytest.skip("running as a user that ignores directory permissions")
        view = build_factory_floor(_config(tmp_path), now=_NOW)
    finally:
        logs.chmod(0o755)
    [run] = view.in_flight
    assert (run.last_activity_at, run.stale) == (None, False)
    assert view.sources["maestro"].state == "partial"
    assert "cannot list" in (view.sources["maestro"].detail or "")


def test_an_empty_wal_touched_by_a_reader_is_not_activity(tmp_path: Path) -> None:
    """Live 2026-09-30: maestro opens every sibling run DB while resolving
    one, which refreshes an EMPTY -wal's mtime — a month-old orphan looked
    active. Only a WAL holding pages is a live writer."""
    db = _run(
        "01ORPH",
        "2026-08-24T07:00:00+00:00",
        active=datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
        tmp_path=tmp_path,
    )
    wal = db.parent / "state.db-wal"
    wal.write_bytes(b"")
    _touch(wal, _RECENT)
    [run] = build_factory_floor(_config(tmp_path), now=_NOW).in_flight
    assert run.stale is True
    assert run.last_activity_at == "2026-08-24T08:00:00+00:00"


def test_a_wal_holding_pages_is_activity(tmp_path: Path) -> None:
    db = _run(
        "01LIVEW",
        "2026-08-24T07:00:00+00:00",
        active=datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
        tmp_path=tmp_path,
    )
    wal = db.parent / "state.db-wal"
    wal.write_bytes(b"\x00" * 4096)
    _touch(wal, _RECENT)
    [run] = build_factory_floor(_config(tmp_path), now=_NOW).in_flight
    assert run.stale is False


# --- C2: what the agent merged in the last 24 h --------------------------


def _merge(number: int, by: str | None, at: str, repo: str = "acme/app") -> dict:
    return {
        "repo": repo,
        "number": number,
        "title": f"pr {number}",
        "url": f"https://github.com/{repo}/pull/{number}",
        "merged_at": at,
        "merged_by": by,
    }


def _merged(merges: list[dict] | None, *, ok: bool = True) -> ActionOutcome:
    return ActionOutcome(
        action="merged-prs",
        dir="dispatcher",
        ok=ok,
        error=None if ok else "search counted 3 merges but returned 2",
        merges=merges,
        phase="readable_result",
    )


def _sync(fn):
    fn()


def _reader(search, **kw) -> AgentMergesReader:
    return AgentMergesReader(
        search, "ai-prosto", wall=lambda: _NOW, clock=lambda: 0.0, spawn=_sync, **kw
    )


def test_only_the_agents_merges_newest_first() -> None:
    result = from_merged_prs(
        _merged(
            [
                _merge(1, "ai-prosto", "2026-09-30T08:00:00Z"),
                _merge(2, "andrei-shtanakov", "2026-09-30T09:00:00Z"),
                _merge(3, "AI-Prosto", "2026-09-30T10:00:00Z", "acme/lib"),
                _merge(4, None, "2026-09-30T11:00:00Z"),
            ]
        ),
        "ai-prosto",
    )
    assert result.status.state == "ok"
    # a human's merge and a deleted account's merge are not the agent's
    assert [(m.repo, m.number) for m in result.merges] == [
        ("acme/lib", 3),
        ("acme/app", 1),
    ]


def test_an_unread_search_is_unavailable_not_no_merges() -> None:
    for outcome in (_merged(None, ok=False), _merged([], ok=False)):
        result = from_merged_prs(outcome, "ai-prosto")
        assert result.status.state == "unavailable"
        assert result.merges == []


def test_the_reader_searches_the_last_24_hours() -> None:
    asked: list[str] = []

    def search(since: str) -> ActionOutcome:
        asked.append(since)
        return _merged([])

    result = _reader(search).read()
    assert result.status.state == "ok"
    assert asked == ["2026-09-29T12:00:00+00:00"]


def test_a_raising_search_is_unavailable() -> None:
    def search(since: str) -> ActionOutcome:
        raise RuntimeError("github down")

    result = _reader(search).read()
    assert result.status.state == "unavailable"
    assert "github down" in (result.status.detail or "")


def test_before_the_first_search_lands_it_says_in_progress() -> None:
    pending: list = []
    reader = AgentMergesReader(
        lambda since: _merged([]),
        "ai-prosto",
        clock=lambda: 0.0,
        spawn=pending.append,
    )
    first = reader.read()
    assert first.status.state == "unavailable"
    assert "in progress" in (first.status.detail or "")


def test_the_view_carries_the_merges_and_their_completeness(tmp_path: Path) -> None:
    reader = _reader(
        lambda since: _merged([_merge(9, "ai-prosto", "2026-09-30T09:15:33Z")])
    )
    view = build_factory_floor(_config(tmp_path), now=_NOW, merges=reader)
    assert [m.number for m in view.agent_merges] == [9]
    assert (view.agent_merge_login, view.merges_window_hours) == ("ai-prosto", 24)
    assert view.sources["agent_merges"].state == "ok"
    assert view.complete is True


def test_an_unread_merges_section_makes_the_view_incomplete(tmp_path: Path) -> None:
    reader = _reader(lambda since: _merged(None, ok=False))
    view = build_factory_floor(_config(tmp_path), now=_NOW, merges=reader)
    assert view.agent_merges == []
    assert view.sources["agent_merges"].state == "unavailable"
    assert view.complete is False


def test_no_login_is_not_configured(tmp_path: Path) -> None:
    view = build_factory_floor(_config(tmp_path), now=_NOW)
    assert view.sources["agent_merges"].state == "not_configured"
    assert view.agent_merge_login is None
    assert view.complete is True


def test_a_reader_that_cannot_start_is_unavailable_not_a_500(tmp_path: Path) -> None:
    def no_threads(fn):
        raise RuntimeError("can't start new thread")

    reader = AgentMergesReader(lambda since: _merged([]), "ai-prosto", spawn=no_threads)
    view = build_factory_floor(_config(tmp_path), now=_NOW, merges=reader)
    assert view.sources["agent_merges"].state == "unavailable"
    assert "new thread" in (view.sources["agent_merges"].detail or "")


def test_a_garbled_merge_time_sorts_last_not_unavailable() -> None:
    """Review on #285: one bad merged_at must not blank a search that
    succeeded."""
    result = from_merged_prs(
        _merged(
            [
                _merge(1, "ai-prosto", "garbled"),
                _merge(2, "ai-prosto", "2026-09-30T08:00:00Z"),
                _merge(3, "ai-prosto", "2026-09-30T08:30:00"),
            ]
        ),
        "ai-prosto",
    )
    assert result.status.state == "ok"
    assert [m.number for m in result.merges] == [3, 2, 1]
