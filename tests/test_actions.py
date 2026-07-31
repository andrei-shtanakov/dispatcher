"""TASK-210: live whitelist actions — guards, delegation, audit."""

import logging
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from dispatcher.core import actions as actions_module
from dispatcher.core.actions import (
    ActionBusyError,
    ActionOutcome,
    ActionRejectedError,
    ActionRunner,
)
from dispatcher.core.discovery import DispatcherConfig


def _wait_for(flag: Path, message: str, timeout: float = 10.0) -> None:
    """Poll for a flag file; fail with a legible message instead of hanging
    forever when the thing we're waiting on never shows up."""
    deadline = time.monotonic() + timeout
    while not flag.exists():
        if time.monotonic() > deadline:
            pytest.fail(message)
        time.sleep(0.01)


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
    assert outcome.detail is not None
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


def test_no_local_clone_reports_not_applicable(tmp_path: Path) -> None:
    """Outcome-table row 4: no local clone to sync is still a green merge."""
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
                    "ok": True,
                    "local_sync": "not_applicable",
                },
            },
        ),
    )
    outcome = runner.merge_and_sync("alpha", 7, HEAD)
    assert outcome.merged is True
    assert outcome.local_sync == "not_applicable"
    assert outcome.ok is True


def test_transport_failure_leaves_merged_unknown(tmp_path: Path) -> None:
    """A missing binary means we never learned whether the merge landed.

    `merged` must stay `None` (unknown), never `False` — `False` claims a
    confirmed non-merge, which is exactly the fact we don't have.
    """
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("no-such-binary-xyz",)
    )
    outcome = runner.merge_and_sync("alpha", 7, HEAD)
    assert outcome.ok is False
    assert outcome.merged is None
    assert outcome.local_sync == "not_attempted"


