"""Snapshot collection shared by the HTTP server and the TUI."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dispatcher.core.collectors import COLLECTORS, CollectContext
from dispatcher.core.descriptions import extract_project_description
from dispatcher.core.discovery import DispatcherConfig, discover
from dispatcher.core.models import ErrorEvent, ProjectSnapshot

# Freshness default owned by frontends (the API `days` default stays None).
# The web JS cannot import this and keeps a copy in index.html; a parity
# test asserts the two values match.
ERRORS_DAYS_DEFAULT = 14
_CACHE_TTL_SECONDS = 5.0
_ISO_PREFIX = 19  # "YYYY-MM-DDTHH:MM:SS" — comparable across naive/aware


def recent_errors(
    events: list[ErrorEvent], days: int, now: datetime | None = None
) -> list[ErrorEvent]:
    """Keep events newer than `days` days; undated events are never dropped.

    Source timestamps mix naive and timezone-aware ISO strings, so the
    comparison uses the first 19 characters, which sort chronologically.
    """
    moment = now if now is not None else datetime.now(tz=UTC)
    cutoff = (moment - timedelta(days=days)).isoformat()[:_ISO_PREFIX]
    return [
        e for e in events if e.timestamp is None or e.timestamp[:_ISO_PREFIX] >= cutoff
    ]


class SnapshotService:
    """Collect-on-demand snapshot cache shared by all frontends.

    Thread-safe: the TUI calls `get()` from worker threads where an
    auto-refresh tick and a manual refresh can overlap; the lock serializes
    collection and the loser of the race is served from the TTL cache
    instead of collecting twice.
    """

    def __init__(self, config: DispatcherConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._at = 0.0
        self._data: tuple[list[ProjectSnapshot], list[str]] | None = None

    def get(self) -> tuple[list[ProjectSnapshot], list[str]]:
        """Return (snapshots, discovery warnings), collecting when stale."""
        with self._lock:
            now = time.monotonic()
            if self._data is not None and now - self._at < _CACHE_TTL_SECONDS:
                return self._data
            self._data = self._collect()
            self._at = now
            return self._data

    def _collect(self) -> tuple[list[ProjectSnapshot], list[str]]:
        # Known gap (accepted in the Stage 2 spec): discover() failures other
        # than the OSErrors it swallows internally propagate out of get().
        found, warnings = discover(self._config.roots, COLLECTORS)
        paths = {d.name: d.path for d in found}
        atp_root = paths.get("atp-platform")
        ctx = CollectContext(
            home=Path.home(),
            maestro_db=self._config.maestro_db,
            catalog_path=(
                None
                if atp_root is None
                else atp_root / "method" / "agents-catalog.toml"
            ),
        )
        snapshots: list[ProjectSnapshot] = []
        for project in found:
            try:
                snapshots.append(project.collector.collect(project.path, ctx))
            except Exception as err:  # noqa: BLE001 — last-resort guard
                snapshots.append(
                    ProjectSnapshot(
                        name=project.name,
                        path=str(project.path),
                        warnings=[f"collector crashed: {err}"],
                    )
                )
        detected = {s.name for s in snapshots}
        snapshots.extend(
            ProjectSnapshot(name=c.name, path="", detected=False)
            for c in COLLECTORS
            if c.name not in detected
        )
        # DESIGN-801: one post-collect enrichment instead of five
        # per-collector implementations; undetected rows (path="") stay None.
        for snap in snapshots:
            if snap.detected and snap.path:
                desc, source = extract_project_description(Path(snap.path))
                snap.description = desc
                snap.description_source = source
        return snapshots, warnings
