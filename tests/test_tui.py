"""Pilot tests for the textual TUI."""

from pathlib import Path

import pytest
from conftest import (
    make_arbiter,
    make_atp,
    make_maestro_home,
    make_spec_runner,
)
from textual.widgets import DataTable, TabPane

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.tui.app import DispatcherApp

pytestmark = pytest.mark.anyio


def _app(tmp_path: Path) -> DispatcherApp:
    make_atp(tmp_path)
    make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    db = make_maestro_home(tmp_path)
    return DispatcherApp(DispatcherConfig(roots=(tmp_path,), maestro_db=db))


async def _settled(app: DispatcherApp, pilot) -> None:
    """Wait for background collection workers, then for the message pump."""
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_app_boots_with_four_tabs(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        assert len(app.query(TabPane)) == 4
        for table_id in (
            "projects-table",
            "errors-table",
            "models-table",
            "contracts-table",
        ):
            assert len(app.query_one(f"#{table_id}", DataTable).columns) > 0


async def test_projects_table_populates(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        table = app.query_one("#projects-table", DataTable)
        assert table.row_count == 5  # 3 detected + 2 undetected collectors
        row = table.get_row("arbiter")
        assert str(row[2]) == "1"  # one decision task in the fixture


async def test_undetected_project_row_dimmed(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        table = app.query_one("#projects-table", DataTable)
        row = table.get_row("Maestro")  # make_maestro() was not called
        assert str(row[1]) == "not detected"
        assert str(row[2]) == "—"


async def test_footer_shows_update_time(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        assert app.sub_title.startswith("updated ")


async def test_r_binding_recollects(tmp_path: Path, monkeypatch) -> None:
    app = _app(tmp_path)
    calls: list[int] = []
    real_get = app._service.get

    def counting_get():
        calls.append(1)
        return real_get()

    monkeypatch.setattr(app._service, "get", counting_get)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        assert len(calls) == 1  # initial collect on mount
        await pilot.press("r")
        await _settled(app, pilot)
        assert len(calls) == 2


async def test_collect_failure_keeps_last_data(tmp_path: Path, monkeypatch) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        assert app.query_one("#projects-table", DataTable).row_count == 5

        def broken_get():
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(app._service, "get", broken_get)
        await pilot.press("r")
        await _settled(app, pilot)
        # layer-3: toast shown, previous data still on screen
        assert app.query_one("#projects-table", DataTable).row_count == 5
