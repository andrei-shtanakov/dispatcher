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
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from dispatcher.core.discovery import DispatcherConfig

_ACTION_TIMEOUT = 120
_SAFE_DIR_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")
# C0 controls plus DEL. None of them belong in a value that becomes its own
# argv element: NUL is the sharp case (`subprocess.run` refuses argv containing
# one, and JSON permits it in a string, so it arrives straight off the wire),
# but a newline or an ESC in a slug or an issue title is equally meaningless
# and equally capable of confusing whatever reads the audit log afterwards.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")

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
    matches: list[dict[str, Any]] | None = None
    malformed: list[dict[str, Any]] | None = None
    created: bool | None = None
    issue: dict[str, Any] | None = None


class ActionBusyError(Exception):
    """The repo already has an action in flight (the API turns this into 409)."""


class ActionRejectedError(Exception):
    """Bad target: unsafe name or not a git repo in the workspace (→ 422)."""


def reject_control_chars(**fields: str) -> None:
    """Layer 1 of the argv defence: refuse control characters by name.

    Every value here becomes a structural argv element (`--slug <slug>`,
    `--title <title>`, …). A NUL makes `subprocess.run` raise before it even
    forks, which used to escape as a 500 with no audit line at all; the rest
    of the C0 range is simply not meaningful in a slug or a one-line title.

    This is the INTENDED refusal, and it is deliberately not the only one:
    `_invoke` still catches `ValueError` around `subprocess.run` regardless
    of whether this ran (layer 2). A validator only covers the fields someone
    remembered to list, and the next field added to an argv is exactly the
    one that will be forgotten — so the boundary keeps its own guard rather
    than trusting that this one was called.
    """
    for name, value in fields.items():
        found = _CONTROL_RE.search(value)
        if found is not None:
            raise ActionRejectedError(
                f"{name} contains a control character "
                f"(U+{ord(found.group()):04X}); it must not"
            )


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
        if outcome.created is not None:
            # `is not None`, not truthiness: created=False is the idempotent
            # case (slug already existed) — precisely the one D1a-4 needs
            # visible in the audit line, and truthiness would drop it
            merge_fields += f" created={outcome.created}"
        _audit.info(
            "action=%s repo=%s ok=%s%s detail=%s error=%s",
            action,
            repo_dir,
            outcome.ok,
            merge_fields,
            outcome.detail,
            outcome.error,
        )

    def _invoke(
        self,
        action: str,
        target: Path,
        *extra: str,
        refusal_created: bool | None = None,
    ) -> ActionOutcome:
        """Run one github-checker verb and parse its answer.

        *refusal_created* is the `created` value to stamp when the launch is
        refused BEFORE anything runs (see the `ValueError` branch); callers
        that mutate pass `False`, read-only callers leave it unknown.
        """
        argv = [*self._command, action, str(target), *extra]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=_ACTION_TIMEOUT
            )
        except ValueError as err:
            # subprocess validates argv BEFORE it forks and raises ValueError
            # for anything it cannot pass to exec — reachably, an embedded NUL,
            # which JSON permits in a string (as the escape `\u0000`) and which therefore
            # arrives straight off the wire. Uncaught it escaped as a 500 and,
            # worse, left NO audit line at all, breaking this module's stated
            # per-attempt guarantee and ADR-ECO-004a D1a-4 with it.
            #
            # Classified as a pre-mutation REFUSAL, not an unknown: the check
            # happens before process creation, so no verb ran, nothing was
            # mutated, and there is nothing to be uncertain about. That is the
            # same reasoning as the body-write failure in request_task below —
            # `created=None` would claim we cannot tell whether an issue was
            # filed, when we know for certain none was.
            return ActionOutcome(
                action=action,
                dir=target.name,
                ok=False,
                created=refusal_created,
                error=f"refused before launch: {err}",
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            # N-3: widened from `FileNotFoundError` (an OSError subclass, so
            # this is strictly wider, not a replacement). Any other OSError on
            # exec — E2BIG "Argument list too long" from an oversized
            # client-supplied argument such as `--if-head`, EACCES, ENOMEM —
            # raised straight out of here: a 500 with ZERO audit lines, the
            # same guarantee break as F-1. The producer side of this contract
            # already widened its equivalent catch to `(OSError,
            # TimeoutExpired)`; we fixed one end and left the other.
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
        if not isinstance(data, dict):
            return ActionOutcome(
                action=action,
                dir=target.name,
                ok=False,
                error="github-checker returned JSON that is not an object",
            )
        local = data.get("local")
        if not isinstance(local, dict):
            local = {}
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
                matches=data.get("matches"),
                malformed=data.get("malformed"),
                created=data.get("created"),
                issue=data.get("issue"),
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

    def issue_lookup(self, repo_dir: str, slug: str) -> ActionOutcome:
        """Ask whether a slug already has an inbox issue. A read takes no lock."""
        try:
            target = self._target(repo_dir)
            reject_control_chars(slug=slug)
        except ActionRejectedError as err:
            # slug is %r, not %s: a rejected slug can contain the very control
            # characters that made it a rejection, and writing them raw into
            # the audit trail is how a log line stops being readable
            _audit.info(
                "action=issue-lookup repo=%s slug=%r ok=False rejected=%s",
                repo_dir,
                slug,
                err,
            )
            raise
        outcome = self._invoke("issue-lookup", target, "--slug", slug)
        # `len(matches or [])` printed the one value that means "could not
        # read the inbox" as `matches=0`, i.e. as "read it, confirmed empty" —
        # the exact unknown-vs-empty collapse the rest of this feature exists
        # to prevent, reproduced in the audit trail where nobody would see it.
        matches = outcome.matches
        # not _audit_outcome: that helper's format has no room for the slug,
        # and the slug is the whole point of this line
        _audit.info(
            # %r on the slug here too: the success path logs whatever the
            # client sent, and a slug that merely LOOKS ordinary can still
            # carry characters that break a log line
            "action=issue-lookup repo=%s slug=%r ok=%s matches=%s",
            repo_dir,
            slug,
            outcome.ok,
            len(matches) if isinstance(matches, list) else "unknown",
        )
        return outcome

    def request_task(
        self, repo_dir: str, *, slug: str, sender: str, title: str, prose: str
    ) -> ActionOutcome:
        """File an inbox issue in *repo_dir*, holding the repo's lock.

        github-checker's `issue-create` re-checks for an existing slug and
        reads the result back inside one verb, so a single guarded call
        covers the whole lookup → create → read-back sequence.

        `created` is passed through untouched: `true` / `false` / `None`
        mean created / not created / unknown, and flattening `None` into
        `false` would tell the operator a request does not exist when it
        may well do.

        Writing *prose* can itself fail before any subprocess ever runs: a
        lone UTF-16 surrogate survives `json.loads` (JSON permits unpaired
        `\\uXXXX` escapes) but cannot be encoded to UTF-8, and a full disk
        raises the same shape of error. Since nothing was attempted, that is
        a pre-mutation refusal exactly like a failed pre-create lookup —
        reported as `created=False`, not an escaping exception and not
        `created=None` (`None` would claim we don't know whether something
        was attempted, when we know nothing was).
        """
        # Layer 1, before the lock: a request this malformed must not occupy
        # the repo, and — the point of F-1 — must not vanish from the log.
        try:
            reject_control_chars(slug=slug, sender=sender, title=title)
        except ActionRejectedError as err:
            _audit.info(
                "action=request-task repo=%s ok=False created=False rejected=%s",
                repo_dir,
                err,
            )
            raise
        with self._hold("request-task", repo_dir) as target:
            # the name must be recorded before anything can fail with the
            # file already open, so every exit below — success, a broken
            # create call, or a failure while creating/writing the file
            # itself — can still find it, unlink it, and still produce an
            # outcome to audit (a leaked file *and* a silent, unaudited
            # attempt is the failure mode this guards against)
            body_file: str | None = None
            try:
                # prose is multi-line; argv is where newlines and quoting die
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".md", delete=False, encoding="utf-8"
                ) as handle:
                    body_file = handle.name
                    handle.write(prose)
                outcome = self._invoke(
                    "issue-create",
                    target,
                    "--slug",
                    slug,
                    "--from",
                    sender,
                    "--title",
                    title,
                    "--body-file",
                    body_file,
                    refusal_created=False,
                )
            except (OSError, UnicodeEncodeError) as err:
                outcome = ActionOutcome(
                    action="issue-create",
                    dir=target.name,
                    ok=False,
                    created=False,
                    error=f"could not write the request body: {err}",
                )
            finally:
                if body_file is not None:
                    Path(body_file).unlink(missing_ok=True)
        outcome.action = "request-task"
        self._audit_outcome("request-task", repo_dir, outcome)
        return outcome
