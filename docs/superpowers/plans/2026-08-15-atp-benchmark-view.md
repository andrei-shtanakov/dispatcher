# ATP Benchmark View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dispatcher renders the benchmark list and per-benchmark leaderboards
of one configured ATP eco server, read-only, over the public Benchmark API.

**Architecture:** A thin `httpx` client runs inside a background thread of
`BenchmarkService` (structural copy of `SyncService`; NFR-02: no network on
render). `core/benchmarks.py` owns strict Pydantic models and fail-closed
classification (`unconfigured | ok | unavailable | unreadable`). One GET route
serves the cached report; the web panel renders it with hard zero-state rules.
The consumed API surface is vendored as a pruned `openapi.json` + fixtures with
copy-integrity (PR gate) and a scheduled upstream-drift advisory.

**Tech Stack:** Python 3.12+, FastAPI, Pydantic v2, httpx (new dependency),
pytest + anyio, Node harness (`tests/web/`), vanilla-JS SPA.

**Spec:** `docs/superpowers/specs/2026-08-15-atp-benchmark-view-design.md` —
read it before starting; section references (§N) below point into it.

## Global Constraints

- Package management: `uv` only (`uv add httpx`, `uv run pytest`), never pip.
- Before every commit: `uv run ruff format . && uv run ruff check .` and
  `uv run pyrefly check` — both must be clean.
- Git: work on the task's feature branch; **never** commit to `master`.
  PR-1 tasks (1–8) go on branch `feat/atp-benchmark-view-core`; PR-2 tasks
  (9–11) go on `feat/atp-benchmark-view-panel` (created after PR-1 merges).
- Model config everywhere in `core/benchmarks.py`:
  `ConfigDict(extra="ignore", strict=True)` (§4).
- Wire dict keys are strings: `leaderboards: dict[str, LeaderboardState]`
  keyed by `str(benchmark_id)` (§4).
- Single time semantics (§5): `fetched_at` = completion of the attempt that
  formed the current report; no `last_fetch_at` / `last_fetch_error` fields
  anywhere on the wire.
- Error strings: one line, ≤300 chars, never echo response bodies —
  exception class + status code + URL only (§4).
- Line length 88; type hints required; public APIs get docstrings.

---

### Task 1: Config — `benchmarks_url` in `DispatcherConfig`

**Files:**
- Modify: `dispatcher/core/discovery.py` (dataclass at ~line 23, `load_config` at ~line 49)
- Test: `tests/test_discovery_config.py` (create if absent; if a config test file already exists, add there)

**Interfaces:**
- Produces: `DispatcherConfig.benchmarks_url: str | None` (default `None`);
  `load_config` raises `ValueError` on an invalid `[benchmarks].url`.

- [ ] **Step 1: Write the failing tests**

```python
"""Config parsing for [benchmarks] (spec §3)."""

from pathlib import Path

import pytest

from dispatcher.core.discovery import load_config


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "dispatcher.toml"
    p.write_text(body)
    return p


def test_absent_benchmarks_section_yields_none(tmp_path: Path) -> None:
    config = load_config(_write(tmp_path, 'roots = []\n'))
    assert config.benchmarks_url is None


def test_valid_url_is_kept_with_trailing_slash_stripped(tmp_path: Path) -> None:
    config = load_config(
        _write(tmp_path, '[benchmarks]\nurl = "http://127.0.0.1:8000/"\n')
    )
    assert config.benchmarks_url == "http://127.0.0.1:8000"


def test_base_path_is_allowed(tmp_path: Path) -> None:
    config = load_config(
        _write(tmp_path, '[benchmarks]\nurl = "https://host.example/atp/"\n')
    )
    assert config.benchmarks_url == "https://host.example/atp"


@pytest.mark.parametrize(
    "bad",
    [
        "ftp://host",                      # wrong scheme
        "http://",                         # no host
        "/api",                            # not absolute
        "http://host/x?query=1",           # query forbidden
        "http://host/x#frag",              # fragment forbidden
        "http://user:pw@host/",            # userinfo forbidden
    ],
)
def test_invalid_url_is_a_load_time_error(tmp_path: Path, bad: str) -> None:
    path = _write(tmp_path, f'[benchmarks]\nurl = "{bad}"\n')
    with pytest.raises(ValueError):
        load_config(path)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_discovery_config.py -v`
Expected: FAIL — `DispatcherConfig` has no `benchmarks_url`, `load_config`
accepts anything.

- [ ] **Step 3: Implement**

In `discovery.py`, add to the dataclass (after `suggest_claude_cli`):

```python
    # Base URL of one ATP eco server (spec 2026-08-15 §3). None → the
    # benchmark view is off: no service, hidden panel, "unconfigured" report.
    benchmarks_url: str | None = None
```

Add a module-level validator (above `load_config`):

```python
def _validate_benchmarks_url(raw: object) -> str:
    """Spec §3: absolute http(s) URL, host required, no query/fragment/
    userinfo; base path allowed; trailing slash stripped."""
    if not isinstance(raw, str):
        raise ValueError("benchmarks.url must be a string")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"benchmarks.url must be http(s), got: {raw!r}")
    if not parts.hostname:
        raise ValueError(f"benchmarks.url must include a host: {raw!r}")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ValueError(
            f"benchmarks.url must not carry query/fragment/userinfo: {raw!r}"
        )
    return raw.rstrip("/")
```

Add `from urllib.parse import urlsplit` to the imports. In `load_config`,
before the final `return`:

