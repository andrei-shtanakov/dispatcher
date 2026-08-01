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
from collections import Counter
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

import jsonschema
from pydantic import BaseModel, ConfigDict

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "github-checker-actions"
    / "v1"
    / "actions.schema.json"
)

_SUPPORTED_SCHEMA_VERSION = 1
_VARIANT_DEFS = {
    "action": "action_result",
    "cli_error": "cli_error",
    "contract_error": "contract_error",
}
_KNOWN_RESULT_KINDS = frozenset(_VARIANT_DEFS)
_MAX_SCHEMA_ERROR_LEN = 200
_MAX_RULE_VALUE_LEN = 80
_SCHEMA_ERROR_ARITY = 3
_FIELD_NAMES_ARITY = 3


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
    """State of one local clone relative to its upstream (`$defs/local_status`).

    All five fields are `required` in the schema, so none carries a
    default: a default here would be a consumer answer standing in for a
    producer fact, and `error: None` in particular reads as "the clone was
    read and was fine".
    """

    model_config = ConfigDict(extra="forbid")

    branch: str | None
    ahead: int | None
    behind: int | None
    dirty: bool
    error: str | None


class CheckRun(BaseModel):
    """One check run on a PR head (`$defs/check_run`)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    state: str


class ChangedFile(BaseModel):
    """One file in a PR's diff (`$defs/changed_file`)."""

    model_config = ConfigDict(extra="forbid")

    path: str
    additions: int
    deletions: int


class ReviewThread(BaseModel):
    """One review thread (`$defs/review_thread`).

    ``path``/``author``/``excerpt`` are optional in the schema, so they
    keep the three-state rule one level down: absent is not the same
    answer as ``null``, and ``model_fields_set`` is what tells them apart.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    is_resolved: bool
    is_outdated: bool
    path: str | None = None
    author: str | None = None
    excerpt: str | None = None


class IssueRef(BaseModel):
    """One inbox issue (`$defs/issue_ref`)."""

    model_config = ConfigDict(extra="forbid")

    number: int
    title: str
    state: Literal["open", "closed"]
    url: str
    author: str
    labels: list[str]


class PrDetail(BaseModel):
    """One PR's reviewable state (`$defs/pr_detail`).

    The ``*_truncated`` flags are load-bearing rather than cosmetic: a
    green verdict computed over a truncated check list is not a green
    verdict, so they are required by the schema and required here.
    """

    model_config = ConfigDict(extra="forbid")

    number: int
    title: str
    url: str
    state: str
    is_draft: bool
    mergeable: str
    merge_state_status: str | None = None
    head_branch: str
    head_sha: str
    base_branch: str
    review_decision: str | None = None
    checks: list[CheckRun]
    checks_truncated: bool
    files: list[ChangedFile]
    files_total: int
    files_truncated: bool
    review_threads: list[ReviewThread]
    threads_truncated: bool
    diff: str | None = None
    diff_truncated: bool
    allows_squash: bool | None = None


class ActionPayload(BaseModel):
    """`result_kind=action`: one of the eight verbs answered.

    Fields are the union across all eight verb leaves; each is required or
    optional exactly as the schema leaf for the verb that was actually on
    the wire says, and ``model_validate`` is called with exactly that
    verb's validated dict — so a field a verb has no concept of is never
    *set* on this model (present-vs-null-vs-absent survives typing; see
    Task 3, which reads ``model_fields_set``).

    The nested payloads that vary per verb (``pr_detail``, ``matches``,
    ``malformed``, ``issue``) are typed (Task 3). They were already
    checked against the vendored schema before this model is built, so
    the models are a *view* over validated data, not a second validation
    path — nothing here decides whether the producer is to be believed.

    Every optional field keeps its ``= None`` default so that absence is
    representable at all, and the default is never the answer: which of
    absent / ``null`` the producer sent is read from
    ``model_fields_set``, and ``model_validate`` is handed exactly the
    verb's own validated dict so an inapplicable field is never *set*.
    """

    schema_version: int
    result_kind: Literal["action"]
    action: str
    dir: str
    ok: bool
    # No default: the schema marks `error` required on all eight verbs, so
    # the producer always states it — and a field that is always stated
    # must not be defaultable, for the same reason `LocalStatus` has none.
    # `detail` below is genuinely per-verb optional.
    error: str | None
    detail: str | None = None
    local: LocalStatus | None = None
    pr_url: str | None = None
    pr_state: str | None = None
    branch: str | None = None
    base_branch: str | None = None
    commit_sha: str | None = None
    changed_paths: list[str] | None = None
    pr_detail: PrDetail | None = None
    merged: bool | None = None
    local_sync: str | None = None
    gate_failed: list[str] | None = None
    matches: list[IssueRef] | None = None
    malformed: list[IssueRef] | None = None
    created: bool | None = None
    issue: IssueRef | None = None


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

    This buys *recoverability*, not immutability: the schema retained
    inside the cached `_validator()` is still one shared mutable object,
    and mutating it in place still weakens every later `ingest` in the
    process. The difference is that `_validator.cache_clear()` now
    rebuilds from an uncorrupted parse and undoes it, which it could not
    do while both came from the same cached dict.
    """
    return json.loads(_schema_text())


