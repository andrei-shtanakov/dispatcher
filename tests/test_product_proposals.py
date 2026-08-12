"""core/product_proposals.py — read-only gate_waiting classification.

Spec: docs/superpowers/specs/2026-08-12-product-proposal-gate-waiting-design.md
(inbox #129). Fixtures come from the VENDORED contract copies and from
synthetic bundles built in tmp_path — never from ../impresario (CON-03).
"""

from __future__ import annotations

import hashlib
import json as _json
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


def test_root_level_bundle_diagnostic_path_has_one_spelling(tmp_path: Path) -> None:
    """proposal.yaml diagnostics use one path spelling everywhere: the
    root-level bundle case must say "proposal.yaml", not "./proposal.yaml"."""
    from dispatcher.core.product_proposals import _load_bundle

    mirror = tmp_path / "mirror"
    _mk(mirror, "proposal.yaml", "proposal_id: PP-101\n")  # schema-invalid
    b = _load_bundle(mirror, mirror)
    assert b.diagnostics[0].path == "proposal.yaml"


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


def test_uppercase_yaml_decision_is_read(tmp_path: Path) -> None:
    """Only .yaml is contract, recognized case-insensitively — the .YAML
    file is read, and a schema-invalid decision proves it (fail-closed)."""
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={"GD-001.YAML": "decision_id: GD-001\n"},
    )
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["decision-schema-invalid"]


def test_uppercase_yaml_valid_approve_extinguishes(tmp_path: Path) -> None:
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={"GD-001.YAML": decision_yaml(version=8)},
    )
    b = _bundle(tmp_path)
    assert b.state == "ok"
    assert b.waits == []


def test_yml_extension_is_out_of_contract(tmp_path: Path) -> None:
    """.yml is not .yaml — out of contract, not read, no diagnostic."""
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="approved"),
        decisions={},
    )
    bundle = tmp_path / "pilot" / "pp-101"
    (bundle / "decisions" / "gd.yml").write_bytes(b"\xff\xfe broken")
    b = _bundle(tmp_path)
    assert b.state == "ok"
    assert b.diagnostics == []


def test_diagnostics_raw_order_is_the_public_contract(tmp_path: Path) -> None:
    """Diagnostics are sorted by (path, code, message); pin the literal
    output order here instead of re-sorting inside the assertion — a
    sorted() comparison would pass even if the implementation stopped
    sorting."""
    bundle = make_bundle(
        tmp_path,
        decisions={
            "a.yaml": "placeholder\n",
            "b.yaml": "decision_id: GD-001\n",  # schema-invalid
            "c.yaml": "placeholder\n",
        },
    )
    (bundle / "decisions" / "a.yaml").write_bytes(b"\xff\xfe broken a")
    (bundle / "decisions" / "c.yaml").write_bytes(b"\xff\xfe broken c")
    b = _bundle(tmp_path)
    assert [(d.path, d.code) for d in b.diagnostics] == [
        ("pilot/pp-101/decisions/a.yaml", "decision-unreadable"),
        ("pilot/pp-101/decisions/b.yaml", "decision-schema-invalid"),
        ("pilot/pp-101/decisions/c.yaml", "decision-unreadable"),
    ]


def test_integrity_runs_only_over_fully_valid_decision_set(tmp_path: Path) -> None:
    """Any decision-grade read/schema diagnostic makes the whole decision
    history unprovable — supersession integrity is skipped, not partially
    applied to the valid subset."""
    make_bundle(
        tmp_path,
        decisions={
            "a.yaml": "decision_id: GD-001\n",  # schema-invalid
            "b.yaml": decision_yaml(did="GD-002", version=8, supersedes="GD-777"),
        },
    )
    b = _bundle(tmp_path)
    assert [d.code for d in b.diagnostics] == ["decision-schema-invalid"]


def test_gate_wait_requires_product_proposal_subject_kind() -> None:
    """Unit-test _gate_wait directly with a synthetic subject.kind that
    bypasses the schema: a mismatched kind must NOT extinguish the wait."""
    from dispatcher.core.product_proposals import _gate_wait

    proposal: dict[str, object] = {
        "proposal_id": "PP-101",
        "status": "ready_for_business",
        "version": 8,
        "updated_at": "2026-08-12T04:12:30Z",
    }
    record: dict[str, object] = {
        "decision": "approve",
        "gate_id": "qg5_business",
        "decision_id": "GD-001",
        "subject": {
            "kind": "ranked_backlog",
            "ref": "proposal://PP-101",
            "version": 8,
        },
    }
    wait = _gate_wait("p", proposal, [record])
    assert wait is not None


