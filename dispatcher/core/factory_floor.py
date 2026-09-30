"""Factory floor — what the factory is doing without a human.

Specs: docs/superpowers/specs/2026-09-30-factory-floor-c1-design.md (C1) and
2026-09-30-factory-floor-c2-design.md (C2). Every maestro run that has not
ended, with its age, whether dispatcher launched it, and whether it looks
abandoned; and what the agent merged in the last 24 hours. It is an
observation, not a wait: nothing here asks a human for anything — but an
abandoned run carries a prepared `run-end` for the human who decides it is
over.

The same degradation rules as the human queue: what could not be read is
named in `sources`, never absorbed into a shorter list.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from dispatcher.core.actions import ActionOutcome
from dispatcher.core.background_reader import BackgroundReader, spawn_daemon
from dispatcher.core.collectors.maestro import classified_runs
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.human_queue import SourceStatus, proven_since
from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.run_store import LaunchRecord, RunStore

#: A run with no observed activity for this long looks abandoned.
STALE_HOURS = 24

RunStatus = Literal["running", "suspended", "interrupted"]

# `running` has positive evidence of a live process (holder + pid); the other
# two are non-terminal without it — the states an orphan sits in.
_IN_FLIGHT: dict[str, RunStatus] = {
    "running": "running",
    "suspended": "suspended",
    "interrupted": "interrupted",
}


class RunEndAct(BaseModel):
    """`maestro run-end` for a run the human decides is over. The outcome
    (superseded | cancelled) is the human's choice, made when acting."""

    kind: Literal["maestro_run_end"] = "maestro_run_end"
    run_id: str
    repo_key: str
    maestro_home: str
    maestro_cli: str | None = None


class InFlightRun(BaseModel):
    """One maestro run that has not ended."""

    repo_key: str
    run_id: str
    status: RunStatus
    started_at: str | None  # tz-aware ISO-8601, or None when unknown
    # Newest mtime among the run's own files (state.db, its WAL, logs/) —
    # observed activity, not a wait start. None when nothing could be stat'ed.
    last_activity_at: str | None
    request_id: str | None  # the dispatcher launch record, if any
    work_id: str | None
    stale: bool
    act: RunEndAct | None  # only for stale runs


AGENT_MERGES_SOURCE = "agent_merges"
#: How far back "what the agent merged" looks.
MERGES_WINDOW_HOURS = 24
#: merged-prs answers are reused this long: the view is polled every few
#: seconds, and a paginated GitHub search must not run that often.
MERGES_TTL_SECONDS = 60.0


class AgentMerge(BaseModel):
    """One PR the agent merged inside the window."""

    repo: str  # owner/name
    number: int
    title: str
    url: str
    merged_at: str  # ISO-8601, as GitHub reports it


class AgentMergesResult(BaseModel):
    """The agent-merges section: its completeness and, when read, the list."""

    status: SourceStatus
    merges: list[AgentMerge] = []


_MERGES_PENDING = AgentMergesResult(
    status=SourceStatus(state="unavailable", detail="first merged-prs in progress")
)


class FactoryFloorView(BaseModel):
    """In-flight runs, stale ones first, then oldest first; the agent's
    merges, newest first; completeness per source as in the human queue."""

    in_flight: list[InFlightRun]
    agent_merges: list[AgentMerge]
    agent_merge_login: str | None
    merges_window_hours: int
    sources: dict[str, SourceStatus]
    complete: bool
    generated_at: str


def from_merged_prs(outcome: ActionOutcome, login: str) -> AgentMergesResult:
    """The merges *login* made, newest first (spec C2 §1).

    Only a list the producer stated is believed: `merges` null — a failed or
    non-exhaustive search, or github-checker not runnable — is `unavailable`,
    never "the agent merged nothing". A merge whose account no longer exists
    (`merged_by` null) is not claimed for the agent.
    """
    if outcome.merges is None or not outcome.ok:
        detail = outcome.error or f"merged-prs did not answer ({outcome.phase})"
        return AgentMergesResult(
            status=SourceStatus(state="unavailable", detail=detail)
        )
    wanted = login.casefold()  # GitHub logins are case-insensitive
    merges = [
        AgentMerge(
            repo=str(m["repo"]),
            number=int(str(m["number"])),
            title=str(m.get("title") or ""),
            url=str(m["url"]),
            merged_at=str(m["merged_at"]),
        )
        for m in outcome.merges
        if isinstance(m.get("merged_by"), str)
        and str(m["merged_by"]).casefold() == wanted
    ]
    merges.sort(key=lambda m: _merge_instant(m.merged_at), reverse=True)
    return AgentMergesResult(status=SourceStatus(state="ok"), merges=merges)


