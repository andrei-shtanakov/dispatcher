"""FastAPI application: read-only JSON API over collector snapshots."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from dispatcher.core.collectors import COLLECTORS, CollectContext
from dispatcher.core.contracts import check_contracts
from dispatcher.core.discovery import DispatcherConfig, discover
from dispatcher.core.models import (
    ContractStatus,
    ErrorEvent,
    OverviewEntry,
    OverviewResponse,
    ProjectSnapshot,
)

_CACHE_TTL_SECONDS = 5.0
_STATIC_DIR = Path(__file__).parent / "static"


class _SnapshotCache:
    """Collect-on-demand cache so a polling UI does not hammer the disk."""

    def __init__(self, config: DispatcherConfig) -> None:
        self._config = config
        self._at = 0.0
        self._data: tuple[list[ProjectSnapshot], list[str]] | None = None

    def get(self) -> tuple[list[ProjectSnapshot], list[str]]:
        now = time.monotonic()
        if self._data is not None and now - self._at < _CACHE_TTL_SECONDS:
            return self._data
        self._data = self._collect()
        self._at = now
        return self._data

    def _collect(self) -> tuple[list[ProjectSnapshot], list[str]]:
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
        return snapshots, warnings


def create_app(config: DispatcherConfig) -> FastAPI:
    """Build the API app for the given configuration."""
    app = FastAPI(title="Dispatcher", version="0.1.0")
    cache = _SnapshotCache(config)

    @app.get("/api/overview", response_model=OverviewResponse)
    def overview() -> OverviewResponse:
        snapshots, warnings = cache.get()
        entries = [
            OverviewEntry(
                name=s.name,
                path=s.path or None,
                detected=s.detected,
                freshness=s.freshness,
                counts={
                    "tasks": len(s.tasks),
                    "models": len(s.models),
                    "test_results": len(s.test_results),
                    "errors": len(s.errors),
                },
                warnings=s.warnings,
            )
            for s in snapshots
        ]
        return OverviewResponse(projects=entries, warnings=warnings)

    @app.get("/api/projects/{name}", response_model=ProjectSnapshot)
    def project_detail(name: str) -> ProjectSnapshot:
        snapshots, _ = cache.get()
        for snap in snapshots:
            if snap.name == name:
                return snap
        raise HTTPException(status_code=404, detail=f"unknown project: {name}")

    @app.get("/api/errors", response_model=list[ErrorEvent])
    def errors(limit: int = Query(100, ge=0)) -> list[ErrorEvent]:
        snapshots, _ = cache.get()
        merged = [e for s in snapshots for e in s.errors]
        merged.sort(key=lambda e: e.timestamp or "", reverse=True)
        return merged[:limit]

    @app.get("/api/models")
    def models() -> list[dict[str, Any]]:
        snapshots, _ = cache.get()
        return [
            {"project": s.name, **m.model_dump()} for s in snapshots for m in s.models
        ]

    @app.get("/api/contracts", response_model=list[ContractStatus])
    def contracts() -> list[ContractStatus]:
        snapshots, _ = cache.get()
        projects = {s.name: Path(s.path) for s in snapshots if s.detected and s.path}
        return check_contracts(projects)

    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
    return app
