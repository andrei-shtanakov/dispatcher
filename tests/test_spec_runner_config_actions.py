import subprocess
import threading
from pathlib import Path

import pytest
from conftest import assert_no_secret_reachable

from dispatcher.core.actions import (
    PHASE_LAUNCHED_UNREADABLE,
    PHASE_PRE_LAUNCH,
    PHASE_READABLE,
)
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


def fake_checker(
    tmp_path: Path, payload: dict, *, returncode: int = 0
) -> tuple[tuple[str, ...], Path]:
    """Fake github-checker: records argv + the --edit file's content.

    Returns (command, record_path). The record is written AT INVOCATION
    TIME because the runner's temp edit file is deleted before assertions
    can see it. Exits with `returncode` while STILL printing JSON on
    stdout — propose-pr's real no-op behavior (rc=1 + JSON) must never be
    misread as "no JSON".
    """
    if "schema_version" not in payload:
        # Since Task 3 this runner accepts nothing but a legal actions/v1
        # envelope, so the bare `{"ok": ..., "detail": ...}` these tests
        # used to send is refused before they can make their point. The
        # test's own fields go on top of the vendored `propose-pr`
        # envelope: it keeps saying only the thing it is about, inside a
        # real envelope.
        import json as _json

        fixture = (
            Path(__file__).parent.parent
            / "contracts"
            / "github-checker-actions"
            / "v1"
            / "fixtures"
            / "propose-pr-created.json"
        )
        payload = _json.loads(fixture.read_text()) | payload
    record = tmp_path / "record.json"
    script = tmp_path / "fake_checker.py"
    script.write_text(
        "import json, sys\n"
        "argv = sys.argv[1:]\n"
        "edit_content = None\n"
        "edit_bytes_hex = None\n"
        "for a in argv:\n"
        "    if a.startswith('project.yaml='):\n"
        "        p = a.split('=', 1)[1]\n"
        "        try:\n"
        "            raw = open(p, 'rb').read()\n"
        "            edit_bytes_hex = raw.hex()\n"
        "            edit_content = raw.decode('utf-8')\n"
        "        except OSError:\n"
        "            pass\n"
        f"json.dump({{'argv': argv, 'edit_content': edit_content, "
        f"'edit_bytes_hex': edit_bytes_hex}}, "
        f"open({str(record)!r}, 'w'))\n"
        f"json.dump({payload!r}, sys.stdout)\n"
        f"sys.exit({returncode})\n"
    )
    return ("python3", str(script)), record


def test_the_bytes_that_were_rendered_are_the_bytes_propose_pr_receives(
    tmp_path: Path,
) -> None:
    """`Path.write_text` opens with newline=None and translates \\n to
    os.linesep, so a text-mode write can alter bytes after they were built.
    The temp file propose-pr reads must be byte-identical to what the
    renderer produced, on every platform."""
    from dispatcher.core.spec_runner_config_actions import build_new_yaml_bytes

    repo = make_project(tmp_path, "alpha")
    base_bytes = (repo / "project.yaml").read_bytes()
    candidate = _candidate(repo, max_retries=7)
    expected, _, _ = build_new_yaml_bytes(base_bytes, candidate)

    command, record = fake_checker(tmp_path, {"ok": True, "detail": "created"})
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=command
    )
    runner.run("alpha", candidate)

    import json as _json

    written = bytes.fromhex(_json.loads(record.read_text())["edit_bytes_hex"])
    assert written == expected


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


def test_run_delegates_to_propose_pr_live_tree_untouched(tmp_path: Path) -> None:
    import hashlib
    import json as _json

    repo = make_project(tmp_path, "alpha")
    live_before = (repo / "project.yaml").read_bytes()
    payload = {
        "ok": True,
        "detail": "pull request created",
        "pr_url": "https://example/pr/1",
        "branch": "propose/x",
        "base_branch": "main",
        "commit_sha": "abc123",
        "changed_paths": ["project.yaml"],
    }
    command, record = fake_checker(tmp_path, payload)
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=command
    )
    outcome = runner.run("alpha", _candidate(repo, max_retries=7))

    assert outcome.ok
    assert outcome.pr_url == "https://example/pr/1"
    assert outcome.branch == "propose/x"
    assert outcome.base_branch == "main"
    assert outcome.commit_sha == "abc123"
    assert outcome.changed_paths == ["project.yaml"]
    # THE invariant: the live tree was never written
    assert (repo / "project.yaml").read_bytes() == live_before

    rec = _json.loads(record.read_text())
    argv = rec["argv"]
    assert argv[0] == "propose-pr"
    assert argv[1] == str(tmp_path / "alpha")
    assert "--message" in argv
    msg = argv[argv.index("--message") + 1]
    assert msg.startswith("chore(spec-runner): update config")
    assert "max_retries" in msg
    # assert the value POSITIONALLY after its flag — a bare startswith scan
    # could accidentally match the --if-match value instead
    edit_arg = argv[argv.index("--edit") + 1]
    assert edit_arg.startswith("project.yaml=")
    if_match = argv[argv.index("--if-match") + 1]
    expected_hex = hashlib.sha256(live_before).hexdigest()
    assert if_match == f"project.yaml={expected_hex}"
    # the temp edit file carried the DESIGN-402-filtered YAML
    assert "max_retries: 7" in rec["edit_content"]
    assert "workstreams" in rec["edit_content"]


