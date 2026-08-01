"""Pin verification for the vendored github-checker actions/v1 contract.

This is offline-only: it reads the vendored copy under `contracts/` and
never touches `../github-checker`. The vendoring *procedure* (documented in
`scripts/vendor_manifest.py` and re-run to produce this copy) is what must
provably extract the pinned commit's blobs; these tests only guard against
the copy quietly drifting from its own recorded manifest afterward.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from dispatcher.core.contract import (
    _MAX_SCHEMA_ERROR_LEN,
    _VARIANT_DEFS,
    ActionPayload,
    ChangedFile,
    CheckRun,
    CliError,
    ContractError,
    ContractViolation,
    IssueRef,
    LocalStatus,
    PrDetail,
    ReviewThread,
    _describe_untrusted,
    _schema,
    _verb_defs,
    ingest,
)

VENDORED_ROOT = (
    Path(__file__).parent.parent / "contracts" / "github-checker-actions" / "v1"
)
PRODUCER_COMMIT = "ef03fefcded37676b19ef1c6f88b956a09a26d3f"
_EXCLUDED_NAMES = {"PINNED.txt", "manifest.json"}


def _load_manifest() -> dict:
    return json.loads((VENDORED_ROOT / "manifest.json").read_text())


def _fixture(name: str) -> dict[str, Any]:
    """Load one vendored golden fixture as a plain dict, by stem."""
    return json.loads((VENDORED_ROOT / "fixtures" / f"{name}.json").read_text())


def _ingest_action(name: str, returncode: int) -> ActionPayload:
    """Ingest one fixture and narrow to the action variant.

    `ingest` returns the three-variant union, so reading an action-only
    field off it is a type error — and asserting the variant is the right
    fix rather than a cast: a test that reads `.matches` is a test about
    an action payload, and if ingestion ever returns something else that
    is the failure, not a nuisance.
    """
    result = ingest(json.dumps(_fixture(name)), returncode=returncode)
    assert isinstance(result, ActionPayload)
    return result


def test_the_vendored_surface_matches_its_manifest() -> None:
    """A pinned copy nobody re-hashes is a copy that drifted quietly."""
    manifest = _load_manifest()
    assert manifest["producer_commit"] == PRODUCER_COMMIT
    for entry in manifest["surface"]:
        blob = (VENDORED_ROOT / entry["path"]).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == entry["sha256"], entry["path"]


def test_the_manifest_covers_every_vendored_file() -> None:
    """Per-file hashes are worthless if a file can be added without one."""
    listed = {e["path"] for e in _load_manifest()["surface"]}
    on_disk = {
        str(p.relative_to(VENDORED_ROOT))
        for p in VENDORED_ROOT.rglob("*")
        if p.is_file() and p.name not in _EXCLUDED_NAMES
    }
    assert listed == on_disk


def test_all_thirty_four_fixtures_are_present() -> None:
    """The normative surface includes all 34 fixtures, not a subset."""
    assert len(list((VENDORED_ROOT / "fixtures").glob("*.json"))) == 34


def test_the_tree_hash_is_recomputed_not_merely_stored() -> None:
    """Per-file hashes and coverage still leave `tree_sha256` unchecked: it
    could be anything and every other pin test would pass. Recompute it with
    the same canonical algorithm the manifest was built with."""
    manifest = _load_manifest()
    entries = sorted(manifest["surface"], key=lambda e: e["path"])
    recomputed = hashlib.sha256(
        "".join(f"{e['path']}:{e['sha256']}\n" for e in entries).encode()
    ).hexdigest()
    assert recomputed == manifest["tree_sha256"]


def test_readme_carries_the_three_state_rule() -> None:
    """The README is normative: schema without it is shape without meaning."""
    readme = (VENDORED_ROOT / "README.md").read_text()
    assert "three-state rule" in readme


def test_the_manifest_declares_the_contract_it_pins() -> None:
    """`contract_version` is vendored but was asserted by nothing: a future
    re-vendor that forgot to bump it would pass every other pin guard."""
    manifest = json.loads((VENDORED_ROOT / "manifest.json").read_text())
    assert manifest["contract"] == "github-checker-actions"
    assert manifest["contract_version"] == 1
    schema = json.loads((VENDORED_ROOT / "actions.schema.json").read_text())
    # the schema's own version const must agree with what the manifest claims
    assert schema["$defs"]["verb_pull"]["properties"]["schema_version"]["const"] == 1


def test_an_unknown_schema_version_is_refused() -> None:
    payload = _fixture("pull-success") | {"schema_version": 2}
    with pytest.raises(ContractViolation, match="schema_version"):
        ingest(json.dumps(payload), returncode=1)


def test_an_unknown_result_kind_is_refused() -> None:
    payload = _fixture("pull-success") | {"result_kind": "something_new"}
    with pytest.raises(ContractViolation, match="result_kind"):
        ingest(json.dumps(payload), returncode=1)


def test_a_missing_schema_version_is_refused_not_defaulted() -> None:
    payload = {
        k: v for k, v in _fixture("pull-success").items() if k != "schema_version"
    }
    with pytest.raises(ContractViolation):
        ingest(json.dumps(payload), returncode=1)


def test_non_json_is_a_consumer_failure_not_an_empty_result() -> None:
    with pytest.raises(ContractViolation, match="not JSON"):
        ingest("<html>gateway timeout</html>", returncode=1)


def test_a_cli_error_never_becomes_an_action_payload() -> None:
    """`action` is diagnostic there: it may name a verb, and must not
    select that verb's payload."""
    result = ingest(json.dumps(_fixture("cli-error")), returncode=1)
    assert isinstance(result, CliError)
    assert result.action == "merge", "kept for diagnosis"


