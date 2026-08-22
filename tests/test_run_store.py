"""Durable launch records and the per-RepoKey lock (spec §5.2, §5.4)."""

from pathlib import Path

import pytest

from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_store import LockBusyError, RunStore, RunStoreError

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
