"""RunController launch path (spec §5.3, §5.4)."""

import subprocess
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.run_controller import RunController
from dispatcher.core.run_request import RunRequest
from dispatcher.core.run_store import RunStore

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