@pytest.mark.parametrize(
    "raw, why",
    [
        ("[]", "a JSON array is not an envelope"),
        ('"a string"', "a scalar is not an envelope"),
        ('{"schema_version": true, "result_kind": "action"}', "True == 1 in Python"),
        ('{"schema_version": 1, "result_kind": null}', "kind must be a string"),
    ],
    ids=["array", "scalar", "bool-version", "null-kind"],
)
def test_the_prechecks_are_type_strict(raw: str, why: str) -> None:
    with pytest.raises(ContractViolation):
        ingest(raw, returncode=1)


@pytest.mark.parametrize(
    "fixture, returncode",
    [
        ("pull-success", 1),
        ("pull-not-a-repo", 0),
        ("cli-error", 0),
        ("contract-error", 0),
    ],
    ids=[
        "ok-true-exit-1",
        "ok-false-exit-0",
        "cli-error-exit-0",
        "contract-error-exit-0",
    ],
)
def test_a_mismatched_exit_code_is_refused(fixture: str, returncode: int) -> None:
    """The exit code is contract: a producer that answers correctly and
    exits wrongly must not be accepted."""
    with pytest.raises(ContractViolation, match="exit"):
        ingest(json.dumps(_fixture(fixture)), returncode=returncode)


def test_a_contract_error_ingests_end_to_end() -> None:
    """`contract-error.json` was otherwise vendored but never ingested —
    the one variant of the three with no end-to-end coverage."""
    result = ingest(json.dumps(_fixture("contract-error")), returncode=1)
    assert isinstance(result, ContractError)
    assert result.action == "merge", "kept for diagnosis, never for routing"


def test_a_payload_with_a_foreign_field_is_refused() -> None:
    payload = _fixture("pull-success") | {"merged": True}
    with pytest.raises(ContractViolation):
        ingest(json.dumps(payload), returncode=1)


def test_a_payload_missing_a_required_field_is_refused() -> None:
    payload = {k: v for k, v in _fixture("pull-success").items() if k != "local"}
    with pytest.raises(ContractViolation):
        ingest(json.dumps(payload), returncode=1)


# Supplementary to the brief's Step 5 mutation list: the two tests above use
# returncode=1 against a pull-success (ok=true) fixture, so the correctly
# working exit-code guard also rejects them — it fires before schema
# validation and masks a schema-validation mutation from ever reddening
# those two tests. These variants hold the exit code at the value the
# envelope's own `ok` demands, so only additionalProperties/required can be
# the guard doing the rejecting. See task-2-report.md for the mutation-test
# finding this closes.
def test_a_payload_with_a_foreign_field_is_refused_at_a_legal_exit_code() -> None:
    payload = _fixture("pull-success") | {"merged": True}
    with pytest.raises(ContractViolation):
        ingest(json.dumps(payload), returncode=0)


def test_a_payload_missing_a_required_field_is_refused_at_a_legal_exit_code() -> None:
    payload = {k: v for k, v in _fixture("pull-success").items() if k != "local"}
    with pytest.raises(ContractViolation):
        ingest(json.dumps(payload), returncode=0)


# Supplementary: the brief's schema_version/result_kind precheck tests use
# match="schema_version"/match="result_kind", but jsonschema's own fallback
# error message dumps the whole payload dict in its text, so those
# substrings show up even when the Python-level precheck is disabled and
# jsonschema's oneOf+const catches the same defect on its own. These two
# assert the precheck's own distinctive wording, which the schema-only
# fallback never produces, so they redden specifically when the precheck
# itself is bypassed.
def test_an_unknown_schema_version_names_the_pin_in_its_own_words() -> None:
    payload = _fixture("pull-success") | {"schema_version": 2}
    with pytest.raises(ContractViolation, match="this consumer is pinned to v1"):
        ingest(json.dumps(payload), returncode=1)


def test_an_unknown_result_kind_names_itself_an_unrecognised_variant() -> None:
    payload = _fixture("pull-success") | {"result_kind": "something_new"}
    with pytest.raises(ContractViolation, match="cannot interpret"):
        ingest(json.dumps(payload), returncode=1)


def test_the_bool_version_precheck_diagnosis_is_specific_not_generic() -> None:
    """`test_the_prechecks_are_type_strict[bool-version]` only proves that
    *something* refuses `schema_version: true`: the vendored schema's own
    `const: 1` on every leaf already rejects it independently (jsonschema's
    instance equality is JSON-Schema-typed, so it never has Python's
    `True == 1` problem), so a value-loose `schema_version != 1` mutation
    of the precheck — the exact trap the brief warns about — would still
    raise `ContractViolation` via the schema-validation fallback and leave
    that test green. This asserts the precheck's own distinctive wording
    on a payload that is otherwise fully schema-valid, which only the
    Python-level type check itself can produce; the generic schema
    fallback never says it. See task-2-report.md for the mutation proof
    (this test reddens under the `!= 1` mutation; the array/scalar/
    bool-version/null-kind test above does not)."""
    payload = _fixture("pull-success") | {"schema_version": True}
    with pytest.raises(ContractViolation, match="this consumer is pinned to v1"):
        ingest(json.dumps(payload), returncode=1)


def test_a_schema_violation_message_never_echoes_producer_content() -> None:
    """A validation failure must not become a channel that copies a
    producer's free-text field content (which can carry secrets or
    tokens) into a log or a UI. The message must name what failed, stay
    single-line, and stay bounded — never reproduce the offending value."""
    secret = "ghp_" + "A" * 200
    payload = _fixture("pull-success") | {"detail": secret, "merged": True}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(payload), returncode=1)
    message = str(exc_info.value)
    assert secret not in message
    assert "\n" not in message
    assert len(message) < 300


