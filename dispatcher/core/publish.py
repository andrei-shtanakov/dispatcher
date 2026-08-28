"""Publisher: this host's workspace snapshot → KB `derived/snapshots/<host>.json`.

The one write path of the sync feature (DESIGN-203), and it writes only into
the KB zone the constitution assigns to tools (prograph-vault#24) — never into
observed repos. The write target is the `derived-snapshots` branch, delivered
through an ephemeral `git worktree` rather than the vault's own checkout: the
vault may sit on any branch with any local changes, and this publisher must
never touch it (spec 2026-08-28-snapshot-publish-branch). Scheduling stays
with the user (cron/launchd ≤ 1 h, README); every failure exits non-zero so a
dead cron is visible, not silent (RK-03).
"""

from __future__ import annotations

import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from dispatcher.core.snapshot_contract import (
    Snapshot,
    SnapshotContractError,
    parse_snapshot,
)
from dispatcher.core.sync import KB_REPO, SAFE_HOST_RE, SNAPSHOT_BRANCH

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


def take_snapshot(
    workspace: Path, *, command: tuple[str, ...] = ("github-checker",)
) -> Snapshot:
    """Full snapshot (gh data when available, git-only otherwise) of *workspace*."""
    out = _run(
        [*command, "snapshot", "--workspace", str(workspace), "--indent", "0"],
        timeout=_SNAPSHOT_TIMEOUT,
    )
    try:
        return parse_snapshot(out)
    except SnapshotContractError as err:
        raise PublishError(
            f"producer output violates the github-checker snapshot contract: {err}"
        ) from err


def write_snapshot(snapshot: Snapshot, snapshots_dir: Path) -> Path:
    """Atomically (re)place `<host>.json`; the filename IS the host identity."""
    host = snapshot.host
    if not SAFE_HOST_RE.fullmatch(host) or host in (".", ".."):
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


_SNAPSHOT_REFSPEC = (
    f"+refs/heads/{SNAPSHOT_BRANCH}:refs/remotes/origin/{SNAPSHOT_BRANCH}"
)
_SNAPSHOT_REF = f"origin/{SNAPSHOT_BRANCH}"
_PUSH_ATTEMPTS = 3
_RETRY = "__retry__"  # внутренний маркер: non-fast-forward, цикл повторяется


def _classify_push(proc: subprocess.CompletedProcess[str]) -> str:
    """'ok' | 'non_fast_forward' | 'fatal' — по porcelain, не по stderr.

    Локализованный stderr нестабилен; `--porcelain` даёт машинный формат
    `!\t<src>:<dst>\t[rejected] (<reason>)`. Retryable — только настоящий
    non-fast-forward; hook/auth/protected-branch не лечатся повтором.
    """
    if proc.returncode == 0:
        return "ok"
    for line in proc.stdout.splitlines():
        if (
            line.startswith("!")
            and "[rejected]" in line
            and ("non-fast-forward" in line or "fetch first" in line)
        ):
            return "non_fast_forward"
    return "fatal"


