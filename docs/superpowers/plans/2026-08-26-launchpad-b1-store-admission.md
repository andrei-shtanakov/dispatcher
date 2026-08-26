# Launchpad PR-B1: store rework, admission core, single-live-run gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The server half of launchpad that does not depend on the `@dag`
contract: guarded lock lifecycle, idempotency fingerprint, reproducible
admission rejections, `RunStore.list`, the single-live-run gate, and both
audited escapes.

**Architecture:** All lock-path mutations move inside a per-`RepoKey`
guard section (advisory `fcntl` lock, 2 s bound). The lock file becomes a
minimal preflight record. A new pure module `dispatcher/core/admission.py`
classifies captured inputs; `submit` consumes it for the
single-live-run gate. Two new endpoints provide the audited escapes.

**Tech Stack:** Python 3.12, pydantic v2, FastAPI, pytest; no new
dependencies (`fcntl` is stdlib).

**Spec:** `docs/superpowers/specs/2026-08-26-launchpad-design.md` — §§4.2
(response codes), 5 (classifier), 7 (gate), 8 (store + escapes), 10
(tests), 11 (B1 scope), 12 (limits).

## Global Constraints

- **No `@dag` recognition in shipping code** (spec §3.1, §11): B1's
  classifier types accept synthetic `PlanItem` values built by tests; the
  real parser wiring is PR-B2.
- **The submit body does not change in B1.** v2 bodies, HTTP 409
  taxonomy and `/api/launchpad` are PR-C. In B1 a gate refusal travels
  through the existing refusal channel — `LaunchReceipt(accepted=False,
  reason=...)` — with the reason **prefixed by the admission code**, e.g.
  `"run_in_flight: 01ABC… has no terminal outcome"`. PR-C lifts these to
  structured 409s; the codes are shared via `admission.py` constants now.
- Guard acquisition bound: **2 seconds**, then `GuardBusyError` → refusal
  `"guard_busy: …"` with **zero lock-file mutations** (spec §8.1).
- Fail-closed run classification (spec §7): only
  `{"completed","cancelled","superseded","failed"}` count as terminal;
  everything else — including `unreadable` and `legacy` — blocks.
- Typecheck: `uv run pyrefly check dispatcher tests scripts` — explicit
  paths (a bare run checks zero files in a worktree). Format/lint:
  `uv run ruff format . && uv run ruff check . --fix`.
- Exactly three pre-existing live-smoke failures
  (`test_governance_live_smoke`, `test_product_proposals_live_smoke`,
  `test_spec_runner_config_integration`); anything else is yours.
- Every fix RED-verified: run the new test against pre-change code.

## File Structure

| File | Responsibility |
|---|---|
| `dispatcher/core/run_store.py` (modify, 310 lines today) | guard section, lock-as-preflight, fingerprint, `list()`, tombstone transitions, legacy-record load |
| `dispatcher/core/admission.py` (create) | pure classification: input types, `classify_item`, `classify_repo`, code constants |
| `dispatcher/core/run_controller.py` (modify) | submit gate wiring, `acknowledge_vanished`, `release_malformed_lock` |
| `dispatcher/server/app.py` (modify) | the two escape endpoints |
| `tests/test_run_store.py`, `tests/test_admission.py` (create), `tests/test_run_controller.py`, `tests/test_run_api.py` | per-layer tests |

---

### Task 1: Guard section in RunStore

**Files:**
- Modify: `dispatcher/core/run_store.py` (imports at :14-24; class body near `_lock_path` :113)
- Test: `tests/test_run_store.py`

**Interfaces:**
- Produces: `RunStore.guard(key: RepoKey)` — context manager;
  `GuardBusyError(RunStoreError)`; module constant
  `GUARD_TIMEOUT_SECONDS = 2.0`.
- Consumes: existing `RunStore._ensure()`, `safe_path_parts`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_run_store.py
import multiprocessing
import time
from pathlib import Path

import pytest

from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_store import GUARD_TIMEOUT_SECONDS, GuardBusyError, RunStore

_KEY = RepoKey(host="github.com", owner="owner", repo="deployer")


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
    holder.kill()          # crash, not clean exit
    holder.join()
    with store.guard(_KEY):  # the OS released the advisory lock
        pass
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_run_store.py -q`
Expected: FAIL — `ImportError: cannot import name 'GuardBusyError'`

- [ ] **Step 3: Implement**

```python
# run_store.py — imports: add
import fcntl
import time

