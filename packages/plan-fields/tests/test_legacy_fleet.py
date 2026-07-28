"""check_legacy_fleet — the transitional legacy blocker graph over un-@id'd sources.

Reproduces the pre-package devtools resolution; every finding is a warning with
identity_grade='legacy'; a source that gains an @id migrates to the canonical
pipeline and disappears from here.
"""

from __future__ import annotations

from plan_fields.fleet_api import (
    RepoInput,
    check_fleet,
    check_legacy_fleet,
    parse_fleet,
)

MANIFEST = {"maestro", "proctor", "arbiter", "atp-platform"}


def test_missing_slug_is_dangling() -> None:
    inputs = [
        RepoInput("maestro", "- [ ] R-03b something @owner:o\n"),
        RepoInput("proctor", "- [ ] x @owner:o @blocked_by:maestro#gone\n"),
    ]
    d = check_legacy_fleet(inputs, MANIFEST)
    assert [x.code for x in d] == ["PF-BLOCKER-DANGLING"]
    assert d[0].identity_grade == "legacy" and d[0].severity == "warning"
    assert d[0].source_repo == "proctor" and d[0].target_repo == "maestro"


def test_stale_when_target_only_closed() -> None:
    inputs = [
        RepoInput("maestro", "- [x] done shipped\n"),
        RepoInput("proctor", "- [ ] x @owner:o @blocked_by:maestro#done\n"),
    ]
    d = check_legacy_fleet(inputs, MANIFEST)
    assert [x.code for x in d] == ["PF-BLOCKER-STALE"]
    assert d[0].severity == "warning"  # transitional: never a blocking error


def test_open_target_is_a_valid_blocker_no_diagnostic() -> None:
    inputs = [
        RepoInput("maestro", "- [ ] done still open\n"),
        RepoInput("proctor", "- [ ] x @owner:o @blocked_by:maestro#done\n"),
    ]
    assert check_legacy_fleet(inputs, MANIFEST) == []


def test_mixed_open_and_closed_hits_is_valid_like_old_devtools() -> None:
    # old devtools: any open hit -> valid; only all-closed -> stale
    maestro = "- [x] seam closed\n- [ ] seam open\n"
    inputs = [
        RepoInput("maestro", maestro),
        RepoInput("proctor", "- [ ] x @owner:o @blocked_by:maestro#seam\n"),
    ]
    assert check_legacy_fleet(inputs, MANIFEST) == []


def test_three_repo_states_are_distinct() -> None:
    proctor = (
        "- [ ] a @owner:o @blocked_by:operator-host#y\n"  # not in manifest
        "- [ ] b @owner:o @blocked_by:atp-platform#z\n"  # manifest, no TODO
        "- [ ] c @owner:o @blocked_by:arbiter#w\n"  # manifest, not checked out
    )
    inputs = [
        RepoInput("proctor", proctor),
        RepoInput("atp-platform", todo_text=None, available=True),
    ]
    by_target = {x.target_repo: x.code for x in check_legacy_fleet(inputs, MANIFEST)}
    assert by_target["operator-host"] == "PF-BLOCKER-REPO-UNKNOWN"
    assert by_target["atp-platform"] == "PF-BLOCKER-NO-TODO"
    assert by_target["arbiter"] == "PF-BLOCKER-UNRESOLVABLE"


def test_case_insensitive_target_and_multiple_blockers_preserved() -> None:
    inputs = [
        RepoInput("maestro", "- [ ] u open\n"),
        RepoInput(
            "proctor",
            "- [ ] c @owner:o @blocked_by:Maestro#gone1 @blocked_by:maestro#gone2\n",
        ),
    ]
    d = check_legacy_fleet(inputs, MANIFEST)
    assert sorted(x.slug for x in d) == ["gone1", "gone2"]
    assert {x.target_repo for x in d} == {"maestro"}  # 'Maestro' folded to canonical


def test_slug_only_inside_a_tag_value_does_not_resolve() -> None:
    # the slug appears only inside the target item's tag value, never its prose:
    # matching display_text (not raw_text) must NOT count that as a hit.
    maestro = '- [ ] unrelated work @owner:o @trigger:"myslug shipped"\n'
    inputs = [
        RepoInput("maestro", maestro),
        RepoInput("proctor", "- [ ] x @owner:o @blocked_by:maestro#myslug\n"),
    ]
    d = check_legacy_fleet(inputs, MANIFEST)
    assert [x.code for x in d] == ["PF-BLOCKER-DANGLING"]  # not a false hit


def test_slug_match_is_case_insensitive() -> None:
    # target names it "R-07" in prose; a lowercase legacy ref still resolves
    maestro = "- [ ] ship the R-07 feature now @owner:o\n"
    inputs = [
        RepoInput("maestro", maestro),
        RepoInput("proctor", "- [ ] x @owner:o @blocked_by:maestro#r-07\n"),
    ]
    assert check_legacy_fleet(inputs, MANIFEST) == []  # open hit, valid blocker


