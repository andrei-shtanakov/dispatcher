"""Collector for spec-runner: executor state DB, config, schemas, logs."""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.collectors.base import (
    CollectContext,
    SourceReadError,
    mask_secrets,
    newest_mtime,
    read_otel_errors,
    read_rows,
    read_yaml,
    shallow_summary,
    table_names,
    version_check,
)
from dispatcher.core.models import (
    ConfigSummary,
    ErrorEvent,
    ModelInUse,
    ProjectSnapshot,
    TaskInfo,
)

_EXPECTED_TABLES = {"tasks", "attempts", "executor_meta"}


class SpecRunnerCollector:
    """Reads spec-runner's on-disk executor state."""

    name = "spec-runner"

    def detect(self, path: Path) -> bool:
        return (path / "src" / "spec_runner").is_dir()

    def collect(self, path: Path, ctx: CollectContext) -> ProjectSnapshot:
        snap = ProjectSnapshot(name=self.name, path=str(path))
        db = path / "spec" / ".executor-state.db"
        self._collect_db(db, snap)
        self._collect_config(path / "executor.config.yaml", snap)
        snap.errors.extend(read_otel_errors(path / "logs"))
        snap.freshness = newest_mtime(
            [db, path / "executor.config.yaml", path / "logs"]
        )
        return snap

    def _collect_db(self, db: Path, snap: ProjectSnapshot) -> None:
        if not db.is_file() or db.stat().st_size == 0:
            snap.warnings.append("executor-state.db missing or empty")
            return
        try:
            tables = table_names(db)
            present = tables & _EXPECTED_TABLES
            snap.schema_versions.append(
                version_check(
                    db.name,
                    ",".join(sorted(present)),
                    ",".join(sorted(_EXPECTED_TABLES)),
                )
            )
            if not _EXPECTED_TABLES <= tables:
                return
            rows = read_rows(
                db,
                "SELECT task_id, status, started_at, completed_at FROM tasks "
                "ORDER BY started_at DESC LIMIT 50",
            )
            snap.tasks = [
                TaskInfo(
                    task_id=r["task_id"],
                    status=r["status"],
                    started_at=r["started_at"],
                    completed_at=r["completed_at"],
                    source=str(db),
                )
                for r in rows
            ]
            fails = read_rows(
                db,
                "SELECT task_id, timestamp, error, error_kind, error_stage "
                "FROM attempts WHERE success = 0 "
                "ORDER BY timestamp DESC LIMIT 20",
            )
            snap.errors.extend(
                ErrorEvent(
                    timestamp=r["timestamp"],
                    service=self.name,
                    body=(
                        f"{r['task_id']}: {r['error'] or r['error_kind'] or 'failed'}"
                        f" (stage={r['error_stage']})"
                    ),
                    source=str(db),
                )
                for r in fails
            )
        except SourceReadError as err:
            snap.warnings.append(str(err))

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
        model = data.get("review_model")
        if isinstance(model, str):
            snap.models.append(
                ModelInUse(model_id=model, role="review", source=str(cfg))
            )
