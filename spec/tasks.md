# Tasks — Dispatcher Roadmap Module

Traceability: [requirements.md](requirements.md) · [design.md](design.md)

**Priorities:** 🔴 P0 critical · 🟠 P1 high · 🟡 P2 medium · 🟢 P3 low
**Statuses:** ⬜ TODO · 🔄 IN PROGRESS · ✅ DONE · ⏸️ BLOCKED

No TASK-000: the project already exists (pyproject.toml, tests, CI conventions in place).

---

## M1 — Roadmap MVP ✅ (shipped: PR #6 roadmap-api, PR #7 tui-roadmap-tab, fix b440034)

### TASK-001: Roadmap read-model core
🟠 P1 | ✅ DONE

**Description:**
`dispatcher/core/roadmap.py`: YAML loading from
`prograph-vault/authored/roadmaps/` (override via `roadmap_dirs`),
`RoadmapItemView`/`EvidenceResult`/`RoadmapResponse` models, 5 typed
evidence rules, status ladder `unknown/planned/implemented/verified` +
`blocked` from `depends_on`, warnings for malformed/duplicate input,
path-safe joins.

**Traces to:** [REQ-001], [REQ-002], [REQ-003], [REQ-004], [REQ-005], [DESIGN-001], [DESIGN-002]

### TASK-002: Roadmap API endpoints
🟠 P1 | ✅ DONE

**Description:**
`GET /api/roadmap` (RoadmapResponse) and `GET /api/roadmap/{item_id}`
(404 on unknown id) in `dispatcher/server/app.py`.

**Traces to:** [REQ-006], [DESIGN-003]
**Depends on:** [TASK-001]

### TASK-003: Web dashboard Roadmap section
🟠 P1 | ✅ DONE

**Description:**
Roadmap section in `dispatcher/server/static/index.html`:
`Phase | Item | Owner | Status | Blockers | Evidence`.

**Traces to:** [REQ-007], [DESIGN-004]
**Depends on:** [TASK-002]

### TASK-004: TUI Roadmap tab
🟡 P2 | ✅ DONE

**Description:**
`TabPane("Roadmap")` + DataTable in `dispatcher/tui/app.py`, mirroring
web columns; vault project resolution fixed via roadmap dirs (b440034).
Tests: `tests/test_roadmap.py` (10), `tests/test_tui.py`.

**Traces to:** [REQ-008], [DESIGN-005]
**Depends on:** [TASK-001]

---

## M2 — Surface completion

### TASK-101: Roadmap view in VSCode extension
🟠 P1 | ✅ DONE | Est: 1d

**Description:**
The last MVP surface from recommendation §6 not yet implemented
(`vscode-ext/src` has no roadmap code).

**Checklist:**
- [x] Add Roadmap tree/webview to `vscode-ext` consuming `GET /api/roadmap`
- [x] Render `Phase | Item | Owner | Status | Blockers | Evidence` mirroring web/TUI columns
- [x] Status icons for `planned/implemented/verified/blocked/unknown`
- [x] Item drill-down showing per-rule `EvidenceResult` (passed/detail)
- [x] Graceful state when server unreachable or no roadmaps found
- [x] Extension tests (or smoke script) for view data mapping

**Traces to:** [REQ-009], [DESIGN-006]
**Depends on:** [TASK-002]

---

## M3 — Post-MVP views & governance linkage

### TASK-102: Drift projection over existing `/api/contracts`
🟡 P2 | ✅ DONE | Est: 1d

**Description:**
Second-step item from recommendation §8: `drift` as a projection of the
existing contracts checker, not a new mechanism.

**Checklist:**
- [x] `GET /api/roadmap/drift` — join roadmap items (`target_contract`) with `/api/contracts` sync state
- [x] Surface `drift` in `computed_status` only when contracts checker reports canon/vendored mismatch for the item's `target_contract`
- [x] Web + TUI: Contract Drift view/column
- [x] Tests: in-sync, drifted, contract unknown → status stays `unknown`/unchanged
- [x] Regression: existing 4+1 statuses unaffected when no `target_contract`

**Traces to:** [REQ-010], [DESIGN-007]
**Depends on:** [TASK-001]

### TASK-103: Evidence freshness (`last_seen`)
🟡 P2 | ✅ DONE | Est: 0.5d