def _merge_instant(value: str) -> datetime:
    """Newest-first sort key; a time that does not parse sorts last instead
    of failing a search that did succeed (review on #285)."""
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _spawn_merges(fn: Callable[[], None]) -> None:
    spawn_daemon(fn, "floor-merged-prs")


class AgentMergesReader(BackgroundReader[AgentMergesResult]):
    """The agent-merges section, refreshed in the background like the human
    queue's forge source: a paginated search never runs on the request
    path, and before the first one lands the section says "in progress"."""

    def __init__(
        self,
        search: Callable[[str], ActionOutcome],
        login: str,
        *,
        wall: Callable[[], datetime] = lambda: datetime.now(UTC),
        ttl: float = MERGES_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None]], None] = _spawn_merges,
    ) -> None:
        def fetch() -> AgentMergesResult:
            since = wall() - timedelta(hours=MERGES_WINDOW_HOURS)
            try:
                return from_merged_prs(search(since.isoformat()), login)
            except Exception as exc:  # noqa: BLE001 — isolation IS the contract
                return _merges_unavailable(exc)

        super().__init__(fetch, _MERGES_PENDING, ttl=ttl, clock=clock, spawn=spawn)
        self.login = login


def _merges_unavailable(exc: Exception) -> AgentMergesResult:
    return AgentMergesResult(
        status=SourceStatus(state="unavailable", detail=f"{type(exc).__name__}: {exc}")
    )


def build_factory_floor(
    config: DispatcherConfig,
    *,
    now: datetime,
    merges: AgentMergesReader | None = None,
) -> FactoryFloorView:
    """One read of maestro's runs and dispatcher's launch records, plus the
    agent's merges as the background reader last saw them."""
    sources: dict[str, SourceStatus] = {}
    records: list[LaunchRecord] = []
    if config.run_state_dir is None:
        sources["dispatcher_runs"] = SourceStatus(
            state="not_configured", detail="run_state_dir is not set"
        )
    else:
        try:
            records, unreadable = RunStore(config.run_state_dir).list()
        except Exception as exc:  # noqa: BLE001 — isolation IS the contract
            sources["dispatcher_runs"] = SourceStatus(
                state="unavailable", detail=f"{type(exc).__name__}: {exc}"
            )
        else:
            sources["dispatcher_runs"] = (
                SourceStatus(
                    state="partial", detail=f"unreadable: {', '.join(unreadable)}"
                )
                if unreadable
                else SourceStatus(state="ok")
            )
    try:
        runs, maestro_status = _in_flight(config, records, now)
    except Exception as exc:  # noqa: BLE001 — isolation IS the contract
        runs = []
        maestro_status = SourceStatus(
            state="unavailable", detail=f"{type(exc).__name__}: {exc}"
        )
    sources["maestro"] = maestro_status
    runs.sort(key=lambda r: (not r.stale, _instant(r.started_at), r.run_id))
    if merges is None:
        merged = AgentMergesResult(
            status=SourceStatus(
                state="not_configured", detail="agent_merge_login is not set"
            )
        )
    else:
        try:
            merged = merges.read()
        except Exception as exc:  # noqa: BLE001 — isolation IS the contract
            merged = _merges_unavailable(exc)
    sources[AGENT_MERGES_SOURCE] = merged.status
    return FactoryFloorView(
        in_flight=runs,
        agent_merges=merged.merges,
        agent_merge_login=None if merges is None else merges.login,
        merges_window_hours=MERGES_WINDOW_HOURS,
        sources=sources,
        complete=all(s.state in ("ok", "not_configured") for s in sources.values()),
        generated_at=now.isoformat(),
    )


