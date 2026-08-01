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

import logging
import re
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from dispatcher.core.contract import (
    ActionPayload,
    ContractViolation,
    Ingested,
    ingest,
)
from dispatcher.core.discovery import DispatcherConfig

_ACTION_TIMEOUT = 120
_MAX_STDERR_LEN = 200
_SAFE_DIR_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")
# C0 controls plus DEL. None of them belong in a value that becomes its own
# argv element: NUL is the sharp case (`subprocess.run` refuses argv containing
# one, and JSON permits it in a string, so it arrives straight off the wire),
# but a newline or an ESC in a slug or an issue title is equally meaningless
# and equally capable of confusing whatever reads the audit log afterwards.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
# The three phases an outcome can be decided in. They are a property of WHERE
# in _invoke the decision was made, not of which exception was raised.
PHASE_PRE_LAUNCH = "pre_launch"
PHASE_LAUNCHED_UNREADABLE = "launched_unreadable"
PHASE_READABLE = "readable_result"

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
    # Which side of the fork this outcome was decided on. Not cosmetic: it is
    # what stops "nothing ran" and "it ran and we could not read the answer"
    # from being told apart by Python's exception hierarchy, which is the
    # inference that produced the created=False-on-a-completed-run bug.
    phase: str | None = None  # pre_launch | launched_unreadable | readable_result


_PLAIN_PROJECTED = (
    "detail",
    "error",
    "pr_url",
    "branch",
    "base_branch",
    "commit_sha",
    "changed_paths",
    "merged",
    "local_sync",
    "gate_failed",
    "created",
)


def project_outcome(ingested: Ingested, *, action: str, dir_name: str) -> ActionOutcome:
    """The legacy HTTP DTO, projected from one typed producer answer.

    `ActionOutcome` is the `response_model` of eight endpoints, so its
    field set is the wire contract the SPA and the VS Code extension read.
    It is therefore a *DTO*, not the canonical producer model, and this is
    where the two are kept apart: `ingest` decides what to believe, and
    nothing here re-decides it — no validation, no interpretation, no
    second parse.

    The rule that makes the projection faithful is that it copies only
    fields the producer actually **set**. `.get(name)` returning `None` is
    indistinguishable from the producer sending `null`, and that
    distinction — absent means the verb has no such concept, `null` means
    applicable but unknown — is the entire reason the producer workstream
    happened. So absence stays absence, in this model's
    ``model_fields_set`` too.

    (What crosses the wire is a separate question with a separate answer:
    no route sets ``response_model_exclude_unset``, so unset fields still
    serialise as explicit nulls. Turning that on would be a wire change
    for the SPA and the extension, so it is pinned by a golden test rather
    than done here as a side effect.)
    """
    fields: dict[str, Any] = {
        "action": action,  # the requested verb; the producer's is diagnostic
        "dir": dir_name,
        "ok": ingested.ok,
        "phase": PHASE_READABLE,
    }
    sent = ingested.model_fields_set
    if not isinstance(ingested, ActionPayload):
        # cli_error / contract_error are not a ninth verb: they carry no
        # verb payload, so only the shared diagnostic fields project.
        if "error" in sent:
            fields["error"] = ingested.error
        return ActionOutcome(**fields)

    for name in _PLAIN_PROJECTED:
        if name in sent:
            fields[name] = getattr(ingested, name)
    for name in ("pr_detail", "issue"):
        if name in sent:
            nested = getattr(ingested, name)
            # `exclude_unset` for the same reason as above, one level
            # down: a full dump would refill the nested optionals the
            # producer omitted, and this field used to reach the wire as
            # the producer's own dict.
            fields[name] = (
                None if nested is None else nested.model_dump(exclude_unset=True)
            )
    for name in ("matches", "malformed"):
        if name in sent:
            # `exclude_unset` here is knowingly unpinned: `issue_ref`
            # declares all six of its fields required, so today it can
            # make no difference and no test can catch its removal. It
            # stays for the same reason it is right above — the contract's
            # own README blesses additive evolution within v1, and the
            # first optional field added to `issue_ref` is the moment a
            # full dump starts inventing nulls.
            listed = getattr(ingested, name)
            fields[name] = (
                None
                if listed is None
                else [item.model_dump(exclude_unset=True) for item in listed]
            )
    if "local" in sent and ingested.local is not None and ingested.local.error is None:
        # Two consumer columns over one producer fact. With no `local` on
        # the wire there is no fact to project, and `local_behind=None`
        # would read as "read, and not behind".
        #
        # `local.error` is the same distinction one column over: `dirty`
        # is required by the schema, so a clone that could not be read
        # still carries `dirty: false` — the field's floor, not a reading.
        # Projecting it would turn "could not look" into "looked, and it
        # is clean". The failure itself is not lost: it is the envelope's
        # own `error` that describes it, and that field does project.
        fields["local_behind"] = ingested.local.behind
        fields["local_dirty"] = ingested.local.dirty
    return ActionOutcome(**fields)


