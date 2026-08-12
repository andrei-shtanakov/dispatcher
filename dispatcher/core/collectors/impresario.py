"""Discovery-only collector for the impresario product-governance mirror.

Inbox #129: detection is content-based and requires BOTH anchors, so one
incidental file cannot masquerade as the mirror. The snapshot stays light
on purpose — gate_waiting classification runs on demand in
`core/product_proposals.py` and never enters the snapshot cache.
"""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.collectors.base import CollectContext, newest_mtime
from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.product_proposals import ANCHOR_FILES


class ImpresarioCollector:
    """Detects the impresario mirror; collects a light snapshot only."""

    name = "impresario"

    def detect(self, path: Path) -> bool:
        return all((path / rel).is_file() for rel in ANCHOR_FILES)

    def collect(self, path: Path, ctx: CollectContext) -> ProjectSnapshot:
        snap = ProjectSnapshot(name=self.name, path=str(path))
        snap.freshness = newest_mtime([path / rel for rel in ANCHOR_FILES])
        return snap