def test_a_merge_answer_without_merged_is_not_claimed_as_merged(
    tmp_path: Path,
) -> None:
    """The mirror of the transport-failure rule, on the success path.

    github-checker stamps `merged` on every answer today; if it ever stops,
    `ok: true` alone must not be reported as a *confirmed* merge — an unknown
    is an unknown in both directions.
    """
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)),
        command=scripted_checker(
            tmp_path,
            {
                "merge": {"action": "merge", "dir": "alpha", "ok": True},
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
    assert outcome.merged is None
    assert outcome.ok is True  # the merge step's own verdict, unchanged
    assert outcome.local_sync == "ok"


def test_malformed_envelope_is_a_failed_outcome_with_an_audit_line(
    tmp_path: Path, caplog
) -> None:
    """A wrong-typed envelope field used to raise out of `_invoke`, so a
    subprocess that genuinely ran left no audit line at all."""
    make_repo(tmp_path, "alpha")
    payload = {"action": "pull", "dir": "alpha", "ok": True, "merged": {"nope": 1}}
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    with caplog.at_level("INFO", logger="dispatcher.actions"):
        outcome = runner.run("pull", "alpha")
    assert outcome.ok is False
    assert outcome.error is not None
    assert "unparseable envelope" in outcome.error
    assert "merged" in outcome.error  # the offending field, not just "invalid"
    assert "\n" not in outcome.error  # one audit line per attempt
    assert any(
        "action=pull" in r.getMessage() and "unparseable envelope" in r.getMessage()
        for r in caplog.records
    )


def test_lock_is_held_across_both_steps(tmp_path: Path, monkeypatch) -> None:
    """The composite must take the repo lock exactly once, across both steps.

    Proven two ways:
    * structurally — `_hold` is wrapped to count entries; a two-`_hold`
      implementation (release between merge and sync) shows up as 2, not 1.
    * behaviourally — the fake blocks inside BOTH the merge and the
      post-merge-sync subprocess calls, and a concurrent `pull` must still
      get `ActionBusyError` while blocked in either one.
    """
    make_repo(tmp_path, "alpha")
    hold_entries: list[str] = []
    original_hold = ActionRunner._hold

    @contextmanager
    def counting_hold(self, action, repo_dir):
        # only append on a SUCCESSFUL acquisition — a busy-rejected `pull`
        # must not count towards the composite's own hold tally
        with original_hold(self, action, repo_dir) as target:
            hold_entries.append(action)
            yield target

    monkeypatch.setattr(ActionRunner, "_hold", counting_hold)

    in_merge = tmp_path / "in_merge"
    in_sync = tmp_path / "in_sync"
    go_merge = tmp_path / "go_merge"
    go_sync = tmp_path / "go_sync"
    script = tmp_path / "blocking_checker.py"
    script.write_text(
        "import sys, json, pathlib, time\n"
        f"in_merge = pathlib.Path({str(in_merge)!r})\n"
        f"in_sync = pathlib.Path({str(in_sync)!r})\n"
        f"go_merge = pathlib.Path({str(go_merge)!r})\n"
        f"go_sync = pathlib.Path({str(go_sync)!r})\n"
        "action = sys.argv[1]\n"
        "if action == 'merge':\n"
        "    in_merge.touch()\n"
        "    while not go_merge.exists():\n"
        "        time.sleep(0.01)\n"
        "    json.dump({'action':'merge','dir':'alpha','ok':True,'merged':True},"
        " sys.stdout)\n"
        "else:\n"
        "    in_sync.touch()\n"
        "    while not go_sync.exists():\n"
        "        time.sleep(0.01)\n"
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

    _wait_for(in_merge, "worker never entered the merge step")
    with pytest.raises(ActionBusyError):
        runner.run("pull", "alpha")
    go_merge.touch()

    _wait_for(in_sync, "worker never entered the post-merge-sync step")
    with pytest.raises(ActionBusyError):
        runner.run("pull", "alpha")
    go_sync.touch()

    worker.join(timeout=10)
    assert not worker.is_alive(), "merge_and_sync worker did not finish in time"
    assert result[0].ok is True
    assert hold_entries == ["merge-and-sync"]  # exactly one hold, not two
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
    assert outcome.pr_detail is not None
    assert outcome.pr_detail["number"] == 7
    assert runner._busy == set()


def test_pr_detail_succeeds_while_a_composite_is_in_flight(tmp_path: Path) -> None:
    """`pr_detail` must not queue behind an in-flight merge_and_sync.

    Proven by actually calling it while one is genuinely blocked mid-merge —
    checking `_busy` only after the composite finishes would pass even if
    `pr_detail` silently waited its turn first.
    """
    make_repo(tmp_path, "alpha")
    in_merge = tmp_path / "in_merge"
    go = tmp_path / "go"
    script = tmp_path / "blocking_merge.py"
    script.write_text(
        "import sys, json, pathlib, time\n"
        f"flag = pathlib.Path({str(in_merge)!r})\n"
        f"gate = pathlib.Path({str(go)!r})\n"
        "action = sys.argv[1]\n"
        "if action == 'merge':\n"
        "    flag.touch()\n"
        "    while not gate.exists():\n"
        "        time.sleep(0.01)\n"
        "    json.dump({'action':'merge','dir':'alpha','ok':True,'merged':True},"
        " sys.stdout)\n"
        "elif action == 'pr-detail':\n"
        "    json.dump({'action':'pr-detail','dir':'alpha','ok':True,"
        "'pr_detail':{'number':7}}, sys.stdout)\n"
        "else:\n"
        "    json.dump({'action':'post-merge-sync','dir':'alpha','ok':True,"
        "'local_sync':'ok'}, sys.stdout)\n"
    )
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    worker = threading.Thread(target=runner.merge_and_sync, args=("alpha", 7, HEAD))
    worker.start()
    _wait_for(in_merge, "worker never entered the merge step")

    assert runner._busy == {"alpha"}  # the repo really is held right now
    outcome = runner.pr_detail("alpha", 7)  # must succeed anyway, no queueing
    assert outcome.ok is True

    go.touch()
    worker.join(timeout=10)
    assert not worker.is_alive(), "merge_and_sync worker did not finish in time"


def test_pr_detail_still_validates_the_repo_dir(tmp_path: Path) -> None:
    runner = ActionRunner(DispatcherConfig(roots=(tmp_path,)))
    with pytest.raises(ActionRejectedError, match="unsafe"):
        runner.pr_detail("../etc", 7)


def test_outcome_carries_the_issue_fields(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-lookup",
        "dir": "alpha",
        "ok": True,
        "matches": [{"number": 7, "state": "open"}],
        "malformed": [],
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.issue_lookup("alpha", "wanted")
    assert outcome.ok is True
    assert outcome.matches is not None  # narrow for pyrefly, mirrors pr_detail tests
    assert outcome.matches[0]["number"] == 7
    assert outcome.malformed == []


def test_issue_lookup_succeeds_while_a_composite_is_in_flight(tmp_path: Path) -> None:
    """`issue_lookup` must not queue behind an in-flight merge_and_sync.

    Proven by actually calling it while one is genuinely blocked mid-merge —
    asserting `_busy == set()` only after the call returns would pass even if
    `issue_lookup` took the lock and silently waited its turn first, since
    `_hold`'s `finally` always clears `_busy` before returning.
    """
    make_repo(tmp_path, "alpha")
    in_merge = tmp_path / "in_merge"
    go = tmp_path / "go"
    script = tmp_path / "blocking_merge_for_lookup.py"
    script.write_text(
        "import sys, json, pathlib, time\n"
        f"flag = pathlib.Path({str(in_merge)!r})\n"
        f"gate = pathlib.Path({str(go)!r})\n"
        "action = sys.argv[1]\n"
        "if action == 'merge':\n"
        "    flag.touch()\n"
        "    while not gate.exists():\n"
        "        time.sleep(0.01)\n"
        "    json.dump({'action':'merge','dir':'alpha','ok':True,'merged':True},"
        " sys.stdout)\n"
        "elif action == 'issue-lookup':\n"
        "    json.dump({'action':'issue-lookup','dir':'alpha','ok':True,"
        "'matches':[],'malformed':[]}, sys.stdout)\n"
        "else:\n"
        "    json.dump({'action':'post-merge-sync','dir':'alpha','ok':True,"
        "'local_sync':'ok'}, sys.stdout)\n"
    )
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    worker = threading.Thread(target=runner.merge_and_sync, args=("alpha", 7, HEAD))
    worker.start()
    _wait_for(in_merge, "worker never entered the merge step")

    assert runner._busy == {"alpha"}  # the repo really is held right now
    outcome = runner.issue_lookup("alpha", "wanted")  # must succeed, no queueing
    assert outcome.ok is True

    go.touch()
    worker.join(timeout=10)
    assert not worker.is_alive(), "merge_and_sync worker did not finish in time"


@pytest.mark.parametrize("payload", ["[1, 2]", "5", '"a string"', "null"])
def test_non_object_json_is_a_failed_outcome_not_an_exception(
    tmp_path: Path, payload: str
) -> None:
    """Pre-existing hole: `data.get` sat outside the try, so a non-object
    top-level payload raised AttributeError out of the runner."""
    make_repo(tmp_path, "alpha")
    script = tmp_path / "bad_json.py"
    script.write_text(f"import sys; sys.stdout.write({payload!r})")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    outcome = runner.run("pull", "alpha")
    assert outcome.ok is False
    assert "not an object" in (outcome.error or "")


def test_issue_lookup_still_validates_the_repo_dir(tmp_path: Path) -> None:
    runner = ActionRunner(DispatcherConfig(roots=(tmp_path,)))
    with pytest.raises(ActionRejectedError, match="unsafe"):
        runner.issue_lookup("../etc", "wanted")


def test_issue_lookup_passes_the_slug_through(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    script = tmp_path / "echo_argv.py"
    script.write_text(
        "import sys, json;"
        "json.dump({'action':'issue-lookup','dir':'alpha','ok':True,"
        "'detail':' '.join(sys.argv[1:])}, sys.stdout)"
    )
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    outcome = runner.issue_lookup("alpha", "wanted")
    assert "--slug wanted" in (outcome.detail or "")


@pytest.mark.parametrize("bad_local", [5, "x", [1, 2]])
def test_non_dict_local_degrades_instead_of_crashing(
    tmp_path: Path, bad_local: object
) -> None:
    """Pre-existing hole one line below the envelope guard: `data.get("local")
    or {}` only rescues a *falsy* `local` (missing, `None`, `{}`); a truthy
    non-dict scalar, string, or list still reached `local.get(...)` and
    raised AttributeError out of the runner."""
    make_repo(tmp_path, "alpha")
    payload = {"action": "pull", "dir": "alpha", "ok": True, "local": bad_local}
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.run("pull", "alpha")
    assert outcome.local_behind is None
    assert outcome.local_dirty is None


def test_missing_matches_and_malformed_stay_none_not_empty(tmp_path: Path) -> None:
    """`matches`/`malformed` unset (`null`) means github-checker hit its
    internal cap or could not read a candidate — it could NOT confirm the
    inbox is empty. `[]` means it looked and found nothing. Collapsing the
    two (e.g. `data.get("matches") or []`) would silently turn "unknown"
    into "confirmed empty" — the exact failure the producer's truncation
    guard exists to prevent."""
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-lookup",
        "dir": "alpha",
        "ok": False,
        "matches": None,
        "malformed": None,
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.issue_lookup("alpha", "wanted")
    assert outcome.matches is None
    assert outcome.malformed is None


def test_request_task_creates_and_confirms(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-create",
        "dir": "alpha",
        "ok": True,
        "created": True,
        "issue": {"number": 9, "url": "https://x/9"},
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.request_task(
        "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert outcome.ok is True
    assert outcome.created is True
    assert outcome.issue is not None
    assert outcome.issue["number"] == 9


def test_request_task_reports_a_taken_slug_as_success(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-create",
        "dir": "alpha",
        "ok": True,
        "created": False,
        "issue": {"number": 5, "url": "https://x/5"},
        "detail": "an inbox issue for this slug already exists",
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.request_task(
        "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert outcome.ok is True
    assert outcome.created is False
    assert outcome.issue is not None
    assert outcome.issue["number"] == 5


def test_request_task_preserves_created_none_on_a_broken_create(
    tmp_path: Path,
) -> None:
    """The call broke: whether it landed is unknown, not known-negative."""
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-create",
        "dir": "alpha",
        "ok": False,
        "created": None,
        "error": "gh issue create failed",
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.request_task(
        "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert outcome.ok is False
    assert outcome.created is None


def test_request_task_keeps_created_true_when_read_back_failed(
    tmp_path: Path,
) -> None:
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-create",
        "dir": "alpha",
        "ok": True,
        "created": True,
        "issue": None,
        "detail": "created, but reading it back failed",
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.request_task(
        "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert outcome.ok is True
    assert outcome.created is True
    assert outcome.issue is None


def test_request_task_passes_a_duplicate_conflict_through(tmp_path: Path) -> None:
    """Several issues claim the slug: a human decides, dispatcher does not."""
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-create",
        "dir": "alpha",
        "ok": False,
        "created": False,
        "error": "several inbox issues claim this slug",
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.request_task(
        "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert outcome.ok is False
    assert outcome.created is False  # definitively not created: no mutation ran
    assert outcome.issue is None


def test_request_task_reports_an_unavailable_lookup_as_not_created(
    tmp_path: Path,
) -> None:
    """The pre-create check failed, so nothing was attempted — created is False,
    not None: `None` would claim we might have mutated something."""
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-create",
        "dir": "alpha",
        "ok": False,
        "created": False,
        "error": "slug lookup failed before create",
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.request_task(
        "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert outcome.ok is False
    assert outcome.created is False
    assert outcome.issue is None


def test_request_task_audits_whether_it_created(tmp_path: Path, caplog) -> None:
    """D1a-4: the audit must distinguish created from already-existed —
    an idempotency rule whose log cannot show idempotency is not auditable."""
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-create",
        "dir": "alpha",
        "ok": True,
        "created": False,
        "issue": {"number": 5},
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        runner.request_task(
            "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
        )
    assert "created=False" in caplog.text


def test_pull_audit_line_is_unchanged_by_the_created_field(
    tmp_path: Path, caplog
) -> None:
    """The new field must not leak into unrelated actions' audit lines."""
    make_repo(tmp_path, "alpha")
    payload = {"action": "pull", "dir": "alpha", "ok": True}
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        runner.run("pull", "alpha")
    assert "created=" not in caplog.text


def test_request_task_passes_prose_through_a_file_not_argv(
    tmp_path: Path,
) -> None:
    """Multi-line prose must survive; argv would mangle it."""
    make_repo(tmp_path, "alpha")
    script = tmp_path / "echo_body.py"
    script.write_text(
        "import sys, json, pathlib\n"
        "i = sys.argv.index('--body-file')\n"
        "body = pathlib.Path(sys.argv[i + 1]).read_text()\n"
        "json.dump({'action':'issue-create','dir':'alpha','ok':True,"
        "'created':True,'detail':body}, sys.stdout)\n"
    )
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    outcome = runner.request_task(
        "alpha",
        slug="wanted",
        sender="dispatcher",
        title="t",
        prose="line one\nline two\n",
    )
    assert outcome.detail == "line one\nline two\n"


def test_request_task_holds_the_repo_lock(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    script = tmp_path / "blocking.py"
    script.write_text(
        "import sys, json, pathlib, time\n"
        f"flag = pathlib.Path({str(tmp_path / 'in_create')!r})\n"
        f"gate = pathlib.Path({str(tmp_path / 'go')!r})\n"
        "flag.touch()\n"
        "while not gate.exists():\n"
        "    time.sleep(0.01)\n"
        "json.dump({'action':'issue-create','dir':'alpha','ok':True,"
        "'created':True}, sys.stdout)\n"
    )
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    result: list[ActionOutcome] = []
    worker = threading.Thread(
        target=lambda: result.append(
            runner.request_task(
                "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
            )
        )
    )
    worker.start()
    deadline = time.monotonic() + 10
    while not (tmp_path / "in_create").exists():
        if time.monotonic() > deadline:
            pytest.fail("worker never entered issue-create")
        time.sleep(0.01)

    # The concurrent call runs in its own thread with a *bounded* join. If
    # `_hold` queued instead of rejecting, `runner.run` here would block
    # until the worker releases the lock — waiting that out inline (as a
    # bare `with pytest.raises(...): runner.run(...)`) would deadlock the
    # whole test, since the `go` touch that unblocks the worker sits below.
    # Bounding the wait turns "the guard queues" into a prompt, readable
    # assertion failure instead of a hung job. `go` is touched afterward
    # unconditionally, so both threads are released no matter what happened.
    second_outcome: list[ActionBusyError] = []

    def attempt_pull() -> None:
        try:
            runner.run("pull", "alpha")
        except ActionBusyError as err:
            second_outcome.append(err)

    second = threading.Thread(target=attempt_pull)
    second.start()
    second.join(timeout=2)
    rejected_immediately = not second.is_alive() and len(second_outcome) == 1

    (tmp_path / "go").touch()
    worker.join(timeout=10)
    second.join(timeout=10)
    assert not worker.is_alive(), "worker wedged"
    assert not second.is_alive(), "concurrent pull thread wedged (queued, not rejected)"
    assert rejected_immediately, (
        "a concurrent action must get an immediate ActionBusyError, not queue "
        "behind the in-flight request_task"
    )
    assert result[0].ok is True
    runner.run("pull", "alpha")  # lock released


def test_request_task_handles_an_unencodable_prose_without_leaking_or_hanging(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    """A lone UTF-16 surrogate survives `json.loads` (JSON permits unpaired
    `\\uXXXX` escapes) but cannot be encoded to UTF-8 when writing the temp
    file — reachable straight from an HTTP request body. This is a real
    write failure, not a mocked `_invoke` failure: nothing was attempted, so
    it must surface as `created=False` (not raise, not `created=None`), the
    temp file must not leak, and the attempt must still be audited."""
    make_repo(tmp_path, "alpha")
    created_paths: list[str] = []
    original_ntf = tempfile.NamedTemporaryFile

    def recording_ntf(*args, **kwargs):
        handle = original_ntf(*args, **kwargs)
        created_paths.append(handle.name)
        return handle

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", recording_ntf)
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", "-c", "pass")
    )
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        outcome = runner.request_task(
            "alpha",
            slug="wanted",
            sender="dispatcher",
            title="t",
            prose="\ud83d",  # lone surrogate: valid JSON, invalid UTF-8
        )
    assert outcome.ok is False
    assert outcome.created is False
    assert len(created_paths) == 1
    assert not Path(created_paths[0]).exists()  # the temp file did not leak
    assert "action=request-task" in caplog.text  # the attempt was audited


def test_request_task_handles_a_temp_file_creation_failure(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    """A failure while creating the temp file itself (e.g. a full disk)
    happens before any name is ever recorded — there is nothing to unlink,
    but the attempt must still leave an audit line, not vanish silently."""
    make_repo(tmp_path, "alpha")

    def boom(*args, **kwargs):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", boom)
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", "-c", "pass")
    )
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        outcome = runner.request_task(
            "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
        )
    assert outcome.ok is False
    assert outcome.created is False
    assert "action=request-task" in caplog.text


# --- F-1: two layers of argv defence ---------------------------------------
#
# Layer 1 (`reject_control_chars`) is the intended, named refusal. Layer 2
# (`_invoke`'s `except ValueError`) is boundary defence that must hold even
# when layer 1 is bypassed or a future argv field is added without being
# listed in it. Both are tested, and layer 2 is tested WITH layer 1 disabled
# — testing it through layer 1 would only prove layer 1 works.

NUL = "\x00"


def test_issue_lookup_rejects_a_control_character_by_name(
    tmp_path: Path, caplog
) -> None:
    """Layer 1 on the read path: a named 422-shaped refusal, plus an audit
    line. Before this, `subprocess.run` raised `ValueError("embedded null
    byte")` while validating argv — the request became a 500 and the attempt
    left NO audit line, breaking this module's per-attempt guarantee and
    ADR-ECO-004a D1a-4 with it."""
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", "-c", "pass")
    )
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        with pytest.raises(ActionRejectedError, match="control character"):
            runner.issue_lookup("alpha", f"want{NUL}ed")
    assert "action=issue-lookup" in caplog.text
    assert "rejected=" in caplog.text
    assert NUL not in caplog.text  # the slug is repr'd, not pasted in raw


@pytest.mark.parametrize("field", ["slug", "title"])
def test_request_task_rejects_a_control_character_before_taking_the_lock(
    tmp_path: Path, caplog, field: str
) -> None:
    """Layer 1 on the write path, for every value that becomes its own argv
    element. It runs before `_hold`, so a request this malformed cannot
    occupy the repo — and the audit line says `created=False`, because
    nothing was attempted and `created=None` would claim we cannot tell."""
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", "-c", "pass")
    )
    kwargs = {"slug": "wanted", "sender": "dispatcher", "title": "t", "prose": "p"}
    kwargs[field] = f"bad{NUL}value"
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        with pytest.raises(ActionRejectedError, match="control character"):
            runner.request_task("alpha", **kwargs)  # type: ignore[arg-type]
    assert "action=request-task" in caplog.text
    assert "created=False" in caplog.text
    # the repo was never held: the next request must not meet a 409
    outcome = runner.request_task(
        "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert outcome.action == "request-task"


def test_invoke_catches_a_nul_even_with_layer_one_disabled(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    """Layer 2 alone. `reject_control_chars` is stubbed to a no-op, standing
    in for the field nobody remembered to list — the boundary must still turn
    `subprocess.run`'s pre-fork `ValueError` into a controlled failure with an
    audit line, not an exception out of the runner."""
    make_repo(tmp_path, "alpha")
    monkeypatch.setattr(actions_module, "reject_control_chars", lambda **kw: None)
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", "-c", "pass")
    )
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        outcome = runner.issue_lookup("alpha", f"want{NUL}ed")
    assert outcome.ok is False
    assert "refused before launch" in (outcome.error or "")
    assert "action=issue-lookup" in caplog.text


def test_request_task_layer_two_classifies_the_refusal_as_not_created(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    """Layer 2 on the write path. It is a pre-mutation refusal, not an
    unknown: `subprocess.run` validates argv before it forks, so no verb ran
    and no issue was filed. `created=None` would claim we cannot tell whether
    one exists, when we know for certain none does."""
    make_repo(tmp_path, "alpha")
    monkeypatch.setattr(actions_module, "reject_control_chars", lambda **kw: None)
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", "-c", "pass")
    )
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        outcome = runner.request_task(
            "alpha", slug=f"wan{NUL}ted", sender="dispatcher", title="t", prose="p"
        )
    assert outcome.ok is False
    assert outcome.created is False
    assert "refused before launch" in (outcome.error or "")
    assert "created=False" in caplog.text
    # and the lock came back: the refusal must not wedge the repo
    again = runner.request_task(
        "alpha", slug="ok", sender="dispatcher", title="t", prose="p"
    )
    assert again.action == "request-task"


def test_issue_lookup_audit_distinguishes_unreadable_from_confirmed_empty(
    tmp_path: Path, caplog
) -> None:
    """`len(matches or [])` logged `matches: null` as `matches=0` — the audit
    trail then read "read the inbox, found nothing" for the one value that
    means "could not read the inbox", reproducing the exact collapse the rest
    of this feature exists to prevent."""
    make_repo(tmp_path, "alpha")
    payload = {"action": "issue-lookup", "dir": "alpha", "ok": False}
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        runner.issue_lookup("alpha", "wanted")
    assert "matches=unknown" in caplog.text
    assert "matches=0" not in caplog.text

    caplog.clear()
    empty = {"action": "issue-lookup", "dir": "alpha", "ok": True, "matches": []}
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, empty)
    )
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        runner.issue_lookup("alpha", "wanted")
    assert "matches=0" in caplog.text


def test_invoke_survives_an_oversized_argument(tmp_path: Path, caplog) -> None:
    """N-3: `_invoke` caught only `FileNotFoundError`/`TimeoutExpired`, so any
    OTHER `OSError` on exec raised straight out — a 500 with zero audit lines,
    the same guarantee break as the NUL byte. `--if-head` is client-supplied
    and wire-reachable, so an oversized one is enough to trigger a real
    `[Errno 7] Argument list too long` here — not a mocked failure.

    The producer side of this contract had already widened its equivalent
    catch to `(OSError, TimeoutExpired)`; this is the matching end.
    """
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", "-c", "pass")
    )
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        outcome = runner.merge_and_sync("alpha", 1, "d" * 2_000_000)
    assert outcome.ok is False
    assert "Argument list too long" in (outcome.error or "")
    # merged stays UNKNOWN: exec never happened, but this path must not claim
    # a non-merge it was never told about
    assert outcome.merged is None
    assert "action=merge-and-sync" in caplog.text
    assert "local_sync=not_attempted" in caplog.text


def test_issue_lookup_success_audit_quotes_the_slug(tmp_path: Path, caplog) -> None:
    """The success path logged the slug with `%s` while its own rejection
    sibling used `%r`. Layer 1 now refuses control characters, so nothing
    hostile should reach here — but that is exactly the assumption that makes
    a raw `%s` survive a later widening of the validator unnoticed. Pinned as
    the quoted form, so the two lines cannot drift apart again."""
    make_repo(tmp_path, "alpha")
    payload = {"action": "issue-lookup", "dir": "alpha", "ok": True, "matches": []}
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        runner.issue_lookup("alpha", "wanted")
    assert "slug='wanted'" in caplog.text
    assert "slug=wanted " not in caplog.text


def _undecodable_checker(tmp_path: Path) -> tuple[str, ...]:
    """A github-checker stand-in that RUNS to completion and writes bytes that
    are not valid UTF-8 — the post-fork half of the NEW-1 distinction."""
    script = tmp_path / "bad_bytes.py"
    script.write_text(
        "import sys;"
        'sys.stdout.buffer.write(b\'{"ok": true, "created": true, \\xff}\');'
        "sys.exit(0)"
    )
    return ("python3", str(script))


def test_a_post_run_decode_failure_is_not_labelled_a_pre_launch_refusal(
    tmp_path: Path, caplog
) -> None:
    """NEW-1: `UnicodeDecodeError` IS a `ValueError`, and `text=True` raises it
    AFTER the child has run. The single `except ValueError` written for
    `subprocess.run`'s pre-fork argv validation therefore swallowed a
    post-execution failure and called it "refused before launch"."""
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=_undecodable_checker(tmp_path)
    )
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        outcome = runner.issue_lookup("alpha", "wanted")
    assert outcome.ok is False
    assert "could not be decoded" in (outcome.error or "")
    assert "refused before launch" not in (outcome.error or "")
    assert "action=issue-lookup" in caplog.text


def test_request_task_leaves_created_unknown_when_output_cannot_be_decoded(
    tmp_path: Path, caplog
) -> None:
    """The classification ruling, pinned. The verb RAN: `issue-create` may
    well have filed the issue and only its answer failed to decode, so
    `created` is None (unknown), never False. `created=False` would send the
    screen down the ordinary-refusal arm, which RE-ENABLES Create — the
    duplicate this whole feature exists to prevent."""
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=_undecodable_checker(tmp_path)
    )
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        outcome = runner.request_task(
            "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
        )
    assert outcome.ok is False
    assert outcome.created is None
    assert "could not be decoded" in (outcome.error or "")
    assert "refused before launch" not in (outcome.error or "")
    assert "action=request-task" in caplog.text
    # `created=` is omitted entirely when unknown — never printed as False
    assert "created=False" not in caplog.text


def test_request_task_audits_even_when_the_temp_file_cannot_be_removed(
    tmp_path: Path, caplog, monkeypatch
) -> None:
    """The one exit of the eight that could still vanish: an OSError from the
    cleanup `unlink` in `finally` discarded the already-decided outcome and
    took the audit call with it — a 500 with zero audit lines."""
    make_repo(tmp_path, "alpha")

    def boom(self, missing_ok: bool = False) -> None:
        raise OSError("read-only filesystem (simulated)")

    payload = {"action": "issue-create", "dir": "alpha", "ok": True, "created": True}
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    monkeypatch.setattr(Path, "unlink", boom)
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        outcome = runner.request_task(
            "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
        )
    assert outcome.created is True  # the decided outcome survives the cleanup
    assert "action=request-task" in caplog.text
    assert "cleanup_failed=" in caplog.text
