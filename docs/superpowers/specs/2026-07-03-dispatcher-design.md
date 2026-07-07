# Dispatcher — Design Spec (2026-07-03)

## Purpose

Dispatcher is a read-only monitoring and control dashboard for the ecosystem
projects living in `~/labs/all_ai_orchestrators/`: atp-platform, Maestro,
arbiter, spec-runner, proctor. The project list is extensible; more projects
may be added later.

It reads on-disk artifacts (SQLite databases, TOML/YAML configs, OTel JSONL
logs) directly. It never launches the monitored projects and never requires
them to be running or even installed: a missing project or file is a normal
condition, not an error.

Delivery is staged:

1. **Stage 1 (this spec):** local HTTP server with an HTML dashboard.
2. **Stage 2:** TUI (textual) reusing the same core library.
3. **Stage 3:** VSCode extension consuming the same HTTP API.

Stage 1 is view-only. Editing capabilities may come later; the design must not
block them (the API layer is where mutations would be added).

## Decisions taken (revisit if needed)

- **HTML version = local FastAPI server** with a polling single-page dashboard
  (user-confirmed). A static snapshot generator was rejected.
- **Dispatcher is a shippable ecosystem project**, not a dev-tool in
  `_cowork_output/devtools/` (default chosen while user was away). Follow-ups
  outside this spec: register it in the ecosystem registry
  (COWORK_CONTEXT.md) and set up CI, same as spec-runner-vscode.
- **Discovery = config-first + auto-detect** (default chosen while user was
  away): `dispatcher.toml` lists root directories; collectors auto-detect
  their project inside those roots by signature files. When no config exists,
  dispatcher falls back to its own parent directory — a monorepo-layout
  convenience only; standalone installs must list roots explicitly.
- **No reading of `_cowork_output/`** — monorepo rule: that directory is
  dev-only and absent on user installs. All data comes from in-repo sources
  (`method/`, `config/`, `schemas/`, `spec/`, `logs/`, vendored contract
  copies).
- **Direct file reads, no aggregation DB, no per-project APIs** — volumes are
  small; read at request time. Using the projects' own servers (Maestro
  dashboard, arbiter MCP) was rejected because it requires running services.

## Architecture

Three layers; one core shared by all three frontends.

```
dispatcher/
├── pyproject.toml
├── dispatcher.toml          # user config (roots, port); optional
├── dispatcher/
│   ├── core/
│   │   ├── models.py        # pydantic models (normalized views)
│   │   ├── discovery.py     # roots from config → detected projects
│   │   └── collectors/
│   │       ├── base.py      # Collector protocol
│   │       ├── atp.py
│   │       ├── maestro.py
│   │       ├── arbiter.py
│   │       ├── spec_runner.py
│   │       └── proctor.py
│   ├── server/
│   │   ├── app.py           # FastAPI: JSON API + static
│   │   └── static/          # index.html + vanilla JS (no build step)
│   └── cli.py               # `dispatcher serve [--port 8787]`
└── tests/
    ├── fixtures/            # miniature fake project trees
    └── ...
```

### Collector protocol (`core/collectors/base.py`)

```python
class Collector(Protocol):
    name: str                                  # "arbiter", "maestro", ...
    def detect(self, path: Path) -> bool: ...  # is this directory my project?
    def collect(self, path: Path) -> ProjectSnapshot: ...
```

- `detect` checks signature files (e.g. arbiter: `config/agents.toml` +
  `arbiter-core/`; spec-runner: `src/spec_runner/`; Maestro: `maestro/`
  package; atp-platform: `atp/` + `method/agents-catalog.toml`; proctor:
  `config/proctor.yaml`).
- `collect` must never raise for bad/missing data: it returns a partial
  `ProjectSnapshot` with `warnings: list[str]`.
- Adding a future project = adding one collector module; core and server do
  not change.

### Normalized models (`core/models.py`, pydantic)

- `ProjectSnapshot`: name, path, detected, collected_at, freshness (newest
  source mtime), `models: list[ModelInUse]`, `tasks: list[TaskInfo]`,
  `test_results: list[TestRunSummary]`, `configs: list[ConfigSummary]`,
  `contracts: list[ContractStatus]`, `errors: list[ErrorEvent]`,
  `warnings: list[str]`.
- `ModelInUse`: model id, vendor/harness, role (default/fallback/routable),
  source file.
- `TaskInfo`: id, title/status, started/finished, cost/tokens if available,
  source.
- `TestRunSummary`: suite/run id, passed/failed/total or score, timestamp,
  source.
- `ContractStatus`: contract name, canonical path, vendored path(s),
  in_sync: bool (hash comparison), last_modified.
- `ErrorEvent`: timestamp, service, severity, body, pipeline_id, source file.
- `ConfigSummary`: path, format, key facts (selected fields only), secrets
  masked at two levels: by key name (token/key/secret/password → `***`) and
  by value pattern — credentials inside URLs (`scheme://user:pass@host`),
  bearer/API-token-shaped strings. Masking runs in the collector, before
  data reaches the API layer.

### Data sources per requirement

