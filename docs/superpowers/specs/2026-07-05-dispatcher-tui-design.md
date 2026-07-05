# Dispatcher Stage 2: TUI — Design

Date: 2026-07-05
Status: approved
Depends on: `2026-07-03-dispatcher-design.md` (Stage 1, shipped)

## Goal

A terminal UI (textual) with full feature parity to the Stage 1 web
dashboard: overview, per-project detail, errors with filters, models,
contracts. Read-only, same invariants as Stage 1. The TUI consumes
`dispatcher.core` directly — no HTTP server required.

## Decisions

- Scope: full parity with the web dashboard.
- Data access: direct `dispatcher.core` reuse (approach A below).
- Refresh: background auto-refresh every 10 s + `r` key for immediate.
- Layout: top tabs `Projects | Errors | Models | Contracts`; Enter on a
  project row pushes a detail screen.
- `textual` is a regular dependency; entry point is `dispatcher tui`.
  An optional `[tui]` extra was considered and rejected: dispatcher is
  an end-user tool, the extra install weight is acceptable and one
  install path is simpler.

### Approaches considered

- **A (chosen): extract snapshot collection into core.** Server and TUI
  share one implementation; "reuse dispatcher.core" is real, not
  declarative.
- B: TUI duplicates the collect loop — duplicated invariants (crash
  guard, undetected projects), rejected.
- C: TUI consumes the HTTP API — needs a running server, contradicts the
  roadmap decision, overview endpoint is too thin for the detail screen.

## 1. Core refactoring (prerequisite)

Move from `server/app.py` into a new `dispatcher/core/service.py`:

- `SnapshotService(config)` — public class wrapping today's
  `_SnapshotCache`: 5 s TTL cache, per-collector last-resort guard,
  appends undetected projects.
  `get() -> tuple[list[ProjectSnapshot], list[str]]`.
  **Thread-safe**: `get()` holds an internal `threading.Lock`. The
  Stage 1 cache was written for a single-threaded server; the TUI calls
  it from `thread=True` workers where an auto-refresh tick and a manual
  `r` refresh can overlap. The lock serializes collection (the loser of
  the race is then served from the TTL cache instead of collecting
  twice).
- `recent_errors(events, days, now=None)` — freshness filter shared by
  API and TUI (same 19-char ISO-prefix comparison).
- `ERRORS_DAYS_DEFAULT = 14` — the freshness default, imported by the
  TUI. The web JS cannot import it and keeps its own copy in
  `index.html`; a parity test asserts the two values match (§6). The
  API `days` default stays `None` — the default is owned by frontends,
  not by the API.

`server/app.py` keeps only endpoint wiring and imports from core. API
behavior is unchanged; existing server tests must pass as-is.

## 2. TUI structure

Package `dispatcher/tui/`:

- `app.py` — `DispatcherApp` (textual `App`): `TabbedContent` with the
  four tabs, footer with key bindings, refresh orchestration.
- `detail.py` — `ProjectDetailScreen` (pushed via `push_screen`,
  Esc/q to go back).

### Screens

- **Projects** — DataTable: name, freshness, counts (tasks / models /
  tests / errors), warnings. Undetected projects render dimmed with "—".
  Enter opens the detail screen for the highlighted project.
- **Detail** — sections of one `ProjectSnapshot`: schema version checks
  (ok/drift), models, tasks, test runs, configs (already masked by
  collectors), errors, warnings.
- **Errors** — DataTable of errors merged across projects, newest first.
  Filters mirror the web UI: `Select` for project, `Select` for service,
  and a 14-days/show-all toggle bound to `a`. Service options are
  derived from the current snapshot data.
- **Models** — DataTable, same columns as the web table: project,
  model, harness, role, vendor, status (`—` for missing values;
  `source` is not shown, matching the web).
- **Contracts** — DataTable, same columns as the web table: name,
  canonical path, vendored path (`—` when empty), sync
  (`✓ in sync` / `✗ drift` / `detail` text when `in_sync` is `None`).

