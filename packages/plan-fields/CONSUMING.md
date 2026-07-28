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

- `plan_fields.parse_todo(text, repo, ...)` → canonical plan-fields JSON
  (nodes with checkbox status, `todo://` identity, `@owner`, `@blocked_by`
  references/edges, and the freshness triple `source_ref` / `observed_at` /
  `recheck_by`), plus diagnostics.
- `plan_fields.validator.run_conformance()` and the `plan-fields` CLI.
- The pinned contract under `plan_fields/contract/` (schema, registries,
  fixtures, `manifest.json`).
