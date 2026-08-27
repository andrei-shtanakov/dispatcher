"""`RunController.submit_v2` — canon-recovered fields, structured admission
refusals (PR-C Task 4, spec §4.2).

Every numbered row here corresponds to a row in the Task 4 brief's
`submit_v2` flow: store.get failure, `_replay_existing`'s three shapes,
repo_key/checkout resolution, the in-guard item gate (in order), and the
clean launch tail.
"""

from __future__ import annotations

import multiprocessing
import subprocess
import time
from pathlib import Path

import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.run_controller import AdmissionRefused, RunController
from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_request import RunRequest, SubmitV2
from dispatcher.core.run_store import GUARD_TIMEOUT_SECONDS, RunStore
from tests.test_inventory_capture import make_repo

_OWNER = "andrei-shtanakov"
_REQ = "11111111-1111-4111-8111-111111111111"


def _key(name: str) -> RepoKey:
    return RepoKey(host="github.com", owner=_OWNER, repo=name)


def _remote(name: str) -> str:
    return f"git@github.com:{_OWNER}/{name}.git"


def _repo(tmp_path: Path, name: str, todo: str, dags: dict[str, str]) -> Path:
    """A workspace checkout under `<tmp_path>/ws/<name>`, DAGs naming
    itself via `repo_url:` — `make_repo`'s pattern
    (`tests/test_inventory_capture.py`), reused here so submit_v2 sees the
    SAME inventory shape the launchpad assembler tests already pin."""
    (tmp_path / "ws").mkdir(exist_ok=True)
    return make_repo(tmp_path / "ws", todo, dags, remote=_remote(name), name=name)


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit_trivial(root: Path) -> str:
    """A new commit that changes nothing an item/DAG decision reads —
    moves HEAD without touching TODO.md or dags/."""
    marker = root / "marker.txt"
    marker.write_text("x")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "move head",
        ],
        check=True,
    )
    return _head(root)


def _fake_maestro(path: Path, *, creates_run: str | None) -> Path:
    # A stand-in that publishes a run directory the way maestro does — it
    # derives which repo it's launching under from its OWN cwd (the
    # checkout `_spawn_reserved` passes as `cwd=`), mirroring
    # `tests/test_run_controller.py::_fake_maestro`'s reliance on
    # `$MAESTRO_HOME` rather than any inherited ambient state.
    body = (
        "#!/usr/bin/env python3\n"
        "import os, pathlib, sys\n"
        f"run_id = {creates_run!r}\n"
        "if run_id:\n"
        '    home = pathlib.Path(os.environ["MAESTRO_HOME"])\n'
        "    cwd = pathlib.Path.cwd()\n"
        "    # cwd is the checkout; find its origin's owner/repo by name\n"
        "    name = cwd.name\n"
        f'    d = home / "projects/github.com/{_OWNER}" / name / "runs" / run_id\n'
        "    d.mkdir(parents=True, exist_ok=True)\n"
        '    (d / "state.db").write_text("")\n'
        "sys.exit(0)\n"
    )
    path.write_text(body)
    path.chmod(0o755)
    return path


def _catalog(tmp_path: Path) -> Path:
    path = tmp_path / "agents-catalog.toml"
    path.write_text('[[agents]]\nharness = "claude_code"\n')
    return path


def _config(tmp_path: Path, cli: Path) -> DispatcherConfig:
    return DispatcherConfig(
        roots=(tmp_path / "ws",),
        maestro_home=tmp_path / "mhome",
        run_state_dir=tmp_path / "state",
        maestro_cli=cli,
        atp_catalog=_catalog(tmp_path),
    )


def _body(
    *,
    repo_key: str,
    work_id: str,
    revision: str,
    request_id: str = _REQ,
    snapshot_id: str = "snap-1",
) -> SubmitV2:
    return SubmitV2(
        snapshot_id=snapshot_id,
        repo_key=repo_key,
        work_id=work_id,
        request_id=request_id,
        seen_revision=revision,
    )


def _ready(tmp_path: Path, name: str, work_id: str) -> tuple[Path, str]:
    root = _repo(
        tmp_path,
        name,
        f"- [ ] Ready item @id:{work_id} @dag:dags/{work_id}.yaml\n",
        {f"dags/{work_id}.yaml": f"repo_url: {_remote(name)}\ntasks: []\n"},
    )
    return root, _head(root)


# --- row 1: hostile request_id -----------------------------------------


