"""Sync freshness service: instant cached report + background fetch run.

TASK-203 (DESIGN-202): the render path always serves a cached/local-only
report (NFR-02: no network on render), while a background thread runs
`git fetch --prune` across workspace repos so ahead/behind becomes current
within the ≤ 30 s verdict budget (NFR-03). The in-flight flag drives the UI
corner spinner from the approved FR-01 acceptance.

Scope note (NFR-01): the background run touches only remote-tracking refs
(`git fetch`), never the working tree — the explicit-click whitelist
{pull, PR} stays intact; background fetch is named in the approved FR-01
acceptance («фоновый fetch с индикатором, не блокирует»).
"""

from __future__ import annotations

import subprocess
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.sync import SyncReport, collect_sync

_REPORT_TTL_SECONDS = 5.0
_FETCH_MIN_INTERVAL_SECONDS = 60.0
_FETCH_TIMEOUT_PER_REPO = 30

Collector = Callable[[DispatcherConfig], SyncReport]
Fetcher = Callable[[Path], list[str]]


class SyncStatus(BaseModel):
    """A sync report plus the freshness metadata every response must carry."""

    report: SyncReport
    report_generated_at: datetime
    fetch_in_flight: bool
    last_fetch_at: datetime | None = None
    last_fetch_error: str | None = None


def fetch_workspace(workspace: Path) -> list[str]:
    """`git fetch --prune` every `<workspace>/*/.git` repo; collect error lines.

    Hidden and underscore-prefixed dirs are skipped (same rule as discovery:
    `_cowork_output` must never be read).
    """
    errors: list[str] = []
    for git_dir in sorted(workspace.glob("*/.git")):
        repo = git_dir.parent
        if repo.name.startswith(("_", ".")):
            continue
        try:
            proc = subprocess.run(
                ["git", "-C", str(repo), "fetch", "--prune", "--quiet"],
                capture_output=True,
                text=True,
                timeout=_FETCH_TIMEOUT_PER_REPO,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as err:
            errors.append(f"{repo.name}: {err}")
            continue
        if proc.returncode != 0:
            errors.append(f"{repo.name}: {proc.stderr.strip() or 'fetch failed'}")
    return errors


class SyncService:
    """Collect-on-demand sync cache + at-most-one background fetch run.

    Thread-safe like `SnapshotService`: the TUI calls `get()` from worker
    threads; the lock serializes cache refresh and fetch bookkeeping, while
    the fetch itself runs outside the lock in a daemon thread.
    """

    def __init__(
        self,
        config: DispatcherConfig,
        *,
        collector: Collector = collect_sync,
        fetcher: Fetcher = fetch_workspace,
    ) -> None:
        self._config = config
        self._collector = collector
        self._fetcher = fetcher
        self._lock = threading.Lock()
        self._report: SyncReport | None = None
        self._report_monotonic = 0.0
        self._report_at: datetime | None = None
        self._fetch_thread: threading.Thread | None = None
        self._fetch_monotonic: float | None = None
        self._last_fetch_at: datetime | None = None
        self._last_fetch_error: str | None = None

    def get(self, *, start_fetch: bool = True) -> SyncStatus:
        """Return the current status instantly; never awaits the network run."""
        with self._lock:
            now = time.monotonic()
            if (
                self._report is None
                or now - self._report_monotonic >= _REPORT_TTL_SECONDS
            ):
                self._report = self._collector(self._config)
                self._report_monotonic = now
                self._report_at = datetime.now(UTC)
            if start_fetch:
                self._maybe_start_fetch_locked(now)
            assert self._report is not None and self._report_at is not None
            return SyncStatus(
                report=self._report,
                report_generated_at=self._report_at,
                fetch_in_flight=self._fetch_thread is not None
                and self._fetch_thread.is_alive(),
                last_fetch_at=self._last_fetch_at,
                last_fetch_error=self._last_fetch_error,
            )

    def _maybe_start_fetch_locked(self, now: float) -> None:
        if self._fetch_thread is not None and self._fetch_thread.is_alive():
            return
        if (
            self._fetch_monotonic is not None
            and now - self._fetch_monotonic < _FETCH_MIN_INTERVAL_SECONDS
        ):
            return
        workspace = next((r for r in self._config.roots if r.is_dir()), None)
        if workspace is None:
            return
        self._fetch_monotonic = now
        self._fetch_thread = threading.Thread(
            target=self._fetch_run, args=(workspace,), daemon=True
        )
        self._fetch_thread.start()

    def _fetch_run(self, workspace: Path) -> None:
        errors = self._fetcher(workspace)
        with self._lock:
            self._last_fetch_at = datetime.now(UTC)
            self._last_fetch_error = "; ".join(errors) if errors else None
            # fresh remote-tracking refs → invalidate so the next get()
            # recollects with current ahead/behind
            self._report_monotonic = 0.0