def test_noop_rc1_with_json_is_parsed_not_no_json(tmp_path: Path) -> None:
    repo = make_project(tmp_path, "alpha")
    # A real no-op has nothing to point at: the overlay would otherwise
    # leave `propose-pr-created`'s `pr_url`/`commit_sha`/`branch` on an
    # answer that says no PR was opened, and the test would be asserting
    # about an envelope the producer could never send.
    payload = {
        "ok": False,
        "detail": "no-op",
        "error": "no changes vs main",
        "pr_url": None,
        "pr_state": None,
        "branch": None,
        "base_branch": None,
        "commit_sha": None,
        "changed_paths": None,
    }
    command, _ = fake_checker(tmp_path, payload, returncode=1)
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=command
    )
    outcome = runner.run("alpha", _candidate(repo))
    assert not outcome.ok
    assert outcome.detail == "no-op"
    assert outcome.error == "no changes vs main"
    assert "no JSON" not in (outcome.error or "")


def test_one_in_flight_per_repo(tmp_path: Path, monkeypatch) -> None:
    repo = make_project(tmp_path, "alpha")
    runner = SpecRunnerConfigActionRunner(DispatcherConfig(roots=(tmp_path,)))
    started = threading.Event()
    release = threading.Event()

    def slow_invoke(target, **kwargs):
        started.set()
        release.wait(timeout=10)
        from dispatcher.core.actions import ActionOutcome

        return ActionOutcome(
            action="update-spec-runner-config", dir=target.name, ok=True
        )

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
    payload = {"ok": True, "detail": "pull request created"}
    command, _record = fake_checker(tmp_path, payload)
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=command
    )
    candidate = _candidate(repo)

    def boom(base_text, cand):
        raise RuntimeError("yaml render exploded")

    monkeypatch.setattr(mod, "build_new_yaml_bytes", boom)
    with caplog.at_level("INFO", logger="dispatcher.actions.spec_runner_config"):
        outcome = runner.run("alpha", candidate)
    assert not outcome.ok
    assert "yaml render exploded" in (outcome.error or "")
    assert any(
        "ok=False" in r.getMessage() and "yaml render exploded" in r.getMessage()
        for r in caplog.records
    )
    # Invariant: exactly ONE audit line per attempt
    attempt_lines = [
        r
        for r in caplog.records
        if "action=update-spec-runner-config" in r.getMessage()
        and "yaml render exploded" in r.getMessage()
    ]
    assert len(attempt_lines) == 1
    # busy slot must be freed: a follow-up run succeeds
    monkeypatch.undo()
    assert runner.run("alpha", _candidate(repo)).ok


def test_audit_line_written(tmp_path: Path, caplog) -> None:
    repo = make_project(tmp_path, "alpha")
    payload = {"ok": True, "detail": "pull request created"}
    command, _record = fake_checker(tmp_path, payload)
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=command
    )
    candidate = _candidate(repo)
    with caplog.at_level("INFO", logger="dispatcher.actions.spec_runner_config"):
        runner.run("alpha", candidate)
    assert any(
        "action=update-spec-runner-config" in r.getMessage()
        and "repo=alpha" in r.getMessage()
        for r in caplog.records
    )


_BASE_YAML = """\
project: alpha
spec_runner:
  max_retries: 5
  claude_model: claude-opus-4-8
workstreams: []
"""

_BASE_YAML_WITH_EXTRA = """\
project: alpha
spec_runner:
  max_retries: 5
  extra_executor_config:
    executor:
      telegram_bot_token: "123:abc"
workstreams: []
"""


def _cand(**typed_overrides) -> ConfigCandidate:
    from dispatcher.core.spec_runner_config import TYPED_DEFAULTS

    return ConfigCandidate(typed={**TYPED_DEFAULTS, **typed_overrides}, base_mtime=0.0)


def _render_text(text: str, candidate: ConfigCandidate) -> tuple[str, list[str], bool]:
    """String-in/string-out wrapper: the renderer's contract is bytes, but
    most tests are about YAML shape, not encoding."""
    from dispatcher.core.spec_runner_config_actions import build_new_yaml_bytes

    out, changed, extra = build_new_yaml_bytes(text.encode("utf-8"), candidate)
    return out.decode("utf-8"), changed, extra