def one_line(text: str | None) -> str | None:
    """Collapse producer text to a single line for the audit log.

    Public because both runners log producer-derived `detail`/`error`, and
    the guarantee they state — one audit line per attempt — is a property
    of the log as a whole, not of whichever module remembered to flatten.
    """
    return None if text is None else " ".join(text.split())


def unusable_answer(reason: str) -> str:
    """The error text for a producer answer that cannot be used."""
    return f"github-checker returned an unusable answer: {reason}"


def consumer_failure(err: BaseException) -> str:
    """The error text for *our* failure to interpret a producer answer.

    Named apart from :func:`unusable_answer` on purpose. A vendored
    contract bump where the models and the schema disagree makes `ingest`
    raise on every answer, and a headline sentence that says
    "github-checker returned ..." sends triage to a producer repo that is
    healthy while the broken consumer install is the last place looked.
    """
    return f"dispatcher could not interpret the answer: {type(err).__name__}"


def audit_stderr(
    logger: logging.Logger, action: str, repo_dir: str, stderr: str
) -> None:
    """Put the producer's stderr in the operator's log — and only there.

    "gh: could not authenticate: token expired" is the sentence an
    operator needs, and the pre-contract code surfaced it. It cannot go
    back into `ActionOutcome.error`: that is the `response_model` of eight
    endpoints, so a `git` failure printing a remote with an embedded
    credential (`https://x-access-token:ghs_…@github.com`, which `git`
    echoes verbatim) would serve the token to the browser. `contract.py`
    closed the adjacent channel on this very message and withdrew an
    "identifier-shaped values are fine" allowlist while doing it; a token
    *denylist* here would be weaker than the allowlist already judged too
    weak.

    The audit log is a different exposure: local, operator-owned, and
    already carrying the producer's own `detail`/`error`. Emitted under a
    key that is not `action=`, so counting attempts in the log is
    unaffected.
    """
    flattened = one_line(stderr) or ""
    if not flattened:
        return
    if len(flattened) > _MAX_STDERR_LEN:
        flattened = flattened[: _MAX_STDERR_LEN - 1] + "…"
    logger.info("stderr-from=%s repo=%s: %s", action, repo_dir, flattened)


def verb_mismatch(ingested: Ingested, requested: str) -> str | None:
    """Name the mismatch when the answer is about a different verb.

    For ``result_kind: action`` the producer's ``action`` states which
    verb actually ran — a fact, not a diagnostic — and nothing checked it
    against the verb that was asked for. The projection then relabels the
    answer with the requested verb, which makes a mismatch *invisible*
    rather than merely unchecked.

    `ingest` cannot make this check: it validates an envelope and knows
    nothing about the request. So it belongs to the callers, which are the
    only code holding both halves.

    Not applied to ``cli_error``/``contract_error``: there ``action`` is
    diagnostic by contract — it may name the attempted verb, an unknown
    string, or ``"unknown"`` — and comparing it would refuse legitimate
    refusals.
    """
    if isinstance(ingested, ActionPayload) and ingested.action != requested:
        return f"answer is about {ingested.action!r}, not the requested {requested!r}"
    return None


class ActionBusyError(Exception):
    """The repo already has an action in flight (the API turns this into 409)."""


