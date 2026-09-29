# Human Queue A1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `GET /api/human-queue` — every wait for a human that dispatcher can see from its own and local sources, with completeness stated per source.

**Architecture:** A pure `assemble()` over per-source `SourceResult`s holds every invariant (merge by key, ordering, completeness). Three adapters read dispatcher's `RunStore`, maestro's per-run `state.db` files (own full query, not the capped collector snapshot) and the existing impresario read model; two forge sources are listed as `not_connected`. Each adapter runs under an isolation guard, so one failing source becomes `unavailable` and never a 500.

**Tech Stack:** Python 3.12+, pydantic v2, FastAPI, sqlite3 (read-only URIs via `read_rows`), pytest + anyio + httpx ASGI transport.

**Spec:** `docs/superpowers/specs/2026-09-29-human-queue-a1-design.md` (parent: `docs/superpowers/specs/2026-09-29-human-control-plane-design.md`).

## Global Constraints

- Source names, verbatim: `dispatcher_runs`, `maestro`, `impresario`, `forge_labelled_prs`, `forge_candidate_prs`.
- Source states: `ok | partial | unavailable | not_configured | not_connected`; `complete` iff every source is `ok` or `not_configured`.
- Reasons: `launch_unknown`, `run_needs_review`, `run_awaiting_approval`, `loop_needs_human`, `proposal_gate`, `backlog_gate`.
- Keys: `dispatcher-run:{request_id}`, `maestro:{repo_key}:{run_id}:{task_id}:{status}`, `impresario-loop:{loop_id}:{iteration}`, `impresario-gate:{proposal_id}:{gate_id}:{version}`, `impresario-qg4:{backlog_id}:{version}`.
- `since` only from a proven wait start: `LaunchRecord.unknown_at`, `LoopWait.stopped_at`. Everything else null. Never mtime, creation time, or first-seen time.
- The endpoint always answers 200; unavailability is content.
- No write to any neighbour's state; the maestro read uses `read_rows` (read-only URI).
- Every commit step formats first: `uv run ruff format <changed files>` and the commit includes the formatter's changes; CI runs `ruff format --check` (`.github/workflows/ci.yml`).
- `classified_runs` changes only behind `report_missing_state=True`; launchpad, admission and the run controller call it without the flag and must behave exactly as before.
- Tooling: `uv run pytest`, `uv run ruff format .`, `uv run ruff check .`, `uv run pyrefly check dispatcher tests scripts` (explicit paths — a bare `pyrefly check` in a worktree under `.claude/` checks zero files). Line length 88. Async tests use anyio.

## Review Focus

- More than 50 waiting tasks in one run, or a waiting task in a non-newest run → all must appear; a `LIMIT` placed after the status filter must fail a test (Task 2, Task 4).
- A waiting task inside a finished run (`outcome` set) → must not appear (Task 4).
- One unreadable run DB, a failing task query on a readable DB, a run directory without `state.db`, an unlistable `runs/` → source `partial`, the healthy run still listed (Task 4).
- An adapter raising an unexpected exception → that source `unavailable`, HTTP 200, the other sources keep their waits (Task 6).
- `since` strings whose text order differs from their instant order (`10:00+02:00` vs `09:00Z`) → sorted by instant (Task 3).
- A legacy `launch_unknown` record (no `unknown_at`) marked unknown again → age stays unknown, not "now" (Task 1).

---

## File Structure

- Modify `dispatcher/core/run_store.py` — `LaunchRecord.unknown_at`, stamped by `mark_unknown`.
- Modify `dispatcher/core/collectors/maestro.py` — `waiting_tasks(db)`.
- Create `dispatcher/core/human_queue.py` — models, `proven_since`, `assemble`.
- Create `dispatcher/core/human_queue_sources.py` — adapters, forge placeholders, guard, `build_human_queue`.
- Modify `dispatcher/server/app.py` — route.
- Tests: `tests/test_run_store.py` (extend), `tests/test_human_queue.py`, `tests/test_human_queue_sources.py`, `tests/test_human_queue_api.py`.

---

### Task 1: `unknown_at` on launch records

**Files:**
- Modify: `dispatcher/core/run_store.py` (`LaunchRecord` fields; `mark_unknown` at ~line 697)
- Test: `tests/test_run_store.py`