@lru_cache(maxsize=1)
def _validator() -> jsonschema.Draft202012Validator:
    return jsonschema.Draft202012Validator(_schema())


@lru_cache(maxsize=1)
def _verb_defs() -> dict[str, str]:
    """Each verb name mapped to the `$defs` key that describes it."""
    return {
        definition["properties"]["action"]["const"]: name
        for name, definition in _schema()["$defs"].items()
        if name.startswith("verb_")
    }


@lru_cache(maxsize=len(_VARIANT_DEFS) + 8)
def _diagnostic_validator(
    result_kind: str, verb_def: str | None
) -> jsonschema.Draft202012Validator:
    """A validator for the one branch the discriminators named — for
    *diagnosis only*.

    Accept/reject stays with `_validator()` and the schema's root `oneOf`,
    which is the contract: exactly one variant must match, and that is not
    re-decided here. But a `oneOf` error names the combinator, not the
    rule — see :func:`_describe_schema_error`. The discriminators have
    already said which branch was meant, so validating against that branch
    alone yields errors that point at the leaf that actually failed.

    For `action` the same problem repeats one level down (`action_result`
    is itself a `oneOf` over the eight verbs), so the verb's `$defs` entry
    is `allOf`-ed with `action_result`'s own envelope-level constraints —
    dropping its `oneOf` and nothing else, so no constraint is lost.
    """
    schema = _schema()
    schema.pop("oneOf", None)
    if verb_def is None:
        schema["$ref"] = f"#/$defs/{_VARIANT_DEFS[result_kind]}"
        return jsonschema.Draft202012Validator(schema)
    envelope = dict(schema["$defs"][_VARIANT_DEFS[result_kind]])
    envelope.pop("oneOf", None)
    schema["allOf"] = [envelope, {"$ref": f"#/$defs/{verb_def}"}]
    return jsonschema.Draft202012Validator(schema)


def _leaf_errors(
    error: jsonschema.ValidationError,
) -> Iterator[jsonschema.ValidationError]:
    """The sub-errors that name a real rule, not the combinator above them."""
    if not error.context:
        yield error
        return
    for sub in error.context:
        yield from _leaf_errors(sub)