| Requirement | Source |
|---|---|
| ATP test results | `.atp-dashboard.db` (benchmark_runs, test_executions, evaluation_results), `results/experiment/experiment_results.json`, `_bench_output/*.db` |
| Models in Maestro / arbiter / spec-runner | SSOT `atp-platform/method/agents-catalog.toml`; `arbiter/config/agents.toml` + vendored `config/agents-catalog.toml`; Maestro spawner defaults (`maestro/spawners/*.py` constants) and `$ATP_CATALOG`; `proctor/config/proctor.yaml → llm` |
| Current tasks | `spec-runner/spec/.executor-state.db` (tasks, attempts); `proctor/data/state.db` (tasks, schedules); `arbiter/arbiter.db` (decisions, benchmark_runs); Maestro `~/.maestro/maestro.db` (tasks, task_costs, workstreams — CLI default path, `maestro/cli.py`; overridable in `dispatcher.toml`) + `~/.maestro/maestro.pid` for running/not-running status |
| Configurations | `executor.config.yaml`, `atp.config.yaml`, `config/*.toml`, `config/proctor.yaml` — displayed as summaries with secrets masked |
| Contracts | `spec-runner/schemas/*.json`, `atp-platform/method/contract/`, catalog drift check: canon `method/agents-catalog.toml` vs an **explicit whitelist of vendored copies** (currently `arbiter/config/agents-catalog.toml`), compared by content hash. Never search by filename — test fixtures like `Maestro/tests/fixtures/agents-catalog.toml` must not trigger false drift |
| Failures and errors | OTel JSONL `<project>/logs/<ULID>/<service>-<pid>.jsonl` filtered to `SeverityNumber >= 17` (ERROR), merged across projects, newest first; plus spec-runner `attempts.error_kind/error_stage` |

### SQLite access rules

- Open read-only, WAL-aware: `sqlite3.connect("file:...?mode=ro", uri=True)`
  with a short `busy_timeout` and one retry on `database is locked` /
  transient read errors — several source DBs are actively written (WAL
  sidecars present). `immutable=1` is forbidden: it asserts the file never
  changes and yields incorrect reads on live WAL databases.
- **Version-gate every DB read.** `arbiter.db` carries `schema_version`,
  `.atp-dashboard.db` carries `alembic_version`, spec-runner state has
  `executor_meta`. Collectors read the version first, compare against the
  version they were written for, and surface it in the snapshot/UI
  ("arbiter schema v7 ✓" / "⚠ v8 — data may be incomplete"). Owner-side
  schema migrations then degrade visibly instead of silently.
- Never create or migrate; if schema is unexpected, emit a warning and return
  partial data.
- 0-byte or missing DBs → warning, empty section.

Known limitation (future direction): reading other projects' private DB
schemas is implicit coupling. The version gate makes drift visible but does
not remove the coupling. The proper long-term fix is a small stable
read-model per owner project (a documented view or exported `status.json`),
vendored as a pinned contract per ADR-ECO-003 practice. That is a cross-repo
change and out of scope for Stage 1.

### HTTP API (`server/app.py`)

- `GET /api/overview` — all `ProjectSnapshot`s (light: counts + freshness).
- `GET /api/projects/{name}` — full snapshot for one project.
- `GET /api/errors?limit=100` — merged cross-project error feed.
- `GET /api/models` — merged model usage + catalog drift status.
- `GET /api/contracts` — contract sync status.
- `GET /` — static dashboard page.

Responses are pydantic-serialized; the schemas double as the public contract
for the future VSCode extension. API errors from bad project data are
impossible by construction (collectors degrade to warnings); 500s indicate
dispatcher bugs only.

### HTML dashboard (`server/static/`)

One `index.html` + vanilla JS, no build step, no npm. Cards per project
(status, freshness, warnings), sections for tasks / models / test results /
contracts, a cross-project error feed. Polls `/api/overview` every ~10 s;
detail views fetch on demand. Missing projects render as a dim "not detected"
row.

## Error handling

- Collector-level: every file read is individually guarded; failures become
  `warnings` entries on the snapshot.
- Discovery-level: unreadable root → warning in overview.
- Server-level: catch-all handler logs and returns 500 only for genuine bugs.

## Testing

- `tests/fixtures/` holds miniature fake project trees: happy path (small
  populated SQLite + JSONL), empty DBs, missing files, corrupt JSONL, absent
  project, unexpected schema version, and a concurrently-written DB (writer
  thread holds a transaction while the collector reads — exercises the
  lock-retry path).
- Unit tests per collector against fixtures (pytest + anyio where async).
- API integration tests via httpx `ASGITransport` against a fixtures root.
- Regression rule: every degradation bug gets a fixture reproducing it.

## Out of scope (Stage 1)

- Any write/edit/control operations (pause, retry, config editing).
- TUI and VSCode frontends (stages 2–3; core/API designed for them).
- Live push (WebSocket/SSE) — polling is sufficient at this scale.
- Reading `_cowork_output/` anything.
- Historical trend storage — dispatcher keeps no state of its own.

## Tech stack

Python ≥3.12, uv-managed. Dependencies: fastapi, uvicorn, pydantic, pyyaml
(tomllib is stdlib). Dev: pytest, anyio, httpx, ruff, pyrefly.
