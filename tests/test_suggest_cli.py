"""DESIGN-902: CLI adapter — envelope parsing, partial acceptance, cancel."""

import json
import threading
import time
from pathlib import Path

import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.suggest_cli import (
    SuggestInvalidError,
    SuggestRunner,
    SuggestRunnerBusyError,
    SuggestTimeoutError,
    SuggestUnavailableError,
)

_BUNDLE = {"instruction": "x", "requested_fields": ["claude_model"]}


def _fake_cli(tmp_path: Path, envelope: dict, sleep_s: float = 0.0) -> tuple[str, ...]:
    """A stand-in claude binary: reads stdin, prints the given envelope."""
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import json, sys, time\n"
        "_ = sys.stdin.read()\n"
        f"time.sleep({sleep_s})\n"
        f"print(json.dumps({envelope!r}))\n"
    )
    return ("python3", str(script))


def _envelope(result_payload: dict, **extra: object) -> dict:
    return {"type": "result", "result": json.dumps(result_payload), **extra}


def _config(tmp_path: Path) -> DispatcherConfig:
    return DispatcherConfig(roots=(tmp_path,))


def test_happy_path_with_cost_and_partial_drop(tmp_path: Path) -> None:
    envelope = _envelope(
        {
            "suggestions": {
                "claude_model": {"value": "sonnet", "rationale": "peers use it"},
                "max_retries": {"value": 9, "rationale": "not requested"},
                "auto_commit": {"value": "yes", "rationale": "wrong type"},
                "review_model": {"value": "", "rationale": "equals default"},
            }
        },
        total_cost_usd=0.04,
    )
    runner = SuggestRunner(_config(tmp_path), command=_fake_cli(tmp_path, envelope))
    outcome = runner.run(
        "steward", _BUNDLE, requested={"claude_model", "auto_commit", "review_model"}
    )
    assert outcome.suggestions["claude_model"].value == "sonnet"
    assert sorted(outcome.dropped) == ["auto_commit", "max_retries", "review_model"]
    assert outcome.cost_usd == 0.04
    assert outcome.duration_s >= 0


def test_non_dict_suggestion_entry_is_dropped(tmp_path: Path) -> None:
    envelope = _envelope({"suggestions": {"claude_model": "sonnet"}})
    runner = SuggestRunner(_config(tmp_path), command=_fake_cli(tmp_path, envelope))
    outcome = runner.run("steward", _BUNDLE, requested={"claude_model"})
    assert outcome.suggestions == {} and outcome.dropped == ["claude_model"]


def test_missing_cost_is_none(tmp_path: Path) -> None:
    envelope = _envelope({"suggestions": {}})
    runner = SuggestRunner(_config(tmp_path), command=_fake_cli(tmp_path, envelope))
    outcome = runner.run("steward", _BUNDLE, requested=set())
    assert outcome.cost_usd is None and outcome.suggestions == {}


def test_result_not_json_is_invalid(tmp_path: Path) -> None:
    envelope = {"type": "result", "result": "I think you should…"}
    runner = SuggestRunner(_config(tmp_path), command=_fake_cli(tmp_path, envelope))
    with pytest.raises(SuggestInvalidError):
        runner.run("steward", _BUNDLE, requested=set())


def test_stdout_not_json_is_invalid(tmp_path: Path) -> None:
    script = tmp_path / "fake_claude.py"
    script.write_text("import sys\n_ = sys.stdin.read()\nprint('plain text')\n")
    runner = SuggestRunner(_config(tmp_path), command=("python3", str(script)))
    with pytest.raises(SuggestInvalidError):
        runner.run("steward", _BUNDLE, requested=set())


def test_binary_missing_is_unavailable(tmp_path: Path) -> None:
    runner = SuggestRunner(_config(tmp_path), command=(str(tmp_path / "nope"),))
    with pytest.raises(SuggestUnavailableError):
        runner.run("steward", _BUNDLE, requested=set())