### Web-parity checklist

"Full parity" means these observable behaviors of
`server/static/index.html`, with their TUI equivalents:

| Web behavior | TUI equivalent |
| --- | --- |
| Project cards: counts tasks/models/tests/errors, errors highlighted when > 0 | Projects table columns; errors cell styled when > 0 |
| Freshness per card, "freshness unknown" fallback | Freshness column, same fallback |
| Undetected projects dimmed, "not detected", not clickable | Dimmed row with "—" counts; Enter disabled |
| Per-project warnings (⚠) on cards | Warnings column / marker on the row |
| Card click → detail + errors filtered to that project | Enter → detail screen; `e` on a project row opens Errors tab pre-filtered to it |
| Detail = full snapshot | Detail screen sections cover every `ProjectSnapshot` field |
| Errors: newest first, limit 50 | Same order and limit |
| Errors: default last 14 days, "show all" toggle | Same default (core constant), `a` toggle |
| Errors: project filter, clearable | Project `Select` with "all projects" option |
| Errors: service select = sorted union of seen services + "all services" | Same, from snapshot data |
| Errors: message truncated at 160 chars, click to expand | Truncated cell; Enter on the row shows the full message |
| Errors: "no errors 🎉" empty state, `—` for missing time/service | Same |
| Errors count `(N)` next to the header | Count in the tab title / table header |
| Models / Contracts columns and `—` fallbacks | As listed above |
| "updated HH:MM:SS" / "refresh failed: …" footer | Footer: last collect time, warnings count, refresh-failure toast |

## 3. Data flow and refresh

Collectors are synchronous (disk + SQLite), so collection runs in a
textual worker with `thread=True`; the UI event loop never blocks.
`set_interval` (10 s) triggers the worker; `r` triggers an immediate
refresh. The footer shows the last collect time and the number of
warnings.

Threading model, explicitly: worker threads call
`SnapshotService.get()`, which is lock-protected (§1). The 5 s TTL is
*shorter* than the 10 s tick, so every scheduled tick performs a full
disk + SQLite collect — identical to Stage 1, where the web UI polls
every 10 s against the same 5 s server cache. The TTL only absorbs
near-coincident calls (e.g. `r` pressed during or right after a tick);
it does not reduce the steady-state collect rate.

## 4. Error handling

Three layers; the first two already exist:

1. Collectors degrade into `snapshot.warnings` (never raise).
2. `SnapshotService` guards against collector crashes
   (`collector crashed: …` warning snapshot).
3. New: an exception inside the TUI worker shows a toast
   (`App.notify`) and keeps the last successful data on screen.

Known gap (accepted): `discover()` runs *outside* the per-collector
guard. It swallows `OSError` internally, but any other exception
propagates out of `get()` — layer 2 produces no warning snapshot for
it. In the TUI that surfaces as the layer-3 toast; in the server it
would be a 500. Unchanged from Stage 1 behavior.

## 5. CLI and dependencies

- New subcommand: `dispatcher tui [--config path]` (no port).
- `uv add textual` (regular dependency).

## 6. Testing

- Core refactoring: direct `SnapshotService` tests on the existing
  fixture mini-trees (including a concurrent-`get()` test exercising
  the lock); server tests pass unchanged.
- TUI: `App.run_test()` + Pilot (async via anyio): Projects table
  populates from fixtures, Enter pushes the detail screen, tab
  switching, Errors filters, `r` binding triggers a refresh.
- Parity guards:
  - a test asserting `index.html` contains the same errors-days default
    as `core.service.ERRORS_DAYS_DEFAULT` (the JS copy cannot import
    it);
  - the checklist in §2 is the review reference for the TUI screens —
    each row is either covered by a Pilot test or checked manually
    during review.

## Out of scope

- Editing (strictly view-only), historical trends, live push
  (WebSocket/SSE) — same exclusions as Stage 1.
