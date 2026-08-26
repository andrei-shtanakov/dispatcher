"""Pure admission classification (spec §5, §7).

No IO by construction: every function consumes captured values. Both
adapters — the launchpad snapshot assembler (PR-C) and submit's gate
(this PR) — call these same functions, and the adapter-level property
test of spec §5 is what keeps a second implementation from growing.
"""

from __future__ import annotations

from dataclasses import dataclass

from dispatcher.core.run_store import LockInfo, Malformed

# Codes — the single vocabulary shared by receipts now and 409s in PR-C.
LAUNCH_BUSY = "launch_busy"
RUN_IN_FLIGHT = "run_in_flight"
RUN_VANISHED = "run_vanished"
LOCK_MALFORMED = "lock_malformed"
LOCK_IO_UNREADABLE = "lock_io_unreadable"
RUN_STATE_UNREADABLE = "run_state_unreadable"
GUARD_BUSY = "guard_busy"

#: Fail-closed by SUBTRACTION (spec §7): terminal is the allowlist, and
#: any status outside it — today's, or one invented after this line was
#: written — blocks. An allowlist of blocking statuses would fail open
#: on the first new status maestro grows.
TERMINAL_RUN_STATUSES = frozenset({"completed", "cancelled", "superseded", "failed"})


@dataclass(frozen=True)
class RunFact:  # captured from classified_runs / launch_records
    run_id: str
    status: str
    request_id: str | None
    run_dir_exists: bool


@dataclass(frozen=True)
class Blocker:
    code: str
    request_id: str | None = None
    run_id: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class RepoAdmission:
    admission: str  # "ready" | "blocked"
    blockers: tuple[Blocker, ...]


def classify_repo(
    lock: LockInfo | Malformed | None,
    lock_error: str | None,  # an IO failure reading the lock
    runs: tuple[RunFact, ...],
    runs_unreadable: tuple[str, ...],  # unreadable state sources, by name
) -> RepoAdmission:
    """One repo's admission decision from already-captured facts.

    Every blocker that applies is collected — no hidden priority between
    a busy lock and an in-flight run, both surface (spec review).
    """
    blockers: list[Blocker] = []
    if lock_error is not None:
        blockers.append(Blocker(code=LOCK_IO_UNREADABLE, detail=lock_error))
    elif isinstance(lock, Malformed):
        blockers.append(Blocker(code=LOCK_MALFORMED, detail=lock.detail))
    elif isinstance(lock, LockInfo):
        blockers.append(Blocker(code=LAUNCH_BUSY, request_id=lock.request_id))
    for name in runs_unreadable:
        blockers.append(Blocker(code=RUN_STATE_UNREADABLE, detail=name))
    for run in runs:
        if run.status in TERMINAL_RUN_STATUSES:
            continue
        if run.request_id is not None and not run.run_dir_exists:
            blockers.append(
                Blocker(code=RUN_VANISHED, request_id=run.request_id, run_id=run.run_id)
            )
        else:
            blockers.append(
                Blocker(
                    code=RUN_IN_FLIGHT, request_id=run.request_id, run_id=run.run_id
                )
            )
    return RepoAdmission(
        admission="blocked" if blockers else "ready",
        blockers=tuple(blockers),
    )
