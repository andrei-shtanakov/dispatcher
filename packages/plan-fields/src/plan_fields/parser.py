"""Parse a single repo's TODO.md into a canonical plan-fields document.

Implements the PF-1 contract semantics that are provable from ONE snapshot:
identity/tombstone extraction, canonical + legacy @blocked_by resolution, and the
diagnostics PF-ID-MISSING / PF-ID-DUPLICATE / PF-ID-DANGLING / PF-LEGACY-AMBIGUOUS /
PF-OWNER-MISSING / PF-OWNER-GRAMMAR. History-dependent findings (PF-ID-REUSED,
PF-TOMBSTONE-REMOVED) need cross-snapshot context and live in ``reuse.py``. Blocker
stale/no-todo/unresolvable and PF-RECHECK-EXPIRED are not yet implemented in v0.1.
"""

from __future__ import annotations

import re
from typing import Any

from plan_fields.canonical import canonicalize
from plan_fields.scrape import _canonical_title, scrape_items

CONTRACT_VERSION = 1
SCHEMA_VERSION = 1

TODO_URI_RE = re.compile(r"^todo://([a-z0-9][a-z0-9-]*)/([a-z0-9][a-z0-9._-]{0,63})$")
LEGACY_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)#(\S+)$")
ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")

FRESHNESS = {
    "source-ref": "source_ref",
    "observed-at": "observed_at",
    "recheck-by": "recheck_by",
}


def _prov(repo: str, path: str, line: int) -> dict[str, Any]:
    return {
        "repo": repo,
        "remote_origin": None,
        "commit": None,
        "path": path,
        "line": line,
    }


def _freshness(raw: dict[str, str]) -> dict[str, Any] | None:
    if not any(k in raw for k in FRESHNESS):
        return None
    return {field: raw.get(tag) for tag, field in FRESHNESS.items()}


def parse_todo(
    text: str,
    repo: str,
    path: str = "TODO.md",
    generated_at: str = "1970-01-01T00:00:00Z",
) -> dict[str, Any]:
    """Parse one repo's TODO.md text into a canonical plan-fields document."""
    nodes: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    # first pass: collect nodes (need the id set before resolving references).
    # Built on the shared operational scrape — one tokenizer for the whole fleet.
    parsed: list[tuple[dict[str, Any], tuple[str, ...]]] = []
    for item in scrape_items(text):
        status = "closed" if item.checked else "open"
        raw = dict(item.tags)
        title = _canonical_title(item.raw_text)
        item_id = item.item_id
        prov = _prov(repo, path, item.line)
        if not item_id:
            # PF-ID-MISSING is for ACTIONABLE (open) items only; a closed line
            # without an @id is not actionable, so it is silently skipped.
            if status == "open":
                diagnostics.append(
                    _diag(
                        "PF-ID-MISSING",
                        "warning",
                        None,
                        None,
                        None,
                        f"actionable item in repo {repo} has no @id (line {item.line})",
                        prov,
                    )
                )
            continue
        node_id = f"todo://{repo}/{item_id}"
        owner = raw.get("owner")
        node = {
            "node_id": node_id,
            "kind": "operational_item",
            "id": item_id,
            "repo": repo,
            "title": title,
            "declared_status": status,
            "owner_role": owner,
            "trigger": raw.get("trigger"),
            "freshness": _freshness(raw),
            "tombstone": status == "closed",
            "raw": dict(raw),
            "provenance": prov,
        }
        nodes.append(node)
        # every @blocked_by on the item, in source order — a key can repeat, and
        # each blocker gets its own reference below (never collapsed to one)
        parsed.append((node, item.values("blocked_by")))
        if owner is None:
            diagnostics.append(
                _diag(
                    "PF-OWNER-MISSING",
                    "warning",
                    node_id,
                    None,
                    None,
                    f"{node_id} has no @owner",
                    prov,
                )
            )
        elif not ROLE_RE.match(owner):
            diagnostics.append(
                _diag(
                    "PF-OWNER-GRAMMAR",
                    "warning",
                    node_id,
                    None,
                    None,
                    f"@owner '{owner}' is not a role-slug",
                    prov,
                )
            )

    ids = {n["id"] for n in nodes}
    # duplicate @id (error) — one diagnostic per duplicated id, at the later line
    seen: dict[str, dict[str, Any]] = {}
    for n in nodes:
        prev = seen.get(n["id"])
        if prev is not None:
            diagnostics.append(
                _diag(
                    "PF-ID-DUPLICATE",
                    "error",
                    n["node_id"],
                    None,
                    None,
                    f"@id '{n['id']}' appears on two items in repo {repo} "
                    f"(lines {prev['provenance']['line']} and {n['provenance']['line']})",
                    n["provenance"],
                )
            )
        else:
            seen[n["id"]] = n

    # references + edges: one reference per @blocked_by (repeats preserved)
    for node, blockers in parsed:
        for blocked in blockers:
            ref, edge, diag = _resolve(node, blocked, repo, nodes, ids)
            references.append(ref)
            if edge is not None:
                edges.append(edge)
            if diag is not None:
                diagnostics.append(diag)

    doc = {
        "contract_version": CONTRACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "nodes": nodes,
        "references": references,
        "edges": edges,
        "diagnostics": diagnostics,
    }
    return canonicalize(doc)


