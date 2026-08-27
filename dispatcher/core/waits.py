"""Waits view — the fleet's waiting axis, read from the canonical package.

Spec: docs/superpowers/specs/2026-08-26-waits-graph-design.md (acceptance of
inbox #201). Everything semantic — edge resolution, legacy handling, the
"blocker delivered" verdict — is plan_fields' (`parse_fleet` + `check_fleet`);
this module only lays the snapshot out for the panel and keeps the honesty
rules: a stale edge is a STATE of the edge, never its disappearance, and
loose references are shown as what they are — text, not relations.

Diagnostics are NOT stitched to individual references: the package gives them
no machine key to one reference (`PF-LEGACY-AMBIGUOUS` carries
`related_uri=None`, and two legacy tags on one node share subject and
provenance). Matching by message text would mean re-parsing the package's
semantics, which the spec forbids — so the view hands both lists over as-is.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.epics import _manifest_path, _workspace_root

# The wait family: everything parse_fleet/check_fleet can say about a
# reference that did not become (or no longer is) a healthy edge.
# PF-BLOCKER-STALE is deliberately absent — it is already shown as the
# edge's state, and repeating it as a finding would double-report.
_WAIT_FINDING_PREFIXES = ("PF-BLOCKER-", "PF-ID-DANGLING", "PF-LEGACY-")
_STALE = "PF-BLOCKER-STALE"


class NodeRef(BaseModel):
    node_id: str
    repo: str
    title: str
    status: str  # declared_status: open | closed


class WaitEdge(BaseModel):
    source: NodeRef
    target: NodeRef
    state: str  # waiting | stale (stale = target closed, source still open)


class LooseRef(BaseModel):
    """A written reference that is text, not a relation — shown as such."""

    source_node_id: str
    repo: str
    raw_ref: str
    normalized: str | None  # legacy_blocker_ref, when the package derived one


class Finding(BaseModel):
    """One package diagnostic, verbatim — the view never rewords the canon."""

    code: str
    subject_uri: str | None
    related_uri: str | None
    message: str
    repo: str


class TriggerItem(BaseModel):
    node: NodeRef
    condition: str


class AbsentRepo(BaseModel):
    repo: str
    reason: str


class WaitsPlane(BaseModel):
    state: str  # read | partial | unavailable
    detail: str | None = None
    repos_read: int = 0


class WaitsView(BaseModel):
    todo_plane: WaitsPlane
    absent_repos: list[AbsentRepo]
    edges: list[WaitEdge]
    loose_refs: list[LooseRef]
    findings: list[Finding]
    triggers: list[TriggerItem]
    generated_at: str


def _unavailable(detail: str, generated_at: str) -> WaitsView:
    return WaitsView(
        todo_plane=WaitsPlane(state="unavailable", detail=detail),
        absent_repos=[],
        edges=[],
        loose_refs=[],
        findings=[],
        triggers=[],
        generated_at=generated_at,
    )


def _node_ref(node: dict[str, Any]) -> NodeRef:
    return NodeRef(
        node_id=node["node_id"],
        repo=node["repo"],
        title=node["title"],
        status=node["declared_status"],
    )


def build_waits(config: DispatcherConfig, *, now: str) -> WaitsView:
    """One pass over the live fleet checkouts; always returns a view.

    Unavailability is content, not a transport error: every failure mode of
    the source lands in `todo_plane`, and the HTTP layer stays 200.
    """
    manifest = _manifest_path(config)
    if manifest is None:
        return _unavailable(
            "workspace-manifest.toml not found; repo identity unresolvable", now
        )

    from plan_fields import (
        RepoInput,
        check_fleet,
        check_legacy_fleet,
        checkout_map,
        manifest_index,
        parse_fleet,
    )

    # Manifest/workspace failures (undecodable TOML, ambiguous git_dir, OS
    # errors while walking) happen before parse_fleet and would otherwise be a
    # 500 — the guard turns them into the plane's own unavailable.
    try:
        index = manifest_index(manifest)
        checkouts = checkout_map(_workspace_root(manifest), index)
    except Exception as exc:  # noqa: BLE001 — the guard IS the contract (§3.1)
        return _unavailable(f"{type(exc).__name__}: {exc}", now)

    # Inputs carry EVERY manifest repo, not just checkouts with a TODO.md —
    # that is what lets the package tell PF-BLOCKER-NO-TODO (checkout present,
    # nothing to read) from PF-BLOCKER-UNRESOLVABLE (no checkout here at all).
    # Skipping absent repos, as the epics plane does, would collapse both
    # cases into the second.
    inputs: list[RepoInput] = []
    absent: list[AbsentRepo] = []
    repos_read = 0
    for name in sorted(index.canonical_keys):
        root = checkouts.get(name)
        if root is None:
            inputs.append(RepoInput(name, None, available=False))
            absent.append(AbsentRepo(repo=name, reason="no checkout"))
            continue
        todo = root / "TODO.md"
        if not todo.is_file():
            inputs.append(RepoInput(name, None))
            absent.append(AbsentRepo(repo=name, reason="no TODO.md"))
            continue
        try:
            text = todo.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # An unreadable file maps to the same RepoInput as "no TODO.md":
            # the package's signature has no third state, and inventing one is
            # a canon change, not this view's. The true reason lives here.
            inputs.append(RepoInput(name, None))
            absent.append(
                AbsentRepo(repo=name, reason=f"unreadable: {type(exc).__name__}")
            )
            continue
        inputs.append(RepoInput(name, text))
        repos_read += 1

    if repos_read == 0:
        return _unavailable("no TODO.md checked out", now)

    try:
        doc = parse_fleet(inputs, index)
        graph_findings = check_fleet(doc)
        # The canonical pipeline drops an item with no @id at PF-ID-MISSING,
        # so its @blocked_by never reaches references — a wait on such an item
        # would silently read as "no obligation" (review finding on #207).
        # check_legacy_fleet is the package's own pass over exactly those
        # sources; `exclude` keeps anything the canonical plane already owns.
        canonical_refs = {
            (ref["provenance"]["repo"], ref["raw_ref"]) for ref in doc["references"]
        }
        legacy_findings = check_legacy_fleet(inputs, index, exclude=canonical_refs)
    except Exception as exc:  # noqa: BLE001 — package failure, same contract
        return _unavailable(f"{type(exc).__name__}: {exc}", now)

    by_id = {n["node_id"]: n for n in doc["nodes"]}
    stale_pairs = {
        (d["subject_uri"], d["related_uri"])
        for d in graph_findings
        if d["code"] == _STALE
    }

    edges: list[WaitEdge] = []
    for edge in doc["edges"]:
        src = by_id.get(edge["source_node_id"])
        tgt = by_id.get(edge["target_node_id"])
        if src is None or tgt is None:
            continue
        state = (
            "stale"
            if (edge["source_node_id"], edge["target_node_id"]) in stale_pairs
            else "waiting"
        )
        edges.append(
            WaitEdge(source=_node_ref(src), target=_node_ref(tgt), state=state)
        )
    edges.sort(key=lambda e: (e.target.node_id, e.source.node_id))

    loose: list[LooseRef] = []
    for ref in doc["references"]:
        if ref["resolved_target"] is not None:
            continue
        loose.append(
            LooseRef(
                source_node_id=ref["source_node_id"],
                repo=ref["provenance"]["repo"],
                raw_ref=ref["raw_ref"],
                normalized=ref.get("legacy_blocker_ref"),
            )
        )
    loose.sort(key=lambda r: (r.source_node_id, r.raw_ref))

    findings: list[Finding] = []
    # canonicalize() already ordered doc["diagnostics"]; graph findings come
    # sorted from check_fleet — appending keeps each canon's own order (§3.2).
    for diag in [*doc["diagnostics"], *graph_findings]:
        if diag["code"] == _STALE:
            continue
        if not diag["code"].startswith(_WAIT_FINDING_PREFIXES):
            continue
        findings.append(
            Finding(
                code=diag["code"],
                subject_uri=diag["subject_uri"],
                related_uri=diag["related_uri"],
                message=diag["message"],
                repo=diag["provenance"]["repo"],
            )
        )

    triggers = [
        TriggerItem(node=_node_ref(n), condition=n["trigger"])
        for n in doc["nodes"]
        if n["declared_status"] == "open" and not n["tombstone"] and n["trigger"]
    ]
    triggers.sort(key=lambda t: t.node.node_id)

    # Legacy pass verdicts, verbatim. Unlike the canonical PF-BLOCKER-STALE
    # (excluded above — the edge itself carries it), a legacy STALE stays: an
    # un-@id'd source has no edge to carry the state, so the finding is the
    # ONLY place a delivered wait on such an item is visible. A live, healthy
    # legacy blocker returns no diagnostic by the package's design — it
    # becomes visible on this surface when the item gains an @id.
    for legacy in legacy_findings:
        findings.append(
            Finding(
                code=legacy.code,
                subject_uri=None,
                related_uri=None,
                message=legacy.message,
                repo=legacy.source_repo,
            )
        )

    return WaitsView(
        todo_plane=WaitsPlane(
            state="partial" if absent else "read", repos_read=repos_read
        ),
        absent_repos=absent,
        edges=edges,
        loose_refs=loose,
        findings=findings,
        triggers=triggers,
        generated_at=now,
    )


__all__ = [
    "AbsentRepo",
    "Finding",
    "LooseRef",
    "NodeRef",
    "TriggerItem",
    "WaitEdge",
    "WaitsPlane",
    "WaitsView",
    "build_waits",
]
