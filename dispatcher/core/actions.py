"""Sync + merge-gate actions, delegated to github-checker (DESIGN-204).

This module never writes file content itself — it shells out to the shipped
github-checker headless commands (`pull` is ff-only by construction, `open-pr`
never pushes; github-checker#8). `merge` is constrained the same way but by a
different mechanism: github-checker's own fail-closed gate refuses anything
not clean, and `merge` is absent from `_WHITELIST`, so it is reachable only
through `merge_and_sync`, never through `run()`. Guards here implement the
design's word: explicit human action only, one in-flight action per repo, an
audit line for every attempt.

`merge_and_sync` composes `merge` and `post-merge-sync` under a single lock
hold — the two steps must not let another action wedge into the gap between
merging and re-syncing the local clone. `ok` follows the merge step, not the
local sync: a merged PR is finished work regardless of whether the clone
afterwards refuses to update, and unlike the merge itself, a bad sync can be
retried. `merged` is always whatever `_invoke` established — `True`/`False`
when github-checker answered, `None` when we never got a readable answer at
all (a transport failure). Neither direction is ever asserted locally:
claiming a merge, or claiming a non-merge, states a certainty we don't have.
`pr_detail` is a read and takes no lock.

A second, independent action class — content-PR actions, where dispatcher
itself renders a scoped diff before handing off to github-checker — lives in
`core/spec_runner_config_actions.py` (DESIGN-304, resolves X-02). The two
classes are deliberately not merged; see that module's docstring.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from dispatcher.core.discovery import DispatcherConfig

_ACTION_TIMEOUT = 120
_SAFE_DIR_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")

Action = Literal["pull", "open-pr", "post-merge-sync"]
_WHITELIST = frozenset({"pull", "open-pr", "post-merge-sync"})
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
    branch: str | None = None
    base_branch: str | None = None
    commit_sha: str | None = None
    changed_paths: list[str] | None = None
    merged: bool | None = None
    local_sync: str | None = None  # ok | failed | not_attempted | not_applicable
    gate_failed: list[str] | None = None
    pr_detail: dict[str, Any] | None = None


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

    @contextmanager
    def _hold(self, action: str, repo_dir: str) -> Iterator[Path]:
        """Own the repo for a whole operation — composite actions included.

        The lock must span every step: releasing between merge and sync
        would let another action wedge into the gap.
        """
        try:
            target = self._target(repo_dir)
            with self._lock:
                if repo_dir in self._busy:
                    raise ActionBusyError(f"{repo_dir}: action already in flight")
                self._busy.add(repo_dir)
        except (ActionRejectedError, ActionBusyError) as err:
            _audit.info("action=%s repo=%s ok=False rejected=%s", action, repo_dir, err)
            raise
        try:
            yield target
        finally:
            with self._lock:
                self._busy.discard(repo_dir)

    def run(self, action: Action, repo_dir: str) -> ActionOutcome:
        """Execute one whitelist action; EVERY attempt leaves an audit line —
        including rejected (422) and busy (409) ones."""
        # runtime-гарантия белого списка, независимая от тайпинга; проверяется
        # до захвата лока, чтобы неразрешённое действие не могло занять репо
        if action not in _WHITELIST:
            _audit.info(
                "action=%s repo=%s ok=False rejected=not whitelisted",
                action,
                repo_dir,
            )
            raise ActionRejectedError(f"action not whitelisted: {action!r}")
        with self._hold(action, repo_dir) as target:
            outcome = self._invoke(action, target)
        self._audit_outcome(action, repo_dir, outcome)
        return outcome

    def _audit_outcome(
        self, action: str, repo_dir: str, outcome: ActionOutcome
    ) -> None:
        # merged/local_sync only carry meaning for the merge-gate composite;
        # a plain pull/open-pr line stays exactly as terse as before it existed
        merge_fields = ""
        if outcome.merged is not None or outcome.local_sync is not None:
            merge_fields = f" merged={outcome.merged} local_sync={outcome.local_sync}"
        _audit.info(
            "action=%s repo=%s ok=%s%s detail=%s error=%s",
            action,
            repo_dir,
            outcome.ok,
            merge_fields,
            outcome.detail,
            outcome.error,
        )

    def _invoke(self, action: str, target: Path, *extra: str) -> ActionOutcome:
        argv = [*self._command, action, str(target), *extra]
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
        try:
            return ActionOutcome(
                action=action,
                dir=target.name,
                ok=bool(data.get("ok")),
                detail=data.get("detail"),
                error=data.get("error"),
                pr_url=data.get("pr_url"),
                local_behind=local.get("behind"),
                local_dirty=local.get("dirty"),
                merged=data.get("merged"),
                local_sync=data.get("local_sync"),
                gate_failed=data.get("gate_failed"),
                pr_detail=data.get("pr_detail"),
            )
        except ValidationError as err:
            # the pr_detail PAYLOAD is validated by the console; the envelope
            # around it had no boundary at all — a wrong-typed field raised
            # out of here, so a merge subprocess that genuinely ran left no
            # audit line, breaking this module's stated guarantee
            return ActionOutcome(
                action=action,
                dir=target.name,
                ok=False,
                error=(
                    "github-checker returned an unparseable envelope: "
                    # one audit line per attempt: pydantic's report is
                    # multi-line, and its detail is what makes the drift
                    # diagnosable, so flatten rather than truncate
                    + " ".join(str(err).split())
                ),
            )

    def merge_and_sync(self, repo_dir: str, pr: int, if_head: str) -> ActionOutcome:
        """Merge one PR and re-sync the clone, holding the repo lock throughout.

        `ok` follows the merge step: a merged PR is finished work even when the
        local sync afterwards refuses, and it cannot be retried — the warning
        rides on `local_sync` instead of flipping the operation to failed.
        `merged` is propagated from github-checker on both paths and never
        synthesized here: `False` only when it actually answered (a parsed gate
        refusal), `None` on a transport failure — unknown, not a claimed
        non-merge, and equally not a claimed merge.
        """
        with self._hold("merge-and-sync", repo_dir) as target:
            merge = self._invoke("merge", target, str(pr), "--if-head", if_head)
            self._audit_outcome("merge", repo_dir, merge)
            if not merge.ok:
                merge.action = "merge-and-sync"
                # merge.merged is left as _invoke set it: False for a parsed
                # gate refusal (github-checker read the PR and said no), None
                # for a transport failure (timeout/missing binary/unparseable
                # output) — we never learned whether it merged, so it stays
                # unknown rather than a claimed False
                merge.local_sync = "not_attempted"
                self._audit_outcome("merge-and-sync", repo_dir, merge)
                return merge
            sync = self._invoke("post-merge-sync", target)
            self._audit_outcome("post-merge-sync", repo_dir, sync)

        outcome = ActionOutcome(
            action="merge-and-sync",
            dir=merge.dir,
            ok=merge.ok,  # the merge step's verdict, never the local sync's
            # propagated, not asserted: github-checker stamps `merged` on every
            # merge answer, and a producer that stopped doing so must surface
            # as unknown — claiming a merge we were not told about is the same
            # defect as claiming a non-merge we were not told about
            merged=merge.merged,
            local_sync=sync.local_sync or ("ok" if sync.ok else "failed"),
            detail=merge.detail,
            error=sync.error,
            pr_url=merge.pr_url,
            branch=sync.branch,
            local_behind=sync.local_behind,
            local_dirty=sync.local_dirty,
        )
        self._audit_outcome("merge-and-sync", repo_dir, outcome)
        return outcome

    def pr_detail(self, repo_dir: str, pr: int) -> ActionOutcome:
        """Read one PR through github-checker. A read takes no lock."""
        try:
            target = self._target(repo_dir)
        except ActionRejectedError as err:
            _audit.info(
                "action=pr-detail repo=%s pr=%s ok=False rejected=%s",
                repo_dir,
                pr,
                err,
            )
            raise
        outcome = self._invoke("pr-detail", target, str(pr))
        _audit.info("action=pr-detail repo=%s pr=%s ok=%s", repo_dir, pr, outcome.ok)
        return outcome
