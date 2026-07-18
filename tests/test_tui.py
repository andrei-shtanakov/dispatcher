"""Pilot tests for the textual TUI."""

from datetime import UTC, datetime
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
from dispatcher.core.models import ProjectSnapshot, TaskInfo
from dispatcher.core.spec_runner_config_actions import ConfigCandidate
from dispatcher.core.sync import HostPanel, RepoVerdict, SyncReport
from dispatcher.core.sync_service import SyncStatus
from dispatcher.tui.app import (
    DispatcherApp,
    SyncRow,
    _can_open_pr,
    _can_pull,
    _contract_cell,
    truncate,
)
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


async def test_app_boots_with_seven_tabs(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        assert len(app.query(TabPane)) == 7
        for table_id in (
            "sync-table",
            "roadmap-summary-table",
            "projects-table",
            "errors-table",
            "models-table",
            "contracts-table",
            "roadmap-table",
            "config-table",
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


async def test_detail_renders_onboarding_sections(tmp_path: Path) -> None:
    # fixture: snapshot + synthetic OnboardingView (the builder is already
    # pinned by its own unit tests — here only the section rendering is
    # pinned).
    from dispatcher.core.onboarding import (
        OnboardingNextItem,
        OnboardingProject,
        OnboardingRoadmapPosition,
        OnboardingView,
    )
    from dispatcher.core.roadmap import ProjectSummary
    from dispatcher.tui.detail import ProjectDetailScreen

    snap = ProjectSnapshot(name="arbiter", path="/w/arbiter")
    view = OnboardingView(
        project=OnboardingProject(
            name="arbiter",
            path="/w/arbiter",
            description="Arbiter routes agents.",
            description_source="readme",
        ),
        roadmap_position=OnboardingRoadmapPosition(
            summary=ProjectSummary(
                project="arbiter",
                total=2,
                done=1,
                readiness=0.5,
                lagging=True,
                contract_drift=False,
            ),
            median_readiness=0.75,
            phases=[],
        ),
        next_items=[
            OnboardingNextItem(
                id="RD-1",
                title="Do the thing",
                phase="1",
                computed_status="planned",
                actionable=False,
                blocked_by=["RD-0"],
            )
        ],
        live_tasks=[TaskInfo(task_id="T-7", status="pending", source="db")],
        warnings=["unknown dependency id: RD-0 (item RD-1)"],
    )
    screen = ProjectDetailScreen(snap, view)
    rendered = "\n".join(screen._render_texts())
    assert "Arbiter routes agents." in rendered
    assert "RD-1" in rendered and "blocked by: RD-0" in rendered
    assert "T-7" in rendered
    assert "collected:" in rendered  # old snapshot sections still live
    assert "readiness 50%" in rendered
    assert "median 75%" in rendered
    assert "LAGGING" in rendered
    assert "unknown dependency id: RD-0 (item RD-1)" in rendered


async def test_detail_without_onboarding_degrades(tmp_path: Path) -> None:
    from dispatcher.tui.detail import ProjectDetailScreen

    snap = ProjectSnapshot(name="arbiter", path="/w/arbiter")
    rendered = "\n".join(ProjectDetailScreen(snap)._render_texts())
    assert "collected:" in rendered
    assert "next items" not in rendered


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

  - id: TUI-4
    title: File-backed freshness
    phase: "4"
    owner_project: arbiter
    evidence_rules:
      - rule: file_exists
        kind: implementation
        project: arbiter
        path: config/agents.toml
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
        assert table.row_count == 4
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
        row4 = table.get_row("TUI-4")
        # file_exists matched → freshness cell renders the artifact mtime,
        # not the "—" fallback (success path of app.py freshness column)
        agents_toml = tmp_path / "arbiter" / "config" / "agents.toml"
        expected = datetime.fromtimestamp(
            agents_toml.stat().st_mtime, tz=UTC
        ).isoformat()
        assert str(row4[7]) == expected


def _sync_status() -> SyncStatus:
    report = SyncReport(
        current_host="mac-a",
        top_line="pull-first",
        top_reason="alpha: behind 2",
        hosts=[
            HostPanel(
                host="mac-a",
                source="live",
                age_seconds=3.0,
                stale=False,
                verdicts=[
                    RepoVerdict(repo="prograph-vault", verdict="ok", is_kb=True),
                    RepoVerdict(
                        repo="alpha",
                        verdict="pull-first",
                        reason="behind 2",
                        branch="master",
                        ahead=0,
                        behind=2,
                    ),
                ],
            ),
            HostPanel(
                host="mac-b",
                source="kb",
                age_seconds=7200.0,
                stale=True,
                verdicts=[
                    RepoVerdict(
                        repo="alpha",
                        verdict="unknown",
                        reason="stale snapshot (older than 1 h)",
                    )
                ],
            ),
        ],
    )
    return SyncStatus(
        report=report,
        report_generated_at=datetime(2026, 7, 14, 12, 0, tzinfo=UTC),
        fetch_in_flight=True,
    )


async def test_sync_tab_renders_verdicts_and_topline(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app(tmp_path)
    monkeypatch.setattr(app._sync_service, "get", lambda **kw: _sync_status())
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        table = app.query_one("#sync-table", DataTable)
        assert table.row_count == 3
        first = [str(c) for c in table.get_row_at(0)]
        assert first[0].startswith("mac-a")
        assert "📌 prograph-vault" in first[2]  # KB закреплён первой строкой
        stale_row = [str(c) for c in table.get_row_at(2)]
        assert "stale" in stale_row[1]
        label = str(app.query_one(TabbedContent).get_tab("tab-sync").label)
        assert "pull-first" in label
        assert "⚙ fetching" in app.sub_title  # индикатор фонового fetch


async def test_roadmap_summary_table_populates(tmp_path: Path) -> None:
    roadmaps = tmp_path / "prograph-vault" / "authored" / "roadmaps"
    roadmaps.mkdir(parents=True)
    (roadmaps / "eco.yaml").write_text(
        """
version: 1
roadmap: eco
items:
  - id: S-1
    title: Detected
    owner_project: arbiter
    evidence_rules:
      - rule: project_detected
        kind: implementation
        project: arbiter
"""
    )
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        table = app.query_one("#roadmap-summary-table", DataTable)
        assert table.row_count == 1
        row = [str(c) for c in table.get_row_at(0)]
        assert row[0] == "arbiter"
        assert row[1] == "1/1"
        assert row[2] == "100%"


async def test_sync_tab_empty_and_error_panels_stay_visible(
    tmp_path: Path, monkeypatch
) -> None:
    status = _sync_status()
    status.report.hosts.append(
        HostPanel(host="mac-empty", source="kb", age_seconds=60.0, verdicts=[])
    )
    status.report.hosts.append(
        HostPanel(host="mac-broken", source="kb", error="unsupported schema_version=2")
    )
    app = _app(tmp_path)
    monkeypatch.setattr(app._sync_service, "get", lambda **kw: status)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        table = app.query_one("#sync-table", DataTable)
        rows = [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]
        empty_row = next(r for r in rows if r[0].startswith("mac-empty"))
        assert "(kb)" in empty_row[0]  # source виден и у пустой панели
        assert "no tracked repos" in empty_row[2]
        broken_row = next(r for r in rows if r[0].startswith("mac-broken"))
        assert "(kb)" in broken_row[0]  # ...и у панели-ошибки
        assert "schema_version" in broken_row[4]


class _FakeActionRunner:
    """Records calls; returns a preset ActionOutcome."""

    def __init__(self, outcome=None) -> None:
        from dispatcher.core.actions import ActionOutcome

        self.calls: list[tuple[str, str]] = []
        self.outcome = outcome or ActionOutcome(
            action="pull", dir="alpha", ok=True, detail="fast-forwarded"
        )

    def run(self, action, repo_dir):
        self.calls.append((action, repo_dir))
        return self.outcome


def _sync_with_rows() -> SyncStatus:
    report = SyncReport(
        current_host="h1",
        top_line="pull-first",
        hosts=[
            HostPanel(
                host="h1",
                source="live",
                verdicts=[
                    RepoVerdict(repo="alpha", verdict="pull-first", ahead=2),
                    RepoVerdict(repo="beta", verdict="ok"),
                    RepoVerdict(repo="gamma", verdict="pull-first", ahead=None),
                ],
            ),
            HostPanel(
                host="h2",
                source="kb",
                verdicts=[RepoVerdict(repo="alpha", verdict="pull-first", ahead=1)],
            ),
        ],
    )
    return SyncStatus(
        report=report,
        report_generated_at=datetime.now(tz=UTC),
        fetch_in_flight=False,
    )


def _app_with_runner(tmp_path: Path, runner) -> DispatcherApp:
    make_atp(tmp_path)
    make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    db = make_maestro_home(tmp_path)
    return DispatcherApp(
        DispatcherConfig(roots=(tmp_path,), maestro_db=db),
        action_runner=runner,
    )


async def _sync_app(tmp_path, monkeypatch, runner) -> DispatcherApp:
    app = _app_with_runner(tmp_path, runner)
    # Matches this file's existing instance-level SyncService.get monkeypatch
    # convention (see test_sync_tab_renders_verdicts_and_topline) rather than
    # patching the class attribute directly.
    monkeypatch.setattr(app._sync_service, "get", lambda **kw: _sync_with_rows())
    return app


def _move_sync_cursor(app: DispatcherApp, repo: str, live: bool) -> None:
    """Position the sync-table cursor on the row for (repo, live)."""
    rows = app._sync_rows
    idx = next(
        i
        for i, r in enumerate(rows)
        if r.repo == repo and r.live is live and r.kind == "verdict"
    )
    table = app.query_one("#sync-table", DataTable)
    table.move_cursor(row=idx)


async def test_pull_key_runs_action_on_live_pull_first(
    tmp_path: Path, monkeypatch
) -> None:
    runner = _FakeActionRunner()
    app = await _sync_app(tmp_path, monkeypatch, runner)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-sync"
        await pilot.pause()
        _move_sync_cursor(app, "alpha", live=True)
        await pilot.press("p")
        await _settled(app, pilot)
        assert runner.calls == [("pull", "alpha")]


async def test_pull_key_refuses_ok_and_non_live_rows(
    tmp_path: Path, monkeypatch
) -> None:
    runner = _FakeActionRunner()
    app = await _sync_app(tmp_path, monkeypatch, runner)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-sync"
        await pilot.pause()
        _move_sync_cursor(app, "beta", live=True)  # verdict ok
        await pilot.press("p")
        _move_sync_cursor(app, "alpha", live=False)  # kb host row
        await pilot.press("p")
        await _settled(app, pilot)
        assert runner.calls == []


async def test_open_pr_key_requires_ahead(tmp_path: Path, monkeypatch) -> None:
    runner = _FakeActionRunner()
    app = await _sync_app(tmp_path, monkeypatch, runner)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-sync"
        await pilot.pause()
        _move_sync_cursor(app, "gamma", live=True)  # pull-first, ahead=None
        await pilot.press("o")
        await _settled(app, pilot)
        assert runner.calls == []
        _move_sync_cursor(app, "alpha", live=True)  # ahead=2
        await pilot.press("o")
        await _settled(app, pilot)
        assert runner.calls == [("open-pr", "alpha")]


async def test_action_keys_ignore_other_tabs_and_empty_table(
    tmp_path: Path, monkeypatch
) -> None:
    runner = _FakeActionRunner()
    app = await _sync_app(tmp_path, monkeypatch, runner)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        # Sync is the default active tab on boot; force a non-sync tab.
        app.query_one(TabbedContent).active = "tab-projects"
        await pilot.press("p")
        await _settled(app, pilot)
        assert runner.calls == []


def test_sync_action_visibility_matches_web() -> None:
    """Web: pull ⇔ live && pull-first; open PR ⇔ additionally truthy ahead
    (dispatcher/server/static/index.html, the `actions` helper)."""
    live_pf = SyncRow(kind="verdict", repo="a", live=True, verdict="pull-first")
    assert _can_pull(live_pf)
    assert not _can_open_pr(live_pf)  # ahead None → falsy, web hides the button
    assert _can_open_pr(
        SyncRow(kind="verdict", repo="a", live=True, verdict="pull-first", ahead=2)
    )
    assert not _can_pull(
        SyncRow(kind="verdict", repo="a", live=False, verdict="pull-first", ahead=2)
    )
    assert not _can_pull(SyncRow(kind="verdict", repo="a", live=True, verdict="ok"))
    assert not _can_pull(SyncRow(kind="proposal", repo="a"))


def _sync_with_proposal() -> SyncStatus:
    report = SyncReport(
        current_host="h1",
        top_line="ok",
        hosts=[
            HostPanel(
                host="h1",
                source="live",
                verdicts=[RepoVerdict(repo="alpha", verdict="ok")],
            )
        ],
        proposals=["newrepo"],
    )
    return SyncStatus(
        report=report,
        report_generated_at=datetime.now(tz=UTC),
        fetch_in_flight=False,
    )


async def test_proposal_row_renders_and_track_writes_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    tracking = tmp_path / "dispatcher-sync.toml"
    app = DispatcherApp(
        DispatcherConfig(
            roots=(tmp_path,),
            maestro_db=make_maestro_home(tmp_path),
            tracking_file=tracking,
        )
    )
    make_atp(tmp_path)
    monkeypatch.setattr(
        "dispatcher.core.sync_service.SyncService.get",
        lambda self: _sync_with_proposal(),
    )
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-sync"
        await pilot.pause()
        idx = next(i for i, r in enumerate(app._sync_rows) if r.kind == "proposal")
        app.query_one("#sync-table", DataTable).move_cursor(row=idx)
        await pilot.press("t")
        await _settled(app, pilot)
        assert tracking.is_file()
        assert "newrepo" in tracking.read_text()


async def test_track_key_unconfigured_and_wrong_row(
    tmp_path: Path, monkeypatch
) -> None:
    app = _app(tmp_path)
    monkeypatch.setattr(
        "dispatcher.core.sync_service.SyncService.get",
        lambda self: _sync_with_proposal(),
    )
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-sync"
        await pilot.pause()
        # wrong row (verdict row) → no crash, nothing written
        app.query_one("#sync-table", DataTable).move_cursor(row=0)
        await pilot.press("t")
        # proposal row but tracking unconfigured → toast, no crash
        idx = next(i for i, r in enumerate(app._sync_rows) if r.kind == "proposal")
        app.query_one("#sync-table", DataTable).move_cursor(row=idx)
        await pilot.press("i")
        await _settled(app, pilot)


class _FakeConfigRunner:
    def __init__(self, outcome=None) -> None:
        from dispatcher.core.actions import ActionOutcome

        self.calls: list[tuple[str, ConfigCandidate]] = []
        self.outcome = outcome or ActionOutcome(
            action="update-spec-runner-config",
            dir="steward",
            ok=True,
            pr_url="https://example/pr/9",
        )

    def run(self, repo_dir, candidate):
        self.calls.append((repo_dir, candidate))
        return self.outcome


def _add_config_project(tmp_path: Path) -> Path:
    repo = tmp_path / "steward"
    repo.mkdir()
    (repo / "project.yaml").write_text(
        "project: steward\nspec_runner:\n  max_retries: 5\nworkstreams: []\n"
    )
    return repo


async def test_boots_with_seven_tabs_incl_config(tmp_path: Path) -> None:
    _add_config_project(tmp_path)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        assert len(app.query(TabPane)) == 7
        table = app.query_one("#config-table", DataTable)
        assert table.row_count == 1  # steward listed


async def test_config_editor_confirm_sends_candidate_live_tree_untouched(
    tmp_path: Path,
) -> None:
    from textual.widgets import Input

    from dispatcher.tui.config_edit import ConfigEditScreen

    repo = _add_config_project(tmp_path)
    live_before = (repo / "project.yaml").read_bytes()
    runner = _FakeConfigRunner()
    app = _app_with_config_runner(tmp_path, runner)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-config"
        await pilot.pause()
        table = app.query_one("#config-table", DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ConfigEditScreen)
        field = app.screen.query_one("#field-max_retries", Input)
        field.value = "9"
        await pilot.press("ctrl+y")
        await _settled(app, pilot)

    assert len(runner.calls) == 1
    repo_dir, candidate = runner.calls[0]
    assert repo_dir == "steward"
    assert candidate.typed["max_retries"] == 9
    assert candidate.extra_executor_config is None  # tri-state preserve
    assert candidate.base_mtime > 0
    assert (repo / "project.yaml").read_bytes() == live_before


async def test_config_editor_strict_coercion_blocks_runner(
    tmp_path: Path,
) -> None:
    from textual.widgets import Input

    _add_config_project(tmp_path)
    runner = _FakeConfigRunner()
    app = _app_with_config_runner(tmp_path, runner)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-config"
        await pilot.pause()
        table = app.query_one("#config-table", DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        app.screen.query_one("#field-auto_commit", Input).value = "yes"
        await pilot.press("ctrl+y")
        await _settled(app, pilot)
        assert runner.calls == []  # invalid bool never reaches the runner
        app.screen.query_one("#field-auto_commit", Input).value = "true"
        app.screen.query_one("#field-max_retries", Input).value = "3.5"
        await pilot.press("ctrl+y")
        await _settled(app, pilot)
        assert runner.calls == []  # invalid int refused too


async def test_config_editor_noop_outcome_benign(tmp_path: Path) -> None:
    from dispatcher.core.actions import ActionOutcome

    _add_config_project(tmp_path)
    runner = _FakeConfigRunner(
        ActionOutcome(
            action="update-spec-runner-config",
            dir="steward",
            ok=False,
            detail="no-op",
            error="no changes vs main",
        )
    )
    app = _app_with_config_runner(tmp_path, runner)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        app.query_one(TabbedContent).active = "tab-config"
        await pilot.pause()
        table = app.query_one("#config-table", DataTable)
        table.focus()
        table.move_cursor(row=0)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        await pilot.press("ctrl+y")
        await _settled(app, pilot)
        assert len(runner.calls) == 1  # ran, and the app didn't crash on no-op


def _app_with_config_runner(tmp_path: Path, runner) -> DispatcherApp:
    make_atp(tmp_path)
    make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    db = make_maestro_home(tmp_path)
    return DispatcherApp(
        DispatcherConfig(roots=(tmp_path,), maestro_db=db),
        config_runner=runner,
    )