def test_exact_id_resolves_even_when_title_differs() -> None:
    # target carries @id:done but its title is unrelated; a legacy slug 'done'
    # resolves via the exact @id, and it is closed -> stale
    maestro = "- [x] Mode 2 workstream routing @owner:o @id:done\n"
    inputs = [
        RepoInput("maestro", maestro),
        RepoInput("proctor", "- [ ] x @owner:o @blocked_by:maestro#done\n"),
    ]
    d = check_legacy_fleet(inputs, MANIFEST)
    assert [x.code for x in d] == ["PF-BLOCKER-STALE"]


def test_self_blocker_is_ignored() -> None:
    inputs = [RepoInput("proctor", "- [ ] a @owner:o @blocked_by:proctor#b\n")]
    assert check_legacy_fleet(inputs, MANIFEST) == []


def test_ided_source_is_left_to_the_canonical_pipeline() -> None:
    # an @id'd source is parse_fleet's job even if its legacy ref would dangle
    inputs = [
        RepoInput("proctor", "- [ ] x @owner:o @blocked_by:maestro#gone @id:x\n"),
        RepoInput("maestro", "- [ ] unrelated open\n"),
    ]
    assert check_legacy_fleet(inputs, MANIFEST) == []


def test_closed_source_is_skipped() -> None:
    inputs = [
        RepoInput("proctor", "- [x] x @owner:o @blocked_by:maestro#gone\n"),
        RepoInput("maestro", "- [ ] unrelated\n"),
    ]
    assert check_legacy_fleet(inputs, MANIFEST) == []


def test_todo_uri_ref_is_not_a_legacy_ref() -> None:
    # canonical todo:// on an un-@id'd source is ignored here (no legacy shape)
    inputs = [
        RepoInput("proctor", "- [ ] x @owner:o @blocked_by:todo://maestro/r\n"),
        RepoInput("maestro", "- [ ] r @owner:o @id:r\n"),
    ]
    assert check_legacy_fleet(inputs, MANIFEST) == []


def test_exclude_drops_named_relations() -> None:
    inputs = [
        RepoInput("proctor", "- [ ] x @owner:o @blocked_by:maestro#gone\n"),
        RepoInput("maestro", "- [ ] u\n"),
    ]
    assert len(check_legacy_fleet(inputs, MANIFEST)) == 1
    excluded = check_legacy_fleet(
        inputs, MANIFEST, exclude={("proctor", "maestro#gone")}
    )
    assert excluded == []


def test_stale_relation_migrates_legacy_to_canonical_on_id() -> None:
    # the same stale relation is a LEGACY warning while the source has no @id,
    # and a CANONICAL check_fleet diagnostic once it does — never both.
    maestro = "- [x] done shipped @owner:o @id:done\n"
    before = [
        RepoInput("maestro", maestro),
        RepoInput("proctor", "- [ ] x @owner:o @blocked_by:maestro#done\n"),
    ]
    after = [
        RepoInput("maestro", maestro),
        RepoInput(
            "proctor",
            "- [ ] x @owner:o @blocked_by:todo://maestro/done @id:x\n",
        ),
    ]
    lb = check_legacy_fleet(before, MANIFEST)
    assert [d.code for d in lb] == ["PF-BLOCKER-STALE"]
    assert check_fleet(parse_fleet(before, MANIFEST)) == []  # source not @id'd yet

    assert check_legacy_fleet(after, MANIFEST) == []  # migrated out of legacy
    canonical = check_fleet(parse_fleet(after, MANIFEST))
    assert [d["code"] for d in canonical] == ["PF-BLOCKER-STALE"]  # ...into canonical


def test_empty_at_full_id_coverage() -> None:
    inputs = [
        RepoInput("maestro", "- [ ] a @owner:o @id:a\n"),
        RepoInput("proctor", "- [ ] b @owner:o @blocked_by:maestro#a @id:b\n"),
    ]
    assert check_legacy_fleet(inputs, MANIFEST) == []


def test_legacy_and_canonical_do_not_double_count_a_relation() -> None:
    # one un-@id'd legacy relation + one @id'd canonical relation = exactly one each
    maestro = "- [ ] r open @owner:o @id:r\n- [ ] g open @owner:o\n"
    proctor = (
        "- [ ] canonical @owner:o @blocked_by:todo://maestro/r @id:c\n"
        "- [ ] legacy @owner:o @blocked_by:maestro#g\n"
    )
    inputs = [RepoInput("maestro", maestro), RepoInput("proctor", proctor)]
    snap = parse_fleet(inputs, MANIFEST)
    edges = {(e["source_node_id"], e["target_node_id"]) for e in snap["edges"]}
    assert edges == {("todo://proctor/c", "todo://maestro/r")}  # canonical, once
    legacy = check_legacy_fleet(
        inputs,
        MANIFEST,
        exclude={(r["provenance"]["repo"], r["raw_ref"]) for r in snap["references"]},
    )
    # 'g' is open -> the legacy relation is valid, no diagnostic; the point is it
    # is NOT reported as an edge, and the canonical one is not reported as legacy
    assert legacy == []
