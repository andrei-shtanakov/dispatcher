"""Canonicalization: deterministic ordering by identity + canonical JSON.

Ordering is by identity, never by file line, EXCEPT as the documented collision
tie-breaker `(provenance.path, provenance.line)` for equal identity keys (which
only happen on invalid input, e.g. a duplicate @id). See the contract README.
"""

from __future__ import annotations

import json
from typing import Any


def _pv(obj: dict[str, Any]) -> tuple[str, int]:
    prov = obj.get("provenance") or {}
    return (prov.get("path") or "", prov.get("line") or 0)


def canonicalize(doc: dict[str, Any]) -> dict[str, Any]:
    """Sort nodes/references/edges/diagnostics into canonical order in place."""
    doc["nodes"].sort(key=lambda n: (n["node_id"], *_pv(n)))
    doc["references"].sort(key=lambda r: (r["source_node_id"], r["raw_ref"], *_pv(r)))
    doc["edges"].sort(
        key=lambda e: (e["source_node_id"], e["target_node_id"], e["kind"])
    )
    doc["diagnostics"].sort(
        key=lambda d: (
            d["code"],
            d.get("subject_uri") or "",
            d.get("related_uri") or "",
            d.get("rule_id") or "",
            *_pv(d),
        )
    )
    return doc


def canonical_dumps(doc: dict[str, Any]) -> str:
    """Canonical JSON: UTF-8, LF, keys sorted, trailing newline."""
    return json.dumps(doc, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
