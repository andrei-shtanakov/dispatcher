"""Publisher: this host's workspace snapshot → KB `derived/snapshots/<host>.json`.

The one write path of the sync feature (DESIGN-203), and it writes only into
the KB zone the constitution assigns to tools (prograph-vault#24) — never into
observed repos. Scheduling stays with the user (cron/launchd ≤ 1 h, README);
every failure exits non-zero so a dead cron is visible, not silent (RK-03).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from dispatcher.core.snapshot_contract import (
    SnapshotContractError,
    WorkspaceSnapshotV1,
    parse_snapshot,
)
from dispatcher.core.sync import KB_REPO

_SNAPSHOT_TIMEOUT = 300
_GIT_TIMEOUT = 120


class PublishError(Exception):
    """Any failure of the publish pipeline; the CLI turns it into exit 1."""


def _run(argv: list[str], *, timeout: int, cwd: Path | None = None) -> str:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, cwd=cwd
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as err:
        raise PublishError(f"{argv[0]}: {err}") from err
    if proc.returncode != 0:
        raise PublishError(
            f"{' '.join(argv)} failed: {proc.stderr.strip() or proc.returncode}"
        )
    return proc.stdout


# hostnames: letters/digits/dot/hyphen/underscore — anything else could
# escape snapshots_dir when used as a filename component
_SAFE_HOST_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")  # без ведущего дефиса


def take_snapshot(
    workspace: Path, *, command: tuple[str, ...] = ("github-checker",)
) -> WorkspaceSnapshotV1:
    """Full snapshot (gh data when available, git-only otherwise) of *workspace*."""
    out = _run(
        [*command, "snapshot", "--workspace", str(workspace), "--indent", "0"],
        timeout=_SNAPSHOT_TIMEOUT,
    )
    try:
        return parse_snapshot(out)
    except SnapshotContractError as err:
        raise PublishError(f"producer output violates contract v1: {err}") from err


def write_snapshot(snapshot: WorkspaceSnapshotV1, snapshots_dir: Path) -> Path:
    """Atomically (re)place `<host>.json`; the filename IS the host identity."""
    host = snapshot.host
    if not _SAFE_HOST_RE.fullmatch(host) or host in (".", ".."):
        raise PublishError(f"unsafe host name for a filename: {host!r}")
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    target = snapshots_dir / f"{host}.json"
    payload = snapshot.model_dump_json(indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(dir=snapshots_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, target)
    except OSError as err:
        Path(tmp_name).unlink(missing_ok=True)
        raise PublishError(f"cannot write {target}: {err}") from err
    return target


def commit_and_push(vault_repo: Path, target: Path, *, push: bool = True) -> str:
    """Commit the snapshot into the KB repo; rebase-and-push unless *push* is off.

    Per-host files never conflict with each other, so `pull --rebase` only
    reconciles the branch pointer when several machines publish concurrently.
    """
    try:
        rel = target.relative_to(vault_repo)
    except ValueError as err:
        raise PublishError(
            f"snapshot {target} is outside the KB repo {vault_repo}"
        ) from err
    _run(["git", "-C", str(vault_repo), "add", "--", str(rel)], timeout=_GIT_TIMEOUT)
    status = _run(
        ["git", "-C", str(vault_repo), "status", "--porcelain", "--", str(rel)],
        timeout=_GIT_TIMEOUT,
    )
    if not status.strip():
        return "no changes"
    _run(
        [
            "git",
            "-C",
            str(vault_repo),
            "commit",
            "-q",
            "-m",
            f"chore(snapshots): {target.stem} sync snapshot",
            "--",
            str(rel),
        ],
        timeout=_GIT_TIMEOUT,
    )
    if not push:
        return "committed (push skipped)"
    _run(["git", "-C", str(vault_repo), "pull", "--rebase", "-q"], timeout=_GIT_TIMEOUT)
    _run(["git", "-C", str(vault_repo), "push", "-q"], timeout=_GIT_TIMEOUT)
    return "committed and pushed"


def publish(
    workspace: Path,
    *,
    command: tuple[str, ...] = ("github-checker",),
    push: bool = True,
    snapshot: WorkspaceSnapshotV1 | None = None,
) -> str:
    """Full pipeline: snapshot → atomic write → KB commit (+push)."""
    vault_repo = workspace / KB_REPO
    if not (vault_repo / ".git").exists():
        raise PublishError(f"KB repo not found at {vault_repo}")
    snap = (
        snapshot if snapshot is not None else take_snapshot(workspace, command=command)
    )
    target = write_snapshot(snap, vault_repo / "derived" / "snapshots")
    outcome = commit_and_push(vault_repo, target, push=push)
    return f"{target}: {outcome}"
