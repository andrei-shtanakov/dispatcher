"""A value refreshed in the background, served without waiting.

Extracted from the human queue's forge reader (spec A2 §1) when the factory
floor (slice C2) needed the same thing: a GitHub read can outlast a client's
request timeout, and a view that flapped to `unavailable` once per TTL
window would be worse than a slightly old one.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Generic, TypeVar

T = TypeVar("T")


def spawn_daemon(fn: Callable[[], None], name: str) -> None:
    """Run *fn* on a daemon thread."""
    threading.Thread(target=fn, name=name, daemon=True).start()


class BackgroundReader(Generic[T]):
    """The last fetched value; a stale one is refreshed by ONE background run.

    `read()` never waits on `fetch`. It returns the last value at once and,
    when that is older than the TTL, starts a refresh — a refresh already in
    flight is never doubled by the next poll. Before the first fetch lands it
    returns `pending`, which must say "in progress", never an empty answer.
    `fetch` must not raise (wrap it); whatever it returns — failures
    included — is cached, so an outage is not re-fetched per poll.
    """

    def __init__(
        self,
        fetch: Callable[[], T],
        pending: T,
        *,
        ttl: float,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None]], None],
    ) -> None:
        self._fetch, self._pending = fetch, pending
        self._ttl, self._clock, self._spawn = ttl, clock, spawn
        self._lock = threading.Lock()
        self._at: float | None = None
        self._value: T | None = None
        self._inflight = False

    def read(self) -> T:
        """The last known value; kicks off a refresh when it is stale."""
        with self._lock:
            stale = self._at is None or self._clock() - self._at >= self._ttl
            start = stale and not self._inflight
            if start:
                self._inflight = True
        if start:
            try:
                self._spawn(self._refresh)
            except Exception:  # noqa: BLE001 — a failed spawn must not wedge
                with self._lock:
                    self._inflight = False
                raise
        with self._lock:
            return self._value if self._value is not None else self._pending

    def _refresh(self) -> None:
        value = self._fetch()
        with self._lock:
            self._value, self._at = value, self._clock()
            self._inflight = False
