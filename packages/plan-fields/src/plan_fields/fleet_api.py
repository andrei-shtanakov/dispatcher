"""Fleet API (PF-7): resolve cross-repo plan graphs from frozen, pinned inputs.

``parse_todo`` sees one repo and leaves every cross-repo ``@blocked_by`` reference
unresolved — a single file cannot know whether ``todo://maestro/r-03b`` names a
real node. The fleet API is that missing resolver: it takes the ALREADY-COLLECTED
snapshot of each repo (its canonical name, pinned commit, and TODO.md text) and
builds one canonical graph in which cross-repo edges resolve.

Two deliberate boundaries:

* **No discovery.** ``parse_fleet`` never lists a directory, reads git, or hits the
  network. The caller freezes the inputs (a manifest-pinned scan) and passes them
  in. What repos exist is the *manifest*'s call, never folder presence.
* **Manifest is the authority.** A ``@blocked_by`` whose target repo is not in the
  frozen manifest is a PLAN defect (``PF-BLOCKER-REPO-UNKNOWN``); a manifest repo
  that simply is not checked out here is ENVIRONMENTAL (``PF-BLOCKER-UNRESOLVABLE``).
  The two are never conflated — the PF-2B0 not-in-manifest vs absent split, now at
  the canonical layer.

The returned snapshot is the same contract-valid envelope ``parse_todo`` emits
(schema.json), just spanning the whole fleet. ``check_fleet`` adds the one
graph-semantic diagnostic that needs the resolved graph but not the inputs
(``PF-BLOCKER-STALE``).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from plan_fields.canonical import canonicalize
from plan_fields.fleet import AmbiguousIdentityError, ManifestIndex
from plan_fields.parser import (
    CONTRACT_VERSION,
    LEGACY_RE,
    SCHEMA_VERSION,
    TODO_URI_RE,
    _diag,
    parse_todo,
)
from plan_fields.scrape import ScrapedItem, scrape_items

# The canonical parser's LEGACY_RE requires a lowercase repo; the transitional
# legacy resolver must also match the historical mixed-case citations that
# predate the canonical-name ruling (e.g. `Maestro#R-03b`), so it case-folds.
_LEGACY_REF_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)#(\S+)$", re.IGNORECASE)


@dataclass(frozen=True)
class RepoInput:
    """One repo's frozen contribution to a fleet scan.

    ``available`` is False when the manifest declares the repo but no checkout is
    present on this host; ``todo_text`` is None when the checkout is present but
    keeps no ``TODO.md``. The two are distinct environmental states.
    """

    repo: str
    todo_text: str | None = None
    commit: str | None = None
    available: bool = True


def _canonical_input_repos(
    inputs: Sequence[RepoInput], index: ManifestIndex, caller: str
) -> list[str]:
    """One canonical repo name per input, in order, refusing two spellings of one.

    An input's own name is normalised on the same rule as the names references
    are written with. A caller that supplies a repo under its ``git_dir``
    spelling would otherwise key the scan by that spelling, so an inbound
    reference — correctly normalised to the key — would find nothing.

    Normalising can MERGE two names that were distinct as written, which is why
    the duplicate refusal lives here rather than in one caller: without it the
    second input silently overwrites the first, the loser's whole TODO.md
    disappears from the answer, and which one loses depends on input order. An
    ambiguity that resolves by argument order is the same defect
    ``checkout_map`` refuses on disk.
    """
    repos = [index.resolve_ref(inp.repo) for inp in inputs]
    dupes = sorted({r for r in repos if repos.count(r) > 1})
    if dupes:
        raise AmbiguousIdentityError(
            f"{caller}: duplicate RepoInput for repo(s) {dupes}"
        )
    return repos


def _pin(doc: dict[str, Any], commit: str | None) -> None:
    """Stamp the pinned commit onto every provenance block in a parsed doc."""
    if commit is None:
        return
    for coll in (doc["nodes"], doc["references"], doc["diagnostics"]):
        for obj in coll:
            prov = obj.get("provenance")
            if isinstance(prov, dict):
                prov["commit"] = commit


def parse_fleet(
    inputs: Sequence[RepoInput],
    index: ManifestIndex,
    generated_at: str = "1970-01-01T00:00:00Z",
) -> dict[str, Any]:
    """Build one canonical, contract-valid snapshot across the frozen inputs.

    Intra-repo nodes/references/edges/diagnostics come straight from
    ``parse_todo``; this layer only resolves the cross-repo references it leaves
    open and emits the diagnostics that need manifest/availability knowledge.

    The manifest arrives as a ``ManifestIndex``, not a bare set of names: every
    written repo name is normalised through it before membership or
    availability is decided, so a reference spelled with a declared ``git_dir``
    locator reaches the same verdict as one spelled with the key.
    """
    repos = _canonical_input_repos(inputs, index, "parse_fleet")

    nodes: list[dict[str, Any]] = []
    references: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []

    present: dict[str, list[dict[str, Any]]] = {}  # repo -> its nodes
    no_todo: set[str] = set()

    for inp, repo in zip(inputs, repos):
        if not inp.available or inp.todo_text is None:
            # declared but not usable here: no checkout (available=False) or a
            # checkout with no TODO.md. Both are environmental; the target repo's
            # inbound refs resolve to NO-TODO / UNRESOLVABLE below.
            if inp.available and inp.todo_text is None:
                no_todo.add(repo)
            continue
        doc = parse_todo(inp.todo_text, repo, generated_at=generated_at)
        _pin(doc, inp.commit)
        nodes.extend(doc["nodes"])
        references.extend(doc["references"])
        edges.extend(doc["edges"])
        diagnostics.extend(doc["diagnostics"])
        present[repo] = doc["nodes"]

    node_ids = {n["node_id"] for n in nodes}

    # resolve every still-open cross-repo reference against the merged graph
    for ref in references:
        if ref["resolved_target"] is not None:
            continue
        src_repo = ref["provenance"]["repo"]
        edge, diag = _resolve_cross(ref, src_repo, index, present, node_ids, no_todo)
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


def _resolve_cross(ref, src_repo, index, present, node_ids, no_todo):
    """Resolve one cross-repo reference; returns (edge_or_None, diag_or_None).

    Mutates ``ref`` in place to set ``resolved_target`` when an edge forms, and
    to normalise ``legacy_blocker_ref`` when the reference was written with a
    declared locator. A same-repo reference left unresolved was already
    diagnosed by ``parse_todo`` (dangling / ambiguous), so it is skipped here.
    """
    raw = ref["raw_ref"]
    src = ref["source_node_id"]
    prov = ref["provenance"]

    m = TODO_URI_RE.match(raw)
    lm = None if m else LEGACY_RE.match(raw)
    if m:
        trepo, key, is_canonical = m.group(1), m.group(2), True
    elif lm:
        trepo, key, is_canonical = lm.group(1), lm.group(2), False
    else:
        return None, None  # malformed — not this layer's job (parity with parser)

    # The written repo name is normalised before anything is decided: a name
    # spelled with a declared locator names the same repo as one spelled with
    # the key. `raw_ref` keeps the spelling exactly as the author wrote it.
    canonical = index.resolve_ref(trepo)
    if canonical == src_repo:
        return None, None  # intra-repo miss already diagnosed by parse_todo

    if not is_canonical and canonical != trepo:
        # ADR-ECO-005: a legacy <repo>#<slug> written with a declared locator
        # normalises for LOOKUP only. legacy_blocker_ref carries the key so the
        # reference names a real repo, but the reference stays legacy — the
        # contract's load-bearing split is that edges come from identity and
        # references come from text, and a resolving alias does not cross it.
        ref["legacy_blocker_ref"] = f"{canonical}#{key}"

    reason = _repo_reason(trepo, index, present, no_todo)
    if reason is not None:
        code, msg = reason
        return None, _diag(code, "warning", src, raw, None, msg(raw, canonical), prov)

    # target repo is present and parsed — resolve within it
    if is_canonical:
        target = f"todo://{canonical}/{key}"
        if target in node_ids:
            ref["resolved_target"] = target
            return {
                "kind": "blocked_by",
                "source_node_id": src,
                "target_node_id": target,
            }, None
        return (
            None,
            _diag(
                "PF-ID-DANGLING",
                "warning",
                src,
                target,
                None,
                f"canonical reference {raw} points to an @id absent from repo "
                f"{canonical}",
                prov,
            ),
        )
    if canonical != trepo:
        # An alias-spelled legacy reference: normalised above for lookup, and
        # that is all it earns. It keeps no resolved_target and yields no edge
        # (contract, Identity & provenance) — the author migrates it to an @id.
        return None, None
    # legacy transitional resolution against the target repo's canonical nodes
    matches = _legacy_matches(present[canonical], key)
    if len(matches) == 1:
        target = matches[0]["node_id"]
        ref["resolved_target"] = target  # legacy_blocker_ref stays set (transitional)
        return {
            "kind": "blocked_by",
            "source_node_id": src,
            "target_node_id": target,
        }, None
    return (
        None,
        _diag(
            "PF-LEGACY-AMBIGUOUS",
            "warning",
            src,
            None,
            None,
            f"legacy reference {raw} matches "
            f"{'more than one' if matches else 'no'} item in repo {trepo}; migrate to an @id",
            prov,
        ),
    )


def _repo_reason(trepo, index, present, no_todo):
    """Why the target repo cannot host a resolvable node, or None if it can.

    The written name is normalised through the manifest before anything is
    decided: a reference spelled with a declared locator names the same repo as
    one spelled with the key, so both must reach the same verdict. Order still
    matters below — manifest membership (a plan defect) is decided before any
    environmental state, so a non-fleet repo is never excused as "not checked
    out". A manifest repo that was not parsed and has no TODO.md recorded — whether
    the input was ``available=False`` or simply omitted — collapses to the same
    not-checked-out environmental default (``PF-BLOCKER-UNRESOLVABLE``).
    """
    trepo = index.resolve_ref(trepo)
    if trepo not in index.canonical_keys:
        return (
            "PF-BLOCKER-REPO-UNKNOWN",
            lambda raw, r: (
                f"@blocked_by {raw} names repo {r}, absent from the frozen manifest"
            ),
        )
    if trepo in present:
        return None
    if trepo in no_todo:
        return (
            "PF-BLOCKER-NO-TODO",
            lambda raw, r: (
                f"@blocked_by {raw} names repo {r}, which keeps no TODO.md here"
            ),
        )
    return (
        "PF-BLOCKER-UNRESOLVABLE",
        lambda raw, r: (
            f"@blocked_by {raw} target repo {r} is not checked out on this host"
        ),
    )


def _legacy_matches(nodes: list[dict[str, Any]], slug: str) -> list[dict[str, Any]]:
    """Transitional legacy match: exact @id (case-insensitive), else slug in title.

    Once the target carries an @id equal to the slug this is exact; before the
    backfill it falls back to a title-substring match so a pre-@id target can
    still resolve. De-duplicated by node_id so one node never counts twice.
    """
    low = slug.lower()
    hits: dict[str, dict[str, Any]] = {}
    for n in nodes:
        if n["id"].lower() == low or low in n["title"].lower():
            hits[n["node_id"]] = n
    return list(hits.values())


def check_fleet(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Graph-semantic diagnostics derivable from a built snapshot alone.

    Currently ``PF-BLOCKER-STALE``: a resolved ``@blocked_by`` edge whose target
    is already closed/tombstoned while the source stays open — the blocker no
    longer blocks. Needs the resolved edges + node statuses, not the raw inputs,
    so it lives here rather than in ``parse_fleet``.
    """
    by_id = {n["node_id"]: n for n in snapshot["nodes"]}
    out: list[dict[str, Any]] = []
    for edge in snapshot["edges"]:
        src = by_id.get(edge["source_node_id"])
        tgt = by_id.get(edge["target_node_id"])
        if src is None or tgt is None:
            continue
        if src["declared_status"] == "open" and tgt["declared_status"] == "closed":
            out.append(
                _diag(
                    "PF-BLOCKER-STALE",
                    "warning",
                    src["node_id"],
                    tgt["node_id"],
                    None,
                    f"{src['node_id']} is open but its blocker {tgt['node_id']} is closed",
                    src["provenance"],
                )
            )
    out.sort(key=lambda d: (d["code"], d["subject_uri"] or "", d["related_uri"] or ""))
    return out


