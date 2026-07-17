# Config-editor Un-gating Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un-gate the config-editor write path by reworking it onto github-checker's `propose-pr` — the live tree becomes read-only for this action class, only explicit-or-changed keys are emitted, `detail=="no-op"` is benign, and the whole thing is proven against real git, not `{"ok": true}` stubs.

**Architecture:** All changes stay inside the existing modules: `core/spec_runner_config_actions.py` (render-to-temp-file + `propose-pr` invocation), `core/actions.py` (4 additive `ActionOutcome` fields), `server/app.py` (gate removal), `static/index.html` (confirm restore + no-op info). One new test file for the real-git integration level.

**Tech Stack:** unchanged (Python 3.12, FastAPI, pydantic v2, ruamel.yaml, pytest+anyio+httpx). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-17-config-editor-ungating-design.md` (DESIGN-401..406) — read it first. The consumed `propose-pr` contract: `github-checker propose-pr <dir> --message <msg> --edit project.yaml=<file> --if-match project.yaml=<sha256_hex>`; JSON `ActionResult` on stdout (`ok/detail/error/pr_url` + `branch/base_branch/commit_sha/changed_paths`); **no-op is `ok=false`, exit 1, `detail=="no-op"`, JSON still on stdout**.

## Global Constraints

- Line length 88 (ruff), type hints required, `uv run pyrefly check` must pass, `uv run ruff format --check .` must pass before every commit (run `uv run ruff format .` first — CI enforces format).
- Baseline: 231 tests passing on master. Branch: create `feat/config-editor-ungating` off master before Task 1 (direct master commits forbidden); push + PR after the last task.
- The live `project.yaml` is READ (once per run) but NEVER written by dispatcher — every task's tests that touch the write path must assert the workspace file is byte-for-byte unchanged.
- `run()` must never let an exception escape for runtime failures: the guard exceptions (`SpecRunnerConfigRejectedError`/`Busy`/`Conflict`, mapped to 422/409) still raise; everything past the guards degrades to `ActionOutcome(ok=False)` (this CHANGES the shipped behavior of the second try-block, which currently re-raises — spec §3).
- `_invoke` parses stdout JSON independently of the subprocess return code (already true — keep it; the no-op test exists to pin exactly this).
- Sync actions (`pull`/`create-pr`), their `ActionRunner`, lock, and tests: untouched.

---

## File Structure

- Modify: `dispatcher/core/actions.py` — 4 additive `ActionOutcome` fields, nothing else.
- Modify: `dispatcher/core/spec_runner_config_actions.py` — `build_new_yaml_text` (new signature + DESIGN-402 emission), `_commit_message` (new), `run()` (hash + temp file + degrade-to-outcome), `_invoke()` (propose-pr argv + additive-field parsing), module docstring.
- Modify: `dispatcher/server/app.py` — delete gate constant + route check.
- Modify: `dispatcher/server/static/index.html` — restore armed confirm, no-op info branch.
- Modify: `tests/test_spec_runner_config_actions.py` — rework write-path tests to the new contract; add emission/message/no-op/additive-fields tests.
- Modify: `tests/test_api.py` — delete gate test, strip gate monkeypatches, add no-op passthrough test.
- Create: `tests/test_spec_runner_config_integration.py` — real-git fake binary + live smoke.
- Modify (docs): `docs/superpowers/specs/2026-07-17-spec-runner-config-editor-design.md`, `README.md`, `CLAUDE.md` (check), `COWORK_CONTEXT.md`.

---

### Task 1: Emission logic + message + `ActionOutcome` fields (pure functions first)

**Files:**
- Modify: `dispatcher/core/actions.py`
- Modify: `dispatcher/core/spec_runner_config_actions.py` (only `build_new_yaml_text` + new `_commit_message`)
- Test: `tests/test_spec_runner_config_actions.py`

**Interfaces:**
- Consumes: `TYPED_FIELDS`/`TYPED_DEFAULTS` from `dispatcher.core.spec_runner_config`; `ConfigCandidate` (unchanged).
- Produces (used by Task 2):
  - `build_new_yaml_text(base_text: str, candidate: ConfigCandidate) -> tuple[str, list[str], bool]` — (rendered YAML, changed typed keys in `TYPED_FIELDS` order, extra-changed flag). NOTE the signature change: takes the captured TEXT, no longer a `Path` — no second file read (spec DESIGN-401 step 3).
  - `_commit_message(changed_keys: list[str], extra_changed: bool) -> str`.
  - `ActionOutcome` gains `branch: str | None = None`, `base_branch: str | None = None`, `commit_sha: str | None = None`, `changed_paths: list[str] | None = None`.

**DESIGN-402 emission rule (exact):** with `current` = the `spec_runner:` mapping parsed from `base_text` (empty dict if absent), for each `k` in `TYPED_FIELDS` order:
- `cand_val = candidate.typed.get(k, current.get(k, TYPED_DEFAULTS[k]))` — a key absent from the candidate keeps its current-file value (fixes the known partial-submission drop as a side effect);
- emit `k: cand_val` iff `k in current` (explicit stays, even when equal to the default) OR `cand_val != TYPED_DEFAULTS[k]`;
- `changed_keys` collects `k` where `cand_val != current.get(k, TYPED_DEFAULTS[k])`.
`extra_changed = (candidate.extra_executor_config or {}) != (current.get("extra_executor_config") or {})`; the rendered block includes `extra_executor_config` iff `candidate.extra_executor_config` is non-empty (as shipped).

- [ ] **Step 1: Add the 4 `ActionOutcome` fields** (after `local_dirty` in `dispatcher/core/actions.py:31-42`):

```python
    branch: str | None = None
    base_branch: str | None = None
    commit_sha: str | None = None
    changed_paths: list[str] | None = None
