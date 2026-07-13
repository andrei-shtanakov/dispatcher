# Requirements — Dispatcher Roadmap Module

> Source of intent: `_cowork_output/roadmap-dashboard-dispatcher-recommendation.md`
> (rev 2, 2026-07-11). That file is a **dev draft**, not a runtime source; this
> spec is the in-repo canonical statement of what dispatcher implements.

## 1. Context

The human team needs to track roadmap implementation across the
AI-orchestrators ecosystem. Manual markdown checklists drift from reality;
instead, status must be **computed from evidence** found in project
artifacts. Dispatcher is already the read-only monitoring dashboard
(read-model over on-disk artifacts), so the roadmap dashboard is an
extension of dispatcher — not a new subproject.

Boundary of responsibilities (per recommendation §4–5):

```text
steward defines gates
ecosystem-kb (prograph-vault) stores roadmap intent
projects emit evidence
dispatcher renders truth
```

## 2. Goals

- Render human-authored roadmap intent as computed, team-facing status.
- Show per-item evidence: why an item counts as implemented/blocked.
- Surface the roadmap in all dispatcher UIs: web, TUI, VSCode extension.

**Success metric:** a roadmap item's status can always be explained by
clicking through to the concrete evidence rules that passed or failed.

## 3. Stakeholders

| Role | Interest |
|------|----------|
| Human team (PM/dev) | Roadmap Overview: phases, progress, blockers |
| Ecosystem KB owner | Authors roadmap YAML in `prograph-vault/authored/roadmaps/` |
| steward (later) | Owns gates/owner_role semantics consumed by dispatcher |
| Other dispatcher views | Reuse of `/api/contracts`, `/api/work-items` |

## 4. Out of Scope

Explicitly NOT part of this module (recommendation §13, §10):

- Owners/load view (PM analytics, not an evidence model)
- Complex query language over roadmap data
- Writing statuses back / manual status overrides / runtime mutations
- GitHub API integration, automatic issue creation
- ML/LLM summaries (natural-language briefing belongs to robin-runtime)
- A new subproject or making steward a dashboard
- Regex/prose-based evidence matching — prose `expected_evidence` is
  documentation of intent only, never machine-checked

## 5. Functional Requirements

#### REQ-001: Load roadmap intent from canonical YAML
**As a** KB owner
**I want** dispatcher to read roadmap YAML from `prograph-vault/authored/roadmaps/*.yaml`
**So that** intent lives in the human-owned KB, not in dispatcher or `_cowork_output/`

**Acceptance Criteria:**
```gherkin
GIVEN roadmap YAML files under <root>/prograph-vault/authored/roadmaps/
WHEN dispatcher builds the roadmap read-model
THEN every mapping item from every *.yaml is loaded with its source file name
AND `roadmap_dirs` in dispatcher.toml can override the default location
AND a missing roadmap directory yields a warning, not an error
AND malformed YAML / non-mapping items yield warnings and are skipped
AND duplicate item ids yield a warning and keep the first occurrence
```

**Priority:** P1 (MVP)
**Traces to:** [TASK-001], [DESIGN-001]

#### REQ-002: RoadmapItem model
**As a** dashboard consumer
**I want** each item exposed with id, title, phase, owner_project,
target_contract, depends_on, expected_evidence, computed_status, evidence,
blockers, source
**So that** every UI renders the same typed contract

**Acceptance Criteria:**
```gherkin
GIVEN a loaded roadmap item
WHEN it is serialized through the API
THEN it is a pydantic-typed `RoadmapItemView` with the fields above
AND `expected_evidence` (prose) is carried as documentation only
```

**Priority:** P1 (MVP)
**Traces to:** [TASK-001], [DESIGN-001]

#### REQ-003: Typed evidence rules (closed set, honest `unknown`)
**As a** team member
**I want** evidence checked only via a small closed set of typed rules
**So that** statuses are trustworthy and never regex-guessed

Rules (recommendation §13.3):
- `project_detected(project)`
- `file_exists(project, path)`
- `sqlite_has_row(project, db, query)` — read-only
- `contract_in_sync(name)` — delegates to the existing contracts checker
- `work_item_chain(work_item_id, min_links)` — delegates to `/api/work-items` correlation

**Acceptance Criteria:**
```gherkin
GIVEN an item with `evidence_rules`
WHEN rules are evaluated
THEN each yields an EvidenceResult {rule, kind, passed, detail}
AND an unknown rule name degrades to a failed check with detail, not a crash
AND a rule error (bad YAML, unreadable DB) degrades to a failed check
GIVEN an item with no evidence_rules
THEN its computed_status is `unknown` (honesty rule)
```

**Priority:** P1 (MVP)
**Traces to:** [TASK-001], [DESIGN-002]

#### REQ-004: Computed status ladder (MVP: 4 + blocked)
**As a** team member
**I want** statuses computed, never manually ticked

Ladder: `planned` / `implemented` / `verified` / `unknown`, plus `blocked`.

**Acceptance Criteria:**
```gherkin
GIVEN evidence rules of kind implementation and verification
WHEN status is computed
THEN no rules → unknown
AND some implementation rule failed (or none defined) → planned
AND all implementation rules passed → implemented
AND additionally all verification rules passed (≥1 present) → verified
```

`in_progress` is deliberately absent (partial evidence is noise);
`drift` arrives post-MVP as a projection of `/api/contracts` [REQ-010].

**Priority:** P1 (MVP)
**Traces to:** [TASK-001], [DESIGN-002]

#### REQ-005: Blocked from dependencies
**As a** team member
**I want** items marked `blocked` strictly when a `depends_on` item has not
reached implemented+
**So that** the Dependencies view shows what blocks what

