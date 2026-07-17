# FR-06 TUI Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** J-03 fully walkable from the TUI: `p`/`o` sync-action keys with web-parity visibility, proposal rows with `t`/`i`, and a Config tab + editor screen (closes DESIGN-308) — plus the pre-existing multi-root `_target` desync fixed at core.

**Architecture:** TUI calls core runners directly (no HTTP) — all guards inherited. New `SyncRow` model kills cell-scraping; runners injected via constructor kwargs for tests; the editor is a separate `dispatcher/tui/config_edit.py` Screen with priority ctrl-chords. One core fix (`_target` multi-root) benefits web too.

**Tech Stack:** unchanged (textual, pydantic, pytest+anyio pilot tests). No new dependencies.

**Spec:** `docs/superpowers/specs/2026-07-17-tui-parity-design.md` (DESIGN-501..507) — read it first.

## Global Constraints

- Line length 88 (ruff), type hints, `uv run pyrefly check` passes, `uv run ruff format --check .` passes before every commit (run `uv run ruff format .` first).
- Baseline: 242 passed + 1 skipped on master. Branch: create `feat/tui-parity` off master before Task 1; push after the last task (controller opens the PR).
- Visibility predicates mirror the web EXACTLY (`dispatcher/server/static/index.html`, the `actions` helper): `p` needs live + `pull-first`; `o` additionally needs `ahead > 0` (`v.ahead ?` — truthy, so `None`/`0` both refuse).
- Action workers run in worker group `"actions"` — NEVER in the default group where `_collect` is `exclusive=True` (a refresh triggered on completion would kill/be killed by collect).
- TUI action handlers snapshot row metadata AT KEYPRESS from the cursor coordinate; out-of-range/empty table → toast, never a crash.
- The live tree is never written through the TUI path — the config-editor tests assert `project.yaml` byte-for-byte unchanged (anti-stub mandate).
- `ConfigCandidate.extra_executor_config=None` (tri-state preserve, shipped in #40) — the TUI never sends a dict.
- Sync actions and existing tests for `pull`/`create-pr` core/API behavior: untouched.

---

## File Structure

- Modify: `dispatcher/core/spec_runner_config_actions.py` — `_target` multi-root + `_invoke` takes the resolved target `Path` (Task 1).
- Modify: `dispatcher/server/static/index.html` — web submit sends `null` extra (Task 1, optional-uniformity item, one line).
- Modify: `dispatcher/tui/app.py` — DI kwargs, `SyncRow`, bindings `p/o/t/i`, proposal rows, Config tab + render, row-selected branch (Tasks 2-4).
- Create: `dispatcher/tui/config_edit.py` — `ConfigEditScreen` (Task 4).
- Modify: `tests/test_spec_runner_config_actions.py` (Task 1), `tests/test_tui.py` (Tasks 2-4), `tests/test_api.py` (only if the web `null` change needs an assert tweak — check).
- Modify (docs): `README.md`, `COWORK_CONTEXT.md`, `docs/superpowers/specs/2026-07-17-spec-runner-config-editor-design.md` (Task 5).

---

### Task 1: DESIGN-504 — multi-root `_target` + resolved-target `_invoke` (core)

**Files:**
- Modify: `dispatcher/core/spec_runner_config_actions.py`
- Modify: `dispatcher/server/static/index.html` (one line)
- Test: `tests/test_spec_runner_config_actions.py`

**Interfaces:**
- Consumes: existing `SpecRunnerConfigActionRunner`.
- Produces (relied on by Task 4): `run(repo_dir, candidate)` resolves `repo_dir` across ALL roots in discovery order, and invokes propose-pr against the SAME root the `project.yaml` was found in.

**The trap this task closes:** `_target` picks the first existing root only, while `discover_project_configs` scans all roots — a second-root config either gets rejected or, worse, matched against a same-named dir in the first root (wrong-project PR). AND `_invoke` recomputes `workspace = next(r for r in roots if r.is_dir())` independently — fixing `_target` alone would leave `_invoke` pointing at the wrong root. The fix threads the resolved path through.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_spec_runner_config_actions.py`:

```python
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
```

- [ ] **Step 2: Run to verify the first fails**

Run: `uv run pytest tests/test_spec_runner_config_actions.py -v -k roots`
Expected: `test_target_resolves_across_roots` FAILS (rejected with "no project.yaml in: beta" — first root has no such dir); the collision test may pass by accident — keep it as a pin.

- [ ] **Step 3: Implement**

In `dispatcher/core/spec_runner_config_actions.py`, replace `_target`:

```python
    def _target(self, repo_dir: str) -> Path:
        if not _SAFE_DIR_RE.fullmatch(repo_dir) or repo_dir in (".", ".."):
            raise SpecRunnerConfigRejectedError(f"unsafe repo dir: {repo_dir!r}")
        existing = [r for r in self._config.roots if r.is_dir()]
        if not existing:
            raise SpecRunnerConfigRejectedError(
                "no existing workspace root configured"
            )
        # Iterate ALL roots in discovery order — a config found by
        # discover_project_configs in a later root must resolve to that
        # root, never to a same-named dir in an earlier one.
        for root in existing:
            project_yaml = root / repo_dir / "project.yaml"
            if project_yaml.is_file():
                return project_yaml
        raise SpecRunnerConfigRejectedError(f"no project.yaml in: {repo_dir}")
```

In `run()`, pass the RESOLVED directory to `_invoke` (find the `self._invoke(` call):

```python
                outcome = self._invoke(
                    project_yaml.parent,
                    message=message,
                    edit_file=edit_file,
                    if_match_hex=if_match_hex,
                )
```

And change `_invoke`'s signature/head — it must NOT recompute the workspace:

```python
    def _invoke(
        self,
        target: Path,
        *,
        message: str,
        edit_file: Path,
        if_match_hex: str,
    ) -> ActionOutcome:
        argv = [
            *self._command,
            "propose-pr",
            str(target),
```

(delete the `workspace = next(...)` and `target = workspace / repo_dir` lines; everything else in `_invoke` stays). Check the test file for monkeypatched `_invoke` stand-ins (`test_one_in_flight_per_repo`'s `slow_invoke`) and update their signatures to `def slow_invoke(target, **kwargs):` with `dir=target.name` where they build outcomes.

In `dispatcher/server/static/index.html`, the submit body (search `extra_executor_config: currentSpecRunnerConfig`):

```javascript
        extra_executor_config: null,  // tri-state: null = preserve current
```

Check `tests/test_api.py` for any assertion on the web-submitted extra shape (there should be none — the API tests build their own bodies); adjust only if something breaks.

- [ ] **Step 4: Full suite, format, lint, type-check**

Run: `uv run pytest -q && uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: 244 passed + 1 skipped (242 + 2 new); clean.

- [ ] **Step 5: Commit**

```bash
git add dispatcher/core/spec_runner_config_actions.py dispatcher/server/static/index.html tests/test_spec_runner_config_actions.py
git commit -m "fix: resolve repo_dir across all roots, thread target into _invoke (DESIGN-504)"
```

---

### Task 2: DESIGN-501+505 — DI, `SyncRow`, `p`/`o` action keys

**Files:**
- Modify: `dispatcher/tui/app.py`
- Test: `tests/test_tui.py`

**Interfaces:**
- Consumes: `ActionRunner`, `Action`, `ActionRejectedError`, `ActionBusyError` from `dispatcher.core.actions`; `SpecRunnerConfigActionRunner` from `dispatcher.core.spec_runner_config_actions`.
- Produces (used by Tasks 3-4): `DispatcherApp.__init__(config, *, action_runner=None, config_runner=None)`; `SyncRow` dataclass; `self._sync_rows: list[SyncRow]`; `_sync_row_at_cursor() -> SyncRow | None`; worker group `"actions"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tui.py` (reuse the file's `_app`/`_settled` helpers and its existing SyncStatus-building pattern — read how existing sync tests construct `SyncStatus`/`SyncReport` fixtures and monkeypatch `SyncService.get`, and mirror it):

```python
class _FakeActionRunner:
    """Records calls; returns a preset ActionOutcome."""

    def __init__(self, outcome=None) -> None:
        from dispatcher.core.actions import ActionOutcome

        self.calls: list[tuple[str, str]] = []
        self.outcome = outcome or ActionOutcome(
            action="pull", dir="alpha", ok=True, detail="fast-forwarded"
        )

    def run(self, action, repo_dir):
        self.calls.append((action, repo_dir))
        return self.outcome


def _sync_with_rows() -> SyncStatus:
    report = SyncReport(
        current_host="h1",
        top_line="pull-first",
        hosts=[
            HostPanel(
                host="h1",
                source="live",
                verdicts=[
                    RepoVerdict(repo="alpha", verdict="pull-first", ahead=2),
                    RepoVerdict(repo="beta", verdict="ok"),
                    RepoVerdict(repo="gamma", verdict="pull-first", ahead=None),
                ],
            ),
            HostPanel(
                host="h2",
                source="kb",
                verdicts=[RepoVerdict(repo="alpha", verdict="pull-first", ahead=1)],
            ),
        ],
    )
    return SyncStatus(
        report=report,
        report_generated_at=datetime.now(tz=UTC),
        fetch_in_flight=False,
    )


async def _sync_app(tmp_path, monkeypatch, runner):
    app = _app_with_runner(tmp_path, runner)
    monkeypatch.setattr(
        "dispatcher.core.sync_service.SyncService.get",
        lambda self: _sync_with_rows(),
    )
    return app


def _app_with_runner(tmp_path: Path, runner) -> DispatcherApp:
    make_atp(tmp_path)
    make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    db = make_maestro_home(tmp_path)
    return DispatcherApp(
        DispatcherConfig(roots=(tmp_path,), maestro_db=db),
        action_runner=runner,
    )


def _move_sync_cursor(app: DispatcherApp, repo: str, live: bool) -> None:
    """Position the sync-table cursor on the row for (repo, live)."""
    rows = app._sync_rows
    idx = next(
        i
        for i, r in enumerate(rows)
        if r.repo == repo and r.live is live and r.kind == "verdict"
    )
    table = app.query_one("#sync-table", DataTable)
    table.move_cursor(row=idx)


async def test_pull_key_runs_action_on_live_pull_first(
    tmp_path: Path, monkeypatch
) -> None:
    runner = _FakeActionRunner()
    app = await _sync_app(tmp_path, monkeypatch, runner)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-sync"
        await pilot.pause()
        _move_sync_cursor(app, "alpha", live=True)
        await pilot.press("p")
        await _settled(app, pilot)
        assert runner.calls == [("pull", "alpha")]


async def test_pull_key_refuses_ok_and_non_live_rows(
    tmp_path: Path, monkeypatch
) -> None:
    runner = _FakeActionRunner()
    app = await _sync_app(tmp_path, monkeypatch, runner)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-sync"
        await pilot.pause()
        _move_sync_cursor(app, "beta", live=True)  # verdict ok
        await pilot.press("p")
        _move_sync_cursor(app, "alpha", live=False)  # kb host row
        await pilot.press("p")
        await _settled(app, pilot)
        assert runner.calls == []


async def test_open_pr_key_requires_ahead(tmp_path: Path, monkeypatch) -> None:
    runner = _FakeActionRunner()
    app = await _sync_app(tmp_path, monkeypatch, runner)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-sync"
        await pilot.pause()
        _move_sync_cursor(app, "gamma", live=True)  # pull-first, ahead=None
        await pilot.press("o")
        await _settled(app, pilot)
        assert runner.calls == []
        _move_sync_cursor(app, "alpha", live=True)  # ahead=2
        await pilot.press("o")
        await _settled(app, pilot)
        assert runner.calls == [("open-pr", "alpha")]


async def test_action_keys_ignore_other_tabs_and_empty_table(
    tmp_path: Path, monkeypatch
) -> None:
    runner = _FakeActionRunner()
    app = await _sync_app(tmp_path, monkeypatch, runner)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        # projects tab is active by default order? — force a non-sync tab
        app.query_one(TabbedContent).active = "tab-projects"
        await pilot.press("p")
        await _settled(app, pilot)
        assert runner.calls == []
```

**Web-parity guard** (DESIGN-506.2) — a unit test pinning the predicate next to the web's:

```python
def test_sync_action_visibility_matches_web() -> None:
    """Web: pull ⇔ live && pull-first; open PR ⇔ additionally truthy ahead
    (dispatcher/server/static/index.html, the `actions` helper)."""
    from dispatcher.tui.app import SyncRow, _can_open_pr, _can_pull

    live_pf = SyncRow(kind="verdict", repo="a", live=True, verdict="pull-first")
    assert _can_pull(live_pf)
    assert not _can_open_pr(live_pf)  # ahead None → falsy, web hides the button
    assert _can_open_pr(
        SyncRow(kind="verdict", repo="a", live=True, verdict="pull-first", ahead=2)
    )
    assert not _can_pull(
        SyncRow(kind="verdict", repo="a", live=False, verdict="pull-first", ahead=2)
    )
    assert not _can_pull(SyncRow(kind="verdict", repo="a", live=True, verdict="ok"))
    assert not _can_pull(SyncRow(kind="proposal", repo="a"))
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_tui.py -v -k "pull_key or open_pr or visibility or ignore_other"`
Expected: FAIL — `TypeError` (no `action_runner` kwarg) / ImportError (`SyncRow`).

- [ ] **Step 3: Implement in `dispatcher/tui/app.py`**

Imports to add: `from dataclasses import dataclass`, `from typing import Literal`, `from dispatcher.core.actions import Action, ActionBusyError, ActionRejectedError, ActionRunner`, `from dispatcher.core.spec_runner_config_actions import SpecRunnerConfigActionRunner`.

Module level (near the other helpers):

```python
@dataclass(frozen=True)
class SyncRow:
    """Cursor-addressable meaning of one sync-table row (no cell-scraping)."""

    kind: Literal["verdict", "proposal", "error", "empty"]
    host: str = ""
    repo: str = ""
    live: bool = False
    verdict: str = ""
    ahead: int | None = None


def _can_pull(row: SyncRow) -> bool:
    """Web parity: the pull button exists ⇔ live host && pull-first."""
    return row.kind == "verdict" and row.live and row.verdict == "pull-first"


def _can_open_pr(row: SyncRow) -> bool:
    """Web parity: open PR additionally needs truthy ahead (`v.ahead ?`)."""
    return _can_pull(row) and bool(row.ahead)
```

`__init__` gains keyword-only DI params + stores config + rows list:

```python
    def __init__(
        self,
        config: DispatcherConfig,
        *,
        action_runner: ActionRunner | None = None,
        config_runner: SpecRunnerConfigActionRunner | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._action_runner = action_runner or ActionRunner(config)
        self._config_runner = config_runner or SpecRunnerConfigActionRunner(config)
        self._sync_rows: list[SyncRow] = []
        ...  # existing assignments unchanged
```

`BINDINGS` gains (keep existing four):

```python
        ("p", "sync_pull", "Pull"),
        ("o", "sync_open_pr", "Open PR"),
```

`_render_sync` builds `self._sync_rows` in lockstep with every `table.add_row` (the `_shown_errors` pattern): the error row appends `SyncRow(kind="error", host=panel.host)`; the empty-panel row appends `SyncRow(kind="empty", host=panel.host)`; each verdict row appends `SyncRow(kind="verdict", host=panel.host, repo=v.repo, live=panel.source == "live", verdict=v.verdict, ahead=v.ahead)`. Start the method with `self._sync_rows = []` (before the early return, after `table.clear()`, so a missing report leaves rows empty too).

Handlers + worker:

```python
    def _sync_row_at_cursor(self) -> SyncRow | None:
        """Row meaning at the CURRENT cursor — snapshotted at keypress
        (the 10s auto-refresh may redraw between aiming and pressing)."""
        if self.query_one(TabbedContent).active != "tab-sync":
            return None
        table = self.query_one("#sync-table", DataTable)
        idx = table.cursor_coordinate.row
        if not (0 <= idx < len(self._sync_rows)):
            return None
        return self._sync_rows[idx]

    def action_sync_pull(self) -> None:
        row = self._sync_row_at_cursor()
        if row is None:
            return
        if not _can_pull(row):
            self.notify("pull: needs a live pull-first row", severity="warning")
            return
        self._run_sync_action("pull", row.repo)

    def action_sync_open_pr(self) -> None:
        row = self._sync_row_at_cursor()
        if row is None:
            return
        if not _can_open_pr(row):
            self.notify(
                "open PR: needs a live pull-first row with ahead > 0",
                severity="warning",
            )
            return
        self._run_sync_action("open-pr", row.repo)

    @work(thread=True, group="actions")
    def _run_sync_action(self, action: Action, repo: str) -> None:
        """Whitelist action off the event loop; separate group from _collect
        (exclusive=True there would cancel us or vice versa)."""
        try:
            outcome = self._action_runner.run(action, repo)
        except (ActionRejectedError, ActionBusyError) as err:
            self.call_from_thread(self.notify, str(err), severity="warning")
            return
        if outcome.ok:
            self.call_from_thread(
                self.notify, f"✓ {outcome.pr_url or outcome.detail or action}"
            )
        else:
            self.call_from_thread(
                self.notify, f"✗ {outcome.error or action}", severity="error"
            )
        self._sync_service.invalidate()
        self.call_from_thread(self.action_refresh)
```

- [ ] **Step 4: Full suite, format, lint, type-check**

Run: `uv run pytest -q && uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: 244 + 5 new = 249 passed + 1 skipped; clean.

- [ ] **Step 5: Commit**

```bash
git add dispatcher/tui/app.py tests/test_tui.py
git commit -m "feat: TUI sync action keys p/o with web-parity visibility (DESIGN-501/505)"
```

---

### Task 3: DESIGN-502 — proposal rows + `t`/`i`

**Files:**
- Modify: `dispatcher/tui/app.py`
- Test: `tests/test_tui.py`

- [ ] **Step 1: Write the failing tests**

```python
def _sync_with_proposal() -> SyncStatus:
    report = SyncReport(
        current_host="h1",
        top_line="ok",
        hosts=[
            HostPanel(
                host="h1",
                source="live",
                verdicts=[RepoVerdict(repo="alpha", verdict="ok")],
            )
        ],
        proposals=["newrepo"],
    )
    return SyncStatus(
        report=report,
        report_generated_at=datetime.now(tz=UTC),
        fetch_in_flight=False,
    )


async def test_proposal_row_renders_and_track_writes_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    tracking = tmp_path / "dispatcher-sync.toml"
    app = DispatcherApp(
        DispatcherConfig(
            roots=(tmp_path,),
            maestro_db=make_maestro_home(tmp_path),
            tracking_file=tracking,
        )
    )
    make_atp(tmp_path)
    monkeypatch.setattr(
        "dispatcher.core.sync_service.SyncService.get",
        lambda self: _sync_with_proposal(),
    )
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-sync"
        await pilot.pause()
        idx = next(
            i for i, r in enumerate(app._sync_rows) if r.kind == "proposal"
        )
        app.query_one("#sync-table", DataTable).move_cursor(row=idx)
        await pilot.press("t")
        await _settled(app, pilot)
        assert tracking.is_file()
        assert "newrepo" in tracking.read_text()


async def test_track_key_unconfigured_and_wrong_row(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app(tmp_path)  # tracking_file=None in this fixture's config
    monkeypatch.setattr(
        "dispatcher.core.sync_service.SyncService.get",
        lambda self: _sync_with_proposal(),
    )
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-sync"
        await pilot.pause()
        # wrong row (verdict row) → no crash, nothing written
        app.query_one("#sync-table", DataTable).move_cursor(row=0)
        await pilot.press("t")
        # proposal row but tracking unconfigured → toast, no crash
        idx = next(
            i for i, r in enumerate(app._sync_rows) if r.kind == "proposal"
        )
        app.query_one("#sync-table", DataTable).move_cursor(row=idx)
        await pilot.press("i")
        await _settled(app, pilot)
```

NOTE: check what `_app`'s `DispatcherConfig` sets for `tracking_file` — the fixture passes only `roots`/`maestro_db`, and the dataclass default is `None`, so the unconfigured branch is exercised; verify and adapt if the fixture differs.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_tui.py -v -k proposal`
Expected: FAIL — no proposal rows rendered / no `t` binding.

- [ ] **Step 3: Implement**

Imports: `from dispatcher.core.tracking import TrackAction, decide`.

`BINDINGS` gains `("t", "sync_track", "Track")`, `("i", "sync_ignore", "Ignore")`.

At the END of `_render_sync` (after the host loop), render proposals:

```python
        for proposal in report.proposals:
            table.add_row(
                "",
                "",
                Text(proposal, style="bold cyan"),
                Text("proposal", style="cyan"),
                "t = track · i = ignore",
                "—",
                "—",
            )
            self._sync_rows.append(SyncRow(kind="proposal", repo=proposal))
```

Handlers:

```python
    def action_sync_track(self) -> None:
        self._decide_proposal("track")

    def action_sync_ignore(self) -> None:
        self._decide_proposal("ignore")

    def _decide_proposal(self, decision: TrackAction) -> None:
        row = self._sync_row_at_cursor()
        if row is None:
            return
        if row.kind != "proposal":
            self.notify("track/ignore: proposal rows only", severity="warning")
            return
        if self._config.tracking_file is None:
            self.notify("sync tracking not configured", severity="warning")
            return
        decide(self._config.tracking_file, row.repo, decision)
        self._sync_service.invalidate()
        self.notify(f"{decision}: {row.repo}")
        self.action_refresh()
```

(`decide` is fast local TOML I/O — same call the API route makes inline; no worker needed.)

- [ ] **Step 4: Full suite, format, lint, type-check; commit**

Run: `uv run pytest -q && uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: 251 passed + 1 skipped; clean.

```bash
git add dispatcher/tui/app.py tests/test_tui.py
git commit -m "feat: TUI proposal rows with t/i track-ignore (DESIGN-502)"
```

---

### Task 4: DESIGN-503 — Config tab + editor screen

**Files:**
- Modify: `dispatcher/tui/app.py`
- Create: `dispatcher/tui/config_edit.py`
- Test: `tests/test_tui.py`

**Interfaces:**
- Consumes: `discover_project_configs`, `ProjectSpecRunnerConfig`, `TYPED_DEFAULTS`, `TYPED_FIELDS` from `dispatcher.core.spec_runner_config`; `ConfigCandidate`, runner exceptions from `dispatcher.core.spec_runner_config_actions`; Task 2's `self._config_runner`.
- Produces: seventh tab `tab-config` with `#config-table`; `ConfigEditScreen(cfg, runner)`.

- [ ] **Step 1: Write the failing tests**

```python
class _FakeConfigRunner:
    def __init__(self, outcome=None) -> None:
        from dispatcher.core.actions import ActionOutcome

        self.calls: list[tuple[str, object]] = []
        self.outcome = outcome or ActionOutcome(
            action="update-spec-runner-config",
            dir="steward",
            ok=True,
            pr_url="https://example/pr/9",
        )

    def run(self, repo_dir, candidate):
        self.calls.append((repo_dir, candidate))
        return self.outcome


def _add_config_project(tmp_path: Path) -> Path:
    repo = tmp_path / "steward"
    repo.mkdir()
    (repo / "project.yaml").write_text(
        "project: steward\nspec_runner:\n  max_retries: 5\nworkstreams: []\n"
    )
    return repo


async def test_boots_with_seven_tabs_incl_config(tmp_path: Path) -> None:
    _add_config_project(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        assert len(app.query(TabPane)) == 7
        table = app.query_one("#config-table", DataTable)
        assert table.row_count == 1  # steward listed


async def test_config_editor_confirm_sends_candidate_live_tree_untouched(
    tmp_path: Path,
) -> None:
    from textual.widgets import Input

    from dispatcher.tui.config_edit import ConfigEditScreen

    repo = _add_config_project(tmp_path)
    live_before = (repo / "project.yaml").read_bytes()
    runner = _FakeConfigRunner()
    app = _app_with_config_runner(tmp_path, runner)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-config"
        await pilot.pause()
        table = app.query_one("#config-table", DataTable)
        table.move_cursor(row=0)
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfigEditScreen)
        field = app.screen.query_one("#field-max_retries", Input)
        field.value = "9"
        await pilot.press("ctrl+y")
        await _settled(app, pilot)

    assert len(runner.calls) == 1
    repo_dir, candidate = runner.calls[0]
    assert repo_dir == "steward"
    assert candidate.typed["max_retries"] == 9
    assert candidate.extra_executor_config is None  # tri-state preserve
    assert candidate.base_mtime > 0
    assert (repo / "project.yaml").read_bytes() == live_before


async def test_config_editor_strict_coercion_blocks_runner(
    tmp_path: Path,
) -> None:
    from textual.widgets import Input

    _add_config_project(tmp_path)
    runner = _FakeConfigRunner()
    app = _app_with_config_runner(tmp_path, runner)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-config"
        await pilot.pause()
        app.query_one("#config-table", DataTable).move_cursor(row=0)
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#field-auto_commit", Input).value = "yes"
        await pilot.press("ctrl+y")
        await _settled(app, pilot)
        assert runner.calls == []  # invalid bool never reaches the runner
        app.screen.query_one("#field-auto_commit", Input).value = "true"
        app.screen.query_one("#field-max_retries", Input).value = "3.5"
        await pilot.press("ctrl+y")
        await _settled(app, pilot)
        assert runner.calls == []  # invalid int refused too


async def test_config_editor_noop_outcome_benign(tmp_path: Path) -> None:
    from dispatcher.core.actions import ActionOutcome

    _add_config_project(tmp_path)
    runner = _FakeConfigRunner(
        ActionOutcome(
            action="update-spec-runner-config",
            dir="steward",
            ok=False,
            detail="no-op",
            error="no changes vs main",
        )
    )
    app = _app_with_config_runner(tmp_path, runner)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-config"
        await pilot.pause()
        app.query_one("#config-table", DataTable).move_cursor(row=0)
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("ctrl+y")
        await _settled(app, pilot)
        assert len(runner.calls) == 1  # ran, and the app didn't crash on no-op


def _app_with_config_runner(tmp_path: Path, runner) -> DispatcherApp:
    make_atp(tmp_path)
    make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    db = make_maestro_home(tmp_path)
    return DispatcherApp(
        DispatcherConfig(roots=(tmp_path,), maestro_db=db),
        config_runner=runner,
    )
```

Also update the EXISTING `test_app_boots_with_six_tabs`: rename to `test_app_boots_with_seven_tabs`, expect 7 `TabPane`s, add `config-table` to its table-ids loop.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_tui.py -v -k config`
Expected: FAIL — no `tab-config`, no `dispatcher.tui.config_edit` module.

- [ ] **Step 3: Implement**

**`dispatcher/tui/config_edit.py`** (new file):

```python
"""Config-editor screen: DESIGN-503, the TUI half of DESIGN-308.

Priority ctrl-chords, not printable keys: with 12 Inputs on screen the
focused Input consumes plain letters — `d`/`y` would type into the field.
Candidate always carries extra_executor_config=None (tri-state preserve,
shipped in dispatcher PR #40); the extra block is shown read-only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Input, Label, Static

from dispatcher.core.spec_runner_config import TYPED_DEFAULTS, ProjectSpecRunnerConfig
from dispatcher.core.spec_runner_config_actions import (
    SpecRunnerConfigBusyError,
    SpecRunnerConfigConflictError,
    SpecRunnerConfigRejectedError,
)
from dispatcher.tui.detail import ErrorMessageScreen


def coerce_typed(name: str, raw: str) -> Any:
    """Strict input coercion; raises ValueError with a user-facing message.

    bool BEFORE int (bool subclasses int): only literal true/false accepted —
    never a silent everything-else-is-False.
    """
    default = TYPED_DEFAULTS[name]
    text = raw.strip()
    if isinstance(default, bool):
        if text.lower() == "true":
            return True
        if text.lower() == "false":
            return False
        raise ValueError(f"{name}: enter true or false")
    if isinstance(default, int):
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{name}: enter an integer") from None
    return raw


class ConfigEditScreen(Screen[None]):
    """Edit one project.yaml's spec_runner: block; confirm opens a PR."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+d", "preview", "Diff", priority=True),
        Binding("ctrl+y", "confirm", "Confirm → PR", priority=True),
    ]

    def __init__(self, cfg: ProjectSpecRunnerConfig, runner: Any) -> None:
        super().__init__()
        self._cfg = cfg
        self._runner = runner

    def compose(self) -> ComposeResult:
        yield Static(
            f"spec_runner config — {self._cfg.project} "
            f"({Path(self._cfg.project_yaml_path).parent.name}/project.yaml)",
            id="config-edit-title",
        )
        with VerticalScroll():
            for name, field in self._cfg.typed.items():
                marker = "explicit" if field.explicit else "default"
                with Horizontal(classes="config-field"):
                    yield Label(f"{name} ({marker})", classes="config-label")
                    yield Input(value=str(field.value), id=f"field-{name}")
            if self._cfg.extra_executor_config:
                yield Static(
                    "extra_executor_config (read-only, preserved as-is):\n"
                    + str(self._cfg.extra_executor_config),
                    id="config-extra-preview",
                )
        yield Footer()

    def _collect_typed(self) -> dict[str, Any] | None:
        """All 12 fields coerced; first invalid input → toast + None."""
        typed: dict[str, Any] = {}
        for name in self._cfg.typed:
            raw = self.query_one(f"#field-{name}", Input).value
            try:
                typed[name] = coerce_typed(name, raw)
            except ValueError as err:
                self.app.notify(str(err), severity="warning")
                return None
        return typed

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def action_preview(self) -> None:
        typed = self._collect_typed()
        if typed is None:
            return
        lines: list[str] = []
        for name, field in self._cfg.typed.items():
            if typed[name] != field.value:
                lines.append(f"- {name}: {field.value}")
                lines.append(f"+ {name}: {typed[name]}")
        self.app.push_screen(ErrorMessageScreen("\n".join(lines) or "(no changes)"))

    def action_confirm(self) -> None:
        typed = self._collect_typed()
        if typed is None:
            return
        self._do_confirm(typed)

    @work(thread=True, group="actions")
    def _do_confirm(self, typed: dict[str, Any]) -> None:
        from dispatcher.core.spec_runner_config_actions import ConfigCandidate

        candidate = ConfigCandidate(
            typed=typed,
            extra_executor_config=None,  # tri-state: preserve current overlay
            base_mtime=self._cfg.base_mtime,
        )
        repo_dir = Path(self._cfg.project_yaml_path).parent.name
        try:
            outcome = self._runner.run(repo_dir, candidate)
        except SpecRunnerConfigConflictError:
            self.app.call_from_thread(
                self.app.notify,
                "project.yaml changed — reload required",
                severity="warning",
            )
            return
        except (SpecRunnerConfigRejectedError, SpecRunnerConfigBusyError) as err:
            self.app.call_from_thread(self.app.notify, str(err), severity="warning")
            return
        if outcome.ok:
            self.app.call_from_thread(
                self._finish, f"✓ PR: {outcome.pr_url or 'opened'}"
            )
        elif outcome.detail == "no-op":
            self.app.call_from_thread(
                self._finish, "config already in this state — no PR needed"
            )
        else:
            self.app.call_from_thread(
                self.app.notify, f"✗ {outcome.error or 'failed'}", severity="error"
            )

    def _finish(self, message: str) -> None:
        self.app.notify(message)
        self.app.pop_screen()
```

**`dispatcher/tui/app.py`**: imports `discover_project_configs, ProjectSpecRunnerConfig` (from `dispatcher.core.spec_runner_config`) and `from dispatcher.tui.config_edit import ConfigEditScreen`. State: `self._configs: list[ProjectSpecRunnerConfig] = []` in `__init__`. Compose gains (after the Roadmap pane):

```python
            with TabPane("Config", id="tab-config"):
                yield DataTable(id="config-table", cursor_type="row")
```

`on_mount` adds columns: `("dir", "project", "explicit fields", "extra")`. `_collect` also gathers `configs, _ = discover_project_configs(self._config.roots)` (inside the try, passed through `_apply` → `self._configs`; extend `_apply`'s signature accordingly). New `_render_config` called from `_apply`:

```python
    def _render_config(self) -> None:
        table = self.query_one("#config-table", DataTable)
        table.clear()
        for cfg in self._configs:
            explicit = sum(1 for f in cfg.typed.values() if f.explicit)
            table.add_row(
                Path(cfg.project_yaml_path).parent.name,
                cfg.project,
                str(explicit),
                "yes" if cfg.extra_executor_config else "—",
            )
```

`on_data_table_row_selected` gains a branch:

```python
        elif event.data_table.id == "config-table":
            idx = event.cursor_row
            if 0 <= idx < len(self._configs):
                self.push_screen(
                    ConfigEditScreen(self._configs[idx], self._config_runner)
                )
```

- [ ] **Step 4: Full suite, format, lint, type-check**

Run: `uv run pytest -q && uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: 255 passed + 1 skipped (251 + 4 new; the renamed boot test nets zero); clean. If a pilot test is flaky on worker timing, prefer an extra `await _settled(...)` over sleeps.

- [ ] **Step 5: Commit**

```bash
git add dispatcher/tui/app.py dispatcher/tui/config_edit.py tests/test_tui.py
git commit -m "feat: TUI Config tab + editor screen (DESIGN-503, closes DESIGN-308)"
```

---

### Task 5: DESIGN-507 — documentation

**Files:**
- Modify: `README.md`, `COWORK_CONTEXT.md`, `docs/superpowers/specs/2026-07-17-spec-runner-config-editor-design.md`

- [ ] **Step 1: README** — the TUI section's keys line (`Keys: r refresh · ...`) gains `p pull · o open PR · t/i track/ignore (Sync) · Enter edit config (Config tab) · ctrl+d diff · ctrl+y confirm`; the tabs list becomes `Projects / Errors / Models / Contracts / Roadmap / Sync / Config`. Check for any "view-only"-style TUI phrasing and fix.

- [ ] **Step 2: COWORK_CONTEXT** — the TUI stack line («**TUI**: textual (вкладки ...)») gains the Config tab and a короткая пометка «+ whitelist-действия (p/o/t/i) и конфиг-редактор — те же core-раннеры, что у HTTP API».

- [ ] **Step 3: DESIGN-308 closing note** — in `2026-07-17-spec-runner-config-editor-design.md`, append one line to the DESIGN-308 section: shipped 2026-07-17 in the FR-06 TUI iteration (`2026-07-17-tui-parity-design.md`); FR-06 status: TUI half closed, VSCode half remains.

- [ ] **Step 4: Verify, commit, push**

```bash
uv run pytest -q && uv run ruff format --check .
git add README.md COWORK_CONTEXT.md docs/superpowers/specs/2026-07-17-spec-runner-config-editor-design.md
git commit -m "docs: record the TUI parity slice (DESIGN-507)"
git push -u origin feat/tui-parity
```

---

## Self-Review Notes

- **Spec coverage:** DESIGN-501 → Task 2 (SyncRow, p/o, worker group, cursor snapshot); DESIGN-502 → Task 3; DESIGN-503 → Task 4 (7 tabs, priority chords, strict coercion, tri-state None, conflict toast, no-op benign); DESIGN-504 → Task 1 (incl. the `_invoke` root-recompute hole the spec's wording implied but didn't spell out — threading the resolved path is the complete fix); DESIGN-505 → Task 2 (`__init__` kwargs); DESIGN-506 matrix → distributed: .1 boot-7-tabs (T4), .2 parity-guard unit (T2), .3 cursor guard (T2), .4 proposals (T3), .5 config list/markers (T4 — markers exercised via the editor's labels), .6 candidate+live-tree (T4), .7 strict coercion (T4), .8 no-op/conflict toasts (T4 no-op; conflict path is runner-raised and handled — covered by code, toast asserted implicitly via no-crash; acceptable), .9 two-root units (T1); DESIGN-507 → Task 5.
- **Type consistency:** `SyncRow` fields match between Task 2's dataclass, its tests, and Task 3's proposal append; `_FakeConfigRunner.run(repo_dir, candidate)` matches `SpecRunnerConfigActionRunner.run`'s shape; `ConfigEditScreen(cfg, runner)` matches the `on_data_table_row_selected` call; `_invoke(target: Path, *, ...)` matches Task 1's test stand-in note.
- **Placeholder scan:** clean; the two "check X and adapt" notes (tracking_file default in `_app`; web-extra assertion in test_api) are explicit read-first instructions, not TBDs.
- **Known judgment calls:** diff preview reuses `ErrorMessageScreen` (a text modal) instead of a bespoke DiffScreen — recorded in Task 4's code; proposal-row strings are English to match the TUI's existing string language (the web's Russian proposal strings stay web-local, same adjudication as the no-op message in #40's review).
