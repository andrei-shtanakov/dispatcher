# Design — FR-05: MCP server over the dispatcher read API

> **Context (2026-07-18):** FR-05 ("MCP-сервер поверх dispatcher API", Should,
> traces G-01): the read endpoints become MCP tools so robin/Maestro/agent
> sessions get ecosystem state without crawling artifacts themselves.
> Ecosystem precedent: maestro ships a FastMCP server that calls its own
> core directly (`maestro/coordination/mcp_server.py` imports
> `maestro.database`, no HTTP hop — verified by reading the file); arbiter
> ships a Rust `arbiter-mcp` binary. Numbering: DESIGN-701+. Two pre-write
> reviews folded in below.

## 1. Principles

1. **`dispatcher mcp` — a stdio subcommand** with `--config` exactly like
   `serve`/`tui`/`publish-snapshot` (review 1): clients register
   `uv run dispatcher mcp --config /path/dispatcher.toml`. No HTTP/SSE
   transport in v1; the MCP process is independent of the dashboard server.
2. **One read facade, two consumers** (review 1): endpoint bodies move into
   `dispatcher/core/read_api.py` — functions like `overview(...)`,
   `project(...)`, `roadmap_summary(...)` returning the existing response
   models. `server/app.py` routes and MCP tools both call the facade, so
   parity is by construction, not by hoping two copies stay in sync.
3. **Read-only is an invariant with teeth**: no action tools — NFR-01/X-02
   demand an explicit HUMAN click; an MCP tool call is an agent action. A
   test pins the EXACT tool-name set (review 1: whitelist equality, not
   "no pull"), so a future action tool cannot leak in silently. And the MCP
   path must not even fetch: `sync_status` calls
   `SyncService.get(start_fetch=False)` — the background `git fetch
   --prune` mutates remote refs and hits the network, which is not
   read-only in the strict sense an agent surface requires (review 1;
   signature verified: `sync_service.py:100`).
4. **The contract is JSON, not objects** (review 1): tools return
   `model_dump(mode="json")` dicts/lists; the parity tests compare the
   tool result's JSON against `http_response.json()` on the same
   workspace. v1 tool schemas are explicitly UNSTABLE (review 2): no
   frozen contract yet; if robin/Maestro adopt these tools, promoting the
   schemas to a frozen vendored contract (ADR-ECO-003 style) is a
   follow-up decision, recorded here so it isn't forgotten.

## 2. Tool surface — 14 tools, curation stated explicitly

All read GETs are exposed except two, and each exception is a decision,
not an accident (review 2 flagged the earlier 11-tool cut as
under-justified against FR-05's literal "read-эндпоинты доступны"):

| Tool | Backing | Notes |
|---|---|---|
| `overview()` | `/api/overview` | |
| `project(name)` | `/api/projects/{name}` | lookup — error semantics §4 |
| `errors(limit, days, project, service)` | `/api/errors` | same defaults as HTTP |
| `models()` | `/api/models` | gains a response model, §3 |
| `contracts()` | `/api/contracts` | |
| `work_items(cross_only, limit)` | `/api/work-items` | |
| `roadmap()` | `/api/roadmap` | |
| `roadmap_item(id)` | `/api/roadmap/{item_id}` | lookup — §4 |
| `roadmap_summary()` | `/api/roadmap/summary` | |
| `roadmap_drift()` | `/api/roadmap/drift` | RESTORED (review 2): `build_drift`'s contract join is canonical and test-covered — an agent must not re-derive it |
| `roadmap_phases()` | `/api/roadmap/phases` | restored, same reasoning |
| `roadmap_blockers()` | `/api/roadmap/blockers` | restored |
| `sync_status()` | `/api/sync` + `/api/sync/hosts` | merged: one tool returns the full `SyncStatus` (hosts and proposals are inside `report`); the hosts endpoint adds no data the status lacks. Parity caveat (review 2): this tool's parity test compares against the COMPOSITION of the two HTTP responses, not one — recorded here so the test doesn't pretend otherwise. `start_fetch=False` per §1.3 |
| `spec_runner_configs()` | `/api/spec-runner-configs` | |

**Curated out (explicit decisions):**
- `/api/spec-runner-config/{name}` (per-name): the list tool returns full
  entries; per-name is a trivial client-side filter with no server-side
  join — unlike drift, nothing canonical would be re-derived.
- `/api/actions/session`: CSRF token for the write path — meaningless and
  misleading on a read-only surface.

## 3. Components

### DESIGN-701: Read facade (`dispatcher/core/read_api.py`)

Extract each read endpoint's body into a facade function (the bodies are
already thin: `cache.get()` + a builder call — this is a move, not a
rewrite). The facade owns the service instances' USE, not their lifetime:
functions take the services/config as parameters so both consumers keep
their own instances (`create_app`'s per-app services; the MCP process's
own). `server/app.py` routes become one-line delegations; behavior and
response models unchanged (full pytest suite is the guard).

### DESIGN-702: `/api/models` gets a response model

