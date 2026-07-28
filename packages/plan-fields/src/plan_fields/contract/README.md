---
title: "plan-fields contract v1"
type: contract
status: proposed
owner: Andrei
updated: 2026-07-28
---

# plan-fields contract — v1

Canonical, cross-cutting contract for the **operational plan plane** (per-repo `TODO.md`)
and the evidence vocabulary that the governance plane consumes. Decided in
[[2026-07-27-adr-eco-005-plan-fields-two-plane-model]]; this directory is the **authority**
(KB owns cross-cutting contracts with no producing repo — CLAUDE.md §4). Consumers vendor a
pinned copy inward; nothing reads this directory at run time.

> **Normative, not yet auto-checked.** These files (schema + registries + fixtures with
> pinned expected outputs) are the normative spec **now**. The KB is a knowledge base, not a
> code repo (CLAUDE.md §1), so the *executable* conformance validator does **not** live here —
> it is a **PF-3** deliverable (the offline `plan-fields` package) that runs these fixtures in
> its own CI. Until PF-3 lands, conformance is verified by hand against the fixtures.
>
> The frontmatter is deliberately `status: proposed`: the contract is normative **as the
> spec**, but graduates to `accepted` once PF-3's validator confirms a real parser conforms to
> these fixtures. "Normative" is the spec's authority; `proposed` tracks that machine-verified
> conformance is still pending.

## Versions

- `contract_version: 1` — the plan-fields contract semantics (this document + registries).
- `schema_version: 1` — the JSON envelope shape in `schema.json`.

They are intentionally separate: the envelope shape can rev without changing contract
semantics, and vice versa.

## Files

| File | Role |
|---|---|
| `schema.json` | JSON Schema (2020-12) for the canonical parser output; `$id`, `additionalProperties:false` on every object **except** `raw` (see note) |
| `rules.yaml` | evidence rule registry with `evidence_grade` (machine \| attestation) |
| `diagnostics.yaml` | diagnostic code registry — `default_severity` / `scope` / `escalation` policy |
| `fixtures/` | input `.md` + pinned `expected.json` (the machine-checkable spec) |

> **The one open object:** `raw` (the `RawTags` `$def`) is intentionally
> `additionalProperties: true` — it preserves tag values exactly as written, including tags
> this contract does not yet model, so grammar diagnostics never lose the original. Every other
> object is closed.

## Model

A parser over a fleet snapshot emits `{contract_version, schema_version, generated_at,
nodes, references, edges, diagnostics}`:

- **nodes** — `OperationalNode` per `@id`-bearing checkbox item. Required: `node_id`
  (`todo://<repo>/<id>`), `id`, `repo`, `title`, `declared_status` (from the checkbox),
  `tombstone`, `provenance`. Optional: `owner_role` (DEC-007 slug), `trigger`, `freshness`,
  and `raw` (the open map of tag values exactly as written). `raw` **MAY** be omitted; when an
  item carries tags a parser **SHOULD** include it so grammar diagnostics keep the original.
  Fixtures include `raw` illustratively on at least one node per case, not on every node.
- **references** — *every* extracted `@blocked_by`, resolved or not (raw / legacy /
  unresolved). Each keeps `raw_ref`, and either `resolved_target` (a canonical URI) or
  `legacy_blocker_ref` (`<repo>#<slug>`).
- **edges** — **only** successfully resolved `todo:// → canonical` relations. A legacy or
  unresolved reference is **never** promoted to an edge (it stays in `references`). This is
  the load-bearing split: the dependency graph (`edges`) is built from identity, not text.
- **diagnostics** — findings keyed by `(code, subject_uri, related_uri, rule_id)`; the
  instance carries the **computed** severity.

### Identity & provenance

- **Canonical repo name = the workspace-manifest key**, not a value derived from the origin
  URL. Provenance keeps `remote_origin` for cross-check/diagnostics, but a rename/case change
  must not mint a new identity.
- `node_id = todo://<repo>/<id>`; `roadmap://<roadmap>/<id>` is reserved for governance nodes.
- `@id` grammar: `^[a-z0-9][a-z0-9._-]{0,63}$`, on the item's first line, unique-within-repo,
  stable across title renames, never reused (see PF-2A).
- Provenance = `{repo, remote_origin, commit, path, line}`. `line` is **not** part of a node's
  identity; it enters ordering **only** as the collision tie-breaker below.

### Canonicalization (stable output)

Deterministic ordering is by **identity**, so moving an item within a `TODO.md` does not
reshuffle the snapshot:

- `nodes` by `node_id`
- `edges` by `(source_node_id, target_node_id, kind)`
- `references` by `(source_node_id, raw_ref)`
- `diagnostics` by `(code, subject_uri, related_uri, rule_id)`

**Collision tie-breaker.** Identity keys are unique for valid input, but invalid input can
collide them (e.g. `PF-ID-DUPLICATE` puts two nodes at the same `node_id`). When identity keys
compare equal, break the tie by `(provenance.path, provenance.line)`. This is the only place
`line` enters ordering, and it keeps the canonical order well-defined even for invalid
snapshots — the `invalid/duplicate-id` fixture orders its two `todo://demo/dup` nodes by line
(3 then 4).

Canonical JSON: UTF-8, LF newlines, object keys sorted, no trailing whitespace.

## Diagnostics — policy vs history

`diagnostics.yaml` describes **policy**: `default_severity`, `scope`
(intra_repo | cross_repo), and `escalation`:

- `immediate` — already terminal on first observation.
- `second_independent_snapshot` — warning now, error once re-seen on a later **independent**
  snapshot (new `scan_id`, new observation time, ≥1 completed fleet-scan between; a rerun of
  identical inputs does not count). ADR-ECO-005 D7.
- `never` — environmental/advisory; never hardens.

A `Diagnostic` instance carries the already-computed severity; **Phase 0b** owns the history
that applies escalation. The contract only states the policy.

`PF-ID-DANGLING` is specifically a **canonical** inbound reference (`todo://…`) to an absent
`@id` — distinct from `PF-BLOCKER-DANGLING` (a legacy `<repo>#<slug>` miss).

## Rule registry

`rules.yaml` is the closed evidence vocabulary with an `evidence_grade`. `machine` rules are
automated proofs; `plan_item_declared_closed` is `attestation` — it promotes to `implemented`
but the projection must set `implementation_is_attested_only`, and consumers must not render
`implemented (attested)` as machine-backed `implemented` (ADR-ECO-005 D3).

## Fixtures

See `fixtures/README.md`. Simple cases are `<case>.md` + `<case>.expected.json` (parsed as
repo `demo`); history-dependent cases (`reused-id/`) are bundles with `previous.md` /
`current.md` / `context.json` / `expected.json`.

## Consumers

Vendor a pinned copy of this directory inward; run the PF-3 validator against these fixtures
in your own CI. Contract-drift control (canonical vs pinned copies) is **PF-6**.
