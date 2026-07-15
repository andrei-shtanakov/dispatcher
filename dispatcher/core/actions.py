"""Live whitelist actions: pull / open-pr, delegated to github-checker (DESIGN-204).

Dispatcher never mutates observed repos itself — it shells out to the shipped
github-checker headless commands (`pull` is ff-only by construction, `open-pr`
never pushes; github-checker#8). Guards here implement the design's word:
explicit human action only, one in-flight action per repo, an audit line for
every attempt.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from dispatcher.core.discovery import DispatcherConfig

_ACTION_TIMEOUT = 120
_SAFE_DIR_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")

Action = Literal["pull", "open-pr"]
_audit = logging.getLogger("dispatcher.actions")


class ActionOutcome(BaseModel):
    """What one whitelist action did; mirrors github-checker's ActionResult."""

    action: str
    dir: str
    ok: bool
    detail: str | None = None
    error: str | None = None
    pr_url: str | None = None
    local_behind: int | None = None
    local_dirty: bool | None = None


class ActionBusyError(Exception):
    """The repo already has an action in flight (the API turns this into 409)."""


class ActionRejectedError(Exception):
    """Bad target: unsafe name or not a git repo in the workspace (→ 422)."""


class ActionRunner:
    """Serialized executor of whitelist actions over workspace repos."""

    def __init__(
        self,
        config: DispatcherConfig,
        *,
        command: tuple[str, ...] = ("github-checker",),
    ) -> None:
        self._config = config
        self._command = command
        self._lock = threading.Lock()
        self._busy: set[str] = set()

    def _target(self, repo_dir: str) -> Path:
        if not _SAFE_DIR_RE.fullmatch(repo_dir) or repo_dir in (".", ".."):
            raise ActionRejectedError(f"unsafe repo dir: {repo_dir!r}")
        workspace = next((r for r in self._config.roots if r.is_dir()), None)
        if workspace is None:
            raise ActionRejectedError("no existing workspace root configured")
        target = workspace / repo_dir
        if not (target / ".git").exists():
            raise ActionRejectedError(f"not a git repo in workspace: {repo_dir}")
        return target

    def run(self, action: Action, repo_dir: str) -> ActionOutcome:
        """Execute one action; EVERY attempt leaves an audit line —
        including rejected (422) and busy (409) ones."""
        try:
            # runtime-гарантия белого списка, независимая от тайпинга
            if action not in ("pull", "open-pr"):
                raise ActionRejectedError(f"action not whitelisted: {action!r}")
            target = self._target(repo_dir)
            with self._lock:
                if repo_dir in self._busy:
                    raise ActionBusyError(f"{repo_dir}: action already in flight")
                self._busy.add(repo_dir)
        except (ActionRejectedError, ActionBusyError) as err:
            _audit.info("action=%s repo=%s ok=False rejected=%s", action, repo_dir, err)
            raise
        try:
            outcome = self._invoke(action, target)
        finally:
            with self._lock:
                self._busy.discard(repo_dir)
        _audit.info(
            "action=%s repo=%s ok=%s detail=%s error=%s",
            action,
            repo_dir,
            outcome.ok,
            outcome.detail,
            outcome.error,
        )
        return outcome

    def _invoke(self, action: Action, target: Path) -> ActionOutcome:
        argv = [*self._command, action, str(target)]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=_ACTION_TIMEOUT
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as err:
            return ActionOutcome(
                action=action, dir=target.name, ok=False, error=str(err)
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return ActionOutcome(
                action=action,
                dir=target.name,
                ok=False,
                error=proc.stderr.strip() or "github-checker returned no JSON",
            )
        local = data.get("local") or {}
        return ActionOutcome(
            action=action,
            dir=target.name,
            ok=bool(data.get("ok")),
            detail=data.get("detail"),
            error=data.get("error"),
            pr_url=data.get("pr_url"),
            local_behind=local.get("behind"),
            local_dirty=local.get("dirty"),
        )
