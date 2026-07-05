"""Textual TUI: tabbed dashboard over dispatcher.core snapshots."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Select,
    TabbedContent,
    TabPane,
)

from dispatcher.core.contracts import check_contracts
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.models import ContractStatus, ErrorEvent, ProjectSnapshot
from dispatcher.core.service import (
    ERRORS_DAYS_DEFAULT,
    SnapshotService,
    recent_errors,
)

MSG_LIMIT = 160  # same message truncation threshold as the web UI
ERRORS_LIMIT = 50  # same errors-feed cap as the web UI


def truncate(body: str, limit: int = MSG_LIMIT) -> str:
    """Web-parity message truncation: cap at `limit` chars plus ellipsis."""
    return body if len(body) <= limit else body[:limit] + "…"


class DispatcherApp(App[None]):
    """Read-only terminal dashboard over ecosystem project snapshots."""

    TITLE = "Dispatcher"
    CSS = """
    #errors-filters { height: 3; }
    #errors-filters Select { width: 32; }
    """
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("a", "toggle_days", f"{ERRORS_DAYS_DEFAULT}d/all"),
        ("e", "project_errors", "Project errors"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, config: DispatcherConfig) -> None:
        super().__init__()
        self._service = SnapshotService(config)
        self._snapshots: list[ProjectSnapshot] = []
        self._warnings: list[str] = []
        self._contracts: list[ContractStatus] = []
        self._errors_days: int | None = ERRORS_DAYS_DEFAULT
        self._errors_project: str | None = None
        self._errors_service: str | None = None
        self._shown_errors: list[ErrorEvent] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent():
            with TabPane("Projects", id="tab-projects"):
                yield DataTable(id="projects-table", cursor_type="row")
            with TabPane("Errors", id="tab-errors"):
                with Horizontal(id="errors-filters"):
                    yield Select([], prompt="all projects", id="errors-project")
                    yield Select([], prompt="all services", id="errors-service")
                yield DataTable(id="errors-table", cursor_type="row")
            with TabPane("Models", id="tab-models"):
                yield DataTable(id="models-table", cursor_type="row")
            with TabPane("Contracts", id="tab-contracts"):
                yield DataTable(id="contracts-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#projects-table", DataTable).add_columns(
            "project",
            "freshness",
            "tasks",
            "models",
            "tests",
            "errors",
            "warnings",
        )
        self.query_one("#errors-table", DataTable).add_columns(
            "time", "service", "message"
        )
        self.query_one("#models-table", DataTable).add_columns(
            "project", "model", "harness", "role", "vendor", "status"
        )
        self.query_one("#contracts-table", DataTable).add_columns(
            "name", "canon", "vendored", "sync"
        )
        self.set_interval(10.0, self.action_refresh)
        self.action_refresh()

    def action_refresh(self) -> None:
        self._collect()

    @work(thread=True)
    def _collect(self) -> None:
        """Collect snapshots and contracts off the event loop."""
        try:
            snapshots, warnings = self._service.get()
            projects = {
                s.name: Path(s.path) for s in snapshots if s.detected and s.path
            }
            contracts = check_contracts(projects)
        except Exception as err:  # noqa: BLE001 — keep last data on screen
            self.call_from_thread(
                self.notify, f"refresh failed: {err}", severity="error"
            )
            return
        self.call_from_thread(self._apply, snapshots, warnings, contracts)

    def _apply(
        self,
        snapshots: list[ProjectSnapshot],
        warnings: list[str],
        contracts: list[ContractStatus],
    ) -> None:
        self._snapshots = snapshots
        self._warnings = warnings
        self._contracts = contracts
        self.sub_title = f"updated {datetime.now():%H:%M:%S} · {len(warnings)} warnings"
        self._render_projects()
        self._render_errors()
        self._render_models()
        self._render_contracts()

    def _snapshot(self, name: str) -> ProjectSnapshot | None:
        return next((s for s in self._snapshots if s.name == name), None)

    def _render_projects(self) -> None:
        table = self.query_one("#projects-table", DataTable)
        table.clear()
        for s in self._snapshots:
            if not s.detected:
                table.add_row(
                    Text(s.name, style="dim"),
                    "not detected",
                    "—",
                    "—",
                    "—",
                    "—",
                    "",
                    key=s.name,
                )
                continue
            errors_cell: Text | str = (
                Text(str(len(s.errors)), style="bold red")
                if s.errors
                else str(len(s.errors))
            )
            table.add_row(
                s.name,
                s.freshness or "freshness unknown",
                str(len(s.tasks)),
                str(len(s.models)),
                str(len(s.test_results)),
                errors_cell,
                f"⚠ {len(s.warnings)}" if s.warnings else "",
                key=s.name,
            )

    def _merged_errors(self) -> list[ErrorEvent]:
        """Filter + sort exactly like GET /api/errors with the web defaults."""
        snaps = self._snapshots
        if self._errors_project is not None:
            snaps = [s for s in snaps if s.name == self._errors_project]
        merged = [e for s in snaps for e in s.errors]
        if self._errors_service is not None:
            merged = [e for e in merged if e.service == self._errors_service]
        if self._errors_days is not None:
            merged = recent_errors(merged, self._errors_days)
        merged.sort(key=lambda e: e.timestamp or "", reverse=True)
        return merged[:ERRORS_LIMIT]

    def _render_errors(self) -> None:
        table = self.query_one("#errors-table", DataTable)
        self._shown_errors = self._merged_errors()
        table.clear()
        for e in self._shown_errors:
            table.add_row(
                e.timestamp or "—",
                e.service or "—",
                Text(truncate(e.body), style="red"),
            )
        if not self._shown_errors:
            table.add_row("", "", Text("no errors 🎉", style="green"))
        self.query_one(TabbedContent).get_tab(
            "tab-errors"
        ).label = f"Errors ({len(self._shown_errors)})"
        detected = sorted(s.name for s in self._snapshots if s.detected)
        self._update_select("errors-project", detected, self._errors_project)
        services = {e.service for s in self._snapshots for e in s.errors if e.service}
        self._update_select("errors-service", sorted(services), self._errors_service)

    def _update_select(
        self, select_id: str, values: list[str], current: str | None
    ) -> None:
        """Rebuild a filter Select, keeping the current choice selectable."""
        if current is not None and current not in values:
            values = sorted([*values, current])
        select = self.query_one(f"#{select_id}", Select)
        with select.prevent(Select.Changed):
            select.set_options((v, v) for v in values)
            if current is not None:
                select.value = current

    def on_select_changed(self, event: Select.Changed) -> None:
        value = None if event.value is Select.BLANK else str(event.value)
        if event.select.id == "errors-project":
            self._errors_project = value
        elif event.select.id == "errors-service":
            self._errors_service = value
        self._render_errors()

    def _render_models(self) -> None:
        table = self.query_one("#models-table", DataTable)
        table.clear()
        for s in self._snapshots:
            for m in s.models:
                table.add_row(
                    s.name,
                    m.model_id,
                    m.harness or "—",
                    m.role,
                    m.vendor or "—",
                    m.status or "—",
                )

    def _render_contracts(self) -> None:
        table = self.query_one("#contracts-table", DataTable)
        table.clear()
        for c in self._contracts:
            if c.in_sync is None:
                sync: Text | str = c.detail or "n/a"
            elif c.in_sync:
                sync = Text("✓ in sync", style="green")
            else:
                sync = Text("✗ drift", style="bold red")
            table.add_row(c.name, c.canonical_path, c.vendored_path or "—", sync)

    def action_toggle_days(self) -> None:
        self._errors_days = (
            None if self._errors_days is not None else ERRORS_DAYS_DEFAULT
        )
        self._render_errors()

    def action_project_errors(self) -> None:
        pass  # wired to the errors tab in a later task
