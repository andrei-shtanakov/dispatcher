---
title: "epics contract v1"
type: contract
status: proposed
owner: Andrei
updated: 2026-08-25
---

# epics contract — v1

Canonical, cross-cutting contract for the **stream axis**: which flow of work an artifact
belongs to. One mandatory epic per artifact across four artifact planes (`TODO.md` item,
commit, pull request, issue), plus an orthogonal optional defect class. Decided in
[[2026-08-25-adr-eco-010-epics-and-defect-classes]]; this directory is the **authority**
(the KB owns cross-cutting contracts with no producing repo — CLAUDE.md §4). Consumers vendor
a pinned copy inward; nothing reads this directory at run time.

> **Normative, not yet auto-checked.** These files (schemas + registries + fixtures with
> pinned expected outputs) are the normative spec **now**. The KB is a knowledge base, not a
> code repo, so the *executable* validator lives in `dispatcher/packages/plan-fields`
> (phase Ф1a of the ADR rollout) and runs these fixtures in its own CI. Until it lands,
> conformance is verified by hand against the fixtures. The frontmatter stays
> `status: proposed` until a real parser is shown to conform.

## Scope split — grammar here, values in the umbrella

| | authority | how consumed |
|---|---|---|
| **Grammar** — epic id, defect slug, trailer syntax, registry shape, diagnostics | this directory | vendored, pinned copy |
| **Values** — which programs and epics exist, statuses, coverage policy | `ai-orchestrators-workspace/epics.toml` | read live by path |

A registry that changes weekly cannot be vendored without being stale on arrival; a grammar
that changes rarely must not be re-implemented per consumer. `registry.schema.json` here is
the *shape* of `epics.toml`, never its content.

## Files

| File | Role |
|---|---|
| `classification.schema.json` | JSON Schema (2020-12) for the normalized classification of one artifact — the single output shape every carrier produces |
| `registry.schema.json` | JSON Schema for the `epics.toml` data model (programs, epics, defect classes, coverage policy, exclusions) |
| `diagnostics.yaml` | diagnostic code registry — `default_severity` / `scope` / `escalation` policy |
| `fixtures/` | input + pinned `expected.json` (the machine-checkable spec) |
| `manifest.json` | canonical surface fingerprint — per-file `sha256` + `tree_sha256` rollup |
| `drift-control.md` | drift-control policy — surface, compatibility, consumer registry, pin linkage |

## Model

### Two levels

- **Program** — a product or initiative. Key grammar `^[a-z0-9][a-z0-9-]{0,31}$`. Carries
  `kind: ecosystem | external`, which is the "our own platform vs third-party work" filter.
- **Epic** — a stream of work inside one program. The canonical id is always two levels,
  exactly one dot, no deeper nesting:

```
^[a-z0-9][a-z0-9-]{0,31}\.[a-z0-9][a-z0-9-]{0,63}$
```

The program prefix is **part of the epic's identity by design**: a raw line
`@epic:airun.kapelle-m3` must be readable without consulting the registry. The cost is that
re-parenting an epic is not a rename — it goes through a tombstone (see *Tombstones*).

Standing background work is an epic with `status: standing` inside its program (`eco.ops`,
`airun.ops`), never the absence of an epic.

### Exactly one epic per artifact

Not a list. An artifact in two streams double-counts in every aggregate; genuinely borderline
work belongs to the more specific epic. Two epic tags on one artifact are `EP-MULTIPLE`
**even when the values are identical** — the duplicate is a grammar defect, not a consensus.

### Defect class is orthogonal

`@defect:<class>` / `Defect:` is optional and appears **only** on fixes. It never substitutes
for the epic: a bug found while working inside an epic carries both. Slug grammar:

```
^[a-z0-9][a-z0-9-]{0,31}$
```

The set of classes is closed and lives in the registry (`[defect_classes]`). The defect tag
takes no part in the single-epic check and forms its own independent cut.

## Carriers

Two syntaxes, one vocabulary.

**Inline tag** — `TODO.md` items only, on the item's **first line**, alongside the plan-fields
tags. Continuation lines are not scanned (the operational parser reads items line by line):

```
- [ ] Build the producer console @id:df-console @epic:eco.dark-factory-slice0 @owner:github:andrei-shtanakov
- [ ] Fix pipeline resume crash @id:pipe-resume @epic:eco.ops @defect:pipeline
```

**Trailer** — commit messages, pull-request bodies, issue bodies. Same syntax as
`Co-Authored-By`: in the final trailer block, one `Key: value` per line, no blank line inside
the block:

```
Epic: eco.dark-factory-slice0
Defect: pipeline
```

A trailer key outside the final trailer block is not a trailer and is ignored — a body that
merely *mentions* `Epic: x` in prose does not classify the artifact. `Epic`/`Defect` keys are
matched case-sensitively; values are matched exactly (no case folding), so `Eco.Ops` is
`EP-GRAMMAR`, not a synonym.

GitHub labels are **not** a carrier in v1. Consequence, recorded so nobody looks for it later:
**in the GitHub web UI an epic is findable only by full-text search (`in:body`) — there is no
clickable label filter.** Mirroring trailers into labels is a possible later extension.

