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


def _repo(root: Path, name: str) -> Path:
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
    (repo / "tasks.yaml").write_text("tasks: []\n")
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
