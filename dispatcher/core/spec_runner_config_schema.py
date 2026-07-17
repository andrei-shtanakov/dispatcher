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