def test_cross_gate_supersession_disarms_the_approve(tmp_path: Path) -> None:
    """Owner ruling: ANY record's supersedes disarms the targeted approve,
    even from a different gate — the Gate A wait is NOT extinguished."""
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={
            "gd-001.yaml": decision_yaml(did="GD-001", version=8),
            "gd-002.yaml": decision_yaml(
                did="GD-002", gate="qg5_committee", version=8, supersedes="GD-001"
            ),
        },
    )
    b = _bundle(tmp_path)
    assert b.state == "ok"
    assert [w.gate_id for w in b.waits] == ["qg5_business"]


def test_gate_b_version_matched_approve_extinguishes(tmp_path: Path) -> None:
    """Symmetry with Gate A: a version-matched, non-superseded committee
    approve extinguishes the Gate B wait."""
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="business_approved", version=7),
        decisions={
            "gd-002.yaml": decision_yaml(did="GD-002", gate="qg5_committee", version=7)
        },
    )
    b = _bundle(tmp_path)
    assert b.state == "ok"
    assert b.waits == []


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


def test_walk_error_reaches_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same failing os.walk as the _discover unit test, but through the
    public entry point — the diagnostic must survive the full pipeline."""
    from dispatcher.core import product_proposals as pp
    from dispatcher.core.product_proposals import collect_product_proposals

    mirror = make_mirror(tmp_path)
    real_walk = os.walk

    def failing_walk(top, **kwargs):  # type: ignore[no-untyped-def]
        onerror = kwargs.get("onerror")
        assert onerror is not None, "walk must pass onerror (spec: fail-loud)"
        onerror(OSError(13, "Permission denied", str(Path(top) / "locked")))
        return real_walk(top, **kwargs)

    monkeypatch.setattr(pp.os, "walk", failing_walk)
    report = collect_product_proposals(mirror)
    assert [d.code for d in report.diagnostics] == ["walk-error"]
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


# Loop-state tests


def loop_state_json(
    pid: str = "PP-101",
    verdict: str | None = "ready_for_business",
    iteration: int = 2,
    reason: str = "done",
    at: str = "2026-08-12T04:01:21Z",
) -> str:
    stop = (
        None
        if verdict is None
        else {"verdict": verdict, "reason": reason, "iteration": iteration, "at": at}
    )
    return _json.dumps(
        {
            "loop_id": "LOOP-101",
            "idea_ref": "idea://IDEA-101",
            "idea_input_hash": "sha256:" + "f" * 64,
            "proposal_id": pid,
            "exchange_log_id": "XL-101",
            "max_iterations": 3,
            "stop": stop,
        }
    )


def _loop(
    tmp_path: Path, text: str | bytes, proposal: str | None = None
) -> ProposalBundle:
    bundle = make_bundle(
        tmp_path,
        proposal=proposal or proposal_yaml(status="ready_for_business", version=6),
    )
    target = bundle / "loop.state"
    if isinstance(text, bytes):
        target.write_bytes(text)
    else:
        target.write_text(text)
    return _bundle(tmp_path)


def test_loop_state_absent_is_normal(tmp_path: Path) -> None:
    make_bundle(tmp_path, proposal=proposal_yaml(status="approved"))
    b = _bundle(tmp_path)
    assert b.state == "ok"
    assert b.loop_status == "absent" and b.loop_waits == []


def test_loop_state_running_has_no_wait(tmp_path: Path) -> None:
    b = _loop(tmp_path, loop_state_json(verdict=None))
    assert b.state == "ok"
    assert b.loop_status == "running" and b.loop_waits == []


def test_loop_state_terminals_are_not_human_waits(tmp_path: Path) -> None:
    for verdict in ("ready_for_business", "failed"):
        mirror = Path(str(tmp_path)) / verdict
        make_bundle(mirror, proposal=proposal_yaml(status="approved"))
        (mirror / "pilot" / "pp-101" / "loop.state").write_text(
            loop_state_json(verdict=verdict)
        )
        b = _bundle(mirror)
        assert b.state == "ok"
        assert b.loop_status == verdict and b.loop_waits == []


def test_loop_state_needs_human_yields_one_wait(tmp_path: Path) -> None:
    b = _loop(
        tmp_path,
        loop_state_json(verdict="needs_human", reason="решить exempt-семантику"),
    )
    assert b.state == "ok" and b.loop_status == "needs_human"
    assert [
        (w.loop_id, w.iteration, w.proposal_id, w.reason, w.stopped_at)
        for w in b.loop_waits
    ] == [
        (
            "LOOP-101",
            2,
            "PP-101",
            "решить exempt-семантику",
            "2026-08-12T04:01:21Z",
        )
    ]
    assert b.loop_waits[0].bundle_path == "pilot/pp-101"


def test_loop_state_not_json_is_unknown(tmp_path: Path) -> None:
    b = _loop(tmp_path, "not json {{{")
    assert b.state == "unknown" and b.loop_status == "unknown"
    assert [d.code for d in b.diagnostics] == ["loop-state-unreadable"]
    assert b.waits == [] and b.loop_waits == []


def test_loop_state_not_a_json_object_is_unreadable(tmp_path: Path) -> None:
    """JSON array or other non-object JSON is rejected."""
    b = _loop(tmp_path, "[1, 2, 3]")
    assert b.state == "unknown" and b.loop_status == "unknown"
    assert [d.code for d in b.diagnostics] == ["loop-state-unreadable"]
    assert b.waits == [] and b.loop_waits == []


def test_loop_state_duplicate_json_keys_are_unreadable(tmp_path: Path) -> None:
    """json.loads keeps the last duplicate silently — fail-closed requires
    rejection (part of loop-state-unreadable, no separate code)."""
    text = loop_state_json()[:-1] + ', "proposal_id": "PP-999"}'
    b = _loop(tmp_path, text)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["loop-state-unreadable"]


def test_loop_state_not_utf8_is_unreadable(tmp_path: Path) -> None:
    b = _loop(tmp_path, b"\xff\xfe not utf8")
    assert [d.code for d in b.diagnostics] == ["loop-state-unreadable"]


def test_loop_state_oserror_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_bundle(tmp_path, proposal=proposal_yaml(status="approved"))
    (tmp_path / "pilot" / "pp-101" / "loop.state").write_text(loop_state_json())
    real = Path.read_bytes

    def failing(self: Path) -> bytes:
        if self.name == "loop.state":
            raise OSError(5, "Input/output error")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", failing)
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["loop-state-unreadable"]


def test_loop_state_schema_invalid_is_unknown(tmp_path: Path) -> None:
    b = _loop(tmp_path, '{"loop_id": "LOOP-101"}')
    assert b.state == "unknown" and b.loop_status == "unknown"
    assert [d.code for d in b.diagnostics] == ["loop-state-schema-invalid"]


def test_loop_state_proposal_mismatch_is_unknown(tmp_path: Path) -> None:
    b = _loop(tmp_path, loop_state_json(pid="PP-999"))
    assert b.state == "unknown" and b.loop_status == "unknown"
    diag = b.diagnostics[0]
    assert diag.code == "loop-state-proposal-mismatch"
    assert "PP-999" in diag.message and "PP-101" in diag.message
    assert diag.path == "pilot/pp-101/loop.state"
    assert b.waits == [] and b.loop_waits == []


def test_loop_state_symlink_escape_is_unknown(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(loop_state_json())
    mirror = tmp_path / "mirror"
    bundle = make_bundle(mirror, proposal=proposal_yaml(status="approved"))
    (bundle / "loop.state").symlink_to(outside)
    b = _bundle(mirror)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["loop-state-path-escape"]


def test_loop_state_in_mirror_symlink_stays_readable(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    bundle = make_bundle(mirror, proposal=proposal_yaml(status="approved"))
    real = bundle / "real-loop.json"
    real.write_text(loop_state_json())
    (bundle / "loop.state").symlink_to(real)
    b = _bundle(mirror)
    assert b.state == "ok" and b.loop_status == "ready_for_business"


def test_untrusted_proposal_collects_loop_read_errors_only(
    tmp_path: Path,
) -> None:
    """No trusted subject: read/parse errors collected; schema and the
    membership check skipped; the non-ok rule owns loop_status."""
    bundle = make_bundle(tmp_path, proposal="status: [broken\n")
    (bundle / "loop.state").write_bytes(b"\xff\xfe")
    b = _bundle(tmp_path)
    assert b.state == "unreadable"
    assert sorted(d.code for d in b.diagnostics) == [
        "loop-state-unreadable",
        "proposal-unreadable",
    ]
    assert b.loop_status == "unknown"


def test_untrusted_proposal_skips_loop_semantic_classification(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path, proposal="status: [broken\n")
    (bundle / "loop.state").write_text(loop_state_json(pid="PP-999"))
    b = _bundle(tmp_path)
    assert b.state == "unreadable"
    assert [d.code for d in b.diagnostics] == ["proposal-unreadable"]
    assert b.loop_status == "unknown"  # no mismatch diagnostic, no trust


LS_SCHEMA_FIXTURES = (
    Path(__file__).parent.parent
    / "contracts"
    / "impresario-loop-state"
    / "v1"
    / "fixtures"
)


def test_vendored_loop_state_fixtures_split_on_the_schema() -> None:
    from dispatcher.core.product_proposals import _loop_state_validator

    for name in ("failed.json", "needs-human.json", "ready.json", "running.json"):
        data = _json.loads((LS_SCHEMA_FIXTURES / "valid" / name).read_text())
        assert _loop_state_validator().is_valid(data), name
    for name in (
        "bad-hash.json",
        "empty-reason.json",
        "extra-field.json",
        "missing-at.json",
        "unknown-verdict.json",
    ):
        data = _json.loads((LS_SCHEMA_FIXTURES / "invalid" / name).read_text())
        assert not _loop_state_validator().is_valid(data), name
