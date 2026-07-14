"""TASK-201: vendored snapshot contract v1 — pin integrity and strict ingestion."""

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

VENDORED = Path(__file__).parent.parent / "contracts" / "github-checker-snapshot" / "v1"
FIXTURES = sorted((VENDORED / "fixtures").glob("*.json"))


def test_pin_readme_hashes_match_vendored_files() -> None:
    readme = (VENDORED / "README.md").read_text()
    rows = re.findall(r"\| `([^`]+)` \| `([0-9a-f]{64})` \|", readme)
    assert rows, "pin README lists no hashes"
    for rel, expected in rows:
        actual = hashlib.sha256((VENDORED / rel).read_bytes()).hexdigest()
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


def test_wrong_schema_version_is_rejected() -> None:
    raw = (VENDORED / "fixtures" / "snapshot_degraded.json").read_text()
    payload = json.dumps({**json.loads(raw), "schema_version": 2})
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
