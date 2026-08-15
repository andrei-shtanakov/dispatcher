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


def test_read_api_benchmarks_without_service_is_unconfigured() -> None:
    from dispatcher.core import read_api

    status = read_api.benchmarks(None)
    assert status.report.status == "unconfigured"
    assert status.fetch_in_flight is False


def test_read_api_benchmarks_passes_through_the_service() -> None:
    from dispatcher.core import read_api
    from dispatcher.core.benchmark_service import BenchmarkService

    service = BenchmarkService(
        "http://atp.test", fetcher=lambda url: initial_report(url)
    )
    status = read_api.benchmarks(service)
    assert status.report.url == "http://atp.test"


def test_vendored_fixtures_parse_as_ok() -> None:
    """The pinned contract and the consumer models must agree, forever (§10)."""
    import json
    from pathlib import Path

    fixtures = (
        Path(__file__).parent.parent
        / "contracts"
        / "atp-benchmark-api"
        / "v1"
        / "fixtures"
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