_STYLE_CASES = {
    "marker_start": "---\nproject: alpha\nspec_runner:\n  max_retries: 5\n",
    "marker_both": "---\nproject: alpha\nspec_runner:\n  max_retries: 5\n...\n",
    "no_final_newline": "project: alpha\nspec_runner:\n  max_retries: 5",
    "crlf": "project: alpha\r\nspec_runner:\r\n  max_retries: 5\r\n",
    "bom": "﻿project: alpha\nspec_runner:\n  max_retries: 5\n",
    "mapping_indent_3": "project: alpha\nspec_runner:\n   max_retries: 5\n",
    "mapping_indent_4": "project: alpha\nspec_runner:\n    max_retries: 5\n",
    "combined": ("﻿---\r\nproject: alpha\r\nspec_runner:\r\n   max_retries: 5\r\n..."),
}


@pytest.mark.parametrize("name", sorted(_STYLE_CASES))
def test_a_noop_reproduces_each_source_style(name: str) -> None:
    """Rows 1-5 are fixed by making the RENDERING reproduce the source, never
    by teaching a comparator to forgive them."""
    from dispatcher.core.spec_runner_config_actions import build_new_yaml_bytes

    base = _STYLE_CASES[name].encode("utf-8")
    out, changed, _ = build_new_yaml_bytes(
        base, ConfigCandidate(typed={}, base_mtime=0.0)
    )
    assert out == base
    assert changed == []


@pytest.mark.parametrize("name", sorted(_STYLE_CASES))
def test_a_real_edit_keeps_each_source_style(name: str) -> None:
    from dispatcher.core.spec_runner_config_actions import build_new_yaml_bytes

    base = _STYLE_CASES[name].encode("utf-8")
    out, changed, _ = build_new_yaml_bytes(
        base, ConfigCandidate(typed={"max_retries": 9}, base_mtime=0.0)
    )
    assert changed == ["max_retries"]
    text = out.decode("utf-8")
    assert "max_retries: 9" in text
    # everything the style test pinned still holds
    assert text.startswith("﻿") == _STYLE_CASES[name].startswith("﻿")
    assert text.endswith("\n") == _STYLE_CASES[name].endswith("\n")
    assert ("\r\n" in text) == ("\r\n" in _STYLE_CASES[name])


def test_emission_omits_implicit_defaults() -> None:
    text, changed, extra_changed = _render_text(
        _BASE_YAML, _cand(max_retries=5, claude_model="claude-opus-4-8")
    )
    # explicit keys stay; implicit-at-default keys are NOT materialized
    assert "max_retries: 5" in text
    assert "claude_model: claude-opus-4-8" in text
    assert "task_timeout_minutes" not in text
    assert "auto_commit" not in text
    assert changed == []
    assert extra_changed is False


def test_emission_adds_changed_from_default() -> None:
    text, changed, _ = _render_text(
        _BASE_YAML,
        _cand(max_retries=5, claude_model="claude-opus-4-8", review_model="x"),
    )
    assert "review_model: x" in text
    assert changed == ["review_model"]


def test_emission_keeps_explicit_even_when_set_back_to_default() -> None:
    from dispatcher.core.spec_runner_config import TYPED_DEFAULTS

    text, changed, _ = _render_text(
        _BASE_YAML,
        _cand(
            max_retries=TYPED_DEFAULTS["max_retries"],
            claude_model="claude-opus-4-8",
        ),
    )
    # max_retries was explicit (5); setting it to default 3 keeps it explicit
    assert f"max_retries: {TYPED_DEFAULTS['max_retries']}" in text
    assert changed == ["max_retries"]


def test_emission_partial_candidate_preserves_explicit_current() -> None:
    cand = ConfigCandidate(typed={"review_model": "y"}, base_mtime=0.0)
    text, changed, _ = _render_text(_BASE_YAML, cand)
    # keys absent from the candidate keep their current-file values
    assert "max_retries: 5" in text
    assert "claude_model: claude-opus-4-8" in text
    assert "review_model: y" in text
    assert changed == ["review_model"]


def test_emission_preserves_rest_of_file() -> None:
    text, _, _ = _render_text(_BASE_YAML, _cand(max_retries=7))
    assert "project: alpha" in text
    assert "workstreams: []" in text


def test_emission_omitted_extra_preserves_current() -> None:
    cand = ConfigCandidate(typed={"review_model": "y"}, base_mtime=0.0)
    text, changed, extra_changed = _render_text(_BASE_YAML_WITH_EXTRA, cand)
    assert "telegram_bot_token" in text  # overlay preserved, not dropped
    assert extra_changed is False
    assert changed == ["review_model"]


def test_emission_explicit_empty_extra_clears() -> None:
    cand = ConfigCandidate(typed={}, extra_executor_config={}, base_mtime=0.0)
    text, _, extra_changed = _render_text(_BASE_YAML_WITH_EXTRA, cand)
    assert "telegram_bot_token" not in text  # intentional clear
    assert extra_changed is True