```

Run: `uv run pytest tests/test_actions.py tests/test_api.py -q` — Expected: pass (additive, None defaults).

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_spec_runner_config_actions.py`:

```python
_BASE_YAML = """\
project: alpha
spec_runner:
  max_retries: 5
  claude_model: claude-opus-4-8
workstreams: []
"""


def _cand(**typed_overrides) -> ConfigCandidate:
    from dispatcher.core.spec_runner_config import TYPED_DEFAULTS

    return ConfigCandidate(
        typed={**TYPED_DEFAULTS, **typed_overrides}, base_mtime=0.0
    )


def test_emission_omits_implicit_defaults() -> None:
    from dispatcher.core.spec_runner_config_actions import build_new_yaml_text

    text, changed, extra_changed = build_new_yaml_text(
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
    from dispatcher.core.spec_runner_config_actions import build_new_yaml_text

    text, changed, _ = build_new_yaml_text(
        _BASE_YAML,
        _cand(max_retries=5, claude_model="claude-opus-4-8", review_model="x"),
    )
    assert "review_model: x" in text
    assert changed == ["review_model"]


def test_emission_keeps_explicit_even_when_set_back_to_default() -> None:
    from dispatcher.core.spec_runner_config import TYPED_DEFAULTS
    from dispatcher.core.spec_runner_config_actions import build_new_yaml_text

    text, changed, _ = build_new_yaml_text(
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
    from dispatcher.core.spec_runner_config_actions import build_new_yaml_text

    cand = ConfigCandidate(typed={"review_model": "y"}, base_mtime=0.0)
    text, changed, _ = build_new_yaml_text(_BASE_YAML, cand)
    # keys absent from the candidate keep their current-file values
    assert "max_retries: 5" in text
    assert "claude_model: claude-opus-4-8" in text
    assert "review_model: y" in text
    assert changed == ["review_model"]


def test_emission_preserves_rest_of_file() -> None:
    from dispatcher.core.spec_runner_config_actions import build_new_yaml_text

    text, _, _ = build_new_yaml_text(_BASE_YAML, _cand(max_retries=7))
    assert "project: alpha" in text
    assert "workstreams: []" in text


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
```

Note: `test_run_rejects_invalid_typed_field_before_touching_disk` and other existing tests call `build_new_yaml_text` only indirectly via `run()` — Task 2 reworks those; this task must keep the OLD `run()` working, so `run()`'s internal call gets a minimal adaptation in Step 3.

- [ ] **Step 3: Implement**

In `dispatcher/core/spec_runner_config_actions.py`, replace `build_new_yaml_text` (currently `spec_runner_config_actions.py:58-80`) with:

