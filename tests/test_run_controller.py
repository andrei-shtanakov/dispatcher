"""RunController launch path (spec §5.3, §5.4)."""

import dataclasses
import json
import os
import shutil
import subprocess
import textwrap
import tracemalloc
from pathlib import Path

import pytest
from pydantic import ValidationError

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.run_controller import RunController
from dispatcher.core.run_request import RunRejectedError, RunRequest
from dispatcher.core.run_store import LockBusyError, RunStore

_REQ = "11111111-1111-4111-8111-111111111111"


def _repo(root: Path) -> str:
    repo = root / "deployer"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "git@github.com:owner/deployer.git",
        ],
        check=True,
    )
    # C1: maestro keys the run by this file's own `repo:` field, not by
    # dispatcher's `request.repository` — self-reference here keeps every
    # test that expects a launch to be reachable meaning what it says.
    (repo / "tasks.yaml").write_text(f"tasks: []\nrepo: {repo}\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _repo_head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root / "deployer"), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _fake_maestro(
    path: Path,
    *,
    creates_run: str | None,
    exit_code: int = 0,
    stderr_msg: str | None = None,
) -> Path:
    """A stand-in that publishes a run directory the way maestro does.

    It reads MAESTRO_HOME from the environment on purpose: a controller that
    launches under one root and watches another must fail this test.
    """
    body = textwrap.dedent(
        f"""
        #!/usr/bin/env python3
        import os, pathlib, sys
        run_id = {creates_run!r}
        msg = {stderr_msg!r}
        if msg:
            print(msg, file=sys.stderr)
        cwd_log = os.environ.get("FAKE_MAESTRO_CWD_LOG")
        if cwd_log:
            # The pilot (2026-08-24) found every verb running in the SERVER's
            # cwd, so maestro resolved the wrong repository and reported a
            # healthy run as missing. Nothing observed cwd, which is why a
            # green suite said nothing about it. Recorded per invocation as
            # "<verb> <cwd>" so a test can assert it per verb.
            with open(cwd_log, "a") as fh:
                verb = sys.argv[1] if len(sys.argv) > 1 else "<none>"
                fh.write(verb + " " + os.getcwd() + "\\n")
        if run_id:
            home = pathlib.Path(os.environ["MAESTRO_HOME"])
            runs = home / "projects/github.com/owner/deployer/runs" / run_id
            runs.mkdir(parents=True)
            (runs / "state.db").write_text("")
        sys.exit({exit_code})
        """
    ).strip()
    path.write_text(body + "\n")
    path.chmod(0o755)
    return path


def _catalog(tmp_path: Path) -> Path:
    """A readable catalog file — declaredness and reachability are all
    dispatcher checks; the CONTENTS belong to ATP and maestro."""
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


def _request(revision: str) -> RunRequest:
    return RunRequest(
        request_id=_REQ,
        work_id="todo://deployer/entrypoint-token-boundary-match",
        repository="deployer",
        revision=revision,
        tasks="tasks.yaml",
    )


def test_accepted_true_only_after_the_run_appears(tmp_path: Path) -> None:
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01AAA")
    controller = RunController(_config(tmp_path, cli), materialize_timeout=10.0)
    receipt = controller.submit(_request(head))
    assert receipt.accepted is True
    assert receipt.run_id == "01AAA"


def test_relative_maestro_cli_is_refused_not_executed_from_the_checkout(
    tmp_path: Path,
) -> None:
    """I6: `maestro_cli` is documented as an ABSOLUTE path but was never
    checked; combined with `cwd=<checkout>` on the launch, a relative value
    containing a slash would execute a binary planted INSIDE the target
    repository instead of the configured location. A marker file proves
    nothing from the checkout actually ran."""
    head = _repo(tmp_path / "ws")
    checkout = tmp_path / "ws" / "deployer"
    marker = tmp_path / "pwned"
    evil = checkout / "bin" / "maestro"
    evil.parent.mkdir(parents=True)
    evil.write_text(f"#!/bin/sh\ntouch {marker}\n")
    evil.chmod(0o755)

    config = _config(tmp_path, Path("bin/maestro"))  # relative, with a slash
    controller = RunController(config, poll_interval=0.05, materialize_timeout=0.5)
    receipt = controller.submit(_request(head))
    assert receipt.accepted is False
    assert "absolute" in (receipt.reason or "")
    assert not marker.exists(), "a relative maestro_cli must never be executed"


def test_reserve_persists_the_request_body(tmp_path: Path) -> None:
    """I3: spec §3.1's five-way join lives in the `LaunchRecord`, not just
    in a validated-then-discarded `RunRequest`."""
    from dispatcher.core.run_store import RunStore

    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01AAA")
    config = _config(tmp_path, cli)
    request = RunRequest(
        request_id=_REQ,
        work_id="todo://deployer/entrypoint-token-boundary-match",
        repository="deployer",
        revision=head,
        tasks="tasks.yaml",
        spec_ref={"path": "docs/s.md"},
        plan_ref={"path": "docs/p.md", "commit": "b" * 40},
    )
    controller = RunController(config, materialize_timeout=10.0)
    receipt = controller.submit(request)
    assert receipt.accepted is True

    assert config.run_state_dir is not None
    record = RunStore(config.run_state_dir).get(_REQ)
    assert record is not None
    assert record.work_id == "todo://deployer/entrypoint-token-boundary-match"
    assert record.revision == head
    assert record.tasks == "tasks.yaml"
    assert record.spec_ref_path == "docs/s.md"
    assert record.spec_commit == head, "spec_ref.commit defaults to revision"
    assert record.plan_ref_path == "docs/p.md"
    assert record.plan_commit == "b" * 40, "an explicit plan_ref.commit is kept"


def test_launch_that_never_materializes_is_null_not_false(tmp_path: Path) -> None:
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(
        _config(tmp_path, cli), poll_interval=0.05, materialize_timeout=0.5
    )
    receipt = controller.submit(_request(head))
    assert receipt.accepted is None, "unknown must never be reported as a refusal"
    assert receipt.run_id is None
    assert "unknown" in (receipt.reason or "").lower()


def test_launch_that_exits_nonzero_without_publishing_is_a_refusal(
    tmp_path: Path,
) -> None:
    """I1: once the child is confirmed dead and a second look still finds
    nothing published, `false` is knowable (spec §5.3) rather than a guess
    — and the lock must be released, or a retry is blocked forever with no
    way out."""
    from dispatcher.core.run_identity import RepoKey
    from dispatcher.core.run_store import RunStore

    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(
        tmp_path / "dying-maestro",
        creates_run=None,
        exit_code=1,
        stderr_msg="config error: bad tasks.yaml",
    )
    config = _config(tmp_path, cli)
    controller = RunController(config, poll_interval=0.05, materialize_timeout=2.0)
    receipt = controller.submit(_request(head))
    assert receipt.accepted is False
    assert receipt.run_id is None
    assert "exited 1" in (receipt.reason or "")
    assert "config error: bad tasks.yaml" in (receipt.reason or "")

    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    assert store.get(_REQ) is not None
    assert store.get(_REQ).state == "terminal"  # type: ignore[union-attr]
    key = RepoKey(host="github.com", owner="owner", repo="deployer")
    assert store.holds_lock(key) is None, "the lock must not survive a dead launch"


