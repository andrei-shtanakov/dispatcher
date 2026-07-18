"""CLI adapter for config suggestions (DESIGN-902).

ALL claude-CLI specifics (envelope shape, `.result` extraction, version/
cost keys) live here and never leak upward (H-3) — swapping spawn for a
sidecar HTTP call replaces this module's internals only. Hardening: argv
is built from an allowlisted binary plus code-fixed flags, the bundle
travels via stdin only, shell=False (H-1). Cancel terminates OUR child
process and frees the lock immediately (H-6).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from typing import Any

from pydantic import BaseModel

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.spec_runner_config import TYPED_DEFAULTS
from dispatcher.core.spec_runner_config_schema import validate_typed_fields

SUGGEST_TIMEOUT_S = 60.0
_FIXED_FLAGS = ("-p", "--output-format", "json")
_KILL_WAIT_S = 5.0


def _stop(proc: subprocess.Popen[str]) -> None:
    """Terminate, escalating to SIGKILL if the child ignores SIGTERM.

    Used only where WE own reaping the child (the timeout path in `run()`).
    `cancel()` deliberately does NOT use this: it holds the lock while the
    run-thread is still blocked in `communicate()`, and that thread reaps
    the child once `communicate()` returns — a second, blocking `wait()`
    here would just extend the lock hold for no benefit.
    """
    proc.terminate()
    try:
        proc.wait(timeout=_KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=_KILL_WAIT_S)
        except subprocess.TimeoutExpired:
            # D-state child: unreapable even after SIGKILL — give up
            # rather than leak TimeoutExpired past the Suggest*Error
            # contract; the zombie is reaped by the OS eventually.
            pass


class SuggestUnavailableError(Exception):
    """CLI not configured / not found — the feature degrades honestly."""


class SuggestRunnerBusyError(Exception):
    """One in-flight suggest per process; carries the busy project."""

    def __init__(self, project: str) -> None:
        self.project = project
        super().__init__(f"suggest in flight for {project}")


class SuggestTimeoutError(Exception):
    """CLI exceeded SUGGEST_TIMEOUT_S; the process was terminated."""


class SuggestCancelledError(Exception):
    """A cancel endpoint terminated this run."""


class SuggestInvalidError(Exception):
    """Envelope or `.result` payload unparseable — loud, not silent."""


class Suggestion(BaseModel):
    """One accepted suggestion for one typed field."""

    value: Any
    rationale: str


class SuggestOutcome(BaseModel):
    """Adapter output; no envelope details cross this boundary (H-3)."""

    suggestions: dict[str, Suggestion]
    dropped: list[str]
    cli_version: str | None = None
    duration_s: float
    cost_usd: float | None = None


class SuggestRunner:
    """Serialized executor of suggest CLI calls (pattern: ActionRunner)."""

    def __init__(
        self,
        config: DispatcherConfig,
        *,
        command: tuple[str, ...] | None = None,
    ) -> None:
        self._config = config
        self._command = command
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._current: str | None = None
        self._cancelled = False

    @property
    def current_project(self) -> str | None:
        return self._current

    def _argv(self) -> tuple[str, ...]:
        if self._command is not None:  # test injection, mirrors fake_checker
            return self._command
        configured = self._config.suggest_claude_cli
        if configured is not None:
            # allowlist: absolute path whose basename MUST be `claude` (H-1)
            if not configured.is_absolute() or configured.name != "claude":
                raise SuggestUnavailableError(
                    f"suggest_claude_cli must be an absolute path to a "
                    f"'claude' binary, got: {configured}"
                )
            if not configured.is_file():
                raise SuggestUnavailableError(f"not found: {configured}")
            return (str(configured),)
        found = shutil.which("claude")
        if found is None:
            raise SuggestUnavailableError("claude CLI not found on PATH")
        return (found,)

    def run(
        self, project: str, bundle: dict[str, Any], requested: set[str]
    ) -> SuggestOutcome:
        """One CLI call: spawn, parse envelope, filter suggestions."""
        argv = (*self._argv(), *_FIXED_FLAGS)
        with self._lock:
            if self._current is not None:
                raise SuggestRunnerBusyError(self._current)
            self._current = project
            self._cancelled = False
            started = time.monotonic()
            try:
                self._proc = subprocess.Popen(  # noqa: S603 — allowlisted argv
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except OSError as err:
                self._current = None
                raise SuggestUnavailableError(str(err)) from err
        # communicate OUTSIDE the lock: cancel() needs the lock to terminate
        proc = self._proc
        try:
            stdout, _ = proc.communicate(
                input=json.dumps(bundle, sort_keys=True),
                timeout=SUGGEST_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as err:
            _stop(proc)  # we own reaping here; cancel() does not, see _stop
            raise SuggestTimeoutError("suggest timed out") from err
        finally:
            with self._lock:
                cancelled = self._cancelled
                self._proc = None
                self._current = None
        if cancelled:
            raise SuggestCancelledError("cancelled")
        duration = time.monotonic() - started
        return self._parse(stdout, requested, duration)

    def cancel(self, project: str) -> bool:
        """Terminate THIS project's in-flight run; True if one was killed."""
        with self._lock:
            if self._current is None:
                return False
            if self._current != project:
                raise SuggestRunnerBusyError(self._current)
            self._cancelled = True
            if self._proc is not None:
                # No kill-fallback here (unlike the timeout path's `_stop`):
                # the run-thread's `communicate()` reaps the child once it
                # exits, so we don't own reaping and mustn't block the lock
                # holder on a second `wait()` for it.
                self._proc.terminate()
            return True

    def _parse(
        self, stdout: str, requested: set[str], duration: float
    ) -> SuggestOutcome:
        try:
            envelope = json.loads(stdout)
            payload = json.loads(envelope["result"])
            raw = payload["suggestions"]
            if not isinstance(raw, dict):
                raise TypeError("suggestions is not an object")
        except (json.JSONDecodeError, KeyError, TypeError) as err:
            raise SuggestInvalidError(f"suggestion invalid: {err}") from err
        suggestions: dict[str, Suggestion] = {}
        dropped: list[str] = []
        for name, entry in raw.items():
            value = entry.get("value") if isinstance(entry, dict) else None
            if (
                name not in requested
                or not isinstance(entry, dict)
                or validate_typed_fields({name: value})
                or value == TYPED_DEFAULTS.get(name)
            ):
                dropped.append(name)
                continue
            suggestions[name] = Suggestion(
                value=value, rationale=str(entry.get("rationale", ""))
            )
        cost = envelope.get("total_cost_usd", envelope.get("cost_usd"))
        version = envelope.get("version")
        return SuggestOutcome(
            suggestions=suggestions,
            dropped=sorted(dropped),
            cli_version=str(version) if version is not None else None,
            duration_s=round(duration, 3),
            cost_usd=cost if isinstance(cost, (int, float)) else None,
        )
