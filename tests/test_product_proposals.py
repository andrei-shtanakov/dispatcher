"""core/product_proposals.py — read-only gate_waiting classification.

Spec: docs/superpowers/specs/2026-08-12-product-proposal-gate-waiting-design.md
(inbox #129). Fixtures come from the VENDORED contract copies and from
synthetic bundles built in tmp_path — never from ../impresario (CON-03).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest
import yaml

from dispatcher.core.product_proposals import (
    ANCHOR_FILES,
    ProposalBundle,
    _strict_load,
)

PP_SCHEMA_FIXTURES = (
    Path(__file__).parent.parent
    / "contracts"
    / "impresario-product-proposal"
    / "v1"
    / "fixtures"
)
GD_SCHEMA_FIXTURES = (
    Path(__file__).parent.parent
    / "contracts"
    / "impresario-gate-decision"
    / "v1"
    / "fixtures"
)


def test_strict_load_rejects_duplicate_mapping_keys() -> None:
    """yaml.safe_load keeps the last duplicate silently — fail-closed for
    decision_id / subject.version / gate_id / status requires rejection."""
    with pytest.raises(yaml.YAMLError, match="duplicate"):
        _strict_load("status: approved\nstatus: draft\n")


def test_strict_load_rejects_nested_duplicate_keys() -> None:
    with pytest.raises(yaml.YAMLError, match="duplicate"):
        _strict_load("subject:\n  version: 1\n  version: 2\n")


def test_strict_load_accepts_plain_mappings() -> None:
    assert _strict_load("a: 1\nb:\n  c: 2\n") == {"a": 1, "b": {"c": 2}}


def test_anchor_files_are_the_two_impresario_markers() -> None:
    assert ANCHOR_FILES == (
        "contracts/product-proposal/v1/schema.json",
        "docs/semantics.md",
    )


def test_vendored_valid_fixtures_pass_their_schemas() -> None:
    from dispatcher.core.product_proposals import (
        _decision_validator,
        _proposal_validator,
    )

    pp = _strict_load((PP_SCHEMA_FIXTURES / "valid" / "pp-001.yaml").read_text())
    assert _proposal_validator().is_valid(pp)
    for name in ("gd-approve.yaml", "gd-recycle.yaml", "gd-select.yaml"):
        gd = _strict_load((GD_SCHEMA_FIXTURES / "valid" / name).read_text())
        assert _decision_validator().is_valid(gd), name


def test_vendored_invalid_fixtures_fail_their_schemas() -> None:
    from dispatcher.core.product_proposals import (
        _decision_validator,
        _proposal_validator,
    )

    for name in (
        "status-ready-for-committee.yaml",
        "status-recycle.yaml",
        "version-zero.yaml",
    ):
        pp = _strict_load((PP_SCHEMA_FIXTURES / "invalid" / name).read_text())
        assert not _proposal_validator().is_valid(pp), name
    for name in (
        "agent-authority.yaml",
        "qg4-approve.yaml",
        "recycle-without-return-to.yaml",
    ):
        gd = _strict_load((GD_SCHEMA_FIXTURES / "invalid" / name).read_text())
        assert not _decision_validator().is_valid(gd), name


def _mk(root: Path, rel: str, text: str = "x: 1\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_discover_finds_bundles_sorted_and_excludes_segments(
    tmp_path: Path,
) -> None:
    from dispatcher.core.product_proposals import _discover

    _mk(tmp_path, "pilot/b/pp-2/proposal.yaml")
    _mk(tmp_path, "pilot/a/pp-1/proposal.yaml")
    _mk(tmp_path, "contracts/examples/pp-0/proposal.yaml")  # excluded: contracts
    _mk(tmp_path, "_drafts/pp-3/proposal.yaml")  # excluded: _ prefix
    _mk(tmp_path, "pilot/.hidden/pp-4/proposal.yaml")  # excluded: . prefix
    _mk(tmp_path, "deep/nested/contracts/pp-5/proposal.yaml")  # excluded: any segment
    bundles, diags = _discover(tmp_path)
    assert diags == []
    rels = [b.relative_to(tmp_path).as_posix() for b in bundles]
    assert rels == ["pilot/a/pp-1", "pilot/b/pp-2"]


def test_discover_does_not_follow_directory_symlinks(tmp_path: Path) -> None:
    from dispatcher.core.product_proposals import _discover

    outside = tmp_path / "outside"
    _mk(outside, "pp-x/proposal.yaml")
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "linked").symlink_to(outside, target_is_directory=True)
    bundles, diags = _discover(mirror)
    assert bundles == [] and diags == []


def test_walk_error_is_a_report_diagnostic_not_zero_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.walk swallows enumeration errors unless onerror is passed — the
    diagnostic proves the callback is wired."""
    from dispatcher.core import product_proposals as pp

    real_walk = os.walk

    def failing_walk(top, **kwargs):  # type: ignore[no-untyped-def]
        onerror = kwargs.get("onerror")
        assert onerror is not None, "walk must pass onerror (spec: fail-loud)"
        onerror(OSError(13, "Permission denied", str(Path(top) / "locked")))
        return real_walk(top, **kwargs)

    monkeypatch.setattr(pp.os, "walk", failing_walk)
    bundles, diags = pp._discover(tmp_path)
    assert [d.code for d in diags] == ["walk-error"]
    assert diags[0].path == "locked"


