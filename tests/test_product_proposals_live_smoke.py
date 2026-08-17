"""Cross-repo live smoke: the REAL impresario tree at the contract pin.

`IMPRESARIO_PINNED_DIR` (exported by scripts/checkout_pinned_impresario.sh)
is a HARD prerequisite — this test FAILS without it, it does not skip (the
PR #98 discipline: a skip is how a suite goes green while covering nothing).
Locally:
    IMPRESARIO_PINNED_DIR="$(scripts/checkout_pinned_impresario.sh --from ../impresario)" \
      uv run pytest tests/test_product_proposals_live_smoke.py -v

Asserted against the SERIALIZED public response — attention, diagnostics,
bundles, waits, loop_status, needs_human, backlog_bundles, backlog_waits —
not the core function's return value. At the pin the tree holds exactly two
bundles outside contracts/ (pp-101 — with an LRD record in its decisions/ —
and pp-104, both approved) with terminal loop statuses, plus the live
RankedBacklog
(BL-ecosystem v4: selectable items, no active QG-4 decision → exactly one
qg4_backlog wait — the invisible wait issue #154 cites): a different result
after a re-vendor is a real contract-behaviour change and must be reviewed,
not re-pinned away. (contracts/examples/pp-001 doubles as live proof of the
contracts/-segment exclusion.)
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio

_MISSING = (
    "IMPRESARIO_PINNED_DIR is a required prerequisite of the product-proposals "
    "live smoke — run scripts/checkout_pinned_impresario.sh (CI does; locally "
    "add --from ../impresario). Without it the end-to-end path is UNVERIFIED, "
    "and that must FAIL, not skip."
)


async def test_http_surface_on_the_pinned_real_mirror() -> None:
    pinned = os.environ.get("IMPRESARIO_PINNED_DIR")
    assert pinned, _MISSING
    mirror = Path(pinned)
    assert (mirror / "pilot" / "forconcept" / "pp-101" / "proposal.yaml").is_file()

    config = DispatcherConfig(roots=(mirror.parent,))
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/projects/impresario/product-proposals")

    assert resp.status_code == 200
    data = resp.json()
    assert data["attention"] is False
    assert data["diagnostics"] == []
    assert [(b["path"], b["state"], b["status"]) for b in data["bundles"]] == [
        ("pilot/forconcept/pp-101", "ok", "approved"),
        ("pilot/forconcept/pp-104", "ok", "approved"),
    ]
    assert [b["loop_status"] for b in data["bundles"]] == [
        "ready_for_business",
        "ready_for_business",
    ]
    assert data["waits"] == []
    assert data["needs_human"] == []
    assert [(b["path"], b["state"]) for b in data["backlog_bundles"]] == [
        ("pilot", "ok")
    ]
    assert [
        (w["backlog_id"], w["version"], w["gate_id"], w["authority"])
        for w in data["backlog_waits"]
    ] == [("BL-ecosystem", 4, "qg4_backlog", "qg4_selector")]
    assert data["backlog_waits"][0]["artifact_path"] == "pilot/backlog.yaml"
