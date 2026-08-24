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

import json
import os
import tempfile
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


class RunStoreError(Exception):
    """The store cannot honour the call (→ 422)."""


class LockBusyError(RunStoreError):
    """The repository already has a launch in flight (→ 409)."""


class _UnreadableLock(Exception):
    """A lock file exists but its content could not be parsed.

    Kept internal: every caller must decide fail-closed what to do about it,
    never inherit a silent "nobody holds it" default.
    """


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
        """
        existing = self.get(request_id)
        if existing is not None:
            return existing
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
            json.dump({"request_id": request_id, "pid": os.getpid()}, handle)
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
        lock = self._lock_path(key)
        try:
            holder = self._lock_holder(lock)
        except _UnreadableLock:
            raise LockBusyError(
                f"{key.as_text()}: lock file is unreadable; refusing to "
                f"release without confirming it is held by {request_id}"
            ) from None
        if holder is None:
            return  # nothing to release
        if holder != request_id:
            raise LockBusyError(
                f"{key.as_text()}: lock is held by {holder}, not {request_id}"
            )
        lock.unlink(missing_ok=True)

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