def test_hostile_request_id_is_422_not_a_crash(tmp_path: Path) -> None:
    name = "hostile-id"
    root, head = _ready(tmp_path, name, "w1")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01AAA")
    controller = RunController(_config(tmp_path, cli))
    body = SubmitV2.model_construct(
        snapshot_id="s",
        repo_key=_key(name).as_text(),
        work_id="w1",
        request_id="bad/id with space",
        seen_revision=head,
    )
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 422
    assert exc.value.code == "invalid_request"


# --- row 2: _replay_existing -------------------------------------------


def test_reserved_state_identity_mismatch_is_request_id_conflict(
    tmp_path: Path,
) -> None:
    name = "reserved-mismatch"
    root, head = _ready(tmp_path, name, "w1")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    config = _config(tmp_path, cli)
    store = RunStore(config.run_state_dir)  # type: ignore[arg-type]
    store.reserve(
        _REQ,
        _key(name),
        known_runs=[],
        window_start="t",
        work_id="w1",
        revision=head,
        repository=name,
    )
    controller = RunController(config)
    body = _body(repo_key=_key(name).as_text(), work_id="w1", revision="b" * 40)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 409
    assert exc.value.code == "request_id_conflict"


def test_admission_rejected_replay_is_409_with_persisted_fields_zero_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replay must not re-classify — proven both by the persisted 409
    staying field-wise identical after the workspace changes AND by an
    instrumented classifier seeing zero calls on the replay. Patched
    where `RunController` itself resolves the name (mirrors
    `test_run_controller.py::test_the_refusal_replays_without_reclassification`'s
    own v1 instrumentation), not the defining module — a bare
    `from ... import classify_inventory` binds a name in `run_controller`'s
    own namespace, and patching the source module's attribute afterwards
    would not reach that already-bound reference."""
    import dispatcher.core.run_controller as rc

    name = "admission-rejected-replay"
    root, head = _ready(tmp_path, name, "wX")
    # wX is never registered — this attempt is refused item_unregistered.
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(_config(tmp_path, cli))
    body = _body(repo_key=_key(name).as_text(), work_id="w-missing", revision=head)

    with pytest.raises(AdmissionRefused) as first:
        controller.submit_v2(body)
    assert first.value.status == 409
    assert first.value.code == "item_unregistered"

    # Change the workspace: register w-missing with a valid ready DAG.
    (root / "TODO.md").write_text(
        "- [ ] Ready item @id:w-missing @dag:dags/w-missing.yaml\n"
        "- [ ] Ready item @id:wX @dag:dags/wX.yaml\n"
    )
    (root / "dags" / "w-missing.yaml").write_text(
        f"repo_url: {_remote(name)}\ntasks: []\n"
    )
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "register w-missing",
        ],
        check=True,
    )

    calls = {"inventory": 0, "repo": 0}
    real_inventory = rc.classify_inventory
    real_repo = rc.classify_repo

    def _counting_inventory(*a: object, **k: object) -> object:
        calls["inventory"] += 1
        return real_inventory(*a, **k)  # type: ignore[arg-type]

    def _counting_repo(*a: object, **k: object) -> object:
        calls["repo"] += 1
        return real_repo(*a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(rc, "classify_inventory", _counting_inventory)
    monkeypatch.setattr(rc, "classify_repo", _counting_repo)

    with pytest.raises(AdmissionRefused) as second:
        controller.submit_v2(body)

    assert calls == {"inventory": 0, "repo": 0}
    assert second.value.status == first.value.status
    assert second.value.code == first.value.code
    assert second.value.detail == first.value.detail
    assert second.value.current == first.value.current


def test_receipt_state_replays_200(tmp_path: Path) -> None:
    name = "receipt-replay"
    root, head = _ready(tmp_path, name, "w1")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01AAA")
    controller = RunController(_config(tmp_path, cli), materialize_timeout=10.0)
    body = _body(repo_key=_key(name).as_text(), work_id="w1", revision=head)

    first = controller.submit_v2(body)
    assert first.accepted is True

    second = controller.submit_v2(body)
    assert second.accepted is True
    assert second.run_id == first.run_id


def test_v1_created_record_replays_through_v2_without_false_conflict(
    tmp_path: Path,
) -> None:
    """A v1-created record replayed through v2 compares canonically — the
    fingerprint's repo dimension is the canonical repo_key text, never a
    raw manifest name vs. a key."""
    name = "v1-then-v2"
    root, head = _ready(tmp_path, name, "w1")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01AAA")
    controller = RunController(_config(tmp_path, cli), materialize_timeout=10.0)

    v1_request = RunRequest(
        request_id=_REQ,
        work_id="w1",
        repository=name,
        revision=head,
        tasks="dags/w1.yaml",
    )
    v1_receipt = controller.submit(v1_request)
    assert v1_receipt.accepted is True

    v2_body = _body(repo_key=_key(name).as_text(), work_id="w1", revision=head)
    v2_receipt = controller.submit_v2(v2_body)
    assert v2_receipt.accepted is True
    assert v2_receipt.run_id == v1_receipt.run_id


# --- row 3 & 4: repo_key / checkout resolution --------------------------


def test_unknown_repo_key_is_repo_unresolved(tmp_path: Path) -> None:
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    (tmp_path / "ws").mkdir()
    controller = RunController(_config(tmp_path, cli))
    body = _body(
        repo_key="github.com/andrei-shtanakov/nope", work_id="w1", revision="a" * 40
    )
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 409
    assert exc.value.code == "repo_unresolved"


def test_checkout_identity_mismatch_is_422(tmp_path: Path) -> None:
    """A checkout exists at the repo_key's own `repo` segment, but its
    ACTUAL origin names a different owner — the declared repo_key must
    not be trusted over the checkout's own identity."""
    name = "impostor"
    root, head = _ready(tmp_path, name, "w1")
    # Directory is named "impostor" but declared repo_key claims a
    # DIFFERENT owner than the checkout's real origin remote.
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(_config(tmp_path, cli))
    body = _body(
        repo_key=f"github.com/someone-else/{name}", work_id="w1", revision=head
    )
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 422
    assert exc.value.code == "identity_mismatch"


