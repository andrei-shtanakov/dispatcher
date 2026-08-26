"""Durable launch records and the per-RepoKey lock (spec §5.2, §5.4).

Two facts live here and nowhere else: what dispatcher asked for, and how far
that ask got. Everything about the run itself is maestro's and is read from
maestro's store (spec §3.2).

The lock is a file on purpose. maestro does not stop two concurrent CLI runs
of one repository — its `RunIsLive` guard fires only for a run classified
`running`, which needs a holder file that only the service tick writes
(`maestro/maestro/service/tick.py:133`) — so this lock is the only thing
between slice 0 and two agent-driven runs mutating one checkout.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from dispatcher.core.run_identity import RepoKey, safe_path_parts

LaunchState = Literal[
    "reserved", "launching", "materialized", "terminal", "launch_unknown"
]

#: States in which the launch is still in flight and the lock must be held.
_LOCK_HELD_STATES = frozenset({"reserved", "launching", "launch_unknown"})

_DIR_MODE = 0o700
_FILE_MODE = 0o600

#: Bound on how long `RunStore.guard()` will wait for the critical section
#: (spec §8.1). A live hung handler must surface as `GuardBusyError`, never
#: as an indefinite wait.
GUARD_TIMEOUT_SECONDS = 2.0


class RunStoreError(Exception):
    """The store cannot honour the call (→ 422)."""


class LockBusyError(RunStoreError):
    """The repository already has a launch in flight (→ 409)."""


class GuardBusyError(RunStoreError):
    """The recovery critical section stayed held past its bound (spec §8.1)."""


class FingerprintMismatch(RunStoreError):
    """A reused `request_id` named a different attempt (spec §8.2).

    Idempotent replay is only safe for the SAME attempt: a fingerprint
    mismatch means the caller reused an id across two different
    `(repo_key, work_id, revision)` triples, and returning the stale
    record would silently adopt another attempt's receipt.
    """


class _UnreadableLock(Exception):
    """A lock file exists but its content could not be parsed.

    Kept internal: every caller must decide fail-closed what to do about it,
    never inherit a silent "nobody holds it" default.
    """


@dataclass(frozen=True)
class Malformed:
    """The lock file was READ and is empty/invalid — damage is proven.

    Distinct from an IO error by design (spec §7): quarantine may be
    offered only where the observed bytes themselves are the damage.
    """

    detail: str


def fingerprint_of(repo_key: str, work_id: str, revision: str) -> str:
    """One attempt's identity (spec §8.2) — order fixed, joined verbatim."""
    return "|".join((repo_key, work_id, revision))


class LockInfo(BaseModel):
    """The lock file's own payload — a preflight record, not just a marker.

    Written to the same fd right after `O_EXCL` succeeds (spec §8.1): "a
    lock with no owning fact" must not be a representable steady state.
    """

    request_id: str
    fingerprint: str = ""
    created_at: str = ""


#: `read_lock`'s result: a healthy lock, a proven-damaged one, or none at
#: all. `None` and `Malformed` must stay distinguishable — `None` means
#: free, `Malformed` means fail-closed blocked pending an audited escape.
LockState = LockInfo | Malformed | None


class LaunchRecord(BaseModel):
    """dispatcher's request, plus how far it got (spec §5.2).

    Spec §3.1: "the join between the five identities lives in the
    RunRequest record" — `work_id`/`revision`/`tasks`/the two ref fields
    ARE that record, persisted at `reserve()` time (I3) rather than
    validated and dropped. Defaulting to `""`/`None` keeps a record written
    before this field existed readable.
    """

    request_id: str
    repo_key: str
    state: LaunchState
    run_id: str | None = None
    reason: str | None = None
    #: `runs/` as it looked immediately before the launch — the only thing an
    #: orphan can be correlated against (spec §5.2.1).
    known_runs: list[str] = Field(default_factory=list)
    window_start: str = ""
    outcome: str | None = None
    work_id: str = ""
    revision: str = ""
    tasks: str = ""
    spec_ref_path: str | None = None
    spec_commit: str | None = None
    plan_ref_path: str | None = None
    plan_commit: str | None = None
    #: The checkout `submit` resolved and launched in, persisted so a later
    #: verb can run maestro from the SAME repository instead of inheriting
    #: the server process's cwd. maestro derives a run's repository from the
    #: directory it is standing in, so a verb run from anywhere else asks
    #: about the wrong repository and reports the run as missing. Stored as
    #: text rather than re-derived from `repo_key` because a `RepoKey` names
    #: an identity, not a location, and the same identity can be checked out
    #: more than once. Empty on records written before this field existed —
    #: callers must refuse rather than fall back to the process cwd, which
    #: is precisely the bug.
    checkout: str = ""
    #: This attempt's identity (spec §8.2) — persisted so a
    #: fingerprint-matching repeat replays the original 409 semantically,
    #: with zero re-classification.
    fingerprint: str = ""
    response_class: str | None = None
    admission_code: str | None = None
    admission_detail: str | None = None
    admission_current: dict | None = None
    rejected_at: str | None = None
    #: The audited `acknowledge-vanished` escape (spec §8.3): who attested
    #: the run was gone, when, why, and which run_id this record HELD
    #: before the tombstone — an administrative release of a fail-closed
    #: block must record the risk taken, not silently absorb it.
    ack_actor: str | None = None
    ack_at: str | None = None
    ack_reason: str | None = None
    prior_run_id: str | None = None


