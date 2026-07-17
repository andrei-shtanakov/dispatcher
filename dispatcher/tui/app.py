"""Textual TUI: tabbed dashboard over dispatcher.core snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

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

from dispatcher.core.actions import (
    Action,
    ActionBusyError,
    ActionRejectedError,
    ActionRunner,
)
from dispatcher.core.contracts import check_contracts
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.models import ContractStatus, ErrorEvent, ProjectSnapshot
from dispatcher.core.roadmap import (
    RoadmapResponse,
    SummaryResponse,
    build_roadmap,
    build_summary,
    contract_sync_by_name,
    default_roadmap_dirs,
)
from dispatcher.core.service import (
    ERRORS_DAYS_DEFAULT,
    SnapshotService,
    recent_errors,
)
from dispatcher.core.spec_runner_config_actions import SpecRunnerConfigActionRunner
from dispatcher.core.sync_service import SyncService, SyncStatus
from dispatcher.tui.detail import ErrorMessageScreen, ProjectDetailScreen

MSG_LIMIT = 160  # same message truncation threshold as the web UI
ERRORS_LIMIT = 50  # same errors-feed cap as the web UI


def truncate(body: str, limit: int = MSG_LIMIT) -> str:
    """Web-parity message truncation: cap at `limit` chars plus ellipsis."""
    return body if len(body) <= limit else body[:limit] + "…"


def _age_cell(seconds: float | None, stale: bool) -> Text | str:
    """Snapshot age; stale (> 1 h) renders amber — staleness is data."""
    if seconds is None:
        return "—"
    label = (
        f"{seconds:.0f}s"
        if seconds < 90
        else f"{seconds / 60:.0f}m"
        if seconds < 5400
        else f"{seconds / 3600:.1f}h"
    )
    return Text(f"{label} stale", style="bold yellow") if stale else label


def _verdict_cell(verdict: str) -> Text | str:
    if verdict == "ok":
        return Text("ok", style="green")
    if verdict == "pull-first":
        return Text("pull-first", style="bold yellow")
    return Text(verdict, style="dim")


def _contract_cell(
    name: str | None, sync_by_name: dict[str, bool | None]
) -> Text | str:
    """Contract Drift column: target contract joined with its sync state."""
    if name is None:
        return "—"
    state = sync_by_name.get(name)
    if state is True:
        return Text(f"{name} ✓ in sync", style="green")
    if state is False:
        return Text(f"{name} ✗ drift", style="bold red")
    return f"{name} n/a"


@dataclass(frozen=True)
class SyncRow:
    """Cursor-addressable meaning of one sync-table row (no cell-scraping)."""

    kind: Literal["verdict", "proposal", "error", "empty"]
    host: str = ""
    repo: str = ""
    live: bool = False
    verdict: str = ""
    ahead: int | None = None


def _can_pull(row: SyncRow) -> bool:
    """Web parity: the pull button exists ⇔ live host && pull-first."""
    return row.kind == "verdict" and row.live and row.verdict == "pull-first"


def _can_open_pr(row: SyncRow) -> bool:
    """Web parity: open PR additionally needs truthy ahead (`v.ahead ?`)."""
    return _can_pull(row) and bool(row.ahead)


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
        ("p", "sync_pull", "Pull"),
        ("o", "sync_open_pr", "Open PR"),
        ("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        config: DispatcherConfig,
        *,
        action_runner: ActionRunner | None = None,
        config_runner: SpecRunnerConfigActionRunner | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._service = SnapshotService(config)
        self._sync_service = SyncService(config)
        self._action_runner = action_runner or ActionRunner(config)
        self._config_runner = config_runner or SpecRunnerConfigActionRunner(config)
        self._roadmap_dirs = config.roadmap_dirs or default_roadmap_dirs(config.roots)
        self._snapshots: list[ProjectSnapshot] = []
        self._warnings: list[str] = []
        self._contracts: list[ContractStatus] = []
        self._roadmap: RoadmapResponse | None = None
        self._sync: SyncStatus | None = None
        self._sync_rows: list[SyncRow] = []
        self._summary: SummaryResponse | None = None
        self._errors_days: int | None = ERRORS_DAYS_DEFAULT
        self._errors_project: str | None = None
        self._errors_service: str | None = None
        self._shown_errors: list[ErrorEvent] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent():
            with TabPane("Sync", id="tab-sync"):
                yield DataTable(id="sync-table", cursor_type="row")
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
            with TabPane("Roadmap", id="tab-roadmap"):
                yield DataTable(id="roadmap-summary-table", cursor_type="row")
                yield DataTable(id="roadmap-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#sync-table", DataTable).add_columns(
            "host", "age", "repo", "verdict", "reason", "branch", "↑/↓"
        )
        self.query_one("#roadmap-summary-table", DataTable).add_columns(
            "project", "done", "readiness", "lagging", "contract drift"
        )
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
        self.query_one("#roadmap-table", DataTable).add_columns(
            "phase",
            "item",
            "owner",
            "status",
            "contract",
            "blockers",
            "evidence",
            "freshness",
        )
        self.set_interval(10.0, self.action_refresh)
        self.action_refresh()

    def action_refresh(self) -> None:
        self._collect()

    @work(thread=True, exclusive=True)
    def _collect(self) -> None:
        """Collect snapshots and contracts off the event loop."""
        try:
            snapshots, warnings = self._service.get()
            projects = {
                s.name: Path(s.path) for s in snapshots if s.detected and s.path
            }
            contracts = check_contracts(projects)
            # Same checker run feeds the roadmap's drift projection, so
            # the Status and Contract columns agree within one refresh.
            roadmap = build_roadmap(self._roadmap_dirs, snapshots, contracts)
            summary = build_summary(roadmap, contracts)
            sync = self._sync_service.get()
        except Exception as err:  # noqa: BLE001 — keep last data on screen
            self.call_from_thread(
                self.notify, f"refresh failed: {err}", severity="error"
            )
            return
        self.call_from_thread(
            self._apply, snapshots, warnings, contracts, roadmap, summary, sync
        )

    def _apply(
        self,
        snapshots: list[ProjectSnapshot],
        warnings: list[str],
        contracts: list[ContractStatus],
        roadmap: RoadmapResponse,
        summary: SummaryResponse,
        sync: SyncStatus,
    ) -> None:
        self._snapshots = snapshots
        self._warnings = warnings
        self._contracts = contracts
        self._roadmap = roadmap
        self._summary = summary
        self._sync = sync
        # «шестерёнка в углу»: индикатор фонового fetch живёт в sub-title
        fetching = " · ⚙ fetching" if sync.fetch_in_flight else ""
        self.sub_title = (
            f"updated {datetime.now():%H:%M:%S} · {len(warnings)} warnings{fetching}"
        )
        self._render_sync()
        self._render_projects()
        self._render_errors()
        self._render_models()
        self._render_contracts()
        self._render_roadmap()

    def _render_sync(self) -> None:
        table = self.query_one("#sync-table", DataTable)
        table.clear()
        self._sync_rows = []
        if self._sync is None:
            return
        report = self._sync.report
        self.query_one(TabbedContent).get_tab(
            "tab-sync"
        ).label = f"Sync · {report.top_line}"
        for panel in report.hosts:
            first = True
            host_cell = f"{panel.host} ({panel.source})"
            if panel.error is not None:
                table.add_row(
                    host_cell,
                    "—",
                    "—",
                    Text("error", style="bold red"),
                    Text(panel.error, style="red"),
                    "—",
                    "—",
                )
                self._sync_rows.append(SyncRow(kind="error", host=panel.host))
                continue
            if not panel.verdicts:
                # хост без tracked-репо всё равно виден — пустая панель
                # не должна молча исчезать с экрана
                table.add_row(
                    host_cell,
                    _age_cell(panel.age_seconds, panel.stale),
                    Text("no tracked repos", style="dim"),
                    _verdict_cell("unknown"),
                    "",
                    "—",
                    "—",
                )
                self._sync_rows.append(SyncRow(kind="empty", host=panel.host))
                continue
            for v in panel.verdicts:
                repo_cell: Text | str = (
                    Text(f"📌 {v.repo}", style="bold") if v.is_kb else v.repo
                )
                table.add_row(
                    host_cell if first else "",
                    _age_cell(panel.age_seconds, panel.stale) if first else "",
                    repo_cell,
                    _verdict_cell(v.verdict),
                    v.reason or "",
                    v.branch or "—",
                    f"{v.ahead if v.ahead is not None else '—'}/"
                    f"{v.behind if v.behind is not None else '—'}"
                    + (" ✎" if v.dirty else ""),
                )
                self._sync_rows.append(
                    SyncRow(
                        kind="verdict",
                        host=panel.host,
                        repo=v.repo,
                        live=panel.source == "live",
                        verdict=v.verdict,
                        ahead=v.ahead,
                    )
                )
                first = False

    def _render_summary(self) -> None:
        table = self.query_one("#roadmap-summary-table", DataTable)
        table.clear()
        if self._summary is None:
            return
        for p in self._summary.projects:
            table.add_row(
                p.project,
                f"{p.done}/{p.total}",
                f"{p.readiness * 100:.0f}%",
                Text("⚠ lagging", style="bold yellow") if p.lagging else "—",
                Text("✗ drift", style="bold red") if p.contract_drift else "—",
            )

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
        value = None if event.value is Select.NULL else str(event.value)
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

    def _render_roadmap(self) -> None:
        self._render_summary()
        table = self.query_one("#roadmap-table", DataTable)
        table.clear()
        if self._roadmap is None:
            return
        sync_by_name = contract_sync_by_name(self._contracts)
        for item in self._roadmap.items:
            passed = sum(1 for e in item.evidence if e.passed)
            total = len(item.evidence)
            evidence_cell = f"{passed}/{total} rules" if total else "no rules"
            blockers_cell = ", ".join(item.blockers) if item.blockers else "—"
            status_cell: Text | str = (
                Text("drift", style="bold red")
                if item.computed_status == "drift"
                else item.computed_status
            )
            table.add_row(
                item.phase or "—",
                f"{item.id} {item.title}",
                item.owner_project or "—",
                status_cell,
                _contract_cell(item.target_contract, sync_by_name),
                blockers_cell,
                evidence_cell,
                item.last_seen or "—",
                key=item.id,
            )

    def action_toggle_days(self) -> None:
        self._errors_days = (
            None if self._errors_days is not None else ERRORS_DAYS_DEFAULT
        )
        self._render_errors()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "projects-table":
            name = str(event.row_key.value)
            snap = self._snapshot(name)
            if snap is not None and snap.detected:
                self.push_screen(ProjectDetailScreen(snap))
        elif event.data_table.id == "errors-table":
            idx = event.cursor_row
            if 0 <= idx < len(self._shown_errors):
                self.push_screen(ErrorMessageScreen(self._shown_errors[idx].body))

    def action_project_errors(self) -> None:
        tabs = self.query_one(TabbedContent)
        if tabs.active != "tab-projects":
            return
        table = self.query_one("#projects-table", DataTable)
        if table.row_count == 0:
            return
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        name = str(row_key.value)
        snap = self._snapshot(name)
        if snap is None or not snap.detected:
            return
        self._errors_project = name
        tabs.active = "tab-errors"
        select = self.query_one("#errors-project", Select)
        with select.prevent(Select.Changed):
            select.value = name
        self._render_errors()

    def _sync_row_at_cursor(self) -> SyncRow | None:
        """Row meaning at the CURRENT cursor — snapshotted at keypress
        (the 10s auto-refresh may redraw between aiming and pressing)."""
        if self.query_one(TabbedContent).active != "tab-sync":
            return None
        table = self.query_one("#sync-table", DataTable)
        idx = table.cursor_coordinate.row
        if not (0 <= idx < len(self._sync_rows)):
            return None
        return self._sync_rows[idx]

    def action_sync_pull(self) -> None:
        row = self._sync_row_at_cursor()
        if row is None:
            return
        if not _can_pull(row):
            self.notify("pull: needs a live pull-first row", severity="warning")
            return
        self._run_sync_action("pull", row.repo)

    def action_sync_open_pr(self) -> None:
        row = self._sync_row_at_cursor()
        if row is None:
            return
        if not _can_open_pr(row):
            self.notify(
                "open PR: needs a live pull-first row with ahead > 0",
                severity="warning",
            )
            return
        self._run_sync_action("open-pr", row.repo)

    @work(thread=True, group="actions")
    def _run_sync_action(self, action: Action, repo: str) -> None:
        """Whitelist action off the event loop; separate group from _collect
        (exclusive=True there would cancel us or vice versa)."""
        try:
            outcome = self._action_runner.run(action, repo)
        except (ActionRejectedError, ActionBusyError) as err:
            self.call_from_thread(self.notify, str(err), severity="warning")
            return
        if outcome.ok:
            self.call_from_thread(
                self.notify, f"✓ {outcome.pr_url or outcome.detail or action}"
            )
        else:
            self.call_from_thread(
                self.notify, f"✗ {outcome.error or action}", severity="error"
            )
        self._sync_service.invalidate()
        self.call_from_thread(self.action_refresh)
