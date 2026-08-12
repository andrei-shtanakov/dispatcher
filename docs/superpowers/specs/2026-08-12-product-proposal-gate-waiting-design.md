# Read-only `gate_waiting` for impresario product proposals (`@id:product-proposal-gate-waiting`)

**Status:** design approved 2026-08-12. Accepts inbox issue #129 from
impresario (ADR-ECO-006, slug `product-proposal-gate-waiting`); the TODO.md
item under «Product-governance (impresario)» carries the same id.

## Problem

The ecosystem grew a product-governance loop (repo `impresario`):
`Idea → RankedBacklog → QG-4 → researcher↔creator cycle → QG-5 (Gate A/B) →
approved ProductProposal → engineering SDLC`. Every authority decision in it
is human, and none of them has a visibility surface: a proposal waiting at a
gate is visible only to whoever manually opens the workspace. The first live
run (PP-101, 2026-08-12) confirmed it — waiting for a human was an invisible
state.

Dispatcher gets a read-only observation: **which product decisions are
waiting for a human right now**, phase 1 — contract-backed states only.

| Waiting state | Gate | Authority (role) |
|---|---|---|
| `status: ready_for_business`, no active `qg5_business` approve | Gate A | `business_owner` |
| `status: business_approved`, no active `qg5_committee` approve | Gate B | `committee_chair` |

Phase 2 (`needs_human` from the researcher↔creator loop) is blocked on the
impresario side (loop.state is not a contracted artifact) and is out of
scope here.

## Scope

**In scope (phase 1):**

- vendored pinned copies of impresario's `product-proposal/v1` and
  `gate-decision/v1` schemas;
- deterministic discovery and validation of proposal bundles in the
  impresario mirror;
- `gate_waiting` classification with version-matched active-approve
  semantics;
- diagnostics / fail-loud behaviour for a missing mirror, invalid bundles,
  invalid decisions, and identity conflicts;
- a read-only GET endpoint;
- a web panel that leads to the specific proposal/gate and shows why it
  waits.

**Out of scope:** TUI, VSCode, MCP parity; approve/reject/recycle or any
other write action; generalizing the collector for arbitrary artifact
types.

Implementation lands as two PRs (see «Delivery»); phase-1 acceptance closes
only after the web-panel PR.

## Architecture (mirrors WS-005)

Hard boundaries, each mapped to a module:

- `dispatcher/core/collectors/impresario.py` — **discovery only**:
  content-based `detect()` plus a light `ProjectSnapshot`. Classification
  results are never stored in the shared snapshot cache.
- `dispatcher/core/product_proposals.py` — on-demand discovery, schema
  validation and classification. All semantics live here.
- `dispatcher/core/read_api.py` + `GET /api/projects/{name}/product-proposals`
  — the only way frontends reach the core function (same shape as
  `read_api.governance` → `collect_governance`).
- The web panel reads the API only; it never scans the filesystem.

Constraints inherited from WS-005: classification only, producer decides —
dispatcher renders (ARCH-C3/D1); no impresario import, no impresario CLI
execution; shipped code never resolves sibling-repo paths — the core
function takes `mirror_root` as an argument (CON-03); read-only end to end
(the module writes nothing; the route is GET).

**Detection** requires BOTH anchors, not one incidental file:

- `contracts/product-proposal/v1/schema.json`
- `docs/semantics.md`

`SnapshotService` already materializes a negative row
(`ProjectSnapshot(name, path="", detected=False)`) for every registered
collector that detects nothing, and the web overview renders it as a
visible «not detected» card — registering the impresario collector is what
makes a missing mirror a *fleet-level* coverage diagnostic.

## Vendored contracts

Two separate versioned contract directories, one shared pin:

- `contracts/impresario-product-proposal/v1/` and
  `contracts/impresario-gate-decision/v1/`, each with `schema.json`,
  upstream `fixtures/` (valid + invalid), `manifest.json` (per-file sha256,
  `tree_sha256`, `producer_commit`) and `PINNED.txt`;
- ONE re-vendor script, `scripts/revendor_impresario_contracts.sh <commit>`,
  regenerates BOTH directories from that single impresario commit;
