---
title: "plan-fields v1 — drift control"
type: contract
status: proposed
owner: Andrei
updated: 2026-07-28
---

# plan-fields v1 — drift control (PF-6)

Normative drift-control for the `plan-fields` contract. The vault owns the
**data** that makes drift machine-detectable; the **executable checkers** live
in code repos (this is a knowledge base, not a code project — see the root
`CLAUDE.md`). This artifact exists **before** the migration (PF-7) so no consumer
pins the contract without a guard, mirroring the two-plane discipline of
ADR-ECO-005.

## Canonical surface

The frozen contract surface is every file under
`authored/contracts/plan-fields/v1/` **except** the drift-control meta files
(`manifest.json`, `drift-control.md`) and the vendor-only `PINNED.txt`. Exactly
these files are what a consumer vendors and depends on.

`manifest.json` is the canonical fingerprint of that surface: `contract_version`,
one `sha256` per file (sorted by path), and a `tree_sha256` rollup over
`"<relpath>\0<filehash>\n"` lines. It is regenerated whenever the surface
changes and is the single source of truth for "what canon currently is".

## Why a manifest (not just a commit ref)

`PINNED.txt` in a vendored copy records the source **commit** — which a consumer
cannot verify without checking out the vault. `manifest.json` records the source
**content**, so a consumer verifies its vendored copy is byte-identical to the
pin **offline**, from its own repo alone. The manifest travels with the vendored
copy alongside `PINNED.txt`.

## How checkers consume this (executable half, code repos)

Two comparison modes, both reading `manifest.json` — no bespoke per-consumer
logic:

1. **Live compare (dispatcher).** Where the vault and the consumer are both
   checked out (as in `dispatcher/core/contracts.py` `_catalog_drift` for the
   agents-catalog), the checker recomputes the vendored surface hashes and
   compares to the canonical `manifest.json`, emitting one `contract_in_sync`
   verdict for the `plan-fields` contract. This is the PF-6 code companion and
   the rule PF-6's verification eventually turns green on.
2. **Offline verify (any consumer's own CI).** A consumer with only its own repo
   recomputes the hashes of its vendored `contract/` surface and compares to the
   `manifest.json` it vendored. No vault checkout required.

Neither mode is defined here as code: this file is the contract they honor.

## Compatibility policy

A surface change is classified before it lands:

- **Additive (minor).** Backward-compatible: a new **optional** schema property,
  a new diagnostic **code**, a new fixture, additional prose. Existing consumers
  keep validating. Bump `manifest.json`; `contract_version` stays `v1`.
- **Breaking (major).** Removing or renaming a schema property or diagnostic
  code, tightening a previously optional field to required, changing canonical
  JSON node/edge ordering, or altering an existing fixture's `expected.json`.
  Breaking changes require a **new contract version** (`v2`), never an in-place
  `v1` edit — consumers migrate deliberately.

Fixtures are the executable spec: any change to an `*.expected.json` is treated
as **breaking** unless it only *adds* a fixture pair, because a changed
expectation silently reinterprets conforming input.

## Escalation (warn → error)

Drift severity mirrors the diagnostics two-snapshot escalation
(`diagnostics.yaml`, `escalation: second_independent_snapshot`):

- First snapshot observing a vendored copy diverge from `manifest.json` →
  **warning** (a copy may be mid-re-vendor).
- A **second independent snapshot** (new scan, ≥1 completed run between; not a
  rerun) still diverging → **error**. Sustained drift is a real defect.

A `manifest.json` that is itself stale relative to the canonical surface (canon
files changed, manifest not regenerated) is always an **immediate error** — the
guard must never certify a fingerprint it no longer matches.

## Consumer registry

Vendored copies under drift control (each pins a canonical commit in its
`PINNED.txt` and vendors the surface above):

| Consumer | Vendored path | Notes |
|---|---|---|
| dispatcher (plan-fields package) | `dispatcher/packages/plan-fields/src/plan_fields/contract/` | first consumer (PF-3); PINNED at the PF-1 canon commit |

Future consumers (devtools, atp-platform, robin-runtime, umbrella sensor) are
added here as PF-7 migrates them onto the package.
