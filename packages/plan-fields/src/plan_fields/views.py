"""Reporter-facing owner views (PF-OWNER-REPO-SELF, WS-dispatcher-229 BEH-08/FR-05).

``parse_fleet`` already classifies every ``repository``-kind owner into exactly
one verdict (BEH-06) via the ``PF-OWNER-REPO-SELF`` / ``PF-OWNER-REPO-UNKNOWN``
diagnostics it emits — absence of both means a valid external repo-owner. This
module is the ONE place that reads those diagnostics back into a verdict per
node, so every reporter (web/TUI/VSCode/MCP, or any future fleet consumer)
shares one classification instead of re-deriving it by re-normalizing
``owner_ref.raw`` itself (FR-05, NFR-02): self-owner must never be counted as a
validly assigned external repo-owned node.
"""

from __future__ import annotations

from typing import Any

REPO_OWNER_SELF = "self"
REPO_OWNER_EXTERNAL = "external"
REPO_OWNER_UNKNOWN = "unknown"

_SELF_CODE = "PF-OWNER-REPO-SELF"
_UNKNOWN_CODE = "PF-OWNER-REPO-UNKNOWN"


def repo_owner_verdicts(doc: dict[str, Any]) -> dict[str, str]:
    """One repo-owner verdict per node with a ``repository``-kind owner.

    Maps each such node's URI to ``REPO_OWNER_SELF``, ``REPO_OWNER_EXTERNAL``
    or ``REPO_OWNER_UNKNOWN`` — the same three mutually exclusive verdicts
    ``parse_fleet`` already computed (BEH-06), read back from its diagnostics
    rather than re-resolved through the manifest a second time. Nodes without
    a ``repository``-kind owner are absent from the result.
    """
    diag_codes: dict[str, set[str]] = {}
    for d in doc["diagnostics"]:
        if d["code"] in (_SELF_CODE, _UNKNOWN_CODE):
            diag_codes.setdefault(d["subject_uri"], set()).add(d["code"])

    verdicts: dict[str, str] = {}
    for node in doc["nodes"]:
        owner_ref = node.get("owner_ref")
        if not (isinstance(owner_ref, dict) and owner_ref.get("kind") == "repository"):
            continue
        codes = diag_codes.get(node["node_id"], set())
        if _SELF_CODE in codes:
            verdicts[node["node_id"]] = REPO_OWNER_SELF
        elif _UNKNOWN_CODE in codes:
            verdicts[node["node_id"]] = REPO_OWNER_UNKNOWN
        else:
            verdicts[node["node_id"]] = REPO_OWNER_EXTERNAL
    return verdicts


def repo_owned_node_ids(doc: dict[str, Any]) -> list[str]:
    """Node URIs validly owned by an external repository (BEH-08/FR-05).

    Self-owner and unknown repo-owner nodes are excluded: only a
    ``repository``-kind owner distinct from the node's own source repository
    and resolvable through the frozen manifest counts as repo-owned.
    """
    return sorted(
        node_id
        for node_id, verdict in repo_owner_verdicts(doc).items()
        if verdict == REPO_OWNER_EXTERNAL
    )