def _describe_schema_error(parsed: dict[str, Any], result_kind: str) -> str:
    """Name *what* failed without ever echoing the producer's payload.

    Two constraints pull against each other here, and both are load-bearing.

    *No disclosure.* `error.message` interpolates the offending JSON
    instance verbatim; for this schema's root — a bare `oneOf` over the
    three envelope variants — a failing payload's top-level message is the
    *entire payload* as a Python repr. A producer's free-text fields
    (`diff`, `error`, `detail`, …) can carry secrets, and a validation
    failure must not become a channel that copies them into a log or a UI.
    `json_path`/`validator`/`validator_value` are schema-side — where the
    check lives and what it demands, never the instance — so a message
    built only from those is safe by construction, not by today's values
    happening not to leak.

    *Actual diagnosis.* Redaction must not be achieved by saying nothing.
    Built from the root error alone this message is a **constant**: path
    always `$`, rule always `oneOf`, value always the same three `$ref`s.
    A foreign field, a mistyped `ok` and a bad nested `local.dirty` all
    read identically, and the prefix already said "failed the schema". So
    descend: re-validate against the variant the discriminator named and
    report the deepest distinct leaf rules. They are schema-side too, so
    the first constraint is untouched.
    """
    verb_def: str | None = None
    if result_kind == "action":
        action = parsed.get("action")
        verb_def = _verb_defs().get(action) if type(action) is str else None
        if verb_def is None:
            # No verb branch was ever going to match, so every leaf rule
            # they fail on describes a branch that was not meant: a payload
            # carrying exactly `pull`'s fields reads as an
            # `additionalProperties` violation against `open-pr`. The
            # discriminator is the diagnosis.
            return (
                f"$.action: {_describe_untrusted(action)} is not one of the "
                f"{len(_verb_defs())} verbs this contract defines"
            )

    leaves = [
        leaf
        for error in _diagnostic_validator(result_kind, verb_def).iter_errors(parsed)
        for leaf in _leaf_errors(error)
    ]
    # Deepest first: a rule that failed on `local.dirty` says more than one
    # that failed on the envelope around it.
    #
    # De-duplicated on the rendered text, because `required` emits one
    # error per missing property while this renders the whole missing set
    # from each of them — four dropped fields otherwise spend the entire
    # arity budget printing one sentence three times. (An earlier revision
    # removed this step as unreachable, on the strength of a survey that
    # only ever dropped a single field. Unreachable-by-the-cases-I-tried
    # is not unreachable.)
    seen: set[str] = set()
    described: list[str] = []
    for leaf in sorted(leaves, key=lambda e: (-len(e.absolute_path), e.json_path)):
        rendered = _describe_leaf(leaf)
        if rendered in seen:
            continue
        seen.add(rendered)
        described.append(rendered)
        if len(described) == _SCHEMA_ERROR_ARITY:
            break

    if not described:
        # The root `oneOf` refused while the named variant alone accepts:
        # the payload matched more than one variant. Impossible while the
        # variants are discriminated by a `const` `result_kind`, but the
        # message must not become a lie if that ever stops holding.
        return "no envelope variant matched uniquely"

    description = "; ".join(described).replace("\n", " ")
    # Reached in practice, and pinned: three empty objects in
    # `pr_detail.checks`/`files`/`review_threads` fail three different
    # `required` rules whose schema-side names run past the bound. An
    # earlier revision called this "a backstop no input reaches today" and
    # was wrong on every count — it fires, the longest is 242 not ~170,
    # and the reason it gave (long array indices) had been removed by the
    # same commit that wrote it. Prose about reachability is not evidence
    # of it; see `test_the_message_length_is_capped_on_a_payload_that_
    # reaches_it`.
    if len(description) > _MAX_SCHEMA_ERROR_LEN:
        description = description[: _MAX_SCHEMA_ERROR_LEN - 1] + "…"
    return description


def _safe_diagnosis(parsed: dict[str, Any]) -> str:
    """The diagnosis, or a fixed string if producing it fails.

    Fail-closed has to cover the instrument, and here the instrument is
    strictly decoration: the refusal was already decided by the
    authoritative root-`oneOf` check above. So its failure must not change
    what the caller sees. Evaluated inline in the `ContractViolation(...)`
    argument it would — the exception is never constructed, and a caller
    written to this module's documented `Raises:` sees a foreign
    exception instead, one whose own message can be the entire schema.

    The authoritative check is deliberately *not* wrapped this way. A
    vendored schema that cannot be loaded or referenced is a consumer
    fault, not untrustworthy producer output, and mislabelling it as
    `ContractViolation` would hide a broken install behind a message that
    blames the producer.
    """
    try:
        return _describe_schema_error(parsed, parsed["result_kind"])
    except Exception:  # noqa: BLE001 - decoration must never mask the refusal
        return "diagnosis unavailable"