def test_structurally_different_schema_violations_diagnose_differently() -> None:
    """Redaction must not be achieved by saying nothing. The vendored
    schema's root is a bare `oneOf` over the three envelope variants, so
    `iter_errors` yields exactly one error — always at `$`, always
    `oneOf`, always the same three `$ref`s. A message built from that
    root error alone is a *constant*: an operator triaging a refused
    merge cannot tell a foreign field from a mistyped `ok` from a bad
    nested `local.dirty`, and the prefix already said "failed the
    schema". The real rule lives in the sub-errors, which are schema-side
    too — naming them costs no disclosure."""
    base = _fixture("pull-success")
    cases = {
        "foreign-field": base | {"merged": True},
        "wrong-type": base | {"ok": "yes"},
        "nested-wrong-type": base | {"local": base["local"] | {"dirty": "nope"}},
    }
    messages: dict[str, str] = {}
    for name, payload in cases.items():
        with pytest.raises(ContractViolation) as exc_info:
            ingest(json.dumps(payload), returncode=0)
        messages[name] = str(exc_info.value)

    assert len(set(messages.values())) == 3, messages
    for name, message in messages.items():
        # A message that leads with "$.action: failed const='open-pr'" is a
        # confidently wrong one: `action` is correct in all three payloads,
        # and that leaf is only the *other* verb branches failing to be
        # selected. Diagnosis that misleads is worse than the constant it
        # replaced, because it is followed.
        assert "$.action: failed const" not in message, (name, message)
    assert "$.ok" in messages["wrong-type"], messages["wrong-type"]
    assert "$.local.dirty" in messages["nested-wrong-type"], messages[
        "nested-wrong-type"
    ]
    assert "unexpected" in messages["foreign-field"], messages["foreign-field"]
    assert "merged" not in messages["foreign-field"], messages["foreign-field"]


def test_a_missing_field_is_named_not_merely_counted() -> None:
    """A field added or removed is by a wide margin the most common
    producer drift, and `required` is the keyword that catches removals —
    yet every removal, in every variant, produced the identical `$: failed
    required`. That is verbatim the say-nothing message this diagnosis
    replaced, still in place for the drift that actually happens.

    The name costs no disclosure: it is a member of the schema's own
    `required` array, so it is schema-side by construction — the instance
    is consulted only to ask which schema-declared names are absent."""
    base = _fixture("pull-success")
    without_local = {k: v for k, v in base.items() if k != "local"}
    without_detail = {k: v for k, v in base.items() if k != "detail"}

    messages = []
    for payload in (without_local, without_detail):
        with pytest.raises(ContractViolation) as exc_info:
            ingest(json.dumps(payload), returncode=0)
        messages.append(str(exc_info.value))

    assert "local" in messages[0], messages[0]
    assert "detail" in messages[1], messages[1]
    assert messages[0] != messages[1]

    # Schema-side names are bounded by the schema, but a verb declaring
    # fifteen required fields would still put fifteen of them in one line.
    dropped = {"dir", "ok", "error", "detail"}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(
            json.dumps({k: v for k, v in base.items() if k not in dropped}),
            returncode=0,
        )
    message = str(exc_info.value)
    assert "and more" in message, message
    # `required` emits one error per missing property and each renders the
    # whole missing set, so without de-duplication the arity budget goes
    # on printing one sentence three times.
    assert message.count("missing required") == 1, message


def test_a_foreign_field_is_counted_never_named() -> None:
    """The mirror of the above, and its opposite. A *missing* name is
    schema-side — it is a member of the schema's own `required` array — so
    it can be printed. An *unexpected* key is producer text with no schema
    provenance at all: the key itself is the thing that must not travel,
    however innocuous it looks. A key is a value here."""
    base = _fixture("pull-success")
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(base | {"merged": True}), returncode=0)
    message = str(exc_info.value)
    assert "merged" not in message, message
    assert "1 unexpected property" in message, message

    smuggled = "ghp_" + "s" * 400
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(base | {smuggled: True}), returncode=0)
    message = str(exc_info.value)
    assert smuggled not in message
    assert len(message) < 300

    # The producer decides how many keys it sends; it must not thereby
    # decide how long the message is.
    many = base | {f"extra_{n}": True for n in range(500)}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(many), returncode=0)
    message = str(exc_info.value)
    assert "500 unexpected" in message, message
    assert len(message) < 300


def test_an_array_index_is_a_position_the_producer_chose() -> None:
    """A `json_path` is safe only where every segment is schema-declared.
    Property names in these paths are — they come from the schema's
    `properties` — but an array index is chosen by the producer, so it is
    instance-derived and does not travel either."""
    base = _fixture("propose-pr-created")
    payload = base | {"changed_paths": ["ok"] * 40000 + [7]}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(payload), returncode=0)
    message = str(exc_info.value)
    assert "$.changed_paths[]" in message, message
    assert "40000" not in message, message


def test_the_variant_map_matches_the_schema_it_indexes() -> None:
    """`_VARIANT_DEFS` is hand-maintained and became load-bearing when the
    diagnosis started building `$ref` strings out of it. A re-vendor that
    renames a `$defs` key — updating the root `oneOf` and re-hashing the
    manifest, as the vendoring procedure requires — leaves the whole suite
    green while that variant's *refusal* path starts raising a foreign
    referencing error instead of `ContractViolation`. Pin it exactly as
    `_verb_defs` is pinned."""
    schema = json.loads((VENDORED_ROOT / "actions.schema.json").read_text())
    from_schema = {branch["$ref"].rsplit("/", 1)[-1] for branch in schema["oneOf"]}
    assert set(_VARIANT_DEFS.values()) == from_schema
    for def_name in _VARIANT_DEFS.values():
        assert def_name in schema["$defs"]


