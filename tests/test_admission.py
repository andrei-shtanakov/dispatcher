"""Pure admission classification (spec §5, §7)."""

import pytest

from dispatcher.core.admission import (
    LAUNCH_BUSY,
    LOCK_IO_UNREADABLE,
    LOCK_MALFORMED,
    RUN_IN_FLIGHT,
    RUN_STATE_UNREADABLE,
    RUN_VANISHED,
    Blocker,
    RunFact,
    classify_repo,
)
from dispatcher.core.run_store import LockInfo, Malformed


def _run(status, *, run_id="01A", request_id="rc-x", exists=True):
    return RunFact(
        run_id=run_id, status=status, request_id=request_id, run_dir_exists=exists
    )


def test_no_locks_no_runs_is_ready():
    a = classify_repo(lock=None, lock_error=None, runs=(), runs_unreadable=())
    assert a.admission == "ready" and a.blockers == ()


def test_terminal_runs_do_not_block():
    runs = tuple(_run(s) for s in ("completed", "cancelled", "superseded", "failed"))
    assert classify_repo(None, None, runs, ()).admission == "ready"


@pytest.mark.parametrize(
    "status", ["running", "interrupted", "suspended", "unreadable", "legacy"]
)
def test_every_non_terminal_status_blocks_fail_closed(status):
    """Spec §7: liveness is unprovable, so everything unproven blocks —
    including statuses this code has never heard of."""
    a = classify_repo(None, None, (_run(status),), ())
    assert a.admission == "blocked"
    assert a.blockers[0].code == RUN_IN_FLIGHT


def test_an_unknown_future_status_blocks_too():
    a = classify_repo(None, None, (_run("someday-new-status"),), ())
    assert a.admission == "blocked"


def test_vanished_is_its_own_code_with_the_precise_predicate():
    """Non-terminal + run_id + directory absent = vanished (escape offered);
    anything short of that is not."""
    a = classify_repo(None, None, (_run("interrupted", exists=False),), ())
    assert a.blockers[0].code == RUN_VANISHED
    assert a.blockers[0].request_id == "rc-x"


def test_unreadable_sources_block_as_unknown_never_as_finished():
    a = classify_repo(None, None, (), ("state.db",))
    assert a.admission == "blocked"
    assert a.blockers[0].code == RUN_STATE_UNREADABLE


def test_lock_states_map_to_their_distinct_codes():
    held = classify_repo(LockInfo(request_id="rc-h"), None, (), ())
    assert held.blockers[0] == Blocker(code=LAUNCH_BUSY, request_id="rc-h")
    mal = classify_repo(Malformed(detail="0 bytes"), None, (), ())
    assert mal.blockers[0].code == LOCK_MALFORMED
    io = classify_repo(None, "permission denied", (), ())
    assert io.blockers[0].code == LOCK_IO_UNREADABLE


def test_blockers_coexist_no_hidden_priority():
    """Spec review: busy AND in-flight at once — a list, not an enum."""
    a = classify_repo(
        LockInfo(request_id="rc-h"), None, (_run("running"),), ("state.db",)
    )
    codes = {b.code for b in a.blockers}
    assert codes == {LAUNCH_BUSY, RUN_IN_FLIGHT, RUN_STATE_UNREADABLE}


def test_an_unlinked_run_blocks_and_names_its_run_id():
    a = classify_repo(
        None, None, (_run("running", request_id=None, run_id="01UNL"),), ()
    )
    b = a.blockers[0]
    assert b.code == RUN_IN_FLIGHT and b.request_id is None and b.run_id == "01UNL"
