# Human queue, slice A2 — PRs awaiting a human merge

**Status:** 2026-09-30. Spec and code ship in one PR (owner decision 2026-09-30); the
producer half landed first as github-checker#48 (`pr-search`), authored from this
workspace by explicit owner permission instead of an inbox issue.
**Parent:** `2026-09-29-human-control-plane-design.md` §3; narrows
`2026-09-29-human-queue-a1-design.md` §3.4.

## 1. Source

`forge_labelled_prs` — every open PR labelled `forge_merge_label` (config; default
`human-merge-required`, empty string turns it off) under the owner of dispatcher's own
clone, read through `github-checker pr-search` against the vendored actions/v1 contract
(re-pinned to `1283a74`).

- `prs` stated → `ok`, one wait per PR; `prs` null (failed, non-exhaustive, capped at 100,
  unreadable, or github-checker not runnable) → `unavailable`. Never an empty queue.
- Label unset (a bare `DispatcherConfig`, i.e. tests and embeddings) → `not_configured`:
  the source runs `gh` over the network and must not do so implicitly.
- **TTL cache, 60 s, failures included** (`ForgeReader`): the VSCode poll is 10 s, and one
  search plus two reads per PR must not run that often; an outage is not re-searched per
  poll.

## 2. Wait

| Field | Value |
|---|---|
| key | `pr:{owner/name}#{number}` |
| reason | `pr_human_merge` |
| since / basis | `labeled_at` — the **last** time the label was added (a re-label starts a new wait) / `"label <label> added"`; null when unreadable |
| act | `human_merge {repo, number, url, head_sha}`; `head_sha` null when the head could not be read — consumers never invent a pin |

## 3. Deviations from A1 and the parent

- `forge_candidate_prs` is **removed**, not connected. §I12 obliges every candidate
  (approval) PR to carry `human-merge-required` (`devtools/merge-pr.sh:59`), so candidates
  arrive through this source already. Telling them apart needs the approval-branch
  template from devtools' SSOT — a contract of its own; `bundle_candidate` stays a reserved
  reason until then. The human act is the same either way: `human-merge.sh`, which itself
  distinguishes a candidate.
- With the forge source connected or deliberately off, `complete` can now be true.
- Scope: one owner (the owner of dispatcher's clone). Repositories under other owners
  (e.g. the `DarkFactory-polygon` org) are not searched yet.

## 4. VSCode

`pr_human_merge` groups first ("PR awaits your merge"). Clicking offers **Open PR in
browser** or **Type human-merge command** — `sh devtools/human-merge.sh <name> <n>
--expect-head <sha>` typed into a terminal, not executed, with the pin omitted (and the
omission said) when the head is unknown. Tree labels now lead with the short repo name,
so a long title cannot hide whose wait it is.
