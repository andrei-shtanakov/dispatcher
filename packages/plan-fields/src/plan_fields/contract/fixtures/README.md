---
title: "plan-fields v1 — conformance fixtures"
type: contract
status: proposed
owner: Andrei
updated: 2026-07-28
---

# plan-fields v1 — fixtures

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
| `valid/basic` | pair | 2 nodes, 1 resolved reference + edge, 0 diagnostics |
| `valid/tombstone` | pair | closed `@id` item → `tombstone: true`, 0 diagnostics |
| `invalid/duplicate-id` | pair | `PF-ID-DUPLICATE` (error) |
| `invalid/missing-id` | pair | `PF-ID-MISSING` (warning); no node (an id is required) |
| `invalid/dangling-id` | pair | canonical ref to an absent `@id` → `PF-ID-DANGLING`; reference kept, no edge |
| `invalid/ambiguous-legacy-ref` | pair | `PF-LEGACY-AMBIGUOUS`; `legacy_blocker_ref` kept, no edge |
| `invalid/reused-id` | bundle | retired `@id` reused → `PF-ID-REUSED` (error) |

Expected outputs follow the canonical ordering in the contract README (nodes by `node_id`,
etc.), so a conforming parser's output should compare equal after canonicalization.
