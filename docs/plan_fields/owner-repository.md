# `repo:<key>` repository owners — self, unknown, external

`@owner:repo:<manifest-key>` names a repository, not a person or team, as the
principal accountable for a plan item. It is useful when responsibility for an
item genuinely belongs to another repository in the fleet. The fleet layer
(the layer that holds the frozen `workspace-manifest.toml`, not the
single-repo parser) resolves that key and puts every `repository`-kind owner
into exactly one of three mutually exclusive states:

| State | Meaning | Diagnostic code | Severity | Counts as `repo-owned`? |
|---|---|---|---|---|
| **External** | the key resolves to a *different*, known repository | *(none)* | — | yes |
| **Unknown** | the key does not resolve to any repository in the frozen manifest | `PF-OWNER-REPO-UNKNOWN` | `warning` | no |
| **Self** | the key resolves to the item's *own* source repository | `PF-OWNER-REPO-SELF` | `warning` | no |

Only the external state is a validly assigned repository owner. Self and
unknown both leave the item without a real external principal, but for
different reasons, so they are reported with different codes — an item never
gets both codes for the same owner reference.

## `PF-OWNER-REPO-SELF`

- **Severity:** `warning` (not an error — see "Why not an error?" below).
- **Scope:** fleet-only. The single-repo parser validates that
  `repo:<manifest-key>` is grammatically well-formed, but it cannot prove
  *identity* — that requires the frozen workspace manifest, which only the
  fleet layer holds. A `repo:<key>` owner never gets a self/unknown/external
  verdict from single-repo parsing alone.
- **What it means:** the owner names the same repository the plan item
  already lives in. Naming your own repository as owner is circular — the
  repository is not "accountable" for the item in any sense beyond the item
  already being recorded there. It identifies no external principal who
  should act.
- **Identity, not spelling:** the match is against the item's *canonical*
  source repository, after normalizing through the frozen manifest — not
  against the raw characters of the owner tag. A repository can appear in the
  manifest under its canonical key and, separately, under a declared
  `git_dir` alias for the same repository; both spellings resolve to the same
  identity and both trigger `PF-OWNER-REPO-SELF` when they name the item's own
  repository.

### Examples

**Canonical key.** `dispatcher/TODO.md` contains:

```
- [ ] work @id:x @owner:repo:dispatcher
```

The manifest's canonical key for this repository is `dispatcher`, so the
owner names the item's own repository. This is self-owner:
`PF-OWNER-REPO-SELF`, attached to `todo://dispatcher/x`.

**Declared `git_dir` spelling.** The manifest additionally declares
`legacy-checkout-dir` as a `git_dir` alias of `dispatcher`. The same item
written as:

```
- [ ] work @id:x @owner:repo:legacy-checkout-dir
```

still names the item's own repository once resolved through the manifest, so
it gets the identical `PF-OWNER-REPO-SELF` verdict — not a pass, and not
`PF-OWNER-REPO-UNKNOWN`. Spelling the self-reference through an alias does not
make it a different, external repository.

By contrast, `@owner:repo:maestro` on the same item is a different, known
repository — a valid external repo-owner, no diagnostic.

## How to fix it

`PF-OWNER-REPO-SELF` never gets an automatic fix: nothing assigns a new owner
for you, and no future migration is planned to bulk-rewrite existing
occurrences. When you see the finding, replace the self-referencing owner
with one of:

- a real accountable principal — `@owner:github:<handle>` or
  `@owner:github-team:<org>/<team>`; or
- a genuinely different, known repository — `@owner:repo:<other-key>`, if
  responsibility truly belongs elsewhere; or
- an explicit `@owner:TBD` if no owner has been decided yet — this is a
  legitimate, honest state, and is treated differently from an item that
  silently claims to be repo-owned by itself.

Do not just delete the `@owner` tag: a missing owner is reported separately
(`PF-OWNER-MISSING`) and is not a fix for this finding.

## Why not an error?

`PF-OWNER-REPO-SELF` ships at `warning` severity and does not, by itself,
introduce a new mandatory governance gate. Existing fleet data may already
contain self-owner records, and turning this into a hard failure without
first measuring and reviewing that backlog would surprise authors who did
nothing new wrong. Whether `PF-OWNER-REPO-SELF` should later participate in a
mandatory gate is a separate, explicit product decision — this diagnostic
being observable today does not commit to that outcome.

## See also

- `PF-OWNER-REPO-UNKNOWN` — same `warning` severity and fleet-only scope, but
  for a `repo:<key>` that does not resolve to *any* repository in the frozen
  manifest, rather than resolving to the item's own repository.
- `packages/plan-fields/src/plan_fields/contract/diagnostics.yaml` — the
  vendored diagnostic registry (canonical source:
  `prograph-vault authored/contracts/plan-fields/v3`).
- `packages/plan-fields/src/plan_fields/views.py` — the one place fleet
  reporters read the self/unknown/external verdict back from diagnostics,
  instead of re-deriving it by re-normalizing the raw owner tag.
