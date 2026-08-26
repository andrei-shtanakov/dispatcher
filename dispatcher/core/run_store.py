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
import hashlib
import json
import os
import secrets
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
    #: The request's OWN repository string, verbatim — the raw identity
    #: dimension the fingerprint's canonical `repo_key` cannot carry into
    #: the replay branch (which by design runs before any resolution, so a
    #: changed repository must be detectable without re-resolving the
    #: checkout). Empty on records written before this field existed.
    repository: str = ""
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
        self._guards = state_dir / "guards"

    def _ensure(self) -> None:
        for path in (self._root, self._requests, self._locks, self._guards):
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
        return self._guards / ("__".join(safe_path_parts(key)) + ".guard")

    def read_lock(self, key: RepoKey) -> LockState:
        """The lock's `{request_id, fingerprint, created_at}`, or its state.

        `None` means free — no lock file exists. `Malformed` means the
        file was READ and its bytes are empty/invalid: an `O_EXCL` create
        raced with a crash before the write landed. The two must stay
        distinguishable (spec §8.1). Invalid UTF-8 is read as `Malformed`
        too — the bytes WERE seen, so damage is proven; a strict decode
        would crash with `UnicodeDecodeError` (a `ValueError`, invisible
        to callers catching `(RunStoreError, OSError)`) instead of
        classifying, which is how `release_malformed_lock` already reads
        the same file.
        """
        path = self._lock_path(key)
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as err:
            # An IO failure is NOT Malformed: the bytes were never seen,
            # so damage is not proven (spec §7's unreadable split).
            raise RunStoreError(f"cannot read lock for {key.as_text()}: {err}") from err
        try:
            return LockInfo.model_validate_json(data.decode(errors="replace"))
        except Exception:
            return Malformed(detail=f"unparseable lock ({len(data)} bytes)")

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
        """The current `LaunchRecord` for `request_id`, or `None` if unknown.

        `None` means PROVEN absence only. A file that exists but cannot be
        read or parsed raises: treating it as a free request_id would let
        the next reserve `os.replace` the broken bytes — destroying the
        evidence the unreadable-blocker points at — and adopt the id as
        fresh (fail-closed, same rule as everywhere else in this module).
        """
        try:
            raw = self._record_path(request_id).read_text(errors="replace")
        except FileNotFoundError:
            return None
        except OSError as err:
            raise RunStoreError(
                f"record for {request_id} exists but cannot be read: {err}"
            ) from err
        try:
            return LaunchRecord.model_validate_json(raw)
        except ValueError as err:
            raise RunStoreError(
                f"record for {request_id} exists but cannot be read (invalid content)"
            ) from err

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
        repository: str = "",
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
                repository=repository,
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
        repository: str = "",
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
            # A pre-B1 record has no stored fingerprint, but the identity
            # triple it is derived from IS persisted — derive it from the
            # record itself rather than conflicting every legacy retry.
            stored = existing.fingerprint or fingerprint_of(
                existing.repo_key, existing.work_id, existing.revision
            )
            if stored == fp:
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
            repository=repository,
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
            raw = lock.read_bytes()
        except OSError:
            return None
        try:
            # Same replace-decode as `read_lock`: invalid UTF-8 must become
            # _UnreadableLock (via json's ValueError), never a bare
            # UnicodeDecodeError escaping every caller's handling.
            return str(json.loads(raw.decode(errors="replace"))["request_id"])
        except (ValueError, KeyError, TypeError) as err:
            raise _UnreadableLock(str(lock)) from err

    def release_lock(self, key: RepoKey, request_id: str) -> None:
        """Release only the lock this request owns (spec §5.2.1).

        Runs inside `guard(key)` (spec §8.1: transition releases are
        lock-path mutations too): read-check-unlink is not
        compare-and-swap, so an unguarded release racing a fresh guarded
        reserve could pass its ownership check against the OLD lock and
        then unlink the NEW owner's. A caller that already holds the
        guard must call `_release_lock_locked` instead — `guard` is not
        reentrant. `GuardBusyError` propagates to the caller, bounded at
        `GUARD_TIMEOUT_SECONDS`.
        """
        with self.guard(key):
            self._release_lock_locked(key, request_id)

    def _release_lock_locked(self, key: RepoKey, request_id: str) -> None:
        """The critical section of `release_lock`; assumes `guard(key)`
        is held by the caller.

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

    def release_malformed_lock(self, key: RepoKey, *, actor: str, reason: str) -> dict:
        """Spec §8.3: quarantine is offered only where damage is PROVEN —
        the observed bytes are the damage. Runs inside the guard, and the
        file's identity is RE-CHECKED under it right before the move: the
        guard is advisory flock and excludes only cooperating writers, so
        proving the file the rename would move is still the file whose
        bytes were read cannot rest on the guard alone. The observed
        identity (inode, size, sha256) is recorded in the audit alongside
        the quarantined bytes.
        """
        with self.guard(key):
            path = self._lock_path(key)
            try:
                stat = path.stat()
                data = path.read_bytes()
            except FileNotFoundError:
                raise RunStoreError(f"{key.as_text()}: no lock to release") from None
            except OSError as err:
                raise RunStoreError(
                    f"{key.as_text()}: lock unreadable ({err}) — fix the "
                    "filesystem first; quarantining on a broken read could "
                    "destroy a healthy lock"
                ) from err
            try:
                LockInfo.model_validate_json(data.decode(errors="replace"))
            except Exception:
                pass
            else:
                raise RunStoreError(
                    f"{key.as_text()}: the lock parses — a healthy lock is "
                    "released only by its owning transitions"
                )
            released = path.parent / "released"
            released.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
            # token_hex: the timestamp alone has 1-second resolution, and a
            # second quarantine within the same second would silently
            # overwrite the previous quarantined bytes and their audit.
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
            name = f"{path.stem}.{stamp}.{secrets.token_hex(4)}.released"
            target = released / name
            audit = {
                "repo_key": key.as_text(),
                "actor": actor,
                "reason": reason,
                "at": datetime.now(UTC).isoformat(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "inode": stat.st_ino,
                "size": stat.st_size,
                "quarantined_as": name,
            }
            # The audit is MANDATORY for an administrative escape, so it is
            # persisted BEFORE the move: if it cannot be written, nothing
            # has changed and the caller gets a clean refusal — never a
            # released block whose actor/reason/time were lost to ENOSPC.
            # The inverse window — a crash between the audit write and the
            # rename — leaves an audit describing a quarantine that never
            # completed; honest and recoverable (the lock is still there,
            # a retry gets a fresh name).
            try:
                target.with_suffix(".audit.json").write_text(json.dumps(audit))
            except OSError as err:
                raise RunStoreError(
                    f"{key.as_text()}: cannot persist the quarantine audit "
                    f"({err}) — refusing to release without it"
                ) from err
            # Spec §8.3: re-check, under the guard and with NO filesystem
            # work between this check and the rename, that the pathname
            # still names the file that was read — an adversary outside
            # the advisory guard could have replaced it with a fresh
            # HEALTHY lock, and moving that would strip a live launch's
            # protection while the audit swears it moved old corrupt
            # bytes. (Cooperating writers are excluded by the guard; this
            # is defence against a non-cooperating one.)
            self._refuse_if_identity_changed(key, path, stat, target)
            os.rename(path, target)
            # Belt to the braces above: rename-by-pathname cannot be
            # atomically bound to the stat, so verify what actually moved.
            # A mismatch restores the file and refuses — the one shape
            # this cannot distinguish is an adversary writing byte-for-
            # byte identical damage, which is the same quarantine either
            # way.
            try:
                moved = target.read_bytes()
            except OSError:
                moved = None
            if moved != data:
                if not path.exists():
                    os.rename(target, path)
                    target.with_suffix(".audit.json").unlink(missing_ok=True)
                raise RunStoreError(
                    f"{key.as_text()}: lock identity changed during "
                    "quarantine — the moved file was restored, nothing "
                    "was released"
                )
            return audit

    @staticmethod
    def _refuse_if_identity_changed(
        key: RepoKey, path: Path, observed: os.stat_result, target: Path
    ) -> None:
        """Refuse (and drop the pre-written audit) unless `path` still
        names the exact file `observed` described."""
        try:
            now = path.stat()
        except OSError as err:
            target.with_suffix(".audit.json").unlink(missing_ok=True)
            raise RunStoreError(
                f"{key.as_text()}: lock identity changed since it was "
                f"read ({err}) — refusing to quarantine"
            ) from err
        if (now.st_dev, now.st_ino, now.st_size, now.st_mtime_ns) != (
            observed.st_dev,
            observed.st_ino,
            observed.st_size,
            observed.st_mtime_ns,
        ):
            target.with_suffix(".audit.json").unlink(missing_ok=True)
            raise RunStoreError(
                f"{key.as_text()}: lock identity changed since it was "
                "read — refusing to quarantine"
            )

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
        """The launch is no longer in flight, so the lock is released here
        — transition AND release inside one guarded section: a contended
        guard fails the WHOLE transition (`GuardBusyError`, no partial
        state) instead of persisting the new state and then abandoning a
        healthy lock that no escape can release (the malformed-lock escape
        correctly refuses a lock that parses)."""
        return self._transition_and_release(
            request_id, state="materialized", run_id=run_id
        )

    def mark_unknown(self, request_id: str, reason: str) -> LaunchRecord:
        """The lock is deliberately NOT released (spec §5.2.1)."""
        return self._transition(request_id, state="launch_unknown", reason=reason)

    def mark_terminal(self, request_id: str, outcome: str) -> LaunchRecord:
        """The run finished; like `mark_materialized`, this releases the
        lock — same one-guarded-section rule, same reason."""
        return self._transition_and_release(
            request_id, state="terminal", outcome=outcome
        )

    def _transition_and_release(
        self, request_id: str, **fields: object
    ) -> LaunchRecord:
        """One guarded section for the transitions that free the lock.

        The record is read first only to derive the RepoKey; the
        transition itself re-reads under the guard, so the persisted
        state and the release are atomic w.r.t. every other lock-path
        mutation. The remaining window — a crash between the write and
        the unlink — is the documented one in `mark_admission_rejected`'s
        docstring (PR-C reconciliation)."""
        record = self.get(request_id)
        if record is None:
            raise RunStoreError(f"no launch record for {request_id}")
        with self.guard(self._key_of(record)):
            updated = self._transition(request_id, **fields)
            self._release_for_locked(updated)
        return updated

    def mark_admission_rejected(
        self, request_id: str, *, code: str, detail: str, current: dict
    ) -> LaunchRecord:
        """Terminalize a refused attempt and release its lock.

        Called only from inside submit's guarded admission section, so the
        release uses `_release_for_locked` (`guard` is not reentrant).

        Two crash windows this shape cannot close (TODO: PR-C's
        reconciliation — "healthy lock whose owning record is terminal →
        release", inside the guard — is the doorway for both):
        a crash between `_reserve_locked` and this call leaves a
        `reserved` record whose lock is held until a retry of the same
        request_id; a crash between the record write below and the unlink
        after it leaves a TERMINAL record with a standing healthy lock,
        which no current escape releases (`release_malformed_lock`
        correctly refuses a lock that parses).
        """
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
        self._release_for_locked(record)
        return record

    def mark_vanished_acknowledged(
        self, request_id: str, *, actor: str, reason: str, prior_run_id: str
    ) -> LaunchRecord:
        """Terminalize the audited `acknowledge-vanished` escape (spec
        §8.3). Mirrors `mark_admission_rejected` — including its
        already-guarded release and its crash windows (see there): the
        caller (`acknowledge_vanished`) holds the guard. This specific
        fact — someone administratively released a fail-closed block on a
        run whose absence was never proven, only observed — is recorded,
        not silently absorbed.
        """
        existing = self.get(request_id)
        if existing is None:
            raise RunStoreError(f"no launch record for {request_id}")
        # The release below refuses a lock this record does not own (a
        # healthy lock held by a DIFFERENT request_id can stand for the
        # same RepoKey). Prove the release CAN happen before persisting
        # the irreversible terminal tombstone — otherwise the caller
        # reports the refusal while the mutation silently applied. Safe
        # against interleaving: the caller holds the guard, and every
        # other lock mutation takes it too.
        key = self._key_of(existing)
        state = self.read_lock(key)
        if isinstance(state, Malformed):
            raise LockBusyError(
                f"{key.as_text()}: lock file is malformed; release it via "
                "the malformed-lock escape before acknowledging"
            )
        if isinstance(state, LockInfo) and state.request_id != request_id:
            raise LockBusyError(
                f"{key.as_text()}: lock is held by {state.request_id}, not {request_id}"
            )
        record = self._transition(
            request_id,
            state="terminal",
            outcome="vanished-acknowledged",
            ack_actor=actor,
            ack_at=datetime.now(UTC).isoformat(),
            ack_reason=reason,
            prior_run_id=prior_run_id,
        )
        self._release_for_locked(record)
        return record

    def _release_for_locked(self, record: LaunchRecord) -> None:
        """Release `record`'s lock for a caller already INSIDE `guard(key)` —
        `guard` is not reentrant, so re-taking it here would self-block
        for `GUARD_TIMEOUT_SECONDS` and die `GuardBusy` on every
        admission rejection and tombstone."""
        self._release_lock_locked(self._key_of(record), record.request_id)

    @staticmethod
    def _key_of(record: LaunchRecord) -> RepoKey:
        parts = record.repo_key.split("/")
        if parts[0] == "_local":
            return RepoKey(host="", owner="", repo=parts[1], local=True)
        return RepoKey(host=parts[0], owner=parts[1], repo=parts[2])

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
        "nothing non-terminal here" off exactly the broken input. The
        same rule covers the enumeration itself: a `requests/` directory
        that cannot be iterated at all is returned AS an unreadable
        entry, never as "no records" — `([], [])` here would silently
        drop every record-derived blocker at once. Ordered by filename
        (== `request_id`) for determinism — callers sort by their own
        keys.
        """
        records: list[LaunchRecord] = []
        unreadable: list[str] = []
        try:
            # os.scandir, not Path.glob: glob SUPPRESSES OSError raised
            # while scanning (explicitly on 3.13+, and pathlib's matcher
            # historically swallowed iteration errors too) — the except
            # below would be dead code and an unlistable directory would
            # read as an empty store, the exact fail-open this method's
            # contract forbids.
            with os.scandir(self._requests) as entries:
                paths = sorted(
                    self._requests / e.name for e in entries if e.name.endswith(".json")
                )
        except FileNotFoundError:
            return [], []
        except OSError as err:
            return [], [f"requests directory unlistable: {err}"]
        for path in paths:
            try:
                records.append(LaunchRecord.model_validate_json(path.read_text()))
            except Exception:
                unreadable.append(path.name)
        return records, unreadable
