"""The DarkFactory halt, slice D1 (spec 2026-09-30-halt-d1-design)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from dispatcher.core.actions import ActionOutcome, ActionRejectedError
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.halt import (
    HaltApplier,
    HaltBusyError,
    HaltReader,
    HaltRejectedError,
    HaltStore,
    build_halt_view,
    read_fleet,
)
from dispatcher.server.app import create_app

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
_FLEET = ("alpha", "beta")


def _outcome(
    state: str | None,
    *,
    ok: bool = True,
    changed: bool | None = None,
    error: str | None = None,
) -> ActionOutcome:
    return ActionOutcome(
        action="halt-read",
        dir="x",
        ok=ok,
        error=error,
        changed=changed,
        halt=None
        if state is None
        else {"state": state, "ruleset_id": 7, "detail": None},
        phase="readable_result",
    )


def _sync(fn):
    fn()


class Forge:
    """Per-repo halt state; halt_set flips it unless told to fail."""

    def __init__(self, states: dict[str, str], *, fail: set[str] | None = None) -> None:
        self.states = dict(states)
        self.fail = fail or set()
        self.writes: list[tuple[str, str]] = []

    def read(self, repo: str) -> ActionOutcome:
        return _outcome(self.states[repo])

    def write(self, repo: str, target: str) -> ActionOutcome:
        self.writes.append((repo, target))
        if repo in self.fail:
            return _outcome(
                self.states[repo], ok=False, changed=False, error="HTTP 403"
            )
        changed = self.states[repo] != target
        self.states[repo] = target
        return _outcome(target, changed=changed)


def _applier(forge: Forge, tmp_path: Path, **kw) -> tuple[HaltApplier, HaltStore]:
    store = HaltStore(tmp_path)
    return HaltApplier(_FLEET, forge.write, store, spawn=_sync, **kw), store


# --- read ---------------------------------------------------------------


def test_a_failed_or_refused_read_is_unknown_never_off() -> None:
    def read(repo: str) -> ActionOutcome:
        if repo == "alpha":
            return _outcome(None, ok=False, error="gh down")
        raise ActionRejectedError("not a git repo in workspace: beta")

    got = read_fleet(_FLEET, read)
    assert [(r.repo, r.state) for r in got] == [
        ("alpha", "unknown"),
        ("beta", "unknown"),
    ]
    assert got[0].detail == "gh down"


def test_the_view_counts_halted_and_names_the_unhealthy(tmp_path: Path) -> None:
    forge = Forge({"alpha": "on", "beta": "misconfigured"})
    reader = HaltReader(_FLEET, forge.read, clock=lambda: 0.0, spawn=_sync)
    view = build_halt_view(_FLEET, reader, HaltStore(tmp_path), now=_NOW.isoformat())
    assert (view.halted, view.unhealthy) == (1, ["beta"])
    assert view.sources["forge_halt"].state == "ok"
    assert view.complete is True


def test_before_the_first_read_it_says_in_progress(tmp_path: Path) -> None:
    pending: list = []
    reader = HaltReader(_FLEET, Forge({}).read, clock=lambda: 0.0, spawn=pending.append)
    view = build_halt_view(_FLEET, reader, HaltStore(tmp_path), now="t")
    assert view.fleet == []
    assert view.sources["forge_halt"].state == "unavailable"
    assert "in progress" in (view.sources["forge_halt"].detail or "")
    assert view.complete is False


def test_an_unknown_repo_makes_the_view_partial(tmp_path: Path) -> None:
    forge = Forge({"alpha": "off", "beta": "unknown"})
    reader = HaltReader(_FLEET, forge.read, clock=lambda: 0.0, spawn=_sync)
    view = build_halt_view(_FLEET, reader, HaltStore(tmp_path), now="t")
    assert view.sources["forge_halt"].state == "partial"
    assert view.complete is False


def test_no_fleet_is_not_configured() -> None:
    view = build_halt_view((), None, None, now="t")
    assert view.sources["forge_halt"].state == "not_configured"
    assert view.sources["halt_requests"].state == "not_configured"
    assert view.complete is True


# --- apply --------------------------------------------------------------


def test_a_fleet_halt_is_recorded_before_and_after_the_writes(tmp_path: Path) -> None:
    forge = Forge({"alpha": "off", "beta": "off"})
    applier, store = _applier(forge, tmp_path)
    request = applier.start("on", None, "incident: bad merges", now=_NOW)
    assert (request.scope, request.repos) == ("fleet", ["alpha", "beta"])
    lines = (tmp_path / "halt-requests.jsonl").read_text().splitlines()
    assert len(lines) == 2  # the request, then the request with its results
    assert json.loads(lines[0])["results"] == []
    last, problem = store.last()
    assert problem is None and last is not None
    assert [(r.repo, r.ok, r.state) for r in last.results] == [
        ("alpha", True, "on"),
        ("beta", True, "on"),
    ]
    assert applier.applying is None


def test_a_partial_failure_keeps_what_landed_and_rolls_nothing_back(
    tmp_path: Path,
) -> None:
    forge = Forge({"alpha": "off", "beta": "off"}, fail={"beta"})
    applier, store = _applier(forge, tmp_path)
    applier.start("on", None, "incident", now=_NOW)
    last, _ = store.last()
    assert last is not None
    assert [(r.repo, r.ok, r.state) for r in last.results] == [
        ("alpha", True, "on"),
        ("beta", False, "off"),
    ]
    assert forge.states == {"alpha": "on", "beta": "off"}
    # Running it again retries: alpha answers unchanged, beta is attempted.
    forge.fail.clear()
    applier.start("on", None, "incident, retry", now=_NOW)
    last, _ = store.last()
    assert last is not None
    assert [(r.repo, r.changed) for r in last.results] == [
        ("alpha", False),
        ("beta", True),
    ]


def test_a_write_that_raises_does_not_stop_the_rest(tmp_path: Path) -> None:
    forge = Forge({"alpha": "off", "beta": "off"})

    def write(repo: str, target: str) -> ActionOutcome:
        if repo == "alpha":
            raise RuntimeError("boom")
        return forge.write(repo, target)

    store = HaltStore(tmp_path)
    HaltApplier(_FLEET, write, store, spawn=_sync).start("on", None, "x", now=_NOW)
    last, _ = store.last()
    assert last is not None
    assert [(r.repo, r.ok) for r in last.results] == [("alpha", False), ("beta", True)]


@pytest.mark.parametrize(
    ("target", "repos", "reason", "needle"),
    [
        ("pause", None, "why", "on|off"),
        ("on", None, "   ", "reason is required"),
        ("on", None, "two\nlines", "reason is required"),
        ("on", None, "x" * 501, "reason is required"),
        ("on", ["gamma"], "why", "not in it"),
        ("on", [], "why", "non-empty subset"),
    ],
)
def test_bad_requests_are_refused_before_anything_is_written(
    tmp_path: Path, target, repos, reason, needle
) -> None:
    forge = Forge({"alpha": "off", "beta": "off"})
    applier, _ = _applier(forge, tmp_path)
    with pytest.raises(HaltRejectedError, match=needle):
        applier.start(target, repos, reason, now=_NOW)
    assert forge.writes == []
    assert not (tmp_path / "halt-requests.jsonl").exists()


def test_one_request_at_a_time(tmp_path: Path) -> None:
    forge = Forge({"alpha": "off", "beta": "off"})
    pending: list = []
    applier = HaltApplier(
        _FLEET, forge.write, HaltStore(tmp_path), spawn=pending.append
    )
    first = applier.start("on", None, "one", now=_NOW)
    assert applier.applying == first.request_id
    with pytest.raises(HaltBusyError):
        applier.start("off", None, "two", now=_NOW)
    pending.pop()()
    assert applier.applying is None


def test_after_applying_the_reader_is_refreshed(tmp_path: Path) -> None:
    forge = Forge({"alpha": "off", "beta": "off"})
    reader = HaltReader(_FLEET, forge.read, clock=lambda: 0.0, spawn=_sync)
    assert [r.state for r in reader.read() or []] == ["off", "off"]
    applier, _ = _applier(forge, tmp_path, on_applied=reader.invalidate)
    applier.start("on", ["alpha"], "one repo", now=_NOW)
    assert [r.state for r in reader.read() or []] == ["on", "off"]


# --- deviations ---------------------------------------------------------


def test_repos_that_joined_after_a_fleet_request_are_deviations(tmp_path: Path) -> None:
    forge = Forge({"alpha": "off", "beta": "off", "gamma": "off"})
    store = HaltStore(tmp_path)
    HaltApplier(_FLEET, forge.write, store, spawn=_sync).start(
        "on", None, "x", now=_NOW
    )
    grown = (*_FLEET, "gamma")
    reader = HaltReader(grown, forge.read, clock=lambda: 0.0, spawn=_sync)
    view = build_halt_view(grown, reader, store, now="t")
    assert view.deviations == ["gamma: joined the fleet after the last fleet request"]


def test_a_repo_that_reads_otherwise_than_requested_is_a_deviation(
    tmp_path: Path,
) -> None:
    forge = Forge({"alpha": "off", "beta": "off"})
    store = HaltStore(tmp_path)
    HaltApplier(_FLEET, forge.write, store, spawn=_sync).start(
        "on", None, "x", now=_NOW
    )
    forge.states["beta"] = "off"  # someone lifted it by hand
    reader = HaltReader(_FLEET, forge.read, clock=lambda: 0.0, spawn=_sync)
    view = build_halt_view(_FLEET, reader, store, now="t")
    assert view.deviations == ["beta: requested on, reads off"]


def test_a_torn_request_line_is_named_not_skipped(tmp_path: Path) -> None:
    forge = Forge({"alpha": "off", "beta": "off"})
    store = HaltStore(tmp_path)
    HaltApplier(_FLEET, forge.write, store, spawn=_sync).start(
        "on", None, "x", now=_NOW
    )
    with (tmp_path / "halt-requests.jsonl").open("a") as fh:
        fh.write('{"torn": \n')
    last, problem = store.last()
    assert last is not None and last.target == "on"
    assert problem is not None and "unreadable line" in problem


# --- HTTP ---------------------------------------------------------------


def _app(tmp_path: Path, halt_fleet: tuple[str, ...] = ()):
    (tmp_path / "ws").mkdir(exist_ok=True)
    return create_app(
        DispatcherConfig(
            roots=(tmp_path / "ws",),
            run_state_dir=tmp_path / "state",
            halt_fleet=halt_fleet,
        )
    )


async def _token(client: httpx.AsyncClient) -> str:
    return (await client.get("/api/actions/session")).json()["token"]


@pytest.mark.anyio
async def test_the_endpoints_without_a_fleet(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=_app(tmp_path))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        view = await client.get("/api/halt")
        assert view.status_code == 200
        assert view.json()["sources"]["forge_halt"]["state"] == "not_configured"
        body = {"state": "on", "reason": "x"}
        assert (await client.post("/api/halt", json=body)).status_code == 403
        resp = await client.post(
            "/api/halt", json=body, headers={"X-Action-Token": await _token(client)}
        )
        assert resp.status_code == 422
        assert "halt_fleet" in resp.json()["detail"]


@pytest.mark.anyio
async def test_a_bad_request_is_422_and_touches_nothing(tmp_path: Path) -> None:
    transport = httpx.ASGITransport(app=_app(tmp_path, halt_fleet=("alpha",)))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post(
            "/api/halt",
            json={"state": "on", "reason": " "},
            headers={"X-Action-Token": await _token(client)},
        )
    assert resp.status_code == 422
    assert not (tmp_path / "state" / "halt-requests.jsonl").exists()


def test_a_repo_that_left_the_fleet_while_halted_stays_visible(tmp_path: Path) -> None:
    """Review #287: dropping a halted repo from halt_fleet must not make its
    halt disappear from view."""
    forge = Forge({"alpha": "off", "beta": "off"})
    store = HaltStore(tmp_path)
    HaltApplier(_FLEET, forge.write, store, spawn=_sync).start(
        "on", None, "x", now=_NOW
    )
    shrunk = ("alpha",)
    reader = HaltReader(shrunk, forge.read, clock=lambda: 0.0, spawn=_sync)
    view = build_halt_view(shrunk, reader, store, now="t")
    assert view.deviations == [
        "beta: left halt_fleet after the last request — its halt is no longer read"
    ]


def test_a_read_that_raises_is_unknown_and_the_rest_are_read() -> None:
    def read(repo: str) -> ActionOutcome:
        if repo == "alpha":
            raise RuntimeError("boom")
        return _outcome("off")

    got = read_fleet(_FLEET, read)
    assert [(r.repo, r.state) for r in got] == [("alpha", "unknown"), ("beta", "off")]


def test_the_journal_is_private(tmp_path: Path) -> None:
    import stat

    state = tmp_path / "state"
    store = HaltStore(state)
    HaltApplier(
        _FLEET, Forge({"alpha": "off", "beta": "off"}).write, store, spawn=_sync
    ).start("on", None, "x", now=_NOW)
    assert stat.S_IMODE((state / "halt-requests.jsonl").stat().st_mode) == 0o600
    assert stat.S_IMODE(state.stat().st_mode) == 0o700


def test_an_invalidation_during_a_refresh_is_not_lost() -> None:
    """Review #287: a halt applied while the fleet was being read must not
    leave that (pre-halt) read trusted for a whole TTL."""
    pending: list = []
    holder: list[HaltReader] = []
    calls = [0]

    def read(repo: str) -> ActionOutcome:
        calls[0] += 1
        if calls[0] == 1:
            holder[0].invalidate()  # the halt lands mid-read
        return _outcome("off")

    reader = HaltReader(_FLEET, read, clock=lambda: 0.0, spawn=pending.append)
    holder.append(reader)
    reader.read()
    pending.pop()()  # the in-flight read completes after the invalidation
    assert reader.read() is not None  # its value is served…
    assert len(pending) == 1  # …but a fresh read has already started
