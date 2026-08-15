"""Benchmark freshness service: instant cached report + background fetch.

Structural copy of `SyncService` (spec §5): `get()` never awaits the
network; a daemon thread runs `fetch_report` at most once per
_FETCH_MIN_INTERVAL_SECONDS, and each completed attempt atomically
replaces the whole report.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from typing import Callable

from dispatcher.core.benchmarks import (
    BenchmarksReport,
    BenchmarksStatus,
    fetch_report,
    initial_report,
)

_FETCH_MIN_INTERVAL_SECONDS = 60.0

Fetcher = Callable[[str], BenchmarksReport]


def _one_line(text: str) -> str:
    return " ".join(text.split())[:300]


class BenchmarkService:
    """Thread-safe cached report + at-most-one background fetch run."""

    def __init__(self, base_url: str, *, fetcher: Fetcher = fetch_report) -> None:
        self._base_url = base_url
        self._fetcher = fetcher
        self._lock = threading.Lock()
        self._report: BenchmarksReport = initial_report(base_url)
        self._fetch_thread: threading.Thread | None = None
        # private throttle bookkeeping — deliberately NOT serialized (§5:
        # single time semantics; fetched_at on the report is the only clock)
        self._fetch_monotonic: float | None = None

    def get(self, *, start_fetch: bool = True) -> BenchmarksStatus:
        """Return the current status instantly; never awaits the network."""
        with self._lock:
            if start_fetch:
                self._maybe_start_fetch_locked(time.monotonic())
            return BenchmarksStatus(
                report=self._report,
                fetch_in_flight=self._fetch_thread is not None
                and self._fetch_thread.is_alive(),
            )

    def _maybe_start_fetch_locked(self, now: float) -> None:
        if self._fetch_thread is not None and self._fetch_thread.is_alive():
            return
        if (
            self._fetch_monotonic is not None
            and now - self._fetch_monotonic < _FETCH_MIN_INTERVAL_SECONDS
        ):
            return
        self._fetch_monotonic = now
        self._fetch_thread = threading.Thread(target=self._fetch_run, daemon=True)
        self._fetch_thread.start()

    def wait_for_fetch(self, timeout: float | None = None) -> bool:
        """Block until the background run finishes (tests); True if idle."""
        thread = self._fetch_thread
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _fetch_run(self) -> None:
        try:
            report = self._fetcher(self._base_url)
        except Exception as err:  # noqa: BLE001 — сбой обязан всплыть в
            # report.error, не убить сервис (образец: SyncService._fetch_run)
            report = BenchmarksReport(
                status="unavailable",
                url=self._base_url,
                fetched_at=datetime.now(UTC),
                error=_one_line(f"fetch crashed: {type(err).__name__}: {err}"),
            )
        with self._lock:
            self._report = report