def test_a_failing_diagnosis_still_refuses_as_a_contract_violation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-closed has to cover the instrument. The diagnosis is decoration
    over a refusal that has *already* been decided authoritatively, so when
    it raises, the refusal must survive unchanged — same type, still
    bounded. Today the diagnosis is evaluated inside the argument to
    `ContractViolation(...)`, so its failure means that exception is never
    constructed and a caller writing the documented `except
    ContractViolation` sees a foreign exception instead."""

    def explode(*args: object, **kwargs: object) -> str:
        raise RuntimeError("x" * 5000)

    monkeypatch.setattr("dispatcher.core.contract._describe_schema_error", explode)
    payload = _fixture("pull-success") | {"merged": True}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(payload), returncode=0)
    message = str(exc_info.value)
    assert len(message) < 300, "an instrument failure must not blow the bound"
    assert "x" * 100 not in message


@pytest.mark.parametrize(
    "value",
    [
        "deadbeefcafebabe0123456789abcdef",
        "sk-proj_abcdefghijklmnop_qrstuv",
        "merge_and_sync",
        "a",
        "",
        2,
        1234567890123456,
        3.5,
        True,
        None,
        ["secret"],
        {"token": "secret"},
    ],
    ids=lambda v: type(v).__name__ + "-" + str(v)[:12],
)
def test_no_instance_value_is_ever_echoed_verbatim(value: object) -> None:
    """The strong form of the rule, stated so that it is checkable by
    reading the function rather than by enumerating attacks: no branch of
    `_describe_untrusted` returns producer bytes. What comes out is the
    type and a size — never content, at any length, in any character
    class. An allowlist can only ever be as safe as the values that
    happen to be tried against it."""
    # The vocabulary is written here, not derived from the module: any
    # producer text reaching the message introduces a word outside it.
    shape_words = {
        "null",
        "bool",
        "int",
        "float",
        "str",
        "list",
        "object",
        "digit",
        "digits",
        "char",
        "chars",
        "item",
        "items",
        "key",
        "keys",
    }
    described = _describe_untrusted(value)
    assert described.startswith("<") and described.endswith(">"), described
    assert set(re.findall(r"[A-Za-z]+", described)) <= shape_words, described
    if isinstance(value, str) and len(value) >= 4:
        assert value not in described
    if isinstance(value, int) and not isinstance(value, bool):
        assert str(value) not in described or len(str(value)) < 3
    assert len(described) < 40


def test_the_shape_description_still_separates_the_cases() -> None:
    """Refusing to echo must not collapse every value into one word: type
    and size are the diagnosis now, so they have to actually differ."""
    described = {
        _describe_untrusted(v)
        for v in ("abc", "abcd", 2, 22, 3.5, True, None, [1], {"a": 1})
    }
    assert len(described) == 9, described


def test_both_shapes_of_an_ambiguous_oneOf_are_reported() -> None:
    """`issue` is `oneOf(issue_ref, null)` and carries this contract's
    central three-state invariant — null with `created=true` means the
    issue exists but the read-back failed. A producer emitting `false`
    there is exactly the absent/null/false confusion the contract exists
    to prevent, and reporting only "must be an object" points the operator
    at building an `issue_ref` instead of at the null-vs-false bug."""
    payload = _fixture("issue-create-created") | {"issue": False}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(payload), returncode=0)
    message = str(exc_info.value)
    assert "'object'" in message, message
    assert "'null'" in message, message


def _many_distinct_rules() -> dict[str, Any]:
    """A vendored-schema-legal payload that fails many *different* rules.

    An earlier version of the test below used `changed_paths: [1] * 5000`,
    which fails one rule five thousand times: array indices render as
    `[]`, so all five thousand leaves render identically and the
    de-duplication collapses them to one. The assertion held at any arity
    and pinned nothing. Distinct paths are what exercises the bound."""
    base = _fixture("pr-detail-full")
    return base | {
        "pr_detail": base["pr_detail"]
        | {"checks": [{}], "files": [{}], "review_threads": [{}]}
    }


def test_the_number_of_reported_rules_is_bounded() -> None:
    """Producer input decides how many rules fail; it must not decide how
    many are printed.

    The payload has five short failing rules on purpose. The obvious
    choice — the long `pr_detail` one used for the length cap below —
    cannot pin this: its rules are long enough that the 200-char cap
    truncates the message before the arity is visible, so the cap masks
    the bound under test and unbounding the arity changes nothing. Two
    bounds that can each hide the other's absence need one payload each."""
    base = _fixture("pull-success")
    payload = base | {
        "local": {"branch": 1, "ahead": "x", "behind": "y", "dirty": "z", "error": 5}
    }
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(payload), returncode=0)
    message = str(exc_info.value)
    assert message.count("; ") == 2, message
    assert len(message) < _MAX_SCHEMA_ERROR_LEN, "the cap must not be what bounds it"


def test_the_message_length_is_capped_on_a_payload_that_reaches_it() -> None:
    """The cap was documented as a backstop no input reaches. It is
    reached: three empty objects in `checks`/`files`/`review_threads` are
    legal JSON against this schema's shape and fail three different
    `required` rules, whose schema-side names are long enough together to
    run past the bound. Documented-as-unreachable is not unreachable, and
    the way to stop being wrong about it is a test, not a better comment."""
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(_many_distinct_rules()), returncode=0)
    message = str(exc_info.value)
    assert "…" in message, "the payload must actually reach the cap"
    assert len(message) < 300


def test_a_schema_side_enum_names_its_legal_values() -> None:
    """`enum` members come from the schema, so the rule permits them to
    travel — and they are the diagnosis: an operator told only that
    `local_sync` failed an enum has to open the schema to learn what the
    legal values were. The per-rule value bound must not be tight enough
    to suppress the longest one this schema actually declares."""
    payload = _fixture("merge-merged") | {"local_sync": "bogus"}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(payload), returncode=0)
    message = str(exc_info.value)
    assert "not_attempted" in message, message


