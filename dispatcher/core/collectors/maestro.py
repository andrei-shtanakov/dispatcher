"""Collector for Maestro: per-project run DBs, catalog models, logs.

Maestro's orchestration state lives one database per run (#147, producer
design maestro `2026-08-15-maestro-state-layout-design.md`):

    <maestro_home>/projects/<host>/<owner>/<repo>/runs/<run-id>/state.db
    <maestro_home>/projects/_local/<name>-<hash>/runs/<run-id>/state.db
    <maestro_home>/projects/<...>/locks/orchestrate.holder
    <maestro_home>/maestro.db          # legacy, frozen, forensics only

Classification is fail-closed: `running` needs positive evidence (the holder
sidecar written under the held stage lock, and its pid alive); a run with no
terminal record is `interrupted`, never in-progress — inferring liveness from
a missing terminal record is exactly the lie #147 removes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from dispatcher.core.collectors.base import (
    CollectContext,
    SourceReadError,
    coerce_str,
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
    OrchestrationRunInfo,
    ProjectSnapshot,
    TaskInfo,
)

_EXPECTED_SCHEMA = "2"
_RUN_ROW_SQL = (
    "SELECT run_id, started_at, outcome, ended_at, reason, suspended_at "
    "FROM run LIMIT 1"
)
_TASKS_SQL = (
    "SELECT id, title, status, agent_type, created_at, "
    "started_at, completed_at FROM tasks "
    "ORDER BY created_at DESC LIMIT 50"
)
_COSTS_SQL = (
    "SELECT task_id, SUM(estimated_cost_usd) AS cost FROM task_costs GROUP BY task_id"
)


class MaestroCollector:
    """Reads Maestro's per-run state DBs and routable models from the catalog."""

    name = "Maestro"

    def detect(self, path: Path) -> bool:
        return (path / "maestro").is_dir() and (path / "pyproject.toml").is_file()

    def collect(self, path: Path, ctx: CollectContext) -> ProjectSnapshot:
        snap = ProjectSnapshot(name=self.name, path=str(path))
        run_dbs = self._collect_runs(ctx.maestro_home, snap)
        self._collect_legacy_db(ctx.maestro_db, snap)
        self._collect_catalog_models(ctx.catalog_path, snap)
        self._collect_config(path / "executor.config.yaml", snap)
        snap.errors.extend(read_otel_errors(path / "logs"))
        sources = [path / "executor.config.yaml", path / "logs", *run_dbs]
        if ctx.maestro_db is not None:
            sources.append(ctx.maestro_db)
        snap.freshness = newest_mtime(sources)
        return snap

    def _collect_runs(self, home: Path | None, snap: ProjectSnapshot) -> list[Path]:
        """Enumerate per-project run DBs; returns freshness sources."""
        if home is None:
            return []
        projects = home / "projects"
        if not projects.is_dir():
            # Normal on a machine where Maestro has not run since the layout
            # change — zero runs render as zero runs, not as a warning.
            return []
        sources: list[Path] = []
        for repo_key, project_dir in _project_dirs(projects, snap):
            holder = _holder_run_id(project_dir / "locks")
            runs: list[tuple[OrchestrationRunInfo, Path]] = []
            for run_dir in _subdirs(project_dir / "runs", snap):
                db = run_dir / "state.db"
                if not db.is_file():
                    continue
                sources.append(db)
                runs.append(
                    (_classify_run(db, repo_key, run_dir.name, holder, snap), db)
                )
            runs.sort(
                key=lambda r: (r[0].started_at or "", r[0].run_id or ""),
                reverse=True,
            )
            snap.runs.extend(info for info, _ in runs)
            if runs:
                newest_db = runs[0][1]
                self._collect_run_tasks(newest_db, snap)
                logs_dir = newest_db.parent / "logs"
                snap.errors.extend(read_otel_errors(logs_dir))
                # Everything read must feed freshness: telemetry can land
                # under the run's logs/ after the last state.db write.
                sources.append(logs_dir)
        return sources

    def _collect_run_tasks(self, db: Path, snap: ProjectSnapshot) -> None:
        try:
            costs = {r["task_id"]: r["cost"] for r in read_rows(db, _COSTS_SQL)}
            rows = read_rows(db, _TASKS_SQL)
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        snap.tasks.extend(_task_info(r, costs, db) for r in rows)

    def _collect_legacy_db(self, db: Path | None, snap: ProjectSnapshot) -> None:
        """The frozen pre-#147 single file: kept visible, labeled legacy."""
        if db is None or not db.is_file():
            snap.warnings.append(
                "maestro.db not found (~/.maestro/maestro.db; "
                "set maestro_db in dispatcher.toml)"
            )
            return
        snap.runs.append(
            OrchestrationRunInfo(repo_key="legacy", status="legacy", source=str(db))
        )
        try:
            ver = read_rows(db, "SELECT MAX(version) AS v FROM schema_migrations")
            found = None if ver[0]["v"] is None else str(ver[0]["v"])
            snap.schema_versions.append(version_check(db.name, found, _EXPECTED_SCHEMA))
            costs = {r["task_id"]: r["cost"] for r in read_rows(db, _COSTS_SQL)}
            rows = read_rows(db, _TASKS_SQL)
            snap.tasks.extend(_task_info(r, costs, db) for r in rows)
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
        agents = data.get("agents", [])
        if not isinstance(agents, list):
            agents = []
        for agent in agents:
            if not isinstance(agent, dict) or not agent.get("routable"):
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