def test_emission_nonempty_extra_replaces() -> None:
    cand = ConfigCandidate(
        typed={},
        extra_executor_config={"executor": {"budget_usd": 5.0}},
        base_mtime=0.0,
    )
    text, _, extra_changed = _render_text(_BASE_YAML_WITH_EXTRA, cand)
    assert "budget_usd" in text
    assert "telegram_bot_token" not in text
    assert extra_changed is True


# --- What the renderer must NOT touch -------------------------------------
#
# `build_new_yaml_bytes` edits one key of somebody else's file and hands the
# result to `propose-pr`, so anything it changes beyond that key ships as an
# unrequested diff in a neighbour repo. These tests pin the blast radius.
# The bug they were written for: the block was rebuilt as a fresh plain dict
# and assigned over `doc["spec_runner"]`, which threw away the loaded
# CommentedMap and with it every comment inside the block, the file's key
# order, and every key the editor has no field for.

_BASE_YAML_COMMENTED = """\
project: alpha
spec_runner:
  max_retries: 5  # inline on a key
  # standalone between two keys
  claude_model: claude-opus-4-8
  # indented, after the last key of the block

# col-0 standalone, right after the block
workstreams: []
"""


def test_standalone_comment_after_the_block_survives() -> None:
    text, _, _ = _render_text(_BASE_YAML_COMMENTED, _cand(max_retries=7))
    assert "# col-0 standalone, right after the block" in text


def test_comments_inside_the_block_survive() -> None:
    text, _, _ = _render_text(_BASE_YAML_COMMENTED, _cand(max_retries=7))
    assert "max_retries: 7  # inline on a key" in text
    assert "# standalone between two keys" in text
    assert "# indented, after the last key of the block" in text


_BASE_YAML_UNKNOWN_KEY = """\
project: alpha
spec_runner:
  max_retries: 5
  # the legacy field must be explicitly nulled or preflight rejects the
  # config as ambiguous
  spec_gen_budget_usd: null
workstreams: []
"""


def test_a_key_the_editor_has_no_field_for_is_not_dropped() -> None:
    """Silently deleting it is worse than losing a comment: `spec_gen_budget_usd`
    is a real key of research-bench's project.yaml, and its own comment says
    preflight rejects the config when it is absent. The editor knows nothing
    about it, which is a reason to leave it alone, not to delete it."""
    text, _, _ = _render_text(_BASE_YAML_UNKNOWN_KEY, _cand(max_retries=7))
    assert "spec_gen_budget_usd: null" in text
    assert "# the legacy field must be explicitly nulled or preflight" in text


def test_the_files_own_key_order_is_kept() -> None:
    """Re-sorting into TYPED_FIELDS order is a diff nobody asked for."""
    base = """\
project: alpha
spec_runner:
  review_model: r
  claude_model: c
  max_retries: 5
workstreams: []
"""
    text, _, _ = _render_text(base, ConfigCandidate(typed={}, base_mtime=0.0))
    block = text[text.index("spec_runner:") : text.index("workstreams:")]
    assert block.index("review_model") < block.index("claude_model")
    assert block.index("claude_model") < block.index("max_retries")


def test_an_appended_key_does_not_displace_the_trailing_comment() -> None:
    """ruamel hangs the text that follows the block off the block's LAST key,
    so appending a key after it emits that text mid-block unless it is moved."""
    text, _, _ = _render_text(_BASE_YAML_COMMENTED, _cand(review_model="rm"))
    assert "review_model: rm" in text
    assert text.index("review_model: rm") < text.index("# indented, after the last")
    assert text.index("# col-0 standalone") < text.index("workstreams:")


def test_clearing_extra_keeps_the_comment_that_follows_the_block() -> None:
    """Same text, but hanging off a NESTED last key that gets deleted whole."""
    base = """\
project: alpha
spec_runner:
  max_retries: 5
  extra_executor_config:
    executor:
      telegram_bot_token: "123:abc"

# col-0 standalone, right after the block
workstreams: []
"""
    cand = ConfigCandidate(typed={}, extra_executor_config={}, base_mtime=0.0)
    text, _, extra_changed = _render_text(base, cand)
    assert extra_changed is True
    assert "telegram_bot_token" not in text  # the clear still happens
    assert "# col-0 standalone, right after the block" in text


def test_a_noop_candidate_reproduces_the_file_byte_for_byte() -> None:
    """The acceptance bar, on a fixture trimmed from a real project.yaml.

    A candidate that changes nothing must render the input back unchanged —
    otherwise the editor opens a PR full of churn it did not intend, and the
    "no-op" the write path reports is not one. Byte equality is the only
    assertion that covers the losses nobody thought to name."""
    base = (
        Path(__file__).parent / "fixtures" / "project_yaml_with_comments.yaml"
    ).read_text()
    text, changed, extra_changed = _render_text(
        base, ConfigCandidate(typed={}, base_mtime=0.0)
    )
    assert changed == []
    assert extra_changed is False
    assert text == base


