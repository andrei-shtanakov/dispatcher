"""core/product_proposals.py — read-only gate_waiting classification.

Spec: docs/superpowers/specs/2026-08-12-product-proposal-gate-waiting-design.md
(inbox #129). Fixtures come from the VENDORED contract copies and from
synthetic bundles built in tmp_path — never from ../impresario (CON-03).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from dispatcher.core.product_proposals import (
    ANCHOR_FILES,
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
