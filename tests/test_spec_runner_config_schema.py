import json
from pathlib import Path

import jsonschema
import pytest

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
