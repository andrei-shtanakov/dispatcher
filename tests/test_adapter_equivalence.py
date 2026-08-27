"""Spec §5's adapter equivalence property, over FROZEN captured sources.

`admission.py`'s module docstring makes the promise this test cashes in:
both adapters — the launchpad snapshot assembler (`launchpad.assemble_snapshot`)
and submit's admission guard (`RunController.submit_v2`) — call the SAME
`classify_inventory` over captured values, so two independent
implementations of "is this repo/item admissible" can never grow apart.

The property is only meaningful over IDENTICAL inputs: each adapter's own
`capture_inventory`/`read_lock_state`/`capture_run_facts` seam is
monkeypatched (the bare names each module imported/defined into its own
namespace — Task 4's precedent, not `admission.py`'s or `inventory.py`'s
own attributes) to return the exact same frozen objects, captured ONCE for
real in this test, regardless of what either adapter passes in. Each
module's own `classify_inventory` name is wrapped to record `(inputs,
decision)` per call. Two scenarios: a clean ready repo, and the same repo
with one live run injected into the frozen run-facts set.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import dispatcher.core.launchpad as launchpad_module
import dispatcher.core.run_controller as rc
from dispatcher.core.admission import (
    CapturedInputs,
    InventoryDecision,
    RunFact,
    classify_inventory,
)
from dispatcher.core.collectors.maestro import classified_runs
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.inventory import capture_inventory
from dispatcher.core.inventory_types import InventorySurface
from dispatcher.core.launchpad import assemble_snapshot
from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.run_controller import (
    AdmissionRefused,
    LaunchReceipt,
    RunController,
    capture_run_facts,
    read_lock_state,
)
from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_request import SubmitV2, ValidatedRequest
from dispatcher.core.run_store import LaunchRecord, LockState, RunStore
from tests.test_inventory_capture import make_repo

_OWNER = "andrei-shtanakov"
_REQUEST_ID = "11111111-1111-4111-8111-111111111111"
_STUB_REASON = "stub: spawn skipped by adapter-equivalence test"


def _key(name: str) -> RepoKey:
    return RepoKey(host="github.com", owner=_OWNER, repo=name)


def _remote(name: str) -> str:
    return f"git@github.com:{_OWNER}/{name}.git"


def _ready_repo(tmp_path: Path, name: str, work_id: str) -> Path:
    """A tmp git checkout with one ready item, `test_launchpad_assembler.
    py::_ready_repo`'s pattern: its DAG names this SAME repo via
    `repo_url:`, the seam `classify_inventory` needs to call it ready."""
    (tmp_path / "ws").mkdir(exist_ok=True)
    remote = _remote(name)
    return make_repo(
        tmp_path / "ws",
        f"- [ ] Ready item @id:{work_id} @dag:dags/{work_id}.yaml\n",
        {f"dags/{work_id}.yaml": f"repo_url: {remote}\ntasks: []\n"},
        remote=remote,
        name=name,
    )


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _catalog(tmp_path: Path) -> Path:
    path = tmp_path / "agents-catalog.toml"
    path.write_text('[[agents]]\nharness = "claude_code"\n')
    return path


def _config(tmp_path: Path) -> DispatcherConfig:
    return DispatcherConfig(
        roots=(tmp_path / "ws",),
        maestro_home=tmp_path / "mhome",
        run_state_dir=tmp_path / "state",
        # Never executed: `_spawn_reserved` is stubbed below — the
        # property under test is admission, not spawning.
        maestro_cli=tmp_path / "fake-maestro",
        atp_catalog=_catalog(tmp_path),
    )


def _capture_baseline(
    config: DispatcherConfig,
    controller: RunController,
    store: RunStore,
    checkout: Path,
    key: RepoKey,
) -> tuple[
    InventorySurface, LockState, str | None, tuple[RunFact, ...], tuple[str, ...]
]:
    """One real capture generation for a clean repo — run ONCE, for real,
    using the same module-level functions each adapter's seam normally
    calls. Both adapters' seams are then monkeypatched to hand back
    exactly this frozen tuple, so their `CapturedInputs` are equal by
    construction rather than by coincidence of timing."""
    inv = capture_inventory(checkout)
    lock_state, lock_error = read_lock_state(store, key)
    home = config.effective_maestro_home
    scratch = ProjectSnapshot(name="maestro", path="")
    records, list_unreadable = store.list_with_mtime()
    classified = classified_runs(home, scratch)
    run_facts, runs_unreadable = capture_run_facts(
        store,
        key,
        controller.runs_dir(key),
        home,
        records=[record for record, _ in records],
        list_unreadable=list_unreadable,
        classified=classified,
        scratch_warnings=scratch.warnings,
    )
    return inv, lock_state, lock_error, run_facts, runs_unreadable


class _Recorder:
    """Wraps `classify_inventory`, recording `(inputs, decision)` per
    call — this IS the property under test: the same captured inputs must
    yield the same decision no matter which adapter's own bound name
    calls it."""

    def __init__(self) -> None:
        self.calls: list[tuple[CapturedInputs, InventoryDecision]] = []

    def __call__(self, captured: CapturedInputs) -> InventoryDecision:
        decision = classify_inventory(captured)
        self.calls.append((captured, decision))
        return decision


def _install_seams(
    monkeypatch: pytest.MonkeyPatch,
    *,
    inv: InventorySurface,
    lock_state: LockState,
    lock_error: str | None,
    run_facts: tuple[RunFact, ...],
    runs_unreadable: tuple[str, ...],
    recorder_launchpad: _Recorder,
    recorder_submit: _Recorder,
) -> None:
    """Monkeypatch BOTH adapters' capture seams to return the SAME frozen
    values regardless of the arguments either adapter passes — the bare
    names `launchpad.py` and `run_controller.py` each import/define into
    their own namespace, not `inventory.capture_inventory` or
    `admission.classify_inventory` themselves (Task 4's precedent: a
    `from ... import x` binds a name in the IMPORTING module, and patching
    the defining module's attribute afterward would not reach it)."""
    monkeypatch.setattr(launchpad_module, "capture_inventory", lambda *a, **k: inv)
    monkeypatch.setattr(
        launchpad_module, "read_lock_state", lambda *a, **k: (lock_state, lock_error)
    )
    monkeypatch.setattr(
        launchpad_module,
        "capture_run_facts",
        lambda *a, **k: (run_facts, runs_unreadable),
    )
    monkeypatch.setattr(launchpad_module, "classify_inventory", recorder_launchpad)

    monkeypatch.setattr(rc, "capture_inventory", lambda *a, **k: inv)
    monkeypatch.setattr(rc, "read_lock_state", lambda *a, **k: (lock_state, lock_error))
    monkeypatch.setattr(
        rc, "capture_run_facts", lambda *a, **k: (run_facts, runs_unreadable)
    )
    monkeypatch.setattr(rc, "classify_inventory", recorder_submit)


def _stub_spawn_reserved(
    self: RunController,
    store: RunStore,
    record: LaunchRecord,
    validated: ValidatedRequest,
    catalog: Path,
    runs: Path,
) -> LaunchReceipt:
    """The launch tail, stubbed to a no-op fake receipt — the property
    under test is admission, not the maestro subprocess (brief step 1)."""
    return LaunchReceipt(
        request_id=record.request_id, run_id=None, accepted=True, reason=_STUB_REASON
    )


def _submit_body(
    *, repo_key: str, work_id: str, revision: str, snapshot_id: str
) -> SubmitV2:
    return SubmitV2(
        snapshot_id=snapshot_id,
        repo_key=repo_key,
        work_id=work_id,
        request_id=_REQUEST_ID,
        seen_revision=revision,
    )


def test_ready_item_agrees_between_snapshot_and_submit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Clean repo, one ready item: `assemble_snapshot` lists it under
    `ready`, and `submit_v2` reaches the reserve (no `AdmissionRefused`
    before the `_spawn_reserved` stub) — off the SAME frozen inputs."""
    name = "adapter-equiv-ready"
    work_id = "w1"
    root = _ready_repo(tmp_path, name, work_id)
    head = _head(root)
    key = _key(name)
    config = _config(tmp_path)
    controller = RunController(config)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    checkout = tmp_path / "ws" / name

    inv, lock_state, lock_error, run_facts, runs_unreadable = _capture_baseline(
        config, controller, store, checkout, key
    )
    assert run_facts == ()  # baseline sanity: no run exists yet

    recorder_launchpad = _Recorder()
    recorder_submit = _Recorder()
    _install_seams(
        monkeypatch,
        inv=inv,
        lock_state=lock_state,
        lock_error=lock_error,
        run_facts=run_facts,
        runs_unreadable=runs_unreadable,
        recorder_launchpad=recorder_launchpad,
        recorder_submit=recorder_submit,
    )
    monkeypatch.setattr(RunController, "_spawn_reserved", _stub_spawn_reserved)

    snap = assemble_snapshot(controller)
    assert any(r.work_id == work_id and r.repo_key == key.as_text() for r in snap.ready)

    body = _submit_body(
        repo_key=key.as_text(),
        work_id=work_id,
        revision=head,
        snapshot_id=snap.snapshot_id,
    )
    receipt = controller.submit_v2(body)  # must not raise AdmissionRefused
    assert receipt.reason == _STUB_REASON

    assert len(recorder_launchpad.calls) == 1
    assert len(recorder_submit.calls) == 1
    snapshot_inputs, snapshot_decision = recorder_launchpad.calls[0]
    submit_inputs, submit_decision = recorder_submit.calls[0]

    assert snapshot_inputs == submit_inputs  # frozen dataclass equality
    assert snapshot_decision.repo.admission == "ready"
    assert submit_decision.repo.admission == "ready"
    assert any(i.work_id == work_id for i in snapshot_decision.ready)
    assert any(i.work_id == work_id for i in submit_decision.ready)


def test_run_in_flight_blocks_snapshot_and_refuses_submit_with_same_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same repo, with one live non-terminal run injected into the
    frozen run-facts set: `assemble_snapshot` blocked-rows the repo
    `run_in_flight`, and `submit_v2` raises `AdmissionRefused(code=
    "run_in_flight")` — the SAME code as the row's blocker, off the SAME
    frozen inputs.

    RED performed (brief step 2): with the `run_controller` capture seam
    temporarily patched AFTER `_install_seams` to hand `submit_v2` the
    BASELINE run-facts (the live run dropped, only on the submit side —
    `monkeypatch.setattr(rc, "capture_run_facts", lambda *a, **k: (
    base_run_facts, runs_unreadable))` inserted right before the
    `pytest.raises(AdmissionRefused)` block, a local edit never
    committed), `submit_v2` no longer saw the in-flight run: it reached
    the reserve and the `_spawn_reserved` stub instead of raising, so
    `pytest.raises(AdmissionRefused)` failed with "DID NOT RAISE" —
    proving this test would have caught the two adapters disagreeing.
    Reverted before committing.
    """
    name = "adapter-equiv-in-flight"
    work_id = "w1"
    root = _ready_repo(tmp_path, name, work_id)
    head = _head(root)
    key = _key(name)
    config = _config(tmp_path)
    controller = RunController(config)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    checkout = tmp_path / "ws" / name

    inv, lock_state, lock_error, base_run_facts, runs_unreadable = _capture_baseline(
        config, controller, store, checkout, key
    )
    assert base_run_facts == ()

    live = RunFact(
        run_id="01LIVE", status="running", request_id=None, run_dir_exists=True
    )
    run_facts = (*base_run_facts, live)

    recorder_launchpad = _Recorder()
    recorder_submit = _Recorder()
    _install_seams(
        monkeypatch,
        inv=inv,
        lock_state=lock_state,
        lock_error=lock_error,
        run_facts=run_facts,
        runs_unreadable=runs_unreadable,
        recorder_launchpad=recorder_launchpad,
        recorder_submit=recorder_submit,
    )
    monkeypatch.setattr(RunController, "_spawn_reserved", _stub_spawn_reserved)

    snap = assemble_snapshot(controller)
    row = next(r for r in snap.repositories if r.repo_key == key.as_text())
    assert row.admission == "blocked"
    assert any(b.code == "run_in_flight" for b in row.blockers)

    body = _submit_body(
        repo_key=key.as_text(),
        work_id=work_id,
        revision=head,
        snapshot_id=snap.snapshot_id,
    )
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.code == "run_in_flight"

    assert len(recorder_launchpad.calls) == 1
    assert len(recorder_submit.calls) == 1
    snapshot_inputs, snapshot_decision = recorder_launchpad.calls[0]
    submit_inputs, submit_decision = recorder_submit.calls[0]

    assert snapshot_inputs == submit_inputs  # frozen dataclass equality
    assert snapshot_decision.repo.blockers[0].code == "run_in_flight"
    assert submit_decision.repo.blockers[0].code == "run_in_flight"
    assert submit_decision.repo.blockers[0].code == exc.value.code
