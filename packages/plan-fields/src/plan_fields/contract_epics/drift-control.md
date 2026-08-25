---
title: "epics v1 — drift control"
type: contract
status: proposed
owner: Andrei
updated: 2026-08-25
---

# epics v1 — drift control

Normative drift-control for the `epics` contract. The vault owns the **data** that makes
drift machine-detectable; the **executable checkers** live in code repos (this is a knowledge
base, not a code project — see the root `CLAUDE.md`). The policy is written before the first
consumer vendors anything, so no consumer pins the contract without a guard — the same
discipline plan-fields v2 adopted in PF-6.

## Canonical surface

The frozen contract surface is every file under `authored/contracts/epics/v1/` **except** the
drift-control meta files (`manifest.json`, `drift-control.md`) and the vendor-only
`PINNED.txt`. Exactly these files are what a consumer vendors and depends on.

`manifest.json` is the canonical fingerprint of that surface: `contract_version`, one
`sha256` per file (sorted by path), and a `tree_sha256` rollup over
`"<relpath>\0<filehash>\n"` lines — byte-identical in construction to plan-fields v2, so one
checker implementation serves both contracts.

## Two guarantees, never folded into one verdict

1. **Integrity — guarantee A (required, offline).** A consumer recomputes the hashes of its
   vendored surface and compares them with the `manifest.json` it vendored, checking the file
   set and the manifest against each other in both directions. No vault checkout, no network,
   so it belongs in ordinary required CI. It proves the copy is intact; it proves **nothing**
   about whether canon has moved.
2. **Upstream drift — guarantee B (advisory, needs canon).** Where a canon checkout is
   available, the checker recomputes canon's surface and compares it with the vendored copy,
   recording the remote, the requested ref, the resolved commit and both tree hashes — so a
   green run says no drift *from what*. This is an **observation, not a gate**: schedule it,
   never make it a required pull-request check, because a commit in one repository must not
   redden another's PRs. "No canon was available" is **unknown**, never "in sync".

## Pin linkage — the epics/v1 ↔ plan-fields join

This contract is unusual among the KB's contracts: it is consumed **through** another one.
`plan-fields` v3 carries `epic` and `defect` on its operational node and delegates their
grammar here rather than restating it.

A consumer that vendors both **must** record the linkage machine-readably next to the pins —
`epics/v1` version, source commit, and `tree_sha256` — and its conformance suite must verify
that the vendored plan-fields copy declares the same `epics/v1` fingerprint it actually
vendored. Delegation that is only documented in prose becomes a second copy of the regex
within months; delegation that is pinned and checked cannot.

## Compatibility policy

A surface change is classified before it lands:

- **Additive (minor).** Backward-compatible: a new **optional** schema property, a new
  diagnostic **code**, a new fixture, additional prose. Existing consumers keep validating.
  Bump `manifest.json`; `contract_version` stays `v1`.
- **Breaking (major).** Removing or renaming a schema property or diagnostic code, tightening
  an optional field to required, changing the epic/defect grammar, adding or removing a
  `classification` state, or altering an existing fixture's `expected.json`. Breaking changes
  require a **new contract version** (`v2`), never an in-place `v1` edit.

Note what is deliberately **not** a contract change: adding a program, an epic, a defect class,
or moving a coverage threshold. Those are registry values in the umbrella (`epics.toml`), they
change weekly by design, and they are read live precisely so that ordinary stream bookkeeping
never touches a pinned surface.

Fixtures are the executable spec: any change to an `*.expected.json` is treated as breaking
unless it is a pure addition of a new case.

## Warn → error escalation

`diagnostics.yaml` states each code's `escalation`. Two of the three values behave as in
plan-fields (`immediate`, `never`); the third, `policy_date`, is specific to adoption:
`EP-MISSING` stays a warning until `coverage_policy.missing_error_after` and hardens per
plane, per ADR-ECO-010 D8. The date lives in the registry, not here — hardening the fleet is
an operational decision, not a contract revision.

## Consumer registry

Consumers vendor a pinned copy and are listed here as they land, so guarantee B knows whom a
canon change affects:

| Consumer | Vendors | Status |
|---|---|---|
| `dispatcher/packages/plan-fields` | grammar + diagnostics, via plan-fields v3 | planned (Ф1a) |
| `github-checker` | grammar + classification schema (`snapshot/v2`) | planned (Ф2) |
| `ai-orchestrators-workspace` (CI sensor) | diagnostics + registry schema | planned (Ф1b) |
| `robin-runtime` | classification schema (read-only consumer of published snapshots) | planned (Ф5) |

An entry is added when the vendored copy lands, not when the phase is planned — a registry of
intentions would report drift against consumers that do not exist.
