"""HTTP surface of the control plane (spec §5.3, §6)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.server.app import create_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    config = DispatcherConfig(roots=(tmp_path / "ws",))
    (tmp_path / "ws").mkdir()
    return TestClient(create_app(config))


def _token(client: TestClient) -> str:
    return client.get("/api/actions/session").json()["token"]


def _body() -> dict:
    return {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "work_id": "todo://deployer/entrypoint-token-boundary-match",
        "repository": "deployer",
        "revision": "a" * 40,
        "tasks": "tasks.yaml",
    }


def test_submit_requires_the_action_token(client: TestClient) -> None:
    assert client.post("/api/runs/submit", json=_body()).status_code == 403


def test_submit_with_the_control_plane_off_is_a_refusal_not_a_crash(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/runs/submit",
        json=_body(),
        headers={"X-Action-Token": _token(client)},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is False
    assert "control plane is off" in payload["reason"]


def test_unknown_request_reads_404(client: TestClient) -> None:
    assert client.get("/api/runs/nope").status_code == 404


def test_verb_outside_the_allowlist_is_422(client: TestClient) -> None:
    response = client.post(
        "/api/runs/11111111-1111-4111-8111-111111111111/verb",
        json={"verb": "workstream-continue"},
        headers={"X-Action-Token": _token(client)},
    )
    assert response.status_code == 422


def test_stop_is_deliberately_not_a_reachable_verb(client: TestClient) -> None:
    """`maestro stop` kills the scheduler process, not one run (spec §6)."""
    response = client.post(
        "/api/runs/11111111-1111-4111-8111-111111111111/verb",
        json={"verb": "stop"},
        headers={"X-Action-Token": _token(client)},
    )
    assert response.status_code == 422
