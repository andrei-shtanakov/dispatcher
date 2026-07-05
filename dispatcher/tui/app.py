"""Textual TUI: tabbed dashboard over dispatcher.core snapshots."""

from __future__ import annotations

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

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.models import ErrorEvent, ProjectSnapshot
from dispatcher.core.service import ERRORS_DAYS_DEFAULT, SnapshotService

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

    def action_refresh(self) -> None:
        pass  # wired to the collect worker in the next task

    def action_toggle_days(self) -> None:
        pass  # wired to the errors renderer in a later task

    def action_project_errors(self) -> None:
        pass  # wired to the errors tab in a later task