GUARD_TIMEOUT_SECONDS = 2.0


class GuardBusyError(RunStoreError):
    """The recovery critical section stayed held past its bound (spec §8.1)."""


# inside RunStore:
    def _guard_path(self, key: RepoKey) -> Path:
        return self._state_dir / "guards" / ("__".join(safe_path_parts(key)) + ".guard")

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
        """
        path = self._guard_path(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
            os.close(fd)   # closing the fd releases the flock
```

(`from contextlib import contextmanager`, `from collections.abc import
Iterator` join the imports.)

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/test_run_store.py -q`
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(store): bounded per-RepoKey guard section for lock-path mutations"`

---

### Task 2: Lock file is a minimal preflight record

**Files:**
- Modify: `dispatcher/core/run_store.py` — `reserve` (:142-…, the `os.open(lock, O_CREAT|O_EXCL…)` block at :172), `_lock_holder` (:221), `release_lock` (:237)
- Test: `tests/test_run_store.py`

**Interfaces:**
- Produces: `LockInfo(BaseModel)` — `request_id: str`, `fingerprint:
  str`, `created_at: str`; `LockState = LockInfo | Malformed | None`
  where `class Malformed: detail: str` (frozen dataclass);
  `RunStore.read_lock(key) -> LockState`;
  `fingerprint_of(repo_key: str, work_id: str, revision: str) -> str`
  (module function: `"|".join((repo_key, work_id, revision))`).
- Consumes: Task 1's `guard`.

- [ ] **Step 1: Failing tests**

```python
def test_lock_file_is_a_preflight_record(tmp_path: Path) -> None:
    """The lock itself carries {request_id, fingerprint, created_at}.

    Spec §8.1: "a lock with no owning fact" must not be a representable
    steady state — the fact travels IN the lock, written to the same fd
    right after O_EXCL.
    """
    store = RunStore(tmp_path)
    store.reserve(
        "rc-aaaaaaaa-11111111", _KEY, known_runs=[], window_start="T0",
        work_id="todo://deployer/x", revision="a" * 40,
    )
    info = store.read_lock(_KEY)
    assert info is not None and not isinstance(info, Malformed)
    assert info.request_id == "rc-aaaaaaaa-11111111"
    assert info.fingerprint == fingerprint_of(_KEY.as_text(), "todo://deployer/x", "a" * 40)
    assert info.created_at


def test_an_empty_lock_reads_as_malformed_not_as_absent(tmp_path: Path) -> None:
    """The §8.1 crash residue: O_EXCL succeeded, the write never happened.

    Malformed and absent MUST be distinguishable — absent means free,
    malformed means fail-closed blocked with an audited escape.
    """
    store = RunStore(tmp_path)
    lock = tmp_path / "locks" / ("__".join(_KEY.as_path_parts()) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("")
    state = store.read_lock(_KEY)
    assert isinstance(state, Malformed)
    assert store.read_lock(RepoKey(host="github.com", owner="o", repo="other")) is None
```

- [ ] **Step 2: Verify failure** — `uv run pytest tests/test_run_store.py -q` → `ImportError` (Malformed, fingerprint_of)

- [ ] **Step 3: Implement**

In `reserve`: wrap the whole acquire-and-write in `with self.guard(key):`;
extend the lock payload — replace the current
`json.dump({"request_id": request_id, "pid": os.getpid()}, handle)` with:

```python
            payload = {
                "request_id": request_id,
                "fingerprint": fingerprint_of(key.as_text(), work_id, revision),
                "created_at": window_start,
                "pid": os.getpid(),
            }
            handle.write(json.dumps(payload))
            handle.flush()
            os.fsync(handle.fileno())
```

Add:

```python
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
    request_id: str
    fingerprint: str = ""
    created_at: str = ""


# inside RunStore:
    def read_lock(self, key: RepoKey) -> LockInfo | Malformed | None:
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
```

`_lock_holder`/`release_lock` keep working: `release_lock` reads via
`read_lock` and refuses on `Malformed` (existing behaviour preserved —
a healthy lock is released only by its owning transitions).

- [ ] **Step 4: Verify pass**; run the whole file: `uv run pytest tests/test_run_store.py -q`
- [ ] **Step 5: Commit** — `git commit -m "feat(store): lock file is a minimal preflight record; Malformed is distinct from absent"`

---

### Task 3: Fingerprint idempotency and reproducible admission rejection

**Files:**
- Modify: `dispatcher/core/run_store.py` — `LaunchRecord` (:53-90), `reserve` (:142), new transition
- Test: `tests/test_run_store.py`

**Interfaces:**
- Produces: `LaunchRecord` fields `fingerprint: str = ""`,
  `response_class: str | None = None`, `admission_code: str | None =
  None`, `admission_detail: str | None = None`, `admission_current:
  dict | None = None`, `rejected_at: str | None = None`;
  `RunStore.mark_admission_rejected(request_id, *, code, detail,
  current) -> LaunchRecord` (terminal, releases the lock);
  `FingerprintMismatch(RunStoreError)`.
- Consumes: Task 2's `fingerprint_of`.

- [ ] **Step 1: Failing tests**

```python
def test_reserve_replays_only_a_matching_fingerprint(tmp_path: Path) -> None:
    """Same request_id + same attempt → prior record; different attempt →
    FingerprintMismatch. A reused id must not adopt another attempt's
    receipt (spec §8.2)."""
    store = RunStore(tmp_path)
    first = store.reserve("rc-aaaaaaaa-11111111", _KEY, known_runs=[],
                          window_start="T0", work_id="w1", revision="a" * 40)
    again = store.reserve("rc-aaaaaaaa-11111111", _KEY, known_runs=[],
                          window_start="T9", work_id="w1", revision="a" * 40)
    assert again == first
    with pytest.raises(FingerprintMismatch):
        store.reserve("rc-aaaaaaaa-11111111", _KEY, known_runs=[],
                      window_start="T9", work_id="OTHER", revision="a" * 40)


def test_admission_rejection_is_terminal_and_reproducible(tmp_path: Path) -> None:
    """The rejection persists an immutable payload (spec §8.2): a repeat
    must replay the original decision after the workspace moved on —
    re-classification could even PASS where the original failed."""
    store = RunStore(tmp_path)
    store.reserve("rc-aaaaaaaa-11111111", _KEY, known_runs=[],
                  window_start="T0", work_id="w1", revision="a" * 40)
    rec = store.mark_admission_rejected(
        "rc-aaaaaaaa-11111111",
        code="run_in_flight", detail="run 01X has no terminal outcome",
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
    path.write_text(json.dumps({
        "request_id": "rc-legacy00-00000000",
        "repo_key": "github.com/owner/deployer",
        "state": "materialized", "run_id": "01OLD",
    }))
    rec = store.get("rc-legacy00-00000000")
    assert rec is not None and rec.fingerprint == "" and rec.response_class is None
```

- [ ] **Step 2: Verify failure** — AttributeError / ImportError.

- [ ] **Step 3: Implement**

`LaunchRecord`: append the six fields with the defaults above (comment:
*persisted so a fingerprint-matching repeat replays the original 409
semantically, with zero re-classification — spec §8.2*). In `reserve`:
compute `fp = fingerprint_of(key.as_text(), work_id, revision)`; in the
existing-record branch compare — equal → return existing, else `raise
FingerprintMismatch(f"{request_id} was already used for a different
attempt")`; store `fingerprint=fp` in the new record. Add:

```python
    def mark_admission_rejected(
        self, request_id: str, *, code: str, detail: str, current: dict
    ) -> LaunchRecord:
        """Terminalize a refused attempt; the lock never outlives the fact."""
        record = self._transition(
            request_id,
            state="terminal", outcome="admission-rejected",
            response_class="admission_rejected",
            admission_code=code, admission_detail=detail,
            admission_current=current,
            rejected_at=datetime.now(UTC).isoformat(),
        )
        self._release_for(record)
        return record
```

(`from datetime import UTC, datetime` joins imports.)

- [ ] **Step 4: Verify pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(store): attempt fingerprint + reproducible admission rejection"`

---

### Task 4: `RunStore.list()`

**Files:**
- Modify: `dispatcher/core/run_store.py`
- Test: `tests/test_run_store.py`

**Interfaces:**
- Produces: `RunStore.list() -> list[LaunchRecord]` — every readable
  record, unreadable files reported, ordering by `request_id` for
  determinism (callers sort by their own keys).

- [ ] **Step 1: Failing test**

```python
def test_list_returns_every_record_and_names_the_unreadable(tmp_path: Path) -> None:
    """An unreadable record must not silently vanish from a listing —
    the gate treats it fail-closed, so the listing must surface it."""
    store = RunStore(tmp_path)
    store.reserve("rc-aaaaaaaa-11111111", _KEY, known_runs=[],
                  window_start="T0", work_id="w1", revision="a" * 40)
    (tmp_path / "requests" / "rc-broken00-00000000.json").write_text("{not json")
    records, unreadable = store.list()
    assert [r.request_id for r in records] == ["rc-aaaaaaaa-11111111"]
    assert unreadable == ["rc-broken00-00000000.json"]
```

- [ ] **Step 2: Verify failure**; **Step 3: Implement** —

```python
    def list(self) -> tuple[list[LaunchRecord], list[str]]:
        """Every record, plus the FILENAMES that failed to parse.

        The second return exists because the single-live-run gate is
        fail-closed: a corrupt record must block as unknown, and a
        listing that silently dropped it would let the gate read
        "nothing non-terminal here" off exactly the broken input.
        """
        requests = self._state_dir / "requests"
        records: list[LaunchRecord] = []
        unreadable: list[str] = []
        try:
            paths = sorted(requests.glob("*.json"))
        except OSError:
            return [], []
        for path in paths:
            try:
                records.append(LaunchRecord.model_validate_json(path.read_text()))
            except Exception:
                unreadable.append(path.name)
        return records, unreadable
```

- [ ] **Step 4: Verify pass**; **Step 5: Commit** — `git commit -m "feat(store): list() that names unreadable records instead of dropping them"`

---

### Task 5: The admission module (pure, synthetic inputs)

**Files:**
- Create: `dispatcher/core/admission.py`
- Test: `tests/test_admission.py`

**Interfaces:**
- Produces (consumed by Tasks 6-8 and PR-B2/C):

```python
# Codes — the single vocabulary shared by receipts now and 409s in PR-C.
LAUNCH_BUSY = "launch_busy"; RUN_IN_FLIGHT = "run_in_flight"
RUN_VANISHED = "run_vanished"; LOCK_MALFORMED = "lock_malformed"
LOCK_IO_UNREADABLE = "lock_io_unreadable"
RUN_STATE_UNREADABLE = "run_state_unreadable"; GUARD_BUSY = "guard_busy"
TERMINAL_RUN_STATUSES = frozenset({"completed", "cancelled", "superseded", "failed"})

@dataclass(frozen=True)
class RunFact:      # captured from classified_runs / launch_records
    run_id: str; status: str; request_id: str | None; run_dir_exists: bool

@dataclass(frozen=True)
class Blocker:
    code: str; request_id: str | None = None; run_id: str | None = None
    detail: str = ""

@dataclass(frozen=True)
class RepoAdmission:
    admission: str            # "ready" | "blocked"
    blockers: tuple[Blocker, ...]

def classify_repo(
    lock: LockInfo | Malformed | None,
    lock_error: str | None,          # an IO failure reading the lock
    runs: tuple[RunFact, ...],
    runs_unreadable: tuple[str, ...],  # unreadable state sources, by name
) -> RepoAdmission: ...
```

  (`classify_item` and the full `CapturedInputs` arrive with the parser
  in PR-B2 — B1 ships the repo-level half the gate needs; the item-level
  half would dangle with no caller and no real `PlanItem` source.)

- [ ] **Step 1: Failing tests** — the condition table, one input each:

```python
import pytest

from dispatcher.core.admission import (
    Blocker, RunFact, classify_repo,
    LAUNCH_BUSY, LOCK_IO_UNREADABLE, LOCK_MALFORMED,
    RUN_IN_FLIGHT, RUN_STATE_UNREADABLE, RUN_VANISHED,
)
from dispatcher.core.run_store import LockInfo, Malformed


def _run(status, *, run_id="01A", request_id="rc-x", exists=True):
    return RunFact(run_id=run_id, status=status, request_id=request_id,
                   run_dir_exists=exists)


def test_no_locks_no_runs_is_ready():
    a = classify_repo(lock=None, lock_error=None, runs=(), runs_unreadable=())
    assert a.admission == "ready" and a.blockers == ()


def test_terminal_runs_do_not_block():
    runs = tuple(_run(s) for s in ("completed", "cancelled", "superseded", "failed"))
    assert classify_repo(None, None, runs, ()).admission == "ready"


@pytest.mark.parametrize("status", ["running", "interrupted", "suspended",
                                   "unreadable", "legacy"])
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
    a = classify_repo(LockInfo(request_id="rc-h"), None,
                      (_run("running"),), ("state.db",))
    codes = {b.code for b in a.blockers}
    assert codes == {LAUNCH_BUSY, RUN_IN_FLIGHT, RUN_STATE_UNREADABLE}


def test_an_unlinked_run_blocks_and_names_its_run_id():
    a = classify_repo(None, None,
                      (_run("running", request_id=None, run_id="01UNL"),), ())
    b = a.blockers[0]
    assert b.code == RUN_IN_FLIGHT and b.request_id is None and b.run_id == "01UNL"
```

- [ ] **Step 2: Verify failure** — module does not exist.

- [ ] **Step 3: Implement** `dispatcher/core/admission.py`:

```python
"""Pure admission classification (spec §5, §7).

