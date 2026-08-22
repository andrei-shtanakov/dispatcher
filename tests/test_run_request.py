"""RunRequest v0 validation (spec §4)."""

import subprocess
from pathlib import Path

import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.run_request import (
    RunRejectedError,
    RunRequest,
    validate_request,
)

_SHA_A = "a" * 40


def _repo(root: Path, name: str, *, repo_line: str | None = None) -> Path:
    """A checkout with a `tasks.yaml` naming ITSELF via `repo:` (C1).

    `repo_line` overrides the default self-reference, for tests that need a
    tasks.yaml naming a repository other than (or missing from) the one
    that will actually be checked out.
    """
    repo = root / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            f"git@github.com:owner/{name}.git",
        ],
        check=True,
    )
    line = repo_line if repo_line is not None else f"repo: {repo}\n"
    (repo / "tasks.yaml").write_text(f"tasks: []\n{line}")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )
    return repo


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _request(**over: object) -> RunRequest:
    base: dict[str, object] = {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "work_id": "todo://deployer/entrypoint-token-boundary-match",
        "repository": "deployer",
        "revision": _SHA_A,
        "tasks": "tasks.yaml",
    }
    base.update(over)
    return RunRequest(**base)  # type: ignore[arg-type]


def test_accepts_a_well_formed_request(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "deployer")
    config = DispatcherConfig(roots=(tmp_path,))
    validated = validate_request(_request(revision=_head(repo)), config)
    assert validated.checkout == repo
    assert validated.key.as_path_parts() == ("github.com", "owner", "deployer")


def test_revision_must_be_a_full_sha(tmp_path: Path) -> None:
    _repo(tmp_path, "deployer")
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="40-hex"):
        validate_request(_request(revision="HEAD"), config)


def test_unknown_repository_is_refused(tmp_path: Path) -> None:
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="not a git repo"):
        validate_request(_request(repository="nope"), config)


def test_unsafe_repository_name_is_refused(tmp_path: Path) -> None:
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="unsafe"):
        validate_request(_request(repository="../etc"), config)


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside.yaml"])
def test_tasks_path_must_be_repo_relative(tmp_path: Path, bad: str) -> None:
    repo = _repo(tmp_path, "deployer")
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="repo-relative"):
        validate_request(_request(revision=_head(repo), tasks=bad), config)


def test_tasks_must_exist_in_that_revision(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "deployer")
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="not present in"):
        validate_request(_request(revision=_head(repo), tasks="missing.yaml"), config)


def test_tasks_yaml_naming_a_different_repo_is_refused(tmp_path: Path) -> None:
    """C1: maestro keys the run by `tasks.yaml`'s own `repo:`, not by
    `request.repository` — a mismatch must be refused before anything
    launches, or the lock/watch/revision-guard all govern the wrong repo."""
    other = _repo(tmp_path, "other")
    repo = _repo(tmp_path, "deployer", repo_line=f"repo: {other}\n")
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="names repository"):
        validate_request(_request(revision=_head(repo)), config)


def test_tasks_yaml_naming_the_same_repo_passes(tmp_path: Path) -> None:
    """The mirror image of the mismatch case: `repo:` pointing back at the
    requested checkout is accepted, same as before C1 existed."""
    repo = _repo(tmp_path, "deployer")
    config = DispatcherConfig(roots=(tmp_path,))
    validated = validate_request(_request(revision=_head(repo)), config)
    assert validated.key.as_path_parts() == ("github.com", "owner", "deployer")


def test_tasks_yaml_naming_the_same_repo_via_repo_url_passes(tmp_path: Path) -> None:
    repo = _repo(
        tmp_path,
        "deployer",
        repo_line="repo_url: git@github.com:owner/deployer.git\n",
    )
    config = DispatcherConfig(roots=(tmp_path,))
    validated = validate_request(_request(revision=_head(repo)), config)
    assert validated.key.as_path_parts() == ("github.com", "owner", "deployer")


def test_tasks_yaml_with_no_repo_field_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "deployer", repo_line="")
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="names no repository"):
        validate_request(_request(revision=_head(repo)), config)


def test_tasks_yaml_with_unresolvable_repo_field_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "deployer", repo_line=f"repo: {tmp_path / 'nowhere'}\n")
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="unresolvable"):
        validate_request(_request(revision=_head(repo)), config)


def test_owner_traversal_in_origin_is_refused_not_a_500(tmp_path: Path) -> None:
    """I4: a traversal in `origin` is a refusal, never an unhandled 500.

    What catches it moved: before the re-pin to maestro 95e5b3f the parser
    accepted `owner='..'` and only `safe_path_parts` stood in the way; now
    `parse_remote_url` refuses the segment itself. Either way the property
    under test is the same one — `validate_request` translates it into a
    refusal rather than letting `IdentityError` escape `submit()` — and it
    is asserted here rather than assuming which layer fires. The
    directly-built-key path that only `safe_path_parts` can catch is
    covered by `test_safe_path_parts_still_guards_a_directly_built_key`.
    """
    repo = tmp_path / "deployer"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "remote",
            "add",
            "origin",
            "git@github.com:owner/../etc.git",
        ],
        check=True,
    )
    (repo / "tasks.yaml").write_text(f"tasks: []\nrepo: {repo}\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "init",
        ],
        check=True,
    )
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="unsafe path segment"):
        validate_request(_request(revision=_head(repo)), config)


def test_checkout_must_already_be_at_the_revision(tmp_path: Path) -> None:
    """Slice 0 refuses rather than moving a neighbour's checkout."""
    repo = _repo(tmp_path, "deployer")
    (repo / "later.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-qm",
            "second",
        ],
        check=True,
    )
    first = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD~1"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="checkout is at"):
        validate_request(_request(revision=first), config)


def test_ref_commit_defaults_to_revision(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "deployer")
    head = _head(repo)
    request = _request(revision=head, spec_ref={"path": "docs/s.md"})
    assert request.spec_ref is not None
    assert request.spec_ref.commit is None
    validated = validate_request(request, DispatcherConfig(roots=(tmp_path,)))
    assert validated.spec_commit == head


def test_git_exception_becomes_run_rejected_error(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Git timeout/missing binary raises RunRejectedError, not raw exception."""
    repo = _repo(tmp_path, "deployer")
    head = _head(repo)

    original_run = subprocess.run
    call_count = [0]

    def mock_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        call_count[0] += 1
        # Let identity setup succeed (first call), then fail on rev-parse
        if call_count[0] <= 1:
            return original_run(*args, **kwargs)
        raise subprocess.TimeoutExpired("git", 15)

    monkeypatch.setattr("dispatcher.core.run_request.subprocess.run", mock_run)
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="cannot run git"):
        validate_request(_request(revision=head), config)


def test_spec_ref_explicit_commit_is_preserved(tmp_path: Path) -> None:
    """Explicitly set spec_ref.commit is preserved; not normalized to revision."""
    repo = _repo(tmp_path, "deployer")
    head = _head(repo)
    different_sha = "b" * 40
    request = _request(
        revision=head, spec_ref={"path": "docs/s.md", "commit": different_sha}
    )
    assert request.spec_ref is not None
    assert request.spec_ref.commit == different_sha
    validated = validate_request(request, DispatcherConfig(roots=(tmp_path,)))
    assert validated.spec_commit == different_sha
    assert validated.spec_commit != head
