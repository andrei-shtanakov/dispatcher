# Read-only `qg4_backlog` wait — phase 3 of gate_waiting (`@id:product-proposal-qg4-backlog-wait`)

**Status:** design accepted 2026-08-17. Accepts inbox issue #154 from
impresario (ADR-ECO-006, slug `product-proposal-qg4-backlog-wait`); the
TODO.md item under «Product-governance (impresario)» carries the same id.

## Problem

Phases 1–2 (#129 → PR #132/#133; #136 → PR #137; parity PR #138) cover the
proposal-level authority waits (Gate A/B) and the loop-level `needs_human`.
One authority-wait class remains invisible: **QG-4 at the backlog level** —
«RankedBacklog version N is published, no QG-4 decision on that version
exists». Live precedent at acceptance time: backlog v4 published
2026-08-17 with selectable items and no QG-4 decision on v4 — the wait
exists and is visible only to whoever opens `pilot/backlog.yaml` by hand.

QG-4 is **one gate over the backlog version**, not a gate per `items[]`
row: waits per version are zero or one. Identity = `(backlog_id, version)`;
`idea_ref` is context, never identity.

## Scope

**In:** vendored `ranked-backlog/v1`; the `qg4_backlog` wait in the
existing read model (`ProductProposalsReport`); web-panel rendering; the
acceptance/unit/API/JS/live-smoke tests; **LRD-awareness** (below).

**Out:** any write action (no «select» buttons — dispatcher's runtime
mutations stay in their narrow whitelist); explicit TUI/VSCode rendering of
the new fields (MCP passes the serialized report through unchanged, so the
data is already on that surface; richer TUI/VSCode rendering is a recorded
follow-up, precedent PR #138).

## Forced by the re-pin: loop-resume-decision awareness

The contract pin moves to the impresario commit that contains
`ranked-backlog/v1` (all vendored impresario contracts share ONE pin — the
anti-mix guarantee). At that pin the live pp-101 bundle carries
`decisions/lrd-001.yaml` — a **loop-resume-decision**, a record type that
legitimately shares `decisions/` but is not a gate decision. Under the
phase-1 rule («every decisions/*.yaml must match gate-decision/v1») it
turns the whole bundle `unknown` — this is ALREADY the live behaviour
against today's mirror, i.e. a real regression the re-pin makes permanent
unless handled.

Rule: a `decisions/*.yaml` mapping is validated against gate-decision/v1
first; a file that fails it but matches the vendored
`loop-resume-decision/v1` is **recognized and ignored** (valid mirror
content with no role in gate classification — its supersedes chain governs
loop resumption, impresario's business, and never touches waits). A file
matching **neither** schema is `decision-schema-invalid` (message names
both contracts). Fail-closed is preserved: nothing unrecognized is
silently skipped.

Both new contracts (`ranked-backlog/v1`, `loop-resume-decision/v1`) are
vendored by the same re-vendor script, share the single pin, and join the
copy-integrity PR gate, the pin-agreement checkout gate and the scheduled
upstream-drift job.

## Discovery

The same walk that finds `proposal.yaml` roots also collects `backlog.yaml`
roots (same exclusions, same symlink rules, same `onerror` fail-loud).
Decisions for a backlog root are read from the **sibling**
`<root>/decisions/*.yaml` — the live layout (`pilot/backlog.yaml` +
`pilot/decisions/`) satisfies this without hardcoding `pilot/`.

## Read model

```python
class BacklogWait(BaseModel):
    backlog_id: str                  # "BL-ecosystem"
    gate_id: Literal["qg4_backlog"]  # constant
    gate_label: str                  # "QG-4"
    authority: str                   # "qg4_selector" (role from live gd-001/gd-002)
    artifact_ref: str                # "backlog://<id>"
    artifact_path: str               # backlog.yaml, mirror-relative
    version: int
    backlog_updated_at: str          # the version publication moment — a real
                                     # freshness signal (unlike the proposal's)
    selectable_idea_refs: list[str]  # context; NOT identity, NOT in artifact_ref
    # dedup identity = (backlog_id, version)

class BacklogBundle(BaseModel):      # every discovered backlog root, lossless
    path: str
    state: Literal["ok", "unreadable", "unknown", "conflict"]
    diagnostics: list[Diagnostic]
    backlog_id: str | None
    version: int | None
    updated_at: str | None
    waits: list[BacklogWait]         # 0..1; computed ONLY for "ok"
```

`ProductProposalsReport` gains `backlog_bundles` (sorted by path) and
`backlog_waits` (aggregate over ok roots, sorted by
`(backlog_id, version, artifact_path)`); `attention` also raises on any
non-ok backlog root. All phase-1 invariants carry over verbatim: non-ok ⇒
`waits: []` means «suppressed», never «nothing waits»; determinism;
read-only.

## Classification semantics

The wait exists for the current backlog version iff:

- `items[]` has at least one **selectable** row — `status` in
  `{new, under_review}` (owner-fixed policy: an item a human is currently
  looking at is still undecided, so it keeps the wait open), AND
- no **active** GateDecision in the sibling `decisions/` has
  `gate_id: qg4_backlog`, `subject.kind: ranked_backlog`,
  `subject.ref == "backlog://<id>"`, `subject.version == version`, and a
  QG-4 outcome — `select | defer | park | reject` (semantics.md «QG-4:
  human select»; ANY outcome extinguishes the wait, not only select).

«Active» = not superseded, same rule as phase 1. A decision for another
version is history, not permission: a version bump extinguishes the old
wait and (with selectable items) opens a new one under the new identity.

Diagnostic codes (stable API contract): `backlog-unreadable`,
`backlog-schema-invalid`, `backlog-path-escape` → state `unreadable`;
decision-level codes are shared with phase 1 → `unknown`;
`backlog-id-conflict` (one backlog id claimed by several roots — global
pass, all participants marked, waits suppressed) → `conflict`.

## Web panel

Same fail-closed rendering discipline inside the existing
product-proposals section: a QG-4 waits table (backlog ref, gate, authority,
version, «Backlog updated», selectable idea refs, path), backlog rows with
the shared state badges, «0 backlog gates waiting» only on a fully
classified scan (no suppressed roots, no report-level diagnostics); no
local path ever becomes an href; everything escaped.

## Acceptance (verbatim from #154, each is a test)

1. Pinned live mirror state (backlog v4, selectable items, no QG-4 decision
   on v4) → **exactly one** wait, identity `(BL-ecosystem, 4)`, freshness
   from `updated_at`.
2. Copy + an active qg4_backlog GateDecision on v4 (each of the four
   outcomes) → **zero** waits.
3. Copy bumped to v5 with selectable items, no v5 decision → exactly one
   wait, identity `(BL-ecosystem, 5)`.
4. Copy with an unreadable `backlog.yaml` → unknown-grade diagnostics
   (`unreadable` state), NOT «zero waits».

Plus: superseded v4 decision does not extinguish; wrong-ref decision does
not extinguish; no-selectable ⇒ no wait; `under_review` is selectable;
id conflict; LRD recognized-and-ignored; neither-schema file still
invalid; API pass-through; JS harness (readable off one screen, suppressed
wording, zero-label discipline, escaping); live smoke asserts the
serialized response at the pin (pp-101 + pp-104 ok, one backlog wait
`(BL-ecosystem, 4)`).

Fixture: `tests/fixtures/product_proposals/pilot-backlog/` — pinned copy of
the live `pilot/backlog.yaml` + `pilot/decisions/` at the contract pin,
with provenance; scenario mutations run on tmp_path copies.

## Delivery

One PR `feat/qg4-backlog-wait` (phase-2 precedent): re-vendor (5 contracts,
one pin) + core + web panel + all tests. The TODO.md item goes `[x]` with
the PR number; issue #154 is closed by a human after the merge.

## Follow-ups (recorded, out of scope)

- Explicit TUI/VSCode rendering of `backlog_waits` (parity precedent
  PR #138); MCP already carries the fields.
