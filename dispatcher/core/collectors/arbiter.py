"""Collector for arbiter: routing decisions, benchmarks, agent policy."""

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
    shallow_summary,
    version_check,
)
from dispatcher.core.models import (
    ConfigSummary,
    ModelInUse,
    ProjectSnapshot,
    TaskInfo,
    TestRunSummary,
)

_EXPECTED_SCHEMA = "1"


def _decision_title(chosen_agent: str, confidence: float | None) -> str:
    """Format a decision title, tolerating a NULL confidence value."""
    conf_txt = "n/a" if confidence is None else f"{confidence:.2f}"
    return f"{chosen_agent} (conf={conf_txt})"


class ArbiterCollector:
    """Reads arbiter's decision DB and agent policy configs."""

    name = "arbiter"

    def detect(self, path: Path) -> bool:
        return (path / "config" / "agents.toml").is_file() and (
            path / "arbiter-core"
        ).is_dir()

    def collect(self, path: Path, ctx: CollectContext) -> ProjectSnapshot:
        snap = ProjectSnapshot(name=self.name, path=str(path))
        db = path / "arbiter.db"
        self._collect_db(db, snap)
        self._collect_agents(path / "config" / "agents.toml", snap)
        self._collect_invariants(path / "config" / "invariants.toml", snap)
        snap.errors.extend(read_otel_errors(path / "logs"))
        snap.freshness = newest_mtime([db, path / "config", path / "logs"])
        return snap

    def _collect_db(self, db: Path, snap: ProjectSnapshot) -> None:
        if not db.is_file() or db.stat().st_size == 0:
            snap.warnings.append("arbiter.db missing or empty")
            return
        try:
            ver = read_rows(db, "SELECT MAX(version) AS v FROM schema_version")
            found = None if ver[0]["v"] is None else str(ver[0]["v"])
            snap.schema_versions.append(version_check(db.name, found, _EXPECTED_SCHEMA))
            decisions = read_rows(
                db,
                "SELECT task_id, timestamp, chosen_agent, action, confidence "
                "FROM decisions ORDER BY timestamp DESC LIMIT 50",
            )
            snap.tasks = [
                TaskInfo(
                    task_id=r["task_id"],
                    title=_decision_title(r["chosen_agent"], r["confidence"]),
                    status=r["action"],
                    started_at=r["timestamp"],
                    source=str(db),
                )
                for r in decisions
            ]
            runs = read_rows(
                db,
                "SELECT run_id, benchmark_id, agent_id, ts, score, "
                "total_cost_usd FROM benchmark_runs ORDER BY ts DESC LIMIT 50",
            )
            snap.test_results = [
                TestRunSummary(
                    run_id=r["run_id"],
                    name=f"{r['benchmark_id']} @ {r['agent_id']}",
                    score=r["score"],
                    timestamp=r["ts"],
                    source=str(db),
                )
                for r in runs
            ]
        except SourceReadError as err:
            snap.warnings.append(str(err))

    def _collect_agents(self, cfg: Path, snap: ProjectSnapshot) -> None:
        if not cfg.is_file():
            snap.warnings.append("config/agents.toml missing")
            return
        try:
            data = read_toml(cfg)
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        for key in data:
            harness, _, model = key.partition("@")
            snap.models.append(
                ModelInUse(
                    model_id=model or harness,
                    harness=harness,
                    role="routable",
                    source=str(cfg),
                )
            )

    def _collect_invariants(self, cfg: Path, snap: ProjectSnapshot) -> None:
        if not cfg.is_file():
            return
        try:
            data = read_toml(cfg)
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        snap.configs.append(
            ConfigSummary(
                path=str(cfg),
                format="toml",
                summary=mask_secrets(shallow_summary(data)),
            )
        )