@pytest.mark.parametrize("scalar", ["0", "false", '""', "hello", "[1, 2]"])
def test_a_non_mapping_spec_runner_is_refused_not_overwritten(scalar: str) -> None:
    """Overwriting it would destroy a value the editor cannot even read.

    `dict(existing or {})` only raised for the TRUTHY non-mappings: `0`,
    `false` and `""` are falsey (and `dict("")` is `{}` anyway), so they
    reached the writer and were silently replaced with a mapping.

    The explicit `TypeError` this check raises is still there — `_stage`
    now wraps it into the one content-free exception used for every
    refusal on the render path, and its message already names only a
    type, so nothing is lost by wrapping it."""
    from dispatcher.core.spec_runner_config_actions import UnsafeEditError

    with pytest.raises(UnsafeEditError) as caught:
        _render_text(f"project: alpha\nspec_runner: {scalar}\n", _cand())
    assert caught.value.stage == "render"
    assert "TypeError" in str(caught.value)


@pytest.mark.parametrize(
    "absent", ["project: alpha\n", "project: alpha\nspec_runner:\n"]
)
def test_an_absent_or_null_spec_runner_still_gets_a_fresh_block(absent: str) -> None:
    """The flip side: `spec_runner:` with no value is not a non-mapping."""
    text, _, _ = _render_text(absent, _cand(max_retries=7))
    assert "max_retries: 7" in text


def test_replacing_the_overlay_keeps_the_comment_that_follows_the_block() -> None:
    """Clearing the overlay is not the only way to destroy the nested map the
    trailing text hangs off — swapping it for a different one does too."""
    base = """\
project: alpha
spec_runner:
  max_retries: 5
  extra_executor_config:
    executor:
      telegram_bot_token: "123:abc"

# col-0 standalone, right after the block
workstreams: []
"""
    cand = ConfigCandidate(
        typed={},
        extra_executor_config={"executor": {"budget_usd": 5.0}},
        base_mtime=0.0,
    )
    text, _, extra_changed = _render_text(base, cand)
    assert extra_changed is True
    assert "budget_usd: 5.0" in text
    assert "telegram_bot_token" not in text
    assert "# col-0 standalone, right after the block" in text
    # and it is still AFTER the block, not folded into it
    assert text.index("budget_usd") < text.index("# col-0 standalone")
    assert text.index("# col-0 standalone") < text.index("workstreams:")


def test_matching_the_files_null_spelling_does_not_leak_to_other_ruamel_users() -> None:
    """`add_representer` is a classmethod: reaching it through an instance
    would rewrite null rendering for every other YAML() in the process,
    including modules that have nothing to do with project.yaml."""
    from io import StringIO

    from ruamel.yaml import YAML

    _render_text(_BASE_YAML_UNKNOWN_KEY, _cand(max_retries=7))

    buf = StringIO()
    YAML().dump({"k": None}, buf)
    assert buf.getvalue() == "k:\n"


def test_commit_message_lists_changed_keys_with_fallback() -> None:
    from dispatcher.core.spec_runner_config_actions import _commit_message

    assert _commit_message(["max_retries", "review_model"], False) == (
        "chore(spec-runner): update config (max_retries, review_model)"
    )
    assert _commit_message(["claude_model"], True) == (
        "chore(spec-runner): update config (claude_model, extra_executor_config)"
    )
    # no listable keys -> bare message, never empty parentheses
    assert _commit_message([], False) == "chore(spec-runner): update config"


def test_target_resolves_across_roots(tmp_path: Path) -> None:
    """A config in the SECOND root resolves there — and propose-pr is
    invoked against that root, not a same-named dir in the first."""
    import json as _json

    root1 = tmp_path / "root1"
    root1.mkdir()
    root2 = tmp_path / "root2"
    repo = root2 / "beta"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "project.yaml").write_text(_PROJECT_YAML)

    payload = {"ok": True, "detail": "pull request created"}
    command, record = fake_checker(tmp_path, payload)
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(root1, root2)), command=command
    )
    outcome = runner.run("beta", _candidate(repo))

    assert outcome.ok, outcome.error
    argv = _json.loads(record.read_text())["argv"]
    assert argv[1] == str(root2 / "beta")  # the SECOND root's dir


