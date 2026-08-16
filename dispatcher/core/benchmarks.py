"""ATP benchmark read model: fetch cycle + fail-closed classification.

Phase 1 (spec 2026-08-15: §4 models, §5 time semantics, §6 client rules)
consumes the public eco-server surface — GET /api/v1/benchmarks and
GET /api/v1/benchmarks/{id}/leaderboard — with no tokens anywhere.

Phase 2 (spec 2026-08-16-atp-benchmark-run-status-design.md) adds the
token-gated GET /api/v1/runs/{id}/status: a single synchronous
click-driven fetch. The token is dispatcher's first stored secret — read
per request from `[benchmarks].token_file` under the §3 fail-closed rules,
sent only in the Authorization header, and never echoed into any report,
error line or log (pinned by the canary test).
"""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

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
    rows: list[LeaderboardRow] = Field(default_factory=list)
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
    benchmarks: list[BenchmarkInfo] = Field(default_factory=list)
    # keys: str(benchmark_id)
    leaderboards: dict[str, LeaderboardState] = Field(default_factory=dict)


class BenchmarksStatus(BaseModel):
    """Report + liveness flag; the whole wire body (§5, §7)."""

    report: BenchmarksReport
    fetch_in_flight: bool


class RunStatusInfo(BaseModel):
    """Mirror of ATP `RunStatusResponse` (phase-2 §6).

    `status` is the producer's own vocabulary passed through verbatim —
    the run's lifecycle is the producer's judgment; the report classifies
    only our read of it. `completed_tasks` is deliberately not mirrored
    (§2 non-goals); `extra="ignore"` drops it.
    """

    model_config = _STRICT

    id: int
    status: str
    current_task_index: int
    tasks_count: int
    total_score: float | None
    score_semantics: dict[str, Any]
    score_components: dict[str, Any]


RunStatusStatusLiteral = Literal[
    "unconfigured",
    "token_unconfigured",
    "token_file_missing",
    "token_file_insecure",
    "token_file_unreadable",
    "unauthorized",
    "not_found",
    "unavailable",
    "unreadable",
    "ok",
]


class RunStatusReport(BaseModel):
    """The wire report of GET /api/benchmarks/runs/{run_id} (phase-2 §6).

    `fetched_at` is null exactly for the config/token states where no
    request was made; `error` is set iff status != "ok" (config/token
    states carry the human-readable reason there too).
    """

    status: RunStatusStatusLiteral
    run_id: int
    fetched_at: datetime | None
    error: str | None
    run: RunStatusInfo | None = None


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


_TokenFailure = tuple[str, str]  # (RunStatusStatusLiteral value, one-line reason)


def read_token_file(path: Path) -> tuple[str, None] | tuple[None, _TokenFailure]:
    """Read the ATP token under the phase-2 §3 fail-closed rules.

    lstat, not stat: a symlink is rejected outright — the permission gate
    is ambiguous through a link (the link's own mode is 0777; gating the
    target invites a check-vs-open race), and a token reached through a
    symlink into a dotfiles checkout is the layout this design must not
    encourage. Every refusal names the mode/path, never the content.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, ("token_file_missing", f"token file does not exist: {path}")
    except OSError as err:
        return None, (
            "token_file_unreadable",
            _one_line(f"{type(err).__name__} stat-ing token file: {path}"),
        )
    if stat.S_ISLNK(st.st_mode):
        return None, (
            "token_file_insecure",
            f"token file is a symlink: {path} — use a regular file",
        )
    if not stat.S_ISREG(st.st_mode):
        return None, (
            "token_file_unreadable",
            f"token file is not a regular file: {path}",
        )
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        return None, (
            "token_file_insecure",
            f"token file is group/other-accessible (0{mode:o}): {path} — chmod 600",
        )
    try:
        text = path.read_text()
    except OSError as err:
        return None, (
            "token_file_unreadable",
            _one_line(f"{type(err).__name__} reading token file: {path}"),
        )
    # Spec §3.3 verbatim: exactly one non-empty line after stripping
    # TRAILING whitespace only. rstrip (not strip) is load-bearing: a
    # leading space would survive into the token and must refuse below —
    # silently "fixing" it would send a token the operator never wrote.
    token = text.rstrip()
    if not token or any(ch.isspace() for ch in token):
        return None, (
            "token_file_unreadable",
            f"token file must hold exactly one non-empty line: {path}",
        )
    return token, None


def fetch_run_status(
    base_url: str | None,
    run_id: int,
    token_file: Path | None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> RunStatusReport:
    """One synchronous, click-driven, token-gated GET (phase-2 §5).

    The token travels only in the Authorization header; redirects stay
    disabled so the header cannot travel anywhere but the configured
    server. Error lines carry status code / exception class / URL only —
    never a response body and never the token (canary-pinned).
    """

    def done(
        status: str,
        *,
        error: str | None = None,
        run: RunStatusInfo | None = None,
        fetched: bool = True,
    ) -> RunStatusReport:
        return RunStatusReport(
            status=status,  # type: ignore[arg-type]
            run_id=run_id,
            fetched_at=datetime.now(UTC) if fetched else None,
            error=error,
            run=run,
        )

    if base_url is None:
        return done(
            "unconfigured", error="no [benchmarks].url configured", fetched=False
        )
    if token_file is None:
        return done(
            "token_unconfigured",
            error="no [benchmarks].token_file configured",
            fetched=False,
        )
    token, failure = read_token_file(token_file)
    if failure is not None:
        status, reason = failure
        return done(status, error=reason, fetched=False)
    segment = quote(str(int(run_id)), safe="")
    url = f"{base_url}/api/v1/runs/{segment}/status"
    with httpx.Client(
        timeout=_TIMEOUT_SECONDS, follow_redirects=False, transport=transport
    ) as client:
        try:
            resp = client.get(url, headers={"Authorization": f"Bearer {token}"})
        except httpx.HTTPError as err:
            return done(
                "unavailable",
                error=_one_line(f"{type(err).__name__}: {err} ({url})"),
            )
    if resp.status_code in (401, 403):
        return done(
            "unauthorized",
            error=_one_line(f"HTTP {resp.status_code} ({url}) — token rejected"),
        )
    if resp.status_code == 404:
        # The producer deliberately conflates a missing run and another
        # owner's run (anti-enumeration); the honest rendering keeps both.
        return done(
            "not_found",
            error=_one_line(
                f"HTTP 404 ({url}) — run not found, or not owned by this token"
            ),
        )
    if resp.status_code // 100 != 2:
        return done("unavailable", error=_one_line(f"HTTP {resp.status_code} ({url})"))
    try:
        payload = resp.json()
    except ValueError:
        return done("unreadable", error=_one_line(f"response is not JSON ({url})"))
    try:
        run = RunStatusInfo.model_validate(payload)
    except ValidationError as err:
        return done(
            "unreadable",
            error=_one_line(
                f"run status failed validation: {err.error_count()} error(s)"
            ),
        )
    return done("ok", run=run)
