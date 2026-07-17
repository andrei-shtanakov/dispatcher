# Design — FR-06 VSCode slice: sync actions + config editor in the IDE

> **Context (2026-07-17):** the TUI half of FR-06 shipped in PR #44; this is
> the VSCode half — after it, FR-06 ("паритет терминал/IDE") closes fully.
> Today the extension is read-only: three tree views + a status-bar verdict,
> `ApiClient` is GET-only and parses only `top_line` from `/api/sync`, no
> action surface, no config editor. Unlike the TUI (which calls core runners
> directly), VSCode consumes the HTTP API — so the CSRF token and HTTP error
> mapping ARE in play here. Numbering: DESIGN-601+. Two pre-write reviews
> folded in below; one review point (that `extra_executor_config: null`
> needs a backend change) was verified factually outdated — tri-state
> `dict | None = None` shipped in PR #40 (`UpdateSpecRunnerConfigRequest`,
> app.py:108) and the web already sends `null` (index.html:539, TUI-slice
> Task 1). No backend contract change is needed for the editor.

## 1. Principles

1. **HTTP API is the only channel** — the extension never touches core or
   the filesystem. Everything the TUI got from core, VSCode gets from
   endpoints that already exist (plus ONE small new list endpoint,
   DESIGN-601).
2. **Visibility parity is finite strings, not logic in `when` clauses**
   (review 2): tree items carry exactly one of three `contextValue`s —
   `dispatcherSyncVerdict.pull` (live + pull-first), 
   `dispatcherSyncVerdict.pullPr` (… + truthy ahead — both actions apply),
   `dispatcherSyncProposal`. Menu `when` clauses match these literally, no
   regex. The predicate that ASSIGNS them is a pure function in `model.ts`
   with a parity-guard unit test mirroring the TUI's
   (`_can_pull`/`_can_open_pr`, tui/app.py:113-119, `ahead` truthy).
