"""Collector for Maestro: task DB in ~/.maestro, catalog models, logs."""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.collectors.base import (
    CollectContext,
    SourceReadError,
    mask_secrets,
    newest_mtime,
    read_otel_errors,
    read_rows,
    read_toml,
    read_yaml,
    shallow_summary,
    version_check,
)
from dispatcher.core.models import (
    ConfigSummary,
    ModelInUse,
    ProjectSnapshot,
    TaskInfo,
)

_EXPECTED_SCHEMA = "2"


class MaestroCollector:
    """Reads Maestro's CLI state DB and routable models from the catalog."""

    name = "Maestro"

    def detect(self, path: Path) -> bool:
        return (path / "maestro").is_dir() and (path / "pyproject.toml").is_file()

    def collect(self, path: Path, ctx: CollectContext) -> ProjectSnapshot:
        snap = ProjectSnapshot(name=self.name, path=str(path))
        self._collect_db(ctx.maestro_db, snap)
        self._collect_catalog_models(ctx.catalog_path, snap)
        self._collect_config(path / "executor.config.yaml", snap)
        snap.errors.extend(read_otel_errors(path / "logs"))
        sources = [path / "executor.config.yaml", path / "logs"]
        if ctx.maestro_db is not None:
            sources.append(ctx.maestro_db)
        snap.freshness = newest_mtime(sources)
        return snap

    def _collect_db(self, db: Path | None, snap: ProjectSnapshot) -> None:
        if db is None or not db.is_file():
            snap.warnings.append(
                "maestro.db not found (~/.maestro/maestro.db; "
                "set maestro_db in dispatcher.toml)"
            )
            return
        try:
            ver = read_rows(db, "SELECT MAX(version) AS v FROM schema_migrations")
            found = None if ver[0]["v"] is None else str(ver[0]["v"])
            snap.schema_versions.append(version_check(db.name, found, _EXPECTED_SCHEMA))
            costs = {
                r["task_id"]: r["cost"]
                for r in read_rows(
                    db,
                    "SELECT task_id, SUM(estimated_cost_usd) AS cost "
                    "FROM task_costs GROUP BY task_id",
                )
            }
            rows = read_rows(
                db,
                "SELECT id, title, status, agent_type, created_at, "
                "started_at, completed_at FROM tasks "
                "ORDER BY created_at DESC LIMIT 50",
            )
            snap.tasks = [
                TaskInfo(
                    task_id=r["id"],
                    title=f"{r['title']} [{r['agent_type']}]",
                    status=r["status"],
                    started_at=_opt_str(r["started_at"]),
                    completed_at=_opt_str(r["completed_at"]),
                    cost_usd=costs.get(r["id"]),
                    source=str(db),
                )
                for r in rows
            ]
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        pid = db.parent / "maestro.pid"
        snap.configs.append(
            ConfigSummary(
                path=str(pid), format="pid", summary={"running": pid.is_file()}
            )
        )

    def _collect_catalog_models(
        self, catalog: Path | None, snap: ProjectSnapshot
    ) -> None:
        if catalog is None or not catalog.is_file():
            snap.warnings.append("agents catalog not available (atp-platform?)")
            return
        try:
            data = read_toml(catalog)
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        for agent in data.get("agents", []):
            if not agent.get("routable"):
                continue
            snap.models.append(
                ModelInUse(
                    model_id=str(agent.get("model", "?")),
                    harness=str(agent.get("harness", "?")),
                    role="routable",
                    source=str(catalog),
                )
            )

    def _collect_config(self, cfg: Path, snap: ProjectSnapshot) -> None:
        if not cfg.is_file():
            return
        try:
            data = read_yaml(cfg)
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        snap.configs.append(
            ConfigSummary(
                path=str(cfg),
                format="yaml",
                summary=mask_secrets(shallow_summary(data)),
            )
        )


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)