```python
def build_new_yaml_text(
    base_text: str, candidate: ConfigCandidate
) -> tuple[str, list[str], bool]:
    """Render project.yaml text with only its `spec_runner:` key replaced.

    Takes the CAPTURED base text (never re-reads the file — the caller
    hashed exactly these bytes for --if-match; a second read would reopen
    the TOCTOU window). Emits a typed key iff it is explicit in the current
    block OR its candidate value differs from the default (DESIGN-402) —
    implicit defaults are never materialized, so a stale TYPED_DEFAULTS
    mirror cannot leak into observed repos. Returns (rendered text,
    changed typed keys, extra-changed flag) for the commit message.

    ruamel round-trip mode preserves comments/order elsewhere in the file.
    `YAML()` defaults to `typ="rt"` — as safe as yaml.safe_load(); never
    pass `typ="unsafe"`.
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    doc = yaml.load(StringIO(base_text))
    current: dict[str, Any] = dict(doc.get("spec_runner") or {})
    new_block: dict[str, Any] = {}
    changed_keys: list[str] = []
    for key in TYPED_FIELDS:
        default = TYPED_DEFAULTS[key]
        cand_val = candidate.typed.get(key, current.get(key, default))
        if key in current or cand_val != default:
            new_block[key] = cand_val
        if cand_val != current.get(key, default):
            changed_keys.append(key)
    extra_changed = (candidate.extra_executor_config or {}) != (
        current.get("extra_executor_config") or {}
    )
    if candidate.extra_executor_config:
        new_block["extra_executor_config"] = candidate.extra_executor_config
    doc["spec_runner"] = new_block
    buf = StringIO()
    yaml.dump(doc, buf)
    return buf.getvalue(), changed_keys, extra_changed


def _commit_message(changed_keys: list[str], extra_changed: bool) -> str:
    """`--message` for propose-pr; stable, greppable, no empty parentheses."""
    parts = list(changed_keys)
    if extra_changed:
        parts.append("extra_executor_config")
    base = "chore(spec-runner): update config"
    return f"{base} ({', '.join(parts)})" if parts else base
```

Add `TYPED_DEFAULTS` to the existing import from `dispatcher.core.spec_runner_config`. Adapt the OLD `run()` body's call site minimally so the suite stays green until Task 2 rewrites it properly:

```python
            new_text, _, _ = build_new_yaml_text(
                project_yaml.read_text(), candidate
            )
```

- [ ] **Step 4: Run, format, lint, type-check**

Run: `uv run pytest -q && uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: 231 baseline + 6 new = 237 passed (the old write-path tests still pass — `run()` still writes in this task); clean.

- [ ] **Step 5: Commit**

```bash
git add dispatcher/core/actions.py dispatcher/core/spec_runner_config_actions.py tests/test_spec_runner_config_actions.py
git commit -m "feat: explicit-or-changed emission + commit message + additive outcome fields (DESIGN-402)"
```

---

### Task 2: Runner rework — render to temp file, delegate to `propose-pr`

**Files:**
- Modify: `dispatcher/core/spec_runner_config_actions.py` (`run()`, `_invoke()`, module docstring, imports)
- Test: `tests/test_spec_runner_config_actions.py` (rework write-path tests)

**Interfaces:**
- Consumes: Task 1's `build_new_yaml_text`/`_commit_message`; `hashlib`, `tempfile` (new imports).
- Produces (consumed by Task 3's API tests): `run()` signature unchanged; behavior — never writes the live tree; `_invoke(self, repo_dir: str, *, message: str, edit_file: Path, if_match_hex: str) -> ActionOutcome` building `[*self._command, "propose-pr", str(target), "--message", message, "--edit", f"project.yaml={edit_file}", "--if-match", f"project.yaml={if_match_hex}"]` and parsing the 4 additive fields.

- [ ] **Step 1: Rework the write-path tests first (they define the new contract)**

In `tests/test_spec_runner_config_actions.py`:

(a) Replace `fake_checker` with a recording variant (keep the name; all call sites updated):

```python
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
    record = tmp_path / "record.json"
    script = tmp_path / "fake_checker.py"
    script.write_text(
        "import json, sys\n"
        "argv = sys.argv[1:]\n"
        "edit_content = None\n"
        "for a in argv:\n"
        "    if a.startswith('project.yaml='):\n"
        "        p = a.split('=', 1)[1]\n"
        "        try:\n"
        "            edit_content = open(p).read()\n"
        "        except OSError:\n"
        "            pass\n"
        f"json.dump({{'argv': argv, 'edit_content': edit_content}}, "
        f"open({str(record)!r}, 'w'))\n"
        f"json.dump({payload!r}, sys.stdout)\n"
        f"sys.exit({returncode})\n"
    )
    return ("python3", str(script)), record