def test_unexpected_properties_are_described_by_shape_and_aggregated() -> None:
    """A key is producer text, so the name never travels — but its type
    and length may, and they carry the little that can be carried: a
    six-character key is a plausible field rename, a forty-character one
    is not. Identical shapes aggregate so the order within a group cannot
    encode anything, and the ordering is by count and then by descriptor —
    both derived from what is already printed, never from the key text."""
    base = _fixture("pull-success")

    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(base | {"merged": True}), returncode=0)
    message = str(exc_info.value)
    assert "1 unexpected property: <str, 6 chars>" in message, message
    assert "merged" not in message, message

    with pytest.raises(ContractViolation) as exc_info:
        ingest(
            json.dumps(base | {f"extra_{n}": True for n in range(500)}), returncode=0
        )
    message = str(exc_info.value)
    assert "500 unexpected properties" in message, message
    assert "extra_" not in message, message
    assert len(message) < 300

    mixed = base | {"ab": 1, "abcdefghijkl": 2, "mnopqrstuvwx": 3}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(mixed), returncode=0)
    message = str(exc_info.value)
    assert "3 unexpected properties" in message, message
    assert "2 × <str, 12 chars>" in message, message
    assert "<str, 2 chars>" in message, message

    # Five *distinct* shapes, not five keys: the count of groups is what
    # the cap bounds, and a payload whose keys share lengths collapses to
    # three groups on its own, leaving the bound undefended.
    five_shapes = base | {"k" * n: True for n in (3, 4, 5, 6, 7)}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(five_shapes), returncode=0)
    message = str(exc_info.value)
    assert "5 unexpected properties" in message, message
    assert "and 2 more" in message, message
    assert "<str, 7 chars>" not in message, message


def test_every_verb_in_the_schema_is_discoverable_for_diagnosis() -> None:
    """The verb map is derived from the vendored schema by naming
    convention, so a re-vendor that renames the `$defs` keys would empty
    it silently — and an empty map diagnoses *every* action payload as an
    unknown verb while `ingest` still accepts them all, which reads as
    working. Pin the count and the names against the schema itself."""
    schema = json.loads((VENDORED_ROOT / "actions.schema.json").read_text())
    from_schema = {
        branch["$ref"].rsplit("/", 1)[-1]
        for branch in schema["$defs"]["action_result"]["oneOf"]
    }
    assert set(_verb_defs().values()) == from_schema
    assert len(_verb_defs()) == 8


def test_an_unknown_verb_is_diagnosed_as_an_unknown_verb() -> None:
    """`action_result` is itself a `oneOf` over the eight verbs, so the
    same combinator problem recurs one level down: with an unrecognised
    verb *every* branch fails, and the leaf rules they fail on describe
    the branch that was never going to match — a payload carrying exactly
    `pull`'s fields reads as "additionalProperties" against `open-pr`.
    The honest diagnosis is the discriminator itself."""
    payload = _fixture("pull-success") | {"action": "ninth_verb"}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(payload), returncode=0)
    message = str(exc_info.value)
    assert "unexpected" not in message, message
    assert "$.action" in message, message
    # The discriminator is the diagnosis, but the verb name is producer
    # text like any other — the *shape* is what travels.
    assert "<str, 10 chars>" in message, message
    assert "ninth_verb" not in message, message


def test_a_combinator_nested_below_the_verb_is_still_descended_into() -> None:
    """Selecting the verb branch removes the two combinators above it, but
    not every one: `verb_issue_create.properties.issue` is itself a
    `oneOf` (an `issue_ref`, or `null` when the read-back failed). Without
    descending, a malformed `issue` reports the combinator — `$.issue:
    failed oneOf` — which is the same say-nothing message one level in."""
    payload = _fixture("issue-create-created")
    assert payload["issue"] is not None, "fixture must exercise the ref branch"
    payload = payload | {"issue": payload["issue"] | {"number": "twelve"}}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(payload), returncode=0)
    message = str(exc_info.value)
    assert "$.issue.number" in message, message
    assert "failed oneOf" not in message, message
    # Deepest rule first: the leaf inside `issue` is the cause; the rule
    # about `issue` itself is the frame around it.
    assert message.index("$.issue.number") < message.index("$.issue: failed"), message


def test_an_unknown_verb_is_named_only_when_it_is_identifier_shaped() -> None:
    """The verb reaches the message from unvalidated input, so it goes
    through the same bounded description as the other discriminators."""
    # Lowercase deliberately: an uppercase byte fails the allowlist's
    # character class on its own, so a capitalised secret exercises only
    # that half and leaves the length bound — the thing standing between
    # this message and a 10 MB log record — pinned by nothing.
    secret = "ghp_" + "s" * 400
    payload = _fixture("pull-success") | {"action": secret}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(payload), returncode=0)
    message = str(exc_info.value)
    assert secret not in message
    assert len(message) < 300


@pytest.mark.parametrize("field", ["schema_version", "result_kind"])
def test_a_precheck_message_never_echoes_producer_content(field: str) -> None:
    """The two discriminator prechecks run *before* schema validation, on
    a value that is whatever the producer put on the wire — so `!r` of it
    is the same disclosure channel the schema branch was fixed for, and
    an unbounded one: a nested envelope or a 10 MB string lands verbatim
    in the message. Redacting one branch and not these two is redaction
    by coincidence of today's producer, not by construction."""
    # Lowercase deliberately: an uppercase byte fails the allowlist's
    # character class on its own, so a capitalised secret exercises only
    # that half and leaves the length bound — the thing standing between
    # this message and a 10 MB log record — pinned by nothing.
    secret = "ghp_" + "s" * 400
    payload = _fixture("pull-success") | {field: {"token": secret, "n": [1, 2]}}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(payload), returncode=0)
    message = str(exc_info.value)
    assert secret not in message
    assert "\n" not in message
    assert len(message) < 300