**Interfaces:**
- Produces: `LaunchRecord.unknown_at: str | None` (UTC ISO-8601, set once by `RunStore.mark_unknown`).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_run_store.py`)

```python
def test_mark_unknown_stamps_when_the_wait_began(tmp_path: Path) -> None:
    from datetime import datetime

    store = _store(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    record = store.mark_unknown(_REQ, "no run appeared within the timeout")
    assert record.unknown_at is not None
    assert datetime.fromisoformat(record.unknown_at).tzinfo is not None
    stored = store.get(_REQ)
    assert stored is not None and stored.unknown_at == record.unknown_at


def test_repeated_mark_unknown_keeps_the_first_stamp(tmp_path: Path) -> None:
    """The wait did not restart; moving the stamp would make it look younger."""
    store = _store(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    first = store.mark_unknown(_REQ, "first")
    time.sleep(0.01)
    second = store.mark_unknown(_REQ, "second")
    assert second.unknown_at == first.unknown_at
    assert second.reason == "second"


def _strip_unknown_at(store: RunStore, request_id: str) -> None:
    """Make the record look written before `unknown_at` existed."""
    path = store._record_path(request_id)  # noqa: SLF001 — simulating an old record
    raw = json.loads(path.read_text())
    raw.pop("unknown_at")
    path.write_text(json.dumps(raw))


def test_record_written_before_unknown_at_reads_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_unknown(_REQ, "no run appeared within the timeout")
    _strip_unknown_at(store, _REQ)
    stored = store.get(_REQ)
    assert stored is not None and stored.unknown_at is None


def test_repeated_mark_on_a_legacy_record_does_not_invent_an_age(
    tmp_path: Path,
) -> None:
    """The record entered launch_unknown before the field existed; the time
    of a later repeat is not the start of the wait."""
    store = _store(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_unknown(_REQ, "first")
    _strip_unknown_at(store, _REQ)
    again = store.mark_unknown(_REQ, "second")
    assert again.unknown_at is None
    assert again.reason == "second"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_run_store.py -k "unknown_at or first_stamp or stamps_when or invent_an_age" -v`
Expected: FAIL — `AttributeError`/`KeyError` on `unknown_at`.

- [ ] **Step 3: Implement**

In `LaunchRecord`, after `prior_run_id`:

```python
    #: When the record entered `launch_unknown` (human-queue A1 spec §3.1) —
    #: the proven start of a human wait. Stamped once by `mark_unknown`;
    #: None on records written before this field existed. The file mtime is
    #: NOT a substitute: every write refreshes it.
    unknown_at: str | None = None
```

Replace `mark_unknown`:

```python
    def mark_unknown(self, request_id: str, reason: str) -> LaunchRecord:
        """The lock is deliberately NOT released (spec §5.2.1).

        Stamps `unknown_at` only on ENTRY into `launch_unknown`. A repeated
        mark keeps what the record has — including None on a record that
        entered the state before the field existed: the time of the repeat
        is not the start of the wait (human-queue A1 spec §3.1).
        """
        existing = self.get(request_id)
        if existing is None:
            raise RunStoreError(f"no launch record for {request_id}")
        stamp = (
            existing.unknown_at
            if existing.state == "launch_unknown"
            else datetime.now(UTC).isoformat()
        )
        return self._transition(
            request_id, state="launch_unknown", reason=reason, unknown_at=stamp
        )
```

- [ ] **Step 4: Run the run-store suite**

Run: `uv run pytest tests/test_run_store.py tests/test_run_controller.py tests/test_run_api.py -q`
Expected: PASS (existing callers of `mark_unknown` are unaffected: signature unchanged).

- [ ] **Step 5: Commit**

```bash
uv run ruff format dispatcher/core/run_store.py tests/test_run_store.py
git add dispatcher/core/run_store.py tests/test_run_store.py
git commit -m "feat(run-store): stamp unknown_at when a launch enters launch_unknown"
```

---

### Task 2: full waiting-task query and opt-in missing-state report

**Files:**
- Modify: `dispatcher/core/collectors/maestro.py` (constant beside `_TASKS_SQL`; `classified_runs` keyword; function after `classified_runs`)
- Test: `tests/test_human_queue_sources.py` (new file, first tests)

**Interfaces:**
- Produces: `waiting_tasks(db: Path) -> list[dict[str, object]]` — rows with keys `id, title, status, agent_type`, statuses only `needs_review | awaiting_approval`, ordered by `id`, no limit. Raises `SourceReadError` like `read_rows`.
- Produces: `classified_runs(home, snap, *, report_missing_state: bool = False)` — with the flag, each run directory lacking a regular `state.db` adds the warning `run <id>: no state.db in <dir> (in flight or damaged)`; without it, behaviour is byte-for-byte unchanged.

- [ ] **Step 1: Write the failing test** (`tests/test_human_queue_sources.py`)

```python
"""Human queue A1 adapters (spec 2026-09-29-human-queue-a1-design §3)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from conftest import make_maestro_run

from dispatcher.core.collectors.maestro import classified_runs, waiting_tasks
from dispatcher.core.models import ProjectSnapshot

_ACME = ("github.com", "acme", "app")


def _add_task(db: Path, task_id: str, status: str, created_at: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                f"title {task_id}",
                status,
                "claude_code",
                created_at,
                None,
                None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_waiting_tasks_has_no_limit_and_no_recency_window(tmp_path: Path) -> None:
    """The collector keeps the newest 50; the queue must keep every wait.

    60 waiting tasks, the oldest first, among 60 newer non-waiting ones: a
    LIMIT before OR after the status filter, or a recency order, fails this.
    """
    db = make_maestro_run(tmp_path, _ACME, "01RUN", started_at="2026-09-01T00:00:00")
    for i in range(60):
        status = "needs_review" if i % 2 == 0 else "awaiting_approval"
        _add_task(db, f"W-{i:03d}", status, f"2026-08-01T00:{i:02d}:00")
    for i in range(60):
        _add_task(db, f"N-{i:03d}", "in_progress", f"2026-09-02T00:{i:02d}:00")
    rows = waiting_tasks(db)
    assert [r["id"] for r in rows] == [f"W-{i:03d}" for i in range(60)]
    assert {r["status"] for r in rows} == {"needs_review", "awaiting_approval"}


def test_missing_state_is_reported_only_when_asked(tmp_path: Path) -> None:
    make_maestro_run(tmp_path, _ACME, "01GOOD", started_at="2026-09-01T00:00:00")
    tmp_path.joinpath("projects", *_ACME, "runs", "01BARE").mkdir(parents=True)

    silent = ProjectSnapshot(name="maestro", path="")
    runs = classified_runs(tmp_path, silent)
    assert [info.run_id for info, _ in runs] == ["01GOOD"]
    assert silent.warnings == []  # every existing caller: unchanged

    loud = ProjectSnapshot(name="maestro", path="")
    runs = classified_runs(tmp_path, loud, report_missing_state=True)
    assert [info.run_id for info, _ in runs] == ["01GOOD"]
    assert len(loud.warnings) == 1
    assert loud.warnings[0].startswith("run 01BARE: no state.db")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_human_queue_sources.py -v`
Expected: FAIL — `ImportError: cannot import name 'waiting_tasks'`.

(`make_maestro_run(home, …)` takes the maestro home; the tests above pass `tmp_path` as that home, so run directories live under `tmp_path/projects/…`.)

- [ ] **Step 3: Implement** (in `collectors/maestro.py`)

After `_TASKS_SQL`:

```python
# The human queue's read (human-queue A1 spec §3.2): every waiting task, no
# recency window and no limit — `_TASKS_SQL` is a panel's newest-50 and
# would drop exactly the oldest waits.
_WAITING_TASKS_SQL = (
    "SELECT id, title, status, agent_type FROM tasks "
    "WHERE status IN ('needs_review', 'awaiting_approval') ORDER BY id"
)
```

Change `classified_runs` — signature and the `state.db` check only:

```python
def classified_runs(
    home: Path | None,
    snap: ProjectSnapshot,
    *,
    report_missing_state: bool = False,
) -> list[tuple[OrchestrationRunInfo, Path]]:
```

Add to its docstring:

```
    `report_missing_state` (human-queue A1 spec §3.2): a run directory
    without `state.db` is skipped silently by default, because the control
    plane models it as a transient in-flight launch (`RUN_IN_FLIGHT`) and
    must keep doing so. The human queue passes True: for it the directory
    is a wait it cannot read, and silence would shorten the queue.
```

and replace the loop body's check:

```python
            db = run_dir / "state.db"
            if not db.is_file():
                if report_missing_state:
                    snap.warnings.append(
                        f"run {run_dir.name}: no state.db in {run_dir} "
                        "(in flight or damaged)"
                    )
                continue
```

After `classified_runs`:

```python
def waiting_tasks(db: Path) -> list[dict[str, object]]:
    """Every task in one run DB that waits for a human.

    Raises `SourceReadError` on any read failure — the caller decides how
    that degrades; an unreadable DB must never read as "nothing waits".
    """
    return read_rows(db, _WAITING_TASKS_SQL)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_human_queue_sources.py tests/test_maestro.py tests/test_run_controller.py tests/test_launchpad_assembler.py tests/test_admission.py -q`
Expected: PASS — the last three prove the default path did not move.

- [ ] **Step 5: Commit**

```bash
uv run ruff format dispatcher/core/collectors/maestro.py tests/test_human_queue_sources.py
git add dispatcher/core/collectors/maestro.py tests/test_human_queue_sources.py
git commit -m "feat(maestro): waiting_tasks and opt-in missing-state report for the queue"
```

---

### Task 3: queue model and `assemble`

**Files:**
- Create: `dispatcher/core/human_queue.py`
- Test: `tests/test_human_queue.py`

**Interfaces:**
- Produces (all in `dispatcher.core.human_queue`): `SourceState`, `SourceStatus`, `Reason`, `RunViewAct`, `MaestroVerbAct`, `OpenArtifactAct`, `Act`, `HumanWait`, `HumanQueueView`, `SourceResult(name: str, status: SourceStatus, waits: list[HumanWait])`, `proven_since(value: str | None, basis: str) -> tuple[str | None, str | None]`, `assemble(results: list[SourceResult], *, now: str) -> HumanQueueView`.

- [ ] **Step 1: Write the failing tests** (`tests/test_human_queue.py`)

```python
"""Human queue model invariants (spec 2026-09-29-human-queue-a1-design §4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dispatcher.core.human_queue import (
    HumanWait,
    OpenArtifactAct,
    Reason,
    SourceResult,
    SourceStatus,
    assemble,
    proven_since,
)

_NOW = "2026-09-29T12:00:00+00:00"


def _wait(
    key: str, since: str | None = None, reason: Reason = "proposal_gate"
) -> HumanWait:
    return HumanWait(
        key=key,
        reasons=[reason],
        source="impresario",
        repo="impresario",
        ref=key,
        title=key,
        since=since,
        since_basis=None if since is None else "test",
        act=OpenArtifactAct(path=f"{key}.yaml"),
    )


def _ok(name: str, *waits: HumanWait) -> SourceResult:
    return SourceResult(name=name, status=SourceStatus(state="ok"), waits=list(waits))


def test_known_ages_first_oldest_first_then_unknown_by_key() -> None:
    view = assemble(
        [
            _ok(
                "impresario",
                _wait("b-unknown"),
                _wait("new", "2026-09-28T00:00:00+00:00"),
                _wait("a-unknown"),
                _wait("old", "2026-09-01T00:00:00Z"),
            )
        ],
        now=_NOW,
    )
    assert [w.key for w in view.waits] == ["old", "new", "a-unknown", "b-unknown"]


def test_since_sorts_by_instant_not_text() -> None:
    """'10:00+02:00' is 08:00Z — earlier than '09:00Z', though it sorts
    after it as text. A string sort puts 'utc' first and fails."""
    view = assemble(
        [
            _ok(
                "impresario",
                _wait("utc", "2026-09-01T09:00:00Z"),
                _wait("plus2", "2026-09-01T10:00:00+02:00"),
            )
        ],
        now=_NOW,
    )
    assert [w.key for w in view.waits] == ["plus2", "utc"]


def test_same_key_merges_and_unions_reasons() -> None:
    first = _wait("pr:7", reason="proposal_gate")
    second = _wait("pr:7", reason="backlog_gate")
    view = assemble([_ok("a", first), _ok("b", second)], now=_NOW)
    assert len(view.waits) == 1
    assert view.waits[0].reasons == ["backlog_gate", "proposal_gate"]
    assert view.waits[0].source == "impresario"


@pytest.mark.parametrize(
    ("states", "complete"),
    [
        (["ok", "not_configured"], True),
        (["ok", "partial"], False),
        (["ok", "unavailable"], False),
        (["ok", "not_connected"], False),
    ],
)
def test_complete_only_when_every_source_is_ok_or_off(
    states: list[str], complete: bool
) -> None:
    results = [
        SourceResult(name=f"s{i}", status=SourceStatus.model_validate({"state": s}))
        for i, s in enumerate(states)
    ]
    assert assemble(results, now=_NOW).complete is complete


def test_since_and_basis_are_paired() -> None:
    with pytest.raises(ValidationError):
        HumanWait(
            key="k",
            reasons=["proposal_gate"],
            source="impresario",
            repo=None,
            ref="k",
            title="k",
            since="2026-09-01T00:00:00Z",
            since_basis=None,
            act=OpenArtifactAct(path="p"),
        )


def test_naive_since_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _wait("k", "2026-09-01T00:00:00")


def test_proven_since_normalizes_or_drops() -> None:
    assert proven_since("2026-09-01T00:00:00Z", "b") == (
        "2026-09-01T00:00:00+00:00",
        "b",
    )
    assert proven_since(None, "b") == (None, None)
    assert proven_since("", "b") == (None, None)
    assert proven_since("not a time", "b") == (None, None)
    assert proven_since("2026-09-01T00:00:00", "b") == (None, None)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_human_queue.py -v`
Expected: FAIL — `ModuleNotFoundError: dispatcher.core.human_queue`.

- [ ] **Step 3: Implement** (`dispatcher/core/human_queue.py`)

```python
"""Human queue — every wait for a human dispatcher can see (slice A1).

Spec: docs/superpowers/specs/2026-09-29-human-queue-a1-design.md. This module
holds the model and `assemble`, which owns every invariant of spec §4.1:
merge by key, ordering, completeness. It reads nothing — the adapters in
`human_queue_sources` do.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

SourceState = Literal[
    "ok", "partial", "unavailable", "not_configured", "not_connected"
]
Reason = Literal[
    "launch_unknown",
    "run_needs_review",
    "run_awaiting_approval",
    "loop_needs_human",
    "proposal_gate",
    "backlog_gate",
]

# `not_configured` = the feature is off, so nothing can wait there; it is not
# incompleteness. Every other non-ok state is.
_COMPLETE_STATES = frozenset({"ok", "not_configured"})


class SourceStatus(BaseModel):
    state: SourceState
    detail: str | None = None


class RunViewAct(BaseModel):
    """Open dispatcher's own run view (where `/resolve` and verbs live)."""

    kind: Literal["run_view"] = "run_view"
    request_id: str


class MaestroVerbAct(BaseModel):
    """A maestro verb for a run dispatcher did not launch; B renders it."""

    kind: Literal["maestro_verb"] = "maestro_verb"
    verb: Literal["retry", "approve"]
    task_id: str
    run_id: str
    repo_key: str


class OpenArtifactAct(BaseModel):
    """The decision is recorded at the source; dispatcher only points to it."""

    kind: Literal["open_artifact"] = "open_artifact"
    path: str


Act = Annotated[
    RunViewAct | MaestroVerbAct | OpenArtifactAct, Field(discriminator="kind")
]


class HumanWait(BaseModel):
    key: str
    reasons: list[Reason]
    source: str
    repo: str | None
    ref: str
    title: str
    since: str | None
    since_basis: str | None
    act: Act

    @model_validator(mode="after")
    def _since_is_proven_and_sortable(self) -> HumanWait:
        if (self.since is None) != (self.since_basis is None):
            raise ValueError("since and since_basis are both set or both null")
        if self.since is not None:
            if datetime.fromisoformat(self.since).tzinfo is None:
                raise ValueError("since must carry a timezone")
        return self


class HumanQueueView(BaseModel):
    waits: list[HumanWait]
    sources: dict[str, SourceStatus]
    complete: bool
    generated_at: str


class SourceResult(BaseModel):
    name: str
    status: SourceStatus
    waits: list[HumanWait] = Field(default_factory=list)


def proven_since(value: str | None, basis: str) -> tuple[str | None, str | None]:
    """`(since, since_basis)` from a source time, or `(None, None)`.

    A value that is empty, unparseable, or has no timezone cannot be ordered
    against the others; it is dropped to "age unknown" rather than guessed.
    """
    if not value:
        return None, None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None, None
    if parsed.tzinfo is None:
        return None, None
    return parsed.isoformat(), basis


def assemble(results: list[SourceResult], *, now: str) -> HumanQueueView:
    """One view over all sources (spec §4.1)."""
    merged: dict[str, HumanWait] = {}
    for result in results:
        for wait in result.waits:
            prior = merged.get(wait.key)
            if prior is None:
                merged[wait.key] = wait
                continue
            union = sorted(set(prior.reasons) | set(wait.reasons))
            merged[wait.key] = prior.model_copy(update={"reasons": union})
    known = sorted(
        (w for w in merged.values() if w.since is not None),
        key=lambda w: (datetime.fromisoformat(w.since or ""), w.key),
    )
    unknown = sorted(
        (w for w in merged.values() if w.since is None), key=lambda w: w.key
    )
    sources = {r.name: r.status for r in results}
    return HumanQueueView(
        waits=[*known, *unknown],
        sources=sources,
        complete=all(s.state in _COMPLETE_STATES for s in sources.values()),
        generated_at=now,
    )
```

- [ ] **Step 4: Run tests and type check**

Run: `uv run pytest tests/test_human_queue.py -v && uv run pyrefly check dispatcher tests scripts`
Expected: PASS; pyrefly 0 errors (fix any it reports before committing).

- [ ] **Step 5: Commit**

```bash
uv run ruff format dispatcher/core/human_queue.py tests/test_human_queue.py
git add dispatcher/core/human_queue.py tests/test_human_queue.py
git commit -m "feat(human-queue): model, proven_since and assemble"
```

---

### Task 4: `dispatcher_runs` and `maestro` adapters

**Files:**
- Create: `dispatcher/core/human_queue_sources.py`
- Test: `tests/test_human_queue_sources.py` (extend)

**Interfaces:**
- Consumes: Task 1 `LaunchRecord.unknown_at`; Task 2 `waiting_tasks`; Task 3 models and `proven_since`; existing `classified_runs(home, snap)`, `RunStore.list()`, `DispatcherConfig.run_state_dir`.
- Produces: `from_run_store(config: DispatcherConfig) -> SourceResult`; `from_maestro(home: Path, records: list[LaunchRecord]) -> SourceResult`; `launch_records(config: DispatcherConfig) -> list[LaunchRecord]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_human_queue_sources.py`; merge the imports into the file's import block)

```python
import json
import os

import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.human_queue_sources import (
    from_maestro,
    from_run_store,
    launch_records,
)
from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_store import RunStore

_REQ = "11111111-1111-4111-8111-111111111111"
_OLD_REQ = "22222222-2222-4222-8222-222222222222"
_KEY = RepoKey(host="github.com", owner="acme", repo="app")


def _config(tmp_path: Path, *, control_plane: bool = True) -> DispatcherConfig:
    return DispatcherConfig(
        roots=(tmp_path / "ws",),
        maestro_home=tmp_path / "mhome",
        run_state_dir=tmp_path / "state" if control_plane else None,
    )


# -- dispatcher_runs ---------------------------------------------------------


def test_run_store_off_is_not_configured(tmp_path: Path) -> None:
    result = from_run_store(_config(tmp_path, control_plane=False))
    assert result.status.state == "not_configured"
    assert result.waits == []


def test_launch_unknown_is_a_wait_aged_by_unknown_at(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t", work_id="w1")
    record = store.mark_unknown(_REQ, "no run appeared")
    result = from_run_store(config)
    assert result.status.state == "ok"
    [wait] = result.waits
    assert wait.key == f"dispatcher-run:{_REQ}"
    assert wait.reasons == ["launch_unknown"]
    assert wait.since == record.unknown_at
    assert wait.since_basis is not None
    assert wait.act.kind == "run_view"


def test_record_without_unknown_at_has_unknown_age(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    store.reserve(_OLD_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_unknown(_OLD_REQ, "no run appeared")
    path = store._record_path(_OLD_REQ)  # noqa: SLF001 — simulating an old record
    raw = json.loads(path.read_text())
    raw.pop("unknown_at")
    path.write_text(json.dumps(raw))
    [wait] = from_run_store(config).waits
    assert (wait.since, wait.since_basis) == (None, None)


def test_unreadable_record_makes_source_partial(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_unknown(_REQ, "no run appeared")
    (config.run_state_dir / "requests" / "broken.json").write_text("{not json")
    result = from_run_store(config)
    assert result.status.state == "partial"
    assert "broken.json" in (result.status.detail or "")
    assert len(result.waits) == 1


# -- maestro -----------------------------------------------------------------


def test_waits_from_every_non_terminal_run_not_only_the_newest(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    old = make_maestro_run(home, _ACME, "01OLD", started_at="2026-09-01T00:00:00")
    _add_task(old, "T-old", "needs_review", "2026-09-01T00:00:00")
    new = make_maestro_run(home, _ACME, "01NEW", started_at="2026-09-10T00:00:00")
    _add_task(new, "T-000", "awaiting_approval", "2026-08-01T00:00:00")
    for i in range(1, 60):
        _add_task(new, f"T-{i:03d}", "in_progress", f"2026-09-10T00:{i:02d}:00")
    result = from_maestro(home, [])
    assert result.status.state == "ok"
    assert sorted(w.key for w in result.waits) == [
        "maestro:github.com/acme/app:01NEW:T-000:awaiting_approval",
        "maestro:github.com/acme/app:01OLD:T-old:needs_review",
    ]
    by_ref = {w.ref: w for w in result.waits}
    assert by_ref["01OLD/T-old"].reasons == ["run_needs_review"]
    assert by_ref["01OLD/T-old"].act.model_dump() == {
        "kind": "maestro_verb",
        "verb": "retry",
        "task_id": "T-old",
        "run_id": "01OLD",
        "repo_key": "github.com/acme/app",
    }
    assert by_ref["01NEW/T-000"].act.model_dump()["verb"] == "approve"
    assert all(w.since is None for w in result.waits)


def test_waiting_task_in_a_finished_run_is_not_a_wait(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    db = make_maestro_run(
        home,
        _ACME,
        "01DONE",
        started_at="2026-09-01T00:00:00",
        outcome="completed",
        ended_at="2026-09-01T01:00:00",
    )
    _add_task(db, "T-1", "needs_review", "2026-09-01T00:00:00")
    result = from_maestro(home, [])
    assert result.status.state == "ok"
    assert result.waits == []


def test_unreadable_run_db_is_partial_others_still_listed(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    good = make_maestro_run(home, _ACME, "01GOOD", started_at="2026-09-01T00:00:00")
    _add_task(good, "T-1", "needs_review", "2026-09-01T00:00:00")
    bad = home.joinpath("projects", *_ACME, "runs", "01BAD", "state.db")
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a database")
    result = from_maestro(home, [])
    assert result.status.state == "partial"
    assert "01BAD" in (result.status.detail or "")
    assert [w.ref for w in result.waits] == ["01GOOD/T-1"]


def test_failing_task_query_on_a_readable_run_is_partial(tmp_path: Path) -> None:
    """The `run` row reads (run classified), the task query does not."""
    home = tmp_path / "mhome"
    good = make_maestro_run(home, _ACME, "01GOOD", started_at="2026-09-01T00:00:00")
    _add_task(good, "T-1", "needs_review", "2026-09-01T00:00:00")
    broken = make_maestro_run(home, _ACME, "01NOTASKS", started_at="2026-09-02T00:00:00")
    conn = sqlite3.connect(broken)
    try:
        conn.execute("DROP TABLE tasks")
        conn.commit()
    finally:
        conn.close()
    result = from_maestro(home, [])
    assert result.status.state == "partial"
    assert "01NOTASKS" in (result.status.detail or "")
    assert [w.ref for w in result.waits] == ["01GOOD/T-1"]


def test_run_dir_without_state_db_is_partial(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    good = make_maestro_run(home, _ACME, "01GOOD", started_at="2026-09-01T00:00:00")
    _add_task(good, "T-1", "needs_review", "2026-09-01T00:00:00")
    home.joinpath("projects", *_ACME, "runs", "01BARE").mkdir()
    result = from_maestro(home, [])
    assert result.status.state == "partial"
    assert "01BARE" in (result.status.detail or "")
    assert [w.ref for w in result.waits] == ["01GOOD/T-1"]


def test_unlistable_runs_dir_is_partial(tmp_path: Path) -> None:
    """An enumeration warning from classified_runs is a hole in the source."""
    home = tmp_path / "mhome"
    good = make_maestro_run(home, _ACME, "01GOOD", started_at="2026-09-01T00:00:00")
    _add_task(good, "T-1", "needs_review", "2026-09-01T00:00:00")
    other = ("github.com", "acme", "locked")
    make_maestro_run(home, other, "01HIDDEN", started_at="2026-09-01T00:00:00")
    runs_dir = home.joinpath("projects", *other, "runs")
    runs_dir.chmod(0)
    try:
        if os.access(runs_dir, os.R_OK):
            pytest.skip("running as a user that ignores directory permissions")
        result = from_maestro(home, [])
    finally:
        runs_dir.chmod(0o755)
    assert result.status.state == "partial"
    assert "cannot list" in (result.status.detail or "")
    assert [w.ref for w in result.waits] == ["01GOOD/T-1"]


def test_absent_maestro_home_is_a_clean_zero(tmp_path: Path) -> None:
    result = from_maestro(tmp_path / "nowhere", [])
    assert result.status.state == "ok"
    assert result.waits == []


def test_run_launched_by_dispatcher_routes_to_run_view(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    home = tmp_path / "mhome"
    db = make_maestro_run(home, _ACME, "01RUN", started_at="2026-09-01T00:00:00")
    _add_task(db, "T-1", "needs_review", "2026-09-01T00:00:00")
    store = RunStore(config.run_state_dir)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_materialized(_REQ, "01RUN")
    [wait] = from_maestro(home, launch_records(config)).waits
    assert wait.act.model_dump() == {"kind": "run_view", "request_id": _REQ}
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_human_queue_sources.py -v`
Expected: FAIL — `ModuleNotFoundError: dispatcher.core.human_queue_sources`.

- [ ] **Step 3: Implement** (`dispatcher/core/human_queue_sources.py`)

```python
"""Human queue A1 adapters — one per source (spec 2026-09-29-human-queue-a1 §3).

Each adapter reads one source and returns a `SourceResult`; none of them
orders, merges, or decides completeness — `human_queue.assemble` does.
Degradation rule for every adapter: what could not be read is named in the
source's status, never absorbed into a shorter list.
"""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.collectors.base import SourceReadError, coerce_str
from dispatcher.core.collectors.maestro import classified_runs, waiting_tasks
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.human_queue import (
    HumanWait,
    MaestroVerbAct,
    Reason,
    RunViewAct,
    SourceResult,
    SourceStatus,
    proven_since,
)
from dispatcher.core.models import OrchestrationRunInfo, ProjectSnapshot
from dispatcher.core.run_store import LaunchRecord, RunStore

_UNKNOWN_BASIS = "dispatcher launch record: entered launch_unknown"

# maestro run statuses that can still hold an actionable wait (spec §3.2);
# a run with an `outcome` is over.
_NON_TERMINAL = frozenset({"running", "suspended", "interrupted"})

# task status → (reason, the slice-0 verb that clears it — spec §6 there)
_MAESTRO_WAITS: dict[str, tuple[Reason, str]] = {
    "needs_review": ("run_needs_review", "retry"),
    "awaiting_approval": ("run_awaiting_approval", "approve"),
}


def _status(problems: list[str]) -> SourceStatus:
    if problems:
        return SourceStatus(state="partial", detail="; ".join(problems))
    return SourceStatus(state="ok")


def launch_records(config: DispatcherConfig) -> list[LaunchRecord]:
    """Readable launch records, or [] when the control plane is off."""
    if config.run_state_dir is None:
        return []
    records, _ = RunStore(config.run_state_dir).list()
    return records


def from_run_store(config: DispatcherConfig) -> SourceResult:
    """`launch_unknown` records (spec §3.1)."""
    if config.run_state_dir is None:
        return SourceResult(
            name="dispatcher_runs",
            status=SourceStatus(
                state="not_configured", detail="run_state_dir is not set"
            ),
        )
    records, unreadable = RunStore(config.run_state_dir).list()
    problems = [f"unreadable: {', '.join(unreadable)}"] if unreadable else []
    waits = [_launch_unknown_wait(r) for r in records if r.state == "launch_unknown"]
    return SourceResult(name="dispatcher_runs", status=_status(problems), waits=waits)


def _launch_unknown_wait(record: LaunchRecord) -> HumanWait:
    since, basis = proven_since(record.unknown_at, _UNKNOWN_BASIS)
    return HumanWait(
        key=f"dispatcher-run:{record.request_id}",
        reasons=["launch_unknown"],
        source="dispatcher_runs",
        repo=record.repo_key or None,
        ref=record.request_id,
        title=f"launch outcome unknown: {record.work_id or record.request_id}",
        since=since,
        since_basis=basis,
        act=RunViewAct(request_id=record.request_id),
    )


def from_maestro(home: Path, records: list[LaunchRecord]) -> SourceResult:
    """Waiting tasks of every non-terminal run (spec §3.2)."""
    snap = ProjectSnapshot(name="maestro", path=str(home))
    runs = classified_runs(home, snap, report_missing_state=True)
    # classified_runs reports enumeration failures, unreadable runs and (with
    # the flag) run directories without state.db as warnings on the
    # snapshot; each one is a hole in this source.
    problems = list(snap.warnings)
    launched: dict[tuple[str, str], str] = {
        (r.repo_key, r.run_id): r.request_id for r in records if r.run_id
    }
    waits: list[HumanWait] = []
    for info, db in runs:
        if info.status not in _NON_TERMINAL:
            continue
        try:
            rows = waiting_tasks(db)
        except SourceReadError as err:
            problems.append(f"run {info.run_id}: {err}")
            continue
        waits.extend(_maestro_wait(info, row, launched) for row in rows)
    return SourceResult(name="maestro", status=_status(problems), waits=waits)


def _maestro_wait(
    info: OrchestrationRunInfo,
    row: dict[str, object],
    launched: dict[tuple[str, str], str],
) -> HumanWait:
    status = coerce_str(row["status"])
    reason, verb = _MAESTRO_WAITS[status]
    run_id = info.run_id or ""
    task_id = coerce_str(row["id"])
    request_id = launched.get((info.repo_key, run_id))
    act: RunViewAct | MaestroVerbAct = (
        RunViewAct(request_id=request_id)
        if request_id
        else MaestroVerbAct(
            verb="retry" if verb == "retry" else "approve",
            task_id=task_id,
            run_id=run_id,
            repo_key=info.repo_key,
        )
    )
    return HumanWait(
        key=f"maestro:{info.repo_key}:{run_id}:{task_id}:{status}",
        reasons=[reason],
        source="maestro",
        repo=info.repo_key,
        ref=f"{run_id}/{task_id}",
        title=f"{row['title']} [{row['agent_type']}]",
        since=None,
        since_basis=None,
        act=act,
    )
```

Before running, confirm two assumptions against the code (both hold at the time of writing; if either changed, adjust and note it in the commit message):
- `coerce_str` exists in `dispatcher/core/collectors/base.py` and returns `str` for any value (it is already imported by `collectors/maestro.py`).
- `OrchestrationRunInfo.repo_key` is `str` (not optional) in `dispatcher/core/models.py`.

- [ ] **Step 4: Run tests, lint and types**

Run: `uv run pytest tests/test_human_queue_sources.py tests/test_human_queue.py -v && uv run ruff check . && uv run pyrefly check dispatcher tests scripts`
Expected: PASS, no lint or type errors.

- [ ] **Step 5: Commit**

```bash
uv run ruff format dispatcher/core/human_queue_sources.py tests/test_human_queue_sources.py
git add dispatcher/core/human_queue_sources.py tests/test_human_queue_sources.py
git commit -m "feat(human-queue): dispatcher_runs and maestro adapters"
```

---

### Task 5: `impresario` adapter

**Files:**
- Modify: `dispatcher/core/human_queue_sources.py`
- Test: `tests/test_human_queue_sources.py` (extend)

**Interfaces:**
- Consumes: `ProductProposalsReport`, `LoopWait`, `GateWait`, `BacklogWait`, `ProposalBundle`, `BacklogBundle`, `Diagnostic` from `dispatcher.core.product_proposals`; `read_api.product_proposals(cache, name)`, `read_api.ReadLookupError`; `SnapshotService` from `dispatcher.core.service`.
- Produces: `from_impresario_report(report: ProductProposalsReport) -> SourceResult`; `from_impresario(cache: SnapshotService) -> SourceResult`.

- [ ] **Step 1: Write the failing tests** (append; merge imports)

```python
from dispatcher.core.human_queue_sources import from_impresario_report
from dispatcher.core.product_proposals import (
    BacklogWait,
    Diagnostic,
    GateWait,
    LoopWait,
    ProductProposalsReport,
    ProposalBundle,
)


def _report(**kw: object) -> ProductProposalsReport:
    return ProductProposalsReport(mirror_path="/m", **kw)  # type: ignore[arg-type]


_LOOP = LoopWait(
    loop_id="L1",
    iteration=3,
    proposal_id="PP-1",
    reason="creator stuck",
    stopped_at="2026-09-20T08:00:00Z",
    bundle_path="pilot/pp-1",
)
_GATE = GateWait(
    proposal_id="PP-2",
    gate_id="qg5_business",
    gate_label="Gate A",
    authority="business_owner",
    artifact_ref="proposal://PP-2",
    bundle_path="pilot/pp-2",
    version=4,
    proposal_updated_at="2026-09-21T00:00:00Z",
)
_BACKLOG = BacklogWait(
    backlog_id="BL-1",
    artifact_ref="backlog://BL-1",
    artifact_path="backlogs/bl-1/backlog.yaml",
    version=2,
    backlog_updated_at="2026-09-22T00:00:00Z",
)


def test_impresario_waits_and_their_ages() -> None:
    result = from_impresario_report(
        _report(needs_human=[_LOOP], waits=[_GATE], backlog_waits=[_BACKLOG])
    )
    assert result.status.state == "ok"
    by_key = {w.key: w for w in result.waits}
    loop = by_key["impresario-loop:L1:3"]
    assert loop.reasons == ["loop_needs_human"]
    assert loop.since == "2026-09-20T08:00:00+00:00"
    gate = by_key["impresario-gate:PP-2:qg5_business:4"]
    assert (gate.since, gate.since_basis) == (None, None)
    assert gate.act.model_dump() == {"kind": "open_artifact", "path": "pilot/pp-2"}
    backlog = by_key["impresario-qg4:BL-1:2"]
    assert backlog.reasons == ["backlog_gate"]
    assert (backlog.since, backlog.since_basis) == (None, None)


def test_non_ok_bundle_makes_source_partial_keeps_ok_waits() -> None:
    bad = ProposalBundle(path="pilot/pp-9", state="unreadable")
    result = from_impresario_report(
        _report(needs_human=[_LOOP], bundles=[bad], attention=True)
    )
    assert result.status.state == "partial"
    assert "pilot/pp-9" in (result.status.detail or "")
    assert [w.key for w in result.waits] == ["impresario-loop:L1:3"]


@pytest.mark.parametrize(
    ("code", "state"),
    [
        # SnapshotService lists every collector; an unobserved impresario
        # arrives as this diagnostic, not as a lookup error (spec §3.3).
        ("mirror-not-detected", "not_configured"),
        # Detected, then its anchors vanished before the scan.
        ("mirror-anchors-missing", "unavailable"),
    ],
)
def test_unscanned_mirror(code: str, state: str) -> None:
    result = from_impresario_report(
        _report(diagnostics=[Diagnostic(code=code, message="m")], attention=True)
    )
    assert result.status.state == state
    assert result.waits == []
```

(Add `import pytest` to the file's imports if not present.)

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_human_queue_sources.py -k impresario -v`
Expected: FAIL — `ImportError: cannot import name 'from_impresario_report'`.

- [ ] **Step 3: Implement** (append to `human_queue_sources.py`; merge imports at the top)

```python
from dispatcher.core import read_api
from dispatcher.core.human_queue import OpenArtifactAct
from dispatcher.core.product_proposals import (
    BacklogWait,
    GateWait,
    LoopWait,
    ProductProposalsReport,
)
from dispatcher.core.service import SnapshotService

_LOOP_BASIS = "impresario loop stop.at"
_NOT_OBSERVED = SourceResult(
    name="impresario",
    status=SourceStatus(state="not_configured", detail="no impresario mirror observed"),
)


def from_impresario(cache: SnapshotService) -> SourceResult:
    """The existing impresario read model, re-shaped (spec §3.3)."""
    try:
        report = read_api.product_proposals(cache, "impresario")
    except read_api.ReadLookupError:
        # Unreachable in practice: SnapshotService lists every collector.
        return _NOT_OBSERVED
    return from_impresario_report(report)


def from_impresario_report(report: ProductProposalsReport) -> SourceResult:
    codes = sorted({d.code for d in report.diagnostics})
    if "mirror-not-detected" in codes:
        return _NOT_OBSERVED
    if "mirror-anchors-missing" in codes:
        return SourceResult(
            name="impresario",
            status=SourceStatus(state="unavailable", detail=", ".join(codes)),
        )
    waits = [
        *(_loop_wait(w) for w in report.needs_human),
        *(_gate_wait(w) for w in report.waits),
        *(_backlog_wait(w) for w in report.backlog_waits),
    ]
    problems: list[str] = []
    if report.attention:
        bad = [b.path for b in report.bundles if b.state != "ok"]
        bad += [b.path for b in report.backlog_bundles if b.state != "ok"]
        if bad:
            problems.append(f"non-ok bundles: {', '.join(bad)}")
        if codes:
            problems.append(f"diagnostics: {', '.join(codes)}")
        if not problems:
            problems.append("report flagged attention")
    return SourceResult(name="impresario", status=_status(problems), waits=waits)


def _loop_wait(wait: LoopWait) -> HumanWait:
    since, basis = proven_since(wait.stopped_at, _LOOP_BASIS)
    return HumanWait(
        key=f"impresario-loop:{wait.loop_id}:{wait.iteration}",
        reasons=["loop_needs_human"],
        source="impresario",
        repo="impresario",
        ref=wait.loop_id,
        title=f"{wait.proposal_id}: {wait.reason}",
        since=since,
        since_basis=basis,
        act=OpenArtifactAct(path=wait.bundle_path),
    )


def _gate_wait(wait: GateWait) -> HumanWait:
    # proposal_updated_at is NOT a wait start (core/product_proposals.py:129).
    return HumanWait(
        key=f"impresario-gate:{wait.proposal_id}:{wait.gate_id}:{wait.version}",
        reasons=["proposal_gate"],
        source="impresario",
        repo="impresario",
        ref=wait.proposal_id,
        title=f"{wait.proposal_id}: {wait.gate_label} ({wait.authority})",
        since=None,
        since_basis=None,
        act=OpenArtifactAct(path=wait.bundle_path),
    )


def _backlog_wait(wait: BacklogWait) -> HumanWait:
    # backlog_updated_at is left out until spec §8 question 1 is answered.
    return HumanWait(
        key=f"impresario-qg4:{wait.backlog_id}:{wait.version}",
        reasons=["backlog_gate"],
        source="impresario",
        repo="impresario",
        ref=wait.backlog_id,
        title=f"{wait.backlog_id}: {wait.gate_label} ({wait.authority})",
        since=None,
        since_basis=None,
        act=OpenArtifactAct(path=wait.artifact_path),
    )
```

Check before running: `dispatcher.core.read_api` must not import `human_queue_sources` (no cycle) — `grep -n "human_queue" dispatcher/core/read_api.py` must print nothing.

- [ ] **Step 4: Run tests, lint and types**

Run: `uv run pytest tests/test_human_queue_sources.py -v && uv run ruff check . && uv run pyrefly check dispatcher tests scripts`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
uv run ruff format dispatcher/core/human_queue_sources.py tests/test_human_queue_sources.py
git add dispatcher/core/human_queue_sources.py tests/test_human_queue_sources.py
git commit -m "feat(human-queue): impresario adapter over the existing read model"
```

---

### Task 6: `build_human_queue`, forge placeholders, route

**Files:**
- Modify: `dispatcher/core/human_queue_sources.py`
- Modify: `dispatcher/server/app.py` (import block; route next to `/api/waits`, ~line 463)
- Test: `tests/test_human_queue_api.py`

**Interfaces:**
- Consumes: Tasks 3–5.
- Produces: `build_human_queue(config: DispatcherConfig, cache: SnapshotService, *, now: str) -> HumanQueueView`; `GET /api/human-queue`.

- [ ] **Step 1: Write the failing tests** (`tests/test_human_queue_api.py`)

```python
"""HTTP surface of the human queue (spec 2026-09-29-human-queue-a1 §4.1, §6)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import httpx
import pytest
from conftest import make_maestro_run

from dispatcher.core import human_queue_sources
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_store import RunStore
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio

_REQ = "11111111-1111-4111-8111-111111111111"
_ACME = ("github.com", "acme", "app")


def _config(tmp_path: Path) -> DispatcherConfig:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return DispatcherConfig(
        roots=(ws,),
        maestro_home=tmp_path / "mhome",
        run_state_dir=tmp_path / "state",
    )


def _client(config: DispatcherConfig) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_app(config))
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_queue_over_local_sources_is_partial_by_construction(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    store.reserve(
        _REQ,
        RepoKey(host="github.com", owner="acme", repo="app"),
        known_runs=[],
        window_start="t",
    )
    store.mark_unknown(_REQ, "no run appeared")
    make_maestro_run(tmp_path / "mhome", _ACME, "01RUN", started_at="2026-09-01T00:00:00")

    async with _client(config) as client:
        resp = await client.get("/api/human-queue")

    assert resp.status_code == 200
    body = resp.json()
    assert [w["key"] for w in body["waits"]] == [f"dispatcher-run:{_REQ}"]
    assert body["sources"]["dispatcher_runs"]["state"] == "ok"
    assert body["sources"]["maestro"]["state"] == "ok"
    assert body["sources"]["impresario"]["state"] == "not_configured"
    assert body["sources"]["forge_labelled_prs"]["state"] == "not_connected"
    assert body["sources"]["forge_candidate_prs"]["state"] == "not_connected"
    assert body["complete"] is False


async def test_a_raising_adapter_is_unavailable_not_a_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The healthy sources carry real waits, so a guard that dropped them
    alongside the failing one would fail here."""
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    store.reserve(
        _REQ,
        RepoKey(host="github.com", owner="acme", repo="app"),
        known_runs=[],
        window_start="t",
    )
    store.mark_unknown(_REQ, "no run appeared")
    db = make_maestro_run(
        tmp_path / "mhome", _ACME, "01RUN", started_at="2026-09-01T00:00:00"
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("T-1", "t", "needs_review", "claude_code", "2026-09-01", None, None),
        )
        conn.commit()
    finally:
        conn.close()

    def boom(_cache: object) -> object:
        raise RuntimeError("mirror exploded")

    monkeypatch.setattr(human_queue_sources, "from_impresario", boom)
    async with _client(config) as client:
        resp = await client.get("/api/human-queue")

    assert resp.status_code == 200
    body = resp.json()
    sources = body["sources"]
    assert sources["impresario"]["state"] == "unavailable"
    assert "mirror exploded" in sources["impresario"]["detail"]
    assert sources["dispatcher_runs"]["state"] == "ok"
    assert sources["maestro"]["state"] == "ok"
    assert sorted(w["source"] for w in body["waits"]) == [
        "dispatcher_runs",
        "maestro",
    ]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_human_queue_api.py -v`
Expected: FAIL — 404 on `/api/human-queue`.

- [ ] **Step 3: Implement**

Append to `human_queue_sources.py` (merge imports: `from collections.abc import Callable`, and `HumanQueueView`, `assemble` from `dispatcher.core.human_queue`):

```python
# A2 connects these (parent spec §3.3). Listed, not omitted, so the API
# cannot be read as the whole queue while they are missing (spec §3.4).
_FORGE_PLACEHOLDERS = (
    SourceResult(
        name="forge_labelled_prs",
        status=SourceStatus(state="not_connected", detail="arrives in slice A2"),
    ),
    SourceResult(
        name="forge_candidate_prs",
        status=SourceStatus(state="not_connected", detail="arrives in slice A2"),
    ),
)


def _guarded(name: str, adapter: Callable[[], SourceResult]) -> SourceResult:
    """One source failing must not take the others down (spec §4.1)."""
    try:
        return adapter()
    except Exception as exc:  # noqa: BLE001 — isolation IS the contract
        return SourceResult(
            name=name,
            status=SourceStatus(
                state="unavailable", detail=f"{type(exc).__name__}: {exc}"
            ),
        )


def build_human_queue(
    config: DispatcherConfig, cache: SnapshotService, *, now: str
) -> HumanQueueView:
    """Every A1 source, each isolated, assembled into one view."""
    results = [
        _guarded("dispatcher_runs", lambda: from_run_store(config)),
        _guarded(
            "maestro",
            lambda: from_maestro(config.effective_maestro_home, launch_records(config)),
        ),
        # Looked up through the module at call time so tests can patch it.
        _guarded("impresario", lambda: from_impresario(cache)),
        *_FORGE_PLACEHOLDERS,
    ]
    return assemble(results, now=now)
```

In `server/app.py`, add to imports:

```python
from dispatcher.core.human_queue import HumanQueueView
from dispatcher.core.human_queue_sources import build_human_queue
```

Add the route right after the `/api/waits` route:

```python
    @app.get("/api/human-queue", response_model=HumanQueueView)
    def human_queue() -> HumanQueueView:
        """Every wait for a human dispatcher can see, completeness per source.

        Always HTTP 200 — an unreadable source is content, not a transport
        error (spec 2026-09-29-human-queue-a1-design §4.1).
        """
        now = datetime.now(timezone.utc).isoformat()
        return build_human_queue(config, cache, now=now)
```

Note on the monkeypatch test: the lambda calls `from_impresario` by its module-global name at call time, so patching `human_queue_sources.from_impresario` reaches it. Do not bind the function early (e.g., `functools.partial(from_impresario, cache)`), or the test stops proving isolation.

- [ ] **Step 4: Full verification**

Run:
```bash
uv run ruff format . && uv run ruff check . \
  && uv run pyrefly check dispatcher tests scripts \
  && uv run pytest -q
```
Expected: all green. The web Node tests run inside the Python suite and need Node on PATH.

- [ ] **Step 5: Commit**

```bash
uv run ruff format dispatcher/core/human_queue_sources.py dispatcher/server/app.py tests/test_human_queue_api.py
git add dispatcher/core/human_queue_sources.py dispatcher/server/app.py tests/test_human_queue_api.py
git commit -m "feat(human-queue): GET /api/human-queue with isolated sources"
```

---

## Acceptance map (spec §6 → tests)

| Spec §6 | Test |
|---|---|
| 1 | `test_waiting_tasks_has_no_limit_and_no_recency_window`, `test_waits_from_every_non_terminal_run_not_only_the_newest` |
| 2 | `test_waiting_task_in_a_finished_run_is_not_a_wait` |
| 3 | `test_unreadable_run_db_is_partial_others_still_listed`, `test_failing_task_query_on_a_readable_run_is_partial`, `test_run_dir_without_state_db_is_partial`, `test_unlistable_runs_dir_is_partial`, `test_missing_state_is_reported_only_when_asked` |
| 4 | `test_launch_unknown_is_a_wait_aged_by_unknown_at`, `test_record_without_unknown_at_has_unknown_age`, `test_repeated_mark_on_a_legacy_record_does_not_invent_an_age` |
| 5 | `test_run_store_off_is_not_configured` |
| 6 | `test_impresario_waits_and_their_ages`, `test_non_ok_bundle_makes_source_partial_keeps_ok_waits`, `test_unscanned_mirror` |
| 7 | `test_a_raising_adapter_is_unavailable_not_a_500` |
| 8 | `test_queue_over_local_sources_is_partial_by_construction`, `test_complete_only_when_every_source_is_ok_or_off` |
| 9 | `test_same_key_merges_and_unions_reasons` |
| 10 | `test_known_ages_first_oldest_first_then_unknown_by_key`, `test_since_sorts_by_instant_not_text` |
