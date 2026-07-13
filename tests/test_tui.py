"""Pilot tests for the textual TUI."""

from pathlib import Path
from typing import Literal

import pytest
from conftest import (
    make_arbiter,
    make_atp,
    make_maestro_home,
    make_spec_runner,
)
from textual.widgets import DataTable, Select, Static, TabbedContent, TabPane

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.tui.app import DispatcherApp, _contract_cell, truncate
from dispatcher.tui.detail import ErrorMessageScreen, ProjectDetailScreen

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


async def test_app_boots_with_five_tabs(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        assert len(app.query(TabPane)) == 5
        for table_id in (
            "projects-table",
            "errors-table",
            "models-table",
            "contracts-table",
            "roadmap-table",
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

        # Spy on notify calls to guard error toast behavior.
        recorded: list[tuple[str, str]] = []
        real_notify = app.notify

        def spy_notify(
            message: str,
            *,
            severity: Literal["error", "information", "warning"] = "information",
            **kwargs,
        ) -> None:
            recorded.append((str(message), severity))
            return real_notify(message, severity=severity, **kwargs)

        monkeypatch.setattr(app, "notify", spy_notify)

        def broken_get():
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(app._service, "get", broken_get)
        await pilot.press("r")
        await _settled(app, pilot)
        # Verify: previous data still on screen
        assert app.query_one("#projects-table", DataTable).row_count == 5
        # Verify: error toast fired with correct severity
        error_messages = [msg for msg, sev in recorded if sev == "error"]
        assert len(error_messages) > 0, "Expected at least one error notification"
        assert any(msg.startswith("refresh failed:") for msg in error_messages), (
            "Expected an error message starting with 'refresh failed:'"
        )


async def test_models_table_matches_web_columns(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        table = app.query_one("#models-table", DataTable)
        assert table.row_count > 0
        rows = [table.get_row_at(i) for i in range(table.row_count)]
        # arbiter agents.toml fixture exposes a routable harness@model
        assert any(str(r[0]) == "arbiter" and str(r[3]) == "routable" for r in rows)
        # missing optional values render as em-dash, like the web
        assert any("—" in map(str, r) for r in rows)


async def test_contracts_table_shows_drift(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        table = app.query_one("#contracts-table", DataTable)
        rows = [table.get_row_at(i) for i in range(table.row_count)]
        catalog = next(r for r in rows if str(r[0]) == "agents-catalog")
        assert "✗ drift" in str(catalog[3])


def test_truncate_web_parity() -> None:
    assert truncate("x" * 160) == "x" * 160
    assert truncate("x" * 161) == "x" * 160 + "…"


def test_contract_cell_states() -> None:
    sync: dict[str, bool | None] = {"a": True, "b": False, "c": None}
    assert _contract_cell(None, sync) == "—"
    assert "a ✓ in sync" in str(_contract_cell("a", sync))
    assert "b ✗ drift" in str(_contract_cell("b", sync))
    assert _contract_cell("c", sync) == "c n/a"  # not comparable
    assert _contract_cell("missing", sync) == "missing n/a"  # unknown name


async def test_errors_tab_lists_and_counts(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        table = app.query_one("#errors-table", DataTable)
        assert table.row_count == len(app._shown_errors) > 0
        # newest first, like the web feed
        stamps = [e.timestamp or "" for e in app._shown_errors]
        assert stamps == sorted(stamps, reverse=True)


async def test_errors_service_filter(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        total = len(app._shown_errors)
        app.query_one("#errors-service", Select).value = "svc"
        await pilot.pause()
        assert 0 < len(app._shown_errors) < total
        assert all(e.service == "svc" for e in app._shown_errors)


async def test_errors_project_filter(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one("#errors-project", Select).value = "arbiter"
        await pilot.pause()
        assert app._shown_errors  # arbiter fixture has an OTel error
        assert not any("lint failed" in e.body for e in app._shown_errors)


async def test_errors_project_filter_clearable(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        total = len(app._shown_errors)
        select = app.query_one("#errors-project", Select)
        select.value = "arbiter"
        await pilot.pause()
        assert app._errors_project == "arbiter"
        assert len(app._shown_errors) < total
        select.clear()
        await pilot.pause()
        # Regression: on_select_changed used to compare against
        # `Select.BLANK` (Widget.BLANK, always False) instead of
        # `Select.NULL`, so clearing never matched and stored the
        # stringified sentinel as a bogus project name.
        assert app._errors_project is None
        assert len(app._shown_errors) == total


async def test_errors_days_toggle(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        assert app._errors_days == 14
        await pilot.press("a")
        assert app._errors_days is None
        await pilot.press("a")
        assert app._errors_days == 14


async def test_errors_empty_state(tmp_path: Path) -> None:
    empty_root = tmp_path / "nothing"
    empty_root.mkdir()
    app = DispatcherApp(
        DispatcherConfig(roots=(empty_root,), maestro_db=tmp_path / "no.db")
    )
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        table = app.query_one("#errors-table", DataTable)
        assert table.row_count == 1
        assert "no errors 🎉" in str(table.get_row_at(0)[2])


async def test_enter_opens_project_detail(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        table = app.query_one("#projects-table", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")  # cursor starts on row 0 = arbiter
        assert isinstance(app.screen, ProjectDetailScreen)
        assert app.screen._snap.name == "arbiter"
        # Verify the rendered header actually contains the collected_at and
        # detected fields (not just that the model attributes exist).
        texts = " ".join(str(w.content) for w in app.screen.query(Static))
        assert "collected:" in texts
        assert "detected:" in texts
        assert "T-9" in texts  # arbiter fixture task rendered in sections
        await pilot.press("escape")
        assert not isinstance(app.screen, ProjectDetailScreen)


async def test_enter_ignored_on_undetected_project(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        table = app.query_one("#projects-table", DataTable)
        table.focus()
        table.move_cursor(row=3)  # first undetected row (Maestro)
        await pilot.pause()
        await pilot.press("enter")
        assert not isinstance(app.screen, ProjectDetailScreen)


async def test_enter_on_error_row_shows_full_message(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-errors"
        await pilot.pause()
        table = app.query_one("#errors-table", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("enter")
        assert isinstance(app.screen, ErrorMessageScreen)
        await pilot.press("escape")
        assert not isinstance(app.screen, ErrorMessageScreen)


async def test_e_key_prefilters_errors_for_project(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        table = app.query_one("#projects-table", DataTable)
        table.focus()
        await pilot.pause()
        await pilot.press("e")  # cursor on row 0 = arbiter
        await pilot.pause()
        assert app.query_one(TabbedContent).active == "tab-errors"
        assert app._errors_project == "arbiter"
        assert app.query_one("#errors-project", Select).value == "arbiter"


_ROADMAP_YAML = """\
version: 1
roadmap: tui-test
title: TUI test roadmap
items:
  - id: TUI-1
    title: Detected item
    phase: "1"
    owner_project: arbiter
    evidence_rules:
      - rule: project_detected
        kind: implementation
        project: arbiter

  - id: TUI-2
    title: No rules yet
    phase: "2"
    evidence_rules: []

  - id: TUI-3
    title: Contract drifted
    phase: "3"
    owner_project: arbiter
    target_contract: agents-catalog
    evidence_rules:
      - rule: project_detected
        kind: implementation
        project: arbiter
"""


def _app_with_roadmap(tmp_path: Path) -> DispatcherApp:
    make_atp(tmp_path)
    make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    db = make_maestro_home(tmp_path)
    vault = tmp_path / "prograph-vault" / "authored" / "roadmaps"
    vault.mkdir(parents=True)
    (vault / "tui-test.yaml").write_text(_ROADMAP_YAML)
    return DispatcherApp(DispatcherConfig(roots=(tmp_path,), maestro_db=db))


async def test_roadmap_tab_columns(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        table = app.query_one("#roadmap-table", DataTable)
        col_labels = [str(c.label) for c in table.columns.values()]
        assert col_labels == [
            "phase",
            "item",
            "owner",
            "status",
            "contract",
            "blockers",
            "evidence",
            "freshness",
        ]


async def test_roadmap_table_populates_from_yaml(tmp_path: Path) -> None:
    app = _app_with_roadmap(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        table = app.query_one("#roadmap-table", DataTable)
        assert table.row_count == 3
        row1 = table.get_row("TUI-1")
        assert str(row1[0]) == "1"  # phase
        assert "TUI-1" in str(row1[1])  # item cell contains id
        assert str(row1[2]) == "arbiter"  # owner
        assert str(row1[3]) == "implemented"  # arbiter detected → impl passed
        assert str(row1[4]) == "—"  # no target_contract
        assert str(row1[5]) == "—"  # no blockers
        assert str(row1[6]) == "1/1 rules"  # 1 rule, 1 passed
        assert str(row1[7]) == "—"  # project_detected carries no mtime
        row2 = table.get_row("TUI-2")
        assert str(row2[3]) == "unknown"  # no rules → unknown
        assert str(row2[6]) == "no rules"
        row3 = table.get_row("TUI-3")
        assert str(row3[3]) == "drift"  # fixture vendored copy differs
        assert "agents-catalog ✗ drift" in str(row3[4])
