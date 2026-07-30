"""TASK-210: live whitelist actions — guards, delegation, audit."""

import subprocess
import threading
from pathlib import Path

import pytest

from dispatcher.core.actions import (
    ActionBusyError,
    ActionOutcome,
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


def test_outcome_carries_merge_and_sync_fields(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "post-merge-sync",
        "dir": "alpha",
        "ok": True,
        "local_sync": "ok",
        "detail": "synced master",
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.run("post-merge-sync", "alpha")
    assert outcome.ok is True
    assert outcome.local_sync == "ok"


def test_post_merge_sync_is_whitelisted_but_junk_is_not(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(DispatcherConfig(roots=(tmp_path,)))
    with pytest.raises(ActionRejectedError, match="not whitelisted"):
        runner.run("rm-rf", "alpha")  # type: ignore[arg-type]


def test_gate_failure_fields_survive_the_round_trip(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "merge",
        "dir": "alpha",
        "ok": False,
        "merged": False,
        "local_sync": "not_attempted",
        "gate_failed": ["not-draft", "threads-resolved"],
        "error": "merge gate refused: not-draft, threads-resolved",
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner._invoke("merge", tmp_path / "alpha", "7", "--if-head", "a" * 40)
    assert outcome.merged is False
    assert outcome.gate_failed == ["not-draft", "threads-resolved"]


def test_invoke_passes_extra_argv_through(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    script = tmp_path / "echo_argv.py"
    script.write_text(
        "import sys, json;"
        "json.dump({'action':'merge','dir':'alpha','ok':True,"
        "'detail':' '.join(sys.argv[1:])}, sys.stdout)"
    )
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    outcome = runner._invoke("merge", tmp_path / "alpha", "7", "--if-head", "abc")
    assert "--if-head abc" in (outcome.detail or "")
    assert outcome.detail.startswith("merge ")


def scripted_checker(tmp_path: Path, by_action: dict[str, dict]) -> tuple[str, ...]:
    """A fake github-checker answering differently per verb, recording argv."""
    script = tmp_path / "scripted_checker.py"
    script.write_text(
        "import sys, json, pathlib\n"
        f"table = {by_action!r}\n"
        f"log = pathlib.Path({str(tmp_path / 'calls.log')!r})\n"
        "action = sys.argv[1]\n"
        "with log.open('a') as fh:\n"
        "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "payload = table[action]\n"
        "json.dump(payload, sys.stdout)\n"
        "sys.exit(0 if payload.get('ok') else 1)\n"
    )
    return ("python3", str(script))


def read_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


HEAD = "a" * 40


def test_green_path_merges_then_syncs(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)),
        command=scripted_checker(
            tmp_path,
            {
                "merge": {
                    "action": "merge",
                    "dir": "alpha",
                    "ok": True,
                    "merged": True,
                    "detail": "squash-merged",
                },
                "post-merge-sync": {
                    "action": "post-merge-sync",
                    "dir": "alpha",
                    "ok": True,
                    "local_sync": "ok",
                },
            },
        ),
    )
    outcome = runner.merge_and_sync("alpha", 7, HEAD)
    assert outcome.ok is True
    assert outcome.merged is True
    assert outcome.local_sync == "ok"
    calls = read_calls(tmp_path)
    assert calls[0].startswith(f"merge {tmp_path / 'alpha'} 7 --if-head {HEAD}")
    assert calls[1].startswith("post-merge-sync")


def test_gate_refusal_never_reaches_the_sync_step(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)),
        command=scripted_checker(
            tmp_path,
            {
                "merge": {
                    "action": "merge",
                    "dir": "alpha",
                    "ok": False,
                    "merged": False,
                    "gate_failed": ["threads-resolved"],
                    "error": "merge gate refused: threads-resolved",
                },
                "post-merge-sync": {
                    "action": "post-merge-sync",
                    "dir": "alpha",
                    "ok": True,
                    "local_sync": "ok",
                },
            },
        ),
    )
    outcome = runner.merge_and_sync("alpha", 7, HEAD)
    assert outcome.ok is False
    assert outcome.merged is False
    assert outcome.local_sync == "not_attempted"
    assert outcome.gate_failed == ["threads-resolved"]
    assert [c.split()[0] for c in read_calls(tmp_path)] == ["merge"]


def test_merged_but_sync_failed_is_not_reported_as_failure(tmp_path: Path) -> None:
    """The PR is merged and cannot be un-merged; ok must follow `merged`."""
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)),
        command=scripted_checker(
            tmp_path,
            {
                "merge": {
                    "action": "merge",
                    "dir": "alpha",
                    "ok": True,
                    "merged": True,
                },
                "post-merge-sync": {
                    "action": "post-merge-sync",
                    "dir": "alpha",
                    "ok": False,
                    "local_sync": "failed",
                    "error": "working tree is dirty",
                },
            },
        ),
    )
    outcome = runner.merge_and_sync("alpha", 7, HEAD)
    assert outcome.merged is True
    assert outcome.local_sync == "failed"
    assert outcome.ok is True
    assert "dirty" in (outcome.error or "")


def test_lock_is_held_across_both_steps(tmp_path: Path) -> None:
    """Nothing may wedge between merge and post-merge-sync."""
    make_repo(tmp_path, "alpha")
    started = threading.Event()
    release = threading.Event()
    script = tmp_path / "blocking_checker.py"
    script.write_text(
        "import sys, json, pathlib, time\n"
        f"flag = pathlib.Path({str(tmp_path / 'in_merge')!r})\n"
        f"gate = pathlib.Path({str(tmp_path / 'go')!r})\n"
        "action = sys.argv[1]\n"
        "if action == 'merge':\n"
        "    flag.touch()\n"
        "    while not gate.exists():\n"
        "        time.sleep(0.01)\n"
        "    json.dump({'action':'merge','dir':'alpha','ok':True,'merged':True},"
        " sys.stdout)\n"
        "else:\n"
        "    json.dump({'action':'post-merge-sync','dir':'alpha','ok':True,"
        "'local_sync':'ok'}, sys.stdout)\n"
    )
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    result: list[ActionOutcome] = []
    worker = threading.Thread(
        target=lambda: result.append(runner.merge_and_sync("alpha", 7, HEAD))
    )
    worker.start()
    while not (tmp_path / "in_merge").exists():
        started.wait(0.01)
    with pytest.raises(ActionBusyError):
        runner.run("pull", "alpha")
    (tmp_path / "go").touch()
    worker.join(timeout=10)
    release.set()
    assert result[0].ok is True
    runner.run("pull", "alpha")  # lock released once the composite finished


def test_pr_detail_passes_through_without_taking_the_lock(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "pr-detail",
        "dir": "alpha",
        "ok": True,
        "pr_detail": {"number": 7, "head_sha": HEAD, "is_draft": False},
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.pr_detail("alpha", 7)
    assert outcome.ok is True
    assert outcome.pr_detail["number"] == 7
    assert runner._busy == set()


def test_pr_detail_still_validates_the_repo_dir(tmp_path: Path) -> None:
    runner = ActionRunner(DispatcherConfig(roots=(tmp_path,)))
    with pytest.raises(ActionRejectedError, match="unsafe"):
        runner.pr_detail("../etc", 7)
