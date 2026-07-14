# Design — Iteration «sync & roadmap» (Gate 1)

> **Gate 1 Design** over the approved discovery bundle:
> `spec/discovery-brief-customer.md` + `spec/discovery-brief-engineer.md`
> (both `status: approved`, PRs #14–#18). Covers the three Must requirements
> FR-01/FR-02/FR-03 of the customer brief; Should items (FR-04..FR-06) are out
> of this design. Owner role: architect (approval = merge of this PR).
> Referenced brief IDs (`FR-*`, `NFR-*`, `CON-*`, `AP-*`, `Q-*`) are the
> briefs' namespaces, distinct from this repo's `REQ-*`/`DESIGN-*`.

## 1. Architectural principles (inherited + amended)

1. **Read-model first.** Everything below except DESIGN-204 keeps dispatcher a
   pure read-model over on-disk artifacts and the vendored snapshot contract.
2. **Whitelist mutations, never background** (brief NFR-01, conflict X-01
   resolved by product): the only write paths are `git pull --ff-only` and
   PR creation, each triggered by an explicit human click, each executed by
   **github-checker**, not by dispatcher (brief AP-01). Zero autonomous writes.
3. **Computed status, honest `unknown`.** Degraded inputs (no `gh`, no network,
   stale host snapshot, repo absent on a host) must render as explicit
   `unknown`/`stale`, never as an optimistic "ok" (brief CON-03/CON-04, RK-03).
4. **Consume contracts, don't re-implement collectors.** Sync data comes from
   the frozen `github-checker` snapshot contract v1 (`schema_version: 1`,
   github-checker PR #7); dispatcher vendors the schema, pinned.

## 2. High-level diagram

```text
            machine A                                machine B (any host)
┌──────────────────────────────┐          ┌──────────────────────────────┐
│ cron/launchd (≤1 h):         │          │ same publisher job           │
│  github-checker snapshot     │          │                              │
│    --workspace <root>        │          │                              │
│  → prograph-vault/derived/   │          │ → …/derived/snapshots/       │
│    snapshots/<host>.json     │          │      <host>.json             │
└──────────────┬───────────────┘          └──────────────┬───────────────┘
               └──────────────── KB (git) ───────────────┘
                                  │
┌───────────────────────────── dispatcher ─────────────────────────────┐
│ contracts/github-checker-snapshot/v1/   (vendored pin, PR #7)        │
│ core/sync.py                                                         │
│   ingest local snapshot (live run) + KB host snapshots (aged)        │
│   → verdict per repo per host: ok | pull-first | unknown(reason)     │
│   → KB repo (prograph-vault) flagged as first-class row              │
│ core/discovery.py (+)  workspace walk vs tracked set → proposals     │
│ core/roadmap.py   (+)  readiness/lag/contract-drift aggregation      │
│ server/app.py:  GET /api/sync   GET /api/sync/hosts                  │
│                 POST /api/actions/pull  POST /api/actions/create-pr  │
│                 GET /api/roadmap/summary                             │
│   ├─► web  static/index.html: Sync screen + Roadmap summary          │
│   ├─► tui  app.py: Sync tab (verdict, age badges, actions)           │
│   └─► vscode-ext: status-bar verdict (M2)                            │
└──────────────────────────────────────────────────────────────────────┘
        actions delegated ──► github-checker headless (handoff H-2)
```

## 3. Components

### DESIGN-201: Vendored snapshot contract (`contracts/github-checker-snapshot/v1/`)

Pinned copy of `github-checker/contracts/snapshot/v1/` (schema + the two golden
fixtures) with a pin header (source repo, commit, sha256) — same discipline as
the observability contract vendoring. Ingestion validates `schema_version == 1`
and rejects anything else with an explicit `unknown(schema)` verdict, never a
silent parse-as-best-effort (closes brief RK-02 on the consumer side).
A CI test validates both vendored fixtures against the vendored schema.

### DESIGN-202: Sync verdict engine (`core/sync.py`)

Input: the freshest available snapshot per host —
- **this host:** live `github-checker snapshot --workspace <root> --local-only`
  run (no network, satisfies NFR-02), plus an async background
  `git fetch`-enabled run for ahead/behind vs origin (NFR-03);
- **other hosts:** `prograph-vault/derived/snapshots/<host>.json` with age.

Verdict per `(repo, host)`:

| condition | verdict |
|---|---|
| `behind == 0` and `ahead == 0` and not `dirty` | `ok` |
| `behind > 0` or `ahead > 0` or `dirty` | `pull-first` (with detail) |
| repo absent in a host's snapshot | `no-data` (CON-03: absence ≠ ok) |
| snapshot age > 1 h, `gh_error` without KB fallback, `schema_version != 1` | `unknown(reason)` |

The aggregate top-line answer «можно работать / сначала pull-PR» is the worst
verdict across repos of the current host, with `prograph-vault` (the KB)
rendered as a pinned first-class row (brief G-03: KB freshness is the special
case). Every host panel always shows `generated_at` age (Q-02 resolution, see
§5).

### DESIGN-203: Cross-machine publisher (brief AP-02)

A tiny CLI entry `dispatcher publish-snapshot [--workspace <root>]`:
runs `github-checker snapshot`, writes
`prograph-vault/derived/snapshots/<host>.json` (atomic replace), commits to the
KB repo — `derived/` is the tool-written zone, so the KB constitution is
respected. Scheduling is the user's cron/launchd at ≤ 1 h. The publisher is the
**only** component that writes anywhere, and it writes only to the KB
`derived/` — it never touches observed repos (NFR-01 untouched: this is not a
mutation of monitored repos; it is the KB feed the KB constitution explicitly
assigns to tools).

### DESIGN-204: Whitelist actions (brief FR-01 buttons, NFR-01)

`POST /api/actions/pull` and `POST /api/actions/create-pr`, each taking
`{repo_dir}`. Guards: never called by refresh logic; CSRF-token per UI session;
one in-flight action per repo; full audit line in dispatcher's own log.
Execution is **delegated to github-checker headless commands** (AP-01 —
github-checker is the executor, `S` is already ff-only):

- `pull` → `github-checker pull <dir>` (fast-forward only),
- `create-pr` → `github-checker open-pr <dir>` (wraps `gh pr create`,
  returns PR URL + status; PR status then appears in the next snapshot).

**Handoff H-2 (github-checker):** these two headless subcommands do not exist
yet — TUI keys `s`/`S` need CLI twins. Tracked as the successor of the Q-01
handoff pattern; until H-2 lands, the UI renders the exact command as
copy-paste next to a disabled button (degraded but honest M1 fallback).

### DESIGN-205: Repo auto-discovery confirmation (brief FR-02)

`core/discovery.py` extension: diff the snapshot's workspace walk (it already
discovers by `*/.git`, zero config) against dispatcher's tracked set. New repo
→ a proposal row in the Sync screen: *«обнаружен `<dir>` — отслеживать?»* with
confirm/reject. The decision persists in dispatcher's own config
(`dispatcher.toml`, `tracked`/`ignored` lists — dispatcher owns this file;
writing it is not an observed-repo mutation). Appears at the next refresh
(per approved acceptance), no daemon needed.

### DESIGN-206: Ecosystem roadmap summary (brief FR-03)

Aggregation layer over the existing roadmap read-model (`core/roadmap.py`,
REQ-001..): per `owner_project` — readiness (share of items with
`computed_status: implemented`), a `lagging` flag (project readiness below the
roadmap-file median), and a `contract-drift` flag (any failing
`contract_in_sync` evidence touching the project, via `core/contracts.py`).
Exposed as `GET /api/roadmap/summary`; rendered as the one-screen list
«проекты × готовность × флаги» in web + TUI (per approved acceptance of FR-03).
No new evidence rules — reuse of the closed rule set (design principle 3 of the
roadmap module).

### DESIGN-207: UI surface

- **Web:** new Sync section (host panels, verdict rows, age badges, corner
  spinner «Fetching…» while the background fetch-run is in flight — the
  approved acceptance detail; screen renders immediately from cached data).
- **TUI:** Sync tab mirroring the web verdict table + `p` (pull) action key
  behind the same whitelist guard; Roadmap tab gains the summary header row.
- **VSCode ext:** status-bar aggregate verdict — M2, out of this design's
  acceptance.

## 4. Degradation matrix (brief CON-04)

| failure | behaviour |
|---|---|
| `gh` unauthorized / offline | snapshot degrades to git-only (`gh_error` recorded); PR data marked `unknown`, verdict still computed from git state |
| no network at all | verdict from last KB snapshot for this host + prominent age; top-line becomes «неизвестно, работаем локально» |
| host snapshot older than 1 h | host panel amber `stale`, its rows `unknown(stale)` |
| repo missing on a host | `no-data` row — never counted as synced |
| `schema_version != 1` | `unknown(schema)` + a warning naming the vendored pin |

## 5. Q-02 resolution (cron-publication reliability, brief RK-03)

Decision: **age is the heartbeat.** Every host panel always displays
`generated_at` age; > 1 h flips the panel to `stale` (amber) and poisons its
verdicts to `unknown(stale)`. No separate alerting channel in this iteration —
a stale panel on the first screen *is* the alert (matches «честное неизвестно»
CON-04 and keeps scope inside the approved bundle). Push-alerts, if ever
wanted, are a future FRD item.

## 6. Traceability

| Brief ID | Design |
|---|---|
| FR-01 (sync status + actions, Must) | DESIGN-201, 202, 203, 204, 207 |
| FR-02 (auto-discovery, Must) | DESIGN-205 |
| FR-03 (ecosystem roadmap, Must) | DESIGN-206, 207 |
| NFR-01 (whitelist mutations) | DESIGN-203 (KB-only writes), 204 (guards, delegation) |
| NFR-02 (refresh < 5 s) | DESIGN-202 local-only live run, no network on render |
| NFR-03 (verdict ≤ 30 s, non-blocking fetch) | DESIGN-202 async fetch-run + 207 corner spinner |
| CON-01..04 | §1 p.3, DESIGN-202/203, §4 |
| AP-01 (github-checker = collector/executor) | DESIGN-201, 202, 204 |
| AP-02 (KB publication, host aggregation) | DESIGN-203, 202 |
| Q-02 / RK-03 | §5 |
| RK-01 (graph.db fragility) | inherited by DESIGN-206 via existing contracts checker; no new graph.db readers added |

## 7. Milestones

- **M1:** DESIGN-201, 202, 205, 206, 207 (web+TUI, actions rendered as
  copy-paste commands while H-2 is pending) — all three Musts observable
  end-to-end, zero write paths beyond the publisher.
- **M2:** DESIGN-204 live buttons (after github-checker H-2), VSCode surface.

## 8. Handoffs

| ID | Repo | What |
|---|---|---|
| H-2 | github-checker | headless `pull <dir>` (ff-only) and `open-pr <dir>` subcommands — CLI twins of TUI `s`/`S` + `gh pr create` wrap |
| H-3 | prograph-vault | register `derived/snapshots/<host>.json` convention in the KB derived-zone docs (tool-written, dispatcher publisher) |