def _in_flight(
    config: DispatcherConfig, records: list[LaunchRecord], now: datetime
) -> tuple[list[InFlightRun], SourceStatus]:
    home: Path = config.effective_maestro_home
    snap = ProjectSnapshot(name="maestro", path=str(home))
    classified = classified_runs(home, snap, report_missing_state=True)
    by_run = {(r.repo_key, r.run_id): r for r in records if r.run_id}
    out: list[InFlightRun] = []
    for info, _db in classified:
        status = _IN_FLIGHT.get(info.status)
        if status is None or info.run_id is None:
            continue
        started, _ = proven_since(info.started_at, "run started")
        activity, unreadable = _last_activity(_db.parent)
        if unreadable is not None:
            # An activity we could not fully read is unknown — never stale —
            # and the hole is named, not swallowed (review on #282).
            snap.warnings.append(f"run {info.run_id}: {unreadable}")
            activity = None
        # Not `started_at`: a run dispatcher launches is never `running` (the
        # holder is written only by maestro's service tick), so age from the
        # start would call a live 25-hour run abandoned. A live run writes.
        # Only `interrupted` can be abandoned: `running` has a live holder,
        # and `suspended` is a run deliberately parked for a human — however
        # long it waits, that is a wait, never an orphan (review on #282).
        stale = status == "interrupted" and _older_than(activity, now, STALE_HOURS)
        record = by_run.get((info.repo_key, info.run_id))
        out.append(
            InFlightRun(
                repo_key=info.repo_key,
                run_id=info.run_id,
                status=status,
                started_at=started,
                last_activity_at=activity,
                request_id=record.request_id if record else None,
                work_id=(record.work_id or None) if record else None,
                stale=stale,
                act=(
                    RunEndAct(
                        run_id=info.run_id,
                        repo_key=info.repo_key,
                        maestro_home=str(home),
                        maestro_cli=(
                            None
                            if config.maestro_cli is None
                            else str(config.maestro_cli)
                        ),
                    )
                    if stale
                    else None
                ),
            )
        )
    status = (
        SourceStatus(state="partial", detail="; ".join(snap.warnings))
        if snap.warnings
        else SourceStatus(state="ok")
    )
    return out, status


def _older_than(started: str | None, now: datetime, hours: int) -> bool:
    """Strictly older; an unknown start is never called stale."""
    if started is None:
        return False
    return (now - datetime.fromisoformat(started)).total_seconds() > hours * 3600


def _instant(value: str | None) -> datetime:
    """Sort key by instant; an unknown start sorts last."""
    return (
        datetime.max.replace(tzinfo=UTC)
        if value is None
        else datetime.fromisoformat(value)
    )


def _last_activity(run_dir: Path) -> tuple[str | None, str | None]:
    """(newest mtime of the run's own files as tz-aware ISO-8601, problem).

    An absent `logs/` is normal; one that exists but cannot be listed is a
    problem — a newer log inside it could be exactly the activity that
    proves the run alive.

    The WAL counts only when it holds pages. Merely OPENING a WAL database
    refreshes an empty `-wal`'s mtime (and `-shm`'s), and maestro opens every
    sibling run while resolving one — observed live 2026-09-30: `run-end` on
    one deployer run made a month-old orphan look active. A writer that is
    alive keeps uncheckpointed pages in the WAL; a checkpoint on close moves
    them into `state.db`, whose own mtime then carries the activity.
    """
    candidates = [run_dir / "state.db"]
    wal = run_dir / "state.db-wal"
    try:
        if wal.stat().st_size > 0:
            candidates.append(wal)
    except OSError:
        pass
    logs = run_dir / "logs"
    try:
        candidates.extend(p for p in logs.iterdir() if p.is_file())
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError as err:
        return None, f"cannot list {logs}: {err}"
    newest: float | None = None
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    if newest is None:
        return None, None
    return datetime.fromtimestamp(newest, tz=UTC).isoformat(), None