```python
    raw_benchmarks = data.get("benchmarks", {})
    raw_url = raw_benchmarks.get("url") if isinstance(raw_benchmarks, dict) else None
    benchmarks_url = _validate_benchmarks_url(raw_url) if raw_url is not None else None
```

and pass `benchmarks_url=benchmarks_url` in the `DispatcherConfig(...)` call.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_discovery_config.py -v` → PASS.
Then the full suite: `uv run pytest -q` → no regressions.

- [ ] **Step 5: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/discovery.py tests/test_discovery_config.py
git commit -m "feat(config): [benchmarks].url with strict load-time validation (spec §3)"
```

---

### Task 2: `core/benchmarks.py` — models + fetch cycle + classification

**Files:**
- Create: `dispatcher/core/benchmarks.py`
- Test: `tests/test_benchmarks.py`
- Modify: `pyproject.toml` (via `uv add httpx`)

**Interfaces:**
- Produces (all consumed by Tasks 3–5, 7, 9):
  - `BenchmarkInfo(id: int, name: str, description: str, tasks_count: int, tags: list[str], version: str, family_tag: str | None, created_at: str)`
  - `LeaderboardRow(user_id: int, agent_name: str, best_score: float, run_count: int)`
  - `LeaderboardState(status: Literal["ok","unavailable","unreadable"], rows: list[LeaderboardRow] = [], error: str | None = None)`
  - `BenchmarksReport(status: Literal["unconfigured","ok","unavailable","unreadable"], url: str | None, fetched_at: datetime | None, error: str | None, benchmarks: list[BenchmarkInfo] = [], leaderboards: dict[str, LeaderboardState] = {})`
  - `BenchmarksStatus(report: BenchmarksReport, fetch_in_flight: bool)`
  - `unconfigured_report() -> BenchmarksReport`
  - `initial_report(url: str) -> BenchmarksReport`
  - `fetch_report(base_url: str, *, transport: httpx.BaseTransport | None = None) -> BenchmarksReport`

- [ ] **Step 1: Add the dependency**

```bash
uv add httpx
```

Verify `pyproject.toml` gained `httpx>=0.27` (or the resolved floor).

- [ ] **Step 2: Write the failing tests**

`tests/test_benchmarks.py` — classification units using
`httpx.MockTransport` (no sockets here; the real-socket path is Task 7):

```python
"""Fail-closed classification of the ATP benchmark fetch (spec §4-§6)."""

from __future__ import annotations

import json

import httpx

from dispatcher.core.benchmarks import (
    BenchmarksReport,
    fetch_report,
    initial_report,
    unconfigured_report,
)

BASE = "http://atp.test"

BENCH = {
    "id": 1,
    "name": "swe-mini",
    "description": "d",
    "tasks_count": 3,
    "tags": ["code"],
    "version": "1.0",
    "family_tag": None,
    "created_at": "2026-08-01T00:00:00Z",
}
ROW = {"user_id": 7, "agent_name": "bot", "best_score": 0.5, "run_count": 2}


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _serve(benchmarks_resp, leaderboard_resp):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/benchmarks":
            return benchmarks_resp(request)
        return leaderboard_resp(request)

    return _transport(handler)


def test_ok_report_with_one_benchmark_and_rows() -> None:
    transport = _serve(
        lambda r: httpx.Response(200, json=[BENCH]),
        lambda r: httpx.Response(200, json=[ROW]),
    )
    report = fetch_report(BASE, transport=transport)
    assert report.status == "ok"
    assert report.error is None
    assert report.fetched_at is not None
    assert [b.id for b in report.benchmarks] == [1]
    assert report.leaderboards["1"].status == "ok"
    assert report.leaderboards["1"].rows[0].agent_name == "bot"


def test_extra_fields_are_ignored() -> None:
    bench = {**BENCH, "brand_new_field": {"x": 1}}
    row = {**ROW, "another_new": True}
    transport = _serve(
        lambda r: httpx.Response(200, json=[bench]),
        lambda r: httpx.Response(200, json=[row]),
    )
    assert fetch_report(BASE, transport=transport).status == "ok"


def test_strict_types_reject_stringified_int() -> None:
    bench = {**BENCH, "id": "1"}  # str where int is declared → unreadable
    transport = _serve(
        lambda r: httpx.Response(200, json=[bench]),
        lambda r: httpx.Response(200, json=[]),
    )
    report = fetch_report(BASE, transport=transport)
    assert report.status == "unreadable"
    assert report.benchmarks == []


def test_int_for_float_is_the_one_allowed_coercion() -> None:
    row = {**ROW, "best_score": 1}  # int for float: allowed (spec §4)
    transport = _serve(
        lambda r: httpx.Response(200, json=[BENCH]),
        lambda r: httpx.Response(200, json=[row]),
    )
    report = fetch_report(BASE, transport=transport)
    assert report.leaderboards["1"].status == "ok"
    assert report.leaderboards["1"].rows[0].best_score == 1.0


def test_transport_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    report = fetch_report(BASE, transport=_transport(handler))
    assert report.status == "unavailable"
    assert report.error is not None
    assert report.fetched_at is not None  # a failed attempt STAMPS time (§5)


def test_non_2xx_is_unavailable_and_429_is_no_exception() -> None:
    transport = _serve(
        lambda r: httpx.Response(429),
        lambda r: httpx.Response(200, json=[]),
    )
    report = fetch_report(BASE, transport=transport)
    assert report.status == "unavailable"
    assert "429" in (report.error or "")


def test_garbage_json_shape_is_unreadable_never_partial() -> None:
    transport = _serve(
        lambda r: httpx.Response(200, json=[BENCH, {"id": 2}]),  # item 2 broken
        lambda r: httpx.Response(200, json=[]),
    )
    report = fetch_report(BASE, transport=transport)
    assert report.status == "unreadable"
    assert report.benchmarks == []  # not "the one good item"


def test_one_failing_leaderboard_does_not_poison_the_report() -> None:
    bench2 = {**BENCH, "id": 2}

    def lb(request: httpx.Request) -> httpx.Response:
        if "/benchmarks/1/" in request.url.path:
            return httpx.Response(500)
        return httpx.Response(200, json=[ROW])

    transport = _serve(lambda r: httpx.Response(200, json=[BENCH, bench2]), lb)
    report = fetch_report(BASE, transport=transport)
    assert report.status == "ok"
    assert report.leaderboards["1"].status == "unavailable"
    assert report.leaderboards["2"].status == "ok"


def test_empty_benchmark_list_is_confidently_ok() -> None:
    transport = _serve(
        lambda r: httpx.Response(200, json=[]),
        lambda r: httpx.Response(200, json=[]),
    )
    report = fetch_report(BASE, transport=transport)
    assert report.status == "ok"
    assert report.benchmarks == []
    assert report.leaderboards == {}


def test_error_lines_never_echo_response_bodies() -> None:
    secret = "SECRET-BODY-TOKEN"
    transport = _serve(
        lambda r: httpx.Response(500, text=secret),
        lambda r: httpx.Response(200, json=[]),
    )
    report = fetch_report(BASE, transport=transport)
    assert secret not in json.dumps(report.model_dump(mode="json"))


def test_leaderboard_url_uses_quoted_id_segment() -> None:
    seen: list[str] = []

    def lb(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json=[])

    transport = _serve(lambda r: httpx.Response(200, json=[BENCH]), lb)
    fetch_report(BASE, transport=transport)
    assert seen == ["/api/v1/benchmarks/1/leaderboard"]


def test_report_constructors() -> None:
    un = unconfigured_report()
    assert un.status == "unconfigured" and un.url is None and un.error is None
    init = initial_report(BASE)
    assert init.status == "unavailable"
    assert init.fetched_at is None and init.error is None  # not-fetched-yet (§5)


def test_wire_model_roundtrips() -> None:
    transport = _serve(
        lambda r: httpx.Response(200, json=[BENCH]),
        lambda r: httpx.Response(200, json=[ROW]),
    )
    report = fetch_report(BASE, transport=transport)
    assert BenchmarksReport.model_validate(report.model_dump()) == report
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_benchmarks.py -v`
Expected: FAIL — `dispatcher.core.benchmarks` does not exist.

