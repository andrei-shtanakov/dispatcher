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

from pydantic import BaseModel

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.run_identity import RepoKey, safe_path_parts
from dispatcher.core.run_request import (
    RunRejectedError,
    RunRequest,
    validate_request,
)
from dispatcher.core.run_store import LaunchRecord, LockBusyError, RunStore

_audit = logging.getLogger("dispatcher.runs")

_POLL_INTERVAL = 0.5
_MATERIALIZE_TIMEOUT = 120.0


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
        existing = store.get(request.request_id)
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
        assert reserved is not None  # just written by `reserve`
        before = set(reserved.known_runs)
        argv = [str(cli), "run", request.tasks]
        env = {**os.environ, "MAESTRO_HOME": str(home)}
        store.mark_launching(request.request_id)
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
            store.mark_terminal(request.request_id, f"launch failed: {err}")
            return self._refuse(request.request_id, f"cannot start maestro: {err}")

        run_id = self._await_materialization(runs, before, child)
        if run_id is not None:
            store.mark_materialized(request.request_id, run_id)
            _audit.info(
                "submit request=%s repo=%s run=%s accepted=True",
                request.request_id,
                key.as_text(),
                run_id,
            )
            return LaunchReceipt(
                request_id=request.request_id, run_id=run_id, accepted=True
            )

        reason = (
            "launch_unknown: no run appeared under "
            f"{runs} within {self._timeout:g}s. A run may or may not exist; "
            "resolve it before retrying (spec §5.2.1). The repository lock is "
            "deliberately still held."
        )
        store.mark_unknown(request.request_id, reason)
        _audit.info(
            "submit request=%s repo=%s accepted=None launch_unknown",
            request.request_id,
            key.as_text(),
        )
        return LaunchReceipt(
            request_id=request.request_id, accepted=None, reason=reason
        )

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