def proposal_yaml(
    pid: str = "PP-101",
    version: int = 8,
    status: str = "approved",
    updated: str = "2026-08-12T04:12:30Z",
) -> str:
    return (
        f"proposal_id: {pid}\n"
        "idea_ref: idea://IDEA-101\n"
        f"version: {version}\n"
        f"status: {status}\n"
        "iteration: 2\n"
        "refs:\n"
        "  exchange_log: exchange-log://XL-101\n"
        "created_at: '2026-08-12T02:08:53Z'\n"
        f"updated_at: '{updated}'\n"
    )


def decision_yaml(
    did: str = "GD-001",
    gate: str = "qg5_business",
    version: int = 8,
    decision: str = "approve",
    ref: str = "proposal://PP-101",
    supersedes: str | None = None,
) -> str:
    text = (
        f"decision_id: {did}\n"
        f"gate_id: {gate}\n"
        "subject:\n"
        "  kind: product_proposal\n"
        f"  ref: {ref}\n"
        f"  version: {version}\n"
        f"decision: {decision}\n"
        "decided_by:\n"
        "  kind: human\n"
        "  id: andrei\n"
        "  role: business_owner\n"
        "decided_at: '2026-08-12T04:09:21Z'\n"
        "reason: test\n"
    )
    if decision == "recycle":
        text += "return_to: in_iteration\nrequired_changes:\n- fix\n"
    if supersedes is not None:
        text += f"supersedes: gate-decision://{supersedes}\n"
    return text


def make_bundle(
    root: Path,
    rel: str = "pilot/pp-101",
    proposal: str | None = None,
    decisions: dict[str, str] | None = None,
) -> Path:
    bundle = root / rel
    (bundle / "decisions").mkdir(parents=True, exist_ok=True)
    _mk(root, f"{rel}/proposal.yaml", proposal or proposal_yaml())
    for name, text in (decisions or {}).items():
        _mk(root, f"{rel}/decisions/{name}", text)
    return bundle


def _bundle(root: Path, rel: str = "pilot/pp-101") -> ProposalBundle:
    from dispatcher.core.product_proposals import _load_bundle

    return _load_bundle(root, root / rel)


def test_ready_for_business_with_no_decisions_waits_for_gate_a(
    tmp_path: Path,
) -> None:
    make_bundle(
        tmp_path, proposal=proposal_yaml(status="ready_for_business", version=6)
    )
    b = _bundle(tmp_path)
    assert b.state == "ok"
    assert [
        (w.gate_id, w.gate_label, w.authority, w.artifact_ref, w.version)
        for w in b.waits
    ] == [("qg5_business", "Gate A", "business_owner", "proposal://PP-101", 6)]
    assert b.waits[0].proposal_updated_at == "2026-08-12T04:12:30Z"
    assert b.waits[0].bundle_path == "pilot/pp-101"


def test_business_approved_without_committee_approve_waits_for_gate_b(
    tmp_path: Path,
) -> None:
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="business_approved", version=7),
        decisions={"gd-001.yaml": decision_yaml(version=6)},
    )
    b = _bundle(tmp_path)
    assert b.state == "ok"
    assert [(w.gate_id, w.authority) for w in b.waits] == [
        ("qg5_committee", "committee_chair")
    ]