- [ ] **Step 4: Implement `dispatcher/core/benchmarks.py`**

```python
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
    return BenchmarksReport(
        status="unavailable", url=url, fetched_at=None, error=None
    )


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

    def done(**fields: object) -> BenchmarksReport:
        return BenchmarksReport(
            url=base_url, fetched_at=datetime.now(UTC), **fields
        )

    with httpx.Client(
        timeout=_TIMEOUT_SECONDS, follow_redirects=False, transport=transport
    ) as client:
        payload, failure = _get_json(client, f"{base_url}/api/v1/benchmarks")
        if failure is not None:
            status, error = failure
            return done(status=status, error=error)
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
                    status=lb_status, error=lb_error
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_benchmarks.py -v` → all PASS.

- [ ] **Step 6: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/benchmarks.py tests/test_benchmarks.py pyproject.toml uv.lock
git commit -m "feat(benchmarks): strict models + fail-closed fetch cycle (spec §4-§6)"
```

---

### Task 3: `BenchmarkService` — cached report + background fetch

**Files:**
- Create: `dispatcher/core/benchmark_service.py`
- Test: `tests/test_benchmark_service.py`

**Interfaces:**
- Consumes: `fetch_report`, `initial_report`, `BenchmarksReport`,
  `BenchmarksStatus` from Task 2.
- Produces: `BenchmarkService(base_url: str, *, fetcher=fetch_report)` with
  `.get(*, start_fetch: bool = True) -> BenchmarksStatus` and
  `.wait_for_fetch(timeout: float | None = None) -> bool` (used by Tasks 4–5).

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_benchmark_service.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement `dispatcher/core/benchmark_service.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_benchmark_service.py -v` → PASS.

- [ ] **Step 5: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/benchmark_service.py tests/test_benchmark_service.py
git commit -m "feat(benchmarks): BenchmarkService — cached report, throttled background fetch (spec §5)"
```

---

### Task 4: `read_api.benchmarks`

**Files:**
- Modify: `dispatcher/core/read_api.py`
- Test: `tests/test_benchmarks.py` (append)

**Interfaces:**
- Consumes: `BenchmarkService` (Task 3), `BenchmarksStatus`,
  `unconfigured_report` (Task 2).
- Produces: `read_api.benchmarks(service: BenchmarkService | None) -> BenchmarksStatus` (used by Task 5).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_benchmarks.py`)

```python
def test_read_api_benchmarks_without_service_is_unconfigured() -> None:
    from dispatcher.core import read_api

    status = read_api.benchmarks(None)
    assert status.report.status == "unconfigured"
    assert status.fetch_in_flight is False


