from pathlib import Path

import pytest

from dispatcher.core.spec_runner_config import (
    discover_project_configs,
    effective_executor_config,
    read_project_spec_runner_config,
)

_STEWARD_YAML = """
project: steward
description: test fixture
spec_runner:
  max_retries: 3
  task_timeout_minutes: 30
  claude_command: claude
  auto_commit: true
  create_git_branch: true
  run_tests_on_done: true
  test_command: uv run pytest
  run_lint_on_done: true
  lint_command: uv run ruff check .
workstreams: []
"""

_WITH_EXTRA_YAML = """
project: alpha
spec_runner:
  max_retries: 5
  claude_model: claude-opus-4-8
  extra_executor_config:
    executor:
      personas:
        reviewer:
          model: claude-opus-4-8
          focus: [security]
      hooks:
        post_done:
          review_parallel: true
workstreams: []
"""


def test_read_typed_fields_and_explicit_flags(tmp_path: Path) -> None:
    project_yaml = tmp_path / "project.yaml"
    project_yaml.write_text(_STEWARD_YAML)
    cfg = read_project_spec_runner_config(project_yaml)
    assert cfg.project == "steward"
    assert isinstance(cfg.base_mtime, float)
    assert cfg.base_mtime == pytest.approx(project_yaml.stat().st_mtime)
    assert cfg.typed["max_retries"].value == 3
    assert cfg.typed["max_retries"].explicit is True
    assert cfg.typed["claude_model"].value == ""
    assert cfg.typed["claude_model"].explicit is False
    assert cfg.extra_executor_config == {}
    assert cfg.extra_explicit is False


def test_read_extra_executor_config(tmp_path: Path) -> None:
    project_yaml = tmp_path / "project.yaml"
    project_yaml.write_text(_WITH_EXTRA_YAML)
    cfg = read_project_spec_runner_config(project_yaml)
    assert cfg.extra_explicit is True
    assert cfg.extra_executor_config["executor"]["personas"]["reviewer"]["model"] == (
        "claude-opus-4-8"
    )
    assert cfg.typed["max_retries"].value == 5
    assert cfg.typed["claude_model"].value == "claude-opus-4-8"


def test_effective_executor_config_merges_typed_and_extra(tmp_path: Path) -> None:
    project_yaml = tmp_path / "project.yaml"
    project_yaml.write_text(_WITH_EXTRA_YAML)
    cfg = read_project_spec_runner_config(project_yaml)
    effective = effective_executor_config(cfg)
    assert effective["executor"]["max_retries"] == 5
    assert effective["executor"]["claude_model"] == "claude-opus-4-8"
    assert effective["executor"]["personas"]["reviewer"]["focus"] == ["security"]
    assert effective["executor"]["hooks"]["post_done"]["review_parallel"] is True
    # typed hooks.post_done keys survive the merge alongside the extra ones
    assert effective["executor"]["hooks"]["post_done"]["run_tests"] is True


def test_discover_project_configs_scans_workspace(tmp_path: Path) -> None:
    (tmp_path / "has-config").mkdir()
    (tmp_path / "has-config" / "project.yaml").write_text(_STEWARD_YAML)
    (tmp_path / "no-config").mkdir()
    (tmp_path / "_cowork_output").mkdir()
    (tmp_path / "_cowork_output" / "project.yaml").write_text(_STEWARD_YAML)

    configs, warnings = discover_project_configs((tmp_path,))

    assert warnings == []
    assert [c.project for c in configs] == ["steward"]
    assert configs[0].project_yaml_path == str(tmp_path / "has-config" / "project.yaml")
