"""core/product_proposals.py — read-only gate_waiting classification.

Spec: docs/superpowers/specs/2026-08-12-product-proposal-gate-waiting-design.md
(inbox #129). Fixtures come from the VENDORED contract copies and from
synthetic bundles built in tmp_path — never from ../impresario (CON-03).
"""

from __future__ import annotations

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
