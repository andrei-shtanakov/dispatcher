"""`RunController.submit_v2` — canon-recovered fields, structured admission
refusals (PR-C Task 4, spec §4.2).

Every numbered row here corresponds to a row in the Task 4 brief's
`submit_v2` flow: store.get failure, `_replay_existing`'s three shapes,
repo_key/checkout resolution, the in-guard item gate (in order), and the
clean launch tail.
"""

from __future__ import annotations

import dataclasses
import multiprocessing
import subprocess
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.run_controller import AdmissionRefused, RunController
from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_request import RunRejectedError, RunRequest, SubmitV2
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


def _fake_maestro_by_identity(path: Path, *, creates_run: str | None) -> Path:
    """Like `_fake_maestro`, but derives the project directory from the
    checkout's ORIGIN REMOTE (as real maestro does,
    `maestro/maestro/repo_identity.py`) instead of from the checkout
    directory's own name — needed once a test's checkout directory name
    diverges from its remote's `repo` segment (review fix wave C, C1)."""
    body = (
        "#!/usr/bin/env python3\n"
        "import os, pathlib, subprocess, sys\n"
        f"run_id = {creates_run!r}\n"
        "if run_id:\n"
        '    home = pathlib.Path(os.environ["MAESTRO_HOME"])\n'
        "    cwd = pathlib.Path.cwd()\n"
        "    remote = subprocess.run(\n"
        "        ['git', 'remote', 'get-url', 'origin'], cwd=str(cwd),\n"
        "        capture_output=True, text=True, check=True,\n"
        "    ).stdout.strip()\n"
        "    path_part = remote.split(':', 1)[-1]\n"
        "    if path_part.endswith('.git'):\n"
        "        path_part = path_part[:-4]\n"
        "    owner, repo = path_part.rsplit('/', 1)\n"
        '    d = home / "projects" / "github.com" / owner / repo / "runs" / run_id\n'
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


@pytest.mark.parametrize(
    "repo_key",
    [
        "onlyonesegment",
        "too/many/segments/here",
        "github.com/owner/..",
        "github.com/owner/repo/",
    ],
)
def test_malformed_repo_key_is_422_invalid_request(
    tmp_path: Path, repo_key: str
) -> None:
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    (tmp_path / "ws").mkdir()
    controller = RunController(_config(tmp_path, cli))
    body = _body(repo_key=repo_key, work_id="w1", revision="a" * 40)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 422
    assert exc.value.code == "invalid_request"


def test_repeat_naming_a_different_repo_key_conflicts(tmp_path: Path) -> None:
    """The fingerprint's repo dimension is `body.repo_key` itself for v2 —
    a repeat under the SAME request_id that switches repo_key must
    conflict even when work_id/revision stay identical."""
    name_a = "repo-a-conflict"
    name_b = "repo-b-conflict"
    _ready(tmp_path, name_a, "w1")
    root_b, head_b = _ready(tmp_path, name_b, "w1")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    config = _config(tmp_path, cli)
    store = RunStore(config.run_state_dir)  # type: ignore[arg-type]
    store.reserve(
        _REQ,
        _key(name_a),
        known_runs=[],
        window_start="t",
        work_id="w1",
        revision=head_b,
        repository=name_a,
    )
    controller = RunController(config)
    body = _body(repo_key=_key(name_b).as_text(), work_id="w1", revision=head_b)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 409
    assert exc.value.code == "request_id_conflict"


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


def test_repeat_on_a_reserved_record_replays_without_reclassification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Superseded semantics (publish-run finding on #209): a repeat of a
    matching `reserved` attempt must NOT re-classify at all — §8.2 binds
    every prior state, and re-running admission here either terminalized
    a possibly-LIVE concurrent attempt or double-spawned. The in-guard
    authoritative re-check returns a poll receipt (accepted=None), the
    record and its lock stay exactly as the owning attempt left them.
    (A reserved record whose owner CRASHED is the recorded
    terminal-crash-window reconciliation tail — an escape, not a repeat
    side effect.)"""
    name = "leak-fix"
    root, head = _ready(tmp_path, name, "w1")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    config = _config(tmp_path, cli)
    store = RunStore(config.run_state_dir)  # type: ignore[arg-type]
    key = _key(name)
    store.reserve(
        _REQ,
        key,
        known_runs=[],
        window_start="t",
        work_id="w1",
        revision=head,
        repository=name,
    )
    assert store.holds_lock(key) == _REQ

    # Dirty the DAG WITHOUT recommitting — the item that was ready when
    # `_REQ` was reserved is now `dag_dirty`.
    (root / "dags" / "w1.yaml").write_text(
        f"repo_url: {_remote(name)}\ntasks: []\n# edited, uncommitted\n"
    )

    controller = RunController(config)
    body = _body(repo_key=key.as_text(), work_id="w1", revision=head)
    receipt = controller.submit_v2(body)
    assert receipt.accepted is None
    assert receipt.reason is not None and "poll" in receipt.reason

    record = store.get(_REQ)
    assert record is not None
    assert record.state == "reserved", "the owning attempt's record is untouched"
    assert store.holds_lock(key) == _REQ, "the owning attempt's lock is untouched"


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


def test_directory_name_differing_from_repo_key_still_resolves_and_launches(
    tmp_path: Path,
) -> None:
    """C1: the real fleet case — a checkout's WORKSPACE DIRECTORY name
    need not match its origin remote's `repo` segment (`open-prose/`
    cloned from `.../libretto.git`). The launchpad assembler
    (`launchpad.py::assemble_snapshot`) classifies this repo by its
    checkout's IDENTITY, never by directory name, so it shows Ready under
    `github.com/andrei-shtanakov/libretto` — submit_v2 must resolve the
    SAME checkout by that identity too, or the Ready row is unlaunchable:
    a 409 the UI can never recover from by refetching (an unbreakable
    loop, since the row stays Ready forever)."""
    from dispatcher.core.launchpad import assemble_snapshot

    dir_name = "open-prose"
    remote_name = "libretto"
    remote = _remote(remote_name)
    (tmp_path / "ws").mkdir(exist_ok=True)
    root = make_repo(
        tmp_path / "ws",
        "- [ ] Ready item @id:w1 @dag:dags/w1.yaml\n",
        {"dags/w1.yaml": f"repo_url: {remote}\ntasks: []\n"},
        remote=remote,
        name=dir_name,
    )
    head = _head(root)
    key = _key(remote_name)

    cli = _fake_maestro_by_identity(tmp_path / "fake-maestro", creates_run="01AAA")
    config = _config(tmp_path, cli)
    controller = RunController(config, materialize_timeout=10.0)

    # The assembler resolves this checkout's TRUE identity (its remote,
    # not its directory name) and classifies it Ready.
    snap = assemble_snapshot(controller)
    repo_row = next(r for r in snap.repositories if r.repo_key == key.as_text())
    assert repo_row.admission == "ready"
    ready_row = next(r for r in snap.ready if r.repo_key == key.as_text())
    assert ready_row.work_id == "w1"

    # submit_v2, given the SAME repo_key the assembler derived, must
    # resolve the SAME checkout and actually launch — not 409.
    body = _body(repo_key=key.as_text(), work_id="w1", revision=head)
    receipt = controller.submit_v2(body)
    assert receipt.accepted is True
    assert receipt.run_id == "01AAA"

    store = RunStore(config.run_state_dir)  # type: ignore[arg-type]
    record = store.get(_REQ)
    assert record is not None
    # The assembler and submit resolved the SAME checkout on disk.
    assert Path(record.checkout).resolve() == root.resolve()
    assert record.repository == dir_name


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


def test_capture_level_oserror_is_repo_unresolved_not_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `OSError` raised while capturing (never an admission decision)
    must not be mistaken for one — 409 `repo_unresolved`, and nothing
    written to the store."""
    import dispatcher.core.run_controller as rc

    name = "capture-oserror"
    root, head = _ready(tmp_path, name, "w1")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    config = _config(tmp_path, cli)
    controller = RunController(config)

    def _boom(checkout: Path) -> object:
        raise OSError("simulated capture failure")

    monkeypatch.setattr(rc, "capture_inventory", _boom)
    body = _body(repo_key=_key(name).as_text(), work_id="w1", revision=head)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 409
    assert exc.value.code == "repo_unresolved"

    store = RunStore(config.run_state_dir)  # type: ignore[arg-type]
    assert store.get(_REQ) is None, "a capture-level OSError must not persist"


def test_validate_request_failure_inside_the_gate_is_422_not_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `validate_request` failure on the internally-recovered
    `RunRequest` (git-level checks reading the checkout) is a decidable
    422 `invalid_request` — and, like every other row-6 gate failure,
    never persisted (an environment/attempt-shape fact, not an admission
    decision the caller could usefully replay)."""
    import dispatcher.core.run_controller as rc

    name = "validate-request-fails"
    root, head = _ready(tmp_path, name, "w1")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    config = _config(tmp_path, cli)
    controller = RunController(config)

    def _boom(request: object, dispatcher_config: object) -> object:
        raise RunRejectedError("simulated validate_request failure")

    monkeypatch.setattr(rc, "validate_request", _boom)
    body = _body(repo_key=_key(name).as_text(), work_id="w1", revision=head)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 422
    assert exc.value.code == "invalid_request"

    store = RunStore(config.run_state_dir)  # type: ignore[arg-type]
    assert store.get(_REQ) is None, "a validate_request failure must not persist"


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


def test_malformed_seen_revision_is_schema_invalid_not_persisted():
    """seen_revision='HEAD' must die at the schema (422), never persist.

    Without the 40-hex pattern the string sails through, the missing-item
    branch persists a terminal admission_rejected with revision='HEAD', and
    the request_id replays that 409 forever (local gate pass-1 finding).
    """
    with pytest.raises(ValidationError):
        SubmitV2(
            snapshot_id="s1",
            repo_key="github.com/o/r",
            work_id="w1",
            request_id="rq-schema-rev",
            seen_revision="HEAD",
        )
    with pytest.raises(ValidationError):
        SubmitV2(
            snapshot_id="s1",
            repo_key="github.com/o/r",
            work_id="w1",
            request_id="rq-schema-rev2",
            seen_revision="abc123",  # short form — display-only per spec
        )


def test_two_checkouts_of_one_repo_key_refuse_ambiguously(tmp_path: Path) -> None:
    """Gate pass-2 finding: two checkouts of one RepoKey in the workspace.

    The resolver must not silently pick the first match — the operator may
    have clicked a Ready row derived from the OTHER copy (different HEAD),
    which would persist a wrong revision_moved. Fail-closed: 409
    repo_unresolved naming both paths; the operator removes the duplicate.
    """
    remote = _remote("dupd")
    (tmp_path / "ws").mkdir(exist_ok=True)
    make_repo(
        tmp_path / "ws",
        "- [ ] A @id:w1 @dag:dags/w1.yaml\n",
        {"dags/w1.yaml": f"repo_url: {remote}\ntasks: []\n"},
        remote=remote,
        name="a-copy",
    )
    root_b = make_repo(
        tmp_path / "ws",
        "- [ ] A @id:w1 @dag:dags/w1.yaml\n",
        {"dags/w1.yaml": f"repo_url: {remote}\ntasks: []\n"},
        remote=remote,
        name="b-copy",
    )
    head_b = _head(root_b)
    key = _key("dupd")

    cli = _fake_maestro_by_identity(tmp_path / "fake-maestro", creates_run=None)
    config = _config(tmp_path, cli)
    controller = RunController(config, materialize_timeout=10.0)

    body = _body(repo_key=key.as_text(), work_id="w1", revision=head_b)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.status == 409
    assert exc.value.code == "repo_unresolved"
    assert "a-copy" in exc.value.detail and "b-copy" in exc.value.detail

    store = RunStore(config.run_state_dir)  # type: ignore[arg-type]
    assert store.get(_REQ) is None  # environment fact — never persisted


def test_root_as_checkout_config_is_visible_and_launchable(tmp_path: Path) -> None:
    """roots=(the repo itself,) — supported since slice 0 (test_run_api pins
    it) — must yield a launchpad row AND resolve through submit v2
    (review-pr finding on #209)."""
    from dispatcher.core.launchpad import assemble_snapshot

    remote = _remote("solo")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    root = make_repo(
        ws,
        "- [ ] A @id:w1 @dag:dags/w1.yaml\n",
        {"dags/w1.yaml": f"repo_url: {remote}\ntasks: []\n"},
        remote=remote,
        name="solo-dir",
    )
    head = _head(root)
    key = _key("solo")

    cli = _fake_maestro_by_identity(tmp_path / "fake-maestro", creates_run="01SOLO")
    config = _config(tmp_path, cli)
    config = dataclasses.replace(config, roots=(root,))
    controller = RunController(config, materialize_timeout=10.0)

    snap = assemble_snapshot(controller)
    assert any(r.repo_key == key.as_text() for r in snap.repositories)
    assert any(r.repo_key == key.as_text() for r in snap.ready)

    receipt = controller.submit_v2(
        _body(repo_key=key.as_text(), work_id="w1", revision=head)
    )
    assert receipt.accepted is True


def test_persist_refusal_never_terminalizes_another_attempts_record(
    tmp_path: Path,
) -> None:
    """Race (review-pr on #209): same request_id, DIFFERENT repo_keys, in
    parallel. Both pass replay before a record exists; A reserves and
    releases its guard; B, refusing under ITS OWN repo's guard, must NOT
    terminalize A's live record and release A's lock — a fingerprint
    mismatch is request_id_conflict, mutating nothing.
    """
    remote_a = _remote("race-a")
    remote_b = _remote("race-b")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    root_a = make_repo(
        ws,
        "- [ ] A @id:wa @dag:dags/wa.yaml\n",
        {"dags/wa.yaml": f"repo_url: {remote_a}\ntasks: []\n"},
        remote=remote_a,
        name="race-a",
    )
    make_repo(
        ws,
        "- [ ] B @id:wb @dag:dags/wb.yaml\n",
        {"dags/wb.yaml": f"repo_url: {remote_b}\ntasks: []\n"},
        remote=remote_b,
        name="race-b",
    )
    cli = _fake_maestro_by_identity(tmp_path / "fake-maestro", creates_run=None)
    config = _config(tmp_path, cli)
    controller = RunController(config, materialize_timeout=10.0)
    store = RunStore(config.run_state_dir)  # type: ignore[arg-type]

    # Attempt A got as far as reserving (the state the racing replay saw
    # as "no record" moments earlier).
    key_a = _key("race-a")
    with store.guard(key_a):
        store._reserve_locked(  # noqa: SLF001 — guarded caller
            _REQ,
            key_a,
            known_runs=[],
            window_start="2026-08-27T00:00:00+00:00",
            work_id="wa",
            revision=_head(root_a),
            tasks="dags/wa.yaml",
            repository="race-a",
            checkout=str(root_a),
        )
    assert store.holds_lock(key_a) is not None

    # Attempt B: SAME request_id, DIFFERENT repo. The race window is
    # between B's PRE-guard replay (which saw NO record) and the guard —
    # silence only that first call; the in-guard authoritative re-check
    # runs the real logic and must raise the conflict.
    real_replay = controller._replay_existing  # noqa: SLF001
    calls = {"n": 0}

    def racy_replay(*a, **k):  # noqa: ANN002, ANN003
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_replay(*a, **k)

    controller._replay_existing = racy_replay  # type: ignore[method-assign]  # noqa: SLF001
    body = _body(
        repo_key=_key("race-b").as_text(), work_id="missing", revision="f" * 40
    )
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.code == "request_id_conflict"

    record = store.get(_REQ)
    assert record is not None and record.state == "reserved"
    assert store.holds_lock(key_a) is not None  # A's lock untouched


def test_symlinked_fast_path_is_not_resolved(tmp_path: Path) -> None:
    """The fast path workspace/<segment> must not follow a symlink out of
    the workspace (review-pr on #209) — same rule as enumeration."""
    remote = _remote("slink")
    outside_ws = tmp_path / "elsewhere"
    outside_ws.mkdir()
    outside = make_repo(
        outside_ws,
        "- [ ] A @id:w1 @dag:dags/w1.yaml\n",
        {"dags/w1.yaml": f"repo_url: {remote}\ntasks: []\n"},
        remote=remote,
        name="slink",
    )
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "slink").symlink_to(outside)

    cli = _fake_maestro_by_identity(tmp_path / "fake-maestro", creates_run=None)
    config = _config(tmp_path, cli)
    controller = RunController(config, materialize_timeout=10.0)
    body = _body(
        repo_key=_key("slink").as_text(), work_id="w1", revision=_head(outside)
    )
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.code == "repo_unresolved"


def test_concurrent_repeat_of_a_reserved_attempt_spawns_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publish-run finding on #209: same request_id, same repo, concurrent.

    Both pass replay while the record is `reserved` (matching fingerprint →
    None → proceed); both reach `_reserve_locked` (returns the one record);
    without a compare-and-set at the launch tail BOTH spawn. Exactly one
    caller may win reserved→launching; the loser gets the existing receipt.
    """
    remote = _remote("caslaunch")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    root = make_repo(
        ws,
        "- [ ] A @id:w1 @dag:dags/w1.yaml\n",
        {"dags/w1.yaml": f"repo_url: {remote}\ntasks: []\n"},
        remote=remote,
        name="caslaunch",
    )
    head = _head(root)
    cli = _fake_maestro_by_identity(tmp_path / "fake-maestro", creates_run="01CAS")
    config = _config(tmp_path, cli)
    controller = RunController(config, materialize_timeout=10.0)

    spawns: list[str] = []
    real_popen = subprocess.Popen

    def counting_popen(*args, **kwargs):  # noqa: ANN002, ANN003
        # subprocess.run() is Popen underneath — count only the maestro
        # launch itself, not the git plumbing.
        argv = args[0] if args else kwargs.get("args", [])
        if any(str(cli) in str(part) for part in argv):
            spawns.append("spawn")
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(
        "dispatcher.core.run_controller.subprocess.Popen", counting_popen
    )

    body = _body(repo_key=_key("caslaunch").as_text(), work_id="w1", revision=head)
    first = controller.submit_v2(body)
    assert first.accepted is True and spawns == ["spawn"]

    # The racing repeat: its replay ran a moment before the record existed.
    controller._replay_existing = (  # type: ignore[method-assign]  # noqa: SLF001
        lambda *a, **k: None
    )
    second = controller.submit_v2(body)
    assert spawns == ["spawn"], "a concurrent repeat must not spawn again"
    assert second.request_id == first.request_id
    # The in-guard re-check is the authority: the repeat neither spawned
    # nor re-classified (its own run would have classified as a blocker
    # and TERMINALIZED the live record — the worse cousin of the race).
    store = RunStore(config.run_state_dir)  # type: ignore[arg-type]
    record = store.get(_REQ)
    assert record is not None and record.state != "terminal"


def test_enumeration_failure_refuses_rather_than_assuming_uniqueness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publish-run finding on #209: a failed/partial workspace scan must
    not count as proof there is no duplicate checkout — unknown is a 409
    repo_unresolved naming the scan failure, never a silent fast-path win.
    """
    import dispatcher.core.run_identity as ri

    remote = _remote("scanfail")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    root = make_repo(
        ws,
        "- [ ] A @id:w1 @dag:dags/w1.yaml\n",
        {"dags/w1.yaml": f"repo_url: {remote}\ntasks: []\n"},
        remote=remote,
        name="scanfail",
    )
    head = _head(root)
    cli = _fake_maestro_by_identity(tmp_path / "fake-maestro", creates_run=None)
    config = _config(tmp_path, cli)
    controller = RunController(config, materialize_timeout=10.0)

    real_scandir = ri.os.scandir

    def broken_scandir(path):  # noqa: ANN001
        if Path(str(path)) == ws:
            raise OSError("EIO: workspace unreadable")
        return real_scandir(path)

    monkeypatch.setattr(ri.os, "scandir", broken_scandir)
    body = _body(repo_key=_key("scanfail").as_text(), work_id="w1", revision=head)
    with pytest.raises(AdmissionRefused) as exc:
        controller.submit_v2(body)
    assert exc.value.code == "repo_unresolved"
    assert "EIO" in exc.value.detail


def test_snapshot_id_is_persisted_as_the_audit_echo(tmp_path: Path) -> None:
    """Spec §4.2 calls snapshot_id an audit echo — the durable record must
    carry which snapshot the operator acted from (escalated on #209)."""
    remote = _remote("snapecho")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    root = make_repo(
        ws,
        "- [ ] A @id:w1 @dag:dags/w1.yaml\n",
        {"dags/w1.yaml": f"repo_url: {remote}\ntasks: []\n"},
        remote=remote,
        name="snapecho",
    )
    cli = _fake_maestro_by_identity(tmp_path / "fake-maestro", creates_run="01SN")
    config = _config(tmp_path, cli)
    controller = RunController(config, materialize_timeout=10.0)
    body = _body(
        repo_key=_key("snapecho").as_text(), work_id="w1", revision=_head(root)
    )
    receipt = controller.submit_v2(body)
    assert receipt.accepted is True
    store = RunStore(config.run_state_dir)  # type: ignore[arg-type]
    record = store.get(_REQ)
    assert record is not None
    assert record.snapshot_id == body.snapshot_id != ""
