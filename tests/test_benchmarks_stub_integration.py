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
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from dispatcher.core.benchmarks import fetch_report, fetch_run_status

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


STUB_TOKEN = "atp_u_stub_integration_token"
OWNED_RUN_ID = 42


def _fixture_app() -> FastAPI:
    benchmarks = json.loads((FIXTURES / "benchmarks.json").read_text())
    rows = json.loads((FIXTURES / "leaderboard.json").read_text())
    run_status = json.loads((FIXTURES / "run-status-completed.json").read_text())
    app = FastAPI()

    @app.get("/api/v1/benchmarks")
    def list_benchmarks() -> list[dict]:
        return benchmarks

    @app.get("/api/v1/benchmarks/{benchmark_id}/leaderboard")
    def leaderboard(benchmark_id: int) -> list[dict]:
        return rows

    @app.get("/api/v1/runs/{run_id}/status")
    def status(run_id: int, request: Request) -> JSONResponse:
        # Producer semantics (phase-2 spec §1): Bearer required; a missing
        # run and a foreign run are BOTH 404 (anti-enumeration).
        if request.headers.get("authorization") != f"Bearer {STUB_TOKEN}":
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        if run_id != OWNED_RUN_ID:
            return JSONResponse({"detail": "not found"}, status_code=404)
        return JSONResponse(run_status)

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


def _token_file(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "atp-token"
    path.write_text(content)
    path.chmod(0o600)
    return path


def test_run_status_end_to_end_with_the_right_token(tmp_path: Path) -> None:
    """Real socket, real Bearer header: the stub asserts the header is
    actually sent (phase-2 spec §9)."""
    token = _token_file(tmp_path, STUB_TOKEN)
    with _serve(_fixture_app()) as base:
        report = fetch_run_status(base, OWNED_RUN_ID, token)
    assert report.status == "ok"
    assert report.run is not None and report.run.status == "completed"


def test_run_status_wrong_token_is_unauthorized(tmp_path: Path) -> None:
    token = _token_file(tmp_path, "atp_u_someone_else")
    with _serve(_fixture_app()) as base:
        report = fetch_run_status(base, OWNED_RUN_ID, token)
    assert report.status == "unauthorized"


def test_run_status_foreign_run_is_not_found(tmp_path: Path) -> None:
    token = _token_file(tmp_path, STUB_TOKEN)
    with _serve(_fixture_app()) as base:
        report = fetch_run_status(base, OWNED_RUN_ID + 1, token)
    assert report.status == "not_found"
    assert report.error is not None
    assert "not owned by this token" in report.error