def _attempt_publish(
    vault_repo: Path,
    snapshot: Snapshot,
    *,
    push: bool,
    attempt: int,
    before_push: Callable[[int], None] | None,
) -> str:
    """Один полный цикл: fetch → worktree → write → commit → push."""
    _run(
        ["git", "-C", str(vault_repo), "fetch", "--quiet", "origin", _SNAPSHOT_REFSPEC],
        timeout=_GIT_TIMEOUT,
    )
    # git worktree add требует несуществующий целевой путь: сам
    # mkdtemp-каталог не годится, worktree живёт в его подпути
    tmp_root = Path(tempfile.mkdtemp(prefix="dispatcher-snapshot-publish-"))
    worktree = tmp_root / "worktree"
    registered = False
    try:
        _run(
            [
                "git",
                "-C",
                str(vault_repo),
                "worktree",
                "add",
                "--detach",
                str(worktree),
                _SNAPSHOT_REF,
            ],
            timeout=_GIT_TIMEOUT,
        )
        registered = True
        target = write_snapshot(snapshot, worktree / "derived" / "snapshots")
        rel = str(target.relative_to(worktree))
        _run(["git", "-C", str(worktree), "add", "--", rel], timeout=_GIT_TIMEOUT)
        status = _run(
            ["git", "-C", str(worktree), "status", "--porcelain", "--", rel],
            timeout=_GIT_TIMEOUT,
        )
        if not status.strip():
            return "no changes"
        if not push:
            # коммит не создаётся: после удаления worktree он был бы
            # недостижим, а "committed" вводил бы в заблуждение
            return "validated; push skipped"
        _run(
            [
                "git",
                "-C",
                str(worktree),
                "commit",
                "-q",
                "-m",
                f"chore(snapshots): {snapshot.host} sync snapshot",
                "--",
                rel,
            ],
            timeout=_GIT_TIMEOUT,
        )
        if before_push is not None:
            before_push(attempt)
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "push",
                    "--porcelain",
                    "origin",
                    f"HEAD:refs/heads/{SNAPSHOT_BRANCH}",
                ],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as err:
            raise PublishError(f"git push: {err}") from err
        kind = _classify_push(proc)
        if kind == "ok":
            return "committed and pushed"
        if kind == "non_fast_forward":
            return _RETRY
        raise PublishError(
            f"push to {SNAPSHOT_BRANCH} rejected: "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    finally:
        _cleanup_worktree(vault_repo, worktree if registered else None, tmp_root)


def _cleanup_worktree(vault_repo: Path, worktree: Path | None, tmp_root: Path) -> None:
    """Строго адресный cleanup: свой worktree и свой tmp_root, ничего чужого.

    `git worktree prune` не используется: глобальная операция могла бы
    подчистить чужое состояние. Сбой remove не маскирует основной результат —
    логируется, затем удаляется только собственный temp-каталог.
    """
    if worktree is not None:
        try:
            proc = subprocess.run(
                [
                    "git",
                    "-C",
                    str(vault_repo),
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree),
                ],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
            if proc.returncode != 0:
                print(
                    f"warning: snapshot worktree cleanup failed: {proc.stderr.strip()}",
                    file=sys.stderr,
                )
        except (FileNotFoundError, subprocess.TimeoutExpired) as err:
            print(
                f"warning: snapshot worktree cleanup failed: {err}",
                file=sys.stderr,
            )
    shutil.rmtree(tmp_root, ignore_errors=True)


def publish_to_branch(
    vault_repo: Path,
    snapshot: Snapshot,
    *,
    push: bool = True,
    attempts: int = _PUSH_ATTEMPTS,
    sleeper: Callable[[float], None] = time.sleep,
    before_push: Callable[[int], None] | None = None,
) -> str:
    """Публикация `<host>.json` в ветку derived-snapshots (спека 2026-08-28).

    Retry — только на non-fast-forward, полным новым циклом от свежего
    fetch; любой другой отказ — немедленный PublishError. *before_push* —
    тестовый шов (вызывается с номером попытки перед push).
    """
    for attempt in range(1, attempts + 1):
        outcome = _attempt_publish(
            vault_repo,
            snapshot,
            push=push,
            attempt=attempt,
            before_push=before_push,
        )
        if outcome != _RETRY:
            return outcome
        if attempt < attempts:
            sleeper(random.uniform(0.5, 2.0))
    raise PublishError(
        f"push to {SNAPSHOT_BRANCH} was not fast-forward after {attempts} attempts"
    )


def publish(
    workspace: Path,
    *,
    command: tuple[str, ...] = ("github-checker",),
    push: bool = True,
    snapshot: Snapshot | None = None,
) -> str:
    """Full pipeline: snapshot → atomic write → `derived-snapshots` branch (+push)."""
    vault_repo = workspace / KB_REPO
    if not (vault_repo / ".git").exists():
        raise PublishError(f"KB repo not found at {vault_repo}")
    snap = (
        snapshot if snapshot is not None else take_snapshot(workspace, command=command)
    )
    outcome = publish_to_branch(vault_repo, snap, push=push)
    return f"{SNAPSHOT_BRANCH}:derived/snapshots/{snap.host}.json: {outcome}"
