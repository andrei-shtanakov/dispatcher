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

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from dispatcher.core.collectors.maestro import classified_runs
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.models import OrchestrationRunInfo, ProjectSnapshot
from dispatcher.core.run_identity import (
    IdentityError,
    RepoKey,
    identity_from_checkout,
    safe_path_parts,
)
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


def _key_from_record(record: "LaunchRecord") -> RepoKey:
    """Rebuild the `RepoKey` a record's text form names.

    One copy on purpose: `_candidates` and `_logs_dir` both need it, and a
    second inline `split("/")` would be a second place to get the `_local`
    shape wrong.
    """
    parts = record.repo_key.split("/")
    if parts[0] == "_local":
        return RepoKey(host="", owner="", repo=parts[1], local=True)
    return RepoKey(host=parts[0], owner=parts[1], repo=parts[2])


def _parse_event(line: str) -> "RunLogEvent":
    """One `events.jsonl` line, degrading to `raw` rather than vanishing.

    A half-written last line is ordinary while a run is live. Dropping it
    would make the console disagree with the file it claims to show, so an
    unparseable line is carried through as text.
    """
    raw = line.rstrip("\n")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return RunLogEvent(raw=raw)
    if not isinstance(data, dict):
        return RunLogEvent(raw=raw)
    return RunLogEvent(
        ts=str(data.get("timestamp", "")),
        event=str(data.get("event", "")),
        task_id=data.get("task_id"),
        message=data.get("message"),
        raw=raw,
    )


class RunLogEvent(BaseModel):
    """One line of maestro's own `events.jsonl`, kept as maestro wrote it.

    `raw` survives a line that does not parse: a truncated tail is normal
    while a run is live, and dropping such a line would make the console
    quietly disagree with the file on disk.
    """

    ts: str = ""
    event: str = ""
    task_id: str | None = None
    message: str | None = None
    raw: str = ""


class RunLogs(BaseModel):
    """The run's event timeline plus the names of its per-task logs.

    Task logs are NAMED here but not inlined: they carry the agent's whole
    output and are fetched one at a time. `warnings` exists for the same
    reason `RunView` has it — an unreadable log directory must not render
    as an empty one (NFR-02).
    """

    run_id: str
    events: list[RunLogEvent] = Field(default_factory=list)
    #: Set when the timeline was longer than the cap: the OLDEST lines were
    #: dropped, never the newest, and the console says so rather than
    #: presenting a truncated file as the whole of it.
    truncated: bool = False
    task_logs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TaskLog(BaseModel):
    """The tail of one task's log."""

    run_id: str
    task_id: str
    text: str = ""
    truncated: bool = False


class RunView(BaseModel):
    """dispatcher's request, joined to maestro's own row for that run."""

    record: LaunchRecord
    run: OrchestrationRunInfo | None = None
    #: `classified_runs`' collection warnings (an unreadable `state.db`, an
    #: unreadable `projects/`, ...): without this, a run that is UNREADABLE
    #: and one that is genuinely ABSENT both surface as `run=None`, which is
    #: exactly the "unreadable looks clean" shape this codebase refuses
    #: elsewhere (NFR-02).
    warnings: list[str] = Field(default_factory=list)


class UnknownResolution(BaseModel):
    """What a `launch_unknown` resolution attempt could and could not decide."""

    request_id: str
    adopted_run_id: str | None = None
    candidates: list[str] = Field(default_factory=list)
    reason: str = ""


