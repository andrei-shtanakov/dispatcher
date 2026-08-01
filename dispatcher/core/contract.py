"""The strict ingestion boundary for github-checker's actions/v1 contract.

`ingest` is the *only* place raw producer stdout is turned into something
the rest of dispatcher may believe. The direction is one-way — raw JSON to
schema validation to typed model — and it stays one-way: nothing downstream
constructs :class:`ActionPayload` / :class:`CliError` / :class:`ContractError`
from unvalidated input, and nothing downstream sees the raw dict.

The schema is read from the vendored copy under
``contracts/github-checker-actions/v1/`` (Task 1); no sibling-repo path is
referenced, at import time or otherwise.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import jsonschema
from pydantic import BaseModel

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "github-checker-actions"
    / "v1"
    / "actions.schema.json"
)

_SUPPORTED_SCHEMA_VERSION = 1
_KNOWN_RESULT_KINDS = {"action", "cli_error", "contract_error"}
_MAX_SCHEMA_ERROR_LEN = 200


class ContractViolation(Exception):
    """Raw producer output that cannot be trusted as a producer answer.

    Covers transport failure (non-JSON stdout), envelope shapes this
    consumer does not recognise (unknown ``schema_version``/``result_kind``,
    a non-object root), a payload that fails schema validation, and an
    ``ok``/exit-code combination the contract forbids. Every one of these is
    a reason to refuse, never a reason to fall back to an empty or default
    result — a consumer failure must stay distinguishable from a producer
    outcome.
    """


class LocalStatus(BaseModel):
    """State of one local clone relative to its upstream (`$defs/local_status`)."""

    branch: str | None = None
    ahead: int | None = None
    behind: int | None = None
    dirty: bool
    error: str | None = None


class ActionPayload(BaseModel):
    """`result_kind=action`: one of the eight verbs answered.

    Fields are the union across all eight verb leaves; each is required or
    optional exactly as the schema leaf for the verb that was actually on
    the wire says, and ``model_validate`` is called with exactly that
    verb's validated dict — so a field a verb has no concept of is never
    *set* on this model (present-vs-null-vs-absent survives typing; see
    Task 3, which reads ``model_fields_set``).

    Nested payloads that vary per verb (``pr_detail``, ``matches``,
    ``malformed``, ``issue``) stay as validated-but-untyped dicts/lists
    here; they were already checked against the vendored schema, so a
    later typed refinement is additive, not a second validation path.
    """

    schema_version: int
    result_kind: Literal["action"]
    action: str
    dir: str
    ok: bool
    error: str | None = None
    detail: str | None = None
    local: LocalStatus | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    branch: str | None = None
    base_branch: str | None = None
    commit_sha: str | None = None
    changed_paths: list[str] | None = None
    pr_detail: dict[str, Any] | None = None
    merged: bool | None = None
    local_sync: str | None = None
    gate_failed: list[str] | None = None
    matches: list[dict[str, Any]] | None = None
    malformed: list[dict[str, Any]] | None = None
    created: bool | None = None
    issue: dict[str, Any] | None = None


class CliError(BaseModel):
    """`result_kind=cli_error`: argv was refused before any verb ran.

    ``action`` is diagnostic only here — it may name the attempted verb,
    an unknown string, or ``"unknown"`` — and must never be used to select
    an action-specific payload. Nothing in this boundary routes on it.
    """

    schema_version: int
    result_kind: Literal["cli_error"]
    action: str
    dir: str
    ok: Literal[False]
    error: str


class ContractError(BaseModel):
    """`result_kind=contract_error`: the producer refused its own wire drift.

    Not a ninth verb. ``action`` is diagnostic only, exactly as for
    :class:`CliError`, and selects nothing.
    """

    schema_version: int
    result_kind: Literal["contract_error"]
    action: str
    dir: str
    ok: Literal[False]
    error: str


Ingested = ActionPayload | CliError | ContractError


@lru_cache(maxsize=1)
def _schema_text() -> str:
    return _SCHEMA_PATH.read_text()


def _schema() -> dict[str, Any]:
    """A fresh copy of the vendored schema, parsed from a cached string.

    Not itself cached: a `dict` returned from an `lru_cache`-wrapped
    function is the *same object* on every call, so an in-place mutation
    by one caller (a test, say) would corrupt every later caller — the
    cached `_validator()` included — and `_schema_text.cache_clear()`
    would not undo it, since existing references stay mutated. Re-parsing
    a cached string is cheap and always hands back a private object.
    """
    return json.loads(_schema_text())


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(_schema())


def _describe_schema_error(error: jsonschema.ValidationError) -> str:
    """Name *what* failed without ever echoing the producer's payload.

    `error.message` interpolates the offending JSON instance verbatim —
    for this schema's shape (one `oneOf` over the three envelope variants
    at the root) a failing payload's top-level error message is the
    *entire payload*, dumped as a Python repr. A producer's free-text
    fields (`diff`, `error`, `detail`, …) can carry secrets or tokens; a
    validation failure must not become a channel that copies them into a
    log or a UI. `json_path`/`validator`/`validator_value` are schema-side
    — where the check lives and what it demands — never the instance
    being checked, so building the message from those instead is safe by
    construction, not by choosing values that happen not to leak today.
    """
    detail = f"{error.validator}={error.validator_value!r}"
    description = f"{error.json_path}: failed {detail}".replace("\n", " ")
    if len(description) > _MAX_SCHEMA_ERROR_LEN:
        description = description[: _MAX_SCHEMA_ERROR_LEN - 1] + "…"
    return description


def ingest(raw: str, *, returncode: int) -> Ingested:
    """Turn one raw github-checker actions/v1 stdout capture into `Ingested`.

    `_invoke` passes the real subprocess exit code as `returncode`; it is
    checked against the envelope, not left to a helper callers can forget
    to call. Order is explicit and matters: parse -> type-strict
    discriminator prechecks -> full schema validation -> exit-code check ->
    typed variant. Discriminators run before schema validation so an
    unknown `schema_version` reports "unknown version" rather than a wall
    of schema errors about a shape that was never ours to judge.

    Raises:
        ContractViolation: stdout is not JSON; the root is not a JSON
            object; `schema_version` is missing, not literally an `int`
            (Python's `True == 1` makes value-only checks unsafe), or not
            the one this consumer is pinned to; `result_kind` is missing,
            not a `str`, or not one of the three known variants; the
            payload fails validation against the vendored schema; or the
            exit code does not match the combination the contract defines
            for this envelope.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ContractViolation(f"stdout is not JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ContractViolation(
            "the envelope root must be a JSON object, got "
            f"{type(parsed).__name__}: an array or scalar is not an envelope"
        )

    # Type-strict, not value-loose: `type(x) is int`/`is str`, never `== 1`
    # or truthiness — `True == 1` in Python, so a value-only check would
    # let `schema_version: true` sail through.
    schema_version = parsed.get("schema_version")
    if type(schema_version) is not int or schema_version != _SUPPORTED_SCHEMA_VERSION:
        raise ContractViolation(
            f"unknown schema_version={schema_version!r}; this consumer is "
            f"pinned to v{_SUPPORTED_SCHEMA_VERSION} "
            "(contracts/github-checker-actions/v1/)"
        )

    result_kind = parsed.get("result_kind")
    if type(result_kind) is not str or result_kind not in _KNOWN_RESULT_KINDS:
        raise ContractViolation(
            f"unknown result_kind={result_kind!r}; an envelope variant this "
            "consumer cannot interpret is not an empty result"
        )

    errors = sorted(_validator().iter_errors(parsed), key=lambda e: list(e.path))
    if errors:
        raise ContractViolation(
            "payload does not match actions/v1 schema: "
            f"{_describe_schema_error(errors[0])}"
        )

    # The exit code is half the contract and is checked here, not left to
    # a separate optional helper: action+ok:true needs 0, action+ok:false
    # and both error variants need 1; anything else fails closed.
    expected_exit = 0 if (result_kind == "action" and parsed["ok"]) else 1
    if returncode != expected_exit:
        raise ContractViolation(
            f"exit code {returncode} does not match the contract for "
            f"result_kind={result_kind!r} ok={parsed.get('ok')!r} "
            f"(expected {expected_exit})"
        )

    if result_kind == "action":
        return ActionPayload.model_validate(parsed)
    if result_kind == "cli_error":
        return CliError.model_validate(parsed)
    return ContractError.model_validate(parsed)
