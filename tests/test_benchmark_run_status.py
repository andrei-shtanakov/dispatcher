"""Phase-2 run-status: token-file gate, fail-closed classification, and the
canary secrecy pin (spec 2026-08-16 §3, §5, §9)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from dispatcher.core.benchmarks import (
    RunStatusInfo,
    RunStatusReport,
    fetch_run_status,
    read_token_file,
)

BASE = "http://atp.test"
CANARY = "atp_u_canary_9f8e7d6c5b4a_secret"

RUN_BODY = {
    "id": 42,
    "status": "completed",
    "current_task_index": 3,
    "tasks_count": 3,
    "total_score": 87.5,
    "score_semantics": {"kind": "aggregated_evaluation"},
    "score_components": {"contains": 91.7},
    "completed_tasks": [{"task_index": 0, "score": 95.0, "eval_results": None}],
}

FIXTURES = (
    Path(__file__).parent.parent / "contracts" / "atp-benchmark-api" / "v1" / "fixtures"
)


def _token_file(tmp_path: Path, content: str = CANARY, mode: int = 0o600) -> Path:
    path = tmp_path / "atp-token"
    path.write_text(content)
    path.chmod(mode)
    return path


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---- token-file gate (§3) ---------------------------------------------------


def test_token_file_0600_and_0400_pass(tmp_path: Path) -> None:
    for mode in (0o600, 0o400):
        token, failure = read_token_file(_token_file(tmp_path, mode=mode))
        assert failure is None, f"mode 0{mode:o}"
        assert token == CANARY


def test_group_or_other_access_refuses(tmp_path: Path) -> None:
    for mode in (0o640, 0o604, 0o644, 0o660, 0o666):
        token, failure = read_token_file(_token_file(tmp_path, mode=mode))
        assert token is None, f"mode 0{mode:o}"
        assert failure is not None and failure[0] == "token_file_insecure"
        assert f"0{mode:o}" in failure[1]
        assert CANARY not in failure[1]


def test_symlink_refuses_even_to_a_valid_target(tmp_path: Path) -> None:
    target = _token_file(tmp_path)
    link = tmp_path / "link-to-token"
    link.symlink_to(target)
    token, failure = read_token_file(link)
    assert token is None
    assert failure is not None and failure[0] == "token_file_insecure"
    assert "symlink" in failure[1]


def test_missing_and_directory_and_content_failures(tmp_path: Path) -> None:
    token, failure = read_token_file(tmp_path / "absent")
    assert token is None and failure is not None
    assert failure[0] == "token_file_missing"

    token, failure = read_token_file(tmp_path)  # a directory
    assert token is None and failure is not None
    assert failure[0] == "token_file_unreadable"

    empty = _token_file(tmp_path, content="\n")
    token, failure = read_token_file(empty)
    assert token is None and failure is not None
    assert failure[0] == "token_file_unreadable"

    multi = _token_file(tmp_path, content="one\ntwo\n")
    token, failure = read_token_file(multi)
    assert token is None and failure is not None
    assert failure[0] == "token_file_unreadable"


def test_trailing_newline_is_stripped(tmp_path: Path) -> None:
    token, failure = read_token_file(_token_file(tmp_path, content=CANARY + "\n"))
    assert failure is None
    assert token == CANARY


# ---- classification (§5 table) ---------------------------------------------


def _status(
    tmp_path: Path, handler, run_id: int = 42
) -> tuple[RunStatusReport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def spy(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    report = fetch_run_status(
        BASE, run_id, _token_file(tmp_path), transport=_transport(spy)
    )
    return report, seen


def test_ok_sends_the_bearer_header_and_parses(tmp_path: Path) -> None:
    report, seen = _status(tmp_path, lambda r: httpx.Response(200, json=RUN_BODY))
    assert report.status == "ok"
    assert report.run is not None
    assert report.run.status == "completed"
    assert report.run.score_components == {"contains": 91.7}
    assert report.fetched_at is not None
    assert report.error is None
    # The token is actually used — and only in the header.
    assert seen[0].headers["authorization"] == f"Bearer {CANARY}"
    assert CANARY not in str(seen[0].url)
    assert seen[0].url.path == "/api/v1/runs/42/status"


def test_401_and_403_are_unauthorized(tmp_path: Path) -> None:
    for code in (401, 403):
        report, _ = _status(tmp_path, lambda r, c=code: httpx.Response(c, json={}))
        assert report.status == "unauthorized"
        assert report.error is not None and str(code) in report.error


def test_404_keeps_both_sides_of_the_producer_ambiguity(tmp_path: Path) -> None:
    report, _ = _status(tmp_path, lambda r: httpx.Response(404, json={}))
    assert report.status == "not_found"
    assert report.error is not None
    assert "not found, or not owned by this token" in report.error


def test_other_non_2xx_and_transport_errors_are_unavailable(tmp_path: Path) -> None:
    for code in (500, 429):
        report, _ = _status(tmp_path, lambda r, c=code: httpx.Response(c, json={}))
        assert report.status == "unavailable"

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    report, _ = _status(tmp_path, boom)
    assert report.status == "unavailable"


def test_2xx_garbage_is_unreadable(tmp_path: Path) -> None:
    report, _ = _status(tmp_path, lambda r: httpx.Response(200, text="not json"))
    assert report.status == "unreadable"

    bad = dict(RUN_BODY, tasks_count="three")
    report, _ = _status(tmp_path, lambda r: httpx.Response(200, json=bad))
    assert report.status == "unreadable"

    missing = {k: v for k, v in RUN_BODY.items() if k != "status"}
    report, _ = _status(tmp_path, lambda r: httpx.Response(200, json=missing))
    assert report.status == "unreadable"


def test_unknown_producer_status_word_passes_through(tmp_path: Path) -> None:
    body = dict(RUN_BODY, status="weird-new-state")
    report, _ = _status(tmp_path, lambda r: httpx.Response(200, json=body))
    assert report.status == "ok"
    assert report.run is not None and report.run.status == "weird-new-state"


def test_config_and_token_states_make_no_request(tmp_path: Path) -> None:
    report = fetch_run_status(None, 7, None)
    assert (report.status, report.fetched_at) == ("unconfigured", None)
    assert report.error is not None

    report = fetch_run_status(BASE, 7, None)
    assert (report.status, report.fetched_at) == ("token_unconfigured", None)

    report = fetch_run_status(BASE, 7, tmp_path / "absent")
    assert (report.status, report.fetched_at) == ("token_file_missing", None)


# ---- the canary secrecy pin (§9) -------------------------------------------


def test_canary_token_never_appears_in_any_serialized_report(
    tmp_path: Path,
) -> None:
    """The design's teeth: run every state the fetcher can produce and
    assert the token is in none of them."""
    handlers = [
        lambda r: httpx.Response(200, json=RUN_BODY),
        lambda r: httpx.Response(401, json={}),
        lambda r: httpx.Response(403, json={}),
        lambda r: httpx.Response(404, json={}),
        lambda r: httpx.Response(500, json={}),
        lambda r: httpx.Response(429, json={}),
        lambda r: httpx.Response(200, text="not json"),
        lambda r: httpx.Response(200, json={"id": "x"}),
    ]

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused connecting", request=request)

    reports = [
        fetch_run_status(BASE, 42, _token_file(tmp_path), transport=_transport(h))
        for h in [*handlers, boom]
    ]
    # Token-file failure states carry a reason naming the path/mode — never
    # the content: build them over a file that HOLDS the canary.
    insecure = _token_file(tmp_path, mode=0o644)
    reports.append(fetch_run_status(BASE, 42, insecure))
    multi = _token_file(tmp_path, content=f"{CANARY}\nsecond")
    reports.append(fetch_run_status(BASE, 42, multi))

    for report in reports:
        assert CANARY not in report.model_dump_json(), report.status


# ---- vendored fixtures (§8/§9) ---------------------------------------------


def test_vendored_run_status_fixtures_parse(tmp_path: Path) -> None:
    """The pinned contract and the consumer model must agree, forever."""
    completed = RunStatusInfo.model_validate(
        json.loads((FIXTURES / "run-status-completed.json").read_text())
    )
    assert completed.status == "completed"
    assert completed.score_components
    in_progress = RunStatusInfo.model_validate(
        json.loads((FIXTURES / "run-status-in-progress.json").read_text())
    )
    assert in_progress.total_score is None


# ---- read_api + service ----------------------------------------------------


def test_read_api_without_service_is_unconfigured() -> None:
    from dispatcher.core import read_api

    report = read_api.benchmark_run_status(None, 9)
    assert (report.status, report.run_id, report.fetched_at) == (
        "unconfigured",
        9,
        None,
    )


def test_service_passes_url_run_id_and_token_path_through(tmp_path: Path) -> None:
    from dispatcher.core.benchmark_service import BenchmarkService

    calls: list[tuple[str | None, int, Path | None]] = []

    def fake(base_url, run_id, token_file):
        calls.append((base_url, run_id, token_file))
        return fetch_run_status(None, run_id, None)

    service = BenchmarkService(BASE, token_file=tmp_path / "t", run_status_fetcher=fake)
    service.run_status(5)
    assert calls == [(BASE, 5, tmp_path / "t")]


def test_background_cycle_never_touches_the_run_status_fetcher() -> None:
    """The secret must not ride the unattended loop (§2 non-goals)."""
    from dispatcher.core.benchmark_service import BenchmarkService
    from dispatcher.core.benchmarks import initial_report

    def poisoned(base_url, run_id, token_file):
        raise AssertionError("run_status_fetcher called by the periodic path")

    service = BenchmarkService(
        BASE, fetcher=lambda url: initial_report(url), run_status_fetcher=poisoned
    )
    service.get()
    assert service.wait_for_fetch(timeout=5)
