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
🟡 P2 | ⬜ TODO | Est: 1d

**Description:**
Second-step item from recommendation §8: `drift` as a projection of the
existing contracts checker, not a new mechanism.

**Checklist:**
- [ ] `GET /api/roadmap/drift` — join roadmap items (`target_contract`) with `/api/contracts` sync state
- [ ] Surface `drift` in `computed_status` only when contracts checker reports canon/vendored mismatch for the item's `target_contract`
- [ ] Web + TUI: Contract Drift view/column
- [ ] Tests: in-sync, drifted, contract unknown → status stays `unknown`/unchanged
- [ ] Regression: existing 4+1 statuses unaffected when no `target_contract`

**Traces to:** [REQ-010], [DESIGN-007]
**Depends on:** [TASK-001]

### TASK-103: Evidence freshness (`last_seen`)
🟡 P2 | ⬜ TODO | Est: 0.5d

**Description:**
`RoadmapItemView` currently has no `last_seen`; the model in
recommendation §7 requires it, and the Freshness view depends on it.

**Checklist:**
- [ ] Add `last_seen` to `EvidenceResult`/`RoadmapItemView` (mtime of matched file/DB/log artifact, None where not applicable)
- [ ] Expose in `/api/roadmap` payload; add Freshness column to web + TUI
- [ ] Tests: freshness populated for `file_exists`/`sqlite_has_row`, absent for `project_detected`

**Traces to:** [REQ-011], [DESIGN-007]
**Depends on:** [TASK-001]

### TASK-104: Aggregation endpoints — phases and blockers
🟢 P3 | ⬜ TODO | Est: 0.5d

**Description:**
Optional endpoints from recommendation §6; pure re-aggregations of
`build_roadmap` output.

**Checklist:**
- [ ] `GET /api/roadmap/phases` — per-phase counts by status, blocked lists
- [ ] `GET /api/roadmap/blockers` — reverse dependency view (what blocks what)
- [ ] Tests for both aggregations incl. empty roadmap and cyclic `depends_on`
- [ ] Register routes so they don't clash with `/api/roadmap/{item_id}`

**Traces to:** [REQ-012], [DESIGN-003]
**Depends on:** [TASK-002]

### TASK-105: Handoff — steward gates / owner_role linkage
🟡 P2 | ⬜ TODO | Est: 0.5d

**Description:**
Recommendation §14 P2 items live in neighbor repos (`steward`,
`prograph`), which are read-only from here — dispatcher's part is a
written handoff, not code.

**Checklist:**
- [ ] Write handoff note to `../prograph-vault/authored/notes/`: proposed `owner_role` field semantics, gates → verification-rule mapping, what dispatcher expects to consume
- [ ] Add `owner_role` as an optional pass-through field on `RoadmapItem` (no evaluation logic) so vault YAML can start carrying it
- [ ] Test: `owner_role` round-trips through the API

**Traces to:** [REQ-013], [DESIGN-001]
**Depends on:** [TASK-001]

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
```

## Summary

| Milestone | Tasks | Done | Remaining est. |
|-----------|-------|------|----------------|
| M1 Roadmap MVP | TASK-001..004 | 4/4 ✅ | — |
| M2 Surface completion | TASK-101 | 1/1 ✅ | — |
| M3 Post-MVP views | TASK-102..105 | 0/4 | ~2.5d |

Recommended order: TASK-101 → TASK-102 → TASK-103 → TASK-105 → TASK-104.