def test_target_prefers_first_root_on_name_collision(tmp_path: Path) -> None:
    """Same-named config in both roots → first root wins (discovery order)."""
    import json as _json

    for i in (1, 2):
        repo = tmp_path / f"root{i}" / "gamma"
        repo.mkdir(parents=True)
        _git(repo, "init", "-q")
        (repo / "project.yaml").write_text(_PROJECT_YAML)

    payload = {"ok": True, "detail": "pull request created"}
    command, record = fake_checker(tmp_path, payload)
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path / "root1", tmp_path / "root2")),
        command=command,
    )
    outcome = runner.run("gamma", _candidate(tmp_path / "root1" / "gamma"))

    assert outcome.ok, outcome.error
    argv = _json.loads(record.read_text())["argv"]
    assert argv[1] == str(tmp_path / "root1" / "gamma")


def test_the_config_runner_has_no_parse_path_of_its_own(tmp_path: Path) -> None:
    """This module shells out to the same producer as `core/actions.py`,
    and had the same second `json.loads` with the same `.get()` per field.
    Two parse paths over one contract are two sets of rules about what to
    believe, and they drift. A payload that is not an actions/v1 envelope
    must be refused here exactly as it is there."""
    repo = make_project(tmp_path, "alpha")
    # Carries its own `schema_version`, so `fake_checker` passes it through
    # untouched — which is how a test sends a drifted envelope on purpose
    # rather than having the helper quietly repair it.
    drifted = {
        "schema_version": 2,
        "result_kind": "action",
        "action": "propose-pr",
        "dir": "alpha",
        "ok": True,
        "pr_url": "https://x",
    }
    command, _ = fake_checker(tmp_path, drifted)
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=command
    )
    outcome = runner.run("alpha", _candidate(repo))
    assert outcome.ok is False
    assert outcome.error is not None
    assert outcome.pr_url is None, "nothing may be read out of a refused envelope"


def test_the_config_runner_projects_only_what_the_producer_set(
    tmp_path: Path,
) -> None:
    """`propose-pr` has no concept of a merge, so the outcome must carry no
    answer about one — and `.get("merged")` returning `None` is exactly the
    answer this distinction forbids."""
    import json as _json

    fixtures = (
        Path(__file__).parent.parent
        / "contracts"
        / "github-checker-actions"
        / "v1"
        / "fixtures"
    )
    payload = _json.loads((fixtures / "propose-pr-created.json").read_text())
    command, _ = fake_checker(tmp_path, payload)
    repo = make_project(tmp_path, "alpha")
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=command
    )
    outcome = runner.run("alpha", _candidate(repo))
    assert outcome.ok is True
    assert outcome.action == "update-spec-runner-config", "the consumer's own verb"
    assert "merged" not in outcome.model_fields_set
    assert "pr_url" in outcome.model_fields_set


def test_the_config_runner_checks_the_exit_code_too(tmp_path: Path) -> None:
    """The exit code is half the contract, and it is `ingest` that checks
    it — so this runner gets that check by going through `ingest` rather
    than by remembering to do it. An answer that says `ok: true` and exits
    1 is a producer contradicting itself, and neither half may be believed
    over the other."""
    repo = make_project(tmp_path, "alpha")
    payload = {"ok": True, "detail": "pull request created"}
    command, _ = fake_checker(tmp_path, payload, returncode=1)
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=command
    )
    outcome = runner.run("alpha", _candidate(repo))
    assert outcome.ok is False
    assert outcome.error is not None
    assert "exit" in outcome.error


def test_the_config_runner_survives_a_consumer_side_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The converged runners must degrade the same way. `ingest` raises
    more than `ContractViolation`, and a narrower guard here would leave
    a `propose-pr` that genuinely ran raising out of the runner — the same
    hole `core/actions.py` states a guarantee against."""
    import dispatcher.core.spec_runner_config_actions as module

    def explode(raw: str, *, returncode: int) -> object:
        raise RuntimeError("the consumer could not interpret its own schema")

    monkeypatch.setattr(module, "ingest", explode)
    repo = make_project(tmp_path, "alpha")
    command, _ = fake_checker(tmp_path, {"ok": True, "detail": "created"})
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=command
    )
    outcome = runner.run("alpha", _candidate(repo))
    assert outcome.ok is False
    # `run()` has an outer catch-all ("degrade, never raise"), so asserting
    # only `ok is False` cannot tell the two apart — the assertions below
    # are about the shape only the inner, ingestion-level guard produces:
    # the phase, and the wording that names it a readable answer we could
    # not use rather than an unclassified explosion.
    assert outcome.phase == PHASE_READABLE
    assert outcome.error is not None
    assert outcome.error.startswith("dispatcher could not interpret")


def test_a_timed_out_propose_pr_is_not_reported_as_nothing_happened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child started and was killed, so the PR may already exist.
    `phase` is what stops that from reading as "nothing happened" — and it
    was left unset on this path while the success path set it, which is
    worse than absent: a half-populated field looks answered."""
    import subprocess as _subprocess

    def timeout(*args: object, **kwargs: object) -> object:
        raise _subprocess.TimeoutExpired(cmd="github-checker", timeout=1)

    repo = make_project(tmp_path, "alpha")
    command, _ = fake_checker(tmp_path, {"ok": True, "detail": "created"})
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=command
    )
    # Patched last: the fixtures above shell out to git themselves, and a
    # global stub would time those out instead.
    monkeypatch.setattr(_subprocess, "run", timeout)
    outcome = runner.run("alpha", _candidate(repo))
    assert outcome.ok is False
    assert outcome.phase == PHASE_LAUNCHED_UNREADABLE