@dataclass(frozen=True)
class _MaterializationResult:
    """What `_await_materialization` could determine about the child.

    `died_without_publishing` is a separate flag from `run_id is None`
    rather than a third `run_id` sentinel: it is the one case where "no run
    appeared" is knowable as `accepted: False` rather than
    `launch_unknown` — the only publisher (maestro itself) exited non-zero
    and a second look still found nothing (spec §5.3, I1).
    """

    run_id: str | None
    died_without_publishing: bool = False


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
        """Run-id subdirectories of `runs/`.

        A genuinely absent `runs/` is normal before a repo's first run and
        reads as clean-empty (`FileNotFoundError`). Every other `OSError` —
        a permissions error, a bad mount, `runs` colliding with a plain
        file (`NotADirectoryError`) — must NOT read as empty: that is
        exactly how a live launch would be reported as "never happened"
        (mirrors `_subdirs`, `dispatcher/core/collectors/maestro.py:222-232`,
        which WARNs rather than swallowing). `NotADirectoryError` is grouped
        with the fault here, unlike `_subdirs` — `_listing` backs
        correlation, where a stray file sitting where `runs/` belongs is an
        anomaly, not the ordinary "no runs yet" case `FileNotFoundError`
        already covers. This raises rather than logging: callers translate
        the propagated `OSError` into their own existing failure shape
        (a refusal receipt in `submit`, `RunRejectedError` in
        `_candidates`) rather than this method inventing a new one.
        """
        try:
            return sorted(p.name for p in runs.iterdir() if p.is_dir())
        except FileNotFoundError:
            return []

    def _listing_since(self, runs: Path, before: set[str]) -> list[str]:
        """Names new in `runs/` relative to `before`, for the post-launch
        poll only (`_await_materialization`).

        Unlike `_listing`'s two other callers — the pre-launch snapshot in
        `submit` and operator resolution in `_candidates`, both of which
        must now fail loud (I7) — the maestro child is already running
        here (`start_new_session=True`, spec §7.1) by the time this is
        called. Letting an `OSError` escape mid-poll would abandon that
        live process behind an unhandled 500 with no diagnostics recorded,
        which is a strictly worse outcome than the transient read failure
        itself. A read failure here degrades to "nothing new this round"
        (logged, not silent) and the poll continues; correctness still
        rests on `submit`'s pre-launch snapshot and `_candidates`' recount
        being trustworthy, which this method does not touch.
        """
        try:
            return [n for n in self._listing(runs) if n not in before]
        except OSError as err:
            _audit.error("await_materialization runs=%s cannot list: %s", runs, err)
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
            reason = existing.reason
            if reason is None and existing.state == "launching":
                # `mark_launching` sets no `reason` (only `mark_unknown`
                # does), so this was the one receipt shape that told the
                # caller nothing at all: accepted=null AND reason=null.
                reason = (
                    f"{request.request_id} is already launching; "
                    "resubmission does not start a second process — poll "
                    "this request_id again"
                )
            return LaunchReceipt(
                request_id=request.request_id,
                run_id=existing.run_id,
                accepted=_accepted_for(existing),
                reason=reason,
            )

        try:
            validated = validate_request(request, self._config)
            # Checked HERE, beside the request's own validation: before the
            # lock is taken, before a record is written, and before any
            # process starts. A launch without a resolvable catalog is a
            # decidable `accepted: false` (spec §5.3) — dispatcher knows no
            # run was created — not the ambiguity of a child that publishes
            # a run directory and then halts on the missing catalog, which
            # is what the pilot saw.
            catalog = self._catalog_path()
        except RunRejectedError as err:
            return self._refuse(request.request_id, str(err))

        runs = self.runs_dir(validated.key)
        try:
            store.reserve(
                request.request_id,
                validated.key,
                known_runs=self._listing(runs),
                window_start=datetime.now(UTC).isoformat(),
                # spec §3.1: the request body is the join dispatcher owns
                # (I3) — persisted here, not validated and dropped.
                work_id=request.work_id,
                revision=request.revision,
                tasks=request.tasks,
                spec_ref_path=request.spec_ref.path if request.spec_ref else None,
                spec_commit=validated.spec_commit,
                plan_ref_path=request.plan_ref.path if request.plan_ref else None,
                plan_commit=validated.plan_commit,
                # The checkout the launch will run in, persisted so every
                # later verb runs maestro from the SAME repository rather
                # than from the server process's cwd (see `_verb_cwd`).
                checkout=str(validated.checkout),
            )
        except LockBusyError as err:
            return self._refuse(request.request_id, str(err))
        except (RunStoreError, OSError) as err:
            # I5: `_write` (inside `reserve`) raises plain `OSError` on a
            # filesystem failure — the branch's own convention elsewhere is
            # `(RunStoreError, OSError)`, and this call was one of four that
            # had fallen behind it.
            return self._refuse(request.request_id, f"cannot use request_id: {err}")

        return self._launch(
            store, request, validated.checkout, validated.key, runs, catalog
        )

    def _launch(
        self,
        store: RunStore,
        request: RunRequest,
        checkout: Path,
        key: RepoKey,
        runs: Path,
        catalog: Path,
    ) -> LaunchReceipt:
        state_dir, cli, home = self._require_on()
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
        try:
            self._validate_maestro_cli(cli)
        except RunRejectedError as err:
            # Nothing has been recorded as launching yet, so — like the
            # `mark_launching` failure just below — the record stays
            # "reserved" and a resubmission will try again.
            return self._refuse(request.request_id, f"cannot start maestro: {err}")
        argv = [str(cli), "run", request.tasks]
        # The CONFIGURED catalog wins over anything ambient: an inherited
        # `$ATP_CATALOG` would make the launch depend on how the server was
        # started, which is the failure `_catalog_path` exists to end.
        # Passed in from `submit`, which validated it, rather than
        # re-derived here: this call site is NOT inside submit's try, so a
        # second validation raising here would escape as an unhandled 500
        # instead of a receipt (PR #176 Copilot review).
        env = {
            **os.environ,
            "MAESTRO_HOME": str(home),
            "ATP_CATALOG": str(catalog),
        }
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

        # A run's diagnostics are discarded past this point unless captured
        # now: once maestro exits, DEVNULL would have thrown away the one
        # message that could tell an operator why (I1). Best-effort — a
        # failure to open the log file must not block the launch itself.
        stderr_path = self._stderr_path(state_dir, request.request_id)
        try:
            stderr_handle = stderr_path.open("wb")
        except OSError:
            stderr_handle = None
        try:
            child = subprocess.Popen(  # noqa: S603 — argv is a fixed shape
                argv,
                cwd=str(checkout),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=stderr_handle
                if stderr_handle is not None
                else subprocess.DEVNULL,
                # The run must survive this web request and a dispatcher
                # restart (spec §7.1): without its own session, a Ctrl-C in
                # the terminal running the server sends SIGINT to the whole
                # foreground process group and kills the run too (I2). The
                # child is intentionally never reaped — a zombie per launch
                # is harmless at pilot volume and unbounded reaping is out
                # of scope for slice 0.
                start_new_session=True,
            )
        except OSError as err:
            # Nothing was executed, so "no run exists" is a fact, not a guess.
            self._try_mark_terminal(store, request.request_id, f"launch failed: {err}")
            return self._refuse(request.request_id, f"cannot start maestro: {err}")
        finally:
            if stderr_handle is not None:
                stderr_handle.close()

        result = self._await_materialization(runs, before, child)
        if result.run_id is not None:
            return self._report_materialized(store, request, key, result.run_id)

        if result.died_without_publishing:
            # Stricter than an ordinary timeout, and deliberately so: the
            # only publisher is dead and a second look still found nothing,
            # so `false` here is knowable rather than a guess (spec §5.3
            # lists "a non-zero exit before publication" under `false`).
            tail = self._tail(stderr_path)
            reason = (
                f"maestro exited {child.returncode} before publishing any "
                f"run under {runs}; nothing was launched."
                + (f" stderr: {tail}" if tail else "")
            )
            self._try_mark_terminal(
                store, request.request_id, f"launch failed: exit {child.returncode}"
            )
            return self._refuse(request.request_id, reason)

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

    def _verb_env(self, home: Path) -> dict[str, str]:
        """Child environment for a control verb.

        Injects the CONFIGURED catalog when there is one, for the same
        reason `_launch` does: an inherited `$ATP_CATALOG` would make the
        verb depend on how the server was started.

        Unlike `submit`, a missing catalog is NOT refused here. `status`
        resolves no models and needs no catalog, and refusing it would undo
        the read path that #174 restored — a control plane that cannot
        report on a run is worse than one that cannot retry a task. `retry`
        does need one; without it the child fails and says so, and its
        stderr reaches the operator through `VerbOutcome`. That is an
        honest failure of the attempt, not a run created in the dark, so it
        does not need the pre-flight refusal `submit` gets.
        """
        env = {**os.environ, "MAESTRO_HOME": str(home)}
        catalog = self._config.atp_catalog
        if catalog is not None:
            env["ATP_CATALOG"] = str(catalog)
        else:
            # Unconfigured means ABSENT, not "whatever the shell had". An
            # inherited value would be the very dependency on how the server
            # was started that this ends — and it would answer the same
            # question two different ways, since `submit` refuses outright
            # while a verb quietly used the ambient one (PR #176 Copilot
            # review). An operator with no configured catalog cannot create
            # runs at all, so nothing legitimate is lost.
            env.pop("ATP_CATALOG", None)
        return env

    def _catalog_path(self) -> Path:
        """The configured ATP catalog, checked before anything is launched.

        maestro resolves a task's model through `$ATP_CATALOG`
        (`maestro/maestro/catalog.py:388`) and halts the whole run when it
        is missing. The pilot's first run died two seconds after a receipt
        that said "started": the variable lived in an interactive shell,
        the server had been started without it, and the child inherited
        that absence. The record went to `materialized` because maestro had
        already published the run directory before it read the catalog, so
        nothing about the failure was visible from the console.

        The value comes from configuration, never from `os.environ`. An
        ambient variable is not a declaration: it is invisible to anyone
        reading the config, differs between an interactive start and a
        service start, and cannot be checked before use. The controller
        already treats `MAESTRO_HOME` this way for the same reason.

        Only declaredness and reachability are checked here. Whether the
        catalog's CONTENTS are valid belongs to ATP and maestro — a
        malformed catalog stays an honest failure of the run itself, and
        duplicating that judgement here would put a second, drifting copy
        of someone else's schema in dispatcher.
        """
        catalog = self._config.atp_catalog
        if catalog is None:
            raise RunRejectedError(
                "atp_catalog is not configured: maestro resolves every "
                "task's model through it and halts without one, so a launch "
                "would produce a run that dies immediately. Set an absolute "
                "atp_catalog in dispatcher.toml"
            )
        if not catalog.is_absolute():
            raise RunRejectedError(
                f"atp_catalog must be an absolute path, got: {catalog}"
            )
        if not catalog.is_file():
            raise RunRejectedError(
                f"atp_catalog does not exist or is not a regular file: {catalog}"
            )
        if not os.access(catalog, os.R_OK):
            raise RunRejectedError(f"atp_catalog is not readable: {catalog}")
        return catalog

    @staticmethod
    def _verb_cwd(record: LaunchRecord) -> Path:
        """The checkout a verb must run maestro from, re-derived and re-checked.

        maestro resolves which repository a run belongs to from the
        directory it is standing in. `_launch` has always passed
        `cwd=<checkout>`; `control` and `end_orphan` did not, so every verb
        inherited the dispatcher SERVER's cwd — normally the dispatcher
        checkout — and asked maestro about the wrong repository. The run was
        then reported as missing ("no run <id> for .../dispatcher; known
        runs: none") no matter how healthy it was. Found by the slice-0
        pilot, 2026-08-24: the same defect class as the launch-side one the
        whole-branch review caught, which is that a repository is named by
        the request and the DAG, never by whatever directory a child happens
        to start in.

        The path comes from the durable record written at `reserve()`, never
        from the caller: a verb must not be re-pointed at another repository
        by its own request body. It is then re-checked rather than trusted —
        a directory can be moved, replaced, or re-pointed at a different
        remote between launch and verb, and acting on the wrong repository
        is worse than refusing.
        """
        if not record.checkout:
            # Written before this field existed. Falling back to the process
            # cwd is exactly the bug, and guessing a checkout from `repo_key`
            # would pick one of possibly several clones, so this refuses.
            raise RunRejectedError(
                f"{record.request_id} predates checkout binding: its record "
                "carries no checkout, and running a verb from the server's "
                "own directory would ask maestro about the wrong repository. "
                "Re-submit the request to get a record that carries one"
            )
        checkout = Path(record.checkout)
        if not checkout.is_absolute():
            # `_checkout` resolves before persisting, so this can only be a
            # record written before it did. Resolving it HERE would resolve
            # against the server process's cwd — the very dependency this
            # binding removes — so it refuses instead.
            raise RunRejectedError(
                f"{record.request_id}: the recorded checkout is relative "
                f"({checkout}); resolving it now would depend on the "
                "server's own directory. Re-submit the request"
            )
        if not (checkout / ".git").exists():
            raise RunRejectedError(
                f"{record.request_id}: the checkout recorded at launch is "
                f"gone or is no longer a git repository ({checkout})"
            )
        try:
            actual = identity_from_checkout(checkout)
        except IdentityError as err:
            raise RunRejectedError(
                f"{record.request_id}: cannot read the identity of the "
                f"recorded checkout {checkout}: {err}"
            ) from err
        if actual.as_text() != record.repo_key:
            raise RunRejectedError(
                f"{record.request_id}: the checkout recorded at launch "
                f"({checkout}) now resolves to {actual.as_text()}, not "
                f"{record.repo_key} — refusing to act on a different "
                "repository than the one the run belongs to"
            )
        return checkout

    @staticmethod
    def _validate_maestro_cli(cli: Path) -> None:
        """Refuse a `maestro_cli` that is not what it claims to be (I6).

        Documented as an ABSOLUTE path
        (`dispatcher/core/discovery.py:54-57`) but never checked before
        this; combined with `cwd=<checkout>` at every subprocess call site
        that uses it, a relative value containing a slash
        (`./bin/maestro`, `tools/maestro`) would execute a binary from
        INSIDE the target repository instead of the configured location.

        "Every call site" became true only on 2026-08-24. When this was
        written, `_launch` passed `cwd=` and `control`/`end_orphan` did not,
        so the sentence described a guarantee two thirds of the code did not
        provide — and the pilot found the verbs asking maestro about the
        dispatcher repository. The absolute-path check below always held; it
        was the cwd half of the claim that was false.
        Mirrors the existing pattern at
        `dispatcher/core/suggest_cli.py:128-133` (H-1): absolute, and the
        file must actually exist.
        """
        if not cli.is_absolute():
            raise RunRejectedError(f"maestro_cli must be an absolute path, got: {cli}")
        if not cli.is_file():
            raise RunRejectedError(f"maestro_cli not found: {cli}")

    @staticmethod
    def _stderr_path(state_dir: Path, request_id: str) -> Path:
        """Where this launch's maestro stderr is captured (I1).

        `request_id` reaching here has already passed
        `RunRequest.request_id`'s pydantic charset constraint (`submit` is
        the only caller of `_launch`), so it is safe to use as a filename.
        """
        log_dir = state_dir / "launch-stderr"
        log_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        return log_dir / f"{request_id}.log"

    @staticmethod
    def _tail(path: Path, limit: int = 4000) -> str:
        """Best-effort: the last `limit` chars of a captured stderr file."""
        try:
            data = path.read_text(errors="replace")
        except OSError:
            return ""
        return data.strip()[-limit:]

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
    ) -> _MaterializationResult:
        """Watch for the rename INTO `runs/` — maestro's own publication point.

        maestro builds the run under `<project>/.staging/<run_id>`, outside
        `runs/`, and renames it in only after the database is closed
        (`maestro/maestro/run_publish.py:45-73`). A new entry here is
        therefore a materialisation defined by the producer.
        """
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            fresh = self._listing_since(runs, before)
            if len(fresh) == 1:
                return _MaterializationResult(run_id=fresh[0])
            if len(fresh) > 1:
                # Two new runs cannot both be ours; the lock is supposed to
                # make this impossible, so it is unknown, not a pick.
                return _MaterializationResult(run_id=None)
            if child.poll() is not None and not fresh:
                # The child is gone and published nothing. It may still have
                # published between these two observations, so this second
                # look — not the exit alone — is what turns a live child's
                # ordinary timeout into a knowable non-launch (I1).
                time.sleep(self._poll)
                late = self._listing_since(runs, before)
                if len(late) == 1:
                    return _MaterializationResult(run_id=late[0])
                if not late and child.returncode != 0:
                    return _MaterializationResult(
                        run_id=None, died_without_publishing=True
                    )
                return _MaterializationResult(run_id=None)
            time.sleep(self._poll)
        return _MaterializationResult(run_id=None)

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

    #: A task id names a FILE inside the run's log directory, so it is the
    #: one part of these paths that arrives off the wire. Two independent
    #: guards, not one: this pattern, and a containment check after
    #: resolution — the pattern is the fence, the check is what still holds
    #: if the fence is ever loosened.
    _TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

    #: Newest-last tails. Generous enough to hold a whole ordinary run, small
    #: enough that one request cannot pull megabytes into a browser.
    _MAX_EVENTS = 500
    _MAX_TASK_LOG_BYTES = 256 * 1024

    def _logs_dir(self, record: LaunchRecord) -> Path:
        """`<run dir>/logs` for an adopted run.

        The run id comes from the durable record, never from the URL: the
        caller names a `request_id`, and which run that is, is dispatcher's
        own recorded answer. That is the same reasoning as `_verb_cwd` —
        a read must not be re-pointed at another run by its own request.
        """
        if record.run_id is None:
            raise RunRejectedError(
                f"{record.request_id} has no run to read logs from "
                f"(state: {record.state})"
            )
        _, _, home = self._require_on()
        key = _key_from_record(record)
        return home.joinpath(
            "projects", *safe_path_parts(key), "runs", record.run_id, "logs"
        )

    def logs(self, request_id: str) -> RunLogs:
        """maestro's own event timeline for this run, plus its task-log names.

        Read-only and on demand: deliberately NOT folded into `view()`, which
        the console polls every five seconds. A growing file re-read on every
        tick is waste, and the poll already rebuilds the whole view — the one
        thing #176's successor had to teach it not to trample.
        """
        record = self._record_for(request_id)
        logs_dir = self._logs_dir(record)
        run_id = record.run_id or ""
        warnings: list[str] = []

        events: list[RunLogEvent] = []
        truncated = False
        events_path = logs_dir / "events.jsonl"
        try:
            lines = events_path.read_text(errors="replace").splitlines()
        except FileNotFoundError:
            # Normal before maestro writes its first event; not a warning.
            lines = []
        except OSError as err:
            # Unreadable is NOT empty — the distinction this codebase keeps
            # insisting on, because the two look identical in a UI.
            warnings.append(f"cannot read {events_path.name}: {err}")
            lines = []
        if len(lines) > self._MAX_EVENTS:
            truncated = True
            lines = lines[-self._MAX_EVENTS :]
        for line in lines:
            events.append(_parse_event(line))

        # Filtered by the SAME rule the reader enforces (codex review, minor):
        # listing a name `task_log()` will reject advertises a log this API
        # will never serve, and the console draws a button for it. Not dropped
        # silently either — a file the operator can see on disk but not here
        # is its own confusion, so it becomes a warning.
        task_logs: list[str] = []
        try:
            for candidate in sorted(logs_dir.glob("*.log")):
                if not candidate.is_file():
                    continue
                if self._TASK_ID_RE.match(candidate.stem):
                    task_logs.append(candidate.stem)
                else:
                    warnings.append(
                        "log file not offered — its name is not a usable "
                        f"task id: {candidate.name}"
                    )
        except OSError as err:
            warnings.append(f"cannot list task logs: {err}")

        return RunLogs(
            run_id=run_id,
            events=events,
            truncated=truncated,
            task_logs=task_logs,
            warnings=warnings,
        )

    def task_log(self, request_id: str, task_id: str) -> TaskLog:
        """The tail of one task's log."""
        if not self._TASK_ID_RE.match(task_id):
            raise RunRejectedError(f"unsafe task id: {task_id!r}")
        record = self._record_for(request_id)
        logs_dir = self._logs_dir(record)
        path = (logs_dir / f"{task_id}.log").resolve()
        # The second guard. `_TASK_ID_RE` already forbids a separator, so
        # this cannot fire today — which is the point: it is what still
        # holds if someone widens the pattern later.
        if not path.is_relative_to(logs_dir.resolve()):
            raise RunRejectedError(f"task log escapes the run directory: {task_id!r}")
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise RunRejectedError(
                f"no log for task {task_id!r} in run {record.run_id}"
            ) from None
        except OSError as err:
            raise RunRejectedError(f"cannot read log for {task_id!r}: {err}") from err
        truncated = len(data) > self._MAX_TASK_LOG_BYTES
        if truncated:
            data = data[-self._MAX_TASK_LOG_BYTES :]
        return TaskLog(
            run_id=record.run_id or "",
            task_id=task_id,
            text=data.decode(errors="replace"),
            truncated=truncated,
        )

    def _record_for(self, request_id: str) -> LaunchRecord:
        """The durable record, or a refusal — shared by the read paths."""
        try:
            record = self._store().get(request_id)
        except RunStoreError as err:
            raise RunRejectedError(
                f"cannot use request_id {request_id!r}: {err}"
            ) from err
        if record is None:
            raise RunRejectedError(f"no launch record for {request_id}")
        return record

    def view(self, request_id: str) -> RunView:
        """The request plus maestro's classification of its run (spec §3.2).

        Status is read through the collector's one classifier; nothing here
        re-derives liveness. A `run_id` whose directory is gone yields
        `run=None` — absent, not invented.
        """
        try:
            record = self._store().get(request_id)
        except RunStoreError as err:
            # Same translation as `record`/`control`: a request_id off the
            # wire carries no pydantic constraint, and an unsafe one is a
            # refusal, not a crash.
            raise RunRejectedError(
                f"cannot use request_id {request_id!r}: {err}"
            ) from err
        if record is None:
            raise RunRejectedError(f"no launch record for {request_id}")
        if record.run_id is None:
            return RunView(record=record)
        # The resolved home, not the raw config field: `maestro_home=None`
        # means "derive from `maestro_db.parent`" (`_require_on()`), and
        # `runs_dir()`/`_launch()` already use this value — the one
        # canonical resolver, not a second fallback expression here.
        _, _, home = self._require_on()
        # A throwaway snapshot: `classified_runs` reports unreadable sources
        # into it. It is not the dashboard's own snapshot, but its
        # `warnings` are still surfaced on the view below — an unreadable
        # run and an absent one must not read the same.
        scratch = ProjectSnapshot(name="maestro", path="")
        match = next(
            (
                info
                for info, _ in classified_runs(home, scratch)
                if info.run_id == record.run_id and info.repo_key == record.repo_key
            ),
            None,
        )
        return RunView(record=record, run=match, warnings=scratch.warnings)

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
        try:
            self._validate_maestro_cli(cli)
        except RunRejectedError as err:
            raise RunRejectedError(f"cannot run maestro {verb}: {err}") from err

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
                # maestro reads a run's repository from the directory it is
                # standing in, so every verb must run in the SAME checkout
                # the launch used — see `_verb_cwd`.
                cwd=str(self._verb_cwd(record)),
                capture_output=True,
                text=True,
                env=self._verb_env(home),
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
            except (RunStoreError, OSError) as err:
                # maestro run-end already succeeded; only releasing the
                # lock/updating the record failed (e.g. `LockBusyError` from
                # a corrupt or foreign-held lock file, or a plain `OSError`
                # from `_write` — I5). Still a refusal, not an unhandled
                # exception — same translation as above.
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
        key = _key_from_record(record)
        before = set(record.known_runs)
        runs = self.runs_dir(key)
        try:
            fresh = [n for n in self._listing(runs) if n not in before]
        except OSError as err:
            # `_listing` no longer reads an unreadable `runs/` as empty
            # (I7). Without this, "cannot list" and "nothing new" would
            # both surface as zero candidates: `resolve_unknown` would say
            # "the launch may never have started" and `end_orphan` would
            # say the named run "is not a candidate" — both false. Wrapped
            # in `RunRejectedError` so the existing 422 handler in
            # `resolve_run` (`dispatcher/server/app.py`) catches it without
            # a new exception type.
            raise RunRejectedError(f"cannot list runs at {runs}: {err}") from err
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
            except (RunStoreError, OSError) as err:
                # A new run WAS observed and correlated; only the durable
                # write failed (e.g. `LockBusyError` from a corrupt or
                # foreign-held lock file, or a plain `OSError` from `_write`
                # — I5). Still a refusal, not an unhandled exception — same
                # translation as `record`/`control`.
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
        record = self._store().get(request_id)
        if record is None:
            # `_candidates` above resolved this request_id, so its record
            # existed moments ago; its absence now is a store invariant
            # violation, not an ordinary failure with a safe fallback.
            raise RunStoreError(f"{request_id} vanished from the store")
        try:
            self._validate_maestro_cli(cli)
        except RunRejectedError as err:
            raise RunRejectedError(f"cannot run maestro run-end: {err}") from err
        try:
            proc = subprocess.run(  # noqa: S603 — argv is a fixed shape
                [str(cli), "run-end", run_id, "--outcome", outcome],
                # Same binding as `control` — this path ends a run too, and
                # ending the wrong repository's run is the worse mistake.
                cwd=str(self._verb_cwd(record)),
                capture_output=True,
                text=True,
                env=self._verb_env(home),
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
        except (RunStoreError, OSError) as err:
            # `maestro run-end` already succeeded above; only releasing the
            # lock/updating the record failed (`LockBusyError`, or a plain
            # `OSError` from `_write` — I5). Still a refusal, not an
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