```

(b) Replace `test_run_writes_diff_and_delegates_to_github_checker` with:

```python
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
```

(c) Add the no-op subprocess-boundary test:

```python
def test_noop_rc1_with_json_is_parsed_not_no_json(tmp_path: Path) -> None:
    repo = make_project(tmp_path, "alpha")
    payload = {"ok": False, "detail": "no-op", "error": "no changes vs main"}
    command, _ = fake_checker(tmp_path, payload, returncode=1)
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=command
    )
    outcome = runner.run("alpha", _candidate(repo))
    assert not outcome.ok
    assert outcome.detail == "no-op"
    assert outcome.error == "no changes vs main"
    assert "no JSON" not in (outcome.error or "")
```

(d) Rework `test_write_failure_audits_and_frees_busy_slot`: the render/invoke block now DEGRADES instead of raising (spec §3). Replace the `pytest.raises(RuntimeError)` expectation with:

```python
    with caplog.at_level("INFO", logger="dispatcher.actions.spec_runner_config"):
        outcome = runner.run("alpha", candidate)
    assert not outcome.ok
    assert "yaml render exploded" in (outcome.error or "")
    assert any(
        "ok=False" in r.getMessage() and "yaml render exploded" in r.getMessage()
        for r in caplog.records
    )
    monkeypatch.undo()
    assert runner.run("alpha", _candidate(repo)).ok
```

(also update its `boom` monkeypatch target signature to the new `build_new_yaml_text(base_text, cand)` arity, and the final follow-up run's fake checker per the new helper shape — read the current test and adapt mechanically). Update the two remaining `fake_checker(...)` call sites (`test_audit_line_written` etc.) to unpack the new `(command, record)` return.

- [ ] **Step 2: Run to see the new tests fail**

Run: `uv run pytest tests/test_spec_runner_config_actions.py -v`
Expected: new/reworked tests FAIL (old `run()` still writes the live tree and calls `open-pr`).

- [ ] **Step 3: Rework `run()` and `_invoke()`**

Add `import hashlib`, `import tempfile` to the module imports. Replace the second try-block of `run()` (currently `spec_runner_config_actions.py:159-182`) with:

```python
        try:
            base_bytes = project_yaml.read_bytes()
            if_match_hex = hashlib.sha256(base_bytes).hexdigest()
            new_text, changed_keys, extra_changed = build_new_yaml_text(
                base_bytes.decode(), candidate
            )
            message = _commit_message(changed_keys, extra_changed)
            with tempfile.TemporaryDirectory(
                prefix="dispatcher-config-edit-"
            ) as tmp_dir:
                edit_file = Path(tmp_dir) / "project.yaml"
                edit_file.write_text(new_text)
                outcome = self._invoke(
                    repo_dir,
                    message=message,
                    edit_file=edit_file,
                    if_match_hex=if_match_hex,
                )
        except Exception as err:  # noqa: BLE001 — spec §3: degrade, never raise
            # Everything past the guards becomes a failed outcome: temp-dir
            # creation, decode, render, even unexpected bugs. Still audits.
            _audit.info(
                "action=update-spec-runner-config repo=%s ok=False error=%s",
                repo_dir,
                err,
            )
            outcome = ActionOutcome(
                action="update-spec-runner-config",
                dir=repo_dir,
                ok=False,
                error=str(err),
            )
        finally:
            with self._lock:
                self._busy.discard(repo_dir)
