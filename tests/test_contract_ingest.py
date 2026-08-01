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
from pathlib import Path
from typing import Any

import pytest

from dispatcher.core.contract import (
    CliError,
    ContractError,
    ContractViolation,
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
    assert "additionalProperties" in messages["foreign-field"], messages[
        "foreign-field"
    ]


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
    with pytest.raises(ContractViolation, match="ninth_verb") as exc_info:
        ingest(json.dumps(payload), returncode=0)
    message = str(exc_info.value)
    assert "additionalProperties" not in message, message
    assert "$.action" in message, message


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


def test_an_unknown_verb_is_named_only_when_it_is_identifier_shaped() -> None:
    """The verb reaches the message from unvalidated input, so it goes
    through the same bounded description as the other discriminators."""
    secret = "ghp_" + "S" * 400
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
    secret = "ghp_" + "S" * 400
    payload = _fixture("pull-success") | {field: {"token": secret, "n": [1, 2]}}
    with pytest.raises(ContractViolation) as exc_info:
        ingest(json.dumps(payload), returncode=0)
    message = str(exc_info.value)
    assert secret not in message
    assert "\n" not in message
    assert len(message) < 300


def test_a_precheck_still_names_a_plausible_drift_value() -> None:
    """Bounding the prechecks must not cost the diagnosis that matters:
    the realistic drift is a producer bumping the version or adding a
    fourth variant, and an operator needs to read *which* off the log."""
    version_drift = _fixture("pull-success") | {"schema_version": 2}
    with pytest.raises(ContractViolation, match=r"schema_version=2\b"):
        ingest(json.dumps(version_drift), returncode=0)

    kind_drift = _fixture("pull-success") | {"result_kind": "merge_and_sync"}
    with pytest.raises(ContractViolation, match="merge_and_sync"):
        ingest(json.dumps(kind_drift), returncode=0)


def test_unparseable_stdout_is_not_retained_on_the_raised_exception() -> None:
    """`raise ... from exc` keeps the `JSONDecodeError` reachable, and its
    `.doc` attribute is the *entire* raw capture. `str()` and the default
    traceback are clean, but any reporter that serialises exception
    attributes (Sentry-style capture, a structured-logging dump of
    `vars(...)`) recovers the payload — the exact artefact this boundary
    keeps out of logs. `from None` is not enough: it only sets
    `__suppress_context__`, leaving the object on `__context__`."""
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
