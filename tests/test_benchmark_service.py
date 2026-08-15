"""BenchmarkService: render never touches the network (spec §5, NFR-02)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime

from dispatcher.core.benchmark_service import BenchmarkService
from dispatcher.core.benchmarks import BenchmarksReport

BASE = "http://atp.test"


def _ok_report() -> BenchmarksReport:
    return BenchmarksReport(
        status="ok", url=BASE, fetched_at=datetime.now(UTC), error=None
    )


def test_get_serves_initial_report_instantly_without_calling_the_fetcher() -> None:
    started = threading.Event()
    release = threading.Event()

    def slow_fetcher(url: str) -> BenchmarksReport:
        started.set()
        release.wait(timeout=5)
        return _ok_report()

    service = BenchmarkService(BASE, fetcher=slow_fetcher)
    status = service.get()
    # the spinner's data source: a fetch is running while the render is stale
    assert status.fetch_in_flight is True
    # the render path returned BEFORE the fetch finished
    assert status.report.status == "unavailable"
    assert status.report.fetched_at is None and status.report.error is None
    assert started.wait(timeout=5)
    release.set()
    assert service.wait_for_fetch(timeout=5)
    assert service.get(start_fetch=False).report.status == "ok"


def test_start_fetch_false_never_spawns_a_thread() -> None:
    calls: list[str] = []

    def fetcher(url: str) -> BenchmarksReport:
        calls.append(url)
        return _ok_report()

    service = BenchmarkService(BASE, fetcher=fetcher)
    service.get(start_fetch=False)
    assert service.wait_for_fetch(timeout=1)
    assert calls == []


def test_throttle_one_fetch_per_interval() -> None:
    calls: list[str] = []

    def fetcher(url: str) -> BenchmarksReport:
        calls.append(url)
        return _ok_report()

    service = BenchmarkService(BASE, fetcher=fetcher)
    service.get()
    assert service.wait_for_fetch(timeout=5)
    service.get()  # inside the 60s window → no second thread
    assert service.wait_for_fetch(timeout=5)
    assert len(calls) == 1


def test_failed_attempt_replaces_report_and_stamps_fetched_at() -> None:
    def failing_fetcher(url: str) -> BenchmarksReport:
        return BenchmarksReport(
            status="unavailable",
            url=url,
            fetched_at=datetime.now(UTC),
            error="HTTP 500 (http://atp.test/api/v1/benchmarks)",
        )

    service = BenchmarkService(BASE, fetcher=failing_fetcher)
    service.get()
    assert service.wait_for_fetch(timeout=5)
    report = service.get(start_fetch=False).report
    assert report.status == "unavailable"
    assert report.fetched_at is not None  # a real failure, not not-fetched-yet
    assert report.error is not None


def test_crashing_fetcher_becomes_unavailable_not_a_dead_service() -> None:
    def crashing(url: str) -> BenchmarksReport:
        raise RuntimeError("boom")

    service = BenchmarkService(BASE, fetcher=crashing)
    service.get()
    assert service.wait_for_fetch(timeout=5)
    report = service.get(start_fetch=False).report
    assert report.status == "unavailable"
    assert "boom" in (report.error or "")
