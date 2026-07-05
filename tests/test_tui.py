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