def test_a_precheck_names_the_shape_of_a_drift_value_never_the_value() -> None:
    """An earlier design echoed identifier-shaped values so an operator
    could read the drifted version or variant name off the log, and
    recorded the residual capacity — 32 characters of `[a-z0-9_-]` is an
    API-key shape — as an accepted risk.

    That trade is withdrawn deliberately: documenting a leak does not make
    it acceptable on a trust boundary, and an allowlist wide enough to be
    useful is wide enough to carry a credential. The diagnosis is now the
    *shape* only. Do not restore the echo to recover the diagnosis; the
    cost was weighed and paid."""
    version_drift = _fixture("pull-success") | {"schema_version": 2}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(version_drift), returncode=0)
    message = str(exc_info.value)
    assert "<int, 1 digit" in message, message
    assert "=2" not in message, message

    kind_drift = _fixture("pull-success") | {"result_kind": "merge_and_sync"}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(kind_drift), returncode=0)
    message = str(exc_info.value)
    assert "<str, 14 chars>" in message, message
    assert "merge_and_sync" not in message, message


@pytest.mark.parametrize(
    "raw, why",
    [
        ('{"schema_version": ' + "9" * 4301 + "}", "CPython's int-to-str digit limit"),
        ("[" * 100_000 + "]" * 100_000, "the decoder recurses on nesting"),
    ],
    ids=["huge-int-literal", "deep-nesting"],
)
def test_stdout_that_breaks_the_parser_is_still_a_contract_violation(
    raw: str, why: str
) -> None:
    """`json.loads` has failure modes beyond `JSONDecodeError` on
    untrusted text: a `ValueError` from CPython's 4300-digit integer
    conversion limit (added in 3.11 as a DoS mitigation, and it applies to
    `json.loads`), and a `RecursionError` from deep nesting. Catching only
    `JSONDecodeError` leaves both escaping as themselves, so a caller
    written to this module's documented `Raises:` — the contract the whole
    boundary is sold on — does not catch them, and a refusal stops being
    distinguishable from a consumer crash.

    This is the previous round's finding one statement earlier: that fix
    covered the *diagnosis* instrument and left the *parser* instrument
    uncovered."""
    with pytest.raises(ContractViolation):
        ingest(raw, returncode=1)


def test_a_huge_integer_is_described_without_being_stringified() -> None:
    """`_describe_untrusted` reports an int's size by rendering it, which
    hits the same 4300-digit limit and raises out of a function whose
    whole job is to make an untrusted value safe to mention. It is
    unreachable through `ingest` today only because `json.loads` shares
    the process-wide limit and fails first — safety resting on an accident
    of an unrelated stdlib call, which is the standard this file refuses
    everywhere else."""
    # Built by arithmetic, not `int("9"*5000)` — the limit applies to
    # parsing a literal too, so that would raise in the test itself.
    described = _describe_untrusted(10**5000)
    assert described.startswith("<int"), described
    assert len(described) < 40, described


def test_unparseable_stdout_is_not_retained_on_the_raised_exception() -> None:
    """`raise ... from exc` keeps the `JSONDecodeError` reachable, and its
    `.doc` attribute is the *entire* raw capture. `str()` and the default
    traceback are clean, but any reporter that serialises exception
    attributes (Sentry-style capture, a structured-logging dump of
    `vars(...)`) recovers the payload — the exact artefact this boundary
    keeps out of logs. `from None` is not enough: it only sets
    `__suppress_context__`, leaving the object on `__context__`.

    What this does NOT achieve, since the rationale above names a reporter
    class it does not fully defeat: `exc.__traceback__.tb_frame.f_locals`
    still holds `raw` and `parsed`, and Sentry's `include_local_variables`
    defaults to `True`. That channel is a property of every Python frame
    that has seen the capture — `ingest`'s caller included — so it cannot
    be closed here; `del` before each raise would only clear this one
    frame. Message, `args`, `__dict__`, `__cause__`, `__context__` and the
    default traceback rendering are clean, and that is the claim."""
    secret = "ghp_" + "S" * 200
    truncated = '{"schema_version": 1, "detail": "' + secret + '", '
    with pytest.raises(ContractViolation) as exc_info:
        ingest(truncated, returncode=1)
    violation = exc_info.value
    assert secret not in str(violation)
    assert violation.__cause__ is None
    assert violation.__context__ is None
    assert "line 1" in str(violation), "position diagnosis must survive"


def test_mutating_the_returned_schema_does_not_corrupt_future_validation() -> None:
    """`_schema()` must hand back a private copy each call: an in-place
    mutation by one caller must not corrupt the cached validator or any
    later call to `_schema()` itself.

    The second half asserts on an *invalid* payload deliberately: an
    emptied schema accepts everything, so checking that a valid fixture
    still ingests cannot distinguish a healthy validator from a destroyed
    one, and passes incidentally under the very defect this names."""
    schema = _schema()
    schema.clear()
    assert _schema() != {}
    result = ingest(json.dumps(_fixture("pull-success")), returncode=0)
    assert result.action == "pull"
    with pytest.raises(ContractViolation):
        ingest(json.dumps(_fixture("pull-success") | {"merged": True}), returncode=0)


# --- Task 3: typed consumer models ------------------------------------
#
# The three-state rule is the whole reason the producer workstream
# happened: absent means the verb has no such concept, `null` means
# applicable but unknown, `false`/`[]` means applicable and definitely
# negative. Typing is where that distinction is most easily lost, because
# a pydantic default refills exactly what the producer deliberately left
# out — and a consumer default is indistinguishable from a producer
# answer once it is in the model.


def test_null_and_empty_survive_typing() -> None:
    unread = _ingest_action("issue-lookup-unread", 1)
    confirmed = _ingest_action("issue-lookup-free", 0)
    assert unread.matches is None, "the inbox was not read exhaustively"
    assert confirmed.matches == [], "read, and empty"