- copy-integrity test in the PR gate verifies both directories against
  their manifests AND asserts the two manifests' `producer_commit` are
  equal — pin and provenance are machine-readable, so the two contracts can
  never silently mix versions;
- a scheduled upstream-drift workflow covers both contracts. Drift itself
  is advisory; a failure of the job's own machinery (checkout, script,
  integrity check) is red — «advisory» applies to detected upstream drift,
  not to a broken check.

The pin is chosen at implementation time (PR-1) — the then-current
impresario commit; the PP-101 test fixture (below) is copied at the same
commit.

## Bundle discovery contract

- Recursively search for `proposal.yaml` files under the impresario mirror
  root; the directory of a found file is a proposal-bundle root.
- Decisions are read ONLY from `<bundle>/decisions/*.yaml`.
- Exclusions apply to ANY path segment, recursively: segments starting with
  `.` or `_`, and any segment equal to `contracts`.
- Directory symlinks are not followed; a found `proposal.yaml` must still
  be inside `mirror_root` after `resolve()`.
- Bundles are sorted by normalized relative path; the scan never leaves the
  mirror root.

Fail-loud behaviour:

- a found `proposal.yaml` that fails the contract yields a diagnosable
  bundle row, never disappears silently;
- broken or invalid decision files are visible in diagnostics;
- duplicate proposal identities are a conflict, never arbitrary dedup;
- a directory-walk error must not read as «0 bundles»: it becomes a
  report-level diagnostic (which files exist is unknown);
- the single expected impresario mirror being absent is an explicit
  coverage error (the fleet-level «not detected» card + the API behaviour
  below);
- zero discovered bundles with a healthy mirror and a completed scan is an
  explicit «0 bundles» state, not an error.

Discovery therefore does not depend on the current
`pilot/forconcept/pp-101/` layout, while staying anchored to the semantic
markers `proposal.yaml` and `decisions/`.

## Read model

```python
GateId = Literal["qg5_business", "qg5_committee"]

class Diagnostic(BaseModel):
    code: str                    # stable enum below — the API contract
    message: str                 # human-readable; NOT part of the contract
    path: str | None = None      # mirror-relative file/dir when applicable

class GateWait(BaseModel):       # one «waiting for a human» record
    proposal_id: str             # "PP-101"
    gate_id: GateId
    gate_label: str              # "Gate A" | "Gate B" (static mapping)
    authority: str               # "business_owner" | "committee_chair"
    artifact_ref: str            # "proposal://PP-101"
    bundle_path: str             # bundle dir, relative to the mirror root
    version: int                 # current proposal version
    proposal_updated_at: str     # proposal.updated_at; UI label
                                 # "Proposal updated" — NOT a proven
                                 # wait-start time, so not named "since"
    # dedup identity = (proposal_id, gate_id, version): re-passing a gate
    # after recycle is a NEW wait, not a duplicate

class ProposalBundle(BaseModel): # every discovered bundle, lossless
    path: str                    # relpath of the proposal.yaml directory
    state: Literal["ok", "unreadable", "unknown", "conflict"]
    diagnostics: list[Diagnostic]   # ALL collected problems, not one
    proposal_id: str | None      # filled when the proposal is readable
    status: str | None
    version: int | None
    updated_at: str | None
    waits: list[GateWait]        # 0..1 in phase 1; computed ONLY for "ok"

class ProductProposalsReport(BaseModel):
    mirror_path: str
    bundles: list[ProposalBundle]   # sorted by normalized path
    waits: list[GateWait]           # aggregate over "ok" bundles
    diagnostics: list[Diagnostic]   # report-level (walk-error, mirror-*)
    attention: bool                 # any non-ok bundle OR any report-level
                                    # diagnostic; a plain GateWait does NOT
                                    # raise attention — waiting is expected
                                    # business work, not a read defect
```

`state` is derived from the collected diagnostics, highest priority first:

1. `conflict` — global `proposal_id` conflict (earlier diagnostics are
   preserved on the row, the conflict diagnostic is added);
2. `unreadable` — a proposal-level diagnostic;
3. `unknown` — at least one decision/supersedes-level diagnostic;
4. `ok` — no diagnostics.

