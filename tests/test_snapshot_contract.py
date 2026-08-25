"""TASK-201 / ADR-ECO-010: vendored snapshot contracts — pin integrity and ingestion.

Two versions are vendored side by side and both are pinned. v2 carries the epic
axis; v1 stays because a producer that has not upgraded is a supported state, and
a consumer that refused it would convert a neighbour's schedule into our outage.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

from dispatcher.core.snapshot_contract import (
    SnapshotContractError,
    WorkspaceSnapshotV1,
    parse_snapshot,
)

_CONTRACTS = Path(__file__).parent.parent / "contracts" / "github-checker-snapshot"
VENDORED = _CONTRACTS / "v1"
VENDORED_V2 = _CONTRACTS / "v2"
FIXTURES = sorted((VENDORED / "fixtures").glob("*.json"))
FIXTURES_V2 = sorted((VENDORED_V2 / "fixtures").glob("*.json"))


@pytest.mark.parametrize("vendored", [VENDORED, VENDORED_V2], ids=["v1", "v2"])
def test_pin_readme_hashes_match_vendored_files(vendored: Path) -> None:
    readme = (vendored / "README.md").read_text()
    rows = re.findall(r"\| `([^`]+)` \| `([0-9a-f]{64})` \|", readme)
    assert rows, "pin README lists no hashes"
    for rel, expected in rows:
        actual = hashlib.sha256((vendored / rel).read_bytes()).hexdigest()
        assert actual == expected, (
            f"{rel} diverged from its pin — re-vendor consciously"
        )


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda p: p.name)
def test_vendored_fixture_parses_and_roundtrips(fixture: Path) -> None:
    raw = fixture.read_text()
    snapshot = parse_snapshot(raw)
    assert snapshot.schema_version == 1
    assert snapshot.host
    # structural round-trip: extra="allow" must not drop any contract fields
    dumped = json.loads(snapshot.model_dump_json())
    assert dumped == json.loads(raw)


def test_degraded_fixture_reports_gh_error() -> None:
    degraded = parse_snapshot(
        (VENDORED / "fixtures" / "snapshot_degraded.json").read_text()
    )
    assert degraded.gh_error is not None
    assert all(repo.github is None for repo in degraded.repos)


@pytest.mark.parametrize("version", [0, 3, 99])
def test_a_version_outside_the_supported_set_is_rejected(version: int) -> None:
    """Strictness is about the SET, not about a particular number.

    This test used to assert that v2 is refused, which was true while v1 was the only
    supported version. Widening the set is a deliberate contract decision (ADR-ECO-010
    Ф2/Ф3), so the assertion moves with it — what must not move is the refusal to
    best-effort parse a version nobody vendored.
    """
    raw = (VENDORED / "fixtures" / "snapshot_degraded.json").read_text()
    payload = json.dumps({**json.loads(raw), "schema_version": version})
    with pytest.raises(SnapshotContractError, match="schema_version"):
        parse_snapshot(payload)


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(SnapshotContractError):
        parse_snapshot("not json")


def test_additive_unknown_fields_are_tolerated() -> None:
    raw = json.loads((VENDORED / "fixtures" / "snapshot_full.json").read_text())
    raw["future_optional_field"] = {"anything": 1}
    raw["repos"][0]["another_new_field"] = "x"
    snapshot = parse_snapshot(json.dumps(raw))
    assert isinstance(snapshot, WorkspaceSnapshotV1)


def test_age_seconds_is_positive_for_fixture() -> None:
    snapshot = parse_snapshot(
        (VENDORED / "fixtures" / "snapshot_full.json").read_text()
    )
    assert snapshot.age_seconds() > 0


@pytest.mark.parametrize("fixture", FIXTURES_V2, ids=lambda p: p.name)
def test_vendored_v2_fixture_parses_and_roundtrips(fixture: Path) -> None:
    """v2 must ingest AND round-trip: `extra="allow"` may not silently drop the axis."""
    raw = fixture.read_text()
    snapshot = parse_snapshot(raw)
    assert snapshot.schema_version == 2
    dumped = json.loads(snapshot.model_dump_json())
    assert dumped == json.loads(raw)


def test_a_v1_payload_is_still_accepted_and_marked_as_carrying_no_axis() -> None:
    """The compatibility promise, asserted rather than described.

    Refusing v1 would make a neighbour's upgrade schedule our outage; treating it as
    "no epics" would make it our wrong number. It must parse, and it must be
    distinguishable.
    """
    from dispatcher.core.snapshot_contract import carries_epic_axis

    raw = FIXTURES[0].read_text()
    snapshot = parse_snapshot(raw)
    assert snapshot.schema_version == 1
    assert carries_epic_axis(snapshot) is False
    assert carries_epic_axis(parse_snapshot(FIXTURES_V2[0].read_text())) is True


def _mutations(base: dict) -> dict[str, dict]:
    """Deliberate v2 violations, each one a way a producer could actually break."""
    import copy

    def mutate(fn) -> dict:
        payload = copy.deepcopy(base)
        fn(payload)
        return payload

    def issues(p: dict) -> list:
        return p["repos"][0]["github"]["issues"]

    return {
        "epic block replaced by junk": mutate(
            lambda p: issues(p)[0].__setitem__("epic", {"ЧУШЬ": 123})
        ),
        "epic block is a string": mutate(
            lambda p: issues(p)[0].__setitem__("epic", "eco.ops")
        ),
        "classification outside the four states": mutate(
            lambda p: issues(p)[0]["epic"].__setitem__("classification", "probably")
        ),
        "classification dropped": mutate(
            lambda p: issues(p)[0]["epic"].pop("classification")
        ),
        "carrier dropped": mutate(lambda p: issues(p)[0]["epic"].pop("carrier")),
        "diagnostics is not a list": mutate(
            lambda p: issues(p)[0]["epic"].__setitem__("diagnostics", "none")
        ),
        "diagnostic severity outside the set": mutate(
            lambda p: issues(p)[0]["epic"].__setitem__(
                "diagnostics", [{"code": "X", "severity": "meh", "message": "m"}]
            )
        ),
        "issue number is a string": mutate(
            lambda p: issues(p)[0].__setitem__("number", "12")
        ),
        "epic axis missing from an issue": mutate(lambda p: issues(p)[0].pop("epic")),
        "merged window loses truncated": mutate(
            lambda p: p["repos"][0]["github"]["merged"].pop("truncated")
        ),
    }


def test_the_typed_models_reject_what_the_vendored_schema_rejects() -> None:
    """Anti-drift: the models are a restatement of the pin, so they must agree with it.

    Hand-written models beside a vendored schema are two sources of truth, and the
    dangerous direction is one-way: a model LOOSER than the pin accepts payloads the
    contract forbids, and does so silently. Asserting agreement on deliberate
    violations is what keeps the restatement honest between re-vendorings.
    """
    from jsonschema import Draft202012Validator

    schema = json.loads((VENDORED_V2 / "snapshot.schema.json").read_text())
    validator = Draft202012Validator(schema)
    base = json.loads((VENDORED_V2 / "fixtures" / "snapshot_full.json").read_text())

    assert validator.is_valid(base), "the pinned fixture must satisfy its own pin"
    assert parse_snapshot(json.dumps(base)).schema_version == 2

    for name, payload in _mutations(base).items():
        schema_rejects = not validator.is_valid(payload)
        try:
            parse_snapshot(json.dumps(payload))
            model_rejects = False
        except SnapshotContractError:
            model_rejects = True
        assert schema_rejects, f"the pin itself does not reject {name!r} — fix the case"
        assert model_rejects, f"models accept what the pin rejects: {name}"
