# Design — FR-06 TUI slice: sync actions + config editor in the terminal

> **Context (2026-07-17):** FR-06 ("паритет терминал/IDE", brief priority
> Should) requires J-01/J-03 to be fully walkable from the TUI and the VSCode
> extension. This iteration takes the TUI half in full: sync ACTION keys
> (today the TUI Sync tab is view-only — J-03's "act" half is missing),
> proposal track/ignore (today the TUI silently drops `report.proposals` — a
> real parity gap found in exploration), and the config-editor screen
> (DESIGN-308, deferred twice). The VSCode half of FR-06 is explicitly a
> separate follow-up iteration (different stack, own PR series; shipped same day — see `2026-07-17-vscode-parity-design.md`; FR-06 closed).
> Numbering: DESIGN-501+. Two pre-write reviews are folded in below.

## 1. Principles

1. **TUI calls core runners directly, no HTTP.** The TUI already reads via
   `SnapshotService`/`SyncService`; actions go through the same
   `ActionRunner` / `SpecRunnerConfigActionRunner` instances the API uses.
   Every guard (busy-lock, mtime conflict, whitelist, unsafe-dir, schema
   validation) lives in the runners — the TUI inherits them and cannot
   bypass them. A keypress is the "explicit human action" of NFR-01; CSRF is
   a browser concern with no TUI analogue.
2. **Visibility rules mirror the web exactly** (review 1 amendment):
   - `p` (pull): row is a live-host verdict row AND verdict == `pull-first`;
   - `o` (open PR): same AND `ahead > 0` — matching
     `dispatcher/server/static/index.html`'s button logic verbatim
     (`v.ahead ? ...`).
   Wrong row / wrong tab → informational toast, never an error.
3. **Anti-stub mandate continues**: TUI tests assert the live tree is
   byte-for-byte untouched through the TUI path, with fake runners injected
   via constructor parameters, not deep monkeypatching.

## 2. Components

### DESIGN-501: Sync row model + action keys

`_render_sync` currently derives display rows inline; handlers must not
scrape cell text. Introduce an internal row model (review 1 amendment):

```python
@dataclass(frozen=True)
class SyncRow:
    kind: Literal["verdict", "proposal", "error", "empty"]
    host: str = ""
    repo: str = ""
    live: bool = False       # panel.source == "live"
    verdict: str = ""
    ahead: int | None = None
```

`_render_sync` builds `self._sync_rows: list[SyncRow]` parallel to the
table rows (the established `_shown_errors` pattern). Action handlers
snapshot the row metadata AT KEYPRESS from the current cursor coordinate
(the 10s auto-refresh can redraw between aiming and pressing; a moved/empty
table is guarded — out-of-range cursor → toast, no crash).

Keys `p`/`o` live in `App.BINDINGS` with an active-tab check (the existing
`action_project_errors` pattern). Execution: `ActionRunner.run()` in a
Textual thread worker in a **separate worker group** from the exclusive
`_collect` worker (review 2: otherwise a refresh triggered on completion
kills or is killed by the collect). On completion: toast (`✓ detail`/PR URL
or `✗ error`), `SyncService.invalidate()`, refresh. `ActionBusyError` →
toast.

### DESIGN-502: Proposal rows + track/ignore

`report.proposals` gets rendered as visually distinct rows
(`kind="proposal"`, dim/accent style: «обнаружен `<dir>` — отслеживать?»).
Keys `t` (track) / `i` (ignore) on proposal rows call `tracking.decide` +
`SyncService.invalidate()` — the same logic as `POST /api/sync/track`,
including the "tracking not configured" guard (`config.tracking_file is
None` → toast, mirroring the web's 409).

### DESIGN-503: Config tab + editor screen (closes DESIGN-308)

**New seventh tab "Config"**: a DataTable listing every discovered
`project.yaml` (`discover_project_configs(config.roots)` — deliberately
broader than the web, whose panel only appears when a project card's name
matches the directory; listed columns: dir, project, explicit-field count,
extra present). NOTE: the existing boot test expects six tabs — it becomes
seven and gains `config-table` (review 1 amendment; the plan updates
`test_app_boots_with_six_tabs` accordingly).

`Enter` on a row pushes a Textual `Screen`:

- One `Input` per typed field (12), label carrying the `(explicit)` /
  `(default)` marker from the read-model.
- `extra_executor_config` shown as a READ-ONLY YAML preview (parity with
  the web, which round-trips it unedited).
- **Key bindings use `Binding(..., priority=True)` on ctrl-chords** (review
  2 amendment — plain printable keys like `d`/`y` are consumed by the
  focused `Input` on a 12-input screen and would just type into the field):
  `ctrl+d` = diff preview (modal: `- key: old` / `+ key: new` lines,
  `(no changes)` when empty), `ctrl+y` = confirm, `escape` = cancel.
- **Strict input coercion** (review 1 amendment): bool fields accept ONLY
  `true`/`false` (case-insensitive) — anything else is a validation toast,
  NOT silently coerced to False; int fields must parse as int or toast; str
  fields pass through. A failed coercion never reaches the runner.
- Confirm builds `ConfigCandidate(typed=<all 12 from the form>,
  extra_executor_config=None, base_mtime=<mtime captured at screen open>)`
  — `None` is the ALREADY-SHIPPED tri-state "preserve current overlay"
  (commit `3eb39bb`, PR #40); no contract change is needed. (Review 1's
  concern that `extra=None` is unsupported was based on pre-#40 code.)
- Runner call in a thread worker (same group discipline as DESIGN-501).
  Results: `✓ PR <url>` toast; `detail=="no-op"` → benign info toast
  ("config already in this state — no PR needed", the web's exact
  semantics); `SpecRunnerConfigConflictError` (file changed since the
  screen opened — the diff preview may be stale, that's accepted) → toast
  "project.yaml changed — reload required" (review 1 amendment: named
  explicitly); `Rejected`/`Busy`/other errors → error toast. On success the
  screen pops.

### DESIGN-504: Core fix — multi-root resolution in `_target` (pre-existing bug)

`SpecRunnerConfigActionRunner._target` resolves `repo_dir` against the
FIRST existing root only, while `discover_project_configs` scans ALL roots
(review 2 finding). A config discovered in a second root is either rejected
("no project.yaml") or — worse — silently matched against a same-named
directory in the first root, proposing a PR against the wrong project. This
desync is **pre-existing and affects the web path too** (the GET lists all
roots; the POST resolves against the first) — fix it at the source:

`_target` iterates `self._config.roots` in order and picks the FIRST root
where `<root>/<repo_dir>/project.yaml` is a file (same order as discovery;
`_SAFE_DIR_RE` and all other guards unchanged). Single-root setups behave
identically. A unit test with two roots pins: a config in root 2 resolves
to root 2; a name present in both roots resolves to root 1 (discovery
order, documented).

Optional, included for uniformity: the web UI's submit switches from
sending the GET's current `extra_executor_config` dict (replace-with-same)
to sending `null` (preserve) — same outcome under the tri-state contract,
uniform semantics across surfaces, smaller payload.

### DESIGN-505: Constructor injection for runners

`DispatcherApp.__init__` gains optional keyword-only parameters
(`action_runner: ActionRunner | None = None`,
`config_runner: SpecRunnerConfigActionRunner | None = None`) defaulting to
constructing from `config` as today (review 1+2 amendment). Tests inject
fakes (command-override runners, the established core-test pattern) instead
of deep-monkeypatching app internals. The CLI path is unchanged.

### DESIGN-506: Testing

Textual-pilot convention (`tests/test_tui.py`), fake runners via DESIGN-505:

1. Boot test updated: SEVEN tabs incl. `config-table`.
2. `p` on a live pull-first row invokes the injected runner with the right
   repo; on a non-live or `ok` row → toast, runner NOT called; `o` requires
   `ahead > 0` (parity-guard: assert the visibility predicate equals the
   web's, pinned side by side).
3. Cursor-guard: empty table / out-of-range cursor → toast, no crash.
4. Proposal rows render; `t`/`i` call `tracking.decide` (sidecar written);
   unconfigured tracking → toast.
5. Config tab lists discovered configs across TWO roots; Enter opens the
   editor; field markers correct.
6. Editor flow with fake config-runner: candidate carries all 12 typed
   values from the form, `extra_executor_config is None`, correct
   `base_mtime`; the live `project.yaml` is byte-for-byte untouched through
   the whole TUI path (anti-stub mandate).
7. Strict coercion: bool input "yes" → toast, runner not called; int input
   "3.5" → toast; valid values coerce by original type.
8. No-op outcome → benign info toast (not error styling); conflict outcome
   → "reload required" toast.
9. DESIGN-504 unit tests (core, not TUI): two-root resolution + both-roots
   precedence.

### DESIGN-507: Documentation

- README: TUI section gains the new keys (`p`/`o`/`t`/`i`, Config tab,
  `ctrl+d`/`ctrl+y`) and drops "view-only" phrasing for the TUI if present.
- COWORK_CONTEXT: TUI line updated (tabs + actions).
- `2026-07-17-spec-runner-config-editor-design.md`: DESIGN-308 closing note
  (shipped in this iteration, pointer here).
- FR-06 status recorded: TUI half closed; VSCode half remains → next
  iteration note in this spec's context block is the tracker.

## 3. Error handling

| failure | behaviour |
|---|---|
| action key on wrong row/tab (visibility predicate false) | informational toast, runner never called |
| cursor out of range after auto-refresh redraw | toast, no crash |
| `ActionBusyError` / `SpecRunnerConfigBusyError` | toast «уже выполняется» |
| conflict on confirm (file changed while the screen was open) | toast "project.yaml changed — reload required"; stale diff preview is accepted-by-design |
| invalid bool/int input | validation toast at the TUI layer; runner not invoked |
| runner outcome ok=False (incl. propose-pr errors) | error toast with the outcome's error text; `detail=="no-op"` → benign info toast instead |
| tracking not configured | toast (the web's 409 equivalent) |

## 4. Out of scope

- VSCode half of FR-06 (actions + config editor in the extension) — next
  iteration.
- Editing `extra_executor_config` in the TUI form (read-only preview, web
  parity).
- AI value suggestions (DESIGN-307) — still deferred.
- Any new HTTP endpoints — the TUI consumes core directly.

## 5. Traceability

| Item | Design |
|---|---|
| FR-06 (J-03 "act" half in TUI) | DESIGN-501, 502 |
| FR-06 (J-01 already passable; config parity) / DESIGN-308 | DESIGN-503 |
| Web-parity visibility rules (`p`/`o`) | §1.2, DESIGN-501, DESIGN-506.2 |
| Proposals parity gap (TUI drops `report.proposals`) | DESIGN-502 |
| Tri-state extra (shipped in #40) consumed, not changed | DESIGN-503 |
| Pre-existing multi-root desync (web too) | DESIGN-504 |
| Testability without deep monkeypatching | DESIGN-505, 506 |
| Anti-stub mandate (live tree untouched via TUI) | DESIGN-506.6 |

## 6. Milestone

Single milestone, one PR series: DESIGN-501..507 together — the parity
claim ("J-03 fully walkable from the TUI") is only true with actions,
proposals, and the config screen all present.