def test_read_api_benchmarks_passes_through_the_service() -> None:
    from dispatcher.core import read_api
    from dispatcher.core.benchmark_service import BenchmarkService

    service = BenchmarkService("http://atp.test", fetcher=lambda url: initial_report(url))
    status = read_api.benchmarks(service)
    assert status.report.url == "http://atp.test"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_benchmarks.py -k read_api -v`
Expected: FAIL — `read_api` has no `benchmarks`.

- [ ] **Step 3: Implement** — in `read_api.py`, add imports

```python
from dispatcher.core.benchmark_service import BenchmarkService
from dispatcher.core.benchmarks import BenchmarksStatus, unconfigured_report
```

and the function (near `sync`-related helpers):

```python
def benchmarks(service: BenchmarkService | None) -> BenchmarksStatus:
    """Spec §7: the one shape both surfaces return.

    No configured service (config.benchmarks_url is None) is answered HERE
    with the unconfigured report, so the route has exactly one return type.
    """
    if service is None:
        return BenchmarksStatus(report=unconfigured_report(), fetch_in_flight=False)
    return service.get()
```

- [ ] **Step 4: Run tests** → PASS, then full suite `uv run pytest -q`.

- [ ] **Step 5: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/read_api.py tests/test_benchmarks.py
git commit -m "feat(benchmarks): read_api.benchmarks facade (spec §7)"
```

---

### Task 5: Route `GET /api/benchmarks` + wiring in `create_app`

**Files:**
- Modify: `dispatcher/server/app.py` (`create_app` signature ~line 162; add route near the sync routes ~line 295)
- Test: `tests/test_api.py` (append)

**Interfaces:**
- Consumes: `read_api.benchmarks` (Task 4), `BenchmarkService` (Task 3),
  `DispatcherConfig.benchmarks_url` (Task 1).
- Produces: `GET /api/benchmarks` returning `BenchmarksStatus` JSON; a
  `benchmark_service` keyword parameter on `create_app` (test seam, used by
  Task 7 and the PR-2 harness).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_api.py`;
  reuse that file's existing `_client`/`make_atp` helpers)

```python
async def test_benchmarks_unconfigured_shape_is_pinned(tmp_path: Path) -> None:
    """No [benchmarks].url → the exact unconfigured wire body (spec §3, §7)."""
    async with _client(tmp_path) as client:
        resp = await client.get("/api/benchmarks")
    assert resp.status_code == 200
    assert resp.json() == {
        "report": {
            "status": "unconfigured",
            "url": None,
            "fetched_at": None,
            "error": None,
            "benchmarks": [],
            "leaderboards": {},
        },
        "fetch_in_flight": False,
    }


async def test_benchmarks_route_serves_injected_service_report(
    tmp_path: Path,
) -> None:
    from dispatcher.core.benchmark_service import BenchmarkService
    from dispatcher.core.benchmarks import initial_report

    service = BenchmarkService(
        "http://atp.test", fetcher=lambda url: initial_report(url)
    )
    make_atp(tmp_path)
    config = DispatcherConfig(roots=(tmp_path,), maestro_db=make_maestro_home(tmp_path))
    app = create_app(config, benchmark_service=service)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/benchmarks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["report"]["status"] == "unavailable"
    assert body["report"]["url"] == "http://atp.test"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_api.py -k benchmarks -v`
Expected: FAIL — 404, route absent.

- [ ] **Step 3: Implement** — in `app.py`:

Imports:

```python
from dispatcher.core.benchmark_service import BenchmarkService
from dispatcher.core.benchmarks import BenchmarksStatus
```

`create_app` signature gains `benchmark_service: BenchmarkService | None = None`
(after `suggest_runner`). In the body, next to the other service wiring:

```python
    benchmarks_service = benchmark_service or (
        BenchmarkService(config.benchmarks_url) if config.benchmarks_url else None
    )
```

Route (next to the sync GET routes):

```python
    @app.get("/api/benchmarks", response_model=BenchmarksStatus)
    def benchmarks_view() -> BenchmarksStatus:
        """Spec §7: global read-only report; state lives in the body (200 always)."""
        return read_api.benchmarks(benchmarks_service)
```

- [ ] **Step 4: Run tests** — the two new ones, then the whole file, then the
  full suite: `uv run pytest -q` → no regressions.

- [ ] **Step 5: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add dispatcher/server/app.py tests/test_api.py
git commit -m "feat(api): GET /api/benchmarks — cached BenchmarksStatus (spec §7)"
```

---

### Task 6: Vendored contract `contracts/atp-benchmark-api/v1/`

**Files:**
- Create: `scripts/prune_atp_openapi.py`
- Create: `scripts/revendor_atp_benchmark_api.sh`
- Create: `contracts/atp-benchmark-api/v1/openapi.json` (generated)
- Create: `contracts/atp-benchmark-api/v1/fixtures/benchmarks.json`,
  `contracts/atp-benchmark-api/v1/fixtures/leaderboard.json`,
  `contracts/atp-benchmark-api/v1/fixtures/leaderboard-empty.json`
- Create: `contracts/atp-benchmark-api/v1/manifest.json`, `PINNED.txt` (generated)
- Create: `docs/revendor-atp-benchmark-api.md`
- Test: `tests/test_atp_benchmark_api_vendor.py`

**Interfaces:**
- Consumes: `scripts/vendor_manifest.py` (`build_manifest(root, producer_commit, contract, contract_version)`).
- Produces: the vendored directory used by Task 7's stub and by
  `tests/test_benchmarks.py` fixture-pin test added here.

- [ ] **Step 1: Write `scripts/prune_atp_openapi.py`**