def _describe_leaf(leaf: jsonschema.ValidationError) -> str:
    """One failed rule, named as specifically as disclosure allows.

    `required` and `additionalProperties` are the two keywords that catch
    the drift that actually happens — a producer adding or removing a
    field — and reporting them as bare keyword names collapses every such
    drift into one message. Both can be resolved to the field itself, from
    different sides:

    * `required` — the names live in the schema's own `required` array;
      the instance is consulted only to ask which of those schema-declared
      names are absent, so the answer is always a subset of the schema and
      never producer text.
    * `additionalProperties` — the offending keys have no schema
      provenance at all, so only their count and shape travel. A key is a
      value: `{"ghp_…": true}` puts the secret in the key.
    """
    path = _safe_path(leaf)
    if leaf.validator == "required":
        # JSON Schema fixes these shapes: `required` is an array of names,
        # and both keywords only ever apply to an object instance.
        required = cast("list[str]", leaf.validator_value)
        instance = cast("dict[str, Any]", leaf.instance)
        missing = [name for name in required if name not in instance]
        return f"{path}: missing required {_join_capped(repr(n) for n in missing)}"
    if leaf.validator == "additionalProperties":
        subschema = cast("dict[str, Any]", leaf.schema)
        instance = cast("dict[str, Any]", leaf.instance)
        declared = set(subschema.get("properties", ()))
        shapes = Counter(
            _describe_untrusted(key) for key in instance if key not in declared
        )
        total = sum(shapes.values())
        headline = _count(total, "unexpected property", "unexpected properties")
        return f"{path}: {headline}: {_join_shapes(shapes, total)}"
    rendered = repr(leaf.validator_value)
    rule = leaf.validator
    if len(rendered) <= _MAX_RULE_VALUE_LEN:
        rule = f"{rule}={rendered}"
    return f"{path}: failed {rule}"


def _join_shapes(shapes: Counter[str], total: int) -> str:
    """Shapes of producer-chosen keys, grouped and bounded.

    A key never travels, but its type and length may, and they carry the
    little that can be carried: a six-character key is a plausible field
    rename, a forty-character one is not. Identical shapes are aggregated
    so that order within a group cannot encode anything, and groups are
    ordered by count and then by the descriptor itself — both derived from
    what is already printed, never from the key text. No hash, prefix or
    suffix: each would rebuild the channel this exists to close.
    """
    groups = sorted(shapes.items(), key=lambda kv: (-kv[1], kv[0]))[:_FIELD_NAMES_ARITY]
    rendered = ", ".join(
        f"{count} × {shape}" if count > 1 else shape for shape, count in groups
    )
    covered = sum(count for _, count in groups)
    if covered < total:
        rendered = f"{rendered} and {total - covered} more"
    return rendered


def _join_capped(names: Iterator[str]) -> str:
    """At most `_FIELD_NAMES_ARITY` names, then a count of what was cut."""
    shown: list[str] = []
    for extra, name in enumerate(names):
        if extra >= _FIELD_NAMES_ARITY:
            return f"{', '.join(shown)} and more"
        shown.append(name)
    return ", ".join(shown) if shown else "nothing nameable"


def _describe_untrusted(value: object) -> str:
    """The shape of a producer-supplied value. Never its content.

    The discriminator prechecks run *before* schema validation, so what
    they report on is arbitrary: whatever JSON the producer put at that
    key, of any size and shape. An earlier design echoed values that
    looked like identifiers, so that an operator could read a drifted
    version or variant name off the log, and recorded the residual
    capacity as an accepted risk.

    That trade is withdrawn. Thirty-two characters of ``[a-z0-9_-]``
    is exactly the shape of a lowercase-hex API key, and documenting a
    leak does not make it acceptable on a trust boundary. An allowlist is
    only ever as safe as the values someone thought to try against it.

    So: no branch here returns producer bytes. That is checkable by
    reading the function rather than by enumerating attacks, which is the
    only form of "safe by construction" worth the name. The diagnosis
    that survives — type, and a size — is enough to tell a version bump
    from a fourth variant from a nested envelope.
    """
    if value is None:
        return "<null>"
    if type(value) is bool:
        return "<bool>"
    if type(value) is int:
        try:
            digits = len(repr(value))
        except ValueError:
            # CPython's int-to-str conversion limit. Rendering a value to
            # measure it is the one way this function can fail on the very
            # input it exists to make safe to mention.
            return "<int, past the conversion limit>"
        return f"<int, {_count(digits, 'digit', 'digits')}>"
    if type(value) is float:
        return "<float>"
    if type(value) is str:
        return f"<str, {_count(len(value), 'char', 'chars')}>"
    if isinstance(value, list):
        return f"<list, {_count(len(value), 'item', 'items')}>"
    if isinstance(value, dict):
        return f"<object, {_count(len(value), 'key', 'keys')}>"
    return f"<{type(value).__name__}>"


