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


def test_the_mirror_now_refuses_the_traversal_the_producer_refuses() -> None:
    """maestro#211 widened the check to host/owner/repo; the mirror follows.

    Before the re-pin this case lived in a
    `producer_accepts_but_dispatcher_must_refuse` section, because the
    producer accepted it and only `safe_path_parts` stood in the way.
    """
    for url in (
        "git@github.com:owner/../etc.git",
        "git@..:owner/repo.git",
        "https://../owner/repo.git",
    ):
        with pytest.raises(IdentityError, match="unsafe path segments"):
            parse_remote_url(url)


def test_dots_inside_a_segment_stay_legal() -> None:
    """Only a segment that IS `.` or `..` is unsafe — `x..y` is a repo name."""
    assert parse_remote_url("git@github.com:owner/x..y.git").as_path_parts() == (
        "github.com",
        "owner",
        "x..y",
    )


def test_safe_path_parts_still_guards_a_directly_built_key() -> None:
    """Belt-and-braces: a `RepoKey` can be constructed without the parser.

    The producer closing the hole does not retire this guard — dispatcher
    joins these segments into a filesystem path itself, and that defence
    must not depend on the neighbour's version.
    """
    with pytest.raises(IdentityError, match="unsafe path segment"):
        safe_path_parts(RepoKey(host="github.com", owner="..", repo="etc"))


def test_safe_path_parts_passes_a_normal_key() -> None:
    key = RepoKey(host="github.com", owner="owner", repo="deployer")
    assert safe_path_parts(key) == ("github.com", "owner", "deployer")


def test_identity_from_checkout_without_origin_refuses(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with pytest.raises(IdentityError):
        identity_from_checkout(tmp_path)
