"""Discovery-only impresario collector (spec «Architecture»).

Light by design: detection needs BOTH anchors; collect() carries no bundles
and no waits — classification never enters the snapshot cache.
"""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.collectors import COLLECTORS
from dispatcher.core.collectors.base import CollectContext
from dispatcher.core.collectors.impresario import ImpresarioCollector
from dispatcher.core.product_proposals import ANCHOR_FILES


def _mirror(tmp_path: Path) -> Path:
    mirror = tmp_path / "impresario"
    for rel in ANCHOR_FILES:
        p = mirror / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    return mirror


def test_detect_requires_both_anchors(tmp_path: Path) -> None:
    collector = ImpresarioCollector()
    mirror = _mirror(tmp_path)
    assert collector.detect(mirror) is True
    (mirror / "docs" / "semantics.md").unlink()
    assert collector.detect(mirror) is False


def test_one_incidental_anchor_is_not_impresario(tmp_path: Path) -> None:
    only_docs = tmp_path / "other"
    (only_docs / "docs").mkdir(parents=True)
    (only_docs / "docs" / "semantics.md").write_text("x\n")
    assert ImpresarioCollector().detect(only_docs) is False


def test_collect_is_light_and_stores_no_classification(tmp_path: Path) -> None:
    mirror = _mirror(tmp_path)
    snap = ImpresarioCollector().collect(mirror, CollectContext(home=tmp_path))
    assert snap.name == "impresario"
    assert snap.path == str(mirror)
    assert snap.tasks == [] and snap.errors == [] and snap.warnings == []
    assert snap.freshness is not None


def test_collector_is_registered() -> None:
    assert "impresario" in {c.name for c in COLLECTORS}
