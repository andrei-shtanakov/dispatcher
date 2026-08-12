"""GET /api/projects/{name}/product-proposals — the spec's API case split.

The endpoint is a pass-through of the core read model: tests seed real tmp
mirrors and exercise the serialized public response.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from conftest import make_arbiter

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.product_proposals import ANCHOR_FILES
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio


def make_impresario(root: Path) -> Path:
    mirror = root / "impresario"
    for rel in ANCHOR_FILES:
        p = mirror / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    return mirror


def _client(tmp_path: Path) -> httpx.AsyncClient:
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _seed_wait(mirror: Path) -> None:
    bundle = mirror / "pilot" / "pp-101"
    (bundle / "decisions").mkdir(parents=True)
    (bundle / "proposal.yaml").write_text(
        "proposal_id: PP-101\n"
        "idea_ref: idea://IDEA-101\n"
        "version: 6\n"
        "status: ready_for_business\n"
        "iteration: 2\n"
        "refs:\n"
        "  exchange_log: exchange-log://XL-101\n"
        "created_at: '2026-08-12T02:08:53Z'\n"
        "updated_at: '2026-08-12T04:12:30Z'\n"
    )


async def test_unknown_project_is_404_project_not_found(tmp_path: Path) -> None:
    make_impresario(tmp_path)
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/nonesuch/product-proposals")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "project-not-found"


async def test_known_non_impresario_project_is_404_not_impresario_mirror(
    tmp_path: Path,
) -> None:
    make_arbiter(tmp_path)
    make_impresario(tmp_path)
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/arbiter/product-proposals")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not-impresario-mirror"


async def test_undetected_mirror_is_200_mirror_not_detected(
    tmp_path: Path,
) -> None:
    """No impresario under the roots: the negative snapshot row answers with
    a report-level diagnostic — safe under direct request, no Path('')."""
    make_arbiter(tmp_path)  # some OTHER project, so discovery runs fine
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/impresario/product-proposals")
    assert resp.status_code == 200
    data = resp.json()
    assert data["bundles"] == []
    assert [d["code"] for d in data["diagnostics"]] == ["mirror-not-detected"]
    assert data["attention"] is True
    assert data["needs_human"] == []


async def test_healthy_empty_mirror_is_200_zero_bundles(tmp_path: Path) -> None:
    make_impresario(tmp_path)
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/impresario/product-proposals")
    assert resp.status_code == 200
    data = resp.json()
    assert data["bundles"] == [] and data["waits"] == []
    assert data["diagnostics"] == [] and data["attention"] is False
    assert data["needs_human"] == []


async def test_anchors_lost_after_discovery_is_200_anchors_missing(
    tmp_path: Path,
) -> None:
    mirror = make_impresario(tmp_path)
    async with _client(tmp_path) as client:
        # First call populates the snapshot cache with the detected mirror…
        first = await client.get("/api/projects/impresario/product-proposals")
        assert first.status_code == 200
        # …then the mirror degrades within the cache TTL.
        (mirror / "docs" / "semantics.md").unlink()
        resp = await client.get("/api/projects/impresario/product-proposals")
    assert resp.status_code == 200
    data = resp.json()
    assert [d["code"] for d in data["diagnostics"]] == ["mirror-anchors-missing"]
    assert data["attention"] is True
    assert data["needs_human"] == []


async def test_partial_result_is_200_with_attention(tmp_path: Path) -> None:
    mirror = make_impresario(tmp_path)
    _seed_wait(mirror)
    broken = mirror / "pilot" / "pp-999"
    broken.mkdir(parents=True)
    (broken / "proposal.yaml").write_bytes(b"\xff\xfe")
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/impresario/product-proposals")
    assert resp.status_code == 200
    data = resp.json()
    assert [b["state"] for b in data["bundles"]] == ["ok", "unreadable"]
    assert [b["loop_status"] for b in data["bundles"]] == ["absent", "unknown"]
    assert [
        (w["proposal_id"], w["gate_id"], w["authority"]) for w in data["waits"]
    ] == [("PP-101", "qg5_business", "business_owner")]
    assert data["waits"][0]["proposal_updated_at"] == "2026-08-12T04:12:30Z"
    assert data["attention"] is True
    assert data["needs_human"] == []
    assert [b["loop_waits"] for b in data["bundles"]] == [[], []]


async def test_needs_human_serializes_through_the_route(tmp_path: Path) -> None:
    mirror = make_impresario(tmp_path)
    _seed_wait(mirror)
    (mirror / "pilot" / "pp-101" / "loop.state").write_text(
        json.dumps(
            {
                "loop_id": "LOOP-101",
                "idea_ref": "idea://IDEA-101",
                "idea_input_hash": "sha256:" + "f" * 64,
                "proposal_id": "PP-101",
                "exchange_log_id": "XL-101",
                "max_iterations": 3,
                "stop": {
                    "verdict": "needs_human",
                    "reason": "ждём человека",
                    "iteration": 1,
                    "at": "2026-08-12T05:00:00Z",
                },
            }
        )
    )
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/impresario/product-proposals")
    assert resp.status_code == 200
    data = resp.json()
    assert [b["loop_status"] for b in data["bundles"]] == ["needs_human"]
    assert [
        (w["loop_id"], w["iteration"], w["reason"], w["stopped_at"])
        for w in data["needs_human"]
    ] == [("LOOP-101", 1, "ждём человека", "2026-08-12T05:00:00Z")]
    # both wait kinds coexist on one bundle: Gate A + loop
    assert [w["gate_id"] for w in data["waits"]] == ["qg5_business"]
    assert data["bundles"][0]["loop_waits"] == data["needs_human"]