def test_a_verb_without_the_concept_has_no_field_at_all() -> None:
    """The plan prescribed exactly one mutation for this task — default
    `matches` to `[]` — and prescribed a test that cannot catch it:
    `issue-lookup-unread` carries an explicit `"matches": null`, so the
    default is never reached, and `model_fields_set` excludes defaults
    either way. What the mutation actually changes is the *value* read off
    a verb that has no such concept: `pull.matches` becomes `[]`, which
    says "the inbox was read and was empty" about a verb that never looked
    at an inbox. So read the value, not only the bookkeeping."""
    pulled = _ingest_action("pull-success", 0)
    assert "matches" not in pulled.model_fields_set
    assert pulled.matches is None, "no concept is not an empty answer"
    assert pulled.issue is None
    assert pulled.pr_detail is None
    assert pulled.created is None

    unread = _ingest_action("issue-lookup-unread", 1)
    assert "matches" in unread.model_fields_set, "null is an answer, absence is not"


def test_the_consumer_never_synthesises_a_value() -> None:
    """A default filled in here is indistinguishable from a producer answer
    once it is in the model."""
    unknown = _ingest_action("merge-unknown", 1)
    assert unknown.merged is None
    assert unknown.merged is not False


def test_created_true_with_an_unread_issue_stays_unread() -> None:
    """`issue: null` with `created: true` is the producer saying the issue
    exists but reading it back failed. Typing must not turn that into
    "no issue" — the screen would re-enable Create and make the duplicate
    this contract exists to prevent."""
    outcome = _ingest_action("issue-create-no-readback", 0)
    assert outcome.created is True
    assert outcome.issue is None
    assert "issue" in outcome.model_fields_set


def test_the_nested_payloads_are_typed_not_dicts() -> None:
    """`pr_detail`, `matches`, `malformed` and `issue` were left as
    validated-but-untyped dicts by Task 2. They are already schema-checked,
    so typing them is a view over the same validated data, not a second
    validation path."""
    detail = _ingest_action("pr-detail-full", 0)
    assert isinstance(detail.pr_detail, PrDetail)
    assert isinstance(detail.pr_detail.checks[0], CheckRun)
    assert isinstance(detail.pr_detail.files[0], ChangedFile)
    assert isinstance(detail.pr_detail.review_threads[0], ReviewThread)

    malformed = _ingest_action("issue-lookup-malformed", 1)
    assert malformed.malformed is not None
    assert isinstance(malformed.malformed[0], IssueRef)

    one = _ingest_action("issue-lookup-one", 0)
    assert one.matches is not None
    assert isinstance(one.matches[0], IssueRef)

    created = _ingest_action("issue-create-created", 0)
    assert isinstance(created.issue, IssueRef)


def test_an_absent_nested_field_stays_absent_after_typing() -> None:
    """The rule holds one level down too: `review_thread.path` is optional
    in the schema, so a thread that omits it must not come back carrying a
    consumer `None` that reads as "the producer said there is no path"."""
    thread = ReviewThread.model_validate(
        {"id": "t1", "is_resolved": False, "is_outdated": False}
    )
    assert "path" not in thread.model_fields_set
    explicit = ReviewThread.model_validate(
        {"id": "t1", "is_resolved": False, "is_outdated": False, "path": None}
    )
    assert "path" in explicit.model_fields_set
    assert explicit.path is None


def test_a_typed_model_refuses_what_the_schema_refuses() -> None:
    """The typed view must not be looser than the boundary that produced
    it: a field the schema forbids must not become reachable by
    constructing the model directly."""
    with pytest.raises(ValidationError):
        IssueRef.model_validate(
            {
                "number": 1,
                "title": "t",
                "state": "open",
                "url": "u",
                "author": "a",
                "labels": [],
                "smuggled": True,
            }
        )


def test_a_local_status_field_the_schema_requires_has_no_default() -> None:
    """All five `local_status` fields are `required`, so a default here
    would be a consumer answer standing in for a producer fact —
    `error: None` in particular reads as "the clone was read and was
    fine", which is what `pull-local-status-error` exists to distinguish."""
    with pytest.raises(ValidationError):
        LocalStatus.model_validate({"dirty": False})
    read = _ingest_action("pull-local-status-error", 1)
    assert read.local is not None
    assert read.local.error is not None, "the fixture must carry a real error"


# --- Task 4: consumer conformance -------------------------------------
#
# The pin tests prove the vendored copy is the producer's bytes. These
# prove the consumer can actually consume them: a fixture that validates
# but cannot be turned into a model is a contract this side does not have.

VENDORED_FIXTURES = sorted((VENDORED_ROOT / "fixtures").glob("*.json"))


def _expected_exit(payload: dict[str, Any]) -> int:
    """The exit code the contract pairs with this envelope."""
    return 0 if (payload["result_kind"] == "action" and payload["ok"]) else 1


def test_the_sweep_covers_every_fixture() -> None:
    """A glob that matched nothing would make every sweep below vacuous
    and green — the failure mode a sweep cannot report about itself."""
    assert len(VENDORED_FIXTURES) == 34


@pytest.mark.parametrize("path", VENDORED_FIXTURES, ids=lambda p: p.stem)
def test_every_vendored_fixture_ingests(path: Path) -> None:
    """Parametrised rather than looped: a loop reports the first failure
    and hides the rest, and which fixtures fail is the diagnosis."""
    payload = json.loads(path.read_text())
    result = ingest(path.read_text(), returncode=_expected_exit(payload))
    assert result is not None
    assert result.schema_version == 1


@pytest.mark.parametrize("path", VENDORED_FIXTURES, ids=lambda p: p.stem)
def test_every_fixture_round_trips_key_for_key(path: Path) -> None:
    """Whole key sets, not selected keys. A significant `null` must not
    vanish on the way in, and a field the verb has no concept of must not
    appear on the way out — the two directions of the same rule, and the
    ones a spot-check of a few fields would miss."""
    payload = json.loads(path.read_text())
    ingested = ingest(path.read_text(), returncode=_expected_exit(payload))
    assert set(ingested.model_dump(exclude_unset=True)) == set(payload)


