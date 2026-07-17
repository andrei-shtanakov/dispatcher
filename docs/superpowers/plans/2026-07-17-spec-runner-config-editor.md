# Spec-runner Config Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a human view and edit, via a PR, the `spec_runner:` block of a Maestro-managed project's `project.yaml` — covering every field Maestro's `SpecRunnerConfig` now supports (typed fields + the `extra_executor_config` overlay) — without dispatcher ever writing to a default branch or acting outside an explicit click.

**Architecture:** Three new `dispatcher/core/` modules layered read → validate → mutate: `spec_runner_config.py` (parse `project.yaml`, compute the effective config), `spec_runner_config_schema.py` (reject bad candidates before any diff exists), `spec_runner_config_actions.py` (build a scoped diff, write it, delegate branch/PR to `github-checker`, one module = one new whitelisted action, kept deliberately separate from `core/actions.py`'s `ActionRunner`). Two new API routes and one extension to the existing web detail panel expose it.

**Tech Stack:** Python 3.12, FastAPI, pydantic v2, PyYAML (read), `ruamel.yaml` (round-trip-preserving write — new dependency), `jsonschema` (new dependency), pytest + anyio.

**Scope note:** This plan covers spec DESIGN-301 through 306 (M1: read, validate, edit-via-PR, docs). DESIGN-307 (AI-agent value suggestions) and DESIGN-308 (TUI parity) are **out of this plan** — the spec's own milestones split them into M2/a follow-up, and they don't block a working, independently-shippable M1 slice. Write a separate plan for DESIGN-307 after this one ships.

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-17-spec-runner-config-editor-design.md` (read it before starting — this plan implements it section by section).
- Line length 88 chars (ruff), type hints required, `uv run pyrefly check` must pass after every task.
- Dispatcher never writes to a default branch and never commits/pushes itself — only `github-checker open-pr` does, exactly as today's `pull`/`create-pr` actions work (`dispatcher/core/actions.py`).
- Every action attempt (rejected, busy, or executed) leaves one audit log line — no exceptions.
- The new action class (`update-spec-runner-config`) must NOT share a class/lock/logger with `core/actions.py`'s `ActionRunner` — this was an explicit stakeholder requirement (spec §1, X-02).
- No edits outside the `spec_runner:` key of `project.yaml`. No new file types are ever touched.

---

## File Structure

- Create: `contracts/executor-config/v0-provisional/schema.json` — pinned JSON Schema for the `extra_executor_config` overlay shape only (not the typed fields — those are validated in code, mirroring Maestro's pydantic types).
- Create: `contracts/executor-config/v0-provisional/README.md` — provenance header (source commit, promotion path).
- Create: `dispatcher/core/spec_runner_config.py` — read-model: discover `project.yaml` files, parse the `spec_runner:` block into typed fields (with explicit/default provenance) + the `extra_executor_config` overlay, compute the effective merged view.
- Create: `dispatcher/core/spec_runner_config_schema.py` — validation for both tiers (typed field types; `extra_executor_config` against the pinned schema).
- Create: `dispatcher/core/spec_runner_config_actions.py` — `SpecRunnerConfigActionRunner`: builds a scoped diff with `ruamel.yaml`, writes it, delegates to `github-checker open-pr`. Own lock, own audit logger.
- Modify: `dispatcher/server/app.py` — add `GET /api/projects/{name}/spec-runner-config` and `POST /api/actions/update-spec-runner-config`.
- Modify: `dispatcher/server/static/index.html` — extend the existing `detail()` panel with a config-editing form.
- Modify: `pyproject.toml` — add `ruamel.yaml` and `jsonschema` dependencies (via `uv add`, folded into the tasks that need them).
- Modify (docs, spec §6): `CLAUDE.md`, `spec/discovery-brief-customer.md`, `spec/discovery-brief-engineer.md`, `docs/superpowers/specs/2026-07-14-sync-roadmap-design.md`, `dispatcher/core/actions.py` (docstring only).
- Test: `tests/test_spec_runner_config.py`, `tests/test_spec_runner_config_schema.py`, `tests/test_spec_runner_config_actions.py`, extend `tests/test_api.py`.

---

### Task 1: Provisional pinned schema (`contracts/executor-config/v0-provisional/`)

**Files:**
- Create: `contracts/executor-config/v0-provisional/schema.json`
- Create: `contracts/executor-config/v0-provisional/README.md`
- Test: `tests/test_spec_runner_config_schema.py` (schema-validity check only in this task; validation-function tests come in Task 3)

**Interfaces:**
- Produces: a file at `contracts/executor-config/v0-provisional/schema.json`, loaded by Task 3's `dispatcher/core/spec_runner_config_schema.py` via a hardcoded relative path (`Path(__file__).resolve().parents[2] / "contracts" / "executor-config" / "v0-provisional" / "schema.json"`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_spec_runner_config_schema.py
import json
from pathlib import Path

import jsonschema

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[1]
    / "contracts" / "executor-config" / "v0-provisional" / "schema.json"
)


def test_pinned_schema_is_valid_json_schema() -> None:
    schema = json.loads(_SCHEMA_PATH.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)


def test_pinned_schema_accepts_a_valid_extra_executor_config() -> None:
    schema = json.loads(_SCHEMA_PATH.read_text())
    candidate = {
        "executor": {
            "personas": {
                "reviewer": {
                    "system_prompt": "You are a strict reviewer.",
                    "model": "claude-opus-4-8",
                    "focus": ["security", "correctness"],
                }
            },
            "hooks": {
                "post_done": {"review_parallel": True, "review_roles": ["quality"]},
                "pre_start": {"sync_deps": False},
            },
            "telegram_bot_token": "123:abc",
            "budget_usd": 50.0,
        }
    }
    jsonschema.Draft202012Validator(schema).validate(candidate)


def test_pinned_schema_rejects_unknown_key_typo() -> None:
    schema = json.loads(_SCHEMA_PATH.read_text())
    candidate = {"executor": {"telegrm_bot_token": "oops"}}  # typo
    errors = list(jsonschema.Draft202012Validator(schema).iter_errors(candidate))
    assert errors
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv add --dev jsonschema pytest 2>/dev/null; uv add jsonschema; uv run pytest tests/test_spec_runner_config_schema.py -v`
Expected: FAIL — `contracts/executor-config/v0-provisional/schema.json` does not exist (`FileNotFoundError`).

- [ ] **Step 3: Write the schema file**

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://dispatcher.local/contracts/executor-config/v0-provisional/schema.json",
  "title": "spec-runner ExecutorConfig — extra_executor_config overlay (provisional)",
  "$comment": "source: spec-runner@72db9f5, hand-derived from ExecutorConfig/Persona in spec-runner/src/spec_runner/config.py — no upstream contracts/schemas/executor-config.schema.json yet (handoff H-4, see docs/superpowers/specs/2026-07-17-spec-runner-config-editor-design.md). Covers only the fields carried through Maestro's extra_executor_config overlay (maestro/models.py:1152, commit 0122942) — typed SpecRunnerConfig fields are validated separately in dispatcher/core/spec_runner_config_schema.py::validate_typed_fields.",
  "$defs": {
    "Persona": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "system_prompt": {"type": "string"},
        "model": {"type": "string"},
        "focus": {"type": "array", "items": {"type": "string"}}
      }
    }
  },
  "type": "object",
  "required": ["executor"],
  "additionalProperties": false,
  "properties": {
    "executor": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "personas": {
          "type": "object",
          "additionalProperties": {"$ref": "#/$defs/Persona"}
        },
        "telegram_bot_token": {"type": "string"},
        "telegram_chat_id": {"type": "string"},
        "webhook_url": {"type": "string"},
        "webhook_method": {"type": "string"},
        "webhook_headers": {
          "type": "object",
          "additionalProperties": {"type": "string"}
        },
        "webhook_template": {"type": "string"},
        "budget_usd": {"type": ["number", "null"]},
        "task_budget_usd": {"type": ["number", "null"]},
        "max_retry_cost_usd": {"type": ["number", "null"]},
        "integration_pr": {"type": "boolean"},
        "main_branch": {"type": "string"},
        "hooks": {
          "type": "object",
          "additionalProperties": false,
          "properties": {
            "pre_start": {
              "type": "object",
              "additionalProperties": false,
              "properties": {
                "sync_deps": {"type": "boolean"}
              }
            },
            "post_done": {
              "type": "object",
              "additionalProperties": false,
              "properties": {
                "review_parallel": {"type": "boolean"},
                "review_roles": {"type": "array", "items": {"type": "string"}},
                "lint_blocking": {"type": "boolean"}
              }
            }
          }
        }
      }
    }
  }
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_spec_runner_config_schema.py -v`
Expected: 3 passed.

- [ ] **Step 5: Compute the pin hash and write the provenance README**

Run: `shasum -a 256 contracts/executor-config/v0-provisional/schema.json`

Take the printed hash and write it into the table below.

```markdown
# Vendored pin — spec-runner executor-config schema (provisional)

