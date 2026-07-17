# Design — FR-06 TUI slice: sync actions, proposals, config editor (DESIGN-5xx)

> **Context (2026-07-17, night):** the web UI now has the full action surface —
> whitelist sync actions (`pull`/`create-pr`, DESIGN-204), tracking proposals
> («обнаружен … — отслеживать?», FR-02), and the un-gated spec-runner config
> editor (DESIGN-3xx/4xx, live via `propose-pr` since PR #40). The TUI is
> still the read-only dashboard of `2026-07-05-dispatcher-tui-design.md`.
> This design closes the TUI half of FR-06 (terminal/IDE parity): the same
> actions, the same visibility logic, the same core runners — no HTTP hop.
> DESIGN-308 ("TUI parity, M2, deferred") of the config-editor design is
> resolved by DESIGN-503 below. The VSCode half of FR-06 remains a later
> iteration. Numbering: DESIGN-501..505.

A cross-cutting note on NFR-01 (whitelist mutations, explicit human action):
the TUI calls the core runners (`ActionRunner`,
`SpecRunnerConfigActionRunner`) directly — it already runs without HTTP. An
explicit keypress is the "явный клик человека"; CSRF protection is a web
concern and does not apply to a local terminal process. All server-side
guards live in the runners themselves (unsafe-dir check, busy-lock, mtime
conflict, whitelist of actions), so the TUI inherits them for free — nothing
is re-implemented, nothing can be bypassed.

## 1. Components

### DESIGN-501: Sync actions on the Sync tab

Sync-table rows gain row metadata (host, repo, verdict, live-source flag,
ahead count) — today rows are render-only strings. New keys, active only on
the Sync tab:

- `p` — run `pull` on the row under the cursor.
- `o` — run `create-pr` on the row under the cursor.

Visibility is the web's logic verbatim (`index.html`: `actions(v, live)`):
both keys require the row to be a repo row of the **live** host with verdict
**pull-first**; `o` additionally requires `ahead > 0`. On any other row the
key is not an error — a neutral toast explains why («действие доступно только
на live-строках с pull-first» / «create-pr требует ahead > 0»), matching the
web's "—" placeholder semantics.

Execution: the same `ActionRunner` (`core/actions.py`) instance, invoked from
a textual `@work(thread=True)` worker — the runner shells out to
`github-checker` with a 120 s timeout and must not freeze the UI. On
completion: toast `✓ <detail>` (plus PR URL for `create-pr`) or `✗ <error>`,
then `SyncService.invalidate()` + refresh so the verdict reflects the new
state. `ActionBusyError` → toast «действие уже выполняется» (parity with the
web's 409).

### DESIGN-502: Tracking proposals on the Sync tab

Parity gap found during recon: the web renders `report.proposals` as
«обнаружен `<dir>` — отслеживать?» cards; the TUI drops them silently.

Proposal rows are appended to the sync table in a visually distinct style
(dim/italic, «обнаружен <dir>» in the repo column). New keys on those rows:

- `t` — track (confirm)
- `i` — ignore (reject)

Both call `tracking.decide` — the same function behind
`POST /api/sync/track` — with the same guard: `config.tracking_file is None`
→ toast «sync tracking not configured» (parity with the route's 409). After a
decision: invalidate + refresh (the proposal row disappears, tracked repos
gain verdicts on the next collect).

### DESIGN-503: Config editor — new "Config" tab

Closes DESIGN-308 of `2026-07-17-spec-runner-config-editor-design.md`.

A new top-level tab **Config** (deliberately not bolted onto the Projects
tab): a table listing every discovered `project.yaml` via
`discover_project_configs(roots)` (`core/spec_runner_config.py`). This is
intentionally **wider than the web**, where the config panel only appears on
a project card whose name resolves to a repo dir — the TUI lists all
configs, including projects the snapshot pipeline doesn't surface.

`Enter` on a row pushes a dedicated `Screen`:

- One `Input` per typed field (all 12, `TYPED_FIELDS` order), each labeled
  with its `(default)` / `(explicit)` marker from `TypedField.explicit`.
- `extra_executor_config` — read-only YAML preview (web parity: not editable
  there either; the overlay round-trips untouched).
- `d` — diff preview modal: `-key: old` / `+key: new` lines per changed key,
  «(no changes)» when the candidate equals the current state (web preview
  parity).
- `y` — confirm: build `ConfigCandidate` (typed = all 12 form values,
  `extra_executor_config=None` — the tri-state "preserve" arm,
  `base_mtime` = the mtime captured when the screen read the config) and run
  `SpecRunnerConfigActionRunner.run()` in a thread worker.
- `Esc` — cancel, pop the screen.

Input strings are coerced by the type of the original value (bool / int /
float / str), the same rule as the web's `typeof original` coercion; a value
that fails coercion is a toast, not a crash.

Result handling mirrors the web (DESIGN-403): `✓ <pr_url>` on success,
`ℹ «config already in this state — PR не нужен»` for `detail == "no-op"`
(benign, not an error), `✗ <error>` otherwise. Busy / conflict / rejected
runner exceptions surface as toasts with the runner's message (conflict says
reload — reopening the screen re-reads the file and its mtime).

### DESIGN-504: Tests (textual-pilot convention, `test_tui.py`)

Following the existing pilot conventions (`app.run_test()`, tmp workspace
fixtures):

- **Sync actions:** `p`/`o` on an eligible row invoke a fake runner
  (command-override with a JSON-printing script, exactly as in the core
  action unit tests) and toast the outcome; guard tests — wrong verdict, KB
  (non-live) host row, `o` with `ahead == 0`, busy — all toast without
  invoking anything.
- **Proposals:** proposal rows render distinct; `t`/`i` update the tracking
  sidecar via a real temp `tracking_file`; the not-configured guard toasts.
- **Config editor, full flow:** open tab → Enter → edit a field → `d` shows
  the diff → `y` runs the (fake-command) runner; assert the exact candidate
  composition — all 12 typed keys present and coerced,
  `extra_executor_config is None`, `base_mtime` matches the read — and that
  the live tree's `project.yaml` is byte-for-byte untouched (the DESIGN-405
  invariant holds through the TUI path too).
- **No-op branch:** fake runner returns `detail="no-op"` → info toast, not
  an error.
- **Parity guard:** a test pinning the TUI's action-visibility predicate to
  the web's logic (pull-first ∧ live; create-pr also needs ahead), so the
  two front-ends can't drift silently.

### DESIGN-505: Documentation

- `README.md` — TUI key reference gains `p`/`o`/`t`/`i`, the Config tab, and
  the editor screen keys (`d`/`y`/`Esc`).
- `COWORK_CONTEXT.md` — one line: TUI now carries the full action surface.
- `2026-07-17-spec-runner-config-editor-design.md` — closing note on
  DESIGN-308 pointing here.
- FR-06 status: TUI half **closed**; VSCode half — next iteration.

## 2. Error handling (delta rows)

| failure | behaviour |
|---|---|
| action key on an ineligible row | neutral toast explaining eligibility — not an error |
| runner busy (`ActionBusyError` / `SpecRunnerConfigBusyError`) | toast, no retry (parity with web 409) |
| tracking not configured | toast «sync tracking not configured» (parity with route 409) |
| config conflict (`SpecRunnerConfigConflictError`, mtime) | toast asking to reopen the screen (re-read = new base) |
| input fails type coercion | toast naming the field; nothing submitted |
| propose-pr `detail == "no-op"` | info toast, benign (DESIGN-403 parity) |
| runner subprocess timeout / binary missing | failed outcome toast with the runner's error string (existing `_invoke` handling) |

## 3. Out of scope

- The VSCode half of FR-06 — next iteration.
- Editing `extra_executor_config` in the form (DESIGN-306 gap) — still
  deferred; read-only preview only.
- AI value suggestions (DESIGN-307) — still deferred.
- Any change to the web UI, the HTTP API, or the core runners — the TUI is
  strictly a new caller of existing core surfaces.

## 4. Traceability

| Item | Design |
|---|---|
| FR-06 (terminal parity), TUI half | DESIGN-501..503 |
| NFR-01 (whitelist, explicit human action) | §1 note — direct core calls, keypress = explicit action, guards live in runners |
| Web visibility logic (DESIGN-204 buttons) | DESIGN-501, parity guard in DESIGN-504 |
| FR-02 proposals (web-only until now) | DESIGN-502 |
| DESIGN-308 (config editor TUI parity, deferred M2) | DESIGN-503 — closed |
| DESIGN-403 no-op semantics | DESIGN-503, DESIGN-504 no-op test |
| DESIGN-405 anti-stub invariant (live tree untouched) | DESIGN-504 config-flow test |
| Tri-state `extra_executor_config` (X-02 round 1) | DESIGN-503 — `None` = preserve |

## 5. Milestone

Single milestone: DESIGN-501..505 land together in one PR. The three
features share the row-metadata plumbing and the worker/toast pattern;
splitting them would ship the parity claim before it is true.
