"""dispatcher must name a repository exactly as maestro does (spec §5.2.1)."""

import json
import subprocess
from pathlib import Path

import pytest

from dispatcher.core.run_identity import (
    IdentityError,
    RepoKey,
    identity_from_checkout,
    parse_remote_url,
    safe_path_parts,
)

_CASES = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "contracts/maestro-repo-identity/v1/cases.json"
    ).read_text()
)


@pytest.mark.parametrize("case", _CASES["parse"], ids=lambda c: c["url"])
def test_parse_matches_the_pinned_table(case: dict) -> None:
    assert list(parse_remote_url(case["url"]).as_path_parts()) == case["key"]


@pytest.mark.parametrize("case", _CASES["reject"], ids=lambda c: c["why"])
def test_rejects_what_the_producer_rejects(case: dict) -> None:
    with pytest.raises(IdentityError):
        parse_remote_url(case["url"])


def test_local_key_is_two_segments() -> None:
    key = RepoKey(host="", owner="", repo="thing-abc123", local=True)
    assert key.as_path_parts() == ("_local", "thing-abc123")


def test_identity_from_checkout_reads_origin(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "git@github.com:Owner/Repo.git",
        ],
        check=True,
    )
    assert identity_from_checkout(tmp_path).as_path_parts() == (
        "github.com",
        "owner",
        "repo",
    )


def test_traversal_segment_is_refused_before_any_join() -> None:
    """The producer accepts owner='..'; dispatcher must not join it."""
    accepted = _CASES["producer_accepts_but_dispatcher_must_refuse"][0]
    key = parse_remote_url(accepted["url"])
    assert list(key.as_path_parts()) == accepted["key"], "mirror stays faithful"
    with pytest.raises(IdentityError, match="unsafe path segment"):
        safe_path_parts(key)


def test_safe_path_parts_passes_a_normal_key() -> None:
    key = RepoKey(host="github.com", owner="owner", repo="deployer")
    assert safe_path_parts(key) == ("github.com", "owner", "deployer")


def test_identity_from_checkout_without_origin_refuses(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with pytest.raises(IdentityError):
        identity_from_checkout(tmp_path)
