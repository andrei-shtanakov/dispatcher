"""The vendored gate-catalog loader (inbox #125).

Runs against the real vendored copy on purpose: the loader's one job is to
read that copy, and these assertions double as the consumer's statement of
what a re-vendor may change knowingly (update them deliberately, with the
new pin, the way `test_gate_catalog_vendor.py`'s surface set is updated).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dispatcher.core.gate_catalog import (
    SUPPORTED_CATALOG_VERSION,
    load_catalog,
    obligation_vocabulary,
)


def test_the_vendored_catalog_loads_and_is_v2() -> None:
    assert load_catalog().version == SUPPORTED_CATALOG_VERSION == 2


def test_the_obligation_vocabulary_is_the_canonical_pair() -> None:
    assert obligation_vocabulary() == {"quality", "approval"}


def test_the_v2_composition_is_what_the_pin_ships() -> None:
    """v2: all 20 gates active — GC-APPROVAL-MISSING flipped declared→active
    (steward AP-5, approval-policy-enforcement). A re-vendor that changes the
    composition must trip this deliberately (upstream bumps `version` on any
    composition change) — this text was updated by exactly such a re-vendor."""
    catalog = load_catalog()
    assert len(catalog.gates) == 20
    non_active = {
        slug: entry for slug, entry in catalog.gates.items() if entry.status != "active"
    }
    assert non_active == {}
    approval = catalog.gates["GC-APPROVAL-MISSING"]
    assert approval.status == "active"
    assert approval.obligation == "approval"


def test_every_gate_speaks_the_catalog_vocabularies() -> None:
    """Internal consistency of the vendored copy: a gate whose obligation or
    stage is outside the declared vocabularies would make the consumer's
    validation claim circular."""
    catalog = load_catalog()
    stages = set(catalog.stage_vocabulary)
    for slug, entry in catalog.gates.items():
        assert entry.obligation in obligation_vocabulary(), slug
        assert set(entry.stages) <= stages, slug


def test_a_future_catalog_version_refuses_to_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Byte-copying a future-version catalog into this directory must raise, not
    silently change what this consumer claims to understand."""
    import dispatcher.core.gate_catalog as mod

    doctored = tmp_path / "gate-catalog.yaml"
    original = mod._CATALOG_PATH.read_text(encoding="utf-8")
    doctored.write_text(original.replace("version: 2", "version: 3", 1))
    monkeypatch.setattr(mod, "_CATALOG_PATH", doctored)
    load_catalog.cache_clear()
    try:
        with pytest.raises(ValueError, match="version 3"):
            mod.load_catalog()
    finally:
        load_catalog.cache_clear()