def test_resubmission_against_a_launching_record_carries_a_reason(
    tmp_path: Path,
) -> None:
    """A resubmission mid-launch used to come back as `accepted: null` with
    `reason: null` — the one receipt shape that told the caller nothing at
    all (`mark_launching` sets no `reason`; only `mark_unknown` does)."""
    from dispatcher.core.run_identity import RepoKey
    from dispatcher.core.run_store import RunStore

    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01AAA")
    config = _config(tmp_path, cli)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    key = RepoKey(host="github.com", owner="owner", repo="deployer")
    store.reserve(_REQ, key, known_runs=[], window_start="t")
    store.mark_launching(_REQ)

    controller = RunController(config)
    receipt = controller.submit(_request(head))
    assert receipt.accepted is None
    assert receipt.reason is not None
    assert "already launching" in receipt.reason


def test_validation_failure_is_accepted_false(tmp_path: Path) -> None:
    _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01AAA")
    controller = RunController(_config(tmp_path, cli))
    receipt = controller.submit(_request("b" * 40))
    assert receipt.accepted is False
    assert receipt.run_id is None


def test_busy_repository_is_accepted_false(tmp_path: Path) -> None:
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(
        _config(tmp_path, cli), poll_interval=0.05, materialize_timeout=0.3
    )
    controller.submit(_request(head))  # leaves launch_unknown, keeps the lock
    second = _request(head).model_copy(
        update={"request_id": "22222222-2222-4222-8222-222222222222"}
    )
    receipt = controller.submit(second)
    assert receipt.accepted is False
    assert "in flight" in (receipt.reason or "")


def test_child_is_launched_with_an_explicit_maestro_home(tmp_path: Path) -> None:
    """The fake binary writes into $MAESTRO_HOME; finding the run proves the
    controller passed the configured home rather than inheriting one."""
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01CCC")
    config = _config(tmp_path, cli)
    receipt = RunController(config, materialize_timeout=10.0).submit(_request(head))
    assert receipt.accepted is True
    expected = (
        config.effective_maestro_home / "projects/github.com/owner/deployer/runs/01CCC"
    )
    assert expected.is_dir()


def test_repeated_request_id_does_not_launch_twice(tmp_path: Path) -> None:
    head = _repo(tmp_path / "ws")
    marker = tmp_path / "calls.txt"
    cli = tmp_path / "counting-maestro"
    cli.write_text(
        textwrap.dedent(
            f"""
            #!/usr/bin/env python3
            import os, pathlib
            p = pathlib.Path({str(marker)!r})
            p.write_text(str(int(p.read_text() or 0) + 1 if p.exists() else 1))
            home = pathlib.Path(os.environ["MAESTRO_HOME"])
            d = home / "projects/github.com/owner/deployer/runs/01DDD"
            d.mkdir(parents=True, exist_ok=True)
            (d / "state.db").write_text("")
            """
        ).strip()
        + "\n"
    )
    cli.chmod(0o755)
    controller = RunController(_config(tmp_path, cli), materialize_timeout=10.0)
    first = controller.submit(_request(head))
    second = controller.submit(_request(head))
    assert first.run_id == second.run_id
    assert int(marker.read_text()) == 1, "the second submit must not re-launch"


# -- fix round 1: Important 1 — an unsafe request_id must not raise --------


def test_request_id_pattern_rejects_path_and_whitespace_characters() -> None:
    """Layer 1: pydantic refuses an unsafe request_id at construction."""
    with pytest.raises(ValidationError):
        RunRequest(
            request_id="bad/id with space",
            work_id="todo://deployer/entrypoint-token-boundary-match",
            repository="deployer",
            revision="a" * 40,
            tasks="tasks.yaml",
        )


def test_submit_refuses_rather_than_raises_on_an_unsafe_request_id(
    tmp_path: Path,
) -> None:
    """Layer 2: even a request that bypassed pydantic validation (e.g. built
    via `model_construct`, or a future field nobody re-guarded) must not
    reach `RunStore` and raise `RunStoreError` out of `submit()` — it must
    come back as an ordinary refusal, because nothing was launched."""
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01AAA")
    controller = RunController(_config(tmp_path, cli))
    request = RunRequest.model_construct(
        request_id="bad/id with space",
        work_id="todo://deployer/entrypoint-token-boundary-match",
        repository="deployer",
        revision=head,
        tasks="tasks.yaml",
    )
    receipt = controller.submit(request)
    assert receipt.accepted is False
    assert receipt.run_id is None


# -- fix round 1: Important 2 — a store-write failure must not lose or -----
# -- misreport a run that genuinely materialized ----------------------------


