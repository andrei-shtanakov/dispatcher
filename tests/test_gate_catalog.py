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


def test_the_vendored_catalog_loads_and_is_v1() -> None:
    assert load_catalog().version == SUPPORTED_CATALOG_VERSION == 1


def test_the_obligation_vocabulary_is_the_canonical_pair() -> None:
    assert obligation_vocabulary() == {"quality", "approval"}


def test_the_v1_composition_is_what_the_pin_ships() -> None:
    """19 active quality gates plus the one declared approval gate; a
    re-vendor that changes the composition must trip this deliberately
    (upstream bumps `version` on any composition change)."""
    catalog = load_catalog()
    assert len(catalog.gates) == 20
    by_status = {
        slug: entry for slug, entry in catalog.gates.items() if entry.status != "active"
    }
    assert set(by_status) == {"GC-APPROVAL-MISSING"}
    assert by_status["GC-APPROVAL-MISSING"].status == "declared"
    assert by_status["GC-APPROVAL-MISSING"].obligation == "approval"


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
    """Byte-copying a v2 catalog into the v1 directory must raise, not
    silently change what this consumer claims to understand."""
    import dispatcher.core.gate_catalog as mod

    doctored = tmp_path / "gate-catalog.yaml"
    original = mod._CATALOG_PATH.read_text(encoding="utf-8")
    doctored.write_text(original.replace("version: 1", "version: 2", 1))
    monkeypatch.setattr(mod, "_CATALOG_PATH", doctored)
    load_catalog.cache_clear()
    try:
        with pytest.raises(ValueError, match="version 2"):
            mod.load_catalog()
    finally:
        load_catalog.cache_clear()