## Obligation

One epic is mandatory on:

- every managed `TODO.md` item;
- every first-party pull request and issue;
- every first-party non-merge commit.

**Inheritance.** A commit unambiguously attributable to a pull request inherits that PR's
epic. Attribution is by the PR↔SHA projection published in the snapshot contract, **never by
parsing `#123` out of a subject line** — that convention is evidence of nothing. A direct
commit with no PR to inherit from (the umbrella repo, where policy has no PRs) must carry its
own trailer.

**Exclusions** are enumerated in the registry and only there: merge commits, bot authors,
imported or vendored artifacts, and all history before `adopted_at`. Without an explicit list,
"100 % coverage" would depend on how one counts, which is the same as being undefined.

## Classification output

Every carrier — the `TODO.md` parser, the snapshot producer reading PR/issue bodies, the
commit reader — emits the **same** normalized object (`classification.schema.json`):

```json
{
  "epic": "eco.ops",
  "defect": "pipeline",
  "classification": "tagged",
  "diagnostics": [],
  "subject_uri": "todo://demo/pipe-resume",
  "carrier": "todo",
  "observed_at": "2026-08-25T00:00:00Z"
}
```

All seven fields are required; `epic`, `defect`, `subject_uri` and `observed_at` are nullable,
`carrier` and `classification` are not. `resolved_from` and `inherited_from` appear only where
they apply. An optional `carrier` would make per-plane completeness unassemblable — the object
would validate and still be unusable for the one aggregate this contract exists to support.

`classification` is a closed four-state:

| state | meaning |
|---|---|
| `tagged` | the artifact was read and carries a valid, registry-known epic |
| `missing` | the artifact was read and carries no epic |
| `invalid` | the artifact was read and its epic is unusable (grammar, unknown, multiple, moved) |
| `unavailable` | the artifact could not be read (body not retrieved, truncated, plane offline) |

The four states exist because `epic: null` alone cannot distinguish "producer did not collect
epics", "producer read the body and found none", "body was truncated", and "parser failed" —
the class of bug where *unknown looks like empty*. `unavailable` is never counted as
`missing`, and neither is ever counted as `tagged`.

Aggregates over these objects **must** carry the per-plane completeness with them: a total
that merges a fully-read plane with an unavailable one is not a total.

## Conflicts and multiplicity

Related artifacts (item → commit → PR → issue) that carry different epics produce
`EP-CONFLICT`. No value is silently chosen by precedence: a conflict is a defect in the
record, and picking a winner would hide it. Consumers report the conflict and treat the
artifact as `invalid` until it is resolved.

## Tombstones

Re-parenting or renaming an epic goes through a registry tombstone:

- `moved_to` is mutually exclusive with a live `status`;
- the target must exist;
- chains and cycles are forbidden — a move points straight at the final id;
- the retired id is accepted **only** on historical artifacts; a newly opened artifact
  carrying it gets `EP-MOVED`;
- a program id may not be deleted or reused while epics or history still reference it.

Identity is never reused — the same rule plan-fields applies to `@id`.

## Coverage policy

Two distinct points, both declared in the registry:

- `robin_cutover_*` — the coverage at which the digest may switch its primary axis to epics;
- `missing_error_after` — the date from which `EP-MISSING` blocks; the enforcement point is
  100 % minus an explicit grandfather list.

They are deliberately different numbers: switching a report and stopping a CI run are not the
same commitment, and equating them would stall the fleet on the last few percent.

A third knob guards the denominator: `min_sample`. A plane holding fewer artifacts than that
reports `insufficient_sample` rather than a ratio, and takes no part in the cutover decision —
a percentage over three artifacts is noise that a single untagged item drives to zero.

Structural diagnostics (`EP-GRAMMAR`, `EP-UNKNOWN`, `EP-MULTIPLE`, `EP-MOVED`, `EP-DEFECT-*`,
`EP-CONFLICT`) block from the moment a validator exists — a transition period exists to clear
old debt, not to license new ambiguity.

## Canonicalization (stable output)

- diagnostics are ordered by `(code, subject_key, subject_uri, related_uri)`, all comparisons
  on raw Unicode code points, `null` sorting before any string;
- a diagnostic instance carries the **computed** severity; `diagnostics.yaml` states the
  policy that computes it;
- `epic` and `defect` are emitted exactly as written after grammar validation — no
  normalization, no case folding, so a fixture's expected output is byte-comparable;
- one exception, deliberate: a **historical** artifact carrying a tombstoned id resolves to
  the final id in `epic`, with the retired value kept in `resolved_from`. Otherwise a rename
  would split one stream into two rows in every aggregate — the retirement stays visible
  without the split.

## Pin linkage (for consumers)

A consumer that vendors `plan-fields` **and** depends on this contract must record the
linkage machine-readably: `epics/v1` version, source commit, and `tree_sha256` from
`manifest.json`. The conformance suite verifies that linkage. Without it, "plan-fields
delegates epic grammar to epics/v1" degrades into a second copy of the regex within months.
