# Consuming `plan-fields` from another repo (PF-7)

The whole point of this package is that **one** parser implements the
plan-fields v1 contract. So an external consumer depends on the package as a
**pinned dependency** — it must never copy the package source inward. (The
*contract* is vendored inside the package, at `src/plan_fields/contract/`, and
drift-controlled by PF-6. The *package* is not vendored per consumer — copying
it would recreate the divergent-parser problem PF-7 exists to remove.)

Until a wheel is published to a registry (deferred to PF-8, after three
consumers exist), the delivery channel is a **pinned git dependency with a
subdirectory**, resolved from an immutable dispatcher commit.

## uv consumer

```toml
# pyproject.toml
[project]
dependencies = ["plan-fields"]

[tool.uv.sources]
plan-fields = {
  git = "https://github.com/andrei-shtanakov/dispatcher",
  rev = "<immutable-commit-sha>",          # a commit, never a branch/tag that moves
  subdirectory = "packages/plan-fields",
}
```

## pip / PEP 508 consumer

```
plan-fields @ git+https://github.com/andrei-shtanakov/dispatcher@<immutable-commit-sha>#subdirectory=packages/plan-fields
```

## Pin policy

- Pin an **immutable commit SHA**, not `master` and not a moving tag — a
  consumer's build must be reproducible and must not silently follow parser
  changes.
- The SHA must be **at or after** the commit that fixed the wheel build
  (this PF-7 bootstrap). Earlier commits ship the package but fail to build
  from a clean checkout (a `force-include` double-add), so they are not
  installable externally.
- Bumping the pin is a deliberate consumer PR. Contract drift between the
  pinned copy and canon is caught separately by PF-6 (`manifest.json`).

## Smoke test (what CI proves, reproducible locally)

A clean install must build the package **with its contract data** and pass
conformance — the editable in-tree install does not exercise this:

```bash
cd packages/plan-fields
uv build                                   # -> dist/plan_fields-*.whl (+ sdist)
uv venv /tmp/pf-smoke
uv pip install --python /tmp/pf-smoke dist/*.whl
/tmp/pf-smoke/bin/plan-fields conformance  # -> 7/7 fixtures conform
```

The `plan-fields` CI job runs exactly this after the unit tests, so external
installability is proven on every change to the package.

## What the package exposes

- `plan_fields.scrape_items(text)` → `list[ScrapedItem]`: the **operational**
  substrate — every checklist item (bullet, checkbox state, section, `raw_text`
  (verbatim, surrounding whitespace trimmed), tag-stripped `display_text`,
  `tags`, `item_id`), **pre-`@id`, no diagnostics, no synthesized identity**.
  `tags` is last-wins; `tag_list` (and `.values(key)`) is the lossless view that
  keeps a repeated key — e.g. two `@blocked_by:` on one item. This is what operational consumers
  (Robin's movement tracker, devtools' fleet check) build on before the fleet
  `@id` backfill (PF-2B) — `parse_todo` would drop every un-`@id`'d item.
- `plan_fields.parse_todo(text, repo, ...)` → canonical plan-fields JSON
  (nodes with checkbox status, `todo://` identity, `@owner`, `@blocked_by`
  references/edges, and the freshness triple `source_ref` / `observed_at` /
  `recheck_by`), plus diagnostics. **Single-repo**: cross-repo `@blocked_by`
  stays unresolved — one file cannot know if `todo://maestro/r-03b` exists.
- `plan_fields.parse_fleet(inputs, manifest)` → the same contract-valid envelope
  spanning the whole fleet, with cross-repo `todo://` (and transitional legacy)
  references **resolved into edges**. `inputs` is a list of
  `RepoInput(repo, todo_text, commit, available)` the caller has already frozen —
  `parse_fleet` does **no** directory/git/network discovery, and the `manifest`
  set is the sole authority for which repos exist. The five target outcomes are
  stable diagnostic codes: `PF-ID-DANGLING` (canonical id missing),
  `PF-BLOCKER-REPO-UNKNOWN` (repo not in manifest — a plan defect),
  `PF-BLOCKER-UNRESOLVABLE` / `PF-BLOCKER-NO-TODO` (environmental),
  `PF-LEGACY-AMBIGUOUS`. `plan_fields.check_fleet(snapshot)` adds
  `PF-BLOCKER-STALE` from the resolved graph. The `plan-fields fleet-graph
  --root <ws> --manifest <manifest.toml>` CLI is the disk-side loader (the
  sensor's entry point) that freezes inputs and calls both.
- `plan_fields.validator.run_conformance()` and the `plan-fields` CLI.
- The pinned contract under `plan_fields/contract/` (schema, registries,
  fixtures, `manifest.json`).
