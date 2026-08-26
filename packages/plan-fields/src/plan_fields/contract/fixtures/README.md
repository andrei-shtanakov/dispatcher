---
title: "plan-fields v3 — conformance fixtures"
type: contract
status: proposed
owner: Andrei
updated: 2026-08-26
---

# plan-fields v3 — fixtures

Normative input → expected-output pairs. They are the machine-checkable spec; the executable
validator that runs them is a **PF-3** deliverable (not in the KB — see the contract README).

## Conventions

- **Simple cases** are a pair: `<case>.md` (input `TODO.md`) + `<case>.expected.json`
  (canonical output per `../schema.json`). Each is parsed **as repo `demo`** at
  `generated_at = 2026-07-28T00:00:00Z`, and `path` in provenance is `TODO.md`.
- **History-dependent cases** cannot be proven from a single current file, so they are a
  bundle directory: `previous.md`, `current.md`, `context.json` (which snapshot is which +
  the retirement fact), and `expected.json` (the diagnostics for `current` given `previous`).
  Only `reused-id/` needs this today; a confirmed removed-tombstone would too.

## Cases

| Case | Kind | Expected |
|---|---|---|
| `valid/basic` | pair | 2 legacy-role nodes, 1 resolved reference + edge, transitional owner diagnostics |
| `valid/typed-owner` | pair | all four typed owner principal forms project to `owner_ref` |
| `valid/tombstone` | pair | closed `@id` item → `tombstone: true`, transitional owner diagnostics |
| `invalid/duplicate-id` | pair | `PF-ID-DUPLICATE` (error) |
| `invalid/missing-id` | pair | `PF-ID-MISSING` (warning); no node (an id is required) |
| `invalid/dangling-id` | pair | canonical ref to an absent `@id` → `PF-ID-DANGLING`; reference kept, no edge |
| `invalid/ambiguous-legacy-ref` | pair | `PF-LEGACY-AMBIGUOUS`; `legacy_blocker_ref` kept, no edge |
| `invalid/reused-id` | bundle | retired `@id` reused → `PF-ID-REUSED` (error) |
| `valid/epic-tagged` | pair | the stream axis in normal form: `@epic` on both items, `@defect` on the fix; `epic_classification: tagged` |
| `invalid/epic-grammar` | pair | `@epic:eco` (no dot) → `EP-GRAMMAR`, `epic_classification: invalid` |
| `invalid/epic-multiple` | pair | two identical `@epic` tags → `EP-MULTIPLE`; a duplicate is a defect, not a consensus |
| `valid/dag-registered` | pair | `@dag:dags/<id>.yaml` in normal form → node carries `dag` (r2) |
| `valid/dag-id-stem-agreement` | pair | 64-char boundary `@id` with full charset; the `@id` grammar and the dag filename stem agree |
| `valid/dag-continuation-invisible` | pair | `@dag` on a continuation line is invisible — no `dag` field, no diagnostic |
| `invalid/dag-without-id` | pair | `@dag` on a line with no `@id` → no node, `PF-ID-MISSING` only (the `@epic` precedent: no identity to attach a dag diagnostic to) |
| `invalid/dag-name-mismatch` | pair | well-formed value ≠ `dags/<id>.yaml` → `PF-DAG-MISMATCH`; `raw` keeps the spelling |
| `invalid/dag-traversal` | pair | `dags/../../…` → `PF-DAG-GRAMMAR`; traversal dies in the grammar, before any filesystem |
| `invalid/dag-quoted` | pair | quoted value → `PF-DAG-GRAMMAR`; the grammar takes a bare token |

Expected outputs follow the canonical ordering in the contract README (nodes by `node_id`,
etc.), so a conforming parser's output should compare equal after canonicalization.

## What these fixtures deliberately cannot prove (v3)

Every pre-existing case now also carries `EP-MISSING` on its open nodes: those inputs were
written before the stream axis existed, and a conforming v3 parser must say so rather than
stay silent. Closed and tombstoned items get no such diagnostic — they carry no obligation.

No pre-r2 fixture gained a `@dag`-related diagnostic: the tag is an optional extension and
its absence is never a finding, so every pre-existing expected output is byte-identical to
its pre-r2 state — that is the additivity the r2 revision promises.

No fixture here expects `EP-UNKNOWN` or `EP-MOVED`. Those need `epics.toml`, which a
single-repo parser does not read; they are fleet-layer findings and are fixtured in
`../../epics/v1/fixtures/`. A v3 fixture asserting them would be asserting a capability this
layer does not have.