# --- row 5: the in-guard item gate ---------------------------------------


def test_item_absent_entirely_is_item_unregistered(tmp_path: Path) -> None:
    name = "no-such-item"
    root, head = _ready(tmp_path, name, "w1")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(_config(tmp_path, cli))
    body = _body(repo_key=_key(name).as_text(), work_id="w-ghost", revision=head)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 409
    assert exc.value.code == "item_unregistered"


def test_closed_item_is_item_closed(tmp_path: Path) -> None:
    name = "closed-item"
    root = _repo(
        tmp_path,
        name,
        "- [x] Done item @id:w1 @dag:dags/w1.yaml\n",
        {},
    )
    head = _head(root)
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(_config(tmp_path, cli))
    body = _body(repo_key=_key(name).as_text(), work_id="w1", revision=head)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 409
    assert exc.value.code == "item_closed"


def test_item_with_no_dag_tag_is_item_unregistered(tmp_path: Path) -> None:
    name = "no-dag-tag"
    root = _repo(tmp_path, name, "- [ ] No dag item @id:w1\n", {})
    head = _head(root)
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(_config(tmp_path, cli))
    body = _body(repo_key=_key(name).as_text(), work_id="w1", revision=head)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 409
    assert exc.value.code == "item_unregistered"


def test_item_naming_an_absent_dag_file_is_dag_invalid(tmp_path: Path) -> None:
    name = "dag-invalid"
    root = _repo(
        tmp_path,
        name,
        "- [ ] Bad item @id:w1 @dag:dags/w1.yaml\n",
        {},  # the DAG file is never created
    )
    head = _head(root)
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(_config(tmp_path, cli))
    body = _body(repo_key=_key(name).as_text(), work_id="w1", revision=head)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 409
    assert exc.value.code == "dag_invalid"
    assert exc.value.current == {"reason": exc.value.detail}


def test_two_items_claiming_the_same_dag_is_dag_duplicate(tmp_path: Path) -> None:
    """Item w1's OWN `@dag:` tag validates (names `dags/w1.yaml`); item
    w2's `@dag:` also names `dags/w1.yaml` verbatim — a PF-DAG-MISMATCH for
    w2 itself (it doesn't equal `dags/w2.yaml`), but a grammar-valid raw
    value still CLAIMS `dags/w1.yaml` (spec §5.1(3), `_named_dag`), so w1
    sees a second claimant and is blocked `dag_duplicate`."""
    name = "dag-duplicate"
    root = _repo(
        tmp_path,
        name,
        "- [ ] First @id:w1 @dag:dags/w1.yaml\n- [ ] Second @id:w2 @dag:dags/w1.yaml\n",
        {"dags/w1.yaml": f"repo_url: {_remote(name)}\ntasks: []\n"},
    )
    head = _head(root)
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(_config(tmp_path, cli))
    body = _body(repo_key=_key(name).as_text(), work_id="w1", revision=head)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 409
    assert exc.value.code == "dag_duplicate"


