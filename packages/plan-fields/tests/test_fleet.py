"""PF-2B0 read-only fleet tooling: suggest / classify / zero-drift."""

from __future__ import annotations

from plan_fields.fleet import (
    ManifestIndex,
    classify_legacy,
    drift,
    snapshot,
    suggest_ids,
)
from plan_fields.scrape import scrape_items


def _items(text: str):
    return scrape_items(text)


def _index(*names: str) -> ManifestIndex:
    """A manifest that declares these repos and no locator aliases."""
    return ManifestIndex(frozenset(names), {})


def test_classify_legacy_sorts_every_reference() -> None:
    fleet = {
        "arbiter": _items("- [ ] gate work @id:crossover-gate\n"),
        "maestro": _items(
            "- [ ] a @blocked_by:arbiter#crossover-gate\n"  # clean-open
            "- [ ] b @blocked_by:arbiter#gone\n"  # missing
            "- [ ] c @blocked_by:atp-platform#x\n"  # absent (in manifest, not scanned)
            "- [ ] d @blocked_by:operator-host#y\n"  # not-in-manifest
        ),
    }
    manifest = ManifestIndex(frozenset({"arbiter", "maestro", "atp-platform"}), {})
    got = {(r.slug, r.resolution) for r in classify_legacy(fleet, manifest)}
    assert ("crossover-gate", "clean-open") in got
    assert ("gone", "missing") in got
    assert ("x", "absent") in got
    assert ("y", "not-in-manifest") in got


def test_classify_flags_ambiguous_and_closed() -> None:
    fleet = {
        "t": _items("- [ ] one seam thing\n- [ ] two seam thing\n"),
        "s": _items("- [ ] w @blocked_by:t#seam\n"),
    }
    refs = {r.slug: r.resolution for r in classify_legacy(fleet, _index("t", "s"))}
    assert refs["seam"] == "ambiguous"


def test_resolution_ignores_slug_inside_a_tag_and_is_case_insensitive() -> None:
    fleet = {
        # the slug appears ONLY in another item's @blocked_by tag, never in prose:
        # that citer must not be mistaken for the target.
        "t": _items(
            "- [ ] Ship the R-99 feature now\n"  # real target (prose, mixed case)
            "- [ ] unrelated @blocked_by:t#R-99\n"  # merely cites it
        ),
        "s": _items("- [ ] w @blocked_by:t#r-99\n"),  # lowercase ref
    }
    refs = [r for r in classify_legacy(fleet, _index("t", "s")) if r.source_repo == "s"]
    assert len(refs) == 1
    assert refs[0].resolution == "clean-open"  # case-insensitive, prose-scoped
    assert refs[0].target_line == 1  # the prose item, not the citer


def test_suggest_reuses_clean_slug_only_for_its_line() -> None:
    items = _items("- [ ] target seam item\n- [ ] another seam item\n")
    # only line 1 is the clean target; line 2 must NOT inherit the slug
    sug = suggest_ids("t", items, {1: "seam-audit"})
    assert (sug[0].suggested_id, sug[0].source) == ("seam-audit", "reused-slug")
    assert sug[1].source == "derived" and sug[1].suggested_id != "seam-audit"


def test_suggest_derives_and_dedupes() -> None:
    items = _items("- [ ] ship the thing\n- [ ] ship the thing again more\n")
    sug = suggest_ids("t", items, {})
    assert sug[0].suggested_id == "ship-the-thing"
    assert sug[1].suggested_id != sug[0].suggested_id  # de-collided


def test_suggest_needs_owner_for_non_ascii() -> None:
    sug = suggest_ids("t", _items("- [ ] Мигрировать роли\n"), {})
    assert sug[0].source == "needs-owner" and sug[0].suggested_id == ""


def test_suggest_skips_closed_and_already_ided() -> None:
    items = _items("- [x] done thing\n- [ ] has one @id:already\n")
    assert suggest_ids("t", items, {}) == []


def test_snapshot_and_drift_zero_when_only_id_added() -> None:
    before = {"r": _items("- [ ] do the work @owner:a\n")}
    after = {"r": _items("- [ ] do the work @id:do-the-work @owner:a\n")}
    # @id strips out of display_text, so the baseline is identical
    assert drift(snapshot(before), snapshot(after)).clean


def test_drift_detects_a_changed_text() -> None:
    before = {"r": _items("- [ ] do the work\n")}
    after = {"r": _items("- [ ] do the WORK reworded\n")}
    d = drift(snapshot(before), snapshot(after))
    assert not d.clean and d.changed_repos == ["r"]


def test_drift_detects_a_status_flip() -> None:
    before = {"r": _items("- [ ] pending @id:p\n")}
    after = {"r": _items("- [x] pending @id:p\n")}
    assert not drift(snapshot(before), snapshot(after)).clean