class ActionRejectedError(Exception):
    """Bad target: unsafe name or not a git repo in the workspace (→ 422)."""


def _argv_refusal(argv: list[str]) -> str | None:
    """Name the first argv element the OS cannot be handed, or None.

    Runs BEFORE `subprocess.run`, so a hit is unambiguously pre-fork: nothing
    was executed and nothing was mutated. An embedded NUL is the reachable
    case — JSON permits it in a string, so it arrives straight off the wire —
    and `subprocess` itself raises `ValueError` for it, an exception type it
    shares with the post-run decode failure. Checking here is what makes the
    two distinguishable at all.
    """
    for index, arg in enumerate(argv):
        if "\x00" in arg:
            return f"argv[{index}] contains an embedded null byte"
    return None


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
                # one line: this string is both a 422 detail and an audit
                # line, and a newline would split the latter in the log
                f"{name} contains a control character "
                f"(U+{ord(found.group()):04X}); this value becomes an argument "
                f"to the github-checker binary, where a control character "
                f"would truncate it — remove the character and try again"
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
        if outcome.phase is not None:
            # Which side of the fork decided this. `launched_unreadable` beside
            # an absent `created=` is the shape a reader must be able to spot:
            # the verb RAN and we cannot say what it did.
            merge_fields += f" phase={outcome.phase}"
        _audit.info(
            "action=%s repo=%s ok=%s%s detail=%s error=%s",
            action,
            repo_dir,
            outcome.ok,
            merge_fields,
            # Flattened: `detail` and `error` are producer text, and a
            # vendored fixture (`pull-local-status-error`) carries an
            # eight-newline `error`, which turned one accepted outcome into
            # a nine-line audit record. "One audit line per attempt" was
            # enforced on the refusal path and not on this one; a log a
            # reader cannot count attempts in is not an audit log.
            one_line(outcome.detail),
            one_line(outcome.error),
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
        refused BEFORE anything runs — a pre-mutation refusal; callers that
        mutate pass `False`, read-only callers leave it unknown. It is
        deliberately NOT applied to anything that happens after the fork: a
        child that ran and then failed to be read leaves `created` unknown.
        """
        argv = [*self._command, action, str(target), *extra]

        def refused(reason: str) -> ActionOutcome:
            """PHASE 1 — nothing was executed. Safe to say nothing changed."""
            return ActionOutcome(
                action=action,
                dir=target.name,
                ok=False,
                created=refusal_created,
                phase=PHASE_PRE_LAUNCH,
                error=f"refused before launch: {reason}",
            )

        def unreadable() -> ActionOutcome:
            """PHASE 2 — the child RAN; its answer could not be read.

            `created` is left None on purpose and `refusal_created` is NOT
            consulted: for `issue-create` the issue may exist. Claiming
            `created=False` here sends the screen down the ordinary-refusal
            arm, which re-enables Create — the duplicate this feature exists
            to prevent.
            """
            return ActionOutcome(
                action=action,
                dir=target.name,
                ok=False,
                created=None,
                phase=PHASE_LAUNCHED_UNREADABLE,
                error=(
                    "process completed, but its response was unreadable; "
                    "mutation outcome unknown"
                ),
            )

        # PHASE 1. Explicit, before the call: a hit here is unambiguously
        # pre-fork because no process has been created yet. Inferring the same
        # thing from the exception subprocess happens to raise is what broke.
        refusal = _argv_refusal(argv)
        if refusal is not None:
            return refused(refusal)
        try:
            # Bytes, NOT text=True. Decoding inside this call would put a
            # post-execution failure inside a pre-launch except clause, and no
            # ordering of handlers can undo that: the phase boundary has to be
            # a boundary in the CODE.
            proc = subprocess.run(argv, capture_output=True, timeout=_ACTION_TIMEOUT)
        except UnicodeDecodeError:
            # ORDER IS LOAD-BEARING: UnicodeDecodeError IS a ValueError, so it
            # must precede the clause below or it would be caught as a
            # pre-launch refusal. Unreachable while output is captured as
            # bytes — kept so that re-adding text=True degrades to the correct
            # phase instead of silently to the wrong one.
            return unreadable()
        except ValueError as err:
            return refused(str(err))
        except subprocess.TimeoutExpired:
            # The child started and was killed: it may have done its work.
            return unreadable()
        except OSError as err:
            # N-3: exec itself failed (E2BIG from an oversized client-supplied
            # `--if-head`, EACCES, ENOMEM). Nothing ran. Previously only
            # FileNotFoundError was caught and the rest raised out — a 500
            # with ZERO audit lines, the same guarantee break as F-1.
            return refused(str(err))
        # PHASE 2 boundary: everything below happens with a completed child.
        try:
            stdout = proc.stdout.decode("utf-8")
            stderr = proc.stderr.decode("utf-8")
        except UnicodeDecodeError:
            return unreadable()

        # PHASE 3 — we hold a readable answer; every outcome below is a
        # statement about what github-checker actually said.
        def unreadable_envelope(reason: str) -> ActionOutcome:
            """PHASE 3 — the child ran and answered; we cannot use it."""
            audit_stderr(_audit, action, target.name, stderr)
            return ActionOutcome(
                action=action,
                dir=target.name,
                ok=False,
                phase=PHASE_READABLE,
                error=reason,
            )

        # The one place a producer answer is judged. Everything this module
        # used to do by hand here — its own `json.loads`, its own
        # root-object check, `.get()` per field — is gone: a second parse
        # path is a second set of rules about what to believe, and the two
        # drift.
        try:
            ingested = ingest(stdout, returncode=proc.returncode)
        except ContractViolation as violation:
            return unreadable_envelope(unusable_answer(str(violation)))
        except Exception as err:  # noqa: BLE001 - see below
            # `ingest` raises more than `ContractViolation`: a pydantic
            # `ValidationError` when a model and the vendored schema
            # disagree (deliberately unwrapped there), an `OSError` if the
            # vendored schema cannot be read. `contract.py` lets those be
            # loud because nothing has run yet at that point. Here
            # something has: a merge subprocess that genuinely ran must not
            # vanish from the audit log because the *consumer* was broken,
            # which is the guarantee this module states and the hole a
            # narrower guard here reopened. Named as a consumer fault, not
            # a producer one, and audited either way.
            return unreadable_envelope(consumer_failure(err))

        mismatch = verb_mismatch(ingested, action)
        if mismatch is not None:
            return unreadable_envelope(unusable_answer(mismatch))
        return project_outcome(ingested, action=action, dir_name=target.name)

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
            # The composite is only as readable as its LEAST readable step:
            # claiming `readable_result` because the merge parsed, while the
            # sync's answer could not be read, would assert knowledge of a
            # step nobody read. Both component lines already carry their own
            # phase; this is the one that was missing.
            phase=(merge.phase if merge.phase != PHASE_READABLE else sync.phase),
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
        _audit.info(
            "action=pr-detail repo=%s pr=%s ok=%s phase=%s",
            repo_dir,
            pr,
            outcome.ok,
            outcome.phase,
        )
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
            "action=issue-lookup repo=%s slug=%r ok=%s phase=%s matches=%s",
            repo_dir,
            slug,
            outcome.ok,
            outcome.phase,
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
                    phase=PHASE_PRE_LAUNCH,
                    error=f"could not write the request body: {err}",
                )
            finally:
                if body_file is not None:
                    try:
                        Path(body_file).unlink(missing_ok=True)
                    except OSError as err:
                        # The one exit of the eight that could still leave no
                        # audit line: a cleanup failure (EACCES, EIO, a
                        # read-only /tmp) raised out of `finally`, discarding
                        # the outcome that was already decided and taking the
                        # audit call below with it. A leaked temp file is a
                        # nuisance; an unlogged mutation attempt is the thing
                        # this module promises cannot happen.
                        _audit.info(
                            "action=request-task repo=%s cleanup_failed=%s",
                            repo_dir,
                            err,
                        )
        outcome.action = "request-task"
        self._audit_outcome("request-task", repo_dir, outcome)
        return outcome
