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

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from dispatcher.core.collectors.maestro import classified_runs
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.human_queue import SourceStatus, proven_since
from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.run_store import LaunchRecord, RunStore

#: A run that is not live and started longer ago than this looks abandoned.
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
    runs.sort(key=lambda r: (not r.stale, r.started_at or "~", r.run_id))
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
        stale = status != "running" and _older_than(started, now, STALE_HOURS)
        record = by_run.get((info.repo_key, info.run_id))
        out.append(
            InFlightRun(
                repo_key=info.repo_key,
                run_id=info.run_id,
                status=status,
                started_at=started,
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