3. **Pure logic stays vscode-free** (the ext's established convention):
   the editor flow's state machine lives in a new `configFlow.ts`; `api.ts`
   keeps its "must stay vscode-free" header. vitest covers both without
   vscode mocks.

## 2. Components

### DESIGN-601: Server — `GET /api/spec-runner-configs`

`response_model=list[ProjectSpecRunnerConfig]`, body =
`discover_project_configs(config.roots)[0]` (warnings dropped — the list is
the payload; the read-model already carries per-field provenance and
`base_mtime`). Docstring documents the **basename-keyed action contract**
(review 1): the action key is the directory NAME; same-named dirs in two
roots appear twice in this list and BOTH resolve to the first root at
action time — fail-closed via the `base_mtime` conflict (409), but the
duplicate listing is documented, not hidden.

pytest pins the key property (review 2): the list includes configs whose
dir matches NO overview project card (e.g. a bare `steward/project.yaml`
in the workspace). Precision (Copilot): the per-name GET can already FETCH
any discovered config if the caller knows the dir name — what's missing is
ENUMERATION: no endpoint lists the names, so the web UI only surfaces the
panel for overview-card names it happens to have. This endpoint closes the
discovery gap, not a fetch gap.

### DESIGN-602: `ApiClient` — full sync parsing, POSTs, error detail

- **Types**: extend the sync response types to the full shape the server
  already sends (hosts[] with source/age_seconds/stale/error and verdicts[]
  with repo/verdict/reason/branch/ahead/behind/dirty/is_kb; top-level
  proposals[]). No server change — the data was always there, the client
  ignored it.
- **`ApiError`** (review 1's real finding): non-ok responses currently
  throw a bare `HTTP ${status}`. New: read the body, extract FastAPI's
  `detail`, throw `ApiError {status, detail}`. This is load-bearing for
  409: the server maps BOTH `SpecRunnerConfigConflictError` ("reload
  required" is right) AND `SpecRunnerConfigBusyError` ("reload" would be
  misinformation) to 409 — only `detail` distinguishes them over HTTP (the
  TUI branches on exception type; the extension cannot). The UI shows
  `detail`, never a hardcoded string per status. Same mechanism fixes 422
  (validation message surfaces verbatim).
- **POSTs**: `pull(dir)`, `createPr(dir)`, `updateSpecRunnerConfig(body)` —
  all carry `X-Action-Token`; `track(dir, action)` posts WITHOUT a token
  (parity with the web and the route's actual contract). Token strategy
  (review 2): fetched once from `/api/actions/session`, cached on the
  client; on 403, re-fetch EXACTLY once and retry; a second 403 fails. Unit
  tests pin both branches: 403→refetch→success and 403→refetch→403→error
  (the client's only stateful logic — review 1).
- **Timeouts**: GETs keep 3s (if `/api/sync` ever produces false timeouts,
  it may get its own 5-10s — noted as tolerance, not built now); action
  POSTs get a separate ~130s timeout (server-side subprocess cap is 120s).
- **`specRunnerConfigs()`** with graceful degradation (review 2): a 404
  from an older server → the config command shows "server does not support
  the config editor (upgrade dispatcher)" and does nothing else; all other
  views are unaffected (the established errors/roadmap degradation
  pattern).

### DESIGN-603: Sync tree view

Fourth view `dispatcherSync`: host nodes (label `host (source)`,
description age + `stale`, error hosts render the error) → verdict leaves
(icon by verdict, description `↑a/↓b ✎`, tooltip reason) → proposal leaves
at the end («обнаружен … ?» analogue in the ext's English strings:
`proposal`, description `track / ignore`). Context menus per §1.2's finite
`contextValue`s:

- `dispatcher.pull` / `dispatcher.openPr` → `withProgress` notification →
  `ApiClient.pull/createPr` → on ok: info toast (`detail`/`pr_url`, with an
  "Open PR" button → `vscode.env.openExternal` when a URL is present); on
  `ok=false`: error toast with the outcome's `error`; on `ApiError`: its
  `detail`. Then an immediate poll (the server invalidates its sync cache
  after successful actions — verified in review: app.py:269, 289).
- `dispatcher.track` / `dispatcher.ignore` → `POST /api/sync/track` → poll.

### DESIGN-604: Config editor — QuickPick flow

Command `dispatcher.editSpecRunnerConfig` (palette + Sync view title menu).

**All flow STATE lives in `configFlow.ts`** (review 1): a small state
object (the fetched config, the edits map, derived diff) with pure
functions — `fieldItems(state)`, `applyEdit(state, field, raw)` (validates
+ coerces), `diffLines(state)`, `requestBody(state)`. The command layer is
a thin driver loop; closing the diff document re-opens the field QuickPick
from the SAME state (the flow's main bug magnet — pinned by vitest on the
state module, not by UI tests).

- Config QuickPick: label dir, description project, detail explicit-count +
  extra marker (from DESIGN-601's list).
- Field loop QuickPick: 12 items (label field, description current value +
  `explicit`/`default`/`edited` marker) + separator + `Preview diff` +
  `Confirm → PR`.
- InputBox with `validateInput`: the TUI's strict rules verbatim — bool
  accepts ONLY true/false (case-insensitive), int must parse, str passes
  through; invalid input never leaves the InputBox.
- **Preview diff**: `- field: old` / `+ field: new` for EDITED fields only,
  `(no changes)` when empty — opened as a read-only `language: "diff"` doc
  (the `showError` pattern). Honesty note (review 2): the preview shows
  the edited-field deltas, which is exactly what the request changes; the
  server-side PR additionally re-emits already-explicit keys per the
  explicit-or-changed rule — the preview must not claim to be the full PR
  diff (one caption line in the doc: "PR diff may include already-explicit
  keys unchanged").
- **Confirm** → `updateSpecRunnerConfig({dir, typed: <all 12 coerced>,
  extra_executor_config: null, base_mtime})` (`base_mtime` from the
  DESIGN-601 list fetch at flow start; the server's mtime conflict guard
  covers staleness). Outcome handling ORDER (review 1): HTTP 200 with
  `ok=false` is NOT an error branch until no-op is checked —
  `detail=="no-op"` → benign info "config already in this state — no PR
  needed"; then `ok=true` → info + Open PR button; then `ok=false` → error
  with the outcome's `error`. `ApiError` → its `detail` (409 busy vs
  conflict distinguished by the server's own message, 422 validation text
  verbatim).

### DESIGN-605: Parity guard

`model.ts` gains `syncItemContext(verdictRow, live): string | null`
returning one of the three finite `contextValue`s (or null for
non-actionable). vitest mirrors the TUI's parity test: live+pull-first →
`.pull`; +ahead>0 → `.pullPr`; ahead 0/None, non-live, ok-verdict, kb-host
→ exactly the web/TUI refusals.

### DESIGN-606: Testing

- pytest: DESIGN-601 endpoint (list across roots; includes non-overview
  projects; basename duplicates listed twice).
- vitest: `ApiError` detail extraction; token cache 403 branches; full
  sync-shape parsing; `syncItemContext` parity; `configFlow` state machine
  (edit → re-enter → diff → body: all 12 typed coerced, extra null,
  base_mtime passthrough; invalid inputs rejected at `validateInput`
  level); graceful 404 degradation of `specRunnerConfigs()`.
- The extension's build gate (`tsc` + esbuild + vitest) stays in CI as
  today.

### DESIGN-607: Contributions + docs

`package.json`: the `dispatcherSync` view, five commands
(`pull`/`openPr`/`track`/`ignore`/`editSpecRunnerConfig`), menus keyed on
the finite `contextValue`s. README's VSCode section: actions + editor.
FR-06 status: **closed fully** (TUI + VSCode) — recorded in this spec, the
TUI spec's context note, and the discovery brief's FR-06 line gets a
resolution pointer.

## 3. Error handling

| failure | behaviour |
|---|---|
| non-ok HTTP anywhere | `ApiError` with the body's `detail`; UI shows `detail` (409 busy vs conflict distinguished by server message, never a hardcoded "reload required") |
| 403 on an action POST | token re-fetch exactly once, retry; second 403 → error toast |
| old server without DESIGN-601 (404) | config command: "server does not support the config editor"; other views unaffected |
| HTTP 200, `ok=false`, `detail=="no-op"` | benign info (checked BEFORE the generic not-ok branch) |
| HTTP 200, `ok=false`, other | error toast with the outcome's `error` |
| action timeout (~130s client cap) | error toast; server's own 120s cap usually fires first |
| server offline mid-flow | existing degradation: views blank to their offline states, `ensureRunning` kicks in |

## 4. Out of scope

- Webviews (decided against — QuickPick flow), editing
  `extra_executor_config`, AI suggestions (DESIGN-307).
- Any backend change beyond DESIGN-601 (tri-state `null` already shipped).
- Multi-root basename disambiguation (documented fail-closed contract).

## 5. Traceability

| Item | Design |
|---|---|
| FR-06 J-03 "act" half in the IDE | DESIGN-602, 603 |
| Config parity (DESIGN-308's IDE sibling) | DESIGN-601, 604 |
| Finite contextValues (review 2) | §1.2, DESIGN-603, 605 |
| 409 detail branching + ApiError (review 1, the real finding) | DESIGN-602, §3 |
| Token cache single-retry (review 2) | DESIGN-602, DESIGN-606 |
| Graceful old-server degradation (review 2) | DESIGN-602, §3 |
| Flow state in configFlow.ts, not a closure (review 1) | DESIGN-604, 606 |
| Preview-vs-PR-diff honesty (review 2) | DESIGN-604 |
| Basename-keyed contract documented (review 1) | DESIGN-601 |
| Tri-state null — verified shipped, no backend change | context note, §4 |

## 6. Milestone

Single PR series: DESIGN-601..607 together. After merge, FR-06 is closed
fully; the discovery brief's Should-item joins FR-04/FR-05 as the remaining
open Should scope.