For `unknown`, `unreadable` and `conflict`, `waits: []` means «result
suppressed», never «nothing waits» — frontends must render it as a state.

Determinism: `bundles` sorted by normalized path; `waits` by
`(proposal_id, gate_id, version, bundle_path)`; `diagnostics` by
`(path or "", code, message)`. Repeated calls on an unchanged mirror
produce byte-identical JSON.

## Classification semantics

For an `ok` bundle:

- `status == "ready_for_business"` → waits for Gate A unless an active
  `qg5_business` approve exists;
- `status == "business_approved"` → waits for Gate B unless an active
  `qg5_committee` approve exists;
- every other status (`draft`, `in_iteration`, `approved`, `on_hold`,
  `killed`) → no waits.

**Active approve** — a decision record in this bundle's `decisions/` that
simultaneously:

- has `decision == "approve"`;
- matches `gate_id`;
- has `subject.kind == "product_proposal"` and
  `subject.ref == "proposal://<proposal_id>"`;
- has `subject.version == proposal.version` (**version-matched**);
- is not superseded: its `decision_id` is not referenced by the
  `supersedes` field of any other record in the bundle.

Rationale, fixed with the impresario semantics (`docs/semantics.md` is the
SSOT): a decision for another subject version cannot extinguish the current
gate wait. After a recycle, the old Gate A approve remains valid historical
evidence but not an active permission for the new version. Torn writes are
treated conservatively: if a version-matched approve is already recorded,
the wait is NOT shown even when the proposal status has not caught up yet —
consistent with the dedup identity `proposal_id + gate_id + version` and
preventing false `gate_waiting`.

Two regression scenarios are pinned by tests:

- **recycle**: `ready_for_business` at version 8 plus a non-superseded
  `qg5_business` approve at version 6 → the Gate A wait IS shown;
- **approve-before-status-update**: `ready_for_business` at version N plus
  an active `qg5_business` approve at version N → the wait is NOT shown.

Supersession rules:

- the supersedes check runs only over a fully validated decision set; any
  invalid decision file puts the whole bundle in `unknown`;
- a direct incoming-reference check is sufficient: supersession is
  irreversible — if B supersedes A, later superseding B does not revive A;
- a `supersedes` pointing at an absent `decision_id` → `unknown`
  (decision-history integrity unproven);
- self-supersession or a cycle → `unknown`;
- `decision_id` uniqueness is checked bundle-wide before classification;
- decisions with another `subject.ref`, `subject.kind`, `subject.version`
  or `gate_id` are valid historical evidence and do not affect the current
  wait.

Error-collection rules:

- ALL available `decisions/*.yaml` are validated even after the first
  failure; all found problems are returned;
- waits are suppressed on ANY bundle-level diagnostic;
- if `proposal.yaml` itself cannot be parsed, decisions are not classified
  semantically (there is no trusted subject), but their read errors are
  still collected;
- conflicts: all bundles are parsed first, then the global `proposal_id`
  check runs; every participant of a conflict receives the identical,
  deterministically ordered list of participant paths.

YAML parsing uses a strict loader that REJECTS duplicate mapping keys —
plain `yaml.safe_load` silently keeps the last value, which breaks
fail-closed for `decision_id`, `subject.version`, `gate_id`, `status`.

## Diagnostic code taxonomy

Codes are the stable public API contract; messages are not.

**Bundle-level** (`ProposalBundle.diagnostics[].code`):

| Code | Resulting state | Condition |
|---|---|---|
| `proposal-unreadable` | `unreadable` | OSError / non-UTF-8 / not YAML / not a mapping |
| `proposal-schema-invalid` | `unreadable` | fails vendored `product-proposal/v1` |
| `decision-unreadable` | `unknown` | a `decisions/*.yaml` unreadable / not YAML |
| `decision-schema-invalid` | `unknown` | fails vendored `gate-decision/v1` |
| `decision-id-duplicate` | `unknown` | `decision_id` repeated in the bundle |
| `supersedes-dangling` | `unknown` | `supersedes` targets an absent `decision_id` |
| `supersedes-cycle` | `unknown` | self-supersede or a cycle |
| `proposal-id-conflict` | `conflict` | `proposal_id` seen in ≥ 2 bundles |

