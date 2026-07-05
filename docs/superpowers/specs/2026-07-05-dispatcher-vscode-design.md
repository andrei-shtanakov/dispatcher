# Dispatcher Stage 3: VSCode Extension — Design

Date: 2026-07-05
Status: approved
Depends on: `2026-07-03-dispatcher-design.md` (Stage 1, shipped),
`2026-07-05-dispatcher-tui-design.md` (Stage 2, shipped)

## Goal

A native VSCode extension surfacing dispatcher's ecosystem monitoring
inside the editor: a sidebar (projects + recent errors) and a status-bar
health indicator, fed by the Stage 1 HTTP API. Glance-and-keep-coding is
the primary use case — no editor-area real estate.

## Decisions

- Form factor: activity-bar view container with two TreeViews
  (Projects, Errors) + one StatusBarItem. No webview.
- Data: HTTP API only (`/api/overview`, `/api/projects/{name}`,
  `/api/errors?days=14&limit=50`). The extension never reads project
  disks itself.
- Server lifecycle: the extension probes the configured URL and, when
  unreachable and `autoStart` is enabled and `projectDir` is set,
  spawns `uv run dispatcher serve --port N` itself; it kills on
  deactivate only a process it spawned.
- v1 scope: projects + errors. Models/contracts trees, error filters,
  and any webview are explicitly out of scope for this iteration.
- Location: `vscode-ext/` subdirectory of the dispatcher repo.
  TypeScript strict, esbuild bundle, vitest unit tests, no UI
  frameworks. Distribution: `vsce package` → install from VSIX (no
  Marketplace).

### Approaches considered

- **A (chosen): thin native extension with a central 10 s poller** —
  matches web/TUI refresh cadence; the server's 5 s snapshot cache makes
  polling cheap; the status bar is only meaningful with fresh data.
- B: on-demand refresh only — simpler but the status-bar health reading
  goes stale; diverges from the other frontends. Rejected.
- C: webview embedding the existing dashboard — rejected with the form
  factor decision (a browser inside the IDE adds no native value).

## 1. Structure

```
vscode-ext/
  package.json        # manifest: views, commands, settings, scripts
  tsconfig.json       # strict
  esbuild.mjs         # bundle to dist/extension.js
  src/extension.ts    # activate/deactivate wiring only
  src/api.ts          # ApiClient + DTO types
  src/server.ts       # ServerManager (probe/spawn/kill)
  src/tree.ts         # ProjectsProvider, ErrorsProvider + pure mappers
  src/status.ts       # status-bar text/state (pure) + item wiring
  test/*.test.ts      # vitest over the pure functions
  test/fixtures/*.json# real API responses, checked in
```

Each `src/` module has one responsibility; VSCode API usage concentrates
in `extension.ts`/provider classes, while all decision logic (labels,
icons, spawn-or-not, port-from-URL) lives in exported pure functions.

## 2. Components

- **ApiClient** (`api.ts`): `fetch` wrapper with a request timeout;
  methods `overview()`, `project(name)`, `errors()`. DTO interfaces
  mirror only the pydantic fields the UI uses (`OverviewEntry`:
  name/detected/freshness/counts/warnings; `ErrorEvent`:
  timestamp/service/body; `ProjectSnapshot` detail subset:
  schema_versions/tasks/test_results/models/configs lengths + warnings).
- **ServerManager** (`server.ts`): `ensureRunning()` — GET
  `/api/overview` with a short timeout; on failure, if
  `dispatcher.autoStart` and `dispatcher.projectDir` are set, spawn
  `uv run dispatcher serve --port <port-from-url>` with
  `cwd=projectDir`, then poll readiness up to ~10 s. One spawn attempt
  per offline episode (no restart loops). `deactivate()` kills the
  child only if this session spawned it. Spawn stderr surfaces in an
  error notification.
- **ProjectsProvider** (`tree.ts`): top level — one item per project:
  icon green (no errors) / red (errors > 0) / dim outline (not
  detected); description like `12t · 3e · 2h ago` (task/error counts +
  freshness). Expanding a detected project lazily fetches
  `/api/projects/{name}` and shows children: counts line, one line per
  schema-version check (`ok`/`DRIFT`/`unknown`), one line per warning.
  Undetected projects are not expandable.
- **ErrorsProvider** (`tree.ts`): flat list of the last errors (14 days,
  limit 50, newest first): label `HH:MM service — message` (truncated),
  tooltip = full body, click opens the full message (read-only
  document). Empty state: single `no errors 🎉` item.
- **Status bar** (`status.ts`): `$(pulse) disp: 4✓ 1✗` — detected
  project count and projects-with-errors count; click focuses the
  sidebar. Offline: `$(debug-disconnected) disp: offline`.

## 3. Data flow

One central poller in `extension.ts`: every `dispatcher.pollSeconds`
(default 10) fetch overview + errors, then fire an event both providers
and the status bar subscribe to. Project detail is fetched lazily on
node expansion, not polled. Command `dispatcher.refresh` triggers an
immediate poll. The extension keeps no cache: offline replaces the view
with an explicit unreachable state (§4), never silently stale data.

## 4. Error handling and offline

- Fetch failure → status bar switches to offline; each tree shows a
  single `server unreachable` node with a `Start server` action; last
  successful data is discarded from view (offline is explicit, not
  silently stale).
- `autoStart` path: one spawn attempt per offline episode (an episode
  ends when a poll succeeds); after the attempt, probing simply
  continues at poll cadence — no restart loops. Spawn failure →
  notification including a stderr tail.
- The extension is read-only toward observed projects by construction:
  it only speaks HTTP to dispatcher and spawns dispatcher itself.

## 5. Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `dispatcher.url` | `http://127.0.0.1:8787` | API base URL |
| `dispatcher.projectDir` | `""` | dispatcher repo path for spawning; empty disables autoStart |
| `dispatcher.autoStart` | `true` | spawn server when unreachable (needs projectDir) |
| `dispatcher.pollSeconds` | `10` (min 5) | poll interval |

## 6. Testing

- vitest unit tests over the pure functions with checked-in fixture
  JSONs captured from the real endpoints: overview→tree-item mapping
  (labels, icons, descriptions), errors mapping + truncation, status-bar
  text (online/offline/counts), port-from-URL parsing, spawn-decision
  logic (url reachable / autoStart off / projectDir empty).
- No automated VSCode-host integration tests in v1; manual smoke
  checklist instead: panel renders, project expands, error click opens
  body, offline state + Start server, autoStart spawn+kill, status-bar
  click focuses view.
- CI: a Node job next to the existing Python job — `npm ci`,
  `tsc --noEmit`, `vitest run`, esbuild build.

## 7. Build and install

`npm run package` → `vsce package` → `.vsix`; install via VSCode
"Install from VSIX". No Marketplace publishing.

## Out of scope (v1)

- Models and contracts trees, error filters (project/service/days
  toggle), webview dashboard, Marketplace publishing, multi-server
  support.
