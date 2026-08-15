# ATP Benchmark View — Design

**Date:** 2026-08-15
**Status:** Approved (design review 2026-08-15)
**Plan item:** `@id:atp-benchmark-view` (acceptance of inbox #139 from
atp-platform, ADR-ECO-006; requested slug `atp-eco-benchmark-view` renamed at
acceptance)

## 1. Goal & context

atp-platform ships an API-only server profile (`ATP_SERVER_PROFILE=eco`,
atp-platform#287/#288) with no HTML UI; the decided display surface is
dispatcher. Dispatcher renders the benchmark list and per-benchmark
leaderboards of one configured eco server, read-only.

This is dispatcher's **first live-HTTP data source** — until now the runtime
is strictly on-disk reads + subprocess (`github-checker`, `git`). The design
therefore leans hard on two existing invariants:

- **NFR-02 (from `core/sync_service.py`): no network on render.** Every HTTP
  request happens in a background thread; the render path only reads a cached
  report.
- **Fail-closed classification (ARCH-C3 precedent):** anything the consumer
  cannot positively read is `unavailable`/`unreadable`, never an empty-but-ok
  answer.

Deployment model: the eco server URL is configurable; today it runs locally
next to dispatcher, a remote host is possible later. Phase 1 consumes only
the **public** surface — no tokens, no secrets anywhere in dispatcher.

## 2. Non-goals (phase 1)

- **No run status.** `GET /api/v1/runs/{id}/status` is token-gated and
  owner-scoped; consuming it means dispatcher's first stored secret and a
  run-id discovery story. Explicit phase 2, own TODO item when wanted.
- **No `atp-platform-sdk` dependency.** For the read-only path the SDK
  returns untyped dicts (`get_leaderboard() -> list[dict]`), its `*_sync`
  helpers assume an agent run loop, and it couples dispatcher to ATP's
  release cycle. We use `httpx` directly.
- **No rich public-leaderboard surface** (`/api/public/leaderboard/*`:
  categories, agent profiles, trends). Phase 1 is the benchmark surface only.
- **No on-disk export from ATP** (would need producer-side work; inbox #139
  says dispatcher-side only).
- **No TUI/VSCode/MCP parity.** Web panel only; parity is a follow-up item,
  mirroring the product-proposals precedent (#132/#133 → #138).
- **No mutations of any kind.** GET-only, per ADR-ECO-004 D1.

## 3. Configuration

`DispatcherConfig` (`dispatcher/core/discovery.py`) gains one field:

```python
benchmarks_url: str | None = None
```

`load_config` reads it from `dispatcher.toml`:

```toml
[benchmarks]
url = "http://127.0.0.1:8000"
```

- Must parse as `http://` or `https://`; trailing slash stripped. A value
  failing that check is a config error at load time, not a silent `None`.
- Absent section/key → `None` → the feature is off: no `BenchmarkService` is
  constructed, the web panel is hidden, and `GET /api/benchmarks` reports
  `status: "unconfigured"`.
- The URL may be displayed in the UI: by phase-1 contract it carries no
  secret (no tokens exist in this phase).

## 4. Data model & classification — `dispatcher/core/benchmarks.py`

Strict Pydantic models for exactly the two consumed responses. Model config:
`extra="ignore"` — the producer's contract blesses additive evolution, so
unknown fields must not break us; a missing or wrongly-typed **required**
field must (fail-closed → `unreadable`).

```python
class BenchmarkInfo(BaseModel):        # mirrors ATP BenchmarkResponse
    id: int
    name: str
    description: str
    tasks_count: int
    tags: list[str]
    version: str
    family_tag: str | None
    created_at: str                    # producer sends a string, keep it one

class LeaderboardRow(BaseModel):       # mirrors ATP benchmark LeaderboardEntry
    user_id: int
    agent_name: str
    best_score: float
    run_count: int
```

`LeaderboardRow` is deliberately our own name: atp-platform has **two**
different `LeaderboardEntry` classes (4-field benchmark one, 13-field public
one); we import neither name nor either ambiguity.

Report shape (the wire model of `GET /api/benchmarks`):

```python
BenchmarkStatusLiteral = Literal["unconfigured", "ok", "unavailable", "unreadable"]

class LeaderboardState(BaseModel):
    status: Literal["ok", "unavailable", "unreadable"]
    rows: list[LeaderboardRow]         # meaningful only when status == "ok"
                                       # (an ok leaderboard may be legitimately
                                       # empty); always [] otherwise
    error: str | None                  # one line, set iff status != "ok"

class BenchmarksReport(BaseModel):
    status: BenchmarkStatusLiteral
    url: str | None                    # the configured base URL (display)
    fetched_at: datetime | None        # see §5 — single time semantics
    error: str | None                  # one line; set on unavailable/unreadable,
                                       # EXCEPT the not-yet-fetched state (§5)
                                       # where it is null; always null on ok
    benchmarks: list[BenchmarkInfo]    # meaningful only when status == "ok";
                                       # always [] otherwise
    leaderboards: dict[int, LeaderboardState]  # keyed by benchmark id;
                                               # populated only when status == "ok"
```

Classification rules:

- `unconfigured` — `benchmarks_url is None`. Terminal; nothing else applies.
- `unavailable` — transport error, timeout, or non-2xx on
  `GET /api/v1/benchmarks`.
- `unreadable` — 2xx but the body fails validation. No partial rendering: a
  list where item 3 is garbage is `unreadable`, not "the two good ones".
- `ok` — the benchmark list parsed. Leaderboards then carry **per-benchmark**
  status with the same rules (`unavailable` on transport failure of that one
  GET, `unreadable` on validation failure); one leaderboard failing does not
  poison the report or its siblings.

Error strings are collapsed to one line and length-capped before entering the
report (the `one_line` precedent from `core/actions.py`). No response body
text is echoed into `error` — status code + exception class + URL only, so a
misconfigured URL pointing at some other service cannot leak that service's
response into our UI.

## 5. `BenchmarkService` — freshness model

New `dispatcher/core/benchmark_service.py`, a deliberate structural copy of
`SyncService`:

- `get(*, start_fetch=True) -> BenchmarksStatus` returns the cached report
  **immediately** (never a network call) and, under the same lock, decides
  whether to start a background fetch: not already in flight, and at least
  `_FETCH_MIN_INTERVAL_SECONDS = 60.0` since the last attempt started.
- The fetch runs in a daemon `threading.Thread`. Failures are swallowed into
  the report; the service never dies.
- Wire status:

```python
class BenchmarksStatus(BaseModel):
    report: BenchmarksReport
    fetch_in_flight: bool
```

**Single time semantics (pinned by design review):** `report.fetched_at` is
the completion time of the last fetch attempt **whose outcome formed the
current report** — a failed attempt forms an `unavailable`/`unreadable`
report and stamps `fetched_at` exactly like a successful one forms an `ok`
report. There is **no second timestamp on the wire**: no `last_fetch_at`
field exists in the API. The throttle's "when did the last attempt start"
bookkeeping is a private field of the service, never serialized. Similarly,
`report.error` is the error of the report-forming attempt; there is no
separate `last_fetch_error`.

Each completed attempt **atomically replaces** the whole report. We do not
keep stale `ok` data alongside a newer failure — fail-closed beats
fail-comfortable, and a "stale but shown" tier is state we don't need in
phase 1.

Before the first attempt completes: `status: "unavailable"`,
`fetched_at: null`, `error: null`. The UI reads that triple (typically with
`fetch_in_flight: true`) as "not fetched yet", distinguishable from a real
failure, which always carries an `error`.

## 6. HTTP client rules

- Dependency: `httpx>=0.27` — dispatcher's first HTTP client, and the only
  new dependency of this feature.
- A synchronous `httpx.Client` used **only inside the fetch thread**, created
  per fetch cycle with `timeout=10.0` (httpx applies it to each phase:
  connect, read, write, pool — a single request cannot hang the cycle for
  more than a few multiples of it) and closed at cycle end. No client object
  escapes the service.
- Requests per cycle: `GET {base}/api/v1/benchmarks`, then per benchmark
  `GET {base}/api/v1/benchmarks/{id}/leaderboard`. Benchmark counts are
  small; ATP's 120/min rate limit is not approachable at one cycle/minute.
- **URL construction (pinned by design review):** the benchmark id is the
  validated `int` field of `BenchmarkInfo`, rendered with `str(int)` and
  joined as a single path segment via URL-safe quoting (`urllib.parse.quote`
  with `safe=""`) — never raw string concatenation of producer-controlled
  text into a URL. With `extra="ignore"` + `id: int` this is belt and
  braces, and the belt stays on if the field type ever loosens.
- Redirects disabled (`follow_redirects=False`): the configured URL must be
  the server, not a hop through somewhere else.

## 7. read_api + route

- `read_api.benchmarks(service | None) -> BenchmarksStatus` — thin
  pass-through; with no service configured it synthesizes the
  `unconfigured` report itself so the route has exactly one shape to return.
- `GET /api/benchmarks` in `server/app.py`, `response_model=BenchmarksStatus`.
  Global, not project-keyed — benchmarks are ecosystem-wide, not a property
  of one mirror. GET-only; 200 always (state is in the body, matching how
  sync status is served); no new mutation routes.
- Wired in `create_app`: `benchmark_service = BenchmarkService(config) if
  config.benchmarks_url else None`, threaded like `sync_service` (injectable
  for tests).

## 8. Web panel

A global **Benchmarks** section in `server/static/index.html`, structurally
next to Sync:

- Status line: configured URL, `fetched_at`, spinner while
  `fetch_in_flight`, and the report `error` when present.
- Body: benchmark list (name, version, tasks_count, tags); selecting a
  benchmark shows its leaderboard table (`agent_name`, `best_score`,
  `run_count`, `user_id`), sorted by `best_score` desc as delivered.
- Refresh rides the page's **existing polling cycle** — no second timer —
  with a stale-response guard by the `ppGen` generation-counter precedent.
- Zero-state rules (cross-surface rule from product-proposals, applied from
  day one):
  - the section is **hidden** entirely when `status == "unconfigured"`;
  - a confident "0 benchmarks" renders only when `status == "ok"`;
  - a confident "0 entries" for one leaderboard renders only when that
    leaderboard's own `status == "ok"`;
  - `unavailable`/`unreadable` (report- or leaderboard-level) render as
    explicit unknown with the error line — never as an empty list.
- All producer text (`name`, `agent_name`, tags, error strings) goes through
  the page's existing `esc()` path before hitting innerHTML.

## 9. Vendored contract — `contracts/atp-benchmark-api/v1/`

Pinned copy per the nine-contract house pattern:

- `openapi.json` — the eco server's OpenAPI **pruned to the consumed
  surface**: the two GET routes and the component schemas they reference
  (`BenchmarkResponse`, the benchmark `LeaderboardEntry`). Pruning keeps the
  pin reviewable and keeps unrelated producer churn out of our drift signal.
- `fixtures/` — captured live responses (a benchmarks list, at least one
  leaderboard, including an empty-leaderboard case). Dual use: valid-input
  fixtures for unit tests and payloads for the integration stub (§10).
- `manifest.json` + `PINNED.txt` — generated by the existing
  `scripts/vendor_manifest.py`; producer commit passed as an argument, per
  house rule.
- Re-vendor: `scripts/revendor_atp_benchmark_api.sh` + runbook
  `docs/revendor-atp-benchmark-api.md`. Generation runs from the neighbor
  checkout at the pin — `uv run --project ../atp-platform` with
  `ATP_SERVER_PROFILE=eco`, build the app via its factory, dump
  `app.openapi()`, prune with a small script (`scripts/prune_atp_openapi.py`)
  that keeps the two paths and transitively-referenced components. Dev
  tooling may read the neighbor; runtime never does.
- **Copy-integrity** (guarantee A): `tests/test_atp_benchmark_api_vendor.py`,
  offline, never skipped, `PRODUCER_COMMIT` literal, asserts
  manifest↔on-disk agreement both directions + reproducible `tree_sha256`.
- **Upstream drift** (guarantee B): a scheduled advisory workflow checking
  out atp-platform at its default branch, regenerating the **pruned**
  OpenAPI the same way, and comparing tree hashes — drift is defined by the
  regenerated artifact, not by hashing producer source files (refactors that
  don't move the contract must not alarm). Exit codes 0/1/2;
  `unavailable ≠ no drift` (a red generation step means "fix the
  observation", never "assume in sync"). Not required, not on PR.

## 10. Testing

- **Classification units** (`tests/test_benchmarks.py`): each of the four
  states; per-leaderboard partial failure isolation; `extra` fields ignored;
  missing required field → `unreadable`; vendored fixtures parse to `ok`
  (this is the fixture-pin: the vendored contract and the consumer models
  must agree, in CI, forever).
- **Service units**: `get()` returns without network (a poisoned
  fetch-function that asserts it's never called on the render path);
  throttle honored; failed attempt replaces the report and stamps
  `fetched_at`; pre-first-fetch triple (`unavailable`/`fetched_at: null`/
  `error: null`).
- **Route level** (`tests/test_api.py`): golden serialization of
  `BenchmarksStatus` including the `unconfigured` shape; endpoint present in
  the GET-only route audit.
- **Integration stub**: an in-test FastAPI app serving the vendored fixtures
  over a real socket; the real `httpx` path end-to-end into an `ok` report.
  Also the failure paths: connection refused → `unavailable`; 500 →
  `unavailable`; valid-JSON-wrong-shape → `unreadable`.
- **Web harness** (`tests/web/`): panel rules under Node — hidden on
  `unconfigured`, zero-state rules, error rendering, stale-guard.
- **Live smoke:** *not in CI.* A real eco server needs a database and
  seeding; the cost is not proportionate to phase 1. Manual runbook
  `docs/atp-benchmark-live-smoke.md` (boot eco server from the neighbor
  checkout, point a scratch `dispatcher.toml` at it, verify the panel).
  Recorded as an accepted residual below.

## 11. Delivery plan

- **PR-1** — vendored contract + `core/benchmarks.py` +
  `BenchmarkService` + `read_api` + route + config field + unit/route/stub
  tests + revendor script/runbook + drift workflow.
- **PR-2** — web panel + Node harness rules + live-smoke runbook.

Both PRs: branch → PR → Copilot review → human merge. TODO checkbox flips
with the PR numbers.

## 12. Known residuals (accepted)

- **No CI live smoke** against a real eco server (§10). The integration stub
  proves the consumer path; it cannot prove the producer still serves this
  shape — that is the drift workflow's job, plus the manual runbook.
- **Whole-report replacement** (§5) means a transient network blip hides
  previously-fetched data until the next successful cycle (≤ ~60s). Chosen
  over a stale-data tier: less state, and "unknown" is the honest answer
  while the source is unreachable.
- **`created_at` stays a string** (§4) exactly as the producer sends it; no
  datetime parsing on our side, so a producer format change cannot become a
  spurious `unreadable`.
- **Subtractive eco profile**: ATP guarantees the benchmark surface; other
  routers "ride along". We consume only the guaranteed surface, so this
  looseness does not reach us.