def _count(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


@lru_cache(maxsize=1)
def _declared_names() -> frozenset[str]:
    """Every property name the vendored schema declares, anywhere."""
    names: set[str] = set()
    stack: list[Any] = [_schema()]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            declared = node.get("properties")
            if isinstance(declared, dict):
                names.update(declared)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return frozenset(names)


def _safe_path(leaf: jsonschema.ValidationError) -> str:
    """`json_path`, with every instance-derived segment removed.

    A path reads as structure rather than content, which makes it easy to
    forget that only *some* of it is schema-side. Property names are —
    they come from the schema's `properties`, and a key that is not
    declared there cannot be reached by a rule that quotes it. Array
    indices are not: the producer chose the position, and a path like
    `$.matches[41]` reports a fact about the payload's contents. Both are
    resolved here so no caller has to remember the distinction.

    The `_declared_names()` check is a guard no vendored-schema shape can
    currently exercise: every object here sets `additionalProperties:
    false`, so a key that is not declared cannot appear in a path at all.
    It stays, and stays unpinned, deliberately — a schema using
    `patternProperties` or a non-`false` `additionalProperties` would put
    producer-chosen keys into paths, and the cost of guessing wrong about
    reachability is a leak. This file has been wrong twice about what no
    input can reach.

    What the check delivers, stated exactly: the lookup is global, so a
    segment prints in full if it is declared *anywhere* in the schema, not
    only at that position. Under the schema shapes above, a producer key
    that happens to collide with one of the ~60 declared names would
    therefore print. That discloses membership in a finite schema-side
    set — categorically what `required` already does — and nothing more.
    Scoping the lookup to the level being described would close even that.
    """
    parts = ["$"]
    for segment in leaf.absolute_path:
        if isinstance(segment, int):
            parts.append("[]")
        elif segment in _declared_names():
            parts.append(f".{segment}")
        else:
            parts.append(".<key>")
    return "".join(parts)


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
    # Raised *after* the handler and without `from`: `JSONDecodeError.doc`
    # is the entire raw capture, so chaining hangs the payload — secrets
    # included — off the exception this boundary raises. `str()` and the
    # default traceback stay clean, but any reporter that serialises
    # exception attributes recovers it. `from None` is not enough either:
    # it only suppresses *display*, leaving the object on `__context__`.
    parsed: Any = None
    parse_failure: str | None = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        parse_failure = f"{exc.msg}: line {exc.lineno} column {exc.colno}"
    except (ValueError, RecursionError) as exc:
        # `JSONDecodeError` is not the decoder's only failure mode on
        # untrusted text: an integer literal past CPython's 4300-digit
        # conversion limit raises a bare `ValueError`, and deep nesting
        # raises `RecursionError`. Catching only the former let both
        # escape as themselves — the parser is an instrument too, and
        # fail-closed has to cover it exactly as it covers the diagnosis.
        parse_failure = f"the decoder refused it ({type(exc).__name__})"
    if parse_failure is not None:
        raise ContractViolation(f"stdout is not JSON: {parse_failure}")

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
            f"unknown schema_version={_describe_untrusted(schema_version)}; this "
            f"consumer is pinned to v{_SUPPORTED_SCHEMA_VERSION} "
            "(contracts/github-checker-actions/v1/)"
        )

    result_kind = parsed.get("result_kind")
    if type(result_kind) is not str or result_kind not in _KNOWN_RESULT_KINDS:
        raise ContractViolation(
            f"unknown result_kind={_describe_untrusted(result_kind)}; an envelope "
            "variant this consumer cannot interpret is not an empty result"
        )

    if any(_validator().iter_errors(parsed)):
        raise ContractViolation(
            f"payload does not match actions/v1 schema: {_safe_diagnosis(parsed)}"
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
