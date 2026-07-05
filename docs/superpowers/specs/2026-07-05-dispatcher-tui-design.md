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
- `recent_errors(events, days, now=None)` — freshness filter shared by
  API and TUI (same 19-char ISO-prefix comparison).

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
- **Models** — DataTable: project, model_id, vendor, role, status,
  source.
- **Contracts** — DataTable: name, in_sync (`✓` / `✗` / `?` when
  `None`), detail.

## 3. Data flow and refresh

Collectors are synchronous (disk + SQLite), so collection runs in a
textual worker with `thread=True`; the UI never blocks. `set_interval`
(10 s) triggers the worker; the service's 5 s TTL cache absorbs
overlapping calls. `r` triggers an immediate refresh. The footer shows
the last collect time and the number of warnings.

## 4. Error handling

Three layers; the first two already exist:

1. Collectors degrade into `snapshot.warnings` (never raise).
2. `SnapshotService` guards against collector crashes
   (`collector crashed: …` warning snapshot).
3. New: an exception inside the TUI worker shows a toast
   (`App.notify`) and keeps the last successful data on screen.

## 5. CLI and dependencies

- New subcommand: `dispatcher tui [--config path]` (no port).
- `uv add textual` (regular dependency).

## 6. Testing

- Core refactoring: direct `SnapshotService` tests on the existing
  fixture mini-trees; server tests pass unchanged.
- TUI: `App.run_test()` + Pilot (async via anyio): Projects table
  populates from fixtures, Enter pushes the detail screen, tab
  switching, Errors filters, `r` binding triggers a refresh.

## Out of scope

- Editing (strictly view-only), historical trends, live push
  (WebSocket/SSE) — same exclusions as Stage 1.