def test_mark_launching_failure_is_accepted_false_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was exec'd yet, so a bookkeeping failure here is still a
    known non-launch, not an unhandled exception."""
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01AAA")
    controller = RunController(_config(tmp_path, cli))

    def _boom(self: RunStore, request_id: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(RunStore, "mark_launching", _boom)
    receipt = controller.submit(_request(head))
    assert receipt.accepted is False
    assert receipt.run_id is None


def test_mark_materialized_failure_never_claims_the_run_did_not_happen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run WAS observed under runs/; a failure to durably record that
    must surface as `accepted: None` with the run_id attached (never
    `False`, and never an unhandled exception), and must keep the
    repository lock held rather than abandon it."""
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01EEE")
    controller = RunController(_config(tmp_path, cli), materialize_timeout=10.0)

    def _boom(self: RunStore, request_id: str, run_id: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(RunStore, "mark_materialized", _boom)
    receipt = controller.submit(_request(head))
    assert receipt.accepted is None, "a materialized run must never read as False"
    assert receipt.run_id == "01EEE"
    assert "01EEE" in (receipt.reason or "")

    # The lock is still held: a second, distinct request against the same
    # repository is refused rather than silently allowed to race.
    second = _request(head).model_copy(
        update={"request_id": "33333333-3333-4333-8333-333333333333"}
    )
    second_receipt = RunController(_config(tmp_path, cli)).submit(second)
    assert second_receipt.accepted is False
    assert "in flight" in (second_receipt.reason or "")


# -- task 5: leaving launch_unknown — adoption and operator resolution ------


def _state(controller: RunController, request_id: str) -> str:
    """The stored state, with the Optional narrowed once instead of per call."""
    record = controller.record(request_id)
    assert record is not None
    return record.state


def _unknown(tmp_path: Path) -> tuple[RunController, Path]:
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    config = _config(tmp_path, cli)
    controller = RunController(config, poll_interval=0.05, materialize_timeout=0.3)
    receipt = controller.submit(_request(head))
    assert receipt.accepted is None
    assert config.maestro_home is not None
    runs = config.maestro_home / "projects/github.com/owner/deployer/runs"
    runs.mkdir(parents=True, exist_ok=True)
    return controller, runs


def test_exactly_one_candidate_is_adopted(tmp_path: Path) -> None:
    controller, runs = _unknown(tmp_path)
    (runs / "01LATE").mkdir()
    resolution = controller.resolve_unknown(_REQ)
    assert resolution.adopted_run_id == "01LATE"
    assert _state(controller, _REQ) == "materialized"


def test_zero_candidates_stays_unknown(tmp_path: Path) -> None:
    controller, _ = _unknown(tmp_path)
    resolution = controller.resolve_unknown(_REQ)
    assert resolution.adopted_run_id is None
    assert _state(controller, _REQ) == "launch_unknown"


def test_two_candidates_are_never_guessed_between(tmp_path: Path) -> None:
    controller, runs = _unknown(tmp_path)
    (runs / "01AAA").mkdir()
    (runs / "01BBB").mkdir()
    resolution = controller.resolve_unknown(_REQ)
    assert resolution.adopted_run_id is None
    assert sorted(resolution.candidates) == ["01AAA", "01BBB"]
    assert _state(controller, _REQ) == "launch_unknown"


def test_adoption_releases_the_lock(tmp_path: Path) -> None:
    controller, runs = _unknown(tmp_path)
    (runs / "01LATE").mkdir()
    controller.resolve_unknown(_REQ)
    head = _repo_head(tmp_path / "ws")
    second = _request(head).model_copy(
        update={"request_id": "33333333-3333-4333-8333-333333333333"}
    )
    assert controller.submit(second).accepted is not False


def test_end_orphan_refuses_a_run_outside_the_candidate_set(tmp_path: Path) -> None:
    controller, runs = _unknown(tmp_path)
    (runs / "01AAA").mkdir()
    (runs / "01BBB").mkdir()
    with pytest.raises(RunRejectedError, match="not a candidate"):
        controller.end_orphan(_REQ, "01ZZZ", "cancelled")


def test_end_orphan_rejects_an_outcome_outside_the_operator_endings(
    tmp_path: Path,
) -> None:
    controller, runs = _unknown(tmp_path)
    (runs / "01AAA").mkdir()
    with pytest.raises(RunRejectedError, match="cancelled|superseded"):
        controller.end_orphan(_REQ, "01AAA", "completed")


# -- task 5 fix round 1 ------------------------------------------------------


def test_end_orphan_missing_binary_is_a_refusal(tmp_path: Path) -> None:
    """A missing/non-executable maestro binary must come back as a
    RunRejectedError, not an unhandled OSError escaping the controller."""
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    config = _config(tmp_path, cli)
    controller = RunController(config, poll_interval=0.05, materialize_timeout=0.3)
    receipt = controller.submit(_request(head))
    assert receipt.accepted is None
    assert config.maestro_home is not None
    runs = config.maestro_home / "projects/github.com/owner/deployer/runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "01AAA").mkdir()

    broken = dataclasses.replace(config, maestro_cli=tmp_path / "no-such-binary")
    broken_controller = RunController(
        broken, poll_interval=0.05, materialize_timeout=0.3
    )
    with pytest.raises(RunRejectedError, match="cannot run maestro run-end"):
        broken_controller.end_orphan(_REQ, "01AAA", "cancelled")


def test_resolve_unknown_refuses_a_settled_record(tmp_path: Path) -> None:
    controller, runs = _unknown(tmp_path)
    (runs / "01LATE").mkdir()
    controller.resolve_unknown(_REQ)
    assert _state(controller, _REQ) == "materialized"
    with pytest.raises(RunRejectedError, match="not launch_unknown"):
        controller.resolve_unknown(_REQ)


def test_end_orphan_refuses_a_settled_record(tmp_path: Path) -> None:
    controller, runs = _unknown(tmp_path)
    (runs / "01LATE").mkdir()
    controller.resolve_unknown(_REQ)
    assert _state(controller, _REQ) == "materialized"
    with pytest.raises(RunRejectedError, match="not launch_unknown"):
        controller.end_orphan(_REQ, "01LATE", "cancelled")


# -- task 6: Mode-1 control verbs --------------------------------------------


def _materialized(tmp_path: Path, script: str) -> RunController:
    head = _repo(tmp_path / "ws")
    cli = tmp_path / "verb-maestro"
    cli.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(script).strip() + "\n")
    cli.chmod(0o755)
    controller = RunController(_config(tmp_path, cli), materialize_timeout=10.0)
    controller.submit(_request(head))
    return controller


_PUBLISH_THEN_ECHO = """
import os, pathlib, sys
home = pathlib.Path(os.environ["MAESTRO_HOME"])
d = home / "projects/github.com/owner/deployer/runs/01AAA"
if not d.exists():
    d.mkdir(parents=True)
    (d / "state.db").write_text("")
    sys.exit(0)
print(" ".join(sys.argv[1:]))
"""


def test_status_is_addressed_to_the_adopted_run(tmp_path: Path) -> None:
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    outcome = controller.control(_REQ, "status")
    assert outcome.ok
    assert "--run 01AAA" in outcome.stdout


def test_verb_outside_the_allowlist_is_refused(tmp_path: Path) -> None:
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    with pytest.raises(RunRejectedError, match="not allowlisted"):
        controller.control(_REQ, "workstream-continue")


def test_approve_requires_a_task_id(tmp_path: Path) -> None:
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    with pytest.raises(RunRejectedError, match="task_id"):
        controller.control(_REQ, "approve")


def test_verbs_refuse_a_request_with_no_run_yet(tmp_path: Path) -> None:
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(
        _config(tmp_path, cli), poll_interval=0.05, materialize_timeout=0.3
    )
    controller.submit(_request(head))
    with pytest.raises(RunRejectedError, match="no run"):
        controller.control(_REQ, "status")


def test_control_missing_binary_is_a_refusal(tmp_path: Path) -> None:
    """A missing/non-executable maestro binary must come back as a
    RunRejectedError, not an unhandled OSError escaping the controller."""
    head = _repo(tmp_path / "ws")
    cli = tmp_path / "verb-maestro"
    cli.write_text(
        "#!/usr/bin/env python3\n" + textwrap.dedent(_PUBLISH_THEN_ECHO).strip() + "\n"
    )
    cli.chmod(0o755)
    config = _config(tmp_path, cli)
    controller = RunController(config, materialize_timeout=10.0)
    controller.submit(_request(head))

    broken = dataclasses.replace(config, maestro_cli=tmp_path / "no-such-binary")
    broken_controller = RunController(broken, materialize_timeout=10.0)
    with pytest.raises(RunRejectedError, match="cannot run maestro"):
        broken_controller.control(_REQ, "status")


# -- task 6 fix round 1 -------------------------------------------------------


def test_approve_is_addressed_to_the_adopted_run_with_its_task_id(
    tmp_path: Path,
) -> None:
    """`maestro approve TASK_ID --run RUN_ID` — losing `--run` means approve
    acts on whatever the resolver picks, not this request's run."""
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    outcome = controller.control(_REQ, "approve", task_id="T1")
    assert outcome.ok
    assert "approve T1 --run 01AAA" in outcome.stdout


def test_retry_requires_a_task_id(tmp_path: Path) -> None:
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    with pytest.raises(RunRejectedError, match="task_id"):
        controller.control(_REQ, "retry")


def test_retry_is_addressed_to_the_adopted_run_with_its_task_id(
    tmp_path: Path,
) -> None:
    """`maestro retry TASK_ID` requires a positional task id — without one
    every call fails with a missing-argument error."""
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    outcome = controller.control(_REQ, "retry", task_id="T2")
    assert outcome.ok
    assert "retry T2 --run 01AAA" in outcome.stdout


def test_stop_is_not_allowlisted(tmp_path: Path) -> None:
    """`maestro stop` takes no `--run`/positional: it stops the whole
    scheduler process, not one run, so it must not be a per-request verb."""
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    with pytest.raises(RunRejectedError, match="not allowlisted"):
        controller.control(_REQ, "stop")


