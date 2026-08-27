"""dispatcher must name a repository exactly as maestro does (spec §5.2.1)."""

import json
import subprocess
from pathlib import Path

import pytest

from dispatcher.core.run_identity import (
    IdentityError,
    RepoKey,
    find_checkout_by_identity,
    identity_from_checkout,
    list_workspace_checkouts,
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


# --- list_workspace_checkouts / find_checkout_by_identity (review fix
# wave C, C1) — the ONE enumeration the launchpad assembler and submit
# v2's checkout resolver both use, so they can never walk a workspace
# differently. ------------------------------------------------------------


def _init_checkout(root: Path, remote: str) -> None:
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", remote], check=True
    )


def test_list_workspace_checkouts_skips_hidden_and_sorts(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "b-repo").mkdir()
    (ws / "a-repo").mkdir()
    (ws / "_scratch").mkdir()
    (ws / ".git-like").mkdir()

    entries, notes = list_workspace_checkouts(ws)

    assert notes == []
    # dot-prefixed skipped; underscore stays (the repo contract permits it —
    # gate pass-4); non-git dirs are the CALLERS' concern, not the scan's
    assert [name for name, _ in entries] == ["_scratch", "a-repo", "b-repo"]
    assert entries[1][1] == ws / "a-repo"


def test_list_workspace_checkouts_reports_an_unscannable_root_as_a_note(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does-not-exist"

    entries, notes = list_workspace_checkouts(missing)

    assert entries == []
    assert len(notes) == 1
    assert str(missing) in notes[0]


def test_find_checkout_by_identity_resolves_a_directory_name_mismatch(
    tmp_path: Path,
) -> None:
    """The real fleet case (C1): a checkout's workspace directory name
    (`open-prose/`) need not match its origin remote's `repo` segment
    (`libretto`). `list_workspace_checkouts` — the SAME enumeration the
    launchpad assembler uses to classify this checkout in the first
    place — and `find_checkout_by_identity` must agree on exactly which
    checkout that repo_key names, so the assembler and submit v2 can
    never resolve one repo_key to two different checkouts."""
    ws = tmp_path / "ws"
    ws.mkdir()
    root = ws / "open-prose"
    _init_checkout(root, "git@github.com:andrei-shtanakov/libretto.git")
    target = RepoKey(host="github.com", owner="andrei-shtanakov", repo="libretto")

    entries, notes = list_workspace_checkouts(ws)
    assert notes == []
    assert [name for name, _ in entries] == ["open-prose"]

    found = find_checkout_by_identity(ws, target)
    assert found == root.resolve()


def test_find_checkout_by_identity_returns_none_when_nothing_matches(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    root = ws / "some-repo"
    _init_checkout(root, "git@github.com:andrei-shtanakov/some-repo.git")
    target = RepoKey(host="github.com", owner="andrei-shtanakov", repo="nope")

    assert find_checkout_by_identity(ws, target) is None


def test_find_checkout_by_identity_skips_non_git_and_unresolvable_entries(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "not-a-checkout").mkdir()  # no .git at all
    no_origin = ws / "no-origin"
    no_origin.mkdir()
    subprocess.run(["git", "init", "-q", str(no_origin)], check=True)  # .git, no origin
    root = ws / "the-target"
    _init_checkout(root, "git@github.com:andrei-shtanakov/the-target.git")
    target = RepoKey(host="github.com", owner="andrei-shtanakov", repo="the-target")

    assert find_checkout_by_identity(ws, target) == root.resolve()


def test_underscore_prefixed_git_checkout_is_enumerated(tmp_path):
    """A valid checkout named `_service` must be visible (gate pass-4).

    The repository contract permits `_` in directory names; only
    dot-prefixed (genuinely hidden) entries are skipped. Non-git
    directories like `_cowork_output` stay invisible NOT via the name
    filter but because they carry no .git.
    """
    ws = tmp_path / "ws"
    (ws / "_service" / ".git").mkdir(parents=True)
    (ws / "_scratch_no_git").mkdir(parents=True)
    (ws / ".hidden").mkdir(parents=True)
    entries, notes = list_workspace_checkouts(ws)
    names = [name for name, _ in entries]
    assert "_service" in names
    assert ".hidden" not in names
    assert notes == []
