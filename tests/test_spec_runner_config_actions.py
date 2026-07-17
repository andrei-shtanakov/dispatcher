import subprocess
import threading
from pathlib import Path

import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.spec_runner_config_actions import (
    ConfigCandidate,
    SpecRunnerConfigActionRunner,
    SpecRunnerConfigBusyError,
    SpecRunnerConfigConflictError,
    SpecRunnerConfigRejectedError,
)

_PROJECT_YAML = """
project: alpha
spec_runner:
  max_retries: 3
  task_timeout_minutes: 30
  claude_command: claude
  auto_commit: true
  create_git_branch: true
  run_tests_on_done: true
  test_command: uv run pytest
  run_lint_on_done: true
  lint_command: uv run ruff check .
  claude_model: ""
  review_command: ""
  review_model: ""
workstreams: []
"""


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )


def make_project(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "project.yaml").write_text(_PROJECT_YAML)
    return repo


def fake_checker(tmp_path: Path, payload: dict) -> tuple[str, ...]:
    script = tmp_path / "fake_checker.py"
    script.write_text(f"import sys, json; json.dump({payload!r}, sys.stdout)")
    return ("python3", str(script))


def _candidate(repo: Path, **typed_overrides) -> ConfigCandidate:
    from dispatcher.core.spec_runner_config import TYPED_DEFAULTS

    typed = {**TYPED_DEFAULTS, **typed_overrides}
    mtime = (repo / "project.yaml").stat().st_mtime
    return ConfigCandidate(typed=typed, base_mtime=mtime)


def test_run_rejects_invalid_typed_field_before_touching_disk(tmp_path: Path) -> None:
    repo = make_project(tmp_path, "alpha")
    original = (repo / "project.yaml").read_text()
    runner = SpecRunnerConfigActionRunner(DispatcherConfig(roots=(tmp_path,)))
    candidate = _candidate(repo, max_retries="not-an-int")
    with pytest.raises(SpecRunnerConfigRejectedError):
        runner.run("alpha", candidate)
    assert (repo / "project.yaml").read_text() == original


def test_run_rejects_stale_mtime(tmp_path: Path) -> None:
    repo = make_project(tmp_path, "alpha")
    runner = SpecRunnerConfigActionRunner(DispatcherConfig(roots=(tmp_path,)))
    candidate = _candidate(repo)
    (repo / "project.yaml").write_text(_PROJECT_YAML + "\n# touched\n")
    with pytest.raises(SpecRunnerConfigConflictError):
        runner.run("alpha", candidate)


def test_run_writes_diff_and_delegates_to_github_checker(tmp_path: Path) -> None:
    repo = make_project(tmp_path, "alpha")
    payload = {"ok": True, "detail": "opened", "pr_url": "https://example/pr/1"}
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    candidate = _candidate(repo, max_retries=7, claude_model="claude-opus-4-8")
    outcome = runner.run("alpha", candidate)
    assert outcome.ok
    assert outcome.pr_url == "https://example/pr/1"
    written = (repo / "project.yaml").read_text()
    assert "max_retries: 7" in written
    assert "claude-opus-4-8" in written
    assert "workstreams" in written  # rest of the file survives


def test_one_in_flight_per_repo(tmp_path: Path, monkeypatch) -> None:
    repo = make_project(tmp_path, "alpha")
    runner = SpecRunnerConfigActionRunner(DispatcherConfig(roots=(tmp_path,)))
    started = threading.Event()
    release = threading.Event()

    def slow_invoke(repo_dir):
        started.set()
        release.wait(timeout=10)
        from dispatcher.core.actions import ActionOutcome

        return ActionOutcome(action="update-spec-runner-config", dir=repo_dir, ok=True)

    monkeypatch.setattr(runner, "_invoke", slow_invoke)
    candidate = _candidate(repo)
    thread = threading.Thread(target=runner.run, args=("alpha", candidate))
    thread.start()
    assert started.wait(timeout=2)
    with pytest.raises(SpecRunnerConfigBusyError):
        runner.run("alpha", candidate)
    release.set()
    thread.join(timeout=2)


def test_write_failure_audits_and_frees_busy_slot(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    """An unexpected build/write exception must still audit and not leak busy."""
    import dispatcher.core.spec_runner_config_actions as mod

    repo = make_project(tmp_path, "alpha")
    payload = {"ok": True, "detail": "opened"}
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    candidate = _candidate(repo)

    def boom(project_yaml, cand):
        raise RuntimeError("yaml render exploded")

    monkeypatch.setattr(mod, "build_new_yaml_text", boom)
    with caplog.at_level("INFO", logger="dispatcher.actions.spec_runner_config"):
        with pytest.raises(RuntimeError, match="yaml render exploded"):
            runner.run("alpha", candidate)
    assert any(
        "ok=False" in r.getMessage() and "yaml render exploded" in r.getMessage()
        for r in caplog.records
    )
    # busy slot must be freed: a follow-up run succeeds
    monkeypatch.undo()
    assert runner.run("alpha", _candidate(repo)).ok


def test_audit_line_written(tmp_path: Path, caplog) -> None:
    repo = make_project(tmp_path, "alpha")
    payload = {"ok": True, "detail": "opened"}
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    candidate = _candidate(repo)
    with caplog.at_level("INFO", logger="dispatcher.actions.spec_runner_config"):
        runner.run("alpha", candidate)
    assert any(
        "action=update-spec-runner-config" in r.getMessage()
        and "repo=alpha" in r.getMessage()
        for r in caplog.records
    )