```

(The trailing success-audit + `return outcome` lines stay as they are.) Replace `_invoke` (currently `:184-215`) with:

```python
    def _invoke(
        self,
        repo_dir: str,
        *,
        message: str,
        edit_file: Path,
        if_match_hex: str,
    ) -> ActionOutcome:
        workspace = next(r for r in self._config.roots if r.is_dir())
        target = workspace / repo_dir
        argv = [
            *self._command,
            "propose-pr",
            str(target),
            "--message",
            message,
            "--edit",
            f"project.yaml={edit_file}",
            "--if-match",
            f"project.yaml={if_match_hex}",
        ]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=_ACTION_TIMEOUT
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as err:
            return ActionOutcome(
                action="update-spec-runner-config",
                dir=target.name,
                ok=False,
                error=str(err),
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return ActionOutcome(
                action="update-spec-runner-config",
                dir=target.name,
                ok=False,
                error=proc.stderr.strip() or "github-checker returned no JSON",
            )
        return ActionOutcome(
            action="update-spec-runner-config",
            dir=target.name,
            ok=bool(data.get("ok")),
            detail=data.get("detail"),
            error=data.get("error"),
            pr_url=data.get("pr_url"),
            branch=data.get("branch"),
            base_branch=data.get("base_branch"),
            commit_sha=data.get("commit_sha"),
            changed_paths=data.get("changed_paths"),
        )
```

Update the module docstring (`:1-9`): the runner renders content to a temp file and hands it to `github-checker propose-pr`; it never writes the live tree — its mutation surface on observed repos is zero; note the `detail=="no-op"` contract.

- [ ] **Step 4: Run, format, lint, type-check**

Run: `uv run pytest -q && uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: all pass (the Task 1 count ± the reworked tests; note `tests/test_api.py`'s POST test asserted `"max_retries: 9" in (repo / "project.yaml").read_text()` — that asserted the OLD live-tree write and now FAILS; flip that assertion to `read_bytes() == before` there as part of this task, it is the same contract change). Clean.

- [ ] **Step 5: Commit**

```bash
git add dispatcher/core/spec_runner_config_actions.py tests/test_spec_runner_config_actions.py tests/test_api.py
git commit -m "feat: write path delegates to propose-pr — zero live-tree writes (DESIGN-401)"
```

---

### Task 3: Gate removal — API + UI

**Files:**
- Modify: `dispatcher/server/app.py`
- Modify: `dispatcher/server/static/index.html`
- Test: `tests/test_api.py`

- [ ] **Step 1: Remove the gate from `app.py`**

Delete the `SPEC_RUNNER_CONFIG_WRITE_GATED = True` constant AND its whole TODO(gate) comment block (`app.py:64-70`), and the `if SPEC_RUNNER_CONFIG_WRITE_GATED: raise HTTPException(503, ...)` block in `action_update_spec_runner_config` (`app.py:332-341`).

- [ ] **Step 2: Update `tests/test_api.py`**

- Delete `test_spec_runner_config_update_gated_returns_503` entirely.
- In the four post-gate tests (`test_spec_runner_config_view_and_update`, `..._invalid_candidate_maps_to_422`, `..._busy_maps_to_409`, `..._stale_mtime_maps_to_409`): remove the `monkeypatch.setattr(app_module, "SPEC_RUNNER_CONFIG_WRITE_GATED", False)` lines and their `# gate off: ...` comments; drop the `import dispatcher.server.app as app_module` lines where the import becomes unused (check each).
- Add a no-op passthrough test at the route level:

```python
@pytest.mark.anyio
async def test_spec_runner_config_noop_reaches_client(
    tmp_path: Path, monkeypatch
) -> None:
    from dispatcher.core.actions import ActionOutcome
    from dispatcher.core.spec_runner_config_actions import (
        SpecRunnerConfigActionRunner,
    )

    def noop_run(self, repo_dir, candidate):
        return ActionOutcome(
            action="update-spec-runner-config",
            dir=repo_dir,
            ok=False,
            detail="no-op",
            error="no changes vs main",
        )

    monkeypatch.setattr(SpecRunnerConfigActionRunner, "run", noop_run)
    # build the same tmp workspace + client + token the sibling tests use
    # (copy the established pattern from test_spec_runner_config_view_and_update)
    ...
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["detail"] == "no-op"
```

(Fill the `...` by copying the sibling test's setup verbatim — workspace with `alpha/project.yaml`, client, session token, POST body; the plan intentionally doesn't duplicate those 15 lines here, they already exist twice in the file.)

- [ ] **Step 3: UI — restore confirm, add no-op branch**

In `dispatcher/server/static/index.html`: in the preview (first-click) path, replace the gated-disable block (`btn.disabled = true; btn.textContent = "PR gated: awaiting github-checker propose-pr";` around line 529, plus its comment) with the original arming behavior:

```javascript
    btn.textContent = "Confirm & open PR";
    btn.dataset.armed = "true";
```

In the submit (second-click) result handling, branch on no-op before the generic failure rendering:

```javascript
    if (!data.ok && data.detail === "no-op") {
      // English: the config panel's strings are uniformly English
      // (the Russian strings elsewhere belong to the sync screen)
      result.textContent = "config already in this state — no PR needed";
      result.className = "fresh";
    } else {
      result.textContent = data.ok
        ? "✓ PR: " + (data.pr_url ?? data.detail ?? "opened")
        : "✗ " + (data.error ?? "failed");
      result.className = data.ok ? "ok" : "err";
    }
```

(Read the current handler first — adapt to its exact variable names; the second-click POST logic itself is unchanged.)

- [ ] **Step 4: Run, format, lint, type-check**

Run: `uv run pytest -q && uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: green (count: −1 gate test, +1 no-op test), clean.

- [ ] **Step 5: Commit**

```bash
git add dispatcher/server/app.py dispatcher/server/static/index.html tests/test_api.py
git commit -m "feat: remove the write-path gate; UI confirm restored, no-op benign (DESIGN-403/404)"
```

---

### Task 4: Real-git integration test + live smoke (DESIGN-405 levels 2-3)

**Files:**
- Create: `tests/test_spec_runner_config_integration.py`

**Interfaces:** consumes only public pieces: `SpecRunnerConfigActionRunner`, `ConfigCandidate`, `DispatcherConfig`.

- [ ] **Step 1: Write the integration test (fake binary, REAL git)**

```python
"""DESIGN-405 level 2: the write path against REAL git.

The gate this feature replaces existed because {"ok": true} stubs masked a
broken contract. Here the fake github-checker performs propose-pr's
observable contract with real git: branch off origin/<default> in a temp
worktree, apply the --edit content, commit, push to a real bare origin.
Level 3 (live smoke with the real binary) is at the bottom, skipif.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.spec_runner_config import TYPED_DEFAULTS
from dispatcher.core.spec_runner_config_actions import (
    ConfigCandidate,
    SpecRunnerConfigActionRunner,
)

_PROJECT_YAML = "project: alpha\nspec_runner:\n  max_retries: 3\nworkstreams: []\n"

_FAKE_PROPOSE_PR = '''\
#!/usr/bin/env python3
"""Fake github-checker honoring propose-pr's observable contract, real git."""
import json, subprocess, sys, tempfile
from pathlib import Path


def git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(json.dump(
            {"ok": False, "error": r.stderr.strip()}, sys.stdout) or 1)
    return r.stdout.strip()


def main():
    assert sys.argv[1] == "propose-pr"
    target = Path(sys.argv[2])
    args = sys.argv[3:]
    message, edits = None, []
    i = 0
    while i < len(args):
        if args[i] == "--message":
            message = args[i + 1]; i += 2
        elif args[i] == "--edit":
            edits.append(args[i + 1]); i += 2
        elif args[i] == "--if-match":
            i += 2  # verified real-side by github-checker's own tests
        else:
            i += 1
    git(target, "fetch", "--prune")
    branch = "propose/fake-test"
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td) / "wt"
        git(target, "worktree", "add", str(wt), "-b", branch, "origin/main")
        paths = []
        for e in edits:
            repo_path, content_file = e.split("=", 1)
            (wt / repo_path).write_bytes(Path(content_file).read_bytes())
            paths.append(repo_path)
        git(wt, "add", "--", *paths)
        git(wt, "commit", "-m", message)
        sha = git(wt, "rev-parse", "HEAD")
        git(wt, "push", "-u", "origin", branch)
        git(target, "worktree", "remove", "--force", str(wt))
    git(target, "branch", "-D", branch)
    json.dump({"ok": True, "detail": "pull request created",
               "pr_url": "https://example/pr/42", "branch": branch,
               "base_branch": "main", "commit_sha": sha,
               "changed_paths": paths}, sys.stdout)


main()
'''


def _git(path: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def _workspace_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """A real bare origin + a workspace clone containing project.yaml."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "project.yaml").write_text(_PROJECT_YAML)
    _git(seed, "add", "project.yaml")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(workspace / "alpha")],
        check=True,
        capture_output=True,
    )
    clone = workspace / "alpha"
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    return origin, workspace


def test_write_path_end_to_end_real_git(tmp_path: Path) -> None:
    origin, workspace = _workspace_with_origin(tmp_path)
    clone = workspace / "alpha"
    live_before = (clone / "project.yaml").read_bytes()
    script = tmp_path / "fake_propose_pr.py"
    script.write_text(_FAKE_PROPOSE_PR)

    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(workspace,)),
        command=("python3", str(script)),
    )
    candidate = ConfigCandidate(
        typed={**TYPED_DEFAULTS, "max_retries": 9},
        base_mtime=(clone / "project.yaml").stat().st_mtime,
    )
    outcome = runner.run("alpha", candidate)

    assert outcome.ok, outcome.error
    assert outcome.commit_sha
    # the edit landed as a real commit on a real branch in the bare origin
    blob = _git(origin, "show", f"{outcome.branch}:project.yaml")
    assert "max_retries: 9" in blob
    assert "workstreams: []" in blob  # rest of file survived the round-trip
    # implicit defaults were NOT materialized (DESIGN-402, end to end)
    assert "task_timeout_minutes" not in blob
    # origin default branch did not move
    # (the fake pushed only propose/fake-test)
    assert _git(origin, "rev-parse", "main") != outcome.commit_sha
    # the live workspace clone is byte-for-byte untouched
    assert (clone / "project.yaml").read_bytes() == live_before


@pytest.mark.skipif(
    shutil.which("github-checker") is None,
    reason="live smoke: real github-checker binary not on PATH",
)
def test_write_path_live_smoke_real_binary(tmp_path: Path, monkeypatch) -> None:
    """DESIGN-405 level 3: the REAL binary + a fake gh on PATH."""
    origin, workspace = _workspace_with_origin(tmp_path)
    clone = workspace / "alpha"
    live_before = (clone / "project.yaml").read_bytes()
    fake_gh_dir = tmp_path / "bin"
    fake_gh_dir.mkdir()
    gh = fake_gh_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '"pr create") echo "https://example/pr/7"; exit 0 ;;\n'
        '"pr view") exit 1 ;;\n'
        "*) exit 1 ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)
    import os

    monkeypatch.setenv("PATH", f"{fake_gh_dir}:{os.environ['PATH']}")

    runner = SpecRunnerConfigActionRunner(DispatcherConfig(roots=(workspace,)))
    candidate = ConfigCandidate(
        typed={**TYPED_DEFAULTS, "max_retries": 9},
        base_mtime=(clone / "project.yaml").stat().st_mtime,
    )
    outcome = runner.run("alpha", candidate)

    assert outcome.ok, outcome.error
    assert outcome.pr_url == "https://example/pr/7"
    blob = _git(origin, "show", f"{outcome.branch}:project.yaml")
    assert "max_retries: 9" in blob
    assert (clone / "project.yaml").read_bytes() == live_before
```

- [ ] **Step 2: Run**

Run: `uv run pytest tests/test_spec_runner_config_integration.py -v`
Expected: `test_write_path_end_to_end_real_git` PASSES against Task 2's runner; the live smoke SKIPS (binary not on PATH on this machine) — both outcomes are the spec's stated acceptance. If the real-git test fails, that is a REAL contract gap in Tasks 1-2 — fix there, do not weaken the test.

- [ ] **Step 3: Full suite, format, lint, type-check; commit**

```bash
uv run pytest -q && uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add tests/test_spec_runner_config_integration.py
git commit -m "test: real-git integration + live smoke for the write path (DESIGN-405)"
```

---

### Task 5: Documentation (DESIGN-406)

**Files:**
- Modify: `docs/superpowers/specs/2026-07-17-spec-runner-config-editor-design.md`
- Modify: `README.md`
- Modify: `CLAUDE.md` (verify; adjust only if wording is now wrong)
- Modify: `COWORK_CONTEXT.md`

- [ ] **Step 1: Amendment note in the original design doc**

After the DESIGN-304 section (find `### DESIGN-305` and insert before it):

```markdown
### DESIGN-304 amendment (2026-07-17, un-gated)

DESIGN-304 as written assumed `github-checker open-pr` would branch/commit/
push a dirty worktree — it does not (its documented contract), which is why
the write path first shipped gated. The flow is now: render the new
`project.yaml` to a temp file and delegate to `github-checker propose-pr
--edit --if-match` — dispatcher never writes the live tree at all. This
closes §4's cross-class lock-race caveat by construction. Full delta:
`2026-07-17-config-editor-ungating-design.md` (DESIGN-401..406).
```

- [ ] **Step 2: README opening**

Replace the first paragraph's "Read-only monitoring dashboard ..." with wording that stays honest: primarily a read-only monitoring dashboard; the ONLY mutations are a narrow, human-click-gated, PR-only whitelist (sync `pull`/`create-pr` and the spec-runner config editor, all delegated to `github-checker`; dispatcher itself never pushes or merges). Also check the README's API section mentions the config endpoints — add one line if absent.

- [ ] **Step 3: CLAUDE.md + COWORK_CONTEXT.md**

- `CLAUDE.md`: the X-02 bullet references `core/actions.py, core/spec_runner_config_actions.py` and says "может открывать PR ... только по явному клику" — still accurate post-un-gating; verify it doesn't reference the gate flag (it doesn't today) and leave as-is if correct.
- `COWORK_CONTEXT.md`: «Жёсткие инварианты» item 1 says строго read-only — amend to the NFR-01/X-02 reality (whitelist: sync actions + content-PR через propose-pr, только по клику, только PR); roadmap line «Возможное редактирование (пока строго view-only)» → отметить config-editor как shipped (write path via propose-pr).

- [ ] **Step 4: Verify docs didn't break anything, commit, push**

```bash
uv run pytest -q && uv run ruff format --check .
git add docs/superpowers/specs/2026-07-17-spec-runner-config-editor-design.md README.md CLAUDE.md COWORK_CONTEXT.md
git commit -m "docs: record the un-gated write path (DESIGN-406)"
git push -u origin feat/config-editor-ungating
```

(Do not open the PR from a task — the controller does that after the final review.)

---

## Self-Review Notes

- **Spec coverage:** DESIGN-401 → Task 2 (single-read + hash + temp file + propose-pr argv + additive parsing; divergence semantics need no code — propose-pr refuses). DESIGN-402 → Task 1 (rule implemented + 5 unit tests incl. partial-candidate preservation). DESIGN-403 → Tasks 2 (runner passthrough no-op test at the subprocess boundary, rc=1+JSON) and 3 (route passthrough + UI branch). DESIGN-404 → Task 3. DESIGN-405 → Task 4 (level 2 acceptance-bar test with real git incl. origin-default-SHA and no-materialized-defaults assertions; level 3 skipif smoke). DESIGN-406 → Task 5. Spec §3 error rows: propose-pr error passthrough (Task 2 reworked delegate test), no-op benign (Tasks 2-3), missing binary (existing test still valid), temp-dir/unexpected failure degrades (Task 2 reworked write-failure test), divergence (propose-pr-side, covered by its repo; integration fake skips if-match verification deliberately — noted inline).
- **Placeholder scan:** one deliberate `...` in Task 3's no-op route test with explicit instructions to copy the sibling test's existing 15-line setup — the pattern exists twice in the file already; duplicating it a third time in the plan invites drift.
- **Type consistency:** `build_new_yaml_text(base_text: str, ...) -> tuple[str, list[str], bool]` consistent across Tasks 1 (def + unit tests) and 2 (call site + monkeypatch arity note); `_invoke` keyword names (`message`, `edit_file`, `if_match_hex`) match between Task 2's def and its test assertions; `fake_checker` returns `(command, record)` everywhere after Task 2 step 1.
- **Known judgment call:** the integration fake ignores `--if-match` verification (github-checker's own 162-test suite covers it; verifying it in the fake would test the fake, not dispatcher). Recorded in the fake's comment.
