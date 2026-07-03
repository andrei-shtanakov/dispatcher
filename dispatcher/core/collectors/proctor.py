"""Collector for proctor-a: task/schedule state, LLM config, text logs."""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.collectors.base import (
    CollectContext,
    SourceReadError,
    coerce_str,
    mask_secrets,
    newest_mtime,
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

_EXPECTED_TABLES = {"tasks", "schedules"}
_LOG_ERRORS_LIMIT = 20


class ProctorCollector:
    """Reads proctor-a's state DB, proctor.yaml, and plain-text logs."""

    name = "proctor-a"

    def detect(self, path: Path) -> bool:
        return (path / "config" / "proctor.yaml").is_file()

    def collect(self, path: Path, ctx: CollectContext) -> ProjectSnapshot:
        snap = ProjectSnapshot(name=self.name, path=str(path))
        db = path / "data" / "state.db"
        self._collect_db(db, snap)
        self._collect_config(path / "config" / "proctor.yaml", snap)
        snap.errors.extend(_text_log_errors(path / "logs"))
        snap.freshness = newest_mtime(
            [db, path / "config" / "proctor.yaml", path / "logs"]
        )
        return snap

    def _collect_db(self, db: Path, snap: ProjectSnapshot) -> None:
        if not db.is_file() or db.stat().st_size == 0:
            snap.warnings.append("data/state.db missing or empty")
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
                "SELECT id, status, created_at, updated_at FROM tasks "
                "ORDER BY updated_at DESC LIMIT 50",
            )
            snap.tasks = [
                TaskInfo(
                    task_id=coerce_str(r["id"]),
                    status=coerce_str(r["status"]),
                    started_at=r["created_at"],
                    source=str(db),
                )
                for r in rows
            ]
            schedules = read_rows(
                db,
                "SELECT id, type, expression, enabled, next_run, last_run "
                "FROM schedules LIMIT 50",
            )
            snap.tasks.extend(
                TaskInfo(
                    task_id=coerce_str(r["id"]),
                    title=f"{r['type']} {r['expression']} (next={r['next_run']})",
                    status="enabled" if r["enabled"] else "disabled",
                    started_at=r["last_run"],
                    source=str(db),
                )
                for r in schedules
            )
        except SourceReadError as err:
            snap.warnings.append(str(err))

    def _collect_config(self, cfg: Path, snap: ProjectSnapshot) -> None:
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
        llm = data.get("llm")
        if not isinstance(llm, dict):
            llm = {}
        for key, role in (("default_model", "default"), ("fallback_model", "fallback")):
            model = llm.get(key)
            if isinstance(model, str):
                snap.models.append(
                    ModelInUse(model_id=model, role=role, source=str(cfg))
                )


def _text_log_errors(
    logs_dir: Path, limit: int = _LOG_ERRORS_LIMIT
) -> list[ErrorEvent]:
    """Grep plain-text logs for ERROR lines (proctor has no OTel dirs)."""
    events: list[ErrorEvent] = []
    if not logs_dir.is_dir():
        return events
    for log in sorted(logs_dir.glob("*.log")):
        try:
            lines = log.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if "ERROR" in line:
                events.append(
                    ErrorEvent(
                        service="proctor-a",
                        body=mask_secrets(line),
                        source=str(log),
                    )
                )
    return events[-limit:]