```python
"""Prune an ATP eco openapi.json to the surface dispatcher consumes.

Spec §9: keep exactly the two consumed GET paths and the component schemas
they transitively reference; write canonical bytes (sorted keys, indent 2,
trailing newline) so upstream-drift can compare file digests directly.

Usage: prune_atp_openapi.py <full-openapi.json> <out-pruned.json>
"""

from __future__ import annotations

import json
import sys
from typing import Any

KEPT_PATHS = (
    "/api/v1/benchmarks",
    "/api/v1/benchmarks/{benchmark_id}/leaderboard",
)


def _collect_refs(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[1])
        for value in node.values():
            _collect_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, found)


def prune(full: dict[str, Any]) -> dict[str, Any]:
    paths = {p: {"get": full["paths"][p]["get"]} for p in KEPT_PATHS}
    schemas: dict[str, Any] = full.get("components", {}).get("schemas", {})
    kept: set[str] = set()
    frontier: set[str] = set()
    _collect_refs(paths, frontier)
    while frontier:
        name = frontier.pop()
        if name in kept or name not in schemas:
            continue
        kept.add(name)
        _collect_refs(schemas[name], frontier)
    return {
        "openapi": full["openapi"],
        "info": {"title": full["info"]["title"], "version": full["info"]["version"]},
        "paths": paths,
        "components": {"schemas": {n: schemas[n] for n in sorted(kept)}},
    }


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as fh:
        full = json.load(fh)
    pruned = prune(full)
    with open(dst, "w") as fh:
        json.dump(pruned, fh, indent=2, sort_keys=True)
        fh.write("\n")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Write `scripts/revendor_atp_benchmark_api.sh`**

Adapt the staging/verify/swap machinery from
`scripts/revendor_steward_gate_catalog.sh` (same trap-restore discipline,
same exit-code scheme — read that file first; it documents itself). The
parts that differ:

- Usage: `scripts/revendor_atp_benchmark_api.sh <NEW_PIN> [--from <atp-platform-checkout>]`
  (default `--from ../atp-platform`).
- Generation instead of git-object extraction: check out `<NEW_PIN>` in a
  temporary worktree of the atp-platform checkout
  (`git -C "$FROM" worktree add "$TMP_WT" "$NEW_PIN"`), then:

```bash
ATP_SERVER_PROFILE=eco uv run --project "$TMP_WT" python - <<'PY' > "$STAGING/openapi-full.json"
import json
from atp.dashboard.v2.factory import create_app
print(json.dumps(create_app().openapi()))
PY
python3 "$REPO_ROOT/scripts/prune_atp_openapi.py" \
  "$STAGING/openapi-full.json" "$STAGING/openapi.json"
rm "$STAGING/openapi-full.json"
git -C "$FROM" worktree remove --force "$TMP_WT"
```

- Fixtures are **carried over** from the existing vendored copy (they are
  consumer-maintained examples; the runbook step below covers refreshing
  them by hand when the shape moves).
- Manifest via the house generator:

```bash
python3 "$REPO_ROOT/scripts/vendor_manifest.py" "$STAGING" "$NEW_PIN" \
  atp-benchmark-api 1
```

(Check `vendor_manifest.py main()` for the exact argv order before wiring —
mirror how `revendor_steward_gate_catalog.sh` calls it.)

- [ ] **Step 3: Produce the initial vendored copy**

Pin = current `master` HEAD of `../atp-platform` (record the SHA):

```bash
git -C ../atp-platform rev-parse master
bash scripts/revendor_atp_benchmark_api.sh <THAT_SHA> --from ../atp-platform
```

Fixtures (first vendoring only — later re-vendors carry them over):

1. **Preferred — live capture:** boot the eco server from the checkout
   (`ATP_SERVER_PROFILE=eco uv run --project ../atp-platform uvicorn
   atp.dashboard.v2.factory:app --port 8600`), create one benchmark via its
   `/docs` or seed script if available, then
   `curl http://127.0.0.1:8600/api/v1/benchmarks > fixtures/benchmarks.json`
   and the same for one populated and one empty leaderboard.
2. **Fallback (if the server cannot boot without external setup):** author
   the three fixture files by hand to conform to the pruned openapi
   component schemas, mark them `"authored, schema-validated"` in
   `PINNED.txt`, and tell the user — the spec (§9) prefers live captures,
   so this is a recorded deviation, not a silent one.

Either way, finish with a fixture-pin test appended to
`tests/test_benchmarks.py`:

```python
def test_vendored_fixtures_parse_as_ok() -> None:
    """The pinned contract and the consumer models must agree, forever (§10)."""
    import json
    from pathlib import Path

    fixtures = (
        Path(__file__).parent.parent
        / "contracts" / "atp-benchmark-api" / "v1" / "fixtures"
    )
    from dispatcher.core.benchmarks import _BENCHMARKS_ADAPTER, _ROWS_ADAPTER

    assert _BENCHMARKS_ADAPTER.validate_python(
        json.loads((fixtures / "benchmarks.json").read_text())
    )
    assert _ROWS_ADAPTER.validate_python(
        json.loads((fixtures / "leaderboard.json").read_text())
    )
    assert (
        _ROWS_ADAPTER.validate_python(
            json.loads((fixtures / "leaderboard-empty.json").read_text())
        )
        == []
    )
```

- [ ] **Step 4: Write the copy-integrity test**

`tests/test_atp_benchmark_api_vendor.py` — copy the three-test structure of
`tests/test_gate_catalog_vendor.py` verbatim (docstring included, adjusted),
changing only:

```python
VENDORED_ROOT = (
    Path(__file__).parent.parent / "contracts" / "atp-benchmark-api" / "v1"
)
PRODUCER_COMMIT = "<THE SHA FROM STEP 3>"
```

