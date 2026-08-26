"""Durable launch records and the per-RepoKey lock (spec §5.2, §5.4)."""

import hashlib
import json
import multiprocessing
import time
from pathlib import Path

import pytest

from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_store import (
    GUARD_TIMEOUT_SECONDS,
    FingerprintMismatch,
    GuardBusyError,
    LockBusyError,
    LockInfo,
    Malformed,
    RunStore,
    RunStoreError,
    fingerprint_of,
)

_KEY = RepoKey(host="github.com", owner="owner", repo="deployer")
_REQ = "11111111-1111-4111-8111-111111111111"
_OTHER = "22222222-2222-4222-8222-222222222222"


def _store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "state")


def test_reserve_writes_a_durable_record_before_anything_starts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    record = store.reserve(
        _REQ, _KEY, known_runs=["01AAA"], window_start="2026-08-22T00:00:00Z"
    )
    assert record.state == "reserved"
    stored = store.get(_REQ)
    assert stored is not None
    assert stored.known_runs == ["01AAA"]


def test_second_request_for_a_busy_repo_is_refused_not_queued(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    with pytest.raises(LockBusyError, match="deployer"):
        store.reserve(_OTHER, _KEY, known_runs=[], window_start="t")


def test_lock_survives_a_new_store_instance(tmp_path: Path) -> None:
    """A process-local lock is released by exactly the restart that creates
    the problem the lock exists to prevent (spec §5.4)."""
    _store(tmp_path).reserve(_REQ, _KEY, known_runs=[], window_start="t")
    with pytest.raises(LockBusyError):
        _store(tmp_path).reserve(_OTHER, _KEY, known_runs=[], window_start="t")


def test_materializing_releases_the_lock(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_materialized(_REQ, "01BBB")
    stored = store.get(_REQ)
    assert stored is not None and stored.run_id == "01BBB"
    store.reserve(_OTHER, _KEY, known_runs=[], window_start="t")  # no raise


def test_launch_unknown_keeps_the_lock(tmp_path: Path) -> None:
    """Dropping the lock on uncertainty would let the next request launch a
    second run into the same tree (spec §5.2.1)."""
    store = _store(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_unknown(_REQ, "no run appeared within the timeout")
    stored = store.get(_REQ)
    assert stored is not None and stored.state == "launch_unknown"
    with pytest.raises(LockBusyError):
        store.reserve(_OTHER, _KEY, known_runs=[], window_start="t")


def test_release_lock_requires_the_owning_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    with pytest.raises(LockBusyError, match="held by"):
        store.release_lock(_KEY, _OTHER)
    store.release_lock(_KEY, _REQ)
    store.reserve(_OTHER, _KEY, known_runs=[], window_start="t")


def test_a_repeated_request_id_returns_the_existing_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    again = store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    assert again.state == first.state
    assert again.request_id == first.request_id


def test_terminal_releases_the_lock(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_terminal(_REQ, "success")
    stored = store.get(_REQ)
    assert stored is not None and stored.outcome == "success"
    store.reserve(_OTHER, _KEY, known_runs=[], window_start="t")  # no raise


def test_lock_held_states_matches_actual_lock_presence(tmp_path: Path) -> None:
    """`_LOCK_HELD_STATES` names the states in which the lock must still be
    held, but nothing asserted it against real transitions (dead code: the
    plan called for a test here, not deletion). Walks every transition and
    checks lock presence against membership for each."""
    from dispatcher.core.run_store import _LOCK_HELD_STATES

    launching_key = RepoKey(host="github.com", owner="owner", repo="launching")
    store = _store(tmp_path)

    record = store.reserve(_REQ, launching_key, known_runs=[], window_start="t")
    assert (store.holds_lock(launching_key) is not None) == (
        record.state in _LOCK_HELD_STATES
    )

    record = store.mark_launching(_REQ)
    assert (store.holds_lock(launching_key) is not None) == (
        record.state in _LOCK_HELD_STATES
    )

    record = store.mark_unknown(_REQ, "no run appeared within the timeout")
    assert (store.holds_lock(launching_key) is not None) == (
        record.state in _LOCK_HELD_STATES
    )

    materialized_key = RepoKey(host="github.com", owner="owner", repo="materialized")
    record = store.reserve(_OTHER, materialized_key, known_runs=[], window_start="t")
    record = store.mark_materialized(_OTHER, "01AAA")
    assert (store.holds_lock(materialized_key) is not None) == (
        record.state in _LOCK_HELD_STATES
    )

    terminal_req = "33333333-3333-4333-8333-333333333333"
    terminal_key = RepoKey(host="github.com", owner="owner", repo="terminal")
    record = store.reserve(terminal_req, terminal_key, known_runs=[], window_start="t")
    record = store.mark_terminal(terminal_req, "cancelled")
    assert (store.holds_lock(terminal_key) is not None) == (
        record.state in _LOCK_HELD_STATES
    )


def test_reserve_releases_the_lock_it_just_took_if_the_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I5: unlike `release_lock`, which must refuse a lock it cannot confirm
    it holds, `reserve` created THIS lock microseconds earlier under
    `O_EXCL` and knows exactly that it owns it. A torn record write must not
    leave that lock standing forever with no record behind it — a permanent
    `LockBusyError` no retry could ever clear."""
    store = _store(tmp_path)

    def _broken_write(self: RunStore, record: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(RunStore, "_write", _broken_write)
    with pytest.raises(RunStoreError, match="lock has been released"):
        store.reserve(_REQ, _KEY, known_runs=[], window_start="t")

    monkeypatch.undo()
    assert store.get(_REQ) is None, "no record should exist after a torn write"
    store.reserve(_OTHER, _KEY, known_runs=[], window_start="t")  # lock is free


def test_release_lock_refuses_a_corrupt_lock_file_rather_than_freeing_it(
    tmp_path: Path,
) -> None:
    """A torn lock write must not read as "nobody holds it": `reserve()` is
    already conservative about an unreadable lock (unreadable → busy), and a
    release path that guesses "free" would let any caller delete a lock it
    never took (spec §5.4)."""
    store = _store(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    lock_path = next((tmp_path / "state" / "locks").glob("*.lock"))
    lock_path.write_text("not valid json")
    with pytest.raises(LockBusyError):
        store.release_lock(_KEY, _REQ)
    assert lock_path.exists()


def _hold_guard(state_dir: str, hold_seconds: float, acquired) -> None:
    store = RunStore(Path(state_dir))
    with store.guard(_KEY):
        acquired.set()
        time.sleep(hold_seconds)


def test_guard_is_exclusive_across_processes_and_bounded(tmp_path: Path) -> None:
    """A held guard makes a second acquirer wait, then fail GuardBusy.

    A separate PROCESS on purpose: fcntl locks do not exclude within one
    process, so a thread-based test would pass against a broken guard.
    """
    acquired = multiprocessing.Event()
    holder = multiprocessing.Process(
        target=_hold_guard, args=(str(tmp_path), GUARD_TIMEOUT_SECONDS + 2, acquired)
    )
    holder.start()
    try:
        assert acquired.wait(timeout=10), "holder never acquired"
        store = RunStore(tmp_path)
        started = time.monotonic()
        with pytest.raises(GuardBusyError):
            with store.guard(_KEY):
                pass
        waited = time.monotonic() - started
        assert waited >= GUARD_TIMEOUT_SECONDS * 0.9, "gave up before the bound"
        assert waited < GUARD_TIMEOUT_SECONDS + 1.5, "waited far past the bound"
    finally:
        holder.terminate()
        holder.join()


def test_guard_releases_on_exit_and_after_crash(tmp_path: Path) -> None:
    """Sequential sections work; a killed holder frees the guard by itself."""
    store = RunStore(tmp_path)
    with store.guard(_KEY):
        pass
    with store.guard(_KEY):  # would deadlock if exit leaked the flock
        pass

    acquired = multiprocessing.Event()
    holder = multiprocessing.Process(
        target=_hold_guard, args=(str(tmp_path), 60, acquired)
    )
    holder.start()
    assert acquired.wait(timeout=10)
    holder.kill()  # crash, not clean exit
    holder.join()
    with store.guard(_KEY):  # the OS released the advisory lock
        pass


def test_lock_file_is_a_preflight_record(tmp_path: Path) -> None:
    """The lock itself carries {request_id, fingerprint, created_at}.

    Spec §8.1: "a lock with no owning fact" must not be a representable
    steady state — the fact travels IN the lock, written to the same fd
    right after O_EXCL.
    """
    store = RunStore(tmp_path)
    store.reserve(
        "rc-aaaaaaaa-11111111",
        _KEY,
        known_runs=[],
        window_start="T0",
        work_id="todo://deployer/x",
        revision="a" * 40,
    )
    info = store.read_lock(_KEY)
    assert info is not None and not isinstance(info, Malformed)
    assert info.request_id == "rc-aaaaaaaa-11111111"
    assert info.fingerprint == fingerprint_of(
        _KEY.as_text(), "todo://deployer/x", "a" * 40
    )
    assert info.created_at


def test_an_empty_lock_reads_as_malformed_not_as_absent(tmp_path: Path) -> None:
    """The §8.1 crash residue: O_EXCL succeeded, the write never happened.

    Malformed and absent MUST be distinguishable — absent means free,
    malformed means fail-closed blocked with an audited escape.
    """
    store = RunStore(tmp_path)
    lock = store._lock_path(_KEY)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    state = store.read_lock(_KEY)
    assert isinstance(state, Malformed)
    assert store.read_lock(RepoKey(host="github.com", owner="o", repo="other")) is None


def test_reserve_replays_only_a_matching_fingerprint(tmp_path: Path) -> None:
    """Same request_id + same attempt → prior record; different attempt →
    FingerprintMismatch. A reused id must not adopt another attempt's
    receipt (spec §8.2)."""
    store = RunStore(tmp_path)
    first = store.reserve(
        "rc-aaaaaaaa-11111111",
        _KEY,
        known_runs=[],
        window_start="T0",
        work_id="w1",
        revision="a" * 40,
    )
    again = store.reserve(
        "rc-aaaaaaaa-11111111",
        _KEY,
        known_runs=[],
        window_start="T9",
        work_id="w1",
        revision="a" * 40,
    )
    assert again == first
    with pytest.raises(FingerprintMismatch):
        store.reserve(
            "rc-aaaaaaaa-11111111",
            _KEY,
            known_runs=[],
            window_start="T9",
            work_id="OTHER",
            revision="a" * 40,
        )


def test_admission_rejection_is_terminal_and_reproducible(tmp_path: Path) -> None:
    """The rejection persists an immutable payload (spec §8.2): a repeat
    must replay the original decision after the workspace moved on —
    re-classification could even PASS where the original failed."""
    store = RunStore(tmp_path)
    store.reserve(
        "rc-aaaaaaaa-11111111",
        _KEY,
        known_runs=[],
        window_start="T0",
        work_id="w1",
        revision="a" * 40,
    )
    rec = store.mark_admission_rejected(
        "rc-aaaaaaaa-11111111",
        code="run_in_flight",
        detail="run 01X has no terminal outcome",
        current={"run_id": "01X", "run_status": "interrupted"},
    )
    assert rec.state == "terminal"
    assert rec.outcome == "admission-rejected"
    assert rec.response_class == "admission_rejected"
    assert rec.admission_code == "run_in_flight"
    assert rec.admission_current == {"run_id": "01X", "run_status": "interrupted"}
    assert rec.rejected_at
    # and the lock is free again for the next attempt
    assert store.read_lock(_KEY) is None


def test_a_legacy_record_without_new_fields_still_loads(tmp_path: Path) -> None:
    """Migration is defaults, not a rewrite: pre-B1 records must read."""
    store = RunStore(tmp_path)
    path = tmp_path / "requests" / "rc-legacy00-00000000.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "request_id": "rc-legacy00-00000000",
                "repo_key": "github.com/owner/deployer",
                "state": "materialized",
                "run_id": "01OLD",
            }
        )
    )
    rec = store.get("rc-legacy00-00000000")
    assert rec is not None and rec.fingerprint == "" and rec.response_class is None
    assert rec.ack_actor is None and rec.prior_run_id is None


def test_list_returns_every_record_and_names_the_unreadable(tmp_path: Path) -> None:
    """An unreadable record must not silently vanish from a listing —
    the gate treats it fail-closed, so the listing must surface it."""
    store = RunStore(tmp_path)
    store.reserve(
        "rc-aaaaaaaa-11111111",
        _KEY,
        known_runs=[],
        window_start="T0",
        work_id="w1",
        revision="a" * 40,
    )
    (tmp_path / "requests" / "rc-broken00-00000000.json").write_text("{not json")
    records, unreadable = store.list()
    assert [r.request_id for r in records] == ["rc-aaaaaaaa-11111111"]
    assert unreadable == ["rc-broken00-00000000.json"]


def test_release_malformed_quarantines_with_hash_and_identity(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    lock = store._lock_path(_KEY)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"half-writ")
    audit = store.release_malformed_lock(
        _KEY, actor="local-unauthenticated", reason="crash residue"
    )
    assert not lock.exists()
    quarantined = lock.parent / "released" / audit["quarantined_as"]
    assert quarantined.read_bytes() == b"half-writ"
    assert audit["sha256"] == hashlib.sha256(b"half-writ").hexdigest()
    assert audit["inode"] and audit["size"] == 9


def test_release_malformed_refuses_a_healthy_lock(tmp_path: Path) -> None:
    """A healthy lock is released only by its owning transitions."""
    store = RunStore(tmp_path)
    store.reserve(
        "rc-aaaaaaaa-11111111",
        _KEY,
        known_runs=[],
        window_start="T0",
        work_id="w",
        revision="a" * 40,
    )
    with pytest.raises(RunStoreError, match="parses"):
        store.release_malformed_lock(_KEY, actor="x", reason="r")


def test_release_malformed_refuses_on_io_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quarantine on the word of a broken read could destroy a healthy
    lock — IO failure is lock_io_unreadable, never malformed.

    The error is INJECTED, scoped to the exact lock path under test, not
    a global `Path.read_bytes` failure (which would also break `guard()`'s
    own internal reads elsewhere) and never a real `chmod` (unstable
    across users/CI, per this test's own brief docstring).
    """
    store = RunStore(tmp_path)
    lock = store._lock_path(_KEY)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"whatever")
    original_read_bytes = Path.read_bytes

    def _boom(self: Path) -> bytes:
        if self == lock:
            raise PermissionError("denied")
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _boom)
    with pytest.raises(RunStoreError, match="unreadable"):
        store.release_malformed_lock(_KEY, actor="x", reason="r")


def _hold_guard_and_reserve(state_dir: str, hold_seconds: float, acquired) -> None:
    """Multiprocessing target: holds the guard AND writes a genuinely
    fresh, healthy lock inside it (via `_reserve_locked`, never `reserve`
    — mirrors the same reentrancy rule `submit` follows). The guard is
    what stops `release_malformed_lock`, running concurrently from
    another process, from ever reading this lock mid-write.
    """
    store = RunStore(Path(state_dir))
    with store.guard(_KEY):
        store._reserve_locked(
            "rc-holder0-00000000", _KEY, known_runs=[], window_start="T0"
        )
        acquired.set()
        time.sleep(hold_seconds)


def test_the_guarded_race_cannot_quarantine_a_fresh_healthy_lock(
    tmp_path: Path,
) -> None:
    """The review scenario as RED against an unguarded implementation:
    hold the guard from another process (as in the guard tests), attempt
    release_malformed_lock → GuardBusyError, and the fresh lock written
    by the holder is untouched."""
    store = RunStore(tmp_path)
    lock = store._lock_path(_KEY)

    acquired = multiprocessing.Event()
    holder = multiprocessing.Process(
        target=_hold_guard_and_reserve,
        args=(str(tmp_path), GUARD_TIMEOUT_SECONDS + 2, acquired),
    )
    holder.start()
    try:
        assert acquired.wait(timeout=10), "holder never acquired"
        with pytest.raises(GuardBusyError):
            store.release_malformed_lock(_KEY, actor="x", reason="r")
        assert lock.exists()
        healthy = store.read_lock(_KEY)
        assert isinstance(healthy, LockInfo)
        assert healthy.request_id == "rc-holder0-00000000"
    finally:
        holder.terminate()
        holder.join()


# -- final fix wave: guarded transition releases (I-1), fail-closed list ----
# -- (I-2), lenient lock decode (deferred 11), quarantine naming (M-7) ------


def test_a_transition_release_takes_the_guard(tmp_path: Path) -> None:
    """I-1 (spec §8.1): a transition release is a lock-path mutation and
    must run inside the per-RepoKey guard. With another process holding
    the guard, `mark_materialized`'s release must surface GuardBusy
    rather than read-check-unlink the lock path unguarded — the
    interleaving the spec names (a duplicate release racing a fresh
    guarded reserve) is exactly what an unguarded unlink allows."""
    store = RunStore(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")

    acquired = multiprocessing.Event()
    holder = multiprocessing.Process(
        target=_hold_guard, args=(str(tmp_path), GUARD_TIMEOUT_SECONDS + 2, acquired)
    )
    holder.start()
    try:
        assert acquired.wait(timeout=10), "holder never acquired"
        with pytest.raises(GuardBusyError):
            store.mark_materialized(_REQ, "01AAA")
        # the unguarded unlink never ran: the lock is still this request's
        assert store.holds_lock(_KEY) == _REQ
        # and the transition did not half-happen: a record persisted as
        # materialized/terminal BEFORE the failed release would strand a
        # healthy lock forever (replay returns the terminal receipt, the
        # malformed-lock escape refuses a lock that parses) — guard
        # contention must fail the WHOLE transition, not just its tail
        rec = store.get(_REQ)
        assert rec is not None and rec.state == "reserved"
        assert rec.run_id is None
    finally:
        holder.terminate()
        holder.join()


def test_a_duplicate_transition_release_cannot_free_anothers_lock(
    tmp_path: Path,
) -> None:
    """The I-1 damage, as far as it can be forced sequentially: request A's
    release runs again after request B reserved a fresh healthy lock at
    the same path. The duplicate must refuse (`held by`), never unlink
    B's lock — B would otherwise be launching with no lock at all."""
    store = RunStore(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_materialized(_REQ, "01AAA")  # releases A's lock
    store.reserve(_OTHER, _KEY, known_runs=[], window_start="t")
    with pytest.raises(LockBusyError, match="held by"):
        store.mark_terminal(_REQ, "completed")  # A's duplicate release
    assert store.holds_lock(_KEY) == _OTHER


def test_an_unlistable_requests_dir_is_an_unreadable_entry_not_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I-2: `([], [])` for a `requests/` directory that cannot be iterated
    lets the gate read "nothing non-terminal here" off exactly the broken
    input — the enumeration-level twin of the per-record unreadable case
    the second return exists for. It must surface as an unreadable entry
    so `classify_repo` blocks."""
    store = RunStore(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    requests_dir = tmp_path / "requests"
    original_glob = Path.glob

    def _boom(self: Path, pattern: str):
        if self == requests_dir:
            raise PermissionError("denied")
        return original_glob(self, pattern)

    monkeypatch.setattr(Path, "glob", _boom)
    records, unreadable = store.list()
    assert records == []
    assert unreadable and "requests directory" in unreadable[0]


def test_invalid_utf8_lock_bytes_read_as_malformed_not_a_crash(
    tmp_path: Path,
) -> None:
    """Deferred 11: a strict decode raised UnicodeDecodeError (a
    ValueError) for invalid UTF-8 — escaping submit's
    `except (RunStoreError, OSError)` as a 500. The bytes WERE read, so
    damage is proven: Malformed, same rule as `release_malformed_lock`."""
    store = RunStore(tmp_path)
    lock = store._lock_path(_KEY)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"\xff\xfe\x00bad")
    state = store.read_lock(_KEY)
    assert isinstance(state, Malformed)


def test_two_quarantines_within_one_second_do_not_clobber(tmp_path: Path) -> None:
    """M-7: the quarantine name had 1-second resolution, so a second
    release within the same second silently overwrote the previous
    quarantined bytes and their audit."""
    store = RunStore(tmp_path)
    lock = store._lock_path(_KEY)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"first")
    first = store.release_malformed_lock(_KEY, actor="x", reason="r")
    lock.write_bytes(b"second")
    second = store.release_malformed_lock(_KEY, actor="x", reason="r")
    released = lock.parent / "released"
    assert first["quarantined_as"] != second["quarantined_as"]
    assert (released / str(first["quarantined_as"])).read_bytes() == b"first"
    assert (released / str(second["quarantined_as"])).read_bytes() == b"second"


def test_ensure_creates_guards_alongside_requests_and_locks(tmp_path: Path) -> None:
    """Deferred 6: `guards/` was created lazily in `guard()` only — an
    asymmetry with `requests/` and `locks/`."""
    store = RunStore(tmp_path)
    store._ensure()
    assert (tmp_path / "guards").is_dir()


def test_a_legacy_reserved_record_retries_instead_of_conflicting(
    tmp_path: Path,
) -> None:
    """Migration rule: a pre-B1 `reserved` record has no stored fingerprint,
    but its identity triple (repo_key, work_id, revision) is persisted — the
    fingerprint is DERIVED from exactly that triple, so an empty stored one
    derives from the record itself. Without this, the documented retry path
    dies `FingerprintMismatch` forever after an upgrade."""
    store = RunStore(tmp_path)
    path = tmp_path / "requests" / "rc-legacy11-00000000.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "request_id": "rc-legacy11-00000000",
                "repo_key": _KEY.as_text(),
                "state": "reserved",
                "work_id": "w",
                "revision": "a" * 40,
            }
        )
    )
    rec = store.reserve(
        "rc-legacy11-00000000",
        _KEY,
        known_runs=[],
        window_start="T1",
        work_id="w",
        revision="a" * 40,
    )
    assert rec.request_id == "rc-legacy11-00000000"
    assert rec.state == "reserved"
    # ...and the identity check keeps its teeth for a DIFFERENT attempt:
    with pytest.raises(FingerprintMismatch):
        store.reserve(
            "rc-legacy11-00000000",
            _KEY,
            known_runs=[],
            window_start="T1",
            work_id="w",
            revision="b" * 40,
        )


def test_quarantine_refuses_if_the_lock_was_replaced_after_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec §8.3: the identity is re-checked under the guard — the guard is
    advisory flock and excludes only cooperating writers, so the file the
    rename would move must be proven to still be the file whose bytes were
    read. Here an adversary swaps in a HEALTHY lock between the read and
    the move: quarantining it would leave the repo unprotected mid-launch
    while the audit swears it moved the old corrupt bytes."""
    store = RunStore(tmp_path)
    lock = store._lock_path(_KEY)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"half-writ")

    healthy = json.dumps(
        {"request_id": "rc-bbbbbbbb-22222222", "fingerprint": "f", "created_at": "t"}
    ).encode()
    real_validate = LockInfo.model_validate_json

    def _swap_then_validate(data: str) -> LockInfo:
        # runs after the read, before the rename — the adversary's window
        lock.write_bytes(healthy)
        return real_validate(data)

    monkeypatch.setattr(LockInfo, "model_validate_json", _swap_then_validate)
    with pytest.raises(RunStoreError, match="identity"):
        store.release_malformed_lock(_KEY, actor="x", reason="r")
    # the healthy lock survived, nothing was quarantined
    assert lock.read_bytes() == healthy
    assert not (lock.parent / "released").exists() or not any(
        (lock.parent / "released").iterdir()
    )