@dataclass(frozen=True)
class LegacyDiagnostic:
    """A TRANSITIONAL diagnostic over an un-@id'd source item's legacy
    ``<repo>#<slug>`` blocker — the graph a pre-PF-2B fleet still lives on.

    Deliberately NOT a canonical contract ``Diagnostic``: the source has no
    stable identity (``identity_grade="legacy"``), so this never becomes a
    canonical node/edge, never hardens to a blocking error (Phase 0b escalation
    needs a stable identity), and carries no durable governance fingerprint. The
    whole API is removable once fleet @id coverage is 100% and no legacy ref
    remains — every source it reports is exactly what PF-2B2 still has to
    migrate. When a source gains an @id it drops out of here and reappears in
    ``parse_fleet``'s canonical references/edges: the relation moves planes, it
    is never counted twice.
    """

    code: str
    severity: str  # always "warning" — transitional, never a blocking error
    source_repo: str
    source_line: int
    target_repo: str  # canonical (lower) name of the resolved target
    slug: str
    raw_ref: str  # `<repo>#<slug>` exactly as written — text, not identity
    message: str
    identity_grade: str = "legacy"


def check_legacy_fleet(
    inputs: Sequence[RepoInput],
    index: ManifestIndex,
    exclude: set[tuple[str, str]] | None = None,
) -> list[LegacyDiagnostic]:
    """Resolve the legacy ``<repo>#<slug>`` blocker graph over un-@id'd sources.

    Built on ``scrape_items`` (the operational substrate, no @id required), so it
    sees the blockers ``parse_fleet`` cannot: those on items with no @id yet.
    Reproduces the pre-package devtools resolution — substring match against the
    target item's text, open/closed staleness — reporting not-in-manifest /
    unresolvable / no-todo / missing / stale outcomes. It never touches a
    relation the canonical pipeline owns: a source carrying an @id is skipped
    (its refs are ``parse_fleet``'s), and ``exclude`` (a set of
    ``(source_repo, raw_ref)`` — e.g. the canonical references) drops any
    remainder. Every ``@blocked_by`` on an item is kept (multiple blockers
    preserved). All findings are warnings.

    The manifest arrives as a ``ManifestIndex``: each written repo name is
    normalised to its canonical key before membership is decided, so a
    reference spelled with a declared locator reaches the same verdict as one
    spelled with the key. ``target_repo`` then carries the key and ``raw_ref``
    the spelling as written.
    """
    exclude = exclude or set()
    repos = _canonical_input_repos(inputs, index, "check_legacy_fleet")
    scraped: dict[str, list[ScrapedItem]] = {}
    no_todo: set[str] = set()
    for inp, srepo in zip(inputs, repos):
        if not inp.available or inp.todo_text is None:
            if inp.available and inp.todo_text is None:
                no_todo.add(srepo)
            continue
        scraped[srepo] = scrape_items(inp.todo_text)

    out: list[LegacyDiagnostic] = []
    for srepo, items in scraped.items():
        for item in items:
            # @id'd or closed sources are the canonical pipeline's job (or not
            # actionable); only open, un-@id'd items still carry a legacy graph.
            if item.item_id or item.checked:
                continue
            for raw in item.values("blocked_by"):
                if (srepo, raw) in exclude:
                    continue
                m = _LEGACY_REF_RE.match(raw)
                if not m:
                    continue  # todo:// (canonical) or malformed — not legacy
                diag = _legacy_resolve(
                    srepo, item.line, m.group(1), m.group(2), index, scraped, no_todo
                )
                if diag is not None:
                    out.append(diag)
    out.sort(key=lambda d: (d.source_repo, d.source_line, d.target_repo, d.slug))
    return out


