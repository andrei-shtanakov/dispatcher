# plan-fields

Offline parser + validator for the **plan-fields v1** contract (ADR-ECO-005, PF-3).

Standalone by design: it **does not import the dispatcher application**. The v1
contract (schema, registries, fixtures) is **vendored** under
`src/plan_fields/contract/` as a pinned copy — see `contract/PINNED.txt`. Contract
drift-control (canonical vs this pinned copy) is a separate item, **PF-6**.

## Install / run (uv)

```bash
uv run plan-fields conformance                 # run the vendored fixture suite
uv run plan-fields parse TODO.md --repo maestro --generated-at 2026-07-28T00:00:00Z
uv run plan-fields validate some-document.json
```

## API

```python
from plan_fields import parse_todo, canonical_dumps, validate_document, run_conformance

doc = parse_todo(open("TODO.md").read(), repo="maestro")
validate_document(doc)            # raises on non-conformance
print(canonical_dumps(doc))
```

## What v0.1 implements

Single-snapshot semantics: identity/tombstone extraction, canonical + legacy
`@blocked_by` resolution (references vs edges), and diagnostics **PF-ID-MISSING**,
**PF-ID-DUPLICATE**, **PF-ID-DANGLING**, **PF-LEGACY-AMBIGUOUS**, **PF-OWNER-MISSING**,
**PF-OWNER-GRAMMAR**. History-dependent **PF-ID-REUSED** is covered via the
`reused-id/` bundle (previous + current snapshot).

Not yet in v0.1 (contract defines them; parser will grow): `PF-BLOCKER-STALE` /
`-UNRESOLVABLE` / `-NO-TODO`, `PF-RECHECK-EXPIRED`, `PF-TOMBSTONE-REMOVED`. These need
the fleet/history layer (Phase 0b) or cross-repo inputs.

## Conformance

`run_conformance()` parses every vendored fixture and compares to its pinned
`expected.json` after canonicalization, ignoring the two INFORMATIVE fields the
contract marks non-normative: `message` (human text) and `raw` (optional). Codes,
severities, URIs, provenance, and structure must match exactly.