and the manifest assertions to `contract == "atp-benchmark-api"`,
`contract_version == 1`. Run it: `uv run pytest tests/test_atp_benchmark_api_vendor.py -v` → PASS.

- [ ] **Step 5: Write the runbook `docs/revendor-atp-benchmark-api.md`**

Follow the structure of `docs/revendor-steward-gate-catalog.md`: when to
re-vendor (drift advisory red, or consuming a new field), the one-command
procedure, the fixture-refresh step (fixtures are consumer-maintained: on a
shape change, re-capture or re-author them and re-run
`test_vendored_fixtures_parse_as_ok`), and the reminder to bump
`PRODUCER_COMMIT` in `tests/test_atp_benchmark_api_vendor.py`.

- [ ] **Step 6: Run the full suite, format, lint, type-check, commit**

```bash
uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add contracts/atp-benchmark-api scripts/prune_atp_openapi.py \
  scripts/revendor_atp_benchmark_api.sh docs/revendor-atp-benchmark-api.md \
  tests/test_atp_benchmark_api_vendor.py tests/test_benchmarks.py
git commit -m "feat(contracts): vendor atp-benchmark-api/v1 — pruned openapi + fixtures + copy-integrity (spec §9)"
```

---

### Task 7: Integration stub — real socket end-to-end

**Files:**
- Test: `tests/test_benchmarks_stub_integration.py`

**Interfaces:**
- Consumes: `fetch_report` (Task 2), vendored fixtures (Task 6).

- [ ] **Step 1: Write the test**

```python
"""End-to-end over a real socket: FastAPI stub serving the vendored
fixtures → real httpx client → ok report (spec §10). Failure paths:
connection refused → unavailable; 500 → unavailable; wrong shape →
unreadable."""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from dispatcher.core.benchmarks import fetch_report

FIXTURES = (
    Path(__file__).parent.parent
    / "contracts" / "atp-benchmark-api" / "v1" / "fixtures"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _serve(app: FastAPI) -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("stub server did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _fixture_app() -> FastAPI:
    benchmarks = json.loads((FIXTURES / "benchmarks.json").read_text())
    rows = json.loads((FIXTURES / "leaderboard.json").read_text())
    app = FastAPI()

    @app.get("/api/v1/benchmarks")
    def list_benchmarks() -> list[dict]:
        return benchmarks

    @app.get("/api/v1/benchmarks/{benchmark_id}/leaderboard")
    def leaderboard(benchmark_id: int) -> list[dict]:
        return rows

    return app


def test_fixture_stub_yields_an_ok_report() -> None:
    with _serve(_fixture_app()) as base:
        report = fetch_report(base)
    assert report.status == "ok"
    assert report.benchmarks
    first_id = str(report.benchmarks[0].id)
    assert report.leaderboards[first_id].status == "ok"


def test_connection_refused_is_unavailable() -> None:
    report = fetch_report(f"http://127.0.0.1:{_free_port()}")
    assert report.status == "unavailable"
    assert report.error is not None


def test_500_is_unavailable_and_wrong_shape_is_unreadable() -> None:
    app = FastAPI()

    @app.get("/api/v1/benchmarks")
    def broken() -> dict:
        return {"not": "a list"}

    with _serve(app) as base:
        report = fetch_report(base)
    assert report.status == "unreadable"
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_benchmarks_stub_integration.py -v`
Expected: PASS (implementation exists since Task 2; this proves the real
socket + real client path). If it fails, the failure is real — debug, don't
skip.