**Acceptance Criteria:**
```gherkin
GIVEN item A `planned` with depends_on [B]
WHEN B's computed_status is not implemented or verified (or B is missing)
THEN A becomes `blocked` with blockers=[B]
AND an item already implemented/verified is never downgraded to blocked
```

**Priority:** P1 (MVP)
**Traces to:** [TASK-001], [DESIGN-002]

#### REQ-006: Roadmap API
**As a** UI or script consumer
**I want** `GET /api/roadmap` and `GET /api/roadmap/{item_id}`

**Acceptance Criteria:**
```gherkin
GIVEN the server is running
WHEN GET /api/roadmap
THEN response is {roadmaps, items[], warnings[]} (pydantic RoadmapResponse)
WHEN GET /api/roadmap/{item_id} with an unknown id
THEN 404 with a helpful detail
```

**Priority:** P1 (MVP)
**Traces to:** [TASK-002], [DESIGN-003]

#### REQ-007: Web dashboard Roadmap tab
**As a** team member
**I want** a Roadmap section in the web dashboard
**So that** the minimal first screen shows `Phase | Item | Owner | Status | Blockers | Evidence`

**Priority:** P1 (MVP)
**Traces to:** [TASK-003], [DESIGN-004]

#### REQ-008: TUI Roadmap tab
**As a** terminal user
**I want** a Roadmap tab in `dispatcher tui` mirroring the web columns

**Priority:** P2 (MVP, second PR allowed)
**Traces to:** [TASK-004], [DESIGN-005]

#### REQ-009: VSCode extension Roadmap view
**As a** VSCode user
**I want** a Roadmap view in the dispatcher extension consuming `GET /api/roadmap`
**So that** all three dispatcher surfaces show the same roadmap truth

**Acceptance Criteria:**
```gherkin
GIVEN the extension is connected to the server
WHEN the Roadmap view renders
THEN it mirrors Phase | Item | Owner | Status | Blockers | Evidence
AND status icons distinguish planned/implemented/verified/blocked/unknown
AND item drill-down shows per-rule EvidenceResult (passed/detail)
AND server-unreachable / no-roadmaps states degrade gracefully
```

**Priority:** P1
**Traces to:** [TASK-101], [DESIGN-006]

#### REQ-010: Drift as projection of `/api/contracts` (post-MVP)
**As a** team member
**I want** `drift` status when the contracts checker reports canon/vendored
mismatch for an item's `target_contract`
**So that** spec-vs-implementation drift is visible without new mechanics

**Acceptance Criteria:**
```gherkin
GIVEN an item with target_contract C
WHEN the contracts checker reports C out of sync
THEN the item surfaces `drift`
AND items without target_contract keep the MVP 4+1 statuses unchanged
AND contract not comparable → status unchanged (stays honest)
```

**Priority:** P2
**Traces to:** [TASK-102], [DESIGN-007]

#### REQ-011: Evidence freshness — `last_seen` (post-MVP)
**As a** team member
**I want** `last_seen` per item (mtime of matched artifacts)
**So that** the Freshness view shows when evidence was last observed

**Priority:** P2
**Traces to:** [TASK-103], [DESIGN-007]

#### REQ-012: Aggregation endpoints (optional)
**As a** consumer
**I want** `GET /api/roadmap/phases` and `GET /api/roadmap/blockers`
as pure re-aggregations of the roadmap read-model

**Priority:** P3
**Traces to:** [TASK-104], [DESIGN-003]

#### REQ-013: Governance linkage — `owner_role` pass-through (post-MVP)
**As a** steward owner
**I want** dispatcher to carry an optional `owner_role` field from roadmap
YAML without evaluating it, plus a written handoff describing gates →
verification-rule mapping
**So that** steward can own the governance model while dispatcher only renders

**Priority:** P2
**Traces to:** [TASK-105], [DESIGN-001]

## 6. Non-Functional Requirements

#### NFR-001: Read-only
Dispatcher never mutates monitored projects, the vault, or roadmap intent.
SQLite access is read-only, as in existing collectors.

#### NFR-002: Graceful degradation
Missing projects/dirs/roadmaps produce warnings, not failures. A malformed
user-authored rule degrades to a failed evidence check; `/api/roadmap`
never 500s on bad YAML.

#### NFR-003: No runtime dependency on `_cowork_output/`
Runtime reads only canonical sources (vault roadmaps, project artifacts).
`_cowork_output/` is dev-draft only (umbrella CLAUDE.md rule).

#### NFR-004: Path safety
Human-authored YAML paths are resolved with absolute-path and `..`-escape
rejection (defense in depth against probing the host filesystem).

#### NFR-005: Tests
Every functional requirement has unit tests; regressions (e.g. statuses
unaffected when no `target_contract`) get explicit regression tests.

#### NFR-006: Code conventions
Python 3.12+, pydantic models, type hints everywhere, ruff (88 cols),
existing collector patterns (`read_rows`, `SourceReadError`).

## 7. Constraints & Tech Stack

- Language: Python (uv-managed); UI: FastAPI + static web, Textual TUI,
  TypeScript VSCode extension in `vscode-ext/`.
- Neighbor repos (`prograph-vault`, `steward`, `prograph`) are read-only;
  cross-repo asks go through handoff notes, never direct edits.
- Git: feature branch → PR; no direct commits to `master`; merge by human.

## 8. Acceptance by Milestone

| Milestone | Contents | Exit criteria | Status |
|-----------|----------|---------------|--------|
| M1 Roadmap MVP | REQ-001..008 | `/api/roadmap` + web + TUI shipped, tests green | ✅ shipped (PR #6, #7) |
| M2 Surface completion | REQ-009 | VSCode Roadmap view consuming the same API | ⬜ |
| M3 Post-MVP views | REQ-010..013 | drift, freshness, aggregations, owner_role handoff | ⬜ |