def test_dag_edited_after_commit_is_dag_dirty(tmp_path: Path) -> None:
    name = "dag-dirty"
    root, head = _ready(tmp_path, name, "w1")
    # Edited WITHOUT re-committing, and without touching repo_url (that
    # would fail the identity check first) — the on-disk blob now differs
    # from the committed HEAD blob dag_dirty exists to catch.
    (root / "dags" / "w1.yaml").write_text(
        f"repo_url: {_remote(name)}\ntasks: []\n# edited, uncommitted\n"
    )
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(_config(tmp_path, cli))
    body = _body(repo_key=_key(name).as_text(), work_id="w1", revision=head)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 409
    assert exc.value.code == "dag_dirty"


def test_revision_moved_is_checked_after_item_decisions(tmp_path: Path) -> None:
    name = "revision-moved"
    root, old_head = _ready(tmp_path, name, "w1")
    new_head = _commit_trivial(root)
    assert new_head != old_head
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(_config(tmp_path, cli))
    body = _body(repo_key=_key(name).as_text(), work_id="w1", revision=old_head)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 409
    assert exc.value.code == "revision_moved"
    assert exc.value.current == {"seen_revision": new_head}


def test_revision_moved_never_fires_for_a_nonexistent_item(tmp_path: Path) -> None:
    """A nonexistent item can never be persisted as revision_moved — item
    checks run BEFORE the revision check (spec §5, row d)."""
    name = "revision-moved-nonexistent"
    root, old_head = _ready(tmp_path, name, "w1")
    _commit_trivial(root)
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(_config(tmp_path, cli))
    body = _body(repo_key=_key(name).as_text(), work_id="w-ghost", revision=old_head)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.code == "item_unregistered"


def test_repo_blocker_refuses_an_otherwise_ready_item(tmp_path: Path) -> None:
    name = "repo-blocked"
    root, head = _ready(tmp_path, name, "w1")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    config = _config(tmp_path, cli)
    store = RunStore(config.run_state_dir)  # type: ignore[arg-type]
    store.reserve(
        "other-req",
        _key(name),
        known_runs=[],
        window_start="t",
        work_id="other",
        revision=head,
        repository=name,
    )
    controller = RunController(config)
    body = _body(repo_key=_key(name).as_text(), work_id="w1", revision=head)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 409
    assert exc.value.code == "launch_busy"
    assert "blockers" in (exc.value.current or {})


# --- row 6: clean submit --------------------------------------------------


def test_clean_submit_recovers_tasks_from_canon(tmp_path: Path) -> None:
    name = "clean-submit"
    root, head = _ready(tmp_path, name, "w1")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01AAA")
    config = _config(tmp_path, cli)
    controller = RunController(config, materialize_timeout=10.0)
    body = _body(repo_key=_key(name).as_text(), work_id="w1", revision=head)

    receipt = controller.submit_v2(body)
    assert receipt.accepted is True
    assert receipt.run_id == "01AAA"

    store = RunStore(config.run_state_dir)  # type: ignore[arg-type]
    record = store.get(_REQ)
    assert record is not None
    assert record.tasks == "dags/w1.yaml"
    assert record.repository == name
    assert record.revision == head
    assert record.work_id == "w1"
    # spec_ref/plan_ref are absent from v2 — recovered refs are a future
    # concern; the record's fields stay empty.
    assert record.spec_ref_path is None
    assert record.spec_commit is None
    assert record.plan_ref_path is None
    assert record.plan_commit is None


# --- row 7: guard_busy ----------------------------------------------------


def _hold_store_guard(
    state_dir: str, key_parts: tuple[str, str, str], acquired
) -> None:
    store = RunStore(Path(state_dir))
    key = RepoKey(host=key_parts[0], owner=key_parts[1], repo=key_parts[2])
    with store.guard(key):
        acquired.set()
        time.sleep(GUARD_TIMEOUT_SECONDS + 2)


def test_guard_busy_is_409_not_persisted(tmp_path: Path) -> None:
    name = "guard-busy"
    root, head = _ready(tmp_path, name, "w1")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    config = _config(tmp_path, cli)
    assert config.run_state_dir is not None
    controller = RunController(config)

    ctx = multiprocessing.get_context("spawn")
    acquired = ctx.Event()
    proc = ctx.Process(
        target=_hold_store_guard,
        args=(str(config.run_state_dir), ("github.com", _OWNER, name), acquired),
    )
    proc.start()
    try:
        assert acquired.wait(timeout=5), "helper process never acquired the guard"
        body = _body(repo_key=_key(name).as_text(), work_id="w1", revision=head)
        with pytest.raises(AdmissionRefused) as exc:
            controller.submit_v2(body)
        assert exc.value.status == 409
        assert exc.value.code == "guard_busy"
    finally:
        proc.terminate()
        proc.join(timeout=5)

    store = RunStore(config.run_state_dir)
    assert store.get(_REQ) is None, "guard_busy must not persist a record"