> Source: hand-derived from `spec-runner/src/spec_runner/config.py`
> (`ExecutorConfig`, `Persona`) @ `72db9f5` — **no upstream machine-readable
> contract exists yet.** spec-runner ships `schemas/*.schema.json` for other
> artifacts (json-result, costs, doctor-result, executor-state, status) but
> not this one. Provisional per
> `docs/superpowers/specs/2026-07-17-spec-runner-config-editor-design.md`
> DESIGN-301; promote to a real vendored copy once handoff **H-4** lands
> (spec-runner publishes `schemas/executor-config.schema.json`).
> Do not treat as authoritative — re-derive by hand if `ExecutorConfig` changes.

Covers only the `extra_executor_config` overlay fields (personas, review
parallelism, telegram/webhook, budgets, `integration_pr`/`main_branch`,
remaining hook flags). The fields already mirrored as typed
`SpecRunnerConfig` fields on the Maestro side (`maestro/models.py:1152`,
commit `0122942`) are validated separately, in
`dispatcher/core/spec_runner_config_schema.py::validate_typed_fields`.

| file | sha256 |
|---|---|
| `schema.json` | `<paste the hash from Step 5 here>` |
```

- [ ] **Step 6: Commit**

```bash
git add contracts/executor-config/v0-provisional/ tests/test_spec_runner_config_schema.py pyproject.toml uv.lock
git commit -m "feat: pin provisional executor-config schema (DESIGN-301)"
```

---

### Task 2: Read-model (`dispatcher/core/spec_runner_config.py`)

**Files:**
- Create: `dispatcher/core/spec_runner_config.py`
- Test: `tests/test_spec_runner_config.py`

**Interfaces:**
- Consumes: `dispatcher.core.collectors.base.read_yaml(path: Path) -> dict[str, Any]`, `SourceReadError` (both already exist, `dispatcher/core/collectors/base.py:198,30`).
- Produces (used by Tasks 3, 4, 5):
  - `TYPED_DEFAULTS: dict[str, Any]` — the 12 typed field names → their Maestro-side defaults.
  - `TYPED_FIELDS: tuple[str, ...]` — `tuple(TYPED_DEFAULTS)`.
  - `class TypedField(BaseModel)` — `value: Any`, `explicit: bool`.
  - `class ProjectSpecRunnerConfig(BaseModel)` — `project: str`, `project_yaml_path: str`, `typed: dict[str, TypedField]`, `extra_executor_config: dict[str, Any]`, `extra_explicit: bool`.
  - `effective_executor_config(cfg: ProjectSpecRunnerConfig) -> dict[str, Any]` — the `{"executor": {...}}` shape spec-runner would actually see.
  - `discover_project_configs(roots: tuple[Path, ...]) -> tuple[list[ProjectSpecRunnerConfig], list[str]]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_spec_runner_config.py
from pathlib import Path