def test_terminal_and_iteration_statuses_have_no_wait(tmp_path: Path) -> None:
    for status in ("draft", "in_iteration", "approved", "on_hold", "killed"):
        make_bundle(tmp_path, rel=f"p/{status}", proposal=proposal_yaml(status=status))
        b = _bundle(tmp_path, rel=f"p/{status}")
        assert b.state == "ok" and b.waits == []


def test_regression_recycle_old_approve_does_not_extinguish_new_wait(
    tmp_path: Path,
) -> None:
    """Pinned semantics: after recycle the un-superseded old approve (v6) is
    history, not permission — the v8 Gate A wait IS shown."""
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={"gd-001.yaml": decision_yaml(version=6)},
    )
    b = _bundle(tmp_path)
    assert b.state == "ok"
    assert [(w.gate_id, w.version) for w in b.waits] == [("qg5_business", 8)]


def test_regression_version_matched_approve_extinguishes_before_status_update(
    tmp_path: Path,
) -> None:
    """Pinned semantics (torn write): a version-matched approve already
    recorded extinguishes the wait even though status has not caught up."""
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={"gd-001.yaml": decision_yaml(version=8)},
    )
    b = _bundle(tmp_path)
    assert b.state == "ok" and b.waits == []


def test_superseded_version_matched_approve_does_not_extinguish(
    tmp_path: Path,
) -> None:
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={
            "gd-001.yaml": decision_yaml(did="GD-001", version=8),
            "gd-002.yaml": decision_yaml(
                did="GD-002", version=8, decision="recycle", supersedes="GD-001"
            ),
        },
    )
    b = _bundle(tmp_path)
    assert b.state == "ok"
    assert [w.gate_id for w in b.waits] == ["qg5_business"]


def test_other_gate_ref_or_kind_is_history_not_permission(tmp_path: Path) -> None:
    """A decision for another subject.ref or gate_id never touches the wait."""
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={
            "gd-001.yaml": decision_yaml(version=8, ref="proposal://PP-999"),
            "gd-002.yaml": decision_yaml(did="GD-002", gate="qg5_committee", version=8),
        },
    )
    b = _bundle(tmp_path)
    assert [w.gate_id for w in b.waits] == ["qg5_business"]


def test_proposal_schema_invalid_is_unreadable(tmp_path: Path) -> None:
    make_bundle(tmp_path, proposal="proposal_id: PP-101\n")  # misses required
    b = _bundle(tmp_path)
    assert b.state == "unreadable"
    assert [d.code for d in b.diagnostics] == ["proposal-schema-invalid"]
    assert b.waits == [] and b.proposal_id is None


def test_proposal_not_utf8_is_unreadable(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    (bundle / "proposal.yaml").write_bytes(b"\xff\xfe broken")
    b = _bundle(tmp_path)
    assert b.state == "unreadable"
    assert [d.code for d in b.diagnostics] == ["proposal-unreadable"]


def test_proposal_oserror_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The I/O-error branch, patched instead of chmod (unstable in CI)."""
    make_bundle(tmp_path)
    real = Path.read_bytes

    def failing(self: Path) -> bytes:
        if self.name == "proposal.yaml":
            raise OSError(5, "Input/output error")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", failing)
    b = _bundle(tmp_path)
    assert b.state == "unreadable"
    assert [d.code for d in b.diagnostics] == ["proposal-unreadable"]


def test_decision_oserror_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision I/O-error branch — patched, not chmod (spec «Testing»:
    unreadability is invalid UTF-8; the OSError branch gets its own test)."""
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={"gd-001.yaml": decision_yaml(version=8)},
    )
    real = Path.read_bytes

    def failing(self: Path) -> bytes:
        if self.name == "gd-001.yaml":
            raise OSError(5, "Input/output error")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", failing)
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["decision-unreadable"]
    assert b.waits == []


def test_invalid_decision_makes_bundle_unknown_not_clean(tmp_path: Path) -> None:
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="approved"),
        decisions={"gd-001.yaml": "decision_id: GD-001\n"},  # schema-invalid
    )
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["decision-schema-invalid"]
    assert b.waits == []
    assert b.proposal_id == "PP-101"  # proposal fields stay filled


