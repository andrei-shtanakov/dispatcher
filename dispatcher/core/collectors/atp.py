"""Collector for atp-platform: test results, SSOT catalog, config."""

from __future__ import annotations

from datetime import UTC, datetime
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
    TestRunSummary,
)

_EXPECTED_ALEMBIC = "f1a2b3c4d5e6"


class AtpCollector:
    """Reads ATP dashboard DB, experiment results, and the SSOT catalog."""

    name = "atp-platform"

    def detect(self, path: Path) -> bool:
        return (path / "atp").is_dir() and (
            path / "method" / "agents-catalog.toml"
        ).is_file()

    def collect(self, path: Path, ctx: CollectContext) -> ProjectSnapshot:
        snap = ProjectSnapshot(name=self.name, path=str(path))
        db = path / ".atp-dashboard.db"
        self._collect_db(db, snap)
        self._collect_experiment(path, snap)
        self._collect_bench_output(path, snap)
        self._collect_catalog(path / "method" / "agents-catalog.toml", snap)
        self._collect_config(path / "atp.config.yaml", snap)
        snap.errors.extend(read_otel_errors(path / "logs"))
        snap.freshness = newest_mtime(
            [db, path / "method" / "agents-catalog.toml", path / "atp.config.yaml"]
        )
        return snap

    def _collect_db(self, db: Path, snap: ProjectSnapshot) -> None:
        if not db.is_file() or db.stat().st_size == 0:
            snap.warnings.append(".atp-dashboard.db missing or empty")
            return
        try:
            ver = read_rows(db, "SELECT version_num FROM alembic_version")
            found = ver[0]["version_num"] if ver else None
            snap.schema_versions.append(
                version_check(db.name, found, _EXPECTED_ALEMBIC)
            )
            tests = read_rows(
                db,
                "SELECT id, test_name, started_at, success, score, status, "
                "total_runs, successful_runs FROM test_executions "
                "ORDER BY started_at DESC LIMIT 50",
            )
            for r in tests:
                total = r["total_runs"]
                good = r["successful_runs"]
                snap.test_results.append(
                    TestRunSummary(
                        run_id=str(r["id"]),
                        name=r["test_name"],
                        passed=good,
                        failed=None if total is None else total - (good or 0),
                        total=total,
                        score=r["score"],
                        timestamp=_opt_str(r["started_at"]),
                        source=str(db),
                    )
                )
            runs = read_rows(
                db,
                "SELECT id, agent_name, status, total_score, started_at "
                "FROM benchmark_runs ORDER BY started_at DESC LIMIT 50",
            )
            snap.test_results.extend(
                TestRunSummary(
                    run_id=str(r["id"]),
                    name=f"benchmark: {r['agent_name']} [{r['status']}]",
                    score=r["total_score"],
                    timestamp=_opt_str(r["started_at"]),
                    source=str(db),
                )
                for r in runs
            )
        except SourceReadError as err:
            snap.warnings.append(str(err))

    def _collect_experiment(self, path: Path, snap: ProjectSnapshot) -> None:
        exp = path / "results" / "experiment" / "experiment_results.json"
        if not exp.is_file():
            return
        stamp = datetime.fromtimestamp(exp.stat().st_mtime, tz=UTC).isoformat()
        snap.test_results.append(
            TestRunSummary(
                run_id="experiment",
                name=exp.name,
                timestamp=stamp,
                source=str(exp),
            )
        )

    def _collect_bench_output(self, path: Path, snap: ProjectSnapshot) -> None:
        """List `_bench_output/**/*.db` artifacts (ad-hoc schemas: name+mtime only)."""
        bench = path / "_bench_output"
        if not bench.is_dir():
            return
        for db in sorted(bench.rglob("*.db")):
            stamp = datetime.fromtimestamp(db.stat().st_mtime, tz=UTC).isoformat()
            snap.test_results.append(
                TestRunSummary(
                    run_id=str(db.relative_to(path)),
                    name=str(db.relative_to(path)),
                    timestamp=stamp,
                    source=str(db),
                )
            )

    def _collect_catalog(self, catalog: Path, snap: ProjectSnapshot) -> None:
        try:
            data = read_toml(catalog)
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        for model_id, spec in data.get("models", {}).items():
            snap.models.append(
                ModelInUse(
                    model_id=model_id,
                    vendor=spec.get("vendor"),
                    status=spec.get("status"),
                    role="catalog",
                    source=str(catalog),
                )
            )
        for agent in data.get("agents", []):
            snap.models.append(
                ModelInUse(
                    model_id=str(agent.get("model", "?")),
                    harness=str(agent.get("harness", "?")),
                    role="routable" if agent.get("routable") else "enrolled",
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
        model = data.get("default_llm_model")
        if isinstance(model, str):
            snap.models.append(
                ModelInUse(model_id=model, role="default", source=str(cfg))
            )


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)
