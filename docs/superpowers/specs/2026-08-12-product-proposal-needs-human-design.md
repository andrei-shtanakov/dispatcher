# Read-only `needs_human` from impresario loop-state (`@id:product-proposal-needs-human`)

**Status:** design approved 2026-08-12. Accepts inbox issue #136 from
impresario (ADR-ECO-006, slug `product-proposal-needs-human`) — phase 2 of
#129, which shipped as PR #132–#135. This spec is a DELTA on the phase-1
spec (`2026-08-12-product-proposal-gate-waiting-design.md`); everything not
restated here — architecture, discovery contract, API case split, panel
conventions, fail-closed invariants — carries over unchanged.

## Problem

Phase 1 shows which product proposals wait at Gate A/B. The researcher ↔
creator loop's `needs_human` stop was invisible: the signal lived in
`loop.state`, an internal artifact of the reference runner. Impresario has
now contracted it — **loop-state/v1** (`impresario/contracts/loop-state/v1/`,
merged at `51e3103b5c88989a1d4a01a659d21790a92bb76b`) with a public
semantics section («Состояние цикла: loop-state») in their
`docs/semantics.md`. `loop.state` is a strict-JSON **current projection**
(not a journal) at the loop-workspace root:

| `stop` | Meaning |
|---|---|
| `null` | no active stop (loop not finished, or resumed) |
| `verdict: "needs_human"` | **a human is being waited for**; `reason` non-empty |
| `verdict: "ready_for_business"` / `"failed"` | terminal; NOT a human wait (terminal projection keeps `stop`) |

Wait identity is `(loop_id, stop.iteration)` — a repeated `needs_human`
after resume is a NEW wait, not a duplicate. Freshness is `stop.at`.

## Scope

**In scope (one vertical PR):**

- atomic re-pin of ALL THREE impresario contracts to one commit `51e3103`;
- vendoring `loop-state/v1`;
- reading, validating and locally cross-checking `<bundle>/loop.state`;
- additive read-model/API extension;
- `needs_human` rendering in the EXISTING panel;
- extension of the EXISTING Node harness;
- acceptance from #136 + updated live smoke;
- the TODO.md item is created AND closed by this same PR with evidence.

**Out of scope:** TUI/VSCode/MCP changes; impresario's producer-side
cross-checks (`LOOPSTATE_IDEA_REF`, `LOOPSTATE_IDEA_HASH`,
`LOOPSTATE_XLOG`, iteration-vs-budget — they stay with impresario's
validator and bundle gate); any new write-action; any phase-1 refactoring
beyond what adding loop-state requires.

## Pin strategy (owner decision)

One producer pin for all impresario contracts, preserved: the re-vendor
script grows to THREE contracts and regenerates all three directories in
one run at `51e3103`. `product-proposal/v1` and `gate-decision/v1` are
byte-identical between `28727ff` and `51e3103` (verified: empty git diff;
the manifests' per-file sha256 for both schema.json stay unchanged — that
is the recorded evidence). The copy-integrity PR gate checks every
directory against its manifest AND the equality of all three
`producer_commit` values (anti-mix stays one mandatory gate). The pinned
PP-101 test fixture is refreshed wholesale from `51e3103` and now includes
`loop.state` (terminal `ready_for_business`). The live-smoke checkout runs
at exactly the shared pin (the script already reads it from the manifests;
its manifest-agreement check now spans three).

## Read model (additive)

```python
LoopStatus = Literal[
    "absent",              # no loop.state file — normal, NOT an error
    "running",             # valid file, stop: null
    "needs_human",         # valid file, active human wait
    "ready_for_business",  # valid file, terminal
    "failed",              # valid file, terminal
    "unknown",             # file exists but its state is untrusted
]

class LoopWait(BaseModel):    # one «the loop waits for a human» record
    loop_id: str              # "LOOP-101"
    iteration: int            # stop.iteration; identity = (loop_id, iteration)
    proposal_id: str          # from loop.state (bundle match verified)
    reason: str               # stop.reason (schema: non-empty)
    stopped_at: str           # stop.at — the honest name: actual stop time
    bundle_path: str          # deterministic tie-break + provenance,
                              # NOT part of the identity

class ProposalBundle(...):    # two new fields
    loop_status: LoopStatus = "absent"
    loop_waits: list[LoopWait]  # 0..1; computed ONLY for state == "ok"

class ProductProposalsReport(...):
    needs_human: list[LoopWait]  # aggregate over ok bundles,
                                 # sorted (loop_id, iteration, bundle_path)
```