**Description:**
`RoadmapItemView` currently has no `last_seen`; the model in
recommendation §7 requires it, and the Freshness view depends on it.

**Checklist:**
- [x] Add `last_seen` to `EvidenceResult`/`RoadmapItemView` (mtime of matched file/DB/log artifact, None where not applicable)
- [x] Expose in `/api/roadmap` payload; add Freshness column to web + TUI
- [x] Tests: freshness populated for `file_exists`/`sqlite_has_row`, absent for `project_detected`

**Traces to:** [REQ-011], [DESIGN-007]
**Depends on:** [TASK-001]

### TASK-104: Aggregation endpoints — phases and blockers
🟢 P3 | ✅ DONE | Est: 0.5d

**Description:**
Optional endpoints from recommendation §6; pure re-aggregations of
`build_roadmap` output.

**Checklist:**
- [x] `GET /api/roadmap/phases` — per-phase counts by status, blocked lists
- [x] `GET /api/roadmap/blockers` — reverse dependency view (what blocks what)
- [x] Tests for both aggregations incl. empty roadmap and cyclic `depends_on`
- [x] Register routes so they don't clash with `/api/roadmap/{item_id}`

**Traces to:** [REQ-012], [DESIGN-003]
**Depends on:** [TASK-002]

### TASK-105: Handoff — steward gates / owner_role linkage
🟡 P2 | ✅ DONE | Est: 0.5d

**Description:**
Recommendation §14 P2 items live in neighbor repos (`steward`,
`prograph`), which are read-only from here — dispatcher's part is a
written handoff, not code.

**Checklist:**
- [x] Write handoff note to `../prograph-vault/authored/notes/`: proposed `owner_role` field semantics, gates → verification-rule mapping, what dispatcher expects to consume
- [x] Add `owner_role` as an optional pass-through field on `RoadmapItem` (no evaluation logic) so vault YAML can start carrying it
- [x] Test: `owner_role` round-trips through the API

**Traces to:** [REQ-013], [DESIGN-001]
**Depends on:** [TASK-001]

---

## Iteration «sync & roadmap» — M1 (Gate 2 decomposition)

