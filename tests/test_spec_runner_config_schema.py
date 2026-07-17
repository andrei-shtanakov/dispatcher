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