def _slug_hits_item(item: ScrapedItem, slug: str) -> bool:
    """Does ``slug`` name this target item? Exact @id (case-insensitive) when the
    item carries one, else the slug in its tag-stripped ``display_text`` (also
    case-insensitive) — matching ``fleet._matches``. Deliberately NOT ``raw_text``:
    a slug must not resolve because it happens to appear inside an unrelated tag
    value (another ``@blocked_by``), and casing must not decide a real relation.
    """
    low = slug.lower()
    if item.item_id and item.item_id.lower() == low:
        return True
    return low in item.display_text.lower()


def _legacy_resolve(srepo, line, trepo_raw, slug, index, scraped, no_todo):
    """Reproduce the old devtools per-blocker outcome, or None when it resolves."""
    # target_repo is identity, raw_ref is text. Never let the raw spelling
    # reach target_repo: a legacy reference resolves *as* the key, and the
    # reader still needs to see what was actually written.
    tkey = index.resolve_ref(trepo_raw)
    raw_ref = f"{trepo_raw}#{slug}"
    if tkey == srepo:  # srepo is already normalised by the caller
        return None  # self-blocker: the repo's own check covers it

    def diag(code: str, msg: str) -> LegacyDiagnostic:
        return LegacyDiagnostic(code, "warning", srepo, line, tkey, slug, raw_ref, msg)

    where = f"{srepo}/TODO.md:{line}"
    if tkey not in index.canonical_keys:
        return diag(
            "PF-BLOCKER-REPO-UNKNOWN",
            f"{where} @blocked_by:{trepo_raw}#{slug} names '{tkey}', absent from "
            f"the frozen manifest",
        )
    if tkey in no_todo:
        return diag(
            "PF-BLOCKER-NO-TODO",
            f"{where} blocked on '{tkey}', which keeps no TODO.md; the claim "
            f"cannot be verified from here",
        )
    if tkey not in scraped:
        return diag(
            "PF-BLOCKER-UNRESOLVABLE",
            f"{where} @blocked_by names '{tkey}', not checked out on this host; "
            f"unresolvable here",
        )
    hits = [i for i in scraped[tkey] if _slug_hits_item(i, slug)]
    if not hits:
        return diag(
            "PF-BLOCKER-DANGLING",
            f"{where} '{slug}' not found in {tkey}/TODO.md; renamed, or never "
            f"tracked there",
        )
    if all(i.checked for i in hits):
        lines = ", ".join(f":{i.line}" for i in hits)
        return diag(
            "PF-BLOCKER-STALE",
            f"{where} blocked on '{tkey}#{slug}', which {tkey} has already "
            f"completed ({lines}); the wait is over",
        )
    return None  # at least one open hit — a live, valid legacy blocker
