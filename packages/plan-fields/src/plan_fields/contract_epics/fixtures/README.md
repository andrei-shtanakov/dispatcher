---
title: "epics v1 — conformance fixtures"
type: contract
status: proposed
owner: Andrei
updated: 2026-08-25
---

# epics v1 — fixtures

Normative input → expected-output pairs. They are the machine-checkable spec; the executable
validator that runs them is a **Ф1a** deliverable in `dispatcher/packages/plan-fields`, not
in the KB (see the contract README).

## Conventions

Every artifact case is resolved against **`registry.toml`** in this directory — the shared
baseline, which doubles as the `registry/valid` case (validating it must yield zero
diagnostics).

Fixed identities, so expected outputs are byte-comparable:

| | value |
|---|---|
| repo | `demo` |
| observation time | `2026-08-25T00:00:00Z` |
| commit | `git://demo/0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c` |
| pull request | `gh://demo/pull/7` |
| issue | `gh://demo/issues/11` |

- **Simple cases** are a pair: `<case>.md` (a `TODO.md` input) or `<case>.txt` (a commit
  message / PR body / issue body) plus `<case>.expected.json` (one classification object per
  `../classification.schema.json`).
- **Bundle cases** are a directory with a `context.json` — the facts a parser cannot read out
  of the text itself (attribution to a pull request, when an artifact was opened, whether a
  body was retrieved at all) — plus the input and `expected.json`.
- **Registry cases** live in `registry/` and are `<case>.toml` + `<case>.expected.json`,
  where the expected object is `{"diagnostics": [...]}`.
- Diagnostic message strings are pinned and normative; a conforming implementation emits them
  verbatim so fixtures compare equal without fuzzy matching.

## Artifact cases

| Case | Kind | Expected |
|---|---|---|
| `valid/todo-tagged` | pair | `tagged`, no defect |
| `valid/todo-defect` | pair | `tagged` + orthogonal `defect: pipeline` |
| `valid/commit-trailer` | pair | trailer block alongside `Co-Authored-By` parses; `tagged` |
| `valid/pr-body` | pair | PR body trailer; `tagged` |
| `valid/prose-mention-is-not-a-trailer` | pair | an `Epic:` line mid-prose is ignored; only the final trailer block classifies |
| `valid/commit-inherited` | bundle | no trailer of its own → inherits the attributed PR's epic, `inherited_from` set. The subject's `(#7)` is **not** the evidence — the attribution projection is |
| `valid/historical-moved` | bundle | opened before `moved_at` → retired id accepted, resolved to the final id, `resolved_from` kept |
| `invalid/todo-missing` | pair | `EP-MISSING` (warning — observation precedes `missing_error_after`) |
| `invalid/todo-grammar` | pair | `eco` (no dot) → `EP-GRAMMAR` |
| `invalid/todo-grammar-case` | pair | `Eco.Ops` → `EP-GRAMMAR`; values are never case-folded into a match |
| `invalid/todo-unknown` | pair | `eco.dark-factroy` → `EP-UNKNOWN` (the typo guard) |
| `invalid/todo-multiple` | pair | two identical `@epic` tags → `EP-MULTIPLE`; a duplicate is a defect, not a consensus |
| `invalid/todo-defect-unknown` | pair | epic stays `tagged`; `EP-DEFECT-UNKNOWN` — the defect axis fails independently |
| `invalid/todo-defect-grammar` | pair | epic stays `tagged`; `EP-DEFECT-GRAMMAR` |
| `invalid/todo-defect-multiple` | pair | epic stays `tagged`; `EP-DEFECT-MULTIPLE` — the defect axis has the same multiplicity rule as the epic |
| `invalid/pr-moved` | bundle | opened after `moved_at` → `EP-MOVED` |
| `invalid/conflict` | bundle | PR and its linked plan item disagree → `EP-CONFLICT`, no winner chosen |
| `invalid/issue-unavailable` | bundle | body never retrieved → `unavailable` + `EP-UNAVAILABLE`, **never** `missing` |

## Registry cases

Seven of the eleven defects are expressible in `registry.schema.json` and are rejected by schema
validation alone; four are **referential** — they hold between keys and cannot be stated in
the schema, so only a semantic validator finds them. Both halves are normative, and a
conforming validator must report the diagnostic in either case:

| Case | Schema | Expected |
|---|---|---|
| `registry/valid` (= `registry.toml`) | accepts | no diagnostics |
| `registry/moved-with-status` | rejects | `EP-REG-MOVED-STATUS` |
| `registry/opened-missing` | rejects | `EP-REG-OPENED-MISSING` |
| `registry/closed-missing` | rejects | `EP-REG-CLOSED-MISSING` |
| `registry/kind-unknown` | rejects | `EP-REG-KIND-UNKNOWN` |
| `registry/standing-fields` | rejects | `EP-REG-STANDING-FIELDS` — a standing epic carrying `goal`/`closed` |
| `registry/policy-invalid` | rejects | `EP-REG-POLICY-INVALID` (ratio > 1, missing threshold, malformed date) |
| `registry/malformed-section` | rejects | `EP-REG-MALFORMED` — `epics` is a string, so no entry-level finding is even reachable |
| `registry/program-unknown` | accepts | `EP-REG-PROGRAM-UNKNOWN` — referential |
| `registry/moved-dangling` | accepts | `EP-REG-MOVED-DANGLING` — referential |
| `registry/moved-chain` | accepts | `EP-REG-MOVED-CHAIN` — referential |
| `registry/moved-cycle` | accepts | `EP-REG-MOVED-CYCLE` — referential, one diagnostic per member of the cycle |

## Deliberately unfixtured

`EP-REG-PROGRAM-IN-USE` is **history-dependent**: a program removed while history still
references it cannot be proven from one registry snapshot — the snapshot simply lacks the
program. Its single-snapshot shadow is `EP-REG-PROGRAM-UNKNOWN`, which is fixtured. Add a
bundle case (`previous.toml` + `current.toml`) when a validator gains snapshot history, the
way plan-fields does for `PF-ID-REUSED`.