class RunStore:
    """Crash-safe records under `<state_dir>/requests`, locks under `locks`."""

    def __init__(self, state_dir: Path) -> None:
        self._root = state_dir
        self._requests = state_dir / "requests"
        self._locks = state_dir / "locks"

    def _ensure(self) -> None:
        for path in (self._root, self._requests, self._locks):
            path.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)

    def _record_path(self, request_id: str) -> Path:
        # request_id reaches this off the wire; it must never shape a path.
        safe = "".join(c for c in request_id if c.isalnum() or c in "-_")
        if not safe or safe != request_id:
            # Not LockBusyError: nothing is in flight, the input is bad.
            raise RunStoreError(f"unsafe request_id: {request_id!r}")
        return self._requests / f"{safe}.json"

    def _lock_path(self, key: RepoKey) -> Path:
        slug = "-".join(safe_path_parts(key)).replace("/", "-")
        return self._locks / f"{slug}.lock"

    def _guard_path(self, key: RepoKey) -> Path:
        return self._root / "guards" / ("__".join(safe_path_parts(key)) + ".guard")

    def read_lock(self, key: RepoKey) -> LockState:
        """The lock's `{request_id, fingerprint, created_at}`, or its state.

        `None` means free — no lock file exists. `Malformed` means the
        file was READ and its bytes are empty/invalid: an `O_EXCL` create
        raced with a crash before the write landed. The two must stay
        distinguishable (spec §8.1).
        """
        path = self._lock_path(key)
        try:
            text = path.read_text()
        except FileNotFoundError:
            return None
        except OSError as err:
            # An IO failure is NOT Malformed: the bytes were never seen,
            # so damage is not proven (spec §7's unreadable split).
            raise RunStoreError(f"cannot read lock for {key.as_text()}: {err}") from err
        try:
            return LockInfo.model_validate_json(text)
        except Exception:
            return Malformed(detail=f"unparseable lock ({len(text)} bytes)")

    @contextmanager
    def guard(self, key: RepoKey) -> Iterator[None]:
        """Per-RepoKey critical section for EVERY lock-path mutation.

        Checking a lock file and later acting on its pathname is not
        compare-and-swap (spec §8.1): between "saw it malformed" and
        "renamed it", another actor may have quarantined it and a fresh
        submit created a healthy lock at the same path. An OS advisory
        lock is used because a crash releases it automatically — the
        guard adds no orphan state of its own. Acquisition is BOUNDED:
        a live hung handler must surface as guard_busy, never as an
        indefinite wait.

        Not reentrant: a caller that already holds the guard for `key`
        must not call this again before releasing it — a second `flock`
        on another fd of the same file blocks even within one process.
        """
        path = self._guard_path(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, _FILE_MODE)
        try:
            deadline = time.monotonic() + GUARD_TIMEOUT_SECONDS
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise GuardBusyError(
                            f"{key.as_text()}: the lock-recovery section has "
                            f"been held past {GUARD_TIMEOUT_SECONDS}s — a "
                            "hung handler; retry, and if this persists "
                            "inspect the dispatcher process"
                        ) from None
                    time.sleep(0.05)
            yield
        finally:
            os.close(fd)  # closing the fd releases the flock

    def get(self, request_id: str) -> LaunchRecord | None:
        """The current `LaunchRecord` for `request_id`, or `None` if unknown."""
        try:
            raw = self._record_path(request_id).read_text()
        except OSError:
            return None
        try:
            return LaunchRecord.model_validate_json(raw)
        except ValueError:
            return None

    def _write(self, record: LaunchRecord) -> None:
        """Temp-then-rename: a half-written record must never be readable."""
        self._ensure()
        target = self._record_path(record.request_id)
        fd, tmp = tempfile.mkstemp(dir=str(self._requests), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(record.model_dump_json(indent=2))
            os.chmod(tmp, _FILE_MODE)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def reserve(
        self,
        request_id: str,
        key: RepoKey,
        *,
        known_runs: list[str],
        window_start: str,
        work_id: str = "",
        revision: str = "",
        tasks: str = "",
        checkout: str = "",
        spec_ref_path: str | None = None,
        spec_commit: str | None = None,
        plan_ref_path: str | None = None,
        plan_commit: str | None = None,
    ) -> LaunchRecord:
        """Take the lock and write the record BEFORE any process starts.

        A repeated `request_id` returns its existing record and never starts a
        second launch (spec §5.2). The request-body kwargs persist the join
        spec §3.1 says lives here (I3); they default to empty/`None` so a
        caller with nothing to add (most of this module's own tests) still
        works unchanged.

        Runs entirely inside `guard(key)` (spec §8.1): checking the lock
        path and then acting on it is not compare-and-swap, so every
        lock-path mutation belongs in the same critical section another
        actor's recovery uses. The section itself lives in
        `_reserve_locked` — kept separate so a caller that already holds
        the guard (`submit`, which reserves as part of its own guarded
        admission) can call `_reserve_locked` directly instead of
        re-entering `guard`, which is not reentrant and would self-block
        for `GUARD_TIMEOUT_SECONDS` before dying `GuardBusy`.
        """
        with self.guard(key):
            return self._reserve_locked(
                request_id,
                key,
                known_runs=known_runs,
                window_start=window_start,
                work_id=work_id,
                revision=revision,
                tasks=tasks,
                checkout=checkout,
                spec_ref_path=spec_ref_path,
                spec_commit=spec_commit,
                plan_ref_path=plan_ref_path,
                plan_commit=plan_commit,
            )

    def _reserve_locked(
        self,
        request_id: str,
        key: RepoKey,
        *,
        known_runs: list[str],
        window_start: str,
        work_id: str = "",
        revision: str = "",
        tasks: str = "",
        checkout: str = "",
        spec_ref_path: str | None = None,
        spec_commit: str | None = None,
        plan_ref_path: str | None = None,
        plan_commit: str | None = None,
    ) -> LaunchRecord:
        """The critical section `reserve` runs inside `guard(key)`.

        Call this directly, instead of `reserve`, only from a caller that
        already holds the guard for `key` — `guard` is not reentrant.
        """
        fp = fingerprint_of(key.as_text(), work_id, revision)
        existing = self.get(request_id)
        if existing is not None:
            if existing.fingerprint == fp:
                return existing
            raise FingerprintMismatch(
                f"{request_id} was already used for a different attempt"
            )
        self._ensure()
        lock = self._lock_path(key)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _FILE_MODE)
        except FileExistsError:
            try:
                holder = self._lock_holder(lock)
            except _UnreadableLock:
                holder = None
            raise LockBusyError(
                f"{key.as_text()}: a launch is already in flight "
                f"(held by {holder or 'an unreadable lock file'}); "
                "refused rather than queued — a queue lengthens the ambiguity "
                "window instead of closing it"
            ) from None
        with os.fdopen(fd, "w") as handle:
            payload = {
                "request_id": request_id,
                "fingerprint": fp,
                "created_at": window_start,
                "pid": os.getpid(),
            }
            handle.write(json.dumps(payload))
            handle.flush()
            os.fsync(handle.fileno())
        record = LaunchRecord(
            request_id=request_id,
            repo_key=key.as_text(),
            state="reserved",
            known_runs=known_runs,
            window_start=window_start,
            work_id=work_id,
            revision=revision,
            tasks=tasks,
            spec_ref_path=spec_ref_path,
            spec_commit=spec_commit,
            plan_ref_path=plan_ref_path,
            plan_commit=plan_commit,
            checkout=checkout,
            fingerprint=fp,
        )
        try:
            self._write(record)
        except OSError as err:
            # I5: unlike `release_lock`, which must refuse a lock it cannot
            # confirm it holds, `reserve` created THIS lock microseconds ago
            # under `O_EXCL` and knows exactly that it owns it — so a torn
            # record write here must not leave the lock standing forever
            # with no record behind it (a permanent, unexplained
            # `LockBusyError` for every future request_id). Unlinked
            # directly rather than through `release_lock`, since a lock
            # file this fresh cannot yet be the "unreadable, refuse"
            # case that method exists to guard.
            lock.unlink(missing_ok=True)
            raise RunStoreError(
                f"reserved {key.as_text()} but could not write its launch "
                f"record; the lock has been released so a retry is not "
                f"blocked forever: {err}"
            ) from err
        return record

    def _lock_holder(self, lock: Path) -> str | None:
        """The `request_id` in a lock file; `None` if no lock file exists.

        Raises `_UnreadableLock` if the file exists but its content cannot be
        parsed — a torn write (`json.dump` here is not temp-then-rename) must
        never read as "nobody holds it".
        """
        try:
            raw = lock.read_text()
        except OSError:
            return None
        try:
            return str(json.loads(raw)["request_id"])
        except (ValueError, KeyError, TypeError) as err:
            raise _UnreadableLock(str(lock)) from err

    def release_lock(self, key: RepoKey, request_id: str) -> None:
        """Release only the lock this request owns (spec §5.2.1).

        A lock file that exists but cannot be parsed is refused, not treated
        as free: `reserve()` is already conservative about this same
        condition (unreadable → busy), and a release path that guesses
        "nobody holds it" would let any caller delete a lock it never took —
        the one instrument standing between two agent-driven runs and one
        checkout failing open in exactly the state it exists to cover.
        """
        state = self.read_lock(key)
        if state is None:
            return  # nothing to release
        if isinstance(state, Malformed):
            raise LockBusyError(
                f"{key.as_text()}: lock file is unreadable; refusing to "
                f"release without confirming it is held by {request_id}"
            )
        if state.request_id != request_id:
            raise LockBusyError(
                f"{key.as_text()}: lock is held by {state.request_id}, not {request_id}"
            )
        self._lock_path(key).unlink(missing_ok=True)

    def _transition(self, request_id: str, **fields: object) -> LaunchRecord:
        record = self.get(request_id)
        if record is None:
            raise RunStoreError(f"no launch record for {request_id}")
        updated = record.model_copy(update=fields)
        self._write(updated)
        return updated

    def mark_launching(self, request_id: str) -> LaunchRecord:
        """The launch process has started; the lock stays held."""
        return self._transition(request_id, state="launching")

    def mark_materialized(self, request_id: str, run_id: str) -> LaunchRecord:
        """The launch is no longer in flight, so the lock is released here."""
        record = self._transition(request_id, state="materialized", run_id=run_id)
        self._release_for(record)
        return record

    def mark_unknown(self, request_id: str, reason: str) -> LaunchRecord:
        """The lock is deliberately NOT released (spec §5.2.1)."""
        return self._transition(request_id, state="launch_unknown", reason=reason)

    def mark_terminal(self, request_id: str, outcome: str) -> LaunchRecord:
        """The run finished; like `mark_materialized`, this releases the lock."""
        record = self._transition(request_id, state="terminal", outcome=outcome)
        self._release_for(record)
        return record

    def mark_admission_rejected(
        self, request_id: str, *, code: str, detail: str, current: dict
    ) -> LaunchRecord:
        """Terminalize a refused attempt; the lock never outlives the fact."""
        record = self._transition(
            request_id,
            state="terminal",
            outcome="admission-rejected",
            response_class="admission_rejected",
            admission_code=code,
            admission_detail=detail,
            admission_current=current,
            rejected_at=datetime.now(UTC).isoformat(),
        )
        self._release_for(record)
        return record

    def mark_vanished_acknowledged(
        self, request_id: str, *, actor: str, reason: str, prior_run_id: str
    ) -> LaunchRecord:
        """Terminalize the audited `acknowledge-vanished` escape (spec
        §8.3). Mirrors `mark_admission_rejected`: the lock never outlives
        the fact, and this specific fact — someone administratively
        released a fail-closed block on a run whose absence was never
        proven, only observed — is recorded, not silently absorbed.
        """
        record = self._transition(
            request_id,
            state="terminal",
            outcome="vanished-acknowledged",
            ack_actor=actor,
            ack_at=datetime.now(UTC).isoformat(),
            ack_reason=reason,
            prior_run_id=prior_run_id,
        )
        self._release_for(record)
        return record

    def _release_for(self, record: LaunchRecord) -> None:
        parts = record.repo_key.split("/")
        key = (
            RepoKey(host="", owner="", repo=parts[1], local=True)
            if parts[0] == "_local"
            else RepoKey(host=parts[0], owner=parts[1], repo=parts[2])
        )
        self.release_lock(key, record.request_id)

    def holds_lock(self, key: RepoKey) -> str | None:
        """The request_id currently holding this repo's launch lock, if any.

        Best-effort only: unlike `release_lock`, a query has no mutation to
        guard, so an unreadable lock file is reported the same as no lock
        rather than raising.
        """
        try:
            return self._lock_holder(self._lock_path(key))
        except _UnreadableLock:
            return None

    def list(self) -> tuple[list[LaunchRecord], list[str]]:
        """Every record, plus the FILENAMES that failed to parse.

        The second return exists because the single-live-run gate is
        fail-closed: a corrupt record must block as unknown, and a
        listing that silently dropped it would let the gate read
        "nothing non-terminal here" off exactly the broken input. Ordered
        by filename (== `request_id`) for determinism — callers sort by
        their own keys.
        """
        records: list[LaunchRecord] = []
        unreadable: list[str] = []
        try:
            paths = sorted(self._requests.glob("*.json"))
        except OSError:
            return [], []
        for path in paths:
            try:
                records.append(LaunchRecord.model_validate_json(path.read_text()))
            except Exception:
                unreadable.append(path.name)
        return records, unreadable