**Report-level** (`ProductProposalsReport.diagnostics[].code`):

| Code | Condition |
|---|---|
| `walk-error` | a directory could not be listed — which files exist is unknown |
| `mirror-anchors-missing` | a previously detected mirror (non-empty `snap.path`) lost an anchor |
| `mirror-not-detected` | the snapshot row is `detected=False` — no scan is attempted, `Path("")` is never resolved |

**HTTP 404 body** (`{code, message}`): `project-not-found` (unknown
`{name}` — no interpretation of the cause), `not-impresario-mirror` (a
known project that is not the impresario mirror).

An absent `decisions/` directory is NOT an error: a valid bundle with no
decisions (`ok`; e.g. a fresh `ready_for_business` with no decision at all
waits for Gate A).

## API

`GET /api/projects/{name}/product-proposals`, GET only. `read_api`
resolves the project from the snapshot cache, then calls
`collect_product_proposals(Path(snap.path))` — a fresh scan per request,
nothing cached. Case split:

- `{name}` not in the cache → **404** `project-not-found`;
- `{name}` known, `snap.name != "impresario"` → **404**
  `not-impresario-mirror` (panel hides the section);
- `snap.name == "impresario"`, `detected=False` / empty path → **200**,
  `bundles: []`, report diagnostic `mirror-not-detected`,
  `attention: true` — safe under direct request, no scan;
- `snap.name == "impresario"`, detected, but an anchor is gone from
  `snap.path` (re-checked before every scan — content check instead of a
  hardcoded name; also catches «mirror vanished after discovery») →
  **200**, `bundles: []`, `mirror-anchors-missing`, `attention: true` —
  the panel shows an error, it does not hide;
- healthy mirror, completed scan, nothing found → **200**, `bundles: []`,
  `waits: []`, no diagnostics;
- invalid bundles / identity conflicts → **200 partial result**: per-bundle
  states + diagnostics represent partiality honestly, `attention: true`;
  nothing is dropped silently — every found `proposal.yaml` produces a
  `bundles` row. No 422 exists on this surface.

## Web panel

A per-project section in `server/static/index.html`, same pattern as the
governance panel (fetch on project selection):

- waits table: proposal, gate label (Gate A/B), authority, version,
  «Proposal updated» (`proposal_updated_at`), bundle path — the navigation
  to the specific proposal/gate;
- a local mirror path is NEVER turned into an href: `artifact_ref` and the
  relative bundle path render as copy-friendly text. A safe repository-URL
  link may come later, only if a reliable URL source appears — separate
  item;
- bundle list with state badges and `code: message` per diagnostic; every
  non-`ok` bundle shows an explicit «classification suppressed —
  unknown/unreadable/conflict» wording so that `waits: []` is visually
  impossible to mistake for «0 gates waiting»;
- «0 gates waiting» and «0 bundles» are explicit, distinct labels;
- non-`ok` states and report diagnostics get attention styling; a plain
  wait does not;
- 404 `not-impresario-mirror` hides the section; `mirror-anchors-missing`
  and `mirror-not-detected` show a panel error instead of hiding;
- fetch-race guard: the panel keeps a token of the current project
  selection and drops responses with a stale token, so a late response
  from the previous project never renders into the new panel.

## Fail-closed invariants (each one is a test)

1. No read/parse/validation error path may produce `state="ok"` or an
   empty `waits` that reads as «nothing waits»: a non-`ok` bundle always
   carries diagnostics, and its `waits: []` means «suppressed».
2. `walk-error` never silently shrinks the report: diagnostic +
   `attention`.
3. Classification is deterministic (orderings above): repeated runs on an
   unchanged mirror are byte-identical.
4. The collector is read-only: no mutation path exists; verified by
   comparing the full file-path set AND content hashes of the fixture tree
   before/after a collect run (creation of new files/dirs counts as a
   violation, not just modification).
5. `detected=False` / empty `snap.path` never reaches `Path.resolve()`.

## Testing & acceptance

**Fixtures.** `tests/fixtures/product_proposals/`: synthetic bundles plus a
pinned copy of the real PP-101 bundle (proposal.yaml v8 `approved` +
gd-001/gd-002) with a provenance note naming the impresario source path and
commit (same commit as the contract pin). Scenario mutations are built
programmatically in `tmp_path` from the pinned copy, so acceptance reads
literally like issue #129.