def test_the_config_runner_refuses_an_answer_about_another_verb(
    tmp_path: Path,
) -> None:
    """It asks github-checker for `propose-pr` while reporting itself as
    `update-spec-runner-config`, so the comparison is against the argv
    verb, not the DTO's label."""
    import json as _json

    fixtures = (
        Path(__file__).parent.parent
        / "contracts"
        / "github-checker-actions"
        / "v1"
        / "fixtures"
    )
    payload = _json.loads((fixtures / "open-pr-created.json").read_text())
    repo = make_project(tmp_path, "alpha")
    command, _ = fake_checker(tmp_path, payload)
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=command
    )
    outcome = runner.run("alpha", _candidate(repo))
    assert outcome.ok is False
    assert outcome.pr_url is None
    assert outcome.error is not None
    assert "propose-pr" in outcome.error and "open-pr" in outcome.error


def test_the_config_runners_audit_line_is_one_line_too(tmp_path: Path, caplog) -> None:
    """The audit-line flattening landed on `core/actions.py` and not on
    this runner, which logs the same producer-derived `detail`/`error` at
    its own site. Two converged consumers, one of them still able to turn
    a single attempt into a six-line record — the same defect one module
    over, which is the shape every round of this review has found."""
    repo = make_project(tmp_path, "alpha")
    command, _ = fake_checker(
        tmp_path,
        {
            "ok": False,
            "detail": "first line\nsecond line",
            "error": "a\nb\nc",
            "pr_url": None,
            "pr_state": None,
            "branch": None,
            "base_branch": None,
            "commit_sha": None,
            "changed_paths": None,
        },
        returncode=1,
    )
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=command
    )
    with caplog.at_level("INFO", logger="dispatcher.actions.spec_runner_config"):
        runner.run("alpha", _candidate(repo))
    lines = [
        r.getMessage()
        for r in caplog.records
        if "action=update-spec-runner-config" in r.getMessage()
    ]
    assert lines, "the attempt must be audited at all"
    assert all("\n" not in line for line in lines), lines


def test_an_undecodable_answer_from_a_child_that_ran_keeps_its_phase(
    tmp_path: Path,
) -> None:
    """`core/actions.py` captures **bytes** and decodes them itself, with a
    comment saying the phase boundary has to be a boundary in the CODE.
    This runner still passed `text=True`, so the decode happens inside
    `subprocess.run` — after the child branched, committed, pushed and
    opened a PR — and a `UnicodeDecodeError` escapes `_invoke` into
    `run()`'s catch-all, which builds an outcome with no `phase` at all.

    `phase=None` is indistinguishable from a pre-launch refusal, so the
    operator reads "nothing happened" for an attempt that opened a PR:
    exactly the inference `PHASE_*` exists to prevent."""
    repo = make_project(tmp_path, "alpha")
    script = tmp_path / "undecodable.py"
    script.write_text("import sys; sys.stdout.buffer.write(b'\\xff\\xfe not utf-8')\n")
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    outcome = runner.run("alpha", _candidate(repo))
    assert outcome.ok is False
    assert outcome.phase == PHASE_LAUNCHED_UNREADABLE


def test_a_failure_before_the_fork_says_so(tmp_path: Path) -> None:
    """The mirror: an `OSError` that is not `FileNotFoundError` (EACCES on
    the binary, E2BIG from an oversized argv, ENOMEM) happens before any
    child exists, so it must not read as "a mutation whose outcome is
    unknown". It reaches `run()`'s catch-all rather than an arm of its
    own — which is fine now that the catch-all labels itself, and is why
    this asserts the property rather than the arm."""
    repo = make_project(tmp_path, "alpha")
    not_executable = tmp_path / "not_executable"
    not_executable.write_text("#!/bin/sh\necho hi\n")
    not_executable.chmod(0o644)
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=(str(not_executable),)
    )
    outcome = runner.run("alpha", _candidate(repo))
    assert outcome.ok is False
    assert outcome.phase == PHASE_PRE_LAUNCH


_SECRET = "s3cr3t-telegram-token-ABC123"


