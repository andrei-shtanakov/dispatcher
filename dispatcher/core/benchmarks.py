"""ATP benchmark read model: fetch cycle + fail-closed classification.

Spec: docs/superpowers/specs/2026-08-15-atp-benchmark-view-design.md
(§4 models, §5 time semantics, §6 client rules). Phase 1 consumes only the
public eco-server surface — GET /api/v1/benchmarks and
GET /api/v1/benchmarks/{id}/leaderboard — with no tokens anywhere.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

_TIMEOUT_SECONDS = 10.0  # per-phase idle timeout, NOT a wall-clock deadline (§6)
_ERROR_LIMIT = 300

_STRICT = ConfigDict(extra="ignore", strict=True)


class BenchmarkInfo(BaseModel):
    """Mirror of ATP `BenchmarkResponse` (pinned contract, §9)."""

    model_config = _STRICT

    id: int
    name: str
    description: str
    tasks_count: int
    tags: list[str]
    version: str
    family_tag: str | None
    created_at: str  # producer sends a string; we keep it one (§12)


class LeaderboardRow(BaseModel):
    """Mirror of ATP's 4-field benchmark LeaderboardEntry — our own name:
    the producer has two different classes called LeaderboardEntry."""

    model_config = _STRICT

    user_id: int
    agent_name: str
    best_score: float
    run_count: int


class LeaderboardState(BaseModel):
    """One benchmark's leaderboard with its own fail-closed status (§4)."""

    status: Literal["ok", "unavailable", "unreadable"]
    rows: list[LeaderboardRow] = []
    error: str | None = None


class BenchmarksReport(BaseModel):
    """The wire report of GET /api/benchmarks (§4).

    `fetched_at` is the completion time of the attempt whose outcome IS this
    report — failed attempts stamp it exactly like successful ones (§5).
    The not-fetched-yet state is `unavailable` with fetched_at=None AND
    error=None; readers must not treat it as a real failure.
    """

    status: Literal["unconfigured", "ok", "unavailable", "unreadable"]
    url: str | None
    fetched_at: datetime | None
    error: str | None
    benchmarks: list[BenchmarkInfo] = []
    leaderboards: dict[str, LeaderboardState] = {}  # keys: str(benchmark_id)


class BenchmarksStatus(BaseModel):
    """Report + liveness flag; the whole wire body (§5, §7)."""

    report: BenchmarksReport
    fetch_in_flight: bool


_BENCHMARKS_ADAPTER = TypeAdapter(list[BenchmarkInfo])
_ROWS_ADAPTER = TypeAdapter(list[LeaderboardRow])


def unconfigured_report() -> BenchmarksReport:
    """The report served when no [benchmarks].url is configured (§3)."""
    return BenchmarksReport(
        status="unconfigured", url=None, fetched_at=None, error=None
    )


def initial_report(url: str) -> BenchmarksReport:
    """Not-fetched-yet (§5): unavailable, fetched_at AND error both null."""
    return BenchmarksReport(status="unavailable", url=url, fetched_at=None, error=None)


def _one_line(text: str) -> str:
    return " ".join(text.split())[:_ERROR_LIMIT]


def _get_json(
    client: httpx.Client, url: str
) -> tuple[object, None] | tuple[None, tuple[str, str]]:
    """GET url; (payload, None) on success, (None, (status, error)) otherwise.

    Error strings carry exception class / HTTP code / URL only — never the
    response body (§4): a misconfigured URL must not leak a stranger's
    response into the report.
    """
    try:
        resp = client.get(url)
    except httpx.HTTPError as err:
        return None, (
            "unavailable",
            _one_line(f"{type(err).__name__}: {err} ({url})"),
        )
    if resp.status_code // 100 != 2:
        return None, ("unavailable", _one_line(f"HTTP {resp.status_code} ({url})"))
    try:
        return resp.json(), None
    except ValueError:
        return None, ("unreadable", _one_line(f"response is not JSON ({url})"))


def fetch_report(
    base_url: str, *, transport: httpx.BaseTransport | None = None
) -> BenchmarksReport:
    """One sequential fetch cycle (§6); the outcome replaces the whole report.

    `transport` is a test seam (httpx.MockTransport); production passes None.
    """

    def done(
        *,
        status: Literal["ok", "unavailable", "unreadable"],
        error: str | None = None,
        benchmarks: list[BenchmarkInfo] | None = None,
        leaderboards: dict[str, LeaderboardState] | None = None,
    ) -> BenchmarksReport:
        return BenchmarksReport(
            url=base_url,
            fetched_at=datetime.now(UTC),
            status=status,
            error=error,
            benchmarks=benchmarks or [],
            leaderboards=leaderboards or {},
        )

    with httpx.Client(
        timeout=_TIMEOUT_SECONDS, follow_redirects=False, transport=transport
    ) as client:
        payload, failure = _get_json(client, f"{base_url}/api/v1/benchmarks")
        if failure is not None:
            status, error = failure
            return done(
                status=status,  # type: ignore[arg-type]
                error=error,
            )
        try:
            benchmarks = _BENCHMARKS_ADAPTER.validate_python(payload)
        except ValidationError as err:
            return done(
                status="unreadable",
                error=_one_line(
                    f"benchmarks list failed validation: {err.error_count()} error(s)"
                ),
            )
        leaderboards: dict[str, LeaderboardState] = {}
        for bench in benchmarks:
            segment = quote(str(bench.id), safe="")
            lb_url = f"{base_url}/api/v1/benchmarks/{segment}/leaderboard"
            lb_payload, lb_failure = _get_json(client, lb_url)
            if lb_failure is not None:
                lb_status, lb_error = lb_failure
                leaderboards[str(bench.id)] = LeaderboardState(
                    status=lb_status,  # type: ignore[arg-type]
                    error=lb_error,
                )
                continue
            try:
                rows = _ROWS_ADAPTER.validate_python(lb_payload)
            except ValidationError as err:
                leaderboards[str(bench.id)] = LeaderboardState(
                    status="unreadable",
                    error=_one_line(
                        f"leaderboard failed validation: {err.error_count()} error(s)"
                    ),
                )
                continue
            leaderboards[str(bench.id)] = LeaderboardState(status="ok", rows=rows)
        return done(
            status="ok", error=None, benchmarks=benchmarks, leaderboards=leaderboards
        )
