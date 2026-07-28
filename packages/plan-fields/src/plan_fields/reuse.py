"""History-dependent detection that needs a previous snapshot.

PF-ID-REUSED: an @id tombstoned (closed) in the previous snapshot reappears on a
different OPEN item in the current one — a reuse of a retired identity, not a rename
(a rename keeps the id on the same item). See the contract fixtures `reused-id/`.
"""

from __future__ import annotations

from typing import Any


def detect_reuse(
    current: dict[str, Any], previous: dict[str, Any]
) -> list[dict[str, Any]]:
    tombstoned = {
        n["id"] for n in previous["nodes"] if n["declared_status"] == "closed"
    }
    out: list[dict[str, Any]] = []
    for n in current["nodes"]:
        if n["declared_status"] == "open" and n["id"] in tombstoned:
            out.append({
                "code": "PF-ID-REUSED", "severity": "error",
                "subject_uri": n["node_id"], "related_uri": None, "rule_id": None,
                "message": (f"@id '{n['id']}' was tombstoned in the previous snapshot "
                            f"and is reused by a different open item"),
                "provenance": n["provenance"],
            })
    return out
