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

from dispatcher.core.contract import CliError, ContractViolation, ingest

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
    [("pull-success", 1), ("pull-not-a-repo", 0), ("cli-error", 0)],
    ids=["ok-true-exit-1", "ok-false-exit-0", "cli-error-exit-0"],
)
def test_a_mismatched_exit_code_is_refused(fixture: str, returncode: int) -> None:
    """The exit code is contract: a producer that answers correctly and
    exits wrongly must not be accepted."""
    with pytest.raises(ContractViolation, match="exit"):
        ingest(json.dumps(_fixture(fixture)), returncode=returncode)


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