No IO by construction: every function consumes captured values. Both
adapters — the launchpad snapshot assembler (PR-C) and submit's gate
(this PR) — call these same functions, and the adapter-level property
test of spec §5 is what keeps a second implementation from growing.
"""
from __future__ import annotations

from dataclasses import dataclass

from dispatcher.core.run_store import LockInfo, Malformed

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
# ... (dataclasses as in Interfaces above)


def classify_repo(lock, lock_error, runs, runs_unreadable):
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
            blockers.append(Blocker(code=RUN_VANISHED,
                                    request_id=run.request_id, run_id=run.run_id))
        else:
            blockers.append(Blocker(code=RUN_IN_FLIGHT,
                                    request_id=run.request_id, run_id=run.run_id))
    return RepoAdmission(
        admission="blocked" if blockers else "ready",
        blockers=tuple(blockers),
    )
```

- [ ] **Step 4: Verify pass**; **Step 5: Commit** — `git commit -m "feat(admission): pure repo classifier — fail-closed by subtraction"`

---

### Task 6: Single-live-run gate in submit

**Files:**
- Modify: `dispatcher/core/run_controller.py` — `submit` (existing-record branch and the `validate_request`→`reserve` sequence at :262-300), plus a capture helper
- Test: `tests/test_run_controller.py`

**Interfaces:**
- Consumes: `classify_repo`, `RunFact`, `GuardBusyError`,
  `FingerprintMismatch`, `RunStore.guard`, `RunStore.list`,
  `classified_runs` (`dispatcher/core/collectors/maestro.py:182`,
  signature `(home, snap) -> list[tuple[OrchestrationRunInfo, Path]]`).
- Produces: `RunController._capture_repo_facts(key) ->
  tuple[LockState, str | None, tuple[RunFact, ...], tuple[str, ...]]`;
  submit order: guard → fingerprint/idempotency → capture → classify →
  reject-or-reserve → launch.

- [ ] **Step 1: Failing tests**

```python
def test_submit_refuses_while_a_nonterminal_run_exists(tmp_path: Path) -> None:
    """Spec §7: at most one run without proven terminal outcome per
    RepoKey — checked against ALL runs, not the latest."""
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01AAA")
    controller = RunController(_config(tmp_path, cli), materialize_timeout=10.0)
    assert controller.submit(_request(head)).accepted is True   # run 01AAA, non-terminal

    second = _request(head).model_copy(
        update={"request_id": "22222222-2222-4222-8222-222222222222"})
    receipt = controller.submit(second)
    assert receipt.accepted is False
    assert receipt.reason.startswith("run_in_flight:")
    assert "01AAA" in receipt.reason
    # the refusal is terminal, reproducible, and freed the lock
    rec = RunStore(tmp_path / "state").get(second.request_id)
    assert rec.outcome == "admission-rejected"
    assert rec.admission_code == "run_in_flight"


def test_the_refusal_replays_without_reclassification(tmp_path: Path, monkeypatch) -> None:
    """Semantic equality of the replay, and provably zero classifier calls
    (spec §8.2 / §10) — re-classification could pass where the original
    failed."""
    # ... (arrange as above, obtain the refusal once)
    import dispatcher.core.run_controller as rc
    calls = []
    monkeypatch.setattr(rc, "classify_repo",
                        lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(AssertionError("re-classified")))
    replay = controller.submit(second)
    assert replay.accepted is False
    assert replay.reason.startswith("run_in_flight:")
    assert calls == []


def test_unreadable_run_state_blocks_fail_closed(tmp_path: Path) -> None:
    """A corrupt state.db must read as unknown, never as finished."""
    # arrange one materialized run, then corrupt its state.db
    # (write b"garbage") and expect refusal reason run_state_unreadable:
    ...


def test_guard_busy_refuses_with_zero_lock_mutations(tmp_path: Path) -> None:
    """A held guard (separate process, as in test_run_store) makes submit
    refuse guard_busy:, and the locks/ dir content is byte-identical
    before and after."""
    ...
```

(The elided arrange blocks follow `test_submit_refuses_while_...`
verbatim; write them out in the test file.)

- [ ] **Step 2: Verify failure** — today the second submit launches.

- [ ] **Step 3: Implement**

In `submit`, after `validate_request` and before `reserve`:

```python
        try:
            with store.guard(validated.key):
                lock_state, lock_err = self._read_lock_state(store, validated.key)
                runs, unreadable = self._capture_run_facts(validated.key)
                verdict = classify_repo(lock_state, lock_err, runs, unreadable)
                if verdict.admission == "blocked":
                    b = verdict.blockers[0]
                    detail = _blocker_detail(b)
                    store.reserve(...)          # preflight fact, then:
                    store.mark_admission_rejected(
                        request.request_id, code=b.code, detail=detail,
                        current={"blockers": [asdict(x) for x in verdict.blockers]},
                    )
                    return self._refuse(request.request_id, f"{b.code}: {detail}")
                store.reserve(... as today ...)
        except GuardBusyError as err:
            return self._refuse(request.request_id, f"guard_busy: {err}")
        except FingerprintMismatch as err:
            return self._refuse(request.request_id, f"request_id_conflict: {err}")
```

`_capture_run_facts` builds `RunFact`s from `classified_runs(home,
scratch_snapshot)` filtered to `info.repo_key == key.as_text()`, joining
`request_id` via `RunStore.list()` records (`record.run_id ==
info.run_id`), `run_dir_exists` via the run directory check, and
`runs_unreadable` from the scratch snapshot's warnings plus `list()`'s
unreadable names. The launch-window lock (`launch_busy`) is itself the
lock taken by `reserve` — inside the guard the flow reserves only after
a clean verdict, so `classify_repo`'s `LAUNCH_BUSY` arm triggers when a
*previous* attempt still holds the lock.

Idempotent replay: in the existing-record branch (submit's top), when the
record has `response_class == "admission_rejected"` and the fingerprint
matches, return
`self._refuse(request_id, f"{record.admission_code}: {record.admission_detail}")`
**without** touching the classifier; a fingerprint mismatch raises
`FingerprintMismatch` from `reserve` — but the replay branch must check
the fingerprint itself since it returns before `reserve`.

- [ ] **Step 4: Verify pass**, then the full suite.
- [ ] **Step 5: Commit** — `git commit -m "feat(gate): single live run per RepoKey — guarded, fail-closed, reproducible refusals"`

---

### Task 7: `acknowledge-vanished`

**Files:**
- Modify: `dispatcher/core/run_store.py` (tombstone transition), `dispatcher/core/run_controller.py`, `dispatcher/server/app.py`
- Test: `tests/test_run_controller.py`, `tests/test_run_api.py`

**Interfaces:**
- Produces: `RunStore.mark_vanished_acknowledged(request_id, *, actor,
  reason, prior_run_id) -> LaunchRecord` (terminal,
  `outcome="vanished-acknowledged"`, audit fields
  `ack_actor/ack_at/ack_reason/prior_run_id`);
  `RunController.acknowledge_vanished(request_id, confirm_run_id,
  reason, display_name) -> LaunchRecord`;
  `POST /api/runs/{request_id}/acknowledge-vanished` (X-Action-Token
  required — it mutates).
- Consumes: guard, `_key_from_record`.

- [ ] **Step 1: Failing tests** (controller level)

```python
def test_acknowledge_requires_the_precise_predicate(tmp_path: Path) -> None:
    """Non-terminal record + run_id + directory absent — and nothing less.
    A present directory refuses; a terminal record refuses."""
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    with pytest.raises(RunRejectedError, match="still exists"):
        controller.acknowledge_vanished(_REQ, "01AAA", "cleanup", None)
    shutil.rmtree(tmp_path / "mhome/projects/github.com/owner/deployer/runs/01AAA")
    rec = controller.acknowledge_vanished(_REQ, "01AAA", "host wiped", None)
    assert rec.outcome == "vanished-acknowledged"
    assert rec.ack_actor == "local-unauthenticated"
    assert rec.prior_run_id == "01AAA"


def test_confirm_run_id_must_match_retyped(tmp_path: Path) -> None:
    # directory removed as above; wrong id refuses:
    with pytest.raises(RunRejectedError, match="confirm_run_id"):
        controller.acknowledge_vanished(_REQ, "01WRONG", "r", None)


def test_reason_is_capped_and_newline_normalized(tmp_path: Path) -> None:
    rec = controller.acknowledge_vanished(_REQ, "01AAA", "a\nb" + "x" * 5000, None)
    assert "\n" not in rec.ack_reason and len(rec.ack_reason) <= 1024


def test_io_error_refuses_rather_than_acknowledges(tmp_path: Path, monkeypatch) -> None:
    """Spec §7: a broken stat is unreadable, not vanished — injected
    error, never chmod (unstable across users/CI)."""
    monkeypatch.setattr(Path, "is_dir",
                        lambda self: (_ for _ in ()).throw(PermissionError("denied")))
    with pytest.raises(RunRejectedError, match="unreadable"):
        controller.acknowledge_vanished(_REQ, "01AAA", "r", None)
```

- [ ] **Step 2: Verify failure**; **Step 3: Implement** — controller:

```python
    _REASON_CAP = 1024

    def acknowledge_vanished(self, request_id, confirm_run_id, reason, display_name):
        """Spec §8.3. The limit, verbatim: отсутствие каталога не
        доказывает отсутствие процесса — this is an administrative
        release of a fail-closed block, with the risk recorded."""
        store = self._store()
        record = self._record_for(request_id)
        if record.state == "terminal":
            raise RunRejectedError(f"{request_id} is already terminal")
        if record.run_id is None:
            raise RunRejectedError(f"{request_id} has no run to acknowledge")
        if confirm_run_id != record.run_id:
            raise RunRejectedError(
                "confirm_run_id does not match the recorded run — retype it")
        key = _key_from_record(record)
        with store.guard(key):
            run_dir = self._runs_root(key) / record.run_id
            try:
                present = run_dir.is_dir()
            except OSError as err:
                raise RunRejectedError(f"run state unreadable: {err}") from err
            if present:
                raise RunRejectedError(
                    f"run {record.run_id} still exists — nothing to acknowledge")
            reason_norm = " ".join(reason.split())[: self._REASON_CAP]
            actor = "local-unauthenticated"   # server-assigned; spec §8.3
            if display_name:
                actor += f" (self_reported: {display_name[:64]})"
            return store.mark_vanished_acknowledged(
                request_id, actor=actor, reason=reason_norm,
                prior_run_id=record.run_id)
```

Store transition mirrors `mark_admission_rejected` (terminal + release +
audit fields, all defaulted for legacy loads). Endpoint in `app.py` next
to `/resolve`, body model `{confirm_run_id: str, reason: str,
display_name: str | None}`, token-gated, `RunRejectedError` → 409 with
the message (PR-C restructures), `GuardBusyError` → 409.

- [ ] **Step 4: Verify pass** (+ an API-level happy-path test in `tests/test_run_api.py` asserting 200 and the tombstone fields in the returned record)
- [ ] **Step 5: Commit** — `git commit -m "feat(escape): audited acknowledge-vanished under the RepoKey guard"`

---

### Task 8: `release-malformed` lock escape

**Files:**
- Modify: `dispatcher/core/run_store.py`, `dispatcher/server/app.py`
- Test: `tests/test_run_store.py`, `tests/test_run_api.py`

**Interfaces:**
- Produces: `RunStore.release_malformed_lock(key, *, actor, reason) ->
  dict` (the audit record); `POST /api/locks/release-malformed`, body
  `{repo_key: str, confirm_repo_key: str, reason: str, display_name:
  str | None}` — the lock *path* is computed server-side from the
  verified repo_key; a client never names a file.

- [ ] **Step 1: Failing tests**

```python
def test_release_malformed_quarantines_with_hash_and_identity(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    lock = tmp_path / "locks" / ("__".join(_KEY.as_path_parts()) + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"half-writ")
    audit = store.release_malformed_lock(_KEY, actor="local-unauthenticated",
                                         reason="crash residue")
    assert not lock.exists()
    quarantined = tmp_path / "locks" / "released" / audit["quarantined_as"]
    assert quarantined.read_bytes() == b"half-writ"
    assert audit["sha256"] == hashlib.sha256(b"half-writ").hexdigest()
    assert audit["inode"] and audit["size"] == 9


def test_release_malformed_refuses_a_healthy_lock(tmp_path: Path) -> None:
    """A healthy lock is released only by its owning transitions."""
    store = RunStore(tmp_path)
    store.reserve("rc-aaaaaaaa-11111111", _KEY, known_runs=[],
                  window_start="T0", work_id="w", revision="a" * 40)
    with pytest.raises(RunStoreError, match="parses"):
        store.release_malformed_lock(_KEY, actor="x", reason="r")


def test_release_malformed_refuses_on_io_error(tmp_path: Path, monkeypatch) -> None:
    """Quarantine on the word of a broken read could destroy a healthy
    lock — IO failure is lock_io_unreadable, never malformed."""
    # injected read error → RunStoreError mentioning unreadable
    ...


def test_the_guarded_race_cannot_quarantine_a_fresh_healthy_lock(tmp_path: Path) -> None:
    """The review scenario as RED against an unguarded implementation:
    hold the guard from another process (as in the guard tests), attempt
    release_malformed_lock → GuardBusyError, and the fresh lock written
    by the holder is untouched."""
    ...
```

- [ ] **Step 2: Verify failure**; **Step 3: Implement** —

```python
    def release_malformed_lock(self, key: RepoKey, *, actor: str, reason: str) -> dict:
        """Spec §8.3: quarantine is offered only where damage is PROVEN —
        the observed bytes are the damage. Runs inside the guard; identity
        (inode) is re-checked under it so the file moved is provably the
        file observed."""
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
                    "destroy a healthy lock") from err
            try:
                LockInfo.model_validate_json(data.decode(errors="replace"))
            except Exception:
                pass
            else:
                raise RunStoreError(
                    f"{key.as_text()}: the lock parses — a healthy lock is "
                    "released only by its owning transitions")
            released = self._state_dir / "locks" / "released"
            released.mkdir(parents=True, exist_ok=True, mode=0o700)
            name = f"{path.stem}.{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}.released"
            target = released / name
            os.rename(path, target)
            audit = {
                "repo_key": key.as_text(), "actor": actor, "reason": reason,
                "at": datetime.now(UTC).isoformat(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "inode": stat.st_ino, "size": stat.st_size,
                "quarantined_as": name,
            }
            (target.with_suffix(".audit.json")).write_text(json.dumps(audit))
            return audit
```

Endpoint: verify `confirm_repo_key == repo_key` (else 409 message
"retype"), resolve `RepoKey` by scanning config roots and comparing
`identity_from_checkout` — reject an unknown key; actor assigned as in
Task 7; token-gated.

- [ ] **Step 4: Verify pass**; full suite + ruff + pyrefly (explicit paths).
- [ ] **Step 5: Commit** — `git commit -m "feat(escape): guarded release-malformed with provable-identity quarantine"`

---

## After the plan

PR-B1 leaves the console UNCHANGED except that gate refusals surface in
the existing receipt's reason text. PR-B2 (after the vault contract)
wires the real parser and the item-level classifier; PR-C lifts refusals
to structured 409s and ships `/api/launchpad` + UI.

## Self-review

**Spec coverage:** §8.1 guard (T1) + preflight lock (T2); §8.2
fingerprint + reproducible rejection (T3); listing (T4); §5/§7 classifier
repo-half (T5) — item-half deferred to B2 with its parser, stated in T5's
interfaces; §7 gate + fail-closed (T6); §8.3 both escapes (T7, T8); §10's
B1-relevant tests distributed into their tasks (injected IO errors, race
RED, zero-reclassification replay, guard-busy zero-mutation).

**Placeholders:** the three `...`-elided arrange blocks in T6/T7/T8 name
exactly which earlier test to copy verbatim; no TBDs.

**Type consistency:** `LockInfo`/`Malformed` defined in T2 and consumed
in T5/T6/T8 with the same shapes; `fingerprint_of(repo_key, work_id,
revision)` used identically in T2/T3/T6; admission codes defined once in
T5 and used as prefixes in T6-T8.
