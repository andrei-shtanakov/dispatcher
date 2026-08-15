"""End-to-end over a real socket: FastAPI stub serving the vendored
fixtures → real httpx client → ok report (spec §10). Failure paths:
connection refused → unavailable; wrong shape → unreadable."""

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
    Path(__file__).parent.parent / "contracts" / "atp-benchmark-api" / "v1" / "fixtures"
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


def test_wrong_shape_is_unreadable() -> None:
    app = FastAPI()

    @app.get("/api/v1/benchmarks")
    def broken() -> dict:
        return {"not": "a list"}

    with _serve(app) as base:
        report = fetch_report(base)
    assert report.status == "unreadable"
