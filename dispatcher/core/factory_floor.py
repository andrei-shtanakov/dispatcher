"""Factory floor, slice C1 — what the factory is running without a human.

Spec: docs/superpowers/specs/2026-09-30-factory-floor-c1-design.md. Every
maestro run that has not ended, with its age, whether dispatcher launched it,
and whether it looks abandoned. It is an observation, not a wait: nothing
here asks a human for anything — but an abandoned run carries a prepared
`run-end` for the human who decides it is over.

The same degradation rules as the human queue: what could not be read is
named in `sources`, never absorbed into a shorter list.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

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
    # Set when dispatcher launched the run and its record can act: the run
    # view's `run-end` verb ends the run AND terminalizes the launch record,
    # which a raw CLI `run-end` would leave open. Consumers route there.
    request_id: str | None = None


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


class FactoryFloorView(BaseModel):
    """In-flight runs, stale ones first, then oldest first; completeness per
    source as in the human queue."""

    in_flight: list[InFlightRun]
    sources: dict[str, SourceStatus]
    complete: bool
    generated_at: str


def build_factory_floor(config: DispatcherConfig, *, now: datetime) -> FactoryFloorView:
    """One read of maestro's runs and dispatcher's launch records."""
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
    return FactoryFloorView(
        in_flight=runs,
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
        activity = _last_activity(_db.parent)
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
                        # Only a record with a checkout: the run view's verbs
                        # refuse records written before that field existed,
                        # so routing those there would be a dead end.
                        request_id=(
                            record.request_id if record and record.checkout else None
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


def _last_activity(run_dir: Path) -> str | None:
    """Newest mtime of the run's own files, as tz-aware ISO-8601."""
    candidates = [run_dir / "state.db", run_dir / "state.db-wal"]
    logs = run_dir / "logs"
    try:
        candidates.extend(p for p in logs.iterdir() if p.is_file())
    except OSError:
        pass
    newest: float | None = None
    for path in candidates:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    return (
        None if newest is None else datetime.fromtimestamp(newest, tz=UTC).isoformat()
    )