def _resolve(node, blocked, repo, nodes, ids):
    src = node["node_id"]
    prov = node["provenance"]
    ref = {
        "kind": "blocked_by",
        "source_node_id": src,
        "raw_ref": blocked,
        "resolved_target": None,
        "legacy_blocker_ref": None,
        "provenance": prov,
    }
    m = TODO_URI_RE.match(blocked)
    if m:
        target_repo, target_id = m.group(1), m.group(2)
        if target_repo != repo:
            # cross-repo canonical: v0.1 has no fleet inputs to decide existence,
            # so leave it unresolved WITHOUT a diagnostic (cross-repo is Phase 0b).
            return ref, None, None
        if target_id in ids:
            ref["resolved_target"] = blocked
            return (
                ref,
                {
                    "kind": "blocked_by",
                    "source_node_id": src,
                    "target_node_id": blocked,
                },
                None,
            )
        return (
            ref,
            None,
            _diag(
                "PF-ID-DANGLING",
                "warning",
                src,
                blocked,
                None,
                f"canonical reference {blocked} points to an @id absent from repo {repo}",
                prov,
            ),
        )
    lm = LEGACY_RE.match(blocked)
    if lm:
        ref["legacy_blocker_ref"] = blocked
        legacy_repo, slug = lm.group(1), lm.group(2)
        if legacy_repo != repo:
            # cross-repo legacy: do NOT resolve against local ids (Phase 0b).
            return ref, None, None
        return ref, None, legacy_self_diagnostic(nodes, slug, blocked, repo, src, prov)
    return ref, None, None


def legacy_self_diagnostic(
    nodes: list[dict[str, Any]],
    slug: str,
    blocked: str,
    repo: str,
    src: str,
    prov: dict[str, Any],
) -> dict[str, Any] | None:
    """Own the verdict on a SAME-REPO legacy ``<repo>#<slug>``: a diagnostic, or None.

    The single owner of that verdict, called by BOTH resolvers so the answer
    cannot depend on which one reached the reference. ``parse_todo`` calls it
    for a reference spelled with the repo's own name; the fleet layer — which
    has the manifest ``parse_todo`` deliberately does not — normalises a
    declared ``git_dir`` spelling and calls it for that case. Sharing the
    function, rather than reimplementing the rule beside it, is what keeps the
    two spellings identical in matching rule, code, severity, ``rule_id`` and
    wording; a second implementation would drift from this one silently.

    A unique match is still not a relation — edges are only resolved
    ``todo:// -> canonical`` ones, so a legacy reference stays in ``references``
    however cleanly it resolves, in its own repo exactly as across repos. Zero
    or several matches is ``PF-LEGACY-AMBIGUOUS``.
    """
    matches = [n for n in nodes if slug in n["id"]]
    if len(matches) == 1:
        return None
    return _diag(
        "PF-LEGACY-AMBIGUOUS",
        "warning",
        src,
        None,
        None,
        f"legacy reference {blocked} matches "
        f"{'more than one' if matches else 'no'} item in repo {repo}; "
        f"migrate to an @id",
        prov,
    )


def _diag(code, severity, subject, related, rule, message, prov):
    return {
        "code": code,
        "severity": severity,
        "subject_uri": subject,
        "related_uri": related,
        "rule_id": rule,
        "message": message,
        "provenance": prov,
    }