> Traceability for this section: approved discovery bundle
> ([discovery-brief-customer.md](discovery-brief-customer.md) FR/NFR/CON,
> [discovery-brief-engineer.md](discovery-brief-engineer.md) AP/CON) and the
> Gate 1 design
> [2026-07-14-sync-roadmap-design.md](../docs/superpowers/specs/2026-07-14-sync-roadmap-design.md)
> (DESIGN-201..207). External handoffs all landed: snapshot contract v1
> (github-checker#7), headless `pull`/`open-pr` (github-checker#8), KB
> `derived/snapshots/` convention (prograph-vault#24).

### TASK-201: Vendor the snapshot contract v1
🔴 P0 | ✅ DONE | Est: 0.5d

**Description:**
Pinned copy of `github-checker/contracts/snapshot/v1/` (schema + both golden
fixtures) into `contracts/github-checker-snapshot/v1/` with a pin header
(source repo, commit, sha256) — ADR-ECO-003 vendoring discipline.

**Checklist:**
- [x] Vendored schema + fixtures + pin header (`contracts/github-checker-snapshot/v1/`, source @ `787f6952d88b`)
- [x] Pydantic ingestion model validating `schema_version == 1`; anything else → explicit rejection, not best-effort parse (`core/snapshot_contract.py`)
- [x] CI test: both vendored fixtures parse and round-trip; pin hashes verified (`tests/test_snapshot_contract.py`, 8 tests)

**Traces to:** [DESIGN-201], brief FR-01, engineer AP-01/IF-03

### TASK-202: Sync verdict engine (`core/sync.py`)
🔴 P0 | ✅ DONE | Est: 1.5d

**Description:**
Verdict per `(repo, host)`: `ok | pull-first | no-data | unknown(reason)`
from the local live snapshot (`github-checker snapshot --local-only`, no
network) plus KB host snapshots (`prograph-vault/derived/snapshots/*.json`)
with `generated_at` age. KB repo (`prograph-vault`) is a pinned first-class
row; the top-line answer is the worst verdict across the current host's
repos. `gh_error` degrades only PR fields; `local.error`, stale (> 1 h) and
`schema_version != 1` → `unknown(reason)`; absent repo on a host → `no-data`.

**Checklist:**
- [x] Live local-only ingestion via `github-checker snapshot` subprocess (`run_live_snapshot`, command override for tests)
- [x] KB host snapshots ingestion + age computation (`load_kb_snapshots`; stale > 1 h → panel amber, verdicts unknown)
- [x] Verdict table per DESIGN-202 incl. degradation matrix §4; unit tests per row (`tests/test_sync.py`, 16)
- [x] Top-line verdict (pull-first > unknown > ok) + KB-repo pinned first row

**Traces to:** [DESIGN-202], brief FR-01/G-03, CON-03/CON-04, engineer AP-02
**Depends on:** [TASK-201]

### TASK-203: Background fetch run (verdict freshness)
🔴 P0 | ✅ DONE | Est: 1d

**Description:**
Async fetch-enabled snapshot run refreshing ahead/behind vs origin without
blocking render: screen serves cached data instantly (NFR-02 < 5 s), fresh
verdict lands ≤ 30 s (NFR-03), an in-flight flag drives the UI corner
spinner.

**Checklist:**
- [x] Non-blocking background run + in-flight status в read-модели (`core/sync_service.py`: SyncService/SyncStatus; HTTP-обвязка — TASK-207)
- [x] Verdict timestamp/age on every response (`report_generated_at`, `last_fetch_at`, `last_fetch_error`)
- [x] Test: render path never awaits the network run (7 тестов в `tests/test_sync_service.py`, в т.ч. `test_get_never_awaits_fetch`; live: get() 0.00 s при fetch в фоне)

**Traces to:** [DESIGN-202], brief NFR-02/NFR-03
**Depends on:** [TASK-202]

### TASK-204: Publisher `dispatcher publish-snapshot`
🔴 P0 | ✅ DONE | Est: 1d

**Description:**
CLI: run `github-checker snapshot --workspace`, atomically write
`prograph-vault/derived/snapshots/<host>.json`, commit to the KB repo —
the only write path, and only into the KB tool zone (prograph-vault#24
convention). Scheduling (cron/launchd ≤ 1 h) stays with the user; document
the crontab line.

**Checklist:**
- [x] Atomic write + KB git commit (`core/publish.py`: mkstemp+replace, no-op → «no changes», pull --rebase + push); failure exits non-zero (cron-visible)
- [x] Docs: crontab/launchd example per machine (README «Sync snapshots»)
- [x] Test: output validates against the vendored contract (`tests/test_publish.py`, 8; live run committed real KB snapshot)

**Traces to:** [DESIGN-203], engineer AP-02/CON-01, brief G-03
**Depends on:** [TASK-201]

### TASK-205: Repo auto-discovery proposals
🔴 P0 | ✅ DONE | Est: 1d

**Description:**
Diff the snapshot workspace walk against dispatcher's tracked set; new repo
→ proposal surfaced via API/UI; confirm/reject persisted in
`dispatcher.toml` (`tracked`/`ignored`) — dispatcher's own config, not an
observed-repo mutation. Appears at the next refresh, no daemon.

**Checklist:**
- [x] Tracked/ignored persistence (`core/tracking.py`, sidecar `dispatcher-sync.toml` — пользовательский dispatcher.toml программно не переписываем; zero-docs bootstrap: первый прогон сидирует всё присутствующее) + diff в `build_report`
- [x] Proposal rows в SyncReport (`proposals`, не влияют на top-line); `POST /api/sync/track` confirm/reject (405 нет, 409 если не сконфигурировано; пишет только sidecar + invalidate кэша)
- [x] Test: clone → proposal → confirm → tracked / reject → silent (`tests/test_tracking.py`, 10 + 2 API; live-смоук на реальном KB-снапшоте)

**Traces to:** [DESIGN-205], brief FR-02/G-04/J-05
**Depends on:** [TASK-202]

### TASK-206: Ecosystem roadmap summary
🔴 P0 | ⬜ TODO | Est: 1d

**Description:**
Aggregation over the existing roadmap read-model: per `owner_project` —
readiness share, `lagging` flag (below roadmap-file median), and
`contract-drift` flag via `check_contracts`. `GET /api/roadmap/summary`.
No new evidence rules (closed-set principle).

**Checklist:**
- [ ] Aggregation + summary endpoint with tests
- [ ] Reuses existing `_RULES`/contracts checker only

**Traces to:** [DESIGN-206], brief FR-03/G-01
**Depends on:** [TASK-001]

### TASK-207: Sync API endpoints
🔴 P0 | ⬜ TODO | Est: 0.5d

**Description:**
`GET /api/sync` (verdict table + top-line + in-flight flag) and
`GET /api/sync/hosts` (host panels with ages) in `server/app.py`,
pydantic-typed like the rest of the API.

**Traces to:** [DESIGN-207]
**Depends on:** [TASK-202], [TASK-203]

### TASK-208: Web Sync screen + roadmap summary row
🔴 P0 | ⬜ TODO | Est: 1.5d

**Description:**
Sync section in `server/static/index.html`: host panels with age badges
(> 1 h → amber `stale`), per-repo verdict rows, KB pinned on top, corner
«Fetching…» spinner, discovery proposals with confirm/reject, whitelist
actions rendered as copy-paste commands (`github-checker pull <dir>` /
`open-pr <dir>`) next to disabled buttons — live buttons are M2
(TASK-210). Roadmap section gains the summary header row (TASK-206).

**Traces to:** [DESIGN-207], brief FR-01 acceptance (spinner in the corner, verdict ≤ 30 s)
**Depends on:** [TASK-205], [TASK-206], [TASK-207]

### TASK-209: TUI Sync tab
🟠 P1 | ⬜ TODO | Est: 1d

**Description:**
`TabPane("Sync")` mirroring the web verdict table (hosts, ages, top-line);
Roadmap tab gains the summary header. Full J-01/J-03 terminal parity is
FR-06 (Should) — M2.

**Traces to:** [DESIGN-207], brief G-05
**Depends on:** [TASK-207]

## Iteration «sync & roadmap» — M2

### TASK-210: Live whitelist action buttons
🟠 P1 | ⬜ TODO | Est: 1d

**Description:**
`POST /api/actions/pull` / `POST /api/actions/create-pr` delegating to the
shipped github-checker headless commands (v0.3.0, github-checker#8):
explicit click only, CSRF token, one in-flight action per repo, audit log
line. Web/TUI buttons replace the copy-paste fallback.

**Traces to:** [DESIGN-204], brief FR-01/NFR-01
**Depends on:** [TASK-208]

### TASK-211: VSCode status-bar verdict
🟡 P2 | ⬜ TODO | Est: 0.5d

**Description:**
Aggregate top-line verdict in the VSCode extension status bar, consuming
`GET /api/sync`.

**Traces to:** [DESIGN-207]
**Depends on:** [TASK-207]

---

## Not planned

Explicitly out of scope per the recommendation (§10, §13): Owners/load
view, query language, status writes, GitHub API, issue automation,
LLM summaries, runtime mutations.

## Dependency Graph

```text
TASK-001 (core ✅)
 ├──► TASK-002 (API ✅)
 │     ├──► TASK-003 (web ✅)
 │     ├──► TASK-101 (VSCode view)
 │     └──► TASK-104 (phases/blockers)
 ├──► TASK-004 (TUI ✅)
 ├──► TASK-102 (drift)
 ├──► TASK-103 (freshness)
 └──► TASK-105 (owner_role handoff)

TASK-201 (vendored contract)
 ├──► TASK-202 (verdict engine) ──► TASK-203 (bg fetch) ──► TASK-207 (sync API)
 │        └──► TASK-205 (auto-discovery) ─┐                     ├──► TASK-209 (TUI tab)
 └──► TASK-204 (KB publisher)             ├──► TASK-208 (web)   └──► TASK-211 (VSCode, M2)
TASK-001 ──► TASK-206 (roadmap summary) ──┘        └──► TASK-210 (live buttons, M2)
```

## Summary

| Milestone | Tasks | Done | Remaining est. |
|-----------|-------|------|----------------|
| M1 Roadmap MVP | TASK-001..004 | 4/4 ✅ | — |
| M2 Surface completion | TASK-101 | 1/1 ✅ | — |
| M3 Post-MVP views | TASK-102..105 | 4/4 ✅ | — |
| **sync & roadmap M1** | TASK-201..209 | 0/9 ⬜ | ~8d |
| **sync & roadmap M2** | TASK-210..211 | 0/2 ⬜ | ~1.5d |

Roadmap-module milestones complete; the sync & roadmap iteration
(Gate 2 decomposition of the approved discovery bundle) is next.
