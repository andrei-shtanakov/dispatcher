"""TASK-204: publisher — atomic write, KB commit, contract-valid output."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dispatcher.core.publish import (
    PublishError,
    commit_and_push,
    publish,
    take_snapshot,
    write_snapshot,
)
from dispatcher.core.snapshot_contract import WorkspaceSnapshotV1, parse_snapshot

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def make_snapshot(host: str = "mac-a") -> WorkspaceSnapshotV1:
    return WorkspaceSnapshotV1(
        schema_version=1,
        workspace="/ws",
        host=host,
        generated_at=NOW,
        gh_error=None,
        repos=[
            {
                "dir": "alpha",
                "remote": "o/alpha",
                "local": {
                    "branch": "master",
                    "ahead": 0,
                    "behind": 0,
                    "dirty": False,
                    "error": None,
                },
                "github": None,
            }
        ],
    )


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )


def make_vault(root: Path) -> Path:
    vault = root / "prograph-vault"
    vault.mkdir(parents=True)
    _git(vault, "init", "-q", "-b", "master")
    _git(vault, "config", "user.email", "t@example.com")
    _git(vault, "config", "user.name", "t")
    (vault / "README.md").write_text("kb\n")
    _git(vault, "add", "README.md")
    _git(vault, "commit", "-q", "-m", "init")
    return vault


def test_write_snapshot_is_atomic_and_named_by_host(tmp_path: Path) -> None:
    target = write_snapshot(make_snapshot("mac-a"), tmp_path)
    assert target.name == "mac-a.json"
    assert not list(tmp_path.glob("*.tmp"))
    # выход валиден против вендоренного контракта v1 (TASK-201)
    reparsed = parse_snapshot(target.read_text())
    assert reparsed.host == "mac-a"
    assert reparsed.schema_version == 1


def test_write_snapshot_overwrites_in_place(tmp_path: Path) -> None:
    write_snapshot(make_snapshot(), tmp_path)
    second = make_snapshot()
    second.gh_error = "changed"
    target = write_snapshot(second, tmp_path)
    assert json.loads(target.read_text())["gh_error"] == "changed"
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_commit_records_snapshot_and_skips_noop(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    target = write_snapshot(make_snapshot(), vault / "derived" / "snapshots")

    outcome = commit_and_push(vault, target, push=False)
    assert outcome == "committed (push skipped)"
    log = subprocess.run(
        ["git", "-C", str(vault), "log", "--oneline"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "chore(snapshots): mac-a" in log

    # тот же контент → честное "no changes", без пустого коммита
    write_snapshot(make_snapshot(), vault / "derived" / "snapshots")
    assert commit_and_push(vault, target, push=False) == "no changes"


def test_publish_pipeline_with_injected_snapshot(tmp_path: Path) -> None:
    make_vault(tmp_path)
    outcome = publish(tmp_path, push=False, snapshot=make_snapshot())
    assert "mac-a.json" in outcome
    assert "committed" in outcome


def test_publish_pushes_to_origin(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "master")
    vault = make_vault(tmp_path)
    _git(vault, "remote", "add", "origin", str(origin))
    _git(vault, "push", "-q", "-u", "origin", "master")

    outcome = publish(tmp_path, push=True, snapshot=make_snapshot())
    assert outcome.endswith("committed and pushed")
    remote_log = subprocess.run(
        ["git", "-C", str(origin), "log", "--oneline"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "chore(snapshots): mac-a" in remote_log


def test_write_snapshot_rejects_traversal_host(tmp_path: Path) -> None:
    evil = make_snapshot()
    evil.host = "../escape"
    with pytest.raises(PublishError, match="unsafe host"):
        write_snapshot(evil, tmp_path / "snapshots")
    assert not (tmp_path / "escape.json").exists()


def test_write_snapshot_rejects_leading_hyphen_host(tmp_path: Path) -> None:
    evil = make_snapshot()
    evil.host = "-rf"
    with pytest.raises(PublishError, match="unsafe host"):
        write_snapshot(evil, tmp_path / "snapshots")


def test_commit_outside_vault_is_publish_error(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    stray = tmp_path / "elsewhere.json"
    stray.write_text("{}")
    with pytest.raises(PublishError, match="outside the KB repo"):
        commit_and_push(vault, stray, push=False)


def test_publish_without_kb_repo_fails(tmp_path: Path) -> None:
    with pytest.raises(PublishError, match="KB repo not found"):
        publish(tmp_path, push=False, snapshot=make_snapshot())


def test_take_snapshot_missing_producer_fails(tmp_path: Path) -> None:
    with pytest.raises(PublishError):
        take_snapshot(tmp_path, command=("definitely-not-a-binary",))


def test_take_snapshot_rejects_contract_violation(tmp_path: Path) -> None:
    # «продюсер», выдающий v2 — публиковать такое нельзя
    bad = json.dumps(
        {**json.loads(make_snapshot().model_dump_json()), "schema_version": 2}
    )
    script = tmp_path / "fake.py"
    script.write_text(f"import sys; sys.stdout.write({bad!r})")
    with pytest.raises(PublishError, match="contract"):
        take_snapshot(tmp_path, command=("python3", str(script), "--ignored"))
