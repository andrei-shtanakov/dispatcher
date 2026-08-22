"""RunController launch path (spec §5.3, §5.4)."""

import dataclasses
import subprocess
import textwrap
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
    (repo / "tasks.yaml").write_text("tasks: []\n")
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


def _fake_maestro(path: Path, *, creates_run: str | None, exit_code: int = 0) -> Path:
    """A stand-in that publishes a run directory the way maestro does.

    It reads MAESTRO_HOME from the environment on purpose: a controller that
    launches under one root and watches another must fail this test.
    """
    body = textwrap.dedent(
        f"""
        #!/usr/bin/env python3
        import os, pathlib, sys
        run_id = {creates_run!r}
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


def _config(tmp_path: Path, cli: Path) -> DispatcherConfig:
    return DispatcherConfig(
        roots=(tmp_path / "ws",),
        maestro_home=tmp_path / "mhome",
        run_state_dir=tmp_path / "state",
        maestro_cli=cli,
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
