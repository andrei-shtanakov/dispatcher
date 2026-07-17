"""Read-model for a Maestro project.yaml's `spec_runner:` block (DESIGN-302).

Mirrors two things that live in the `maestro` repo and cannot be imported
across the polyrepo boundary: `SpecRunnerConfig`'s typed-field defaults and
`to_executor_config()` + its `_deep_merge` (maestro/models.py:1152-1248,
commit 0122942). If Maestro changes that shape, this module's mirror must be
updated by hand — there is no shared import path.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from dispatcher.core.collectors.base import SourceReadError, read_yaml

TYPED_DEFAULTS: dict[str, Any] = {
    "max_retries": 3,
    "task_timeout_minutes": 30,
    "claude_command": "claude",
    "auto_commit": True,
    "create_git_branch": True,
    "run_tests_on_done": True,
    "test_command": "uv run pytest",
    "run_lint_on_done": True,
    "lint_command": "uv run ruff check .",
    "claude_model": "",
    "review_command": "",
    "review_model": "",
}
TYPED_FIELDS: tuple[str, ...] = tuple(TYPED_DEFAULTS)
_SPEC_PREFIX = ""  # Maestro's SPEC_PREFIX constant; not configurable here


class TypedField(BaseModel):
    """One typed `spec_runner:` field, with provenance."""

    value: Any
    explicit: bool  # True: key present in project.yaml. False: pydantic default.


class ProjectSpecRunnerConfig(BaseModel):
    """One project's `spec_runner:` block, as Maestro would load it."""

    project: str
    project_yaml_path: str
    base_mtime: float  # project_yaml's on-disk mtime; echoed back for conflict checks
    typed: dict[str, TypedField]
    extra_executor_config: dict[str, Any] = Field(default_factory=dict)
    extra_explicit: bool


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge; overlay wins on scalar conflicts. Non-mutating."""
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _to_executor_config(typed: dict[str, Any]) -> dict[str, Any]:
    """Mirrors `SpecRunnerConfig.to_executor_config()` (maestro/models.py:1220)."""
    return {
        "executor": {
            "max_retries": typed["max_retries"],
            "task_timeout_minutes": typed["task_timeout_minutes"],
            "claude_command": typed["claude_command"],
            "auto_commit": typed["auto_commit"],
            "claude_model": typed["claude_model"],
            "review_command": typed["review_command"],
            "review_model": typed["review_model"],
            "spec_prefix": _SPEC_PREFIX,
            "hooks": {
                "pre_start": {"create_git_branch": typed["create_git_branch"]},
                "post_done": {
                    "run_tests": typed["run_tests_on_done"],
                    "run_lint": typed["run_lint_on_done"],
                    "auto_commit": typed["auto_commit"],
                },
            },
            "commands": {"test": typed["test_command"], "lint": typed["lint_command"]},
        }
    }


def effective_executor_config(cfg: ProjectSpecRunnerConfig) -> dict[str, Any]:
    """The `{"executor": {...}}` dict spec-runner would actually see."""
    typed_values = {name: field.value for name, field in cfg.typed.items()}
    base = _to_executor_config(typed_values)
    if cfg.extra_executor_config:
        return _deep_merge(base, cfg.extra_executor_config)
    return base


def read_project_spec_runner_config(project_yaml: Path) -> ProjectSpecRunnerConfig:
    """Parse one `project.yaml`'s `spec_runner:` block. Raises SourceReadError."""
    data = read_yaml(project_yaml)
    raw: dict[str, Any] = data.get("spec_runner") or {}
    typed = {
        name: TypedField(value=raw.get(name, default), explicit=name in raw)
        for name, default in TYPED_DEFAULTS.items()
    }
    return ProjectSpecRunnerConfig(
        project=data.get("project") or project_yaml.parent.name,
        project_yaml_path=str(project_yaml),
        base_mtime=project_yaml.stat().st_mtime,
        typed=typed,
        extra_executor_config=raw.get("extra_executor_config") or {},
        extra_explicit="extra_executor_config" in raw,
    )


def discover_project_configs(
    roots: tuple[Path, ...],
) -> tuple[list[ProjectSpecRunnerConfig], list[str]]:
    """Scan `roots` for `<child>/project.yaml` files (Maestro projects).

    A missing `spec_runner:` block is not skipped — the config is returned
    with every typed field at its default (`explicit=False`), matching how
    Maestro itself would load such a file.
    """
    found: list[ProjectSpecRunnerConfig] = []
    warnings: list[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = sorted(
                d
                for d in root.iterdir()
                if d.is_dir() and not d.name.startswith(("_", "."))
            )
        except OSError as err:
            warnings.append(f"cannot list {root}: {err}")
            continue
        for child in children:
            project_yaml = child / "project.yaml"
            if not project_yaml.is_file():
                continue
            try:
                found.append(read_project_spec_runner_config(project_yaml))
            except SourceReadError as err:
                warnings.append(str(err))
    return found, warnings
