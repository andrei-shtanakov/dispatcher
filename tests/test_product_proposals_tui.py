"""TUI parity for the product-proposals read model (TODO
product-proposal-parity): the detail screen renders gate/loop waits with
the SAME zero-state semantics the spec pinned for the web panel —
a confident global zero appears only on a fully classified scan.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import (
    make_arbiter,
    make_atp,
    make_impresario,
    make_maestro_home,
    make_spec_runner,
    seed_impresario_wait,
)
from textual.widgets import DataTable, Static

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.product_proposals import (
    Diagnostic,
    GateWait,
    LoopWait,
    ProductProposalsReport,
    ProposalBundle,
)
from dispatcher.tui.app import DispatcherApp
from dispatcher.tui.detail import ProjectDetailScreen

pytestmark = pytest.mark.anyio

_SNAP = ProjectSnapshot(name="impresario", path="/w/impresario")

_WAIT = GateWait(
    proposal_id="PP-101",
    gate_id="qg5_business",
    gate_label="Gate A",
    authority="business_owner",
    artifact_ref="proposal://PP-101",
    bundle_path="pilot/pp-101",
    version=6,
    proposal_updated_at="2026-08-12T04:12:30Z",
)

_LOOP = LoopWait(
    loop_id="LOOP-101",
    iteration=2,
    proposal_id="PP-101",
    reason="ждём человека",
    stopped_at="2026-08-12T05:00:00Z",
    bundle_path="pilot/pp-101",
)

_OK_BUNDLE = ProposalBundle(
    path="pilot/pp-101",
    state="ok",
    proposal_id="PP-101",
    status="ready_for_business",
    version=6,
)

_BAD_BUNDLE = ProposalBundle(
    path="pilot/pp-999",
    state="unreadable",
    diagnostics=[Diagnostic(code="proposal-unreadable", message="boom")],
)


def _rendered(
    report: ProductProposalsReport | None,
    error: str | None = None,
) -> str:
    screen = ProjectDetailScreen(
        _SNAP, product_proposals=report, product_proposals_error=error
    )
    return "\n".join(screen._render_texts())


def test_populated_report_renders_waits_and_suppression_note() -> None:
    report = ProductProposalsReport(
        mirror_path="/w/impresario",
        bundles=[_OK_BUNDLE, _BAD_BUNDLE],
        waits=[_WAIT],
        needs_human=[_LOOP],
        attention=True,
    )
    rendered = _rendered(report)
    assert "Gate A" in rendered and "business_owner" in rendered
    assert "proposal://PP-101" in rendered
    assert "LOOP-101" in rendered and "ждём человека" in rendered
    assert "classification suppressed" in rendered
    assert "0 gates waiting" not in rendered
    assert "0 loops waiting" not in rendered


def test_fully_classified_scan_shows_confident_zero() -> None:
    report = ProductProposalsReport(mirror_path="/w/impresario", bundles=[_OK_BUNDLE])
    rendered = _rendered(report)
    assert "0 gates waiting" in rendered
    assert "0 loops waiting" in rendered


def test_non_ok_bundle_forbids_confident_zero() -> None:
    report = ProductProposalsReport(
        mirror_path="/w/impresario", bundles=[_BAD_BUNDLE], attention=True
    )
    rendered = _rendered(report)
    assert "0 gates waiting" not in rendered
    assert "0 loops waiting" not in rendered
    assert "classification suppressed" in rendered


def test_report_level_diagnostic_forbids_confident_zero() -> None:
    report = ProductProposalsReport(
        mirror_path="/w/impresario",
        bundles=[_OK_BUNDLE],
        diagnostics=[Diagnostic(code="scan-degraded", message="one dir lost")],
        attention=True,
    )
    rendered = _rendered(report)
    assert "scan-degraded" in rendered
    assert "0 gates waiting" not in rendered
    assert "0 loops waiting" not in rendered


def test_healthy_empty_scan_is_zero_bundles() -> None:
    report = ProductProposalsReport(mirror_path="/w/impresario")
    rendered = _rendered(report)
    assert "0 bundles" in rendered
    assert "0 gates waiting" not in rendered


def test_collect_error_is_fail_loud() -> None:
    rendered = _rendered(None, error="mirror exploded")
    assert "product proposals" in rendered
    assert "mirror exploded" in rendered


def test_without_report_no_product_proposals_section() -> None:
    rendered = "\n".join(ProjectDetailScreen(_SNAP)._render_texts())
    assert "product proposals" not in rendered


def _app(tmp_path: Path) -> DispatcherApp:
    make_atp(tmp_path)
    make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    seed_impresario_wait(make_impresario(tmp_path))
    db = make_maestro_home(tmp_path)
    return DispatcherApp(DispatcherConfig(roots=(tmp_path,), maestro_db=db))


async def _settled(app: DispatcherApp, pilot) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _open_detail(app: DispatcherApp, pilot, name: str) -> None:
    table = app.query_one("#projects-table", DataTable)
    table.focus()
    await pilot.pause()
    rows = [str(table.get_row_at(i)[0]).strip() for i in range(table.row_count)]
    table.move_cursor(row=rows.index(name))
    await pilot.press("enter")


async def test_impresario_detail_shows_gate_wait(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        await _open_detail(app, pilot, "impresario")
        assert isinstance(app.screen, ProjectDetailScreen)
        texts = " ".join(str(w.content) for w in app.screen.query(Static))
        assert "business_owner" in texts
        assert "proposal://PP-101" in texts


async def test_non_impresario_detail_has_no_section(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        await _open_detail(app, pilot, "arbiter")
        assert isinstance(app.screen, ProjectDetailScreen)
        texts = " ".join(str(w.content) for w in app.screen.query(Static))
        assert "product proposals" not in texts