`models()` currently returns `list[dict[str, Any]]` — the only read
endpoint without a pydantic model, which would silently break the "same
models on both surfaces" claim (review 1). Add `ModelUsageRow`
(`project: str` + `ModelInUse`'s fields) in `core/models.py`, use it as
the HTTP `response_model` AND the facade's return type. Serialization is
identical to today's dict (same keys) — non-breaking, pinned by the
existing API test.

### DESIGN-703: MCP server module (`dispatcher/mcp_server.py`)

FastMCP instance + 14 thin tools, each delegating to the facade and
returning `model_dump(mode="json")`. **Every tool and parameter carries a
docstring/description written for an agent** — this is the tool-selection
prompt surface, a hard requirement, not a nicety (review 2): what the tool
answers, when to prefer it over siblings (e.g. `roadmap_summary` vs
`roadmap`), parameter meanings and defaults.

New dependency: `fastmcp` (same major as maestro's `fastmcp>=2.14.5`),
pinned with a floor in pyproject.

### DESIGN-704: Error semantics for lookup tools

HTTP 404 has no MCP equivalent (review 2). The three lookup-shaped tools
(`project`, `roadmap_item`; plus any future one) raise FastMCP's
`ToolError` with EXACTLY the HTTP detail text (`unknown project: {name}`,
`unknown roadmap item: {item_id}`) — the agent sees an isError result with
the same message an HTTP client gets in `detail`. Non-lookup tools let
unexpected exceptions propagate as FastMCP's generic tool failure
(collectors already degrade to warnings inside snapshots, so this path is
rare by construction).

### DESIGN-705: CLI wiring

`dispatcher mcp [--config PATH]` subparser in `cli.py`, registered next to
`serve`/`tui` (review 2 asked for this to be named explicitly): loads the
config exactly like `serve`, builds the services, calls
`mcp_server.build_server(config).run()` (FastMCP's default run = stdio).

### DESIGN-706: Testing

- **Exact tool-set test**: `client.list_tools()` names == the 14-name
  whitelist, equality both ways (review 1's "future action can't leak").
- **Parity tests** on a real workspace fixture (the established
  `make_atp`/`make_arbiter`/... conftest builders): for each tool, the
  tool result's JSON equals the corresponding HTTP response's `.json()`
  via `httpx.ASGITransport` against the same config — `sync_status`
  compares against the documented two-endpoint composition; both surfaces
  must call with equivalent parameters (defaults for the parametrized
  ones, plus one non-default `errors(...)` case).
- **Lookup errors**: `project("no-such")` → isError with the exact detail
  text.
- **No-fetch pin**: `sync_status` invoked through the in-memory client
  never triggers the fetch path (assert via the service's
  `fetch_in_flight`/injected fetcher spy — the test must fail if someone
  drops `start_fetch=False`).
- In-memory `fastmcp.Client(server)` throughout — no subprocess, no stdio
  plumbing in tests.

### DESIGN-707: Documentation

README: new "MCP server" section (registration one-liner for Claude Code /
robin, tool list, read-only statement). COWORK_CONTEXT: interfaces line.
`spec/discovery-brief-customer.md`: FR-05 resolution pointer after merge.

## 4. Error handling

| failure | behaviour |
|---|---|
| unknown `name`/`id` in lookup tools | `ToolError` with the HTTP-identical detail text (§DESIGN-704) |
| collector/source failures | already degrade into `snapshot.warnings` inside the payload — the tool returns normally, the agent sees the warnings field |
| config file missing/invalid | CLI exits with the same error `serve` gives — before the MCP loop starts |
| sync fetch side effects | structurally impossible: `start_fetch=False` + pinned by test |

## 5. Out of scope

- HTTP/SSE transport; MCP resources/prompts (tools only in v1).
- Action tools of any kind (invariant, §1.3).
- Freezing the tool schemas as a vendored contract — explicitly deferred
  until a real consumer (robin/Maestro) adopts them (§1.4).
- Per-name config tool and the CSRF session endpoint (curated out, §2).

## 6. Traceability

| Item | Design |
|---|---|
| FR-05 acceptance (read endpoints as tools) | §2 — 14 tools + two explicit curations |
| Review 1: `--config` | DESIGN-705 |
| Review 1: no background fetch from MCP | §1.3, DESIGN-706 no-fetch pin |
| Review 1: shared read facade | DESIGN-701 |
| Review 1: JSON-level parity, `model_dump(mode="json")` | §1.4, DESIGN-706 |
| Review 1: `/api/models` model gap | DESIGN-702 |
| Review 1: exact tool-set whitelist test | DESIGN-706 |
| Review 2: restore drift/phases/blockers | §2 (restored, reasoning in-table) |
| Review 2: lookup error semantics | DESIGN-704 |
| Review 2: tool descriptions as contract | DESIGN-703 |
| Review 2: sync_status parity caveat | §2 sync_status row, DESIGN-706 |
| Review 2: fastmcp dependency named+pinned | DESIGN-703 |
| Review 2: CLI wiring named | DESIGN-705 |
| Review 2: contract-freeze question answered | §1.4, §5 |
| Review 2: maestro precedent grounded | context note (file cited, verified by reading) |

## 7. Milestone

Single PR series: DESIGN-701..707 together (the facade refactor and the
MCP consumer must land atomically — a facade with one consumer is churn,
an MCP server without the facade is the duplication review 1 vetoed).