def test_all_decision_errors_are_collected_not_just_the_first(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(
        tmp_path,
        decisions={
            "a.yaml": "decision_id: GD-001\n",  # schema-invalid
            "b.yaml": "x: 1\nx: 2\n",  # duplicate keys -> unreadable
        },
    )
    (bundle / "decisions" / "c.yaml").write_bytes(b"\xff\xfe")  # not UTF-8
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert sorted(d.code for d in b.diagnostics) == [
        "decision-schema-invalid",
        "decision-unreadable",
        "decision-unreadable",
    ]


def test_unparseable_proposal_still_collects_decision_read_errors(
    tmp_path: Path,
) -> None:
    """No trusted subject -> no semantic classification of decisions, but
    their READ errors are still collected; the schema-invalid decision is
    deliberately NOT reported (that is semantic classification)."""
    bundle = make_bundle(
        tmp_path,
        proposal="status: [broken\n",
        decisions={"a.yaml": "decision_id: GD-001\n"},
    )
    (bundle / "decisions" / "b.yaml").write_bytes(b"\xff\xfe")
    b = _bundle(tmp_path)
    assert b.state == "unreadable"
    assert sorted(d.code for d in b.diagnostics) == [
        "decision-unreadable",
        "proposal-unreadable",
    ]


def test_duplicate_decision_id_is_unknown(tmp_path: Path) -> None:
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={
            "a.yaml": decision_yaml(did="GD-001", version=8),
            "b.yaml": decision_yaml(did="GD-001", version=8, gate="qg5_committee"),
        },
    )
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["decision-id-duplicate"]
    assert b.waits == []


def test_dangling_supersedes_is_unknown(tmp_path: Path) -> None:
    make_bundle(
        tmp_path,
        decisions={
            "a.yaml": decision_yaml(did="GD-002", version=8, supersedes="GD-777")
        },
    )
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["supersedes-dangling"]


def test_supersedes_cycle_is_unknown(tmp_path: Path) -> None:
    make_bundle(
        tmp_path,
        decisions={
            "a.yaml": decision_yaml(did="GD-001", version=8, supersedes="GD-002"),
            "b.yaml": decision_yaml(did="GD-002", version=8, supersedes="GD-001"),
        },
    )
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert "supersedes-cycle" in {d.code for d in b.diagnostics}


def test_self_supersede_is_unknown(tmp_path: Path) -> None:
    make_bundle(
        tmp_path,
        decisions={
            "a.yaml": decision_yaml(did="GD-001", version=8, supersedes="GD-001")
        },
    )
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert "supersedes-cycle" in {d.code for d in b.diagnostics}