# -- task 7 fix round 1: an unsafe request_id must not raise out of any -----
# -- non-submit entry point --------------------------------------------------
#
# `submit()` already refuses rather than raises on an unsafe request_id (see
# `test_submit_refuses_rather_than_raises_on_an_unsafe_request_id` above). A
# request_id reaching `record`/`control`/`resolve_unknown`/`end_orphan` comes
# from an HTTP PATH PARAMETER, which — unlike `RunRequest.request_id` — carries
# no pydantic constraint, so `RunStore._record_path`'s bare `RunStoreError` was
# reachable through all four with the control plane ON (the off-by-default
# fixture masks this: `_require_on()` raises `ControlPlaneOff` first).

_UNSAFE_ID = "bad/id with space"


def test_record_with_an_unsafe_request_id_is_a_refusal_not_a_crash(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "unused-maestro"
    controller = RunController(_config(tmp_path, cli))
    with pytest.raises(RunRejectedError, match="unsafe|cannot use request_id"):
        controller.record(_UNSAFE_ID)


def test_control_with_an_unsafe_request_id_is_a_refusal_not_a_crash(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "unused-maestro"
    controller = RunController(_config(tmp_path, cli))
    with pytest.raises(RunRejectedError, match="unsafe|cannot use request_id"):
        controller.control(_UNSAFE_ID, "status")


def test_resolve_unknown_with_an_unsafe_request_id_is_a_refusal_not_a_crash(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "unused-maestro"
    controller = RunController(_config(tmp_path, cli))
    with pytest.raises(RunRejectedError, match="unsafe|cannot use request_id"):
        controller.resolve_unknown(_UNSAFE_ID)


def test_end_orphan_with_an_unsafe_request_id_is_a_refusal_not_a_crash(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "unused-maestro"
    controller = RunController(_config(tmp_path, cli))
    with pytest.raises(RunRejectedError, match="unsafe|cannot use request_id"):
        controller.end_orphan(_UNSAFE_ID, "01AAA", "cancelled")


def test_control_run_end_survives_a_lock_release_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LockBusyError` is a `RunStoreError` subclass: `release_lock` raising
    from inside `mark_terminal` (a corrupt or foreign-held lock) must also
    come back as a refusal, not escape `control()` unhandled."""
    controller = _materialized(
        tmp_path,
        """
        import os, pathlib, sys
        home = pathlib.Path(os.environ["MAESTRO_HOME"])
        d = home / "projects/github.com/owner/deployer/runs/01AAA"
        if not d.exists():
            d.mkdir(parents=True)
            (d / "state.db").write_text("")
        sys.exit(0)
        """,
    )

    def _boom(self: RunStore, request_id: str, outcome: str) -> None:
        raise LockBusyError("lock held by someone else")

    monkeypatch.setattr(RunStore, "mark_terminal", _boom)
    with pytest.raises(RunRejectedError, match="run-end"):
        controller.control(_REQ, "run-end", outcome="cancelled")


# -- I7: an unreadable runs/ must not read the same as an absent one --------


def _skip_if_root() -> None:
    """`chmod 0o000` denies nothing to root; a test that would silently
    pass without proving anything must skip loudly instead."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("running as root: chmod does not deny self access")


def test_listing_treats_a_genuinely_absent_runs_dir_as_clean_empty(
    tmp_path: Path,
) -> None:
    assert RunController._listing(tmp_path / "no-such-runs") == []


def test_listing_refuses_an_unreadable_runs_dir(tmp_path: Path) -> None:
    """A permissions fault must not read the same as "no runs yet"."""
    _skip_if_root()
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "01AAA").mkdir()
    runs.chmod(0o000)
    try:
        with pytest.raises(OSError):
            RunController._listing(runs)
    finally:
        runs.chmod(0o755)


def test_listing_refuses_when_runs_is_a_plain_file(tmp_path: Path) -> None:
    """A stray file sitting where `runs/` belongs is grouped with the
    fault, not with absence: `NotADirectoryError` propagates rather than
    reading as clean-empty. This is a deliberate divergence from `_subdirs`
    (`dispatcher/core/collectors/maestro.py:222-232`), which treats
    `NotADirectoryError` the same as `FileNotFoundError` — `_listing` backs
    correlation, where this is a genuine anomaly, not the ordinary
    "nothing here yet" case."""
    runs = tmp_path / "runs"
    runs.write_text("not a directory")
    with pytest.raises(NotADirectoryError):
        RunController._listing(runs)


def test_submit_refuses_rather_than_snapshot_an_unreadable_runs_dir(
    tmp_path: Path,
) -> None:
    """The pre-launch snapshot (`submit`, before `reserve`) must not
    silently read an unreadable `runs/` as "no runs" — a later correlation
    would otherwise see every existing run as new."""
    _skip_if_root()
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01AAA")
    config = _config(tmp_path, cli)
    assert config.maestro_home is not None
    runs = config.maestro_home / "projects/github.com/owner/deployer/runs"
    runs.mkdir(parents=True)
    (runs / "01EXISTING").mkdir()
    runs.chmod(0o000)
    try:
        controller = RunController(config, materialize_timeout=10.0)
        receipt = controller.submit(_request(head))
    finally:
        runs.chmod(0o755)
    assert receipt.accepted is False
    assert "cannot use request_id" in (receipt.reason or "")


def test_resolve_unknown_refuses_rather_than_zero_candidates_when_unreadable(
    tmp_path: Path,
) -> None:
    """A permissions fault while recounting candidates must not read as
    "nothing new to correlate" — `resolve_unknown` would otherwise claim
    "the launch may never have started" when the truth is "I could not
    look"."""
    _skip_if_root()
    controller, runs = _unknown(tmp_path)
    (runs / "01LATE").mkdir()
    runs.chmod(0o000)
    try:
        with pytest.raises(RunRejectedError, match="cannot list runs"):
            controller.resolve_unknown(_REQ)
    finally:
        runs.chmod(0o755)


def test_end_orphan_refuses_rather_than_a_stale_candidate_set_when_unreadable(
    tmp_path: Path,
) -> None:
    """Same fault, the other resolution path: naming a run against a
    candidate set that could not actually be read must refuse, not fall
    through to "not a candidate" for a run that may well still be one."""
    _skip_if_root()
    controller, runs = _unknown(tmp_path)
    (runs / "01AAA").mkdir()
    runs.chmod(0o000)
    try:
        with pytest.raises(RunRejectedError, match="cannot list runs"):
            controller.end_orphan(_REQ, "01AAA", "cancelled")
    finally:
        runs.chmod(0o755)


# --- Repository binding of the verbs (pilot finding, 2026-08-24) -------------
#
# maestro derives which repository a run belongs to from the directory it is
# standing in. `_launch` always passed `cwd=<checkout>`; `control` and
# `end_orphan` did not, so every verb inherited the dispatcher SERVER's cwd
# and asked maestro about the wrong repository — the slice-0 pilot got
# "no run 01M0SB13... for github.com/andrei-shtanakov/dispatcher; known runs:
# none" for a run that was alive and progressing. Nothing observed the child's
# cwd, so the suite stayed green through the whole of it.

_RECORD_CWD = """
import os, pathlib, sys
# Logged BEFORE any branch: an earlier version logged after the publish
# shortcut and silently recorded nothing for the verbs that take it, which
# would have let this very test pass while measuring the launch instead.
log = os.environ["FAKE_MAESTRO_CWD_LOG"]
with open(log, "a") as fh:
    fh.write(sys.argv[1] + " " + os.getcwd() + "\\n")
home = pathlib.Path(os.environ["MAESTRO_HOME"])
d = home / "projects/github.com/owner/deployer/runs/01AAA"
if not d.exists():
    d.mkdir(parents=True)
    (d / "state.db").write_text("")
    sys.exit(0)
print(" ".join(sys.argv[1:]))
"""


def _cwd_log_lines(log: Path) -> list[tuple[str, str]]:
    if not log.exists():
        return []
    return [
        (line.split(" ", 1)[0], line.split(" ", 1)[1])
        for line in log.read_text().splitlines()
        if line
    ]


@pytest.mark.parametrize(
    ("verb", "kwargs"),
    [
        ("status", {}),
        ("retry", {"task_id": "red-test"}),
        ("approve", {"task_id": "red-test"}),
        ("run-end", {"outcome": "cancelled"}),
    ],
)
def test_every_verb_runs_maestro_in_the_run_s_own_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: str, kwargs: dict
) -> None:
    """All four verbs, one guarantee — the checkout, never the process cwd."""
    log = tmp_path / "cwd.log"
    monkeypatch.setenv("FAKE_MAESTRO_CWD_LOG", str(log))
    controller = _materialized(tmp_path, _RECORD_CWD)
    checkout = tmp_path / "ws" / "deployer"

    # The server's own cwd is deliberately NOT the checkout — inheriting it
    # is the defect, so a test run from inside the checkout would pass while
    # measuring nothing.
    assert Path.cwd() != checkout

    outcome = controller.control(_REQ, verb, **kwargs)
    assert outcome.ok

    recorded = _cwd_log_lines(log)
    assert recorded, f"{verb} never reached the fake maestro"
    ran_verb, ran_cwd = recorded[-1]
    assert ran_verb == verb
    assert Path(ran_cwd).resolve() == checkout.resolve(), (
        f"{verb} ran in {ran_cwd}, not the run's checkout {checkout}: "
        "maestro would resolve the wrong repository and report the run missing"
    )


def test_run_end_through_the_resolution_path_also_binds_to_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`end_orphan` is a second subprocess site and needs the same binding.

    Ending the wrong repository's run is the worse of the two mistakes, so
    this path is pinned separately rather than assumed to follow `control`.
    """
    log = tmp_path / "cwd.log"
    monkeypatch.setenv("FAKE_MAESTRO_CWD_LOG", str(log))
    # `_unknown` leaves the request in launch_unknown, which is the only
    # state end_orphan acts from; its fake never publishes, so the two
    # candidates below are what the operator would be shown.
    controller, runs = _unknown(tmp_path)
    checkout = tmp_path / "ws" / "deployer"
    cli = tmp_path / "fake-maestro"
    cli.write_text(
        "#!/usr/bin/env python3\n" + textwrap.dedent(_RECORD_CWD).strip() + "\n"
    )
    cli.chmod(0o755)
    (runs / "01BBB").mkdir(exist_ok=True)
    (runs / "01CCC").mkdir(exist_ok=True)

    controller.end_orphan(_REQ, "01BBB", "cancelled")

    recorded = _cwd_log_lines(log)
    assert recorded, "end_orphan never reached the fake maestro"
    ran_verb, ran_cwd = recorded[-1]
    assert ran_verb == "run-end"
    assert Path(ran_cwd).resolve() == checkout.resolve()


def test_a_verb_refuses_when_the_recorded_checkout_moved_to_another_repo(
    tmp_path: Path,
) -> None:
    """The recorded path is re-checked, not trusted.

    A directory can be moved, replaced, or re-pointed at a different remote
    between launch and verb. Acting on the wrong repository is worse than
    refusing, so the identity is verified against the record's `repo_key`.
    """
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    checkout = tmp_path / "ws" / "deployer"
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "remote",
            "set-url",
            "origin",
            "git@github.com:owner/somewhere-else.git",
        ],
        check=True,
    )
    with pytest.raises(RunRejectedError, match="somewhere-else"):
        controller.control(_REQ, "status")


def test_a_record_with_no_checkout_is_refused_not_run_from_the_server_cwd(
    tmp_path: Path,
) -> None:
    """A pre-binding record must fail loud.

    Falling back to the process cwd is precisely the defect, and guessing a
    checkout from `repo_key` would pick one of possibly several clones.
    """
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    store = RunStore(tmp_path / "state")
    record = store.get(_REQ)
    assert record is not None
    path = tmp_path / "state" / "requests" / f"{_REQ}.json"
    data = json.loads(path.read_text())
    del data["checkout"]  # a record written before the field existed
    path.write_text(json.dumps(data))

    with pytest.raises(RunRejectedError, match="predates checkout binding"):
        controller.control(_REQ, "status")


def test_the_launch_persists_the_checkout_it_used(tmp_path: Path) -> None:
    """The binding is durable — a verb reads it back, never the request body."""
    _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    record = RunStore(tmp_path / "state").get(_REQ)
    assert record is not None
    assert Path(record.checkout).resolve() == (tmp_path / "ws" / "deployer").resolve()


def test_a_relative_workspace_root_still_persists_an_absolute_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative root must not put the cwd dependency back (PR #174 review).

    `config.roots` is only `expanduser()`-normalised, so a relative root in
    dispatcher.toml produced a relative checkout — which the launch passed
    to `cwd=` and the record persisted for later verbs. Both then resolve
    against whatever directory the server happens to be in, which is the
    dependency this whole binding exists to remove, and it survives a
    restart from elsewhere as a wrong path rather than an error.
    """
    _repo(tmp_path / "ws")
    monkeypatch.chdir(tmp_path)
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01AAA")
    config = dataclasses.replace(_config(tmp_path, cli), roots=(Path("ws"),))
    controller = RunController(config, materialize_timeout=10.0)
    controller.submit(_request(_repo_head(tmp_path / "ws")))

    record = RunStore(tmp_path / "state").get(_REQ)
    assert record is not None
    assert Path(record.checkout).is_absolute(), (
        f"persisted {record.checkout!r}: a later verb would resolve it "
        "against the server's cwd"
    )
    assert Path(record.checkout).resolve() == (tmp_path / "ws" / "deployer").resolve()


def test_a_relative_recorded_checkout_is_refused_not_resolved(
    tmp_path: Path,
) -> None:
    """An older record holding a relative path fails loud.

    Resolving it at verb time would resolve against the server's cwd, which
    is exactly the defect — so this refuses rather than guesses.
    """
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    path = tmp_path / "state" / "requests" / f"{_REQ}.json"
    data = json.loads(path.read_text())
    data["checkout"] = "ws/deployer"
    path.write_text(json.dumps(data))

    with pytest.raises(RunRejectedError, match="relative"):
        controller.control(_REQ, "status")


# --- ATP catalog is a declared precondition, not ambient state ------------
#
# The slice-0 pilot's first run died two seconds after a receipt that said
# "started": `$ATP_CATALOG` lived in an interactive shell, the server had
# been started without it, and the child inherited that absence. maestro had
# already published the run directory before reading the catalog, so the
# record went to `materialized` and nothing about the failure was visible
# from the console.

_RECORD_ENV = """
import os, pathlib, sys
log = os.environ["FAKE_MAESTRO_ENV_LOG"]
with open(log, "a") as fh:
    fh.write(os.environ.get("ATP_CATALOG", "<unset>") + "\\n")
home = pathlib.Path(os.environ["MAESTRO_HOME"])
d = home / "projects/github.com/owner/deployer/runs/01AAA"
if not d.exists():
    d.mkdir(parents=True)
    (d / "state.db").write_text("")
sys.exit(0)
"""


def test_the_launched_child_gets_the_configured_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injected from config — and the configured value WINS over ambient."""
    log = tmp_path / "env.log"
    monkeypatch.setenv("FAKE_MAESTRO_ENV_LOG", str(log))
    # An ambient value that must NOT be what the child sees: inheriting it
    # is the dependency on how the server was started that this ends.
    monkeypatch.setenv("ATP_CATALOG", "/somewhere/ambient/catalog.toml")
    head = _repo(tmp_path / "ws")
    cli = tmp_path / "env-maestro"
    cli.write_text(
        "#!/usr/bin/env python3\n" + textwrap.dedent(_RECORD_ENV).strip() + "\n"
    )
    cli.chmod(0o755)
    controller = RunController(_config(tmp_path, cli), materialize_timeout=10.0)

    receipt = controller.submit(_request(head))
    assert receipt.accepted is True

    seen = log.read_text().split()
    assert seen, "the child never ran"
    assert seen[-1] == str(tmp_path / "agents-catalog.toml"), (
        f"child saw {seen[-1]!r}: the ambient value leaked through"
    )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda tmp_path, c: None, "not configured"),
        (lambda tmp_path, c: tmp_path / "gone.toml", "does not exist"),
        (lambda tmp_path, c: tmp_path, "not a regular file"),
        (lambda tmp_path, c: Path("relative/catalog.toml"), "absolute"),
    ],
    ids=["unconfigured", "missing-file", "a-directory", "relative"],
)
def test_submit_refuses_before_starting_anything_when_the_catalog_is_unusable(
    tmp_path: Path, mutate, match: str
) -> None:
    """`accepted: false`, and maestro is never executed.

    This is the decidable-false case (spec §5.3): dispatcher KNOWS no run
    was created, because it refused before the lock, before the record and
    before the process. Asserting only on the receipt would not show that —
    a child that ran and failed could produce the same words.
    """
    head = _repo(tmp_path / "ws")
    marker = tmp_path / "maestro-was-executed"
    cli = tmp_path / "tripwire-maestro"
    cli.write_text(
        "#!/usr/bin/env python3\n"
        f"import pathlib; pathlib.Path({str(marker)!r}).write_text('x')\n"
    )
    cli.chmod(0o755)
    config = dataclasses.replace(
        _config(tmp_path, cli), atp_catalog=mutate(tmp_path, None)
    )
    controller = RunController(config, materialize_timeout=10.0)

    receipt = controller.submit(_request(head))

    assert receipt.accepted is False
    assert match in (receipt.reason or "")
    assert not marker.exists(), "maestro was executed despite the refusal"
    # Nothing was reserved either: no lock to release, no record to resolve.
    assert RunStore(tmp_path / "state").get(_REQ) is None


def test_status_still_works_with_no_catalog_configured(tmp_path: Path) -> None:
    """A read verb must not be held hostage to a launch precondition.

    `status` resolves no models. Refusing it for a missing catalog would
    undo the read path #174 restored — a control plane that cannot report
    on a run is worse than one that cannot retry a task.
    """
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    controller._config = dataclasses.replace(controller._config, atp_catalog=None)
    outcome = controller.control(_REQ, "status")
    assert outcome.ok


_ECHO_CATALOG = """
import os, pathlib, sys
home = pathlib.Path(os.environ["MAESTRO_HOME"])
d = home / "projects/github.com/owner/deployer/runs/01AAA"
if not d.exists():
    d.mkdir(parents=True)
    (d / "state.db").write_text("")
    sys.exit(0)
print("ATP_CATALOG=" + os.environ.get("ATP_CATALOG", "<unset>"))
"""


def test_a_verb_does_not_inherit_an_ambient_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unconfigured means ABSENT, not "whatever the shell had".

    Inheriting it would be the dependency on how the server was started
    that this whole precondition ends, and it would answer the same
    question two ways: `submit` refuses outright while a verb quietly used
    the ambient value (PR #176 Copilot review).
    """
    monkeypatch.setenv("ATP_CATALOG", "/somewhere/ambient/catalog.toml")
    controller = _materialized(tmp_path, _ECHO_CATALOG)
    controller._config = dataclasses.replace(controller._config, atp_catalog=None)

    outcome = controller.control(_REQ, "status")

    assert outcome.ok
    assert "ATP_CATALOG=<unset>" in outcome.stdout, (
        f"the ambient value leaked into the verb: {outcome.stdout!r}"
    )


def test_a_verb_gets_the_configured_catalog_when_there_is_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And when configured, the configured value wins over the ambient one."""
    monkeypatch.setenv("ATP_CATALOG", "/somewhere/ambient/catalog.toml")
    controller = _materialized(tmp_path, _ECHO_CATALOG)

    outcome = controller.control(_REQ, "status")

    assert outcome.ok
    assert f"ATP_CATALOG={tmp_path / 'agents-catalog.toml'}" in outcome.stdout


# --- Run logs (spec §10: the last reason to open a terminal) --------------


def _with_logs(tmp_path: Path, **files: str) -> RunController:
    """A materialized run whose log directory holds `files`."""
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    logs = tmp_path / "mhome/projects/github.com/owner/deployer/runs/01AAA/logs"
    logs.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (logs / name.replace("__", ".")).write_text(body)
    return controller


def test_timeline_is_read_from_maestros_own_events_file(tmp_path: Path) -> None:
    controller = _with_logs(
        tmp_path,
        events__jsonl=(
            '{"timestamp": "T1", "event": "task_ready", "task_id": "red"}\n'
            '{"timestamp": "T2", "event": "task_failed", "task_id": "red",'
            ' "message": "exit 1"}\n'
        ),
        red__log="agent output",
    )
    logs = controller.logs(_REQ)
    assert [e.event for e in logs.events] == ["task_ready", "task_failed"]
    assert logs.events[-1].message == "exit 1"
    assert logs.task_logs == ["red"]
    assert logs.truncated is False


def test_a_half_written_line_survives_as_raw(tmp_path: Path) -> None:
    """A truncated tail is ordinary while a run is live.

    Dropping the line would make the console disagree with the file it
    claims to be showing — the reader would see a timeline that is missing
    its most recent, and most interesting, entry.
    """
    controller = _with_logs(
        tmp_path,
        events__jsonl='{"timestamp": "T1", "event": "task_ready"}\n{"timesta',
    )
    logs = controller.logs(_REQ)
    assert len(logs.events) == 2
    assert logs.events[-1].event == ""
    assert logs.events[-1].raw == '{"timesta'


@pytest.mark.skipif(
    os.geteuid() == 0,
    reason="chmod 000 does not stop root: under uid 0 the file reads fine "
    "and the warning this test is about never fires (codex round 7, minor)",
)
def test_an_unreadable_timeline_warns_rather_than_reading_empty(
    tmp_path: Path,
) -> None:
    """Unreadable and empty must not look the same (NFR-02)."""
    controller = _with_logs(tmp_path, events__jsonl="{}\n")
    events = (
        tmp_path
        / "mhome/projects/github.com/owner/deployer/runs/01AAA/logs/events.jsonl"
    )
    events.chmod(0o000)
    try:
        logs = controller.logs(_REQ)
    finally:
        events.chmod(0o644)
    assert logs.events == []
    assert logs.warnings, "an unreadable timeline rendered as an empty one"


def test_an_absent_timeline_is_not_a_warning(tmp_path: Path) -> None:
    """Before maestro's first event there is simply nothing yet."""
    controller = _with_logs(tmp_path)
    logs = controller.logs(_REQ)
    assert logs.events == []
    assert logs.warnings == []


def test_the_timeline_tail_keeps_the_newest_and_says_it_dropped(
    tmp_path: Path,
) -> None:
    """Oldest lines go, never newest — and the reader is told."""
    body = "".join(f'{{"timestamp": "T{i}", "event": "e{i}"}}\n' for i in range(600))
    controller = _with_logs(tmp_path, events__jsonl=body)
    logs = controller.logs(_REQ)
    assert len(logs.events) == RunController._MAX_EVENTS
    assert logs.events[-1].event == "e599", "the newest line was dropped"
    assert logs.truncated is True


def test_a_task_log_is_tailed_and_flagged(tmp_path: Path) -> None:
    big = "x" * (RunController._MAX_TASK_LOG_BYTES + 100) + "TAIL"
    controller = _with_logs(tmp_path, red__log=big)
    log = controller.task_log(_REQ, "red")
    assert log.text.endswith("TAIL")
    assert len(log.text) <= RunController._MAX_TASK_LOG_BYTES
    assert log.truncated is True


@pytest.mark.parametrize(
    "task_id",
    ["../../../etc/passwd", "..", "red/../../x", "red\\..\\x", ""],
)
def test_an_unsafe_task_id_is_refused(tmp_path: Path, task_id: str) -> None:
    """The one part of these paths that arrives off the wire."""
    controller = _with_logs(tmp_path, red__log="x")
    with pytest.raises(RunRejectedError, match="unsafe task id"):
        controller.task_log(_REQ, task_id)


def test_logs_refuse_a_request_with_no_run(tmp_path: Path) -> None:
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(
        _config(tmp_path, cli), poll_interval=0.05, materialize_timeout=0.3
    )
    controller.submit(_request(head))
    with pytest.raises(RunRejectedError, match="no run to read logs from"):
        controller.logs(_REQ)


def test_a_log_name_the_reader_would_reject_is_not_offered(
    tmp_path: Path,
) -> None:
    """The list and the reader must agree (codex review on PR #191, minor).

    Offering a name `task_log()` refuses advertises a log this API will never
    serve, and the console draws a button for it. Not dropped in silence
    either: a file visible on disk and absent here is its own puzzle.
    """
    controller = _with_logs(tmp_path, red__log="ok")
    logs_dir = tmp_path / "mhome/projects/github.com/owner/deployer/runs/01AAA/logs"
    (logs_dir / (("a" * 65) + ".log")).write_text("too long")
    (logs_dir / ".hidden.log").write_text("leading dot")

    logs = controller.logs(_REQ)

    assert logs.task_logs == ["red"]
    assert len(logs.warnings) == 2, logs.warnings
    # And the reader really would have refused them — the two rules are the
    # same rule, asserted here rather than assumed.
    for rejected in ("a" * 65, ".hidden"):
        with pytest.raises(RunRejectedError, match="unsafe task id"):
            controller.task_log(_REQ, rejected)


def test_a_huge_task_log_is_never_read_whole(tmp_path: Path) -> None:
    """The cap must bound the READ, not just the response (codex round 3).

    `read_bytes()` then slicing enforced 256 KiB only after the entire file
    was in memory, so a log larger than the worker could hold would kill the
    server through an endpoint whose whole contract is "the last 256 KiB".

    Measured with `tracemalloc` rather than by asserting which call the code
    makes: the property is bounded memory, and a test that pinned the API
    would pass for any implementation that merely looked right.
    """
    controller = _with_logs(tmp_path)
    logs_dir = tmp_path / "mhome/projects/github.com/owner/deployer/runs/01AAA/logs"
    big = logs_dir / "huge.log"
    payload = b"y" * (1024 * 1024)
    with big.open("wb") as fh:
        for _ in range(16):  # 16 MiB, 64x the cap
            fh.write(payload)
        fh.write(b"THE-TAIL")

    tracemalloc.start()
    try:
        log = controller.task_log(_REQ, "huge")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert log.text.endswith("THE-TAIL")
    assert log.truncated is True
    # Generous: the tail itself, plus its str form, plus overhead. A whole-file
    # read would be an order of magnitude above this.
    assert peak < 4 * RunController._MAX_TASK_LOG_BYTES, (
        f"peak {peak} bytes for a 16 MiB log — the whole file was read"
    )


def test_a_huge_timeline_is_never_read_whole(tmp_path: Path) -> None:
    """The events cap must bound the READ too (codex round 4).

    The same defect `task_log()` had, one endpoint over: `read_text()` then
    a 500-line slice enforced the cap only after the whole file was in
    memory. Symmetric to `test_a_huge_task_log_is_never_read_whole`, and
    measured the same way — the property is bounded memory, not which call
    the code makes.
    """
    controller = _with_logs(tmp_path)
    logs_dir = tmp_path / "mhome/projects/github.com/owner/deployer/runs/01AAA/logs"
    with (logs_dir / "events.jsonl").open("w") as fh:
        for i in range(200_000):  # ~16 MiB of lines
            fh.write(f'{{"timestamp": "T{i}", "event": "e{i}"}}\n')

    tracemalloc.start()
    try:
        logs = controller.logs(_REQ)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert logs.truncated is True
    assert len(logs.events) == RunController._MAX_EVENTS
    assert logs.events[-1].event == "e199999", "the newest line was dropped"
    assert peak < 8 * RunController._MAX_EVENT_BYTES, (
        f"peak {peak} bytes for a ~16 MiB timeline — the whole file was read"
    )


def test_one_giant_jsonl_line_is_byte_capped_not_line_counted(
    tmp_path: Path,
) -> None:
    """Line count alone does not bound memory (owner review of round 4).

    A single line bigger than the byte cap is one line — a reverse reader
    counting to 500 would still swallow all of it. It must come back as a
    bounded raw tail with `truncated` set, not as an empty timeline claiming
    nothing happened, and not whole.
    """
    controller = _with_logs(tmp_path)
    logs_dir = tmp_path / "mhome/projects/github.com/owner/deployer/runs/01AAA/logs"
    giant = '{"event": "padded", "pad": "' + "z" * (2 * 1024 * 1024) + '"}'
    (logs_dir / "events.jsonl").write_text(giant + "\n")

    logs = controller.logs(_REQ)

    assert logs.truncated is True
    assert len(logs.events) == 1, "a giant line must not vanish entirely"
    assert logs.events[0].event == "", "its tail cannot parse — raw, not typed"
    assert len(logs.events[0].raw) <= RunController._MAX_EVENT_BYTES


# --- Identity provenance (codex round 5): a symlink must never let one ----
# --- run's files answer under another run's id ----------------------------


def _second_run_logs(tmp_path: Path) -> Path:
    """A real OTHER run whose logs an attacker-shaped symlink points at."""
    other = tmp_path / "mhome/projects/github.com/owner/deployer/runs/01BBB/logs"
    other.mkdir(parents=True)
    (other / "events.jsonl").write_text('{"event": "FROM_01BBB"}\n')
    (other / "stolen.log").write_text("BODY-OF-01BBB\n")
    return other


def test_a_symlinked_logs_dir_is_refused_not_served(tmp_path: Path) -> None:
    """The observed case: 01AAA/logs -> 01BBB/logs.

    Every earlier check passed it — containment resolved the link first and
    then confirmed the file sat "inside" — while the response still carried
    run_id="01AAA". Refusal, not empty: bytes with an unknown owner are
    worse than none.
    """
    controller = _with_logs(tmp_path)
    logs_a = tmp_path / "mhome/projects/github.com/owner/deployer/runs/01AAA/logs"
    other = _second_run_logs(tmp_path)
    shutil.rmtree(logs_a)
    logs_a.symlink_to(other, target_is_directory=True)

    with pytest.raises(RunRejectedError, match="redirected by a symlink"):
        controller.logs(_REQ)
    with pytest.raises(RunRejectedError, match="redirected by a symlink"):
        controller.task_log(_REQ, "stolen")


def test_a_symlinked_events_file_is_not_followed(tmp_path: Path) -> None:
    """One level down: the dir is real, events.jsonl points elsewhere.

    A warning rather than a refusal — the task-log listing is still this
    run's own — but the foreign timeline must not appear.
    """
    controller = _with_logs(tmp_path, red__log="ours")
    logs_a = tmp_path / "mhome/projects/github.com/owner/deployer/runs/01AAA/logs"
    other = _second_run_logs(tmp_path)
    (logs_a / "events.jsonl").symlink_to(other / "events.jsonl")

    logs = controller.logs(_REQ)

    assert logs.events == [], "the foreign timeline was served"
    assert any("symlink" in w for w in logs.warnings), logs.warnings
    assert logs.task_logs == ["red"], "the run's own listing should survive"


def test_a_symlinked_task_log_is_refused_and_not_advertised(
    tmp_path: Path,
) -> None:
    """The leaf: red.log real, stolen.log a symlink into another run."""
    controller = _with_logs(tmp_path, red__log="ours")
    logs_a = tmp_path / "mhome/projects/github.com/owner/deployer/runs/01AAA/logs"
    other = _second_run_logs(tmp_path)
    (logs_a / "stolen.log").symlink_to(other / "stolen.log")

    logs = controller.logs(_REQ)
    assert logs.task_logs == ["red"], "the symlink was advertised"
    assert any("symlink" in w for w in logs.warnings), logs.warnings

    with pytest.raises(RunRejectedError, match="symlink"):
        controller.task_log(_REQ, "stolen")
    # And the honest file still reads — provenance must not break the
    # ordinary path.
    assert controller.task_log(_REQ, "red").text == "ours"


# --- Absence vs youth (codex round 6, major): the three states ------------


def test_a_vanished_run_directory_refuses_the_logs_read(tmp_path: Path) -> None:
    """State 1: the run directory is GONE — refusal, for both surfaces.

    `view()` maps this to `run=None`; /logs answering 200 for the same
    request would have the two surfaces disagree about whether the run
    exists. A durable run_id proves the run existed, not that it still does.
    """
    controller = _with_logs(tmp_path, red__log="x")
    run_dir = tmp_path / "mhome/projects/github.com/owner/deployer/runs/01AAA"
    shutil.rmtree(run_dir)

    with pytest.raises(RunRejectedError, match="directory is gone"):
        controller.logs(_REQ)
    with pytest.raises(RunRejectedError, match="directory is gone"):
        controller.task_log(_REQ, "red")


def test_a_run_that_has_not_logged_yet_is_empty_success(tmp_path: Path) -> None:
    """State 2: the run directory EXISTS, `logs/` does not — 200, empty.

    "The run vanished" and "the run has not written anything yet" must not
    collapse into one answer: the second is every young run's first seconds.
    """
    controller = _with_logs(tmp_path)
    logs_dir = tmp_path / "mhome/projects/github.com/owner/deployer/runs/01AAA/logs"
    shutil.rmtree(logs_dir)  # the run dir itself stays

    logs = controller.logs(_REQ)

    assert logs.events == []
    assert logs.task_logs == []
    assert logs.warnings == []
    # State 3 — an ordinary run with logs — is the rest of this file.


# --- Boundary-aligned tails (codex round 6, minor) ------------------------


def _line(i: int, size: int) -> str:
    """One JSONL event padded to exactly `size` bytes including newline."""
    head = f'{{"event": "e{i:05d}", "pad": "'
    body = "x" * (size - len(head) - 3)
    return head + body + '"}\n'


def test_a_mid_line_tail_drops_exactly_the_fragment(tmp_path: Path) -> None:
    """The cut lands inside a line: the fragment goes, whole lines stay."""
    controller = _with_logs(tmp_path)
    logs_dir = tmp_path / "mhome/projects/github.com/owner/deployer/runs/01AAA/logs"
    cap = RunController._MAX_EVENT_BYTES
    # One giant line longer than the cap, then three ordinary events: the
    # tail must begin inside the giant line.
    giant = '{"event": "huge", "pad": "' + "z" * (cap + 1000) + '"}\n'
    normals = [_line(i, 2048) for i in range(3)]
    (logs_dir / "events.jsonl").write_text(giant + "".join(normals))

    logs = controller.logs(_REQ)

    assert logs.truncated is True
    assert [e.event for e in logs.events] == ["e00000", "e00001", "e00002"], (
        "the fragment of the giant line should be the only casualty"
    )


def test_a_boundary_aligned_tail_keeps_its_first_complete_event(
    tmp_path: Path,
) -> None:
    """The cut lands exactly after a newline: NOTHING may be dropped.

    This is the case the old inference lost: `truncated` was read as "the
    first line is partial", and a real event that fit both caps vanished
    from the newest visible slice.
    """
    controller = _with_logs(tmp_path)
    logs_dir = tmp_path / "mhome/projects/github.com/owner/deployer/runs/01AAA/logs"
    cap = RunController._MAX_EVENT_BYTES
    line_size = 2048
    assert cap % line_size == 0, "the alignment this test is ABOUT"
    tail_lines = cap // line_size  # exactly the byte cap, whole lines
    assert tail_lines <= RunController._MAX_EVENTS, (
        "the line cap would hide what the byte boundary does"
    )
    prefix = [_line(i, line_size) for i in range(3)]  # dropped
    tail = [_line(100 + i, line_size) for i in range(tail_lines)]  # kept
    (logs_dir / "events.jsonl").write_text("".join(prefix + tail))

    logs = controller.logs(_REQ)

    assert logs.truncated is True
    assert len(logs.events) == tail_lines, "a complete event was dropped"
    assert logs.events[0].event == "e00100", (
        "the first complete event of the tail is the one the inference lost"
    )