def test_a_duplicate_key_refusal_does_not_echo_the_values(tmp_path: Path) -> None:
    """MEASURED leak, not hypothetical: ruamel's DuplicateKeyError renders as
    'found duplicate key "a" with value "2" (original value: "<secret>")'.
    A neighbour's project.yaml routinely holds tokens."""
    from dispatcher.core.spec_runner_config_actions import (
        UnsafeEditError,
        build_new_yaml_bytes,
    )

    base = f'a: "{_SECRET}"\na: 2\nspec_runner:\n  max_retries: 5\n'
    with pytest.raises(UnsafeEditError) as caught:
        build_new_yaml_bytes(base.encode("utf-8"), _cand())
    assert _SECRET not in str(caught.value)
    assert caught.value.stage == "parse"
    assert "DuplicateKeyError" in str(caught.value)


def test_invalid_utf8_refuses_without_echoing_bytes(tmp_path: Path) -> None:
    from dispatcher.core.spec_runner_config_actions import (
        UnsafeEditError,
        build_new_yaml_bytes,
    )

    with pytest.raises(UnsafeEditError) as caught:
        build_new_yaml_bytes(b"project: \xff\xfe bad\n", _cand())
    assert caught.value.stage == "decode"
    assert "UnicodeDecodeError" in str(caught.value)


def test_no_reference_to_the_original_exception_survives(tmp_path: Path) -> None:
    """The original object carries the secret in its own message, so keeping
    it anywhere would make this boundary depend on some future logger's
    behaviour. Walk the whole chain — asserting on str() would not see it."""
    from dispatcher.core.spec_runner_config_actions import (
        UnsafeEditError,
        build_new_yaml_bytes,
    )

    base = f'a: "{_SECRET}"\na: 2\nspec_runner:\n  max_retries: 5\n'
    with pytest.raises(UnsafeEditError) as caught:
        build_new_yaml_bytes(base.encode("utf-8"), _cand())
    err = caught.value
    assert err.__cause__ is None
    assert err.__context__ is None
    assert not hasattr(err, "original")
    assert_no_secret_reachable(err, _SECRET)


def test_a_bug_in_the_safe_formatter_still_leaves_no_secret_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_safe_cause` is not total — it is called WHILE `_stage` is handling
    the secret-bearing original, so a bug in it (an AttributeError on a
    `problem_mark`-shaped assumption, say) is exactly the kind of statement
    that lives outside every `_stage` block's own coverage. Nothing may
    escape `build_new_yaml_bytes` except a scrubbed `UnsafeEditError`, not
    an unrelated exception with the original still on its `__context__`."""
    import dispatcher.core.spec_runner_config_actions as mod

    def boom(err: BaseException) -> str:
        raise AttributeError("_safe_cause itself is broken")

    monkeypatch.setattr(mod, "_safe_cause", boom)
    base = f'a: "{_SECRET}"\na: 2\nspec_runner:\n  max_retries: 5\n'
    with pytest.raises(mod.UnsafeEditError) as caught:
        mod.build_new_yaml_bytes(base.encode("utf-8"), _cand())
    err = caught.value
    assert err.stage == "unknown"
    assert err.__cause__ is None
    assert err.__context__ is None
    assert_no_secret_reachable(err, _SECRET)


def test_the_outcome_and_audit_line_carry_no_file_content(
    tmp_path: Path, caplog
) -> None:
    """ActionOutcome.error and the audit line are the surfaces this design
    claims to protect. A clean exception does not prove a caller has not
    appended repr(original) on the way out."""
    import logging

    repo = make_project(tmp_path, "alpha")
    (repo / "project.yaml").write_text(
        f'a: "{_SECRET}"\na: 2\nspec_runner:\n  max_retries: 5\n'
    )
    runner = SpecRunnerConfigActionRunner(DispatcherConfig(roots=(tmp_path,)))
    candidate = _candidate(repo, max_retries=7)
    with caplog.at_level(logging.INFO, logger="dispatcher.actions.spec_runner_config"):
        outcome = runner.run("alpha", candidate)

    assert outcome.ok is False
    assert outcome.phase == PHASE_PRE_LAUNCH
    assert _SECRET not in (outcome.error or "")
    assert _SECRET not in caplog.text


def test_the_catch_all_labels_itself_pre_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Everything `run()`'s catch-all can catch happens before the fork —
    rendering the YAML, writing the temp file, resolving the target — so
    it can say so instead of leaving the field unanswered."""
    import dispatcher.core.spec_runner_config_actions as module

    def boom(base_text: object, cand: object) -> str:
        raise RuntimeError("yaml render exploded")

    monkeypatch.setattr(module, "build_new_yaml_bytes", boom)
    repo = make_project(tmp_path, "alpha")
    command, _ = fake_checker(tmp_path, {"ok": True, "detail": "created"})
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=command
    )
    outcome = runner.run("alpha", _candidate(repo))
    assert outcome.ok is False
    assert outcome.phase == PHASE_PRE_LAUNCH
