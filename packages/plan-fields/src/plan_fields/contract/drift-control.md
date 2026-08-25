---
title: "plan-fields v3 — drift control"
type: contract
status: proposed
owner: Andrei
updated: 2026-08-25
---

# plan-fields v3 — drift control (PF-6)

Normative drift-control for the `plan-fields` contract. The vault owns the
**data** that makes drift machine-detectable; the **executable checkers** live
in code repos (this is a knowledge base, not a code project — see the root
`CLAUDE.md`). This artifact exists **before** the migration (PF-7) so no consumer
pins the contract without a guard, mirroring the two-plane discipline of
ADR-ECO-005.

## Canonical surface

The frozen contract surface is every file under
`authored/contracts/plan-fields/v3/` **except** the drift-control meta files
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

Two comparisons, both reading `manifest.json` — no bespoke per-consumer logic.
They answer **different questions** and must not be folded into one verdict
(dispatcher#99):

1. **Integrity — guarantee A (required, offline).** A consumer recomputes the
   hashes of its vendored `contract/` surface and compares them to the
   `manifest.json` it vendored, checking the file set and the manifest against
   each other in both directions. No vault checkout, no network — so it belongs
   in a consumer's ordinary required CI. This is the comparison PF-6's
   `contract_in_sync` evidence resolves. It proves the copy is intact; it proves
   **nothing** about whether canon has moved.
2. **Upstream drift — guarantee B (advisory, needs canon).** Where a canon
   checkout is available, the checker recomputes canon's surface and compares it
   to the vendored copy, recording the remote, the requested ref, the resolved
   commit and both tree hashes — so a green run says no drift *from what*. This
   is an **observation, not a gate**: run it on a schedule, never as a required
   pull-request check, because a commit in one repository must not redden
   another's PRs. "No canon was available" is **unknown**, never "in sync".

Guarantee A's limit is worth stating: it proves the vendored surface is
internally consistent with the manifest travelling beside it. The link from that
manifest to the commit named in `PINNED.txt` is **reviewable provenance, not a
cryptographic attestation** — proving it would need a signature or a checkout of
that commit.

Neither comparison is defined here as code: this file is the contract they honor.

## Compatibility policy

A surface change is classified before it lands:

- **Additive (minor).** Backward-compatible: a new **optional** schema property,
  a new diagnostic **code**, a new fixture, additional prose. Existing consumers
  keep validating. Bump `manifest.json`; `contract_version` stays `v3`.
- **Breaking (major).** Removing or renaming a schema property or diagnostic
  code, tightening a previously optional field to required, changing canonical
  JSON node/edge ordering, or altering an existing fixture's `expected.json`.
  Breaking changes require a **new contract version** (`v4`), never an in-place
  `v3` edit — consumers migrate deliberately.

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

## The third comparison — delegated grammar (v3)

v3 carries `@epic`/`@defect` without restating their grammar, so a consumer holds **two**
pinned surfaces. Guarantee A therefore gains a third, still offline, check: the `tree_sha256`
in `depends.json` must equal the fingerprint of the `epics/v1` copy the consumer actually
vendored. A consumer that vendors plan-fields v3 against one epics surface while its validator
reads another is internally inconsistent even though both copies pass their own integrity
check — that gap is exactly what this comparison closes.

`source_commit` in `depends.json` is `null` in canon by construction: the KB cannot know the
commit that will carry this file. The vendoring consumer fills it, and reviewable provenance —
not cryptographic attestation — is what it provides, the same limit guarantee A already states.
