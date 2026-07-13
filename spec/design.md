# Design — Dispatcher Roadmap Module

> M1 (MVP) components below are **implemented** — this document describes
> the shipped code (`dispatcher/core/roadmap.py`, PR #6/#7) plus the
> planned M2/M3 extensions. Earlier stage designs:
> `docs/superpowers/specs/2026-07-03-dispatcher-design.md` (Stage 1),
> `2026-07-05-dispatcher-tui-design.md` (Stage 2 TUI).

## 1. Architectural Principles

1. **Read-model, not authority.** Intent lives in prograph-vault, gates in
   steward, evidence in project artifacts. Dispatcher only computes and
   renders. `intent + evidence -> computed team-facing status`.
2. **Computed status, never manual ticks.** No write path exists.
3. **Closed rule set, honest `unknown`.** Extend the rule set — yes;
   bend an item onto the rules with regex hacks — no.
4. **Reuse existing read-model machinery.** Contracts via
   `check_contracts`, execution evidence via `build_work_items`,
   SQLite via collectors' `read_rows`.

## 2. High-Level Diagram

```text
prograph-vault/authored/roadmaps/*.yaml        (human-authored intent)
        │
        ▼
┌─────────────────────────── dispatcher ────────────────────────────┐
│ core/roadmap.py                                                    │
│  _load_yaml_items ──► _evaluate_item ──► _apply_blocked            │
│        │                    │                                      │
│        │             _RULES registry                               │
│        │              ├ project_detected ──┐                       │
│        │              ├ file_exists        │  _EvidenceContext     │
│        │              ├ sqlite_has_row ────┤  (snapshots, lazy     │
│        │              ├ contract_in_sync ──┤   contracts & chains) │
│        │              └ work_item_chain ───┘                       │
│        ▼                                                           │
│  RoadmapResponse {roadmaps, items[RoadmapItemView], warnings}      │
│        │                                                           │
│  server/app.py: GET /api/roadmap, /api/roadmap/{item_id}           │
│        ├──► web static/index.html (Roadmap section)                │
│        ├──► tui/app.py (Roadmap tab)                               │
│        └──► vscode-ext (Roadmap view — M2, planned)                │
└────────────────────────────────────────────────────────────────────┘
   evidence sources: project snapshots (discovery), SQLite DBs,
   files, contracts checker, work-item correlation
```

## 3. Components

### DESIGN-001: Roadmap read-model core (`dispatcher/core/roadmap.py`) ✅

Loads YAML intent, evaluates evidence, computes status.

#### Data model
```python
class EvidenceResult(BaseModel):
    rule: str
    kind: str          # implementation | verification
    passed: bool
    detail: str

class RoadmapItemView(BaseModel):
    id: str
    title: str
    phase: str | None
    owner_project: str | None
    target_contract: str | None
    depends_on: list[str]
    expected_evidence: list[str]   # prose, documentation-only
    computed_status: str
    evidence: list[EvidenceResult]
    blockers: list[str]
    source: str                    # YAML file name

class RoadmapResponse(BaseModel):
    roadmaps: list[str]
    items: list[RoadmapItemView]
    warnings: list[str]
```

#### YAML contract (vault-authored)
```yaml
roadmap: contracts-2026
items:
  - id: RD-001
    title: Promote arbiter decision_id to PolicyDecisionRef v1
    phase: P1
    owner_project: arbiter
    target_contract: PolicyDecision
    depends_on: [RD-000]
    expected_evidence:            # prose — intent documentation only
      - arbiter README documents decision_id
    evidence_rules:               # typed — the only machine-checked part
      - {rule: file_exists, kind: implementation, project: arbiter, path: README.md}
      - {rule: work_item_chain, kind: verification, work_item_id: T-42, min_links: 2}
```

Loading behavior: missing dirs → warning `no roadmap directory found`;
YAML/parse errors, non-mapping items, duplicate ids → warnings, item
skipped (first id occurrence wins). Default location
`<root>/prograph-vault/authored/roadmaps/` per configured root,
overridable via `roadmap_dirs` in dispatcher.toml.

Name resolution (`_EvidenceContext.project_path`): two names resolve
outside collected snapshots — `dispatcher` (own repo root, the dashboard
attests itself) and `prograph-vault` (derived from roadmap dirs). All
others come from discovery snapshots.

**Traces to:** [REQ-001], [REQ-002], [REQ-013]

### DESIGN-002: Evidence rules & status computation ✅

Registry `_RULES: dict[str, handler]`; each handler
`(rule: dict, ctx: _EvidenceContext) -> tuple[bool, str]`.

| Rule | Checks | Evidence source |
|------|--------|-----------------|
| `project_detected` | project resolvable | discovery snapshots |
| `file_exists` | `project/path` exists | filesystem (path-safe join) |
| `sqlite_has_row` | `SELECT EXISTS(<query>)` | read-only SQLite via `read_rows` |
| `contract_in_sync` | canon vs vendored in sync | existing `check_contracts` |
| `work_item_chain` | chain has ≥ min_links | existing `build_work_items` |

Failure containment: unknown rule name, `SourceReadError`, or any handler
exception → `EvidenceResult(passed=False, detail=...)`, never a 500.
Path safety: `_safe_join` rejects absolute paths and `..` escapes
[NFR-004].

Status ladder (`_status_from_evidence` + `_apply_blocked`):

```text
no rules ─────────────────────────────► unknown
impl rules exist, not all passed ─────► planned ──depends_on not
all impl passed ──────────────────────► implemented   implemented+──► blocked
… and all verif passed (≥1) ──────────► verified
```

`blocked` only downgrades `planned` items; evidence wins over
dependencies. `_EvidenceContext` computes contracts and work-item chains
lazily and once per build.

**Traces to:** [REQ-003], [REQ-004], [REQ-005]

### DESIGN-003: API layer (`dispatcher/server/app.py`) ✅

```text
GET /api/roadmap              -> RoadmapResponse
GET /api/roadmap/{item_id}    -> RoadmapItemView | 404
```

Each request re-collects snapshots and rebuilds the read-model (same
freshness semantics as the rest of dispatcher — no caching layer).

M3 additions (planned, pure re-aggregations of `build_roadmap` output):
```text
GET /api/roadmap/phases       -> per-phase counts by status, blocked lists
GET /api/roadmap/blockers     -> reverse dependency view
GET /api/roadmap/drift        -> items joined with contracts sync state
```
Note: aggregation routes must be registered before `/{item_id}` (FastAPI
path matching) or use distinct prefixes.

**Traces to:** [REQ-006], [REQ-010], [REQ-012]

### DESIGN-004: Web dashboard Roadmap section (`server/static/index.html`) ✅

Minimal first screen: `Phase | Item | Owner | Status | Blockers | Evidence`,
rendered from `/api/roadmap`; evidence detail per rule (passed/detail).

**Traces to:** [REQ-007]

### DESIGN-005: TUI Roadmap tab (`dispatcher/tui/app.py`) ✅

`TabPane("Roadmap")` with a `DataTable` mirroring the web columns;
built in the same background refresh cycle as other tabs
(`build_roadmap` alongside snapshots/contracts), 10 s auto-refresh.

**Traces to:** [REQ-008]

### DESIGN-006: VSCode extension Roadmap view (`vscode-ext/`) — planned (M2)

Tree/webview consuming `GET /api/roadmap` via the existing extension
client (settings `dispatcher.url`, auto-start). Same columns and status
icon set as web/TUI; drill-down to per-rule `EvidenceResult`; graceful
empty/unreachable states.

**Traces to:** [REQ-009] → [TASK-101]

### DESIGN-007: Drift & freshness projections — planned (M3)

- **Drift** [REQ-010]: no new mechanics — join items' `target_contract`
  with `check_contracts` results already exposed at `/api/contracts`.
  Only an out-of-sync verdict produces `drift`; "not comparable" leaves
  status unchanged.
- **Freshness** [REQ-011]: `last_seen` on `EvidenceResult` /
  `RoadmapItemView` = mtime of the matched artifact (file, DB); `None`
  where not applicable (`project_detected`).

**Traces to:** [REQ-010], [REQ-011] → [TASK-102], [TASK-103]

## 4. Key Decisions (ADR summary)

| ADR | Decision | Why |
|-----|----------|-----|
| ADR-R1 | Extend dispatcher, no new subproject | avoids another UI/API/collector/drift/ownership point (rec. §3) |
| ADR-R2 | steward is governance, not dashboard | keeps authoring/monitoring/UI boundaries clean (rec. §4) |
| ADR-R3 | Typed rules, closed set, honest `unknown` | trustworthy statuses; no regex guessing (rec. §13.3) |
| ADR-R4 | MVP statuses 4+1, no `in_progress` | partial evidence is noise, not signal (rec. §8) |
| ADR-R5 | `drift` = projection of `/api/contracts` | reuse, not a second sync checker (rec. §8) |
| ADR-R6 | Intent canon in `prograph-vault/authored/roadmaps/` | human-owned KB; `_cowork_output/` is dev-draft only (rec. §11) |

## 5. Directory Map

```text
dispatcher/
├── core/
│   ├── roadmap.py            # DESIGN-001/002 ✅
│   ├── contracts.py          # reused by contract_in_sync / drift
│   ├── correlation.py        # reused by work_item_chain
│   └── collectors/base.py    # read_rows, SourceReadError
├── server/
│   ├── app.py                # DESIGN-003 ✅
│   └── static/index.html     # DESIGN-004 ✅
├── tui/app.py                # DESIGN-005 ✅
vscode-ext/                   # DESIGN-006 (planned)
tests/test_roadmap.py         # 10 tests (M1)
spec/                         # this spec
```