def _project_dirs(projects: Path, snap: ProjectSnapshot) -> list[tuple[str, Path]]:
    """(repo_key, project dir) pairs; `_local` keys are two segments."""
    out: list[tuple[str, Path]] = []
    for host in _subdirs(projects, snap):
        if host.name == "_local":
            out.extend((f"_local/{repo.name}", repo) for repo in _subdirs(host, snap))
            continue
        for owner in _subdirs(host, snap):
            out.extend(
                (f"{host.name}/{owner.name}/{repo.name}", repo)
                for repo in _subdirs(owner, snap)
            )
    return sorted(out)


def _subdirs(path: Path, snap: ProjectSnapshot | None = None) -> list[Path]:
    """List subdirectories; an unreadable directory must WARN, not read as
    empty — a silently swallowed OSError here is how «0 runs» would lie
    (the `runs `/`run ` warning prefixes are the surfaces' degradation
    signal — see the prefix pin in tests/test_maestro.py)."""
    if not path.is_dir():
        return []
    try:
        return sorted(d for d in path.iterdir() if d.is_dir())
    except OSError as err:
        if snap is not None:
            snap.warnings.append(f"runs enumeration: cannot list {path}: {err}")
        return []


def _classify_run(
    db: Path,
    repo_key: str,
    run_id: str,
    holder_run_id: str | None,
    snap: ProjectSnapshot,
) -> OrchestrationRunInfo:
    """Producer design §B.3, fail-closed. Order: terminal, running, pause."""
    unreadable = OrchestrationRunInfo(
        repo_key=repo_key, run_id=run_id, status="unreadable", source=str(db)
    )
    try:
        rows = read_rows(db, _RUN_ROW_SQL)
    except SourceReadError as err:
        snap.warnings.append(f"run {run_id}: {err}")
        return unreadable
    if not rows:
        # A visible run directory always carries its `run` row (producer
        # design §D rename-into-place) — its absence reads as corruption,
        # not as legacy.
        snap.warnings.append(f"run {run_id}: no run row in {db.name}")
        return unreadable
    row = rows[0]
    outcome = row["outcome"]
    if outcome is not None:
        status = str(outcome)
    elif holder_run_id is not None and holder_run_id == run_id:
        status = "running"
    elif row["suspended_at"] is not None:
        status = "suspended"
    else:
        status = "interrupted"
    return OrchestrationRunInfo(
        repo_key=repo_key,
        run_id=run_id,
        status=status,
        started_at=_opt_str(row["started_at"]),
        ended_at=_opt_str(row["ended_at"]),
        reason=_opt_str(row["reason"]),
        source=str(db),
    )


def _holder_run_id(locks: Path) -> str | None:
    """The run the held orchestrate lock attributes liveness to, or None.

    The holder sidecar is unlinked on clean release but survives a crash, so
    it never grants liveness alone: the recorded pid must also be alive.
    (Probing the flock itself would mean momentarily *taking* it — a
    read-plane process interfering with producer control flow; the pid check
    is the nearest non-interfering evidence. Fail-closed on anything odd.)
    """
    try:
        data = json.loads((locks / "orchestrate.holder").read_text())
        run_id = data["run_id"]
        pid = data["pid"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(run_id, str) or not isinstance(pid, int):
        return None
    return run_id if _pid_alive(pid) else None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:  # 0/negative would signal a process group, not a process
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _task_info(
    row: dict[str, object], costs: dict[object, float], db: Path
) -> TaskInfo:
    return TaskInfo(
        task_id=coerce_str(row["id"]),
        title=f"{row['title']} [{row['agent_type']}]",
        status=coerce_str(row["status"]),
        started_at=_opt_str(row["started_at"]),
        completed_at=_opt_str(row["completed_at"]),
        cost_usd=costs.get(row["id"]),
        source=str(db),
    )


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)
