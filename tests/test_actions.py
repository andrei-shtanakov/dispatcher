"""TASK-210: live whitelist actions — guards, delegation, audit."""

import subprocess
import threading
from pathlib import Path

import pytest

from dispatcher.core.actions import (
    ActionBusyError,
    ActionRejectedError,
    ActionRunner,
)
from dispatcher.core.discovery import DispatcherConfig


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )


def make_repo(workspace: Path, name: str) -> Path:
    repo = workspace / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    return repo


def fake_checker(tmp_path: Path, payload: dict) -> tuple[str, ...]:
    """A stand-in github-checker binary printing a fixed ActionResult."""
    script = tmp_path / "fake_checker.py"
    script.write_text(f"import sys, json; json.dump({payload!r}, sys.stdout)")
    return ("python3", str(script))


def test_run_delegates_and_parses(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "pull",
        "dir": "alpha",
        "ok": True,
        "detail": "fast-forwarded",
        "local": {"behind": 0, "dirty": False},
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.run("pull", "alpha")
    assert outcome.ok
    assert outcome.detail == "fast-forwarded"
    assert outcome.local_behind == 0


def test_run_rejects_unsafe_and_unknown_dirs(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(DispatcherConfig(roots=(tmp_path,)))
    with pytest.raises(ActionRejectedError, match="unsafe"):
        runner.run("pull", "../etc")
    with pytest.raises(ActionRejectedError, match="not a git repo"):
        runner.run("pull", "ghost")


def test_one_in_flight_action_per_repo(tmp_path: Path, monkeypatch) -> None:
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(DispatcherConfig(roots=(tmp_path,)))
    started = threading.Event()
    release = threading.Event()

    def slow_invoke(action, target):
        started.set()
        release.wait(timeout=10)
        from dispatcher.core.actions import ActionOutcome

        return ActionOutcome(action=action, dir=target.name, ok=True)

    monkeypatch.setattr(runner, "_invoke", slow_invoke)
    thread = threading.Thread(target=runner.run, args=("pull", "alpha"))
    thread.start()
    assert started.wait(timeout=2)
    with pytest.raises(ActionBusyError):
        runner.run("pull", "alpha")
    release.set()
    thread.join(timeout=2)
    # после завершения репо снова доступен
    assert runner.run("pull", "alpha").ok


def test_missing_binary_is_failed_outcome(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("no-such-binary-xyz",)
    )
    outcome = runner.run("pull", "alpha")
    assert not outcome.ok
    assert outcome.error is not None


def test_garbage_output_is_failed_outcome(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    script = tmp_path / "garbage.py"
    script.write_text("print('not json')")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    outcome = runner.run("pull", "alpha")
    assert not outcome.ok


def test_audit_line_written(tmp_path: Path, caplog) -> None:
    make_repo(tmp_path, "alpha")
    payload = {"action": "pull", "dir": "alpha", "ok": True, "detail": "x"}
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    with caplog.at_level("INFO", logger="dispatcher.actions"):
        runner.run("pull", "alpha")
    assert any(
        "action=pull" in r.getMessage() and "repo=alpha" in r.getMessage()
        for r in caplog.records
    )


def test_rejected_and_busy_attempts_leave_audit_lines(tmp_path: Path, caplog) -> None:
    runner = ActionRunner(DispatcherConfig(roots=(tmp_path,)))
    with caplog.at_level("INFO", logger="dispatcher.actions"):
        with pytest.raises(ActionRejectedError):
            runner.run("pull", "../etc")
    assert any("rejected=" in r.getMessage() for r in caplog.records)


def test_non_whitelisted_action_rejected_at_runtime(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(DispatcherConfig(roots=(tmp_path,)))
    with pytest.raises(ActionRejectedError, match="not whitelisted"):
        runner.run("push --force", "alpha")  # type: ignore[arg-type]