`loop_status` semantics (owner-fixed): `"absent"` means the file does not
exist and that is normal; `"unknown"` means the file EXISTS but its state
is untrusted (path escape, read, JSON, schema, or mismatch); every other
value is set only after full validation AND a successful bundle match.
Whenever the bundle's final state is non-`ok` (for any reason, including
proposal-level or decision-level diagnostics and conflicts),
`loop_status` is set to `"unknown"` and `loop_waits` is emptied — simpler
and less ambiguous than preserving a trusted-read observation under an
untrusted bundle.

## Classification of `loop.state`

Read from `<bundle>/loop.state` after the proposal, with the phase-1
fail-closed pipeline:

- **absent** → `loop_status="absent"`, no diagnostics; phase-1
  classification proceeds exactly as before;
- **file symlink** obeys the phase-1 rule: readable ONLY when `resolve()`
  stays inside `mirror_root`; escape → `loop-state-path-escape`, file
  never read;
- **read/parse fail-closed**: OSError / non-UTF-8 / not JSON / not a JSON
  object → `loop-state-unreadable`. JSON is parsed STRICTLY: an
  `object_pairs_hook` rejects duplicate keys at any depth (plain
  `json.loads` silently keeps the last value — the same failure mode the
  strict YAML loader exists to close). Duplicate-key rejection is part of
  `loop-state-unreadable`, not a separate public code;
- **schema**: validated against the VENDORED `loop-state/v1`. The file
  carries no version field; dispatcher knows the version by the vendored
  schema it validates against — an incompatible shape yields
  `loop-state-schema-invalid`;
- **local bundle-membership cross-check** (the only producer check we
  replicate, because both sides are already trusted reads):
  `loop.state.proposal_id == proposal.yaml.proposal_id`. Mismatch →
  `loop-state-proposal-mismatch`; the message carries the relative
  `loop.state` path and BOTH proposal_ids. All other `LOOPSTATE_*`
  cross-checks stay with impresario's validator;
- **valid + matched**: `stop: null` → `"running"`, no wait;
  `needs_human` → `"needs_human"` + one `LoopWait`;
  `ready_for_business` / `failed` → terminal status, no wait;
- **proposal untrusted** (`unreadable` bundle): loop.state read/parse
  errors are still collected; schema validation and the mismatch check are
  skipped (no trusted subject); final `loop_status` is `"unknown"` via the
  non-ok rule;
- every `loop-state-*` diagnostic makes the bundle `unknown` (decision-
  grade), never `unreadable` — the proposal itself remains readable; only
  the loop classification is unknown. Any bundle-level diagnostic (of any
  kind) suppresses BOTH classifications: `waits` and `loop_waits` empty
  means «suppressed», never «nothing waits». A plain `LoopWait` does NOT
  raise `attention` — waiting for a human is expected work.

State priority is unchanged (`conflict` > `unreadable` > `unknown` >
`ok`); the conflict pass suppresses participants' `loop_waits` together
with their `waits` and sets their `loop_status` to `"unknown"`.

## Diagnostic codes (added to the phase-1 taxonomy)

| Code | Resulting state | Condition |
|---|---|---|
| `loop-state-unreadable` | `unknown` | OSError / non-UTF-8 / not JSON / not an object / duplicate JSON keys |
| `loop-state-schema-invalid` | `unknown` | fails vendored `loop-state/v1` (incl. incompatible shape) |
| `loop-state-path-escape` | `unknown` | `loop.state` resolves outside `mirror_root`; never read |
| `loop-state-proposal-mismatch` | `unknown` | `loop.state.proposal_id` ≠ the bundle proposal's id; message names the path and both ids |

## Components

- **Vendoring**: third directory `contracts/impresario-loop-state/v1/`
  (schema.json + 9 upstream fixtures + manifest.json + PINNED.txt);
  `scripts/revendor_impresario_contracts.sh` CONTRACTS array grows to
  three entries; one run at the new pin regenerates all three.
  `tests/test_impresario_contracts_vendor.py`: `PRODUCER_COMMIT` literal →
  `51e3103…`, third directory in `VENDORED`/`EXPECTED_SURFACES`, anti-mix
  asserts all THREE manifests agree. The re-vendor script test's miniature
  producer gains the third subdir; the partial-source case still proves
  no directory changes unless all three extract and verify.
  `drift-impresario-contracts` job: third pair in the loop.
- **Core** (`dispatcher/core/product_proposals.py`): internal
  `_load_loop_state(bundle_dir, mirror_root, proposal) ->
  tuple[LoopStatus, LoopWait | None, list[Diagnostic]]`, called from
  `_load_bundle` after decisions; cached validator for the third schema;
  `_load_bundle` finalizes the non-ok rule; `_mark_conflicts` extended;
  `collect_product_proposals` aggregates `needs_human`.
