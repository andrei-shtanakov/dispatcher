"""The long-work executor: owns the maestro child process (spec §7.1).

`ActionRunner` is not stretched to cover this and must not be. It is
synchronous by construction — `subprocess.run(..., timeout=120)`
(`dispatcher/core/actions.py:498`, `:51`) behind a process-local
`threading.Lock` (`:351`) — so hosting a run there would produce a web
request holding a process open until it times out, not a control plane.

Short forge actions stay with `ActionRunner`; the steward verdict belongs to
neither, because ARCH-C3 forbids dispatcher computing verdicts at all
(`dispatcher/core/governance.py:10-12`).
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.run_identity import RepoKey, safe_path_parts
from dispatcher.core.run_request import (
    RunRejectedError,
    RunRequest,
    validate_request,
)
from dispatcher.core.run_store import (
    LaunchRecord,
    LockBusyError,
    RunStore,
    RunStoreError,
)

_audit = logging.getLogger("dispatcher.runs")

_POLL_INTERVAL = 0.5
_MATERIALIZE_TIMEOUT = 120.0
_VERB_TIMEOUT = 120

#: The two endings a run cannot observe about itself
#: (`maestro/maestro/cli.py:1973`).
_OPERATOR_ENDINGS = frozenset({"cancelled", "superseded"})

#: Mode-1 only (spec §6). `submit` is not here — it is not a short verb.
#: Workstream verbs serve `orchestrate` (Mode 2) and stay outside the slice:
#: one request type must not control two state machines. `stop` is also
#: excluded: `maestro stop` takes no `--run`/positional at all — it "Stops
#: the running scheduler" (sends a termination signal to the scheduler
#: process), not one run. Wiring it up would make a per-request control
#: silently kill every other run the scheduler is managing.
_VERBS = frozenset({"status", "retry", "approve", "run-end"})


class ControlPlaneOff(Exception):
    """`run_state_dir` or `maestro_cli` is unset; there is no control plane."""


class LaunchReceipt(BaseModel):
    """The synchronous answer to a submit (spec §5.3).

    `accepted` is three-valued and the values are not interchangeable:
    `True` — the rename into `runs/` was observed; `False` — refused before
    any run could exist; `None` — `launch_unknown`, a run may or may not
    exist. `False` is a CLAIM and may be made only when the controller knows
    no run was created.
    """

    request_id: str
    run_id: str | None = None
    accepted: bool | None = None
    reason: str | None = None


class UnknownResolution(BaseModel):
    """What a `launch_unknown` resolution attempt could and could not decide."""

    request_id: str
    adopted_run_id: str | None = None
    candidates: list[str] = Field(default_factory=list)
    reason: str = ""


class VerbOutcome(BaseModel):
    """The synchronous result of one Mode-1 control verb (spec §6)."""

    verb: str
    run_id: str
    ok: bool
    stdout: str = ""
    stderr: str = ""


class RunController:
    """Starts Mode-1 runs and reports what is actually known about them."""

    def __init__(
        self,
        config: DispatcherConfig,
        *,
        poll_interval: float = _POLL_INTERVAL,
        materialize_timeout: float = _MATERIALIZE_TIMEOUT,
    ) -> None:
        self._config = config
        self._poll = poll_interval
        self._timeout = materialize_timeout

    # -- wiring -------------------------------------------------------------

    def _require_on(self) -> tuple[Path, Path, Path]:
        """`(state_dir, cli, home)`, or raise `ControlPlaneOff`.

        Only `run_state_dir` and `maestro_cli` gate the control plane
        (`dispatcher/core/discovery.py:49-57`): `maestro_home=None` is not
        "off", it means "derive from `maestro_db.parent`"
        (`DispatcherConfig.effective_maestro_home`,
        `dispatcher/core/service.py:72`) — the same fallback the dashboard
        collector already relies on. Gating on a bare `maestro_home is None`
        would turn every config that only sets `maestro_db` into a false
        "control plane off".
        """
        state_dir = self._config.run_state_dir
        cli = self._config.maestro_cli
        if state_dir is None or cli is None:
            raise ControlPlaneOff(
                "control plane is off: run_state_dir and maestro_cli must "
                "both be configured"
            )
        return state_dir, cli, self._config.effective_maestro_home

    def _store(self) -> RunStore:
        state_dir, _, _ = self._require_on()
        return RunStore(state_dir)

    def runs_dir(self, key: RepoKey) -> Path:
        """`<maestro-home>/projects/<...>/runs` — the watched directory.

        Built from the CONFIGURED home, which is also what the child is given
        as `$MAESTRO_HOME`. One value, two uses: launching under one root and
        watching another is how a healthy run reads as `launch_unknown`.
        """
        _, _, home = self._require_on()
        return home.joinpath("projects", *safe_path_parts(key), "runs")

    @staticmethod
    def _listing(runs: Path) -> list[str]:
        try:
            return sorted(p.name for p in runs.iterdir() if p.is_dir())
        except OSError:
            # An absent `runs/` is normal before the first run of a repo.
            return []

    # -- submit -------------------------------------------------------------

    def submit(self, request: RunRequest) -> LaunchReceipt:
        """Start one run; return what is known, never more (spec §5.3)."""
        try:
            self._require_on()
        except ControlPlaneOff as err:
            return self._refuse(request.request_id, str(err))

        store = self._store()
        try:
            existing = store.get(request.request_id)
        except RunStoreError as err:
            # `RunRequest.request_id` is pydantic-constrained to a safe
            # charset (spec §4), but this is the second, independent layer:
            # `RunStore._record_path` refuses anything outside its own safe
            # charset with a bare `RunStoreError` — the store's base
            # exception, not just `LockBusyError` — and that must not
            # escape as an unhandled error for input that just isn't
            # UUID-shaped. Nothing has been reserved or launched yet, so
            # "no run exists" is a fact dispatcher can safely claim.
            return self._refuse(request.request_id, f"cannot use request_id: {err}")

        if existing is not None and existing.state != "reserved":
            # Idempotency: a repeated request_id continues or returns the
            # existing record and never starts a second process (spec §5.2).
            return LaunchReceipt(
                request_id=request.request_id,
                run_id=existing.run_id,
                accepted=_accepted_for(existing),
                reason=existing.reason,
            )

        try:
            validated = validate_request(request, self._config)
        except RunRejectedError as err:
            return self._refuse(request.request_id, str(err))

        runs = self.runs_dir(validated.key)
        try:
            store.reserve(
                request.request_id,
                validated.key,
                known_runs=self._listing(runs),
                window_start=datetime.now(UTC).isoformat(),
            )
        except LockBusyError as err:
            return self._refuse(request.request_id, str(err))
        except RunStoreError as err:
            return self._refuse(request.request_id, f"cannot use request_id: {err}")

        return self._launch(store, request, validated.checkout, validated.key, runs)

    def _launch(
        self,
        store: RunStore,
        request: RunRequest,
        checkout: Path,
        key: RepoKey,
        runs: Path,
    ) -> LaunchReceipt:
        _, cli, home = self._require_on()
        reserved = store.get(request.request_id)
        if reserved is None:
            # `reserve()` just wrote this record synchronously; its absence
            # here is a store invariant violation, not an ordinary failure
            # mode with a safe fallback, so this is raised rather than
            # folded into a receipt.
            raise RunStoreError(
                f"launch record for {request.request_id} disappeared "
                "between reserve() and _launch(): nothing to launch against"
            )
        before = set(reserved.known_runs)
        argv = [str(cli), "run", request.tasks]
        env = {**os.environ, "MAESTRO_HOME": str(home)}
        try:
            store.mark_launching(request.request_id)
        except (RunStoreError, OSError) as err:
            # No subprocess has been started yet, so "no run exists" is
            # still a fact dispatcher knows, unlike a failure after
            # materialization. The record is untouched (still "reserved"),
            # so a resubmission under the same request_id will try again.
            _audit.error(
                "submit request=%s repo=%s mark_launching failed: %s",
                request.request_id,
                key.as_text(),
                err,
            )
            return self._refuse(request.request_id, f"cannot record launch: {err}")

        try:
            child = subprocess.Popen(  # noqa: S603 — argv is a fixed shape
                argv,
                cwd=str(checkout),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as err:
            # Nothing was executed, so "no run exists" is a fact, not a guess.
            self._try_mark_terminal(store, request.request_id, f"launch failed: {err}")
            return self._refuse(request.request_id, f"cannot start maestro: {err}")

        run_id = self._await_materialization(runs, before, child)
        if run_id is not None:
            return self._report_materialized(store, request, key, run_id)

        reason = (
            "launch_unknown: no run appeared under "
            f"{runs} within {self._timeout:g}s. A run may or may not exist; "
            "resolve it before retrying (spec §5.2.1). The repository lock is "
            "deliberately still held."
        )
        self._try_mark_unknown(store, request.request_id, reason)
        _audit.info(
            "submit request=%s repo=%s accepted=None launch_unknown",
            request.request_id,
            key.as_text(),
        )
        return LaunchReceipt(
            request_id=request.request_id, accepted=None, reason=reason
        )

    def _report_materialized(
        self, store: RunStore, request: RunRequest, key: RepoKey, run_id: str
    ) -> LaunchReceipt:
        """Record and report a materialized run — never claim `False` for it.

        The run was OBSERVED (spec §5.3): that is knowledge dispatcher
        already has, independent of whether the durable write below
        succeeds. A failed write must not raise (the caller would get no
        receipt at all) and must not become `accepted: False` — the one
        claim that is definitely wrong once a run has been seen. It becomes
        `accepted: None` instead, same as an ordinary `launch_unknown`: the
        lock stays held (via the `mark_unknown` fallback, or untouched if
        even that write fails) and `run_id`/`reason` still tell the caller a
        run exists, which is what separates this from a genuine no-run
        `launch_unknown`.
        """
        try:
            store.mark_materialized(request.request_id, run_id)
        except (RunStoreError, OSError) as err:
            _audit.error(
                "submit request=%s repo=%s run=%s mark_materialized failed: %s",
                request.request_id,
                key.as_text(),
                run_id,
                err,
            )
            reason = (
                f"run {run_id} was observed under runs/, but the launch "
                f"record could not be updated to reflect it: {err}. The run "
                "exists; the repository lock is deliberately still held."
            )
            self._try_mark_unknown(store, request.request_id, reason)
            return LaunchReceipt(
                request_id=request.request_id,
                run_id=run_id,
                accepted=None,
                reason=reason,
            )

        _audit.info(
            "submit request=%s repo=%s run=%s accepted=True",
            request.request_id,
            key.as_text(),
            run_id,
        )
        return LaunchReceipt(
            request_id=request.request_id, run_id=run_id, accepted=True
        )

    @staticmethod
    def _try_mark_terminal(store: RunStore, request_id: str, outcome: str) -> None:
        """Best-effort `mark_terminal`.

        Called only when the caller-facing receipt is already settled as
        `accepted: False` (nothing was launched); a write failure here
        costs the audit trail, not correctness, so it is logged and
        swallowed rather than raised.
        """
        try:
            store.mark_terminal(request_id, outcome)
        except (RunStoreError, OSError) as err:
            _audit.error("submit request=%s mark_terminal failed: %s", request_id, err)

    @staticmethod
    def _try_mark_unknown(store: RunStore, request_id: str, reason: str) -> None:
        """Best-effort `mark_unknown`.

        Used for the ordinary materialization timeout and as the fallback
        when `mark_materialized` itself cannot be written. If this also
        fails there is nowhere left to durably record the fact; this only
        logs, since the caller-facing receipt already carries `reason`.
        """
        try:
            store.mark_unknown(request_id, reason)
        except (RunStoreError, OSError) as err:
            _audit.error("submit request=%s mark_unknown failed: %s", request_id, err)

    def _await_materialization(
        self, runs: Path, before: set[str], child: subprocess.Popen[bytes]
    ) -> str | None:
        """Watch for the rename INTO `runs/` — maestro's own publication point.

        maestro builds the run under `<project>/.staging/<run_id>`, outside
        `runs/`, and renames it in only after the database is closed
        (`maestro/maestro/run_publish.py:45-73`). A new entry here is
        therefore a materialisation defined by the producer.
        """
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            fresh = [name for name in self._listing(runs) if name not in before]
            if len(fresh) == 1:
                return fresh[0]
            if len(fresh) > 1:
                # Two new runs cannot both be ours; the lock is supposed to
                # make this impossible, so it is unknown, not a pick.
                return None
            if child.poll() is not None and not fresh:
                # The child is gone and published nothing. It may still have
                # published between these two observations, so this is
                # unknown rather than a claimed non-launch.
                time.sleep(self._poll)
                late = [n for n in self._listing(runs) if n not in before]
                return late[0] if len(late) == 1 else None
            time.sleep(self._poll)
        return None

    # -- leaving launch_unknown ----------------------------------------------

    def record(self, request_id: str) -> LaunchRecord | None:
        """The stored launch record, for the API and for tests.

        Translates a store-level `RunStoreError` into `RunRejectedError`:
        `RunStore._record_path` raises the bare base class for a
        `request_id` outside its safe charset, and unlike
        `RunRequest.request_id`, an id reaching this method from an HTTP
        path parameter carries no pydantic constraint. A refusal, not a
        crash — `submit` makes the same translation for its own entry
        point, into a receipt instead of an exception.
        """
        try:
            return self._store().get(request_id)
        except RunStoreError as err:
            raise RunRejectedError(
                f"cannot use request_id {request_id!r}: {err}"
            ) from err

    def control(
        self,
        request_id: str,
        verb: str,
        *,
        task_id: str | None = None,
        outcome: str | None = None,
    ) -> VerbOutcome:
        """Run one allowlisted Mode-1 verb against this request's run.

        Short and synchronous, unlike `submit`: `status`/`retry`/`approve`/
        `run-end` act on a run that already exists and return once the
        child exits, no materialization polling involved.
        """
        if verb not in _VERBS:
            raise RunRejectedError(f"verb not allowlisted: {verb!r}")
        try:
            record = self._store().get(request_id)
        except RunStoreError as err:
            raise RunRejectedError(
                f"cannot use request_id {request_id!r}: {err}"
            ) from err
        if record is None or record.run_id is None:
            raise RunRejectedError(
                f"{request_id} has no run to act on "
                f"(state: {record.state if record else 'absent'})"
            )
        run_id = record.run_id
        _, cli, home = self._require_on()

        argv: list[str]
        run_end_outcome: str | None = None
        if verb == "approve":
            if not task_id:
                raise RunRejectedError(
                    "approve needs a task_id: it releases a task sitting in "
                    "AWAITING_APPROVAL. A task in NEEDS_REVIEW is cleared by "
                    "retry instead"
                )
            argv = [str(cli), verb, task_id, "--run", run_id]
        elif verb == "retry":
            if not task_id:
                raise RunRejectedError(
                    "retry needs a task_id: it clears a task sitting in "
                    "FAILED or NEEDS_REVIEW. A task in AWAITING_APPROVAL is "
                    "released by approve instead"
                )
            argv = [str(cli), verb, task_id, "--run", run_id]
        elif verb == "run-end":
            if outcome is None or outcome not in _OPERATOR_ENDINGS:
                raise RunRejectedError(
                    f"run-end outcome must be cancelled|superseded, got {outcome!r}"
                )
            run_end_outcome = outcome
            argv = [str(cli), verb, run_id, "--outcome", outcome]
        else:
            argv = [str(cli), verb, "--run", run_id]

        try:
            proc = subprocess.run(  # noqa: S603 — argv is a fixed shape
                argv,
                capture_output=True,
                text=True,
                env={**os.environ, "MAESTRO_HOME": str(home)},
                timeout=_VERB_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as err:
            # A missing/non-executable binary raises OSError; a hang past
            # _VERB_TIMEOUT raises TimeoutExpired (SubprocessError). Neither
            # is a RunRejectedError on its own, and both are otherwise
            # ordinary and recoverable — the run itself is untouched, only
            # this attempt to control it failed (mirrors `end_orphan`,
            # `run_request._git`, `run_controller._launch`'s Popen guard).
            raise RunRejectedError(f"cannot run maestro {verb}: {err}") from err

        _audit.info(
            "verb=%s request=%s run=%s ok=%s",
            verb,
            request_id,
            run_id,
            proc.returncode == 0,
        )
        if run_end_outcome is not None and proc.returncode == 0:
            try:
                self._store().mark_terminal(request_id, run_end_outcome)
            except RunStoreError as err:
                # maestro run-end already succeeded; only releasing the
                # lock/updating the record failed (e.g. `LockBusyError` from
                # a corrupt or foreign-held lock file). Still a refusal, not
                # an unhandled exception — same translation as above.
                raise RunRejectedError(
                    f"run-end for {run_id} succeeded, but the launch record "
                    f"could not be released: {err}"
                ) from err
        return VerbOutcome(
            verb=verb,
            run_id=run_id,
            ok=proc.returncode == 0,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
        )

    def _candidates(self, request_id: str) -> tuple[list[str], RepoKey]:
        """New runs relative to the pre-launch snapshot, and the repo key.

        `known_runs` is `runs/` as it looked immediately before the launch
        (`dispatcher/core/run_store.py:63-64`) — the only thing an orphan can
        be correlated against.

        Guarded here, not only at the API layer that will call
        `resolve_unknown`/`end_orphan`: both methods reach this before doing
        anything else, so every caller — present or future — inherits the
        guard instead of relying on each one to add it. A record that has
        already settled (`materialized`/`terminal`) recomputes "new since
        `known_runs`" against a now-stale snapshot; re-adopting over it would
        silently overwrite a settled outcome.
        """
        try:
            record = self._store().get(request_id)
        except RunStoreError as err:
            raise RunRejectedError(
                f"cannot use request_id {request_id!r}: {err}"
            ) from err
        if record is None:
            raise RunRejectedError(f"no launch record for {request_id}")
        if record.state != "launch_unknown":
            raise RunRejectedError(
                f"{request_id} is not launch_unknown (state={record.state!r}); "
                "nothing to resolve"
            )
        parts = record.repo_key.split("/")
        key = (
            RepoKey(host="", owner="", repo=parts[1], local=True)
            if parts[0] == "_local"
            else RepoKey(host=parts[0], owner=parts[1], repo=parts[2])
        )
        before = set(record.known_runs)
        fresh = [n for n in self._listing(self.runs_dir(key)) if n not in before]
        return fresh, key

    def resolve_unknown(self, request_id: str) -> UnknownResolution:
        """Adopt an orphan only under unambiguous correlation (spec §5.2.1).

        Exactly one new run relative to the pre-launch snapshot is adopted.
        Zero and two-or-more both remain `launch_unknown`: a heuristic
        adoption attributes work to a request that may not have produced it,
        and every later control verb would then act on a stranger's run.
        """
        candidates, _ = self._candidates(request_id)
        if len(candidates) == 1:
            adopted = candidates[0]
            try:
                self._store().mark_materialized(request_id, adopted)
            except RunStoreError as err:
                # A new run WAS observed and correlated; only the durable
                # write failed (e.g. `LockBusyError` from a corrupt or
                # foreign-held lock file). Still a refusal, not an unhandled
                # exception — same translation as `record`/`control`.
                raise RunRejectedError(
                    f"correlated {adopted} for {request_id}, but the launch "
                    f"record could not be updated: {err}"
                ) from err
            _audit.info("resolve request=%s adopted=%s", request_id, adopted)
            return UnknownResolution(
                request_id=request_id,
                adopted_run_id=adopted,
                candidates=candidates,
                reason="exactly one new run correlated; adopted",
            )
        reason = (
            "no new run to correlate; the launch may never have started"
            if not candidates
            else (
                f"{len(candidates)} new runs correlate equally well; "
                "attribution is ambiguous in principle. Name the exact run_id "
                "and end it — the best-fitting timestamp is not evidence."
            )
        )
        _audit.info(
            "resolve request=%s adopted=None candidates=%d",
            request_id,
            len(candidates),
        )
        return UnknownResolution(
            request_id=request_id, candidates=candidates, reason=reason
        )

    def end_orphan(
        self, request_id: str, run_id: str, outcome: str
    ) -> UnknownResolution:
        """Operator resolution: end a NAMED orphan, then release the lock.

        The run must be one the correlation actually offered — ending a run
        that merely fits the launch window is forbidden for the same reason
        automatic adoption is (spec §5.2.1).
        """
        if outcome not in _OPERATOR_ENDINGS:
            raise RunRejectedError(
                f"outcome must be one of cancelled|superseded, got {outcome!r}: "
                "'completed' and 'failed' are recorded by the run itself"
            )
        candidates, key = self._candidates(request_id)
        if run_id not in candidates:
            raise RunRejectedError(
                f"{run_id} is not a candidate for {request_id}; "
                f"candidates are: {', '.join(candidates) or 'none'}"
            )
        _, cli, home = self._require_on()
        try:
            proc = subprocess.run(  # noqa: S603 — argv is a fixed shape
                [str(cli), "run-end", run_id, "--outcome", outcome],
                capture_output=True,
                text=True,
                env={**os.environ, "MAESTRO_HOME": str(home)},
                timeout=_VERB_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as err:
            # A missing/non-executable binary raises OSError; a hang past
            # _VERB_TIMEOUT raises TimeoutExpired (SubprocessError). Neither
            # is a RunRejectedError on its own, and both are otherwise
            # ordinary and recoverable — the run itself is untouched, only
            # this attempt to end it failed (spec §5.2.1; mirrors
            # `run_request._git`, `run_controller._launch`'s Popen guard).
            raise RunRejectedError(f"cannot run maestro run-end: {err}") from err
        if proc.returncode != 0:
            raise RunRejectedError(
                f"maestro run-end refused: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        store = self._store()
        try:
            store.mark_terminal(request_id, outcome)
        except RunStoreError as err:
            # `maestro run-end` already succeeded above; only releasing the
            # lock/updating the record failed. Still a refusal, not an
            # unhandled exception — same translation as `control`.
            raise RunRejectedError(
                f"run-end for {run_id} succeeded, but the launch record "
                f"could not be released: {err}"
            ) from err
        _audit.info(
            "end-orphan request=%s run=%s outcome=%s", request_id, run_id, outcome
        )
        return UnknownResolution(
            request_id=request_id,
            adopted_run_id=run_id,
            candidates=candidates,
            reason=f"operator ended {run_id} as {outcome}; lock released",
        )

    def _refuse(self, request_id: str, reason: str) -> LaunchReceipt:
        _audit.info("submit request=%s accepted=False rejected=%s", request_id, reason)
        return LaunchReceipt(request_id=request_id, accepted=False, reason=reason)


def _accepted_for(record: LaunchRecord) -> bool | None:
    """Map a stored record back to the three-valued receipt.

    Takes the RECORD, not the bare state: `terminal` covers both "the run
    ended" and "the launch failed before any run existed", and only
    `run_id` tells those apart. Deciding from `state` alone would report a
    refusal as `accepted: true` — the exact collapse spec §5.3 forbids.
    """
    if record.state == "materialized":
        return True
    if record.state == "terminal":
        return record.run_id is not None
    if record.state == "launch_unknown":
        return None
    return None