**Acceptance (verbatim from #129):**

1. PP-101 copy with `status: ready_for_business` and GD-001 **removed from
   the decisions set** (deleted — not marked, not corrupted; all other
   decisions retained) → exactly one record
   `(qg5_business, business_owner, proposal://PP-101)`.
2. The true approved PP-101 copy → `ok`, zero waits.
3. PP-101 copy with an unreadable decision file → `unknown` (+
   `decision-unreadable`), NOT «zero waits». Unreadability is modeled as
   invalid UTF-8 bytes (stable in CI, unlike chmod); a separate test
   patches the read path to raise `OSError` for the I/O-error branch.

**Pinned semantics regressions:** the recycle and
approve-before-status-update scenarios from «Classification semantics».

**Unit layers:** one test per diagnostic code; supersession (superseded
approve → wait present; dangling / self / cycle → `unknown`); Gate B
symmetric to Gate A; discovery (recursive segment exclusions, symlinked
dir not followed, `resolve()` escape rejected, deterministic order,
walk-error → report diagnostic); `proposal_id` conflict (all participants
`conflict`, identical deterministic path list, earlier diagnostics
preserved); duplicate YAML keys rejected; the read-only invariant (path
set + hashes).

**API tests:** 404 `project-not-found`; 404 `not-impresario-mirror`; 200
`mirror-not-detected` (detected=False, no scan); 200
`mirror-anchors-missing`; 200 empty mirror; 200 partial + `attention`.

**Vendor tests:** copy-integrity of both directories against their
manifests (pattern: `test_gate_verdicts_vendor.py`); equality of the two
manifests' `producer_commit`; re-vendor script test (pattern:
`test_revendor_steward_script.py`). Scheduled upstream-drift job for both
contracts (advisory for drift; red on its own machinery failing).

**Web panel JS tests:** `tests/web/product_proposals_harness.js`, the
governance-harness discipline — the REAL `<script>` from the shipped
`index.html` runs under Node over the dependency-free DOM; Node is a hard
prerequisite (missing node FAILS, never skips). Asserted: a non-`ok`
bundle never renders as «nothing waits» (M-01 analogue); a wait is
readable off one screen (proposal, gate, authority, «Proposal updated»);
404 `not-impresario-mirror` hides the section while `mirror-anchors-missing`
shows an error; the fetch-race guard never renders a stale project.

**Live smoke (WS-005 precedent):** a script checks out the real impresario
at exactly the shared `producer_commit` from the manifests (clone/worktree
into tmp) and first verifies PP-101 exists at that commit — its absence
FAILS with an explicit provenance error, never skips. Then the HTTP
surface is exercised end-to-end against the checkout and the test asserts
the serialized public response — `attention`, `diagnostics`, `bundles`,
`waits` — not just the core function's return value. Expected: PP-101
`ok`, zero waits. Hard prerequisite in its CI job, like the steward smoke.

## Delivery

- **PR-1** `feat/pp-gate-waiting-collector`: vendored contracts + re-vendor
  script + drift workflow + `core/product_proposals.py` +
  `collectors/impresario.py` + `read_api` + GET route + all non-JS tests +
  live smoke. The TODO.md item stays `[ ]` and gains PR-1/API evidence in
  its body.
- **PR-2** `feat/pp-gate-waiting-panel`: the `index.html` section +
  fetch-race guard + Node-harness JS tests. Phase-1 acceptance closes only
  here, after the Node harness and web acceptance pass: the TODO item goes
  `[x]` with both PR numbers; issue #129 is closed by a human after the
  merge.

## Follow-ups (explicitly out of scope, recorded so they are not lost)

- Retrofit the fetch-race guard into the existing governance panel (same
  file, same pattern) — small separate change.
- TUI / VSCode / MCP parity for `product_proposal` — a separate plan item.
- Phase 2: `needs_human` from the researcher↔creator loop — blocked on
  impresario contracting loop.state (recorded on the impresario side).
- A safe repository-URL link from the panel to the proposal, if a reliable
  URL source appears.
