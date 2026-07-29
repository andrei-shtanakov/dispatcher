"""PF-7 fleet API: cross-repo resolution, the five target outcomes, determinism."""

from __future__ import annotations

from plan_fields.fleet import ManifestIndex
from plan_fields.fleet_api import RepoInput, check_fleet, parse_fleet
from plan_fields.validator import validate_document

MANIFEST = ManifestIndex(
    frozenset({"maestro", "proctor", "arbiter", "atp-platform"}), {}
)


def _edges(snap):
    return {(e["source_node_id"], e["target_node_id"]) for e in snap["edges"]}


def _codes(snap):
    return sorted(d["code"] for d in snap["diagnostics"])


# --- the live PF-2B1 edge, reproduced as a regression -------------------------
MAESTRO_TODO = (
    "## routing\n"
    "- [ ] **R-03b**: Mode 2 workstream-level routing @owner:andrei @id:r-03b\n"
)
PROCTOR_TODO_CANONICAL = (
    "## arbiter routing\n"
    "- [ ] Опционально включить arbiter routing @owner:andrei "
    "@blocked_by:todo://maestro/r-03b @id:arbiter-routing-opt-in\n"
)
PROCTOR_TODO_LEGACY = (
    "## arbiter routing\n"
    "- [ ] Опционально включить arbiter routing @owner:andrei "
    "@blocked_by:maestro#r-03b @id:arbiter-routing-opt-in\n"
)


def _fleet(proctor_todo, extra=()):
    inputs = [
        RepoInput("maestro", MAESTRO_TODO, commit="d0fd80e"),
        RepoInput("proctor", proctor_todo, commit="f839292"),
        *extra,
    ]
    return parse_fleet(inputs, MANIFEST, generated_at="2026-07-28T00:00:00Z")


def test_live_edge_resolves_across_repos() -> None:
    snap = _fleet(PROCTOR_TODO_CANONICAL)
    validate_document(snap)
    assert ("todo://proctor/arbiter-routing-opt-in", "todo://maestro/r-03b") in _edges(
        snap
    )
    # the reference is now resolved, and carries the pinned commit
    ref = next(r for r in snap["references"] if r["kind"] == "blocked_by")
    assert ref["resolved_target"] == "todo://maestro/r-03b"
    assert ref["provenance"]["commit"] == "f839292"
    # no cross-repo defect diagnostics for a clean fleet
    assert not [d for d in snap["diagnostics"] if d["code"].startswith("PF-BLOCKER")]


def test_migrating_legacy_to_canonical_is_what_creates_the_edge() -> None:
    """DoD, inverted by the owner's ruling: the migration IS the change.

    This case used to assert that a legacy `maestro#r-03b` and a canonical
    `todo://maestro/r-03b` produced the SAME edge — representation changing
    without the relation changing. Canon says otherwise, unqualified: edges are
    only resolved `todo:// -> canonical` relations, and a legacy reference is
    never promoted to one however cleanly it resolves. So the legacy form
    carries the blocker as a REFERENCE and nothing else; migrating it to an @id
    is precisely what puts the relation into the graph. Edge-eligibility is
    decided by the reference's syntax, never by whether its slug happens to
    match.
    """
    before = _fleet(PROCTOR_TODO_LEGACY)
    after = _fleet(PROCTOR_TODO_CANONICAL)
    edge = ("todo://proctor/arbiter-routing-opt-in", "todo://maestro/r-03b")
    assert _edges(before) == set()  # legacy: a reference, not a relation
    assert _edges(after) == {edge}  # canonical: the relation enters the graph
    b = next(r for r in before["references"] if r["kind"] == "blocked_by")
    a = next(r for r in after["references"] if r["kind"] == "blocked_by")
    # the blocker is not lost in the legacy form — it is recorded as text
    assert b["raw_ref"] == "maestro#r-03b"
    assert b["legacy_blocker_ref"] == "maestro#r-03b"  # already canonical here
    assert b["resolved_target"] is None  # emitted as null, never omitted
    assert "resolved_target" in b  # schema: required, oneOf [CanonicalUri, null]
    assert a["raw_ref"] == "todo://maestro/r-03b" and a["legacy_blocker_ref"] is None
    assert a["resolved_target"] == "todo://maestro/r-03b"
    # and the legacy form is not silently dropped from the document either
    validate_document(before)


def test_a_same_repo_legacy_ref_is_no_more_an_edge_than_a_cross_repo_one() -> None:
    """The rule is the reference's syntax, so the layer it is resolved at cannot
    change it. `parse_todo` used to resolve a legacy slug against its OWN repo's
    ids and emit an edge — the same transitional shortcut, one layer down, and
    the only place left where a `<repo>#<slug>` could still become a relation.
    """
    from plan_fields.parser import parse_todo

    doc = parse_todo(
        "- [ ] target work @owner:o @id:r-03b\n"
        "- [ ] blocked @owner:o @blocked_by:demo#r-03b @id:x\n",
        "demo",
    )
    validate_document(doc)
    assert doc["edges"] == []  # a unique local match is still not a relation
    ref = next(r for r in doc["references"] if r["kind"] == "blocked_by")
    assert ref["raw_ref"] == "demo#r-03b"
    assert ref["legacy_blocker_ref"] == "demo#r-03b"
    assert ref["resolved_target"] is None
    # and a unique match is not "ambiguous" either — it is simply not an edge
    assert [d["code"] for d in doc["diagnostics"] if "LEGACY" in d["code"]] == []


# --- the five distinct target outcomes, as stable diagnostic codes ------------
def test_target_id_missing_is_dangling() -> None:
    proctor = "- [ ] x @owner:a @blocked_by:todo://maestro/does-not-exist @id:x\n"
    snap = _fleet(proctor)
    assert "PF-ID-DANGLING" in _codes(snap)
    assert not _edges(snap)