def test_unconfigured_and_bad_basename(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(SuggestUnavailableError):
        SuggestRunner(_config(tmp_path)).run("steward", _BUNDLE, requested=set())
    bad = DispatcherConfig(
        roots=(tmp_path,), suggest_claude_cli=tmp_path / "not-claude"
    )
    with pytest.raises(SuggestUnavailableError):
        SuggestRunner(bad).run("steward", _BUNDLE, requested=set())


def test_timeout_terminates_and_frees_lock(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("dispatcher.core.suggest_cli.SUGGEST_TIMEOUT_S", 0.2)
    slow = _fake_cli(tmp_path, _envelope({"suggestions": {}}), sleep_s=30)
    runner = SuggestRunner(_config(tmp_path), command=slow)
    with pytest.raises(SuggestTimeoutError):
        runner.run("steward", _BUNDLE, requested=set())
    assert runner.current_project is None  # SAME runner's lock freed


def test_timeout_kills_sigterm_ignoring_child(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("dispatcher.core.suggest_cli.SUGGEST_TIMEOUT_S", 0.2)
    monkeypatch.setattr("dispatcher.core.suggest_cli._KILL_WAIT_S", 0.5)
    script = tmp_path / "ignores_sigterm.py"
    script.write_text(
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "_ = sys.stdin.read()\n"
        "time.sleep(30)\n"
    )
    runner = SuggestRunner(_config(tmp_path), command=("python3", str(script)))
    with pytest.raises(SuggestTimeoutError):
        runner.run("steward", _BUNDLE, requested=set())
    assert runner.current_project is None  # lock freed, child reaped


def test_cancel_frees_lock_immediately(tmp_path: Path) -> None:
    slow = _fake_cli(tmp_path, _envelope({"suggestions": {}}), sleep_s=30)
    runner = SuggestRunner(_config(tmp_path), command=slow)
    errors: list[Exception] = []

    def _run() -> None:
        try:
            runner.run("steward", _BUNDLE, requested=set())
        except Exception as err:  # noqa: BLE001 — collected for assertion
            errors.append(err)

    thread = threading.Thread(target=_run)
    thread.start()
    for _ in range(100):  # wait until in-flight
        if runner.current_project == "steward":
            break
        time.sleep(0.05)
    assert runner.cancel("steward") is True
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors and type(errors[0]).__name__ == "SuggestCancelledError"
    assert runner.current_project is None  # lock freed immediately


def test_cancel_other_project_raises_busy_with_name(tmp_path: Path) -> None:
    slow = _fake_cli(tmp_path, _envelope({"suggestions": {}}), sleep_s=30)
    runner = SuggestRunner(_config(tmp_path), command=slow)
    thread = threading.Thread(
        target=lambda: pytest.raises(Exception, runner.run, "steward", _BUNDLE, set())
    )
    thread.start()
    for _ in range(100):
        if runner.current_project == "steward":
            break
        time.sleep(0.05)
    with pytest.raises(SuggestRunnerBusyError) as exc:
        runner.cancel("other")
    assert exc.value.project == "steward"
    runner.cancel("steward")
    thread.join(timeout=10)


def test_cancel_idle_returns_false(tmp_path: Path) -> None:
    runner = SuggestRunner(_config(tmp_path), command=("python3", "-c", "pass"))
    assert runner.cancel("steward") is False


def test_non_string_cli_version_is_coerced(tmp_path: Path) -> None:
    envelope = _envelope({"suggestions": {}}, version=2)
    runner = SuggestRunner(_config(tmp_path), command=_fake_cli(tmp_path, envelope))
    assert runner.run("steward", _BUNDLE, requested=set()).cli_version == "2"


def test_busy_second_run_raises(tmp_path: Path) -> None:
    slow = _fake_cli(tmp_path, _envelope({"suggestions": {}}), sleep_s=30)
    runner = SuggestRunner(_config(tmp_path), command=slow)
    thread = threading.Thread(
        target=lambda: pytest.raises(Exception, runner.run, "steward", _BUNDLE, set())
    )
    thread.start()
    for _ in range(100):
        if runner.current_project == "steward":
            break
        time.sleep(0.05)
    with pytest.raises(SuggestRunnerBusyError):
        runner.run("other", _BUNDLE, requested=set())
    runner.cancel("steward")
    thread.join(timeout=10)