- **API**: no route changes; `response_model=ProductProposalsReport`
  picks up the additive fields. Phase-1 404/200 case split untouched.
- **Panel** (existing section in `server/static/index.html`):
  - every bundle row shows a text chip `· loop: <status>` for ALL
    statuses INCLUDING `absent` (owner-fixed: explicit read-model states
    are not re-hidden in the UI — otherwise «file absent», «old API»,
    «loop with no wait» and «frontend without phase 2» are
    indistinguishable). `absent` is normal and carries no attention
    styling; `unknown` carries the attention mark, consistent with the
    suppressed convention;
  - a `needs_human` table (Loop, Iteration, Reason, Stopped at, Bundle)
    renders ONLY when `report.needs_human` is non-empty;
  - an explicit `0 loops waiting` label renders when `needs_human` is
    empty (next to the phase-1 zero-labels, distinct from all of them);
  - everything through `esc()`; no hrefs; `ppGen` guard unchanged.

## Testing & acceptance

**Unit**: one test per new diagnostic code (`loop-state-unreadable`
covers strict-JSON duplicate keys and a monkeypatched OSError branch
separately; `loop-state-path-escape` plus the in-mirror-symlink-stays-
readable positive; `loop-state-proposal-mismatch` asserts both ids in the
message); `absent` / `running` / both terminals / `needs_human` (field
values incl. `stopped_at`); needs_human aggregate ordering
`(loop_id, iteration, bundle_path)`; proposal-untrusted interplay (read
errors collected, no mismatch check, `loop_status == "unknown"`); the
non-ok rule (a decision-level diagnostic alone forces
`loop_status == "unknown"` and empties `loop_waits`); conflict pass
suppression; determinism and the read-only invariant re-run green with
loop.state files present.

**Acceptance (verbatim from #136 + owner's list):** on a tmp copy of the
pinned PP-101:

1. `stop.verdict` mutated to `needs_human` (non-empty `reason`) → exactly
   one `needs_human` record with identity `(LOOP-101, 2)` and freshness
   from `stop.at`;
2. the true PP-101 (terminal `ready_for_business`) → zero loop waits,
   `loop_status == "ready_for_business"`;
3. `stop` mutated to `null` → zero loop waits, `loop_status == "running"`;
4. `loop.state` deleted → phase-1 classification works unchanged, no
   error, `loop_status == "absent"`;
5. unreadable or mismatched `loop.state` → bundle `unknown`, BOTH
   classifications suppressed;
6. every HTTP case test asserts the SERIALIZED `loop_status`,
   `loop_waits` and `needs_human` fields, not only the core function's
   return value.

**Live smoke**: expectations extended — the pp-101 bundle additionally
shows `loop_status == "ready_for_business"` and `needs_human == []`.
`scripts/checkout_pinned_impresario.sh` is EXTENDED to three manifests
(today it hardcodes two and compares `PIN_A`/`PIN_B`): the pin is read as
a LIST over all three vendored manifests; every entry is validated as a
full 40-hex commit id; the set of pins must be exactly one value — any
disagreement (including only `loop-state` differing) is a provenance
FAILURE (exit 3) BEFORE any checkout happens. The «two vendored
manifests» / «the two manifests disagree» comments are updated to three.
The checkout script gains a regression test (sandbox `--from` discipline
of `tests/test_revendor_impresario_script.py`): a sandbox where ONLY the
loop-state manifest names a different pin must fail with the provenance
error and perform no checkout; the agreement-pass direction is covered by
the live smoke itself, which uses the pin only after the three-way
agreement check succeeds.

**JS harness** (extend `tests/web/product_proposals_harness.js`):
a needs_human row is readable off one screen (loop id, iteration, reason,
«Stopped at»); a terminal loop shows its chip and NO table plus the
`0 loops waiting` label; `loop: absent` chip is visible on a bundle
without loop.state; a suppressed bundle shows `loop: unknown`; a hostile
`reason` arrives escaped.

## Delivery

One PR, branch `feat/pp-needs-human`. The TODO.md item
(`@id:product-proposal-needs-human`, accepting inbox #136) is added and
closed `[x]` with the PR number in this same PR. Issue #136 is closed by
a human after the merge.

## Follow-ups (unchanged from phase 1)

TUI/VSCode/MCP parity (`@id:product-proposal-parity`, now including
needs_human); safe repository-URL links. No new follow-ups introduced.
