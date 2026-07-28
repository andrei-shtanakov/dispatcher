# Changelog — plan-fields

## 0.1.0 — 2026-07-28

Initial offline package (PF-3, ADR-ECO-005).

- Parser for a single repo `TODO.md` → canonical plan-fields document (nodes /
  references / edges / diagnostics).
- Canonicalization: ordering by identity with the `(path, line)` collision
  tie-breaker; canonical JSON dump.
- Validator against the vendored `schema.json`; `run_conformance()` over the
  vendored fixture suite (6 pairs + 1 history bundle).
- CLI: `parse`, `validate`, `conformance`.
- Vendored, pinned copy of the plan-fields v1 contract under
  `src/plan_fields/contract/` (`PINNED.txt` records the source commit).
- Standalone: no dispatcher import (enforced by a test).