def test_decision_symlink_escape_is_unknown(tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text(decision_yaml(version=8))
    mirror = tmp_path / "mirror"
    bundle = make_bundle(
        mirror, proposal=proposal_yaml(status="ready_for_business", version=8)
    )
    (bundle / "decisions" / "gd-x.yaml").symlink_to(outside)
    b = _bundle(mirror)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["decision-path-escape"]
    assert b.waits == []


def test_proposal_symlink_escape_is_unreadable(tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text(proposal_yaml())
    mirror = tmp_path / "mirror"
    (mirror / "pilot" / "pp-101").mkdir(parents=True)
    (mirror / "pilot" / "pp-101" / "proposal.yaml").symlink_to(outside)
    b = _bundle(mirror)
    assert b.state == "unreadable"
    assert [d.code for d in b.diagnostics] == ["proposal-path-escape"]


def test_in_mirror_file_symlink_stays_readable(tmp_path: Path) -> None:
    """The rule is escape, not symlink-ness: a link resolving inside the
    mirror is fine."""
    mirror = tmp_path / "mirror"
    bundle = make_bundle(
        mirror,
        proposal=proposal_yaml(status="ready_for_business", version=8),
    )
    real = bundle / "decisions" / "real-gd.txt"
    real.write_text(decision_yaml(version=8))
    (bundle / "decisions" / "gd-001.yaml").symlink_to(real)
    b = _bundle(mirror)
    assert b.state == "ok" and b.waits == []


def test_missing_decisions_dir_is_a_valid_bundle(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    _mk(
        mirror,
        "pilot/pp-101/proposal.yaml",
        proposal_yaml(status="ready_for_business", version=6),
    )
    b = _bundle(mirror)
    assert b.state == "ok"
    assert [w.gate_id for w in b.waits] == ["qg5_business"]


def make_mirror(tmp_path: Path) -> Path:
    """A minimal detectable impresario mirror (both anchors present)."""
    mirror = tmp_path / "impresario"
    for rel in ANCHOR_FILES:
        _mk(mirror, rel, "{}\n" if rel.endswith(".json") else "# semantics\n")
    return mirror


def test_missing_anchor_is_anchors_missing_not_zero_bundles(
    tmp_path: Path,
) -> None:
    mirror = make_mirror(tmp_path)
    (mirror / "docs" / "semantics.md").unlink()
    from dispatcher.core.product_proposals import collect_product_proposals

    report = collect_product_proposals(mirror)
    assert report.bundles == []
    assert [d.code for d in report.diagnostics] == ["mirror-anchors-missing"]
    assert report.diagnostics[0].path == "docs/semantics.md"
    assert report.attention is True


def test_zero_bundles_on_a_healthy_mirror_is_explicit_and_calm(
    tmp_path: Path,
) -> None:
    from dispatcher.core.product_proposals import collect_product_proposals

    report = collect_product_proposals(make_mirror(tmp_path))
    assert report.bundles == [] and report.waits == []
    assert report.diagnostics == [] and report.attention is False


def test_proposal_id_conflict_suppresses_all_participants(tmp_path: Path) -> None:
    from dispatcher.core.product_proposals import collect_product_proposals

    mirror = make_mirror(tmp_path)
    make_bundle(
        mirror,
        rel="pilot/a",
        proposal=proposal_yaml(status="ready_for_business", version=6),
    )
    make_bundle(
        mirror,
        rel="pilot/b",
        proposal=proposal_yaml(status="approved"),
        decisions={"bad.yaml": "decision_id: GD-1\n"},  # earlier diagnostic
    )
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.bundles] == ["conflict", "conflict"]
    assert report.waits == []  # the Gate A wait of pilot/a is suppressed
    for bundle in report.bundles:
        conflict = [d for d in bundle.diagnostics if d.code == "proposal-id-conflict"]
        assert len(conflict) == 1
        assert "pilot/a" in conflict[0].message and "pilot/b" in conflict[0].message
    # earlier diagnostics are preserved, not replaced (spec section 1 refinements)
    b_codes = {d.code for d in report.bundles[1].diagnostics}
    assert "decision-schema-invalid" in b_codes
    assert report.attention is True


def test_waits_aggregate_and_sort_deterministically(tmp_path: Path) -> None:
    from dispatcher.core.product_proposals import collect_product_proposals

    mirror = make_mirror(tmp_path)
    make_bundle(
        mirror,
        rel="pilot/z",
        proposal=proposal_yaml(pid="PP-100", status="business_approved", version=3),
    )
    make_bundle(
        mirror,
        rel="pilot/a",
        proposal=proposal_yaml(pid="PP-200", status="ready_for_business", version=1),
    )
    report = collect_product_proposals(mirror)
    assert [b.path for b in report.bundles] == ["pilot/a", "pilot/z"]
    assert [(w.proposal_id, w.gate_id) for w in report.waits] == [
        ("PP-100", "qg5_committee"),
        ("PP-200", "qg5_business"),
    ]
    assert report.attention is False  # plain waits are business, not defects


def test_repeated_scans_are_byte_identical(tmp_path: Path) -> None:
    from dispatcher.core.product_proposals import collect_product_proposals

    mirror = make_mirror(tmp_path)
    make_bundle(mirror, proposal=proposal_yaml(status="ready_for_business", version=6))
    first = collect_product_proposals(mirror)
    second = collect_product_proposals(mirror)
    assert first.model_dump_json() == second.model_dump_json()


def _tree_state(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): (
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
        )
        for p in root.rglob("*")
    }


def test_collect_is_read_only_paths_and_bytes(tmp_path: Path) -> None:
    """Path SET equality too: creating files/dirs is a violation, not just
    modifying them (spec «Fail-closed invariants» #4)."""
    from dispatcher.core.product_proposals import collect_product_proposals

    mirror = make_mirror(tmp_path)
    make_bundle(
        mirror,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={"gd-001.yaml": decision_yaml(version=6)},
    )
    before = _tree_state(mirror)
    collect_product_proposals(mirror)
    assert _tree_state(mirror) == before


def test_report_mirror_path_is_the_scanned_root(tmp_path: Path) -> None:
    from dispatcher.core.product_proposals import collect_product_proposals

    mirror = make_mirror(tmp_path)
    assert collect_product_proposals(mirror).mirror_path == str(mirror)