- [ ] **Step 3: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add tests/test_benchmarks_stub_integration.py
git commit -m "test(benchmarks): real-socket stub integration over vendored fixtures (spec §10)"
```

---

### Task 8: Upstream-drift advisory + PR-1 wrap-up

**Files:**
- Create: `scripts/atp_openapi_drift_report.py`
- Create: `.github/workflows/atp-openapi-drift.yml`
- Modify: `TODO.md` (progress note under `@id:atp-benchmark-view`)

**Interfaces:**
- Consumes: `scripts/prune_atp_openapi.py` (Task 6), the vendored
  `openapi.json` (Task 6).

- [ ] **Step 1: Write `scripts/atp_openapi_drift_report.py`**

```python
"""Upstream drift for atp-benchmark-api/v1 (guarantee B, spec §9).

Compares the sha256 of the VENDORED pruned openapi.json against a freshly
REGENERATED pruned openapi.json (produced by the caller — this script only
compares and reports). The directory tree hash is copy-integrity's artifact
and is deliberately not consulted here.

Exit codes: 0 no drift · 1 drift · 2 unavailable (missing input — fix the
observation, never assume "in sync").

Usage: atp_openapi_drift_report.py <regenerated-pruned-openapi.json>
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

VENDORED = (
    Path(__file__).resolve().parents[1]
    / "contracts" / "atp-benchmark-api" / "v1" / "openapi.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: atp_openapi_drift_report.py <regenerated.json>")
        return 2
    regenerated = Path(sys.argv[1])
    if not VENDORED.is_file():
        print(f"unavailable: vendored copy missing at {VENDORED}")
        return 2
    if not regenerated.is_file():
        print(f"unavailable: regenerated file missing at {regenerated}")
        return 2
    ours, theirs = _sha256(VENDORED), _sha256(regenerated)
    if ours == theirs:
        print(f"no drift: {ours}")
        return 0
    print(f"DRIFT: vendored {ours} != regenerated {theirs}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Write `.github/workflows/atp-openapi-drift.yml`**

Model it on the existing `.github/workflows/upstream-drift.yml` (read it
first for the schedule/permissions idioms of this repo):

```yaml
name: atp-openapi-drift
on:
  schedule:
    - cron: "17 5 * * 1"   # weekly; advisory, not required, never on PR
  workflow_dispatch: {}
permissions:
  contents: read
jobs:
  drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/checkout@v4
        with:
          repository: andrei-shtanakov/atp-platform
          path: atp-platform-canon
      - uses: astral-sh/setup-uv@v5
      - name: Regenerate pruned openapi from canon default branch
        run: |
          set -euo pipefail
          echo "canon commit: $(git -C atp-platform-canon rev-parse HEAD)"
          ATP_SERVER_PROFILE=eco uv run --project atp-platform-canon python - <<'PY' > full.json
          import json
          from atp.dashboard.v2.factory import create_app
          print(json.dumps(create_app().openapi()))
          PY
          python3 scripts/prune_atp_openapi.py full.json regenerated.json
      - name: Compare against the vendored pin
        run: python3 scripts/atp_openapi_drift_report.py regenerated.json
```

A red `Regenerate` step is the exit-2 class: the job fails without reaching
the compare, which reads as "observation broken", never as "no drift".

- [ ] **Step 3: Update `TODO.md`** — under the `@id:atp-benchmark-view` item's
  indented context (NOT the checkbox line), append a progress line:

```
      Прогресс: спека + план (PR #143); PR-1 — вендор atp-benchmark-api/v1,
      core/benchmarks.py, BenchmarkService, GET /api/benchmarks, стаб-
      интеграция, drift-workflow (PR #<этот PR>). Осталось: PR-2 web-панель.
```

- [ ] **Step 4: Full suite, format, lint, type-check, commit, push, PR**

```bash
uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add scripts/atp_openapi_drift_report.py .github/workflows/atp-openapi-drift.yml TODO.md
git commit -m "feat(contracts): scheduled atp-openapi drift advisory (spec §9)"
git push -u origin feat/atp-benchmark-view-core
gh pr create --title "feat: ATP benchmark view — core, contract, API (PR-1)" \
  --body "PR-1 of docs/superpowers/specs/2026-08-15-atp-benchmark-view-design.md ..."
```

Then: read the Copilot review, fix valid findings, wait for human merge.

---

### Task 9 (PR-2): Web panel — Benchmarks section

**Files:**
- Modify: `dispatcher/server/static/index.html`
  (markup near `#sync-section` ~line 141; JS: new render function near
  `renderSync` ~line 370; fetch in `refresh()` `Promise.all` ~line 503)

**Interfaces:**
- Consumes: `GET /api/benchmarks` (Task 5) — the `BenchmarksStatus` JSON
  shape from Task 2.
- Produces: DOM contract for Task 10's harness: section `#benchmarks-section`
  (has `hidden` attribute when unconfigured), status line
  `#benchmarks-status`, list `#benchmarks-list`, table container
  `#benchmarks-leaderboard`.

- [ ] **Step 1: Add the markup** after the `#sync-section` block:

```html
<section id="benchmarks-section" hidden><h2>Benchmarks
  <span id="benchmarks-status" class="fresh"></span></h2>
  <div id="benchmarks-list"></div>
  <div id="benchmarks-leaderboard"></div></section>
```

- [ ] **Step 2: Add the render function** (near `renderSync`; use the
  page's existing `esc()` for every producer string):

```javascript
let benchSelected = null;  // benchmark id (string) whose leaderboard is open

function renderBenchmarks(status) {
  const section = document.getElementById("benchmarks-section");
  const r = status.report;
  if (r.status === "unconfigured") { section.hidden = true; return; }
  section.hidden = false;
  const when = r.fetched_at ? new Date(r.fetched_at).toLocaleTimeString() : "—";
  const spin = status.fetch_in_flight ? " ⟳" : "";
  document.getElementById("benchmarks-status").innerHTML =
    `${esc(r.url ?? "")} · fetched ${esc(when)}${spin}` +
    (r.error ? ` · <span class="err">${esc(r.error)}</span>` : "");
  const list = document.getElementById("benchmarks-list");
  if (r.status !== "ok") {
    // unavailable/unreadable — explicit unknown, NEVER an empty list.
    // fetched_at:null + error:null is "not fetched yet", not a failure.
    list.innerHTML = `<div class="warn">benchmarks unknown: ${
      r.error ? esc(r.status) : "not fetched yet"}</div>`;
    document.getElementById("benchmarks-leaderboard").innerHTML = "";
    return;
  }
  if (!r.benchmarks.length) {
    list.innerHTML = `<div class="fresh">0 benchmarks</div>`;  // confident: ok
    document.getElementById("benchmarks-leaderboard").innerHTML = "";
    return;
  }
  list.innerHTML = r.benchmarks.map(b => `
    <button class="bench" data-bench="${esc(String(b.id))}">
      ${esc(b.name)} v${esc(b.version)} · ${esc(String(b.tasks_count))} tasks
      ${(b.tags || []).map(t => `<span class="badge dim">${esc(t)}</span>`).join("")}
    </button>`).join("");
  renderLeaderboard(r);
}

function renderLeaderboard(r) {
  const box = document.getElementById("benchmarks-leaderboard");
  if (benchSelected === null || !(benchSelected in (r.leaderboards || {}))) {
    box.innerHTML = "";
    return;
  }
  const lb = r.leaderboards[benchSelected];
  if (lb.status !== "ok") {
    box.innerHTML = `<div class="warn">leaderboard unknown (${esc(lb.status)}):
      ${esc(lb.error ?? "")}</div>`;
    return;
  }
  if (!lb.rows.length) {
    box.innerHTML = `<div class="fresh">0 entries</div>`;  // confident: ok
    return;
  }
  box.innerHTML = `<table><tr><th>agent</th><th>best</th><th>runs</th>
    <th>user</th></tr>` + lb.rows.map(row => `
    <tr><td>${esc(row.agent_name)}</td><td>${esc(String(row.best_score))}</td>
    <td>${esc(String(row.run_count))}</td>
    <td>${esc(String(row.user_id))}</td></tr>`).join("") + "</table>";
}

document.getElementById("benchmarks-list").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-bench]");
  if (!btn) return;
  benchSelected = btn.dataset.bench;
  refresh();
});
```

- [ ] **Step 3: Wire into `refresh()`** — add `get("/api/benchmarks")` to the
  existing `Promise.all` and call `renderBenchmarks(benchmarks)` next to
  `renderSync(sync)`. The page's single refresh loop IS the stale-guard here
  (one global section, one writer); no extra generation counter is needed —
  note this in a one-line comment.

- [ ] **Step 4: Manual smoke** — `uv run dispatcher serve` (no `[benchmarks]`
  in `dispatcher.toml`) → section hidden; add
  `[benchmarks] url = "http://127.0.0.1:9"` → section visible, shows
  "not fetched yet" then the unavailable error after the first cycle.

- [ ] **Step 5: Commit**

```bash
git add dispatcher/server/static/index.html
git commit -m "feat(web): Benchmarks panel — list + leaderboard, hard zero states (spec §8)"
```

---

### Task 10 (PR-2): Node harness rules for the panel

**Files:**
- Create: `tests/web/benchmarks_harness.js`
- Create: `tests/test_benchmarks_js.py`

**Interfaces:**
- Consumes: the DOM contract from Task 9 (`#benchmarks-section`,
  `#benchmarks-status`, `#benchmarks-list`, `#benchmarks-leaderboard`) and
  the `BenchmarksStatus` JSON shape from Task 2.

- [ ] **Step 1: Write the harness** — copy the loader/VM scaffolding from
  `tests/web/product_proposals_harness.js` (it loads `index.html`, runs the
  whole `<script>` over `tests/web/dom.js`, and stubs `fetch`). Cases —
  each drives `renderBenchmarks(...)` through a stubbed `/api/benchmarks`
  response and asserts DOM state:

  1. `unconfigured` → `#benchmarks-section` has `hidden`.
  2. `ok` + empty `benchmarks` → text contains `0 benchmarks`.
  3. `unavailable` with `fetched_at: null, error: null` → text contains
     `not fetched yet`, does NOT contain `0 benchmarks`.
  4. `unavailable` with an error → text contains `unknown`, never
     `0 benchmarks`.
  5. `unreadable` → same rule as 4.
  6. `ok` + one benchmark whose leaderboard is `ok` with `rows: []`, after a
     click on its button → `0 entries`.
  7. `ok` + leaderboard `unavailable`, after click → `leaderboard unknown`,
     never `0 entries`.
  8. Producer strings are escaped: a benchmark named
     `<img src=x onerror=alert(1)>` must not create an element (assert the
     rendered HTML contains `&lt;img`).

- [ ] **Step 2: Write the pytest wrapper** — copy
  `tests/test_task_authoring_js.py` structure verbatim (including the
  hard-fail-without-node rule; adjust names to
  `tests/web/benchmarks_harness.js`).

- [ ] **Step 3: Run** — `uv run pytest tests/test_benchmarks_js.py -v` → PASS
  (iterate on the harness/panel until all eight cases hold).

- [ ] **Step 4: Format, lint, type-check, commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add tests/web/benchmarks_harness.js tests/test_benchmarks_js.py
git commit -m "test(web): benchmarks panel rules under the Node harness (spec §10)"
```

---

### Task 11 (PR-2): Live-smoke runbook + wrap-up

**Files:**
- Create: `docs/atp-benchmark-live-smoke.md`
- Modify: `TODO.md` (`@id:atp-benchmark-view` — flip to `[x]` with both PR numbers)

- [ ] **Step 1: Write `docs/atp-benchmark-live-smoke.md`** — the manual
  procedure (spec §10 accepted residual): boot the eco server from
  `../atp-platform` (`ATP_SERVER_PROFILE=eco uv run --project ../atp-platform
  uvicorn atp.dashboard.v2.factory:app --port 8600`), point a scratch
  `dispatcher.toml` (`[benchmarks] url = "http://127.0.0.1:8600"`) at it,
  `uv run dispatcher serve`, verify: panel visible, list renders, a
  leaderboard opens, killing the eco server flips the panel to
  `unavailable` (never to "0 benchmarks") within ~2 poll cycles.

- [ ] **Step 2: Flip the TODO item** — `- [x]` + both PR numbers on the
  checkbox line; extend the indented context with what shipped and name the
  explicit non-goals left open (runs/tokens phase 2, TUI/VSCode/MCP parity).

- [ ] **Step 3: Full suite, format, lint, type-check, commit, push, PR**

```bash
uv run pytest -q
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add docs/atp-benchmark-live-smoke.md TODO.md
git commit -m "docs: live-smoke runbook; close atp-benchmark-view (PR-1 #NNN, PR-2 #MMM)"
git push -u origin feat/atp-benchmark-view-panel
gh pr create --title "feat: ATP benchmark view — web panel (PR-2)" --body "..."
```

Then: Copilot review → fix valid findings → human merge → post-merge
cleanup (`git switch master && git pull --ff-only`, delete branches).