@pytest.mark.parametrize("path", VENDORED_FIXTURES, ids=lambda p: p.stem)
def test_every_nested_object_round_trips_key_for_key(path: Path) -> None:
    """The same property one level down, where the optional fields are."""
    payload = json.loads(path.read_text())
    ingested = ingest(path.read_text(), returncode=_expected_exit(payload))
    dumped = ingested.model_dump(exclude_unset=True)
    for key in ("local", "pr_detail", "issue"):
        if isinstance(payload.get(key), dict):
            assert set(dumped[key]) == set(payload[key]), key
    for key in ("matches", "malformed"):
        for sent, got in zip(payload.get(key) or [], dumped.get(key) or []):
            assert set(got) == set(sent), key


# --- Step 3: the paths this consumer actually has ---------------------


def test_the_merge_partial_outcome_survives_the_whole_way() -> None:
    """S1's hardest case: the merge answered, and what it did is unknown.
    `merged: null` beside `ok: false` is a producer saying "I could not
    establish it" — the one value that must never become `false`, because
    a screen reading `false` re-enables the merge button."""
    outcome = _ingest_action("merge-unknown", 1)
    assert outcome.merged is None
    assert "merged" in outcome.model_fields_set
    assert outcome.local_sync == "not_attempted"
    assert outcome.pr_detail is None


def test_read_and_empty_is_not_the_same_answer_as_unread() -> None:
    """S2's hardest case, and the reason the producer workstream existed."""
    free = _ingest_action("issue-lookup-free", 0)
    unread = _ingest_action("issue-lookup-unread", 1)
    assert free.matches == [] and free.malformed == []
    assert unread.matches is None and unread.malformed is None
    assert free.ok is True and unread.ok is False


def test_the_malformed_and_conflict_lookups_carry_their_evidence() -> None:
    """A slug claimed by several issues, or by an issue nobody can parse,
    is a human's decision — so the consumer has to be able to show which
    issues, not merely that there was a problem."""
    malformed = _ingest_action("issue-lookup-malformed", 1)
    assert malformed.matches == []
    assert malformed.malformed is not None and len(malformed.malformed) >= 1
    assert isinstance(malformed.malformed[0], IssueRef)

    # `ok: true` — several matches is a legitimate reading of the inbox,
    # not a failure; the decision it forces is a human's.
    conflict = _ingest_action("issue-lookup-conflict", 0)
    assert conflict.matches is not None and len(conflict.matches) > 1
    assert all(isinstance(m, IssueRef) for m in conflict.matches)


def test_a_populated_pr_detail_keeps_its_nested_collections() -> None:
    """The truncation flags are the load-bearing part: a green verdict
    computed over a truncated check list is not a green verdict."""
    full = _ingest_action("pr-detail-full", 0)
    assert isinstance(full.pr_detail, PrDetail)
    assert full.pr_detail.checks and full.pr_detail.files
    assert full.pr_detail.review_threads
    assert full.pr_detail.checks_truncated is False

    truncated = _ingest_action("pr-detail-truncated", 0)
    assert truncated.pr_detail is not None
    # Truncation is stated by the flags, not inferred from arithmetic:
    # this fixture's `files_total` equals its `files` length, so a
    # consumer comparing the two would call a truncated answer complete.
    assert truncated.pr_detail.checks_truncated is True
    assert truncated.pr_detail.files_truncated is True
    assert truncated.pr_detail.threads_truncated is True

    unavailable = _ingest_action("pr-detail-unavailable", 1)
    assert unavailable.pr_detail is None
    assert "pr_detail" in unavailable.model_fields_set, "null, not absent"


# --- Step 4: the negative sweep ---------------------------------------


def _drop(payload: dict[str, Any], key: str) -> dict[str, Any]:
    return {k: v for k, v in payload.items() if k != key}


@pytest.mark.parametrize(
    "mutate, returncode, names_the_reason",
    [
        (lambda p: p | {"schema_version": 2}, 0, "schema_version"),
        (lambda p: p | {"result_kind": "something_new"}, 0, "result_kind"),
        (lambda p: p | {"merged": True}, 0, "unexpected"),
        (lambda p: _drop(p, "local"), 0, "missing required"),
        (lambda p: p | {"ok": "yes"}, 0, "$.ok"),
        (lambda p: p, 1, "exit code"),
    ],
    ids=[
        "unknown-schema-version",
        "unknown-result-kind",
        "extra-field",
        "missing-required-field",
        "wrong-typed-field",
        "mismatched-exit-code",
    ],
)
def test_the_negative_sweep_fails_closed_and_says_why(
    mutate: Any, returncode: int, names_the_reason: str
) -> None:
    """Each refusal must name its own reason. Failing closed is half of
    it: a boundary that refuses everything with one message is fail-closed
    and undiagnosable, which is the state this diagnosis was rebuilt out
    of twice."""
    payload = mutate(_fixture("pull-success"))
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(payload), returncode=returncode)
    message = str(exc_info.value)
    assert names_the_reason in message, message
    assert "\n" not in message
    assert len(message) < 300


def test_a_tampered_vendored_file_is_caught_by_its_own_manifest(
    tmp_path: Path,
) -> None:
    """The pin tests assert the copy matches its manifest. This asserts
    the check can fail: a hash comparison nobody has watched reject
    anything is a hash comparison that might be comparing a value to
    itself."""
    tampered = tmp_path / "v1"
    shutil.copytree(VENDORED_ROOT, tampered)
    victim = tampered / "fixtures" / "pull-success.json"
    victim.write_text(victim.read_text().replace('"ok": true', '"ok": false'))

    manifest = json.loads((tampered / "manifest.json").read_text())
    mismatched = [
        entry["path"]
        for entry in manifest["surface"]
        if hashlib.sha256((tampered / entry["path"]).read_bytes()).hexdigest()
        != entry["sha256"]
    ]
    assert mismatched == ["fixtures/pull-success.json"]