from dispatcher.core.spec_runner_config import (
    ProjectSpecRunnerConfig,
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_spec_runner_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dispatcher.core.spec_runner_config'`.

- [ ] **Step 3: Write the implementation**

```python
# dispatcher/core/spec_runner_config.py
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
        typed=typed,
        extra_executor_config=raw.get("extra_executor_config") or {},
        extra_explicit="extra_executor_config" in raw,
    )


def discover_project_configs(
    roots: tuple[Path, ...],
) -> tuple[list[ProjectSpecRunnerConfig], list[str]]:
    """Scan `roots` for `<child>/project.yaml` files with a `spec_runner:` block."""
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_spec_runner_config.py -v`
Expected: 4 passed.

- [ ] **Step 5: Type-check**

Run: `uv run pyrefly check`
Expected: no new errors from `dispatcher/core/spec_runner_config.py`.

- [ ] **Step 6: Commit**

```bash
git add dispatcher/core/spec_runner_config.py tests/test_spec_runner_config.py
git commit -m "feat: spec-runner config read-model (DESIGN-302)"
```

---

### Task 3: Validation (`dispatcher/core/spec_runner_config_schema.py`)

**Files:**
- Create: `dispatcher/core/spec_runner_config_schema.py`
- Test: extend `tests/test_spec_runner_config_schema.py`

**Interfaces:**
- Consumes: `dispatcher.core.spec_runner_config.TYPED_DEFAULTS` (Task 2), the pinned schema file (Task 1), `jsonschema.Draft202012Validator`.
- Produces (used by Task 4): `class ConfigValidationError(Exception)` with `.errors: list[str]`; `validate_typed_fields(candidate: dict[str, Any]) -> list[str]`; `validate_extra_executor_config(extra: dict[str, Any]) -> list[str]`; `validate_candidate(typed: dict[str, Any], extra: dict[str, Any]) -> None` (raises `ConfigValidationError`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_spec_runner_config_schema.py`:

```python
from dispatcher.core.spec_runner_config_schema import (
    ConfigValidationError,
    validate_candidate,
    validate_extra_executor_config,
    validate_typed_fields,
)


def test_validate_typed_fields_accepts_known_values() -> None:
    assert validate_typed_fields({"max_retries": 5, "claude_model": "x"}) == []


def test_validate_typed_fields_rejects_unknown_key() -> None:
    errors = validate_typed_fields({"totally_made_up": 1})
    assert any("not a known typed field" in e for e in errors)


def test_validate_typed_fields_rejects_wrong_type() -> None:
    errors = validate_typed_fields({"max_retries": "five"})
    assert any("expected int" in e for e in errors)


def test_validate_extra_executor_config_accepts_valid_shape() -> None:
    extra = {"executor": {"telegram_bot_token": "123:abc"}}
    assert validate_extra_executor_config(extra) == []


def test_validate_extra_executor_config_rejects_typo() -> None:
    extra = {"executor": {"telegrm_bot_token": "oops"}}
    assert validate_extra_executor_config(extra) != []


def test_validate_candidate_aggregates_both_tiers() -> None:
    with pytest.raises(ConfigValidationError) as exc_info:
        validate_candidate(
            {"max_retries": "five"}, {"executor": {"telegrm_bot_token": "oops"}}
        )
    assert len(exc_info.value.errors) == 2
```

Add `import pytest` to the top of `tests/test_spec_runner_config_schema.py` if not already present.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_spec_runner_config_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dispatcher.core.spec_runner_config_schema'`.

- [ ] **Step 3: Write the implementation**

```python
# dispatcher/core/spec_runner_config_schema.py
"""Validates spec_runner: candidates before any diff/PR exists (DESIGN-303).

Two independent tiers, matching the spec exactly:
- typed fields: type-checked against Maestro's own SpecRunnerConfig field
  types (mirrored in spec_runner_config.TYPED_DEFAULTS — Maestro validates
  these itself once written, but we reject obviously-wrong values before
  ever building a diff).
- extra_executor_config: validated against the pinned provisional schema,
  since Maestro's own model does not (and by design will not) type-check
  this dict — a malformed key here fails silently at Maestro's next run
  otherwise.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import jsonschema

from dispatcher.core.spec_runner_config import TYPED_DEFAULTS

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "executor-config"
    / "v0-provisional"
    / "schema.json"
)

_TYPED_TYPES: dict[str, type] = {
    "max_retries": int,
    "task_timeout_minutes": int,
    "claude_command": str,
    "auto_commit": bool,
    "create_git_branch": bool,
    "run_tests_on_done": bool,
    "test_command": str,
    "run_lint_on_done": bool,
    "lint_command": str,
    "claude_model": str,
    "review_command": str,
    "review_model": str,
}


class ConfigValidationError(Exception):
    """Candidate spec_runner: block fails validation (either tier)."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


@lru_cache(maxsize=1)
def _schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text())


def validate_typed_fields(candidate: dict[str, Any]) -> list[str]:
    """Type-check candidate typed fields; reject unknown keys (typo guard)."""
    errors: list[str] = []
    for key, value in candidate.items():
        if key not in TYPED_DEFAULTS:
            errors.append(f"typed.{key}: not a known typed field")
            continue
        expected = _TYPED_TYPES[key]
        if not isinstance(value, expected):
            errors.append(
                f"typed.{key}: expected {expected.__name__}, "
                f"got {type(value).__name__}"
            )
    return errors


def validate_extra_executor_config(extra: dict[str, Any]) -> list[str]:
    """Validate the extra_executor_config overlay against the pinned schema."""
    if not extra:
        return []
    validator = jsonschema.Draft202012Validator(_schema())
    return [
        f"extra_executor_config.{'.'.join(str(p) for p in err.path) or '<root>'}: "
        f"{err.message}"
        for err in sorted(validator.iter_errors(extra), key=lambda e: list(e.path))
    ]


def validate_candidate(typed: dict[str, Any], extra: dict[str, Any]) -> None:
    """Raise ConfigValidationError if either tier fails."""
    errors = validate_typed_fields(typed) + validate_extra_executor_config(extra)
    if errors:
        raise ConfigValidationError(errors)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_spec_runner_config_schema.py -v`
Expected: 9 passed (3 from Task 1 + 6 new).

- [ ] **Step 5: Type-check and lint**

Run: `uv run pyrefly check && uv run ruff check dispatcher/core/spec_runner_config_schema.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add dispatcher/core/spec_runner_config_schema.py tests/test_spec_runner_config_schema.py
git commit -m "feat: two-tier validation for spec-runner config candidates (DESIGN-303)"
```

---

### Task 4: Action runner (`dispatcher/core/spec_runner_config_actions.py`)

**Files:**
- Create: `dispatcher/core/spec_runner_config_actions.py`
- Test: `tests/test_spec_runner_config_actions.py`
- Modify: `pyproject.toml` (add `ruamel.yaml`)

**Interfaces:**
- Consumes: `dispatcher.core.actions.ActionOutcome` (existing, `dispatcher/core/actions.py:31`, reused as-is — it is a plain pydantic DTO, not the `ActionRunner` class, so reusing it does not violate the "don't mix with the existing ActionRunner" constraint); `dispatcher.core.discovery.DispatcherConfig`; `dispatcher.core.spec_runner_config.TYPED_FIELDS`; `dispatcher.core.spec_runner_config_schema.validate_candidate`, `ConfigValidationError`.
- Produces (used by Task 5): `class ConfigCandidate(BaseModel)` — `typed: dict[str, Any]`, `extra_executor_config: dict[str, Any] = {}`, `base_mtime: float`; `class SpecRunnerConfigRejectedError(Exception)`; `class SpecRunnerConfigBusyError(Exception)`; `class SpecRunnerConfigConflictError(Exception)`; `class SpecRunnerConfigActionRunner` with `__init__(self, config: DispatcherConfig, *, command: tuple[str, ...] = ("github-checker",))` and `.run(self, repo_dir: str, candidate: ConfigCandidate) -> ActionOutcome`.

- [ ] **Step 1: Add the `ruamel.yaml` dependency**

Run: `uv add ruamel.yaml`
Expected: `pyproject.toml` and `uv.lock` updated; command exits 0.

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_spec_runner_config_actions.py
import subprocess
import threading
from pathlib import Path

import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.spec_runner_config_actions import (
    ConfigCandidate,
    SpecRunnerConfigActionRunner,
    SpecRunnerConfigBusyError,
    SpecRunnerConfigConflictError,
    SpecRunnerConfigRejectedError,
)

_PROJECT_YAML = """
project: alpha
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
  claude_model: ""
  review_command: ""
  review_model: ""
workstreams: []
"""


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )


def make_project(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    (repo / "project.yaml").write_text(_PROJECT_YAML)
    return repo


def fake_checker(tmp_path: Path, payload: dict) -> tuple[str, ...]:
    script = tmp_path / "fake_checker.py"
    script.write_text(f"import sys, json; json.dump({payload!r}, sys.stdout)")
    return ("python3", str(script))


def _candidate(repo: Path, **typed_overrides) -> ConfigCandidate:
    from dispatcher.core.spec_runner_config import TYPED_DEFAULTS

    typed = {**TYPED_DEFAULTS, **typed_overrides}
    mtime = (repo / "project.yaml").stat().st_mtime
    return ConfigCandidate(typed=typed, base_mtime=mtime)


def test_run_rejects_invalid_typed_field_before_touching_disk(tmp_path: Path) -> None:
    repo = make_project(tmp_path, "alpha")
    original = (repo / "project.yaml").read_text()
    runner = SpecRunnerConfigActionRunner(DispatcherConfig(roots=(tmp_path,)))
    candidate = _candidate(repo, max_retries="not-an-int")
    with pytest.raises(SpecRunnerConfigRejectedError):
        runner.run("alpha", candidate)
    assert (repo / "project.yaml").read_text() == original


def test_run_rejects_stale_mtime(tmp_path: Path) -> None:
    repo = make_project(tmp_path, "alpha")
    runner = SpecRunnerConfigActionRunner(DispatcherConfig(roots=(tmp_path,)))
    candidate = _candidate(repo)
    (repo / "project.yaml").write_text(_PROJECT_YAML + "\n# touched\n")
    with pytest.raises(SpecRunnerConfigConflictError):
        runner.run("alpha", candidate)


def test_run_writes_diff_and_delegates_to_github_checker(tmp_path: Path) -> None:
    repo = make_project(tmp_path, "alpha")
    payload = {"ok": True, "detail": "opened", "pr_url": "https://example/pr/1"}
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    candidate = _candidate(repo, max_retries=7, claude_model="claude-opus-4-8")
    outcome = runner.run("alpha", candidate)
    assert outcome.ok
    assert outcome.pr_url == "https://example/pr/1"
    written = (repo / "project.yaml").read_text()
    assert "max_retries: 7" in written
    assert "claude-opus-4-8" in written
    assert "workstreams" in written  # rest of the file survives


def test_one_in_flight_per_repo(tmp_path: Path, monkeypatch) -> None:
    repo = make_project(tmp_path, "alpha")
    runner = SpecRunnerConfigActionRunner(DispatcherConfig(roots=(tmp_path,)))
    started = threading.Event()
    release = threading.Event()

    def slow_invoke(repo_dir):
        started.set()
        release.wait(timeout=10)
        from dispatcher.core.actions import ActionOutcome

        return ActionOutcome(action="update-spec-runner-config", dir=repo_dir, ok=True)

    monkeypatch.setattr(runner, "_invoke", slow_invoke)
    candidate = _candidate(repo)
    thread = threading.Thread(target=runner.run, args=("alpha", candidate))
    thread.start()
    assert started.wait(timeout=2)
    with pytest.raises(SpecRunnerConfigBusyError):
        runner.run("alpha", candidate)
    release.set()
    thread.join(timeout=2)


def test_audit_line_written(tmp_path: Path, caplog) -> None:
    repo = make_project(tmp_path, "alpha")
    payload = {"ok": True, "detail": "opened"}
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    candidate = _candidate(repo)
    with caplog.at_level("INFO", logger="dispatcher.actions.spec_runner_config"):
        runner.run("alpha", candidate)
    assert any(
        "action=update-spec-runner-config" in r.getMessage() and "repo=alpha" in r.getMessage()
        for r in caplog.records
    )
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_spec_runner_config_actions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dispatcher.core.spec_runner_config_actions'`.

- [ ] **Step 4: Write the implementation**

```python
# dispatcher/core/spec_runner_config_actions.py
"""Content-PR action: update-spec-runner-config (DESIGN-304, resolves X-02).

Deliberately NOT `core/actions.py`'s `ActionRunner` — this runner produces
file *content* (a diff limited to one project.yaml's `spec_runner:` block)
before delegating branch/commit/push/PR to github-checker, a different
mutation shape than the pure git-plumbing sync actions (pull/create-pr).
Own lock, own audit logger, so the two action classes stay independently
testable and reasoned about (explicit stakeholder requirement, spec §1).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from io import StringIO
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from ruamel.yaml import YAML

from dispatcher.core.actions import ActionOutcome
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.spec_runner_config import TYPED_FIELDS
from dispatcher.core.spec_runner_config_schema import (
    ConfigValidationError,
    validate_candidate,
)

_ACTION_TIMEOUT = 120
_SAFE_DIR_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")
_audit = logging.getLogger("dispatcher.actions.spec_runner_config")


class ConfigCandidate(BaseModel):
    """A proposed spec_runner: block, as submitted by the editor UI."""

    typed: dict[str, Any]
    extra_executor_config: dict[str, Any] = {}
    base_mtime: float  # project.yaml's mtime when the form was rendered


class SpecRunnerConfigRejectedError(Exception):
    """Bad target or invalid candidate (API turns this into 422)."""


class SpecRunnerConfigBusyError(Exception):
    """This repo's project.yaml already has an update in flight (-> 409)."""


class SpecRunnerConfigConflictError(Exception):
    """project.yaml changed on disk since the form was rendered (-> 409)."""


def build_new_yaml_text(project_yaml: Path, candidate: ConfigCandidate) -> str:
    """Render `project.yaml` with only its `spec_runner:` key replaced.

    Uses ruamel.yaml's round-trip mode so comments, key order, and block
    literals elsewhere in the file (e.g. `workstreams:`) are preserved —
    plain PyYAML load+dump would rewrite the whole file and violate the
    "only the spec_runner: block changes" constraint.

    `YAML()` defaults to `typ="rt"` (round-trip) — as safe as
    `yaml.safe_load()`, no arbitrary object construction. Never pass
    `typ="unsafe"` here.
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    with project_yaml.open() as fh:
        doc = yaml.load(fh)
    new_block: dict[str, Any] = dict(candidate.typed)
    if candidate.extra_executor_config:
        new_block["extra_executor_config"] = candidate.extra_executor_config
    doc["spec_runner"] = new_block
    buf = StringIO()
    yaml.dump(doc, buf)
    return buf.getvalue()


class SpecRunnerConfigActionRunner:
    """Serialized executor of the update-spec-runner-config action."""

    def __init__(
        self,
        config: DispatcherConfig,
        *,
        command: tuple[str, ...] = ("github-checker",),
    ) -> None:
        self._config = config
        self._command = command
        self._lock = threading.Lock()
        self._busy: set[str] = set()

    def _target(self, repo_dir: str) -> Path:
        if not _SAFE_DIR_RE.fullmatch(repo_dir) or repo_dir in (".", ".."):
            raise SpecRunnerConfigRejectedError(f"unsafe repo dir: {repo_dir!r}")
        workspace = next((r for r in self._config.roots if r.is_dir()), None)
        if workspace is None:
            raise SpecRunnerConfigRejectedError("no existing workspace root configured")
        project_yaml = workspace / repo_dir / "project.yaml"
        if not project_yaml.is_file():
            raise SpecRunnerConfigRejectedError(f"no project.yaml in: {repo_dir}")
        return project_yaml

    def run(self, repo_dir: str, candidate: ConfigCandidate) -> ActionOutcome:
        """Validate, diff, write, and hand off to github-checker. Always audits."""
        try:
            unknown = set(candidate.typed) - set(TYPED_FIELDS)
            if unknown:
                raise SpecRunnerConfigRejectedError(f"unknown typed field(s): {unknown}")
            validate_candidate(candidate.typed, candidate.extra_executor_config)
            project_yaml = self._target(repo_dir)
            if project_yaml.stat().st_mtime != candidate.base_mtime:
                raise SpecRunnerConfigConflictError(
                    f"{repo_dir}: project.yaml changed since the form was loaded"
                )
            with self._lock:
                if repo_dir in self._busy:
                    raise SpecRunnerConfigBusyError(f"{repo_dir}: update already in flight")
                self._busy.add(repo_dir)
        except (
            SpecRunnerConfigRejectedError,
            SpecRunnerConfigConflictError,
            SpecRunnerConfigBusyError,
            ConfigValidationError,
        ) as err:
            _audit.info(
                "action=update-spec-runner-config repo=%s ok=False rejected=%s",
                repo_dir,
                err,
            )
            raise
        try:
            new_text = build_new_yaml_text(project_yaml, candidate)
            project_yaml.write_text(new_text)
            outcome = self._invoke(repo_dir)
        finally:
            with self._lock:
                self._busy.discard(repo_dir)
        _audit.info(
            "action=update-spec-runner-config repo=%s ok=%s detail=%s error=%s",
            repo_dir,
            outcome.ok,
            outcome.detail,
            outcome.error,
        )
        return outcome

    def _invoke(self, repo_dir: str) -> ActionOutcome:
        workspace = next(r for r in self._config.roots if r.is_dir())
        target = workspace / repo_dir
        argv = [*self._command, "open-pr", str(target)]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=_ACTION_TIMEOUT
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as err:
            return ActionOutcome(
                action="update-spec-runner-config",
                dir=target.name,
                ok=False,
                error=str(err),
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return ActionOutcome(
                action="update-spec-runner-config",
                dir=target.name,
                ok=False,
                error=proc.stderr.strip() or "github-checker returned no JSON",
            )
        return ActionOutcome(
            action="update-spec-runner-config",
            dir=target.name,
            ok=bool(data.get("ok")),
            detail=data.get("detail"),
            error=data.get("error"),
            pr_url=data.get("pr_url"),
        )
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_spec_runner_config_actions.py -v`
Expected: 5 passed.

- [ ] **Step 6: Type-check and lint**

Run: `uv run pyrefly check && uv run ruff check dispatcher/core/spec_runner_config_actions.py`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add dispatcher/core/spec_runner_config_actions.py tests/test_spec_runner_config_actions.py pyproject.toml uv.lock
git commit -m "feat: update-spec-runner-config content-PR action (DESIGN-304)"
```

---

### Task 5: API (`dispatcher/server/app.py`)

**Files:**
- Modify: `dispatcher/server/app.py`
- Test: extend `tests/test_api.py`

**Interfaces:**
- Consumes: `dispatcher.core.spec_runner_config.{discover_project_configs, ProjectSpecRunnerConfig}` (Task 2), `dispatcher.core.spec_runner_config_actions.{SpecRunnerConfigActionRunner, ConfigCandidate, SpecRunnerConfigRejectedError, SpecRunnerConfigBusyError, SpecRunnerConfigConflictError}` (Task 4), `dispatcher.core.spec_runner_config_schema.ConfigValidationError` (Task 3).
- Produces: `GET /api/projects/{name}/spec-runner-config` -> `ProjectSpecRunnerConfig | 404`; `POST /api/actions/update-spec-runner-config` -> `ActionOutcome | 403 | 404 | 409 | 422` (same `X-Action-Token` CSRF header as the existing `/api/actions/*` routes).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api.py` (match the existing httpx/ASGITransport fixture pattern already used in that file — check its top for the `client`/`app` fixture name before writing these, since this plan assumes a fixture called `client` built from `create_app(config)` over a `tmp_path` workspace; adjust the fixture reference to whatever this file already uses):

```python
def test_spec_runner_config_view_and_update(tmp_path, monkeypatch):
    import subprocess

    from dispatcher.core.discovery import DispatcherConfig
    from dispatcher.server.app import create_app
    from fastapi.testclient import TestClient

    repo = tmp_path / "alpha"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "project.yaml").write_text(
        "project: alpha\nspec_runner:\n  max_retries: 3\nworkstreams: []\n"
    )
    config = DispatcherConfig(roots=(tmp_path,))
    client = TestClient(create_app(config))

    view = client.get("/api/projects/alpha/spec-runner-config")
    assert view.status_code == 200
    assert view.json()["typed"]["max_retries"]["value"] == 3

    missing = client.get("/api/projects/no-such-project/spec-runner-config")
    assert missing.status_code == 404

    token = client.get("/api/actions/session").json()["token"]
    base_mtime = (repo / "project.yaml").stat().st_mtime
    resp = client.post(
        "/api/actions/update-spec-runner-config",
        headers={"X-Action-Token": token},
        json={
            "dir": "alpha",
            "typed": {"max_retries": 9},
            "extra_executor_config": {},
            "base_mtime": base_mtime,
        },
    )
    # github-checker isn't installed in the test env — expect a failed
    # ActionOutcome (200 with ok=False), not a 5xx: the write itself must
    # succeed even when the PR-creation subprocess can't run.
    assert resp.status_code == 200
    assert resp.json()["ok"] is False
    assert "max_retries: 9" in (repo / "project.yaml").read_text()

    bad_token = client.post(
        "/api/actions/update-spec-runner-config",
        headers={"X-Action-Token": "wrong"},
        json={"dir": "alpha", "typed": {}, "extra_executor_config": {}, "base_mtime": 0},
    )
    assert bad_token.status_code == 403
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_api.py -k spec_runner_config -v`
Expected: FAIL — `404` route not registered (or `AttributeError`), since the endpoints don't exist yet.

- [ ] **Step 3: Wire the endpoints**

In `dispatcher/server/app.py`, add to the import block:

```python
from dispatcher.core.spec_runner_config import (
    ProjectSpecRunnerConfig,
    discover_project_configs,
)
from dispatcher.core.spec_runner_config_actions import (
    ConfigCandidate,
    SpecRunnerConfigActionRunner,
    SpecRunnerConfigBusyError,
    SpecRunnerConfigConflictError,
    SpecRunnerConfigRejectedError,
)
from dispatcher.core.spec_runner_config_schema import ConfigValidationError
```

Inside `create_app`, alongside the existing `actions = ActionRunner(config)` line, add:

```python
    spec_runner_config_actions = SpecRunnerConfigActionRunner(config)
```

Add a new request model near `ActionRequest`:

```python
class UpdateSpecRunnerConfigRequest(BaseModel):
    """POST /api/actions/update-spec-runner-config body."""

    dir: str
    typed: dict[str, Any]
    extra_executor_config: dict[str, Any] = {}
    base_mtime: float
```

Add the two routes (after the existing `action_create_pr`, before `app.mount(...)`):

```python
    @app.get(
        "/api/projects/{name}/spec-runner-config",
        response_model=ProjectSpecRunnerConfig,
    )
    def spec_runner_config_view(name: str) -> ProjectSpecRunnerConfig:
        configs, _ = discover_project_configs(config.roots)
        for cfg in configs:
            if Path(cfg.project_yaml_path).parent.name == name:
                return cfg
        raise HTTPException(status_code=404, detail=f"no project.yaml for: {name}")

    @app.post(
        "/api/actions/update-spec-runner-config", response_model=ActionOutcome
    )
    def action_update_spec_runner_config(
        request: UpdateSpecRunnerConfigRequest,
        x_action_token: str | None = Header(default=None),
    ) -> ActionOutcome:
        """Явный клик человека: PR в spec_runner: блок project.yaml (DESIGN-304)."""
        if x_action_token != action_token:
            raise HTTPException(status_code=403, detail="bad or missing action token")
        candidate = ConfigCandidate(
            typed=request.typed,
            extra_executor_config=request.extra_executor_config,
            base_mtime=request.base_mtime,
        )
        try:
            return spec_runner_config_actions.run(request.dir.strip(), candidate)
        except (SpecRunnerConfigRejectedError, ConfigValidationError) as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        except (SpecRunnerConfigBusyError, SpecRunnerConfigConflictError) as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
```

`Path` and `Any` are already imported at the top of `app.py` (`from pathlib import Path`, `from typing import Any`) — no new imports needed for those.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_api.py -k spec_runner_config -v`
Expected: 1 passed.

- [ ] **Step 5: Run the full test suite**

Run: `uv run pytest`
Expected: all tests pass (no regressions in existing `/api/actions/*` routes).

- [ ] **Step 6: Type-check and lint**

Run: `uv run pyrefly check && uv run ruff check dispatcher/server/app.py`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add dispatcher/server/app.py tests/test_api.py
git commit -m "feat: spec-runner config API routes (DESIGN-305)"
```

---

### Task 6: Web UI (`dispatcher/server/static/index.html`)

**Files:**
- Modify: `dispatcher/server/static/index.html`

**Interfaces:**
- Consumes: `GET /api/projects/{name}/spec-runner-config`, `POST /api/actions/update-spec-runner-config`, the existing `ensureActionToken()` helper (`index.html:296`) and `esc()` helper (`index.html:140`).
- Produces: a config panel appended to the existing `#detail-section`, populated whenever `detail(name)` is called (project card click, `index.html:166-174`).

This task has no automated test — dispatcher's web UI has no existing JS test harness (`tests/test_api.py` covers the API only). Verify manually per Step 4.

- [ ] **Step 1: Add the config panel markup**

In `dispatcher/server/static/index.html`, inside `<section id="detail-section" hidden>` (currently ending at line 106), add a config sub-panel after the existing `<div id="detail">`:

```html
<section id="detail-section" hidden>
  <h2>Project detail <span id="detail-name"></span></h2>
  <div id="detail">click a project card…</div>
  <div id="spec-runner-config" hidden>
    <h3>Spec-runner config</h3>
    <form id="spec-runner-config-form"></form>
    <pre id="spec-runner-config-diff" hidden></pre>
    <button id="spec-runner-config-submit" type="button" disabled>Preview diff</button>
    <span id="spec-runner-config-result"></span>
  </div>
</section>
```

- [ ] **Step 2: Extend `detail()` to load and render the config panel**

Replace the `detail()` function (`index.html:430-439`) with:

```javascript
let currentSpecRunnerConfig = null;

function renderSpecRunnerConfigForm(cfg) {
  const panel = document.getElementById("spec-runner-config");
  panel.hidden = false;
  const form = document.getElementById("spec-runner-config-form");
  form.innerHTML = Object.entries(cfg.typed).map(([key, field]) => `
    <label>${esc(key)}${field.explicit ? "" : " (default)"}
      <input data-typed="${esc(key)}" value="${esc(String(field.value))}">
    </label>`).join("");
  document.getElementById("spec-runner-config-submit").disabled = false;
  document.getElementById("spec-runner-config-result").textContent = "";
}

async function detail(name) {
  const section = document.getElementById("detail-section");
  section.hidden = false;
  document.getElementById("detail-name").textContent = "— " + name;
  document.getElementById("detail").textContent = "loading…";
  const snap = await get("/api/projects/" + encodeURIComponent(name));
  document.getElementById("detail").textContent = JSON.stringify(snap, null, 2);

  const panel = document.getElementById("spec-runner-config");
  panel.hidden = true;
  currentSpecRunnerConfig = null;
  try {
    const resp = await fetch(
      "/api/projects/" + encodeURIComponent(name) + "/spec-runner-config"
    );
    if (resp.ok) {
      currentSpecRunnerConfig = await resp.json();
      currentSpecRunnerConfig.repoDir = name;
      renderSpecRunnerConfigForm(currentSpecRunnerConfig);
    }
  } catch {
    // no project.yaml for this project — leave the panel hidden
  }
  section.scrollIntoView({behavior: "smooth", block: "nearest"});
}
```

- [ ] **Step 3: Wire the submit button to preview-then-PR**

Add before the closing `</script>`:

```javascript
document.getElementById("spec-runner-config-submit").addEventListener(
  "click", async () => {
  if (!currentSpecRunnerConfig) return;
  const btn = document.getElementById("spec-runner-config-submit");
  const result = document.getElementById("spec-runner-config-result");
  const form = document.getElementById("spec-runner-config-form");
  const typed = {};
  form.querySelectorAll("input[data-typed]").forEach(input => {
    const original = currentSpecRunnerConfig.typed[input.dataset.typed].value;
    typed[input.dataset.typed] = typeof original === "boolean"
      ? input.value === "true"
      : typeof original === "number" ? Number(input.value) : input.value;
  });
  btn.disabled = true;
  result.textContent = "…";
  try {
    const resp = await fetch("/api/actions/update-spec-runner-config", {
      method: "POST",
      headers: {"Content-Type": "application/json",
                "X-Action-Token": await ensureActionToken()},
      body: JSON.stringify({
        dir: currentSpecRunnerConfig.repoDir,
        typed,
        extra_executor_config: currentSpecRunnerConfig.extra_executor_config,
        base_mtime: currentSpecRunnerConfig.base_mtime,
      }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail ?? `HTTP ${resp.status}`);
    result.textContent = data.ok
      ? "✓ PR: " + (data.pr_url ?? data.detail ?? "opened")
      : "✗ " + (data.error ?? "failed");
    result.className = data.ok ? "ok" : "err";
  } catch (err) {
    result.textContent = "✗ " + err;
    result.className = "err";
  } finally {
    btn.disabled = false;
  }
});
```

Note: `currentSpecRunnerConfig.base_mtime` is referenced here but the API's `ProjectSpecRunnerConfig` model (Task 2) does not currently expose a `base_mtime` field — only `project_yaml_path`. Before this step, add a `base_mtime: float` field to `ProjectSpecRunnerConfig` in `dispatcher/core/spec_runner_config.py` (Task 2's model) and populate it in `read_project_spec_runner_config()` with `project_yaml.stat().st_mtime`, then add one assertion for it in `tests/test_spec_runner_config.py::test_read_typed_fields_and_explicit_flags`. Re-run `uv run pytest tests/test_spec_runner_config.py tests/test_api.py -v` after this addition to confirm nothing regresses before continuing.

- [ ] **Step 4: Manual verification**

Run: `uv run dispatcher serve` (or point `--config` at a `dispatcher.toml` whose `roots` include a directory containing at least one `project.yaml`, e.g. `roots = ["/Users/Andrei_Shtanakov/labs/all_ai_orchestrators"]` to pick up the real `steward/project.yaml`).

In a browser at `http://127.0.0.1:8787`: click the `steward` project card, confirm the "Spec-runner config" panel appears below the raw JSON detail with one input per typed field, click "Preview diff", confirm a result line appears (it will read `✗ ...` in a dev environment without `github-checker` on `PATH` — that failure is expected and still proves the write-then-invoke path ran; open `steward/project.yaml` on disk afterward and confirm only the `spec_runner:` block changed, then `git -C ../steward diff` to visually confirm the block-scoped diff before discarding it with `git -C ../steward checkout -- project.yaml`).

- [ ] **Step 5: Commit**

```bash
git add dispatcher/server/static/index.html dispatcher/core/spec_runner_config.py tests/test_spec_runner_config.py
git commit -m "feat: spec-runner config editor panel in the web UI (DESIGN-306)"
```

---

### Task 7: Documentation updates (spec §6)

**Files:**
- Modify: `CLAUDE.md`
- Modify: `spec/discovery-brief-customer.md`
- Modify: `spec/discovery-brief-engineer.md`
- Modify: `docs/superpowers/specs/2026-07-14-sync-roadmap-design.md`
- Modify: `dispatcher/core/actions.py` (docstring only)

**Interfaces:** none — this task only touches prose/docstrings, no runtime behavior changes. No test.

- [ ] **Step 1: Update `CLAUDE.md`**

Find the line(s) describing neighbors as read-only/never-edit (near the top, "Соседи (READ-ONLY reference)"). Add a clarifying sentence directly after it:

```markdown
- Это правило о **разработке** (сессии Claude Code не редактируют файлы соседей
  напрямую). Отдельно от этого, у dispatcher есть свой собственный, узкий
  whitelist рантайм-мутаций (`core/actions.py`, `core/spec_runner_config_actions.py`):
  запущенное приложение может открывать PR в наблюдаемые репо только по явному
  клику человека, никогда — от имени coding-сессии. См. X-02,
  `docs/superpowers/specs/2026-07-17-spec-runner-config-editor-design.md`.
```

- [ ] **Step 2: Append X-02 to `spec/discovery-brief-customer.md`**

Find the `## Stakeholder Conflicts` section (contains `X-01`). Append:

```markdown
- **X-02** `status: resolved` · `target: FR-04` — расширение `NFR-01`
  («управляемые мутации») до нового класса действий: content-PR
  (`update-spec-runner-config`), где dispatcher сам формирует diff одного
  блока `project.yaml`, а не только делегирует git-плюмбинг. Решение
  product-владельца в сессии 2026-07-17: разрешить, при условии — только
  явный клик, только PR (никогда merge/push в default branch), только
  блок `spec_runner:`, schema-валидация до диффа. Дизайн:
  `docs/superpowers/specs/2026-07-17-spec-runner-config-editor-design.md`.
```

Also amend the `NFR-01` line itself to add a forward reference: after its existing text, append `` (расширено X-02 для content-PR actions — см. ниже).``

- [ ] **Step 3: Update `spec/discovery-brief-engineer.md`**

Find every occurrence of "dispatcher остаётся view" or equivalent blanket read-only-invariant language. Scope it explicitly to sync actions, e.g. change:

```markdown
dispatcher остаётся view-only
```

to:

```markdown
dispatcher остаётся view-only для sync-действий (pull/create-pr, NFR-01); для
content-PR действий (X-02) см. discovery-brief-customer.md
```

(Exact line numbers depend on the file's current content — grep for `view-only` and `read-only` first: `grep -n "view-only\|read-only" spec/discovery-brief-engineer.md`, then edit each hit.)

- [ ] **Step 4: Add a forward-reference section to `docs/superpowers/specs/2026-07-14-sync-roadmap-design.md`**

Do not rewrite `DESIGN-204`'s meaning. Add a new subsection right after it (before `### DESIGN-205`):

```markdown
### DESIGN-204b: Sibling action class (forward reference)

`DESIGN-204` covers **sync actions** only (`pull`, `create-pr`, pure git
plumbing, no file content produced by dispatcher). A second, independent
action class — **content-PR actions** — was added later
(`docs/superpowers/specs/2026-07-17-spec-runner-config-editor-design.md`,
resolves X-02): dispatcher itself renders a schema-validated diff scoped to
one YAML block before handing off to `github-checker`. The two classes use
separate runners, locks, and audit loggers (`core/actions.py` vs.
`core/spec_runner_config_actions.py`) — this is a sibling, not a
replacement.
```

- [ ] **Step 5: Update `dispatcher/core/actions.py`'s module docstring**

Change:

```python
"""Live whitelist actions: pull / open-pr, delegated to github-checker (DESIGN-204).

Dispatcher never mutates observed repos itself — it shells out to the shipped
github-checker headless commands (`pull` is ff-only by construction, `open-pr`
never pushes; github-checker#8). Guards here implement the design's word:
explicit human action only, one in-flight action per repo, an audit line for
every attempt.
"""
```

to:

```python
"""Sync whitelist actions: pull / open-pr, delegated to github-checker (DESIGN-204).

This module never writes file content itself — it shells out to the shipped
github-checker headless commands (`pull` is ff-only by construction, `open-pr`
never pushes; github-checker#8). Guards here implement the design's word:
explicit human action only, one in-flight action per repo, an audit line for
every attempt.

A second, independent action class — content-PR actions, where dispatcher
itself renders a scoped diff before handing off to github-checker — lives in
`core/spec_runner_config_actions.py` (DESIGN-304, resolves X-02). The two
classes are deliberately not merged; see that module's docstring.
"""
```

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md spec/discovery-brief-customer.md spec/discovery-brief-engineer.md \
  docs/superpowers/specs/2026-07-14-sync-roadmap-design.md dispatcher/core/actions.py
git commit -m "docs: record X-02 and scope the read-only invariant to sync actions"
```

---

## Self-Review Notes

- **Spec coverage:** DESIGN-301 → Task 1. DESIGN-302 → Task 2. DESIGN-303 → Task 3. DESIGN-304 → Task 4. DESIGN-305 → Task 5. DESIGN-306 → Task 6. Spec §6 (doc updates) → Task 7. DESIGN-307/308 explicitly deferred (see Scope note at top). Spec §4's degradation matrix rows are each covered by a test: schema failure (Task 3/4 tests), stale mtime (Task 4 `test_run_rejects_stale_mtime`), one-in-flight (Task 4 `test_one_in_flight_per_repo`), github-checker failure (Task 5's API test, which runs without `github-checker` on `PATH`).
- **Placeholder scan:** none found — every step has runnable code or an exact shell command with expected output.
- **Type consistency:** `ConfigCandidate.typed`/`extra_executor_config` (Task 4) match the JSON body shape posted in Task 5's `UpdateSpecRunnerConfigRequest` and Task 6's `fetch()` call. `TYPED_DEFAULTS`/`TYPED_FIELDS` (Task 2) are the single source of truth consumed unchanged by Tasks 3, 4, and the web form (Task 6 renders `Object.entries(cfg.typed)`, matching `ProjectSpecRunnerConfig.typed: dict[str, TypedField]`).
- **Known follow-up folded into Task 6, not deferred silently:** `base_mtime` was missing from the Task 2 model as originally drafted; Task 6 Step 3 calls this out explicitly and has the engineer add it to Task 2's file before wiring the submit handler, with a regression check.