def test_repo_not_in_manifest_is_plan_defect() -> None:
    proctor = "- [ ] x @owner:a @blocked_by:todo://operator-host/y @id:x\n"
    snap = _fleet(proctor)
    assert "PF-BLOCKER-REPO-UNKNOWN" in _codes(snap)
    # distinct from the environmental codes
    assert "PF-BLOCKER-UNRESOLVABLE" not in _codes(snap)


def test_manifest_repo_not_checked_out_is_unresolvable() -> None:
    # arbiter is in the manifest but not among the inputs -> environmental
    proctor = "- [ ] x @owner:a @blocked_by:todo://arbiter/some-id @id:x\n"
    snap = _fleet(proctor)
    assert "PF-BLOCKER-UNRESOLVABLE" in _codes(snap)


def test_manifest_repo_explicitly_unavailable_is_unresolvable() -> None:
    proctor = "- [ ] x @owner:a @blocked_by:todo://arbiter/some-id @id:x\n"
    snap = _fleet(
        proctor, extra=(RepoInput("arbiter", todo_text=None, available=False),)
    )
    assert "PF-BLOCKER-UNRESOLVABLE" in _codes(snap)


def test_manifest_repo_without_todo_is_no_todo() -> None:
    proctor = "- [ ] x @owner:a @blocked_by:todo://arbiter/some-id @id:x\n"
    snap = _fleet(
        proctor, extra=(RepoInput("arbiter", todo_text=None, available=True),)
    )
    assert "PF-BLOCKER-NO-TODO" in _codes(snap)


def test_legacy_ambiguous_when_slug_matches_many() -> None:
    maestro = (
        "- [ ] seam one @owner:a @id:seam-one\n- [ ] seam two @owner:a @id:seam-two\n"
    )
    proctor = "- [ ] x @owner:a @blocked_by:maestro#seam @id:x\n"
    snap = parse_fleet(
        [RepoInput("maestro", maestro), RepoInput("proctor", proctor)], MANIFEST
    )
    assert "PF-LEGACY-AMBIGUOUS" in _codes(snap)
    assert not _edges(snap)


# --- structural invariants ----------------------------------------------------
def test_multivalue_blockers_preserved_as_two_edges() -> None:
    maestro = "- [ ] a @owner:o @id:a\n- [ ] b @owner:o @id:b\n"
    proctor = (
        "- [ ] c @owner:o @blocked_by:todo://maestro/a "
        "@blocked_by:todo://maestro/b @id:c\n"
    )
    snap = parse_fleet(
        [RepoInput("maestro", maestro), RepoInput("proctor", proctor)], MANIFEST
    )
    refs = [r for r in snap["references"] if r["source_node_id"] == "todo://proctor/c"]
    assert len(refs) == 2
    assert _edges(snap) == {
        ("todo://proctor/c", "todo://maestro/a"),
        ("todo://proctor/c", "todo://maestro/b"),
    }


def test_snapshot_is_contract_valid_and_deterministic() -> None:
    snap1 = _fleet(PROCTOR_TODO_CANONICAL)
    snap2 = _fleet(PROCTOR_TODO_CANONICAL)
    validate_document(snap1)
    assert snap1 == snap2  # pure function of inputs -> byte-stable


def test_parse_fleet_rejects_duplicate_repo_inputs() -> None:
    import pytest

    with pytest.raises(ValueError, match="duplicate"):
        parse_fleet(
            [RepoInput("maestro", MAESTRO_TODO), RepoInput("maestro", MAESTRO_TODO)],
            MANIFEST,
        )


def test_intra_repo_diagnostics_survive_the_merge() -> None:
    # an open item with no @id in one repo still reports PF-ID-MISSING at fleet level
    snap = _fleet("- [ ] open item without any id here\n")
    assert "PF-ID-MISSING" in _codes(snap)


# --- check_fleet: graph-semantic stale ---------------------------------------
def test_check_fleet_flags_stale_blocker() -> None:
    maestro = "- [x] done blocker @owner:o @id:done\n"  # closed target
    proctor = "- [ ] still open @owner:o @blocked_by:todo://maestro/done @id:s\n"
    snap = parse_fleet(
        [RepoInput("maestro", maestro), RepoInput("proctor", proctor)], MANIFEST
    )
    # the edge resolves (target exists) ...
    assert ("todo://proctor/s", "todo://maestro/done") in _edges(snap)
    # ... but check_fleet flags that the blocker is already closed
    stale = check_fleet(snap)
    assert [d["code"] for d in stale] == ["PF-BLOCKER-STALE"]
    assert stale[0]["subject_uri"] == "todo://proctor/s"


def test_check_fleet_quiet_when_blocker_open() -> None:
    snap = _fleet(PROCTOR_TODO_CANONICAL)
    assert check_fleet(snap) == []


# --- contract discipline: every fleet code is registered ----------------------
FLEET_CODES = {
    "PF-BLOCKER-REPO-UNKNOWN",
    "PF-BLOCKER-UNRESOLVABLE",
    "PF-BLOCKER-NO-TODO",
    "PF-BLOCKER-STALE",
    "PF-ID-DANGLING",
    "PF-LEGACY-AMBIGUOUS",
}


def test_every_fleet_code_is_in_the_vendored_registry() -> None:
    import re
    from pathlib import Path

    import plan_fields

    registry = (
        Path(plan_fields.__file__).parent / "contract" / "diagnostics.yaml"
    ).read_text(encoding="utf-8")
    registered = set(re.findall(r"^  (PF-[A-Z0-9-]+):", registry, re.MULTILINE))
    missing = FLEET_CODES - registered
    assert not missing, (
        f"fleet emits codes absent from the contract registry: {missing}"
    )
