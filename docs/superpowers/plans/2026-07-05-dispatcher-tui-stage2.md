# Dispatcher Stage 2: TUI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** A textual TUI (`dispatcher tui`) with full feature parity to the
Stage 1 web dashboard, consuming `dispatcher.core` directly.

**Architecture:** Snapshot collection moves from `server/app.py` into a new
thread-safe `dispatcher/core/service.py` shared by the server and the TUI.
The TUI is a tabbed textual app (`Projects | Errors | Models | Contracts`)
that collects in a `thread=True` worker every 10 s and renders DataTables;
Enter on a project row pushes a detail screen.

**Tech Stack:** Python ≥3.12, uv, pydantic v2, FastAPI (existing), textual
(new), pytest + anyio + Pilot.

**Spec:** `docs/superpowers/specs/2026-07-05-dispatcher-tui-design.md` —
read it before starting; its §2 web-parity checklist is the review
reference for every TUI screen.

## Global Constraints

- ONLY uv, never pip: `uv add package`, `uv run tool`.
- Line length 88; `uv run ruff format .` and `uv run ruff check .` must be
  clean after every task.
- `uv run pyrefly check` must be clean after every task (version warnings
  ignorable). Type hints required everywhere.
- Async tests use anyio (`pytestmark = pytest.mark.anyio`), never
  pytest-asyncio. `tests/conftest.py` already provides the `anyio_backend`
  fixture.
- Read-only invariants: never write into observed projects; SQLite strictly
  `mode=ro`; never read `_cowork_output/`. The TUI adds no writes anywhere.
- The public API surface (`/api/*` endpoints, `dispatcher.core.models`)
  must not change. Existing server tests must pass unchanged.
- All commits end with `Co-Authored-By:` trailer per repo convention.

---

### Task 1: Extract thread-safe SnapshotService into core

**Files:**
- Create: `dispatcher/core/service.py`
- Modify: `dispatcher/server/app.py`
- Create: `tests/test_service.py`

**Interfaces:**
- Consumes: `dispatcher.core.discovery.DispatcherConfig`, `discover`;
  `dispatcher.core.collectors.COLLECTORS`, `CollectContext`;
  `dispatcher.core.models.ProjectSnapshot`, `ErrorEvent`.
- Produces (later tasks rely on these exact names):
  - `dispatcher.core.service.SnapshotService` —
    `__init__(self, config: DispatcherConfig)`,
    `get(self) -> tuple[list[ProjectSnapshot], list[str]]` (thread-safe).
  - `dispatcher.core.service.recent_errors(events: list[ErrorEvent],
    days: int, now: datetime | None = None) -> list[ErrorEvent]`.
  - `dispatcher.core.service.ERRORS_DAYS_DEFAULT: int = 14`.
  - `dispatcher.server.app.recent_errors` stays importable (re-export), so
    `tests/test_api.py::test_recent_errors_helper` passes unchanged.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_service.py`:

```python
"""Tests for the shared SnapshotService and error-freshness helpers."""

import re
import threading
import time
from pathlib import Path

from conftest import make_arbiter, make_maestro_home

from dispatcher.core import service as service_mod
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.service import ERRORS_DAYS_DEFAULT, SnapshotService


def _config(tmp_path: Path) -> DispatcherConfig:
    make_arbiter(tmp_path)
    db = make_maestro_home(tmp_path)
    return DispatcherConfig(roots=(tmp_path,), maestro_db=db)


def test_collects_detected_and_undetected(tmp_path: Path) -> None:
    snapshots, _ = SnapshotService(_config(tmp_path)).get()
    by_name = {s.name: s for s in snapshots}
    assert by_name["arbiter"].detected is True
    assert by_name["atp-platform"].detected is False
    assert len(snapshots) == 5  # every registered collector gets a row


def test_ttl_cache_returns_same_object(tmp_path: Path) -> None:
    svc = SnapshotService(_config(tmp_path))
    assert svc.get() is svc.get()


def test_collector_crash_degrades_to_warning(
    tmp_path: Path, monkeypatch
) -> None:
    from dispatcher.core.collectors import COLLECTORS

    arbiter = next(c for c in COLLECTORS if c.name == "arbiter")

    def boom(self, path, ctx):  # noqa: ANN001, ARG001
        raise RuntimeError("kaput")

    monkeypatch.setattr(type(arbiter), "collect", boom)
    snapshots, _ = SnapshotService(_config(tmp_path)).get()
    snap = next(s for s in snapshots if s.name == "arbiter")
    assert any("collector crashed" in w for w in snap.warnings)


def test_concurrent_get_collects_once(tmp_path: Path, monkeypatch) -> None:
    svc = SnapshotService(_config(tmp_path))
    calls: list[int] = []
    real_collect = svc._collect

    def slow_collect():
        calls.append(1)
        time.sleep(0.05)
        return real_collect()

    monkeypatch.setattr(svc, "_collect", slow_collect)
    threads = [threading.Thread(target=svc.get) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(calls) == 1  # losers of the race are served from the cache


def test_web_days_default_matches_core() -> None:
    # The web JS cannot import the core constant and keeps its own copy.
    html_path = (
        Path(service_mod.__file__).parents[1] / "server" / "static" / "index.html"
    )
    match = re.search(r"ERRORS_DAYS_DEFAULT = (\d+)", html_path.read_text())
    assert match is not None
    assert int(match.group(1)) == ERRORS_DAYS_DEFAULT
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_service.py -v`
Expected: FAIL at collection with
`ModuleNotFoundError: No module named 'dispatcher.core.service'`

- [ ] **Step 3: Create the service module**

Create `dispatcher/core/service.py`. The cache/collect bodies move verbatim
from `dispatcher/server/app.py` (`_SnapshotCache`, `recent_errors`,
`_CACHE_TTL_SECONDS`, `_ISO_PREFIX`); new here: the lock, the docstrings,
`ERRORS_DAYS_DEFAULT`.

```python
"""Snapshot collection shared by the HTTP server and the TUI."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dispatcher.core.collectors import COLLECTORS, CollectContext
from dispatcher.core.discovery import DispatcherConfig, discover
from dispatcher.core.models import ErrorEvent, ProjectSnapshot

# Freshness default owned by frontends (the API `days` default stays None).
# The web JS cannot import this and keeps a copy in index.html; a parity
# test asserts the two values match.
ERRORS_DAYS_DEFAULT = 14
_CACHE_TTL_SECONDS = 5.0
_ISO_PREFIX = 19  # "YYYY-MM-DDTHH:MM:SS" — comparable across naive/aware


def recent_errors(
    events: list[ErrorEvent], days: int, now: datetime | None = None
) -> list[ErrorEvent]:
    """Keep events newer than `days` days; undated events are never dropped.

    Source timestamps mix naive and timezone-aware ISO strings, so the
    comparison uses the first 19 characters, which sort chronologically.
    """
    moment = now if now is not None else datetime.now(tz=UTC)
    cutoff = (moment - timedelta(days=days)).isoformat()[:_ISO_PREFIX]
    return [
        e
        for e in events
        if e.timestamp is None or e.timestamp[:_ISO_PREFIX] >= cutoff
    ]


class SnapshotService:
    """Collect-on-demand snapshot cache shared by all frontends.

    Thread-safe: the TUI calls `get()` from worker threads where an
    auto-refresh tick and a manual refresh can overlap; the lock serializes
    collection and the loser of the race is served from the TTL cache
    instead of collecting twice.
    """

    def __init__(self, config: DispatcherConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._at = 0.0
        self._data: tuple[list[ProjectSnapshot], list[str]] | None = None

    def get(self) -> tuple[list[ProjectSnapshot], list[str]]:
        """Return (snapshots, discovery warnings), collecting when stale."""
        with self._lock:
            now = time.monotonic()
            if self._data is not None and now - self._at < _CACHE_TTL_SECONDS:
                return self._data
            self._data = self._collect()
            self._at = now
            return self._data

    def _collect(self) -> tuple[list[ProjectSnapshot], list[str]]:
        # Known gap (accepted in the Stage 2 spec): discover() failures other
        # than the OSErrors it swallows internally propagate out of get().
        found, warnings = discover(self._config.roots, COLLECTORS)
        paths = {d.name: d.path for d in found}
        atp_root = paths.get("atp-platform")
        ctx = CollectContext(
            home=Path.home(),
            maestro_db=self._config.maestro_db,
            catalog_path=(
                None
                if atp_root is None
                else atp_root / "method" / "agents-catalog.toml"
            ),
        )
        snapshots: list[ProjectSnapshot] = []
        for project in found:
            try:
                snapshots.append(project.collector.collect(project.path, ctx))
            except Exception as err:  # noqa: BLE001 — last-resort guard
                snapshots.append(
                    ProjectSnapshot(
                        name=project.name,
                        path=str(project.path),
                        warnings=[f"collector crashed: {err}"],
                    )
                )
        detected = {s.name for s in snapshots}
        snapshots.extend(
            ProjectSnapshot(name=c.name, path="", detected=False)
            for c in COLLECTORS
            if c.name not in detected
        )
        return snapshots, warnings
```

- [ ] **Step 4: Refactor the server to use it**

Replace the whole top half of `dispatcher/server/app.py` (imports through
`_SnapshotCache`) so the file starts like this; every `@app.get` endpoint
body stays byte-identical, only `cache = _SnapshotCache(config)` becomes
`cache = SnapshotService(config)`:

```python
"""FastAPI application: read-only JSON API over collector snapshots."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles

from dispatcher.core.contracts import check_contracts
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.models import (
    ContractStatus,
    ErrorEvent,
    OverviewEntry,
    OverviewResponse,
    ProjectSnapshot,
)
from dispatcher.core.service import SnapshotService, recent_errors

__all__ = ["create_app", "recent_errors"]  # re-export: old import path

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(config: DispatcherConfig) -> FastAPI:
    """Build the API app for the given configuration."""
    app = FastAPI(title="Dispatcher", version="0.1.0")
    cache = SnapshotService(config)
    ...
```

Delete from `app.py`: `import time`, `UTC/datetime/timedelta` imports,
`_CACHE_TTL_SECONDS`, `_ISO_PREFIX`, the `recent_errors` function, the
whole `_SnapshotCache` class, and the now-unused
`from dispatcher.core.collectors import COLLECTORS, CollectContext` and
`discover` imports.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: ALL PASS — including `tests/test_api.py` completely unchanged
(`test_recent_errors_helper` still imports from `dispatcher.server.app`).

- [ ] **Step 6: Format, lint, type-check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean (fix anything reported before committing).

- [ ] **Step 7: Commit**

```bash
git add dispatcher/core/service.py dispatcher/server/app.py tests/test_service.py
git commit -m "refactor(core): extract thread-safe SnapshotService shared by server and tui

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: textual dependency, CLI subcommand, TUI skeleton

**Files:**
- Modify: `pyproject.toml`, `uv.lock` (via `uv add textual`)
- Modify: `dispatcher/cli.py`
- Create: `dispatcher/tui/__init__.py`, `dispatcher/tui/app.py`
- Modify: `tests/test_cli.py`
- Create: `tests/test_tui.py`

**Interfaces:**
- Consumes: `SnapshotService` from Task 1 (constructed, not yet called).
- Produces: `dispatcher.tui.app.DispatcherApp(config: DispatcherConfig)` —
  textual `App[None]` with tab ids `tab-projects`, `tab-errors`,
  `tab-models`, `tab-contracts`; table ids `projects-table`,
  `errors-table`, `models-table`, `contracts-table`; select ids
  `errors-project`, `errors-service`. CLI gains the `tui` subcommand.

- [ ] **Step 1: Add the dependency**

Run: `uv add textual`
Expected: `pyproject.toml` gains `textual>=<resolved>` in
`[project].dependencies`; `uv.lock` updated.

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_tui_subcommand_parses() -> None:
    args = build_parser().parse_args(["tui", "--config", "x.toml"])
    assert args.command == "tui"
    assert args.config == Path("x.toml")
```

(If `build_parser` / `Path` are not already imported there, add
`from pathlib import Path` and `from dispatcher.cli import build_parser` to
the existing imports.)

Create `tests/test_tui.py`:

```python
"""Pilot tests for the textual TUI."""

from pathlib import Path

import pytest
from textual.widgets import DataTable, TabPane

from conftest import (
    make_arbiter,
    make_atp,
    make_maestro_home,
    make_spec_runner,
)

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
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_tui.py tests/test_cli.py -v`
Expected: `test_tui.py` FAILS at collection with
`ModuleNotFoundError: No module named 'dispatcher.tui'`;
`test_tui_subcommand_parses` FAILS with `SystemExit` (unknown command).

- [ ] **Step 4: Create the TUI skeleton**

Create `dispatcher/tui/__init__.py`:

```python
"""Textual TUI frontend (Stage 2) over dispatcher.core."""
```

Create `dispatcher/tui/app.py`:

```python
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
            "project", "freshness", "tasks", "models", "tests", "errors",
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
```

- [ ] **Step 5: Add the CLI subcommand**

Replace `dispatcher/cli.py` content with:

```python
"""Command-line entry point: `dispatcher serve` and `dispatcher tui`."""

from __future__ import annotations

import argparse
from pathlib import Path

from dispatcher.core.discovery import load_config


def build_parser() -> argparse.ArgumentParser:
    """CLI argument parser (separate for testability)."""
    parser = argparse.ArgumentParser(prog="dispatcher")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the dashboard server")
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--config", type=Path, default=None)
    tui = sub.add_parser("tui", help="run the terminal dashboard")
    tui.add_argument("--config", type=Path, default=None)
    return parser


def main() -> None:
    """Entry point for the `dispatcher` console script."""
    args = build_parser().parse_args()
    config = load_config(args.config)
    if args.command == "tui":
        # Imported lazily: `serve` should not pay textual's import cost.
        from dispatcher.tui.app import DispatcherApp

        DispatcherApp(config).run()
        return
    import uvicorn

    from dispatcher.server.app import create_app

    port = args.port if args.port is not None else config.port
    uvicorn.run(create_app(config), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_tui.py tests/test_cli.py -v`
Expected: ALL PASS.

- [ ] **Step 7: Format, lint, type-check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock dispatcher/cli.py dispatcher/tui tests/test_cli.py tests/test_tui.py
git commit -m "feat(tui): textual skeleton with four tabs and dispatcher tui subcommand

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Collect worker, refresh loop, Projects tab

**Files:**
- Modify: `dispatcher/tui/app.py`
- Modify: `tests/test_tui.py`

**Interfaces:**
- Consumes: `SnapshotService.get()`,
  `dispatcher.core.contracts.check_contracts(projects: dict[str, Path])
  -> list[ContractStatus]`.
- Produces (later tasks rely on these): `DispatcherApp._apply(snapshots,
  warnings, contracts)` render entry point; state attrs `_snapshots`,
  `_warnings`, `_contracts`; helper `_snapshot(name) ->
  ProjectSnapshot | None`; stub methods `_render_errors()`,
  `_render_models()`, `_render_contracts()` (no-op bodies filled by Tasks
  4–5). `action_refresh()` spawns the thread worker.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tui.py`:

```python
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


async def test_collect_failure_keeps_last_data(
    tmp_path: Path, monkeypatch
) -> None:
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tui.py -v`
Expected: the four new tests FAIL (`row_count == 0`, `sub_title` not set,
`calls == []`); the Task 2 test still passes.

- [ ] **Step 3: Implement the worker and Projects rendering**

In `dispatcher/tui/app.py`, extend the imports:

```python
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual import work

from dispatcher.core.contracts import check_contracts
from dispatcher.core.models import ContractStatus, ErrorEvent, ProjectSnapshot
```

Add `self._contracts: list[ContractStatus] = []` to `__init__`.

Append to `on_mount` (after the `add_columns` calls):

```python
        self.set_interval(10.0, self.action_refresh)
        self.action_refresh()
```

Replace the `action_refresh` stub and add the worker plus rendering
methods to `DispatcherApp`:

```python
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
        self.sub_title = (
            f"updated {datetime.now():%H:%M:%S} · {len(warnings)} warnings"
        )
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
                    "not detected", "—", "—", "—", "—", "",
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

    def _render_errors(self) -> None:
        pass  # Task 5

    def _render_models(self) -> None:
        pass  # Task 4

    def _render_contracts(self) -> None:
        pass  # Task 4
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tui.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Format, lint, type-check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add dispatcher/tui/app.py tests/test_tui.py
git commit -m "feat(tui): background collect worker, 10s auto-refresh, projects tab

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Models and Contracts tabs

**Files:**
- Modify: `dispatcher/tui/app.py`
- Modify: `tests/test_tui.py`

**Interfaces:**
- Consumes: `self._snapshots`, `self._contracts` from Task 3;
  `ModelInUse`, `ContractStatus` fields from `dispatcher.core.models`.
- Produces: filled `_render_models()` / `_render_contracts()` bodies (same
  signatures as the Task 3 stubs).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tui.py`:

```python
async def test_models_table_matches_web_columns(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await _settled(app, pilot)
        table = app.query_one("#models-table", DataTable)
        assert table.row_count > 0
        rows = [table.get_row_at(i) for i in range(table.row_count)]
        # arbiter agents.toml fixture exposes a routable harness@model
        assert any(
            str(r[0]) == "arbiter" and str(r[3]) == "routable" for r in rows
        )
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tui.py -v`
Expected: the two new tests FAIL with `row_count == 0` / `StopIteration`.

- [ ] **Step 3: Implement the renderers**

Replace the Task 3 stubs in `dispatcher/tui/app.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tui.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Format, lint, type-check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add dispatcher/tui/app.py tests/test_tui.py
git commit -m "feat(tui): models and contracts tabs with web-parity columns

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Errors tab — filters, truncation, count, empty state

**Files:**
- Modify: `dispatcher/tui/app.py`
- Modify: `tests/test_tui.py`

**Interfaces:**
- Consumes: `recent_errors`, `ERRORS_DAYS_DEFAULT` (Task 1); state attrs
  and `truncate()` from Tasks 2–3.
- Produces: filled `_render_errors()`; `_merged_errors() ->
  list[ErrorEvent]`; `_update_select(select_id: str, values: list[str],
  current: str | None) -> None`; working `action_toggle_days()`;
  `on_select_changed` handler. `self._shown_errors` mirrors the rendered
  rows (Task 6 uses it to resolve the row under the cursor).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tui.py` (extend the existing `textual.widgets`
import with `Select`, and add
`from dispatcher.tui.app import DispatcherApp, truncate` in place of the
current `DispatcherApp` import):

```python
def test_truncate_web_parity() -> None:
    assert truncate("x" * 160) == "x" * 160
    assert truncate("x" * 161) == "x" * 160 + "…"


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tui.py -v`
Expected: new tests FAIL (`row_count == 0`, `_errors_days` never toggles,
`truncate` import error until the import line is fixed).

- [ ] **Step 3: Implement the errors pipeline**

Extend the `textual.widgets` import in `app.py` with `TabbedContent` (it is
already there from Task 2) and add the methods to `DispatcherApp`:

```python
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
        self.query_one(TabbedContent).get_tab("tab-errors").label = (
            f"Errors ({len(self._shown_errors)})"
        )
        detected = sorted(s.name for s in self._snapshots if s.detected)
        self._update_select("errors-project", detected, self._errors_project)
        services = {
            e.service for s in self._snapshots for e in s.errors if e.service
        }
        self._update_select(
            "errors-service", sorted(services), self._errors_service
        )

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
```

Replace the `action_toggle_days` stub:

```python
    def action_toggle_days(self) -> None:
        self._errors_days = (
            None if self._errors_days is not None else ERRORS_DAYS_DEFAULT
        )
        self._render_errors()
```

Update the test-file import as described in Step 1 if not already done.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tui.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Format, lint, type-check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add dispatcher/tui/app.py tests/test_tui.py
git commit -m "feat(tui): errors tab with project/service/days filters and truncation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Detail screens and cross-tab navigation

**Files:**
- Create: `dispatcher/tui/detail.py`
- Modify: `dispatcher/tui/app.py`
- Modify: `tests/test_tui.py`

**Interfaces:**
- Consumes: `ProjectSnapshot` fields; `self._shown_errors`,
  `self._snapshot()` from earlier tasks.
- Produces: `dispatcher.tui.detail.ProjectDetailScreen(snap:
  ProjectSnapshot)`; `dispatcher.tui.detail.ErrorMessageScreen(body:
  str)`; `DispatcherApp.on_data_table_row_selected`; working
  `action_project_errors()` (`e` key).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tui.py` (add
`from dispatcher.tui.detail import ErrorMessageScreen, ProjectDetailScreen`
and `from textual.widgets import TabbedContent` to the imports):

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tui.py -v`
Expected: FAIL at collection with
`ModuleNotFoundError: No module named 'dispatcher.tui.detail'`.

- [ ] **Step 3: Create the screens**

Create `dispatcher/tui/detail.py`:

```python
"""Screens pushed from the main app: project detail and full error text."""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, Static

from dispatcher.core.models import ProjectSnapshot


def _section(title: str, lines: list[str]) -> str:
    body = "\n".join(lines) if lines else "(none)"
    return f"[bold]{title}[/bold]\n{body}"


def _sections(s: ProjectSnapshot) -> list[tuple[str, list[str]]]:
    """Every ProjectSnapshot field, pre-escaped for Rich markup."""
    return [
        (
            "schema versions",
            [
                escape(
                    f"{c.database}: found={c.found} expected={c.expected} "
                    + (
                        "ok"
                        if c.ok
                        else "DRIFT" if c.ok is False else "unknown"
                    )
                )
                for c in s.schema_versions
            ],
        ),
        (
            "models",
            [
                escape(
                    f"{m.model_id} · harness={m.harness or '—'} "
                    f"· role={m.role} · vendor={m.vendor or '—'} "
                    f"· status={m.status or '—'}"
                )
                for m in s.models
            ],
        ),
        (
            "tasks",
            [
                escape(
                    f"{t.task_id} · {t.status} · {t.title or ''} "
                    f"({t.started_at or '?'} → {t.completed_at or '…'})"
                )
                for t in s.tasks
            ],
        ),
        (
            "test runs",
            [
                escape(
                    f"{r.name} · passed={r.passed} failed={r.failed} "
                    f"score={r.score} at {r.timestamp or '—'}"
                )
                for r in s.test_results
            ],
        ),
        (
            "configs",
            [escape(f"{c.path} ({c.format}): {c.summary}") for c in s.configs],
        ),
        (
            "errors",
            [
                escape(f"{e.timestamp or '—'} {e.service or '—'}: {e.body}")
                for e in s.errors
            ],
        ),
        ("warnings", [escape(w) for w in s.warnings]),
    ]


class ProjectDetailScreen(Screen[None]):
    """Read-only drill-down into one project's snapshot."""

    BINDINGS = [("escape,q", "app.pop_screen", "Back")]

    def __init__(self, snap: ProjectSnapshot) -> None:
        super().__init__()
        self._snap = snap

    def compose(self) -> ComposeResult:
        yield Header()
        s = self._snap
        with VerticalScroll():
            yield Static(
                f"[bold]{escape(s.name)}[/bold] — {escape(s.path)}\n"
                f"freshness: {escape(s.freshness or 'unknown')}"
            )
            for title, lines in _sections(s):
                yield Static(_section(title, lines), classes="detail-section")
        yield Footer()


class ErrorMessageScreen(ModalScreen[None]):
    """Full text of a truncated errors-table message."""

    BINDINGS = [("escape,q,enter", "app.pop_screen", "Close")]
    DEFAULT_CSS = """
    ErrorMessageScreen { align: center middle; }
    #error-message {
        width: 80%; height: 60%;
        background: $surface; border: solid $accent; padding: 1 2;
    }
    """

    def __init__(self, body: str) -> None:
        super().__init__()
        self._body = body

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="error-message"):
            yield Static(escape(self._body))
```

- [ ] **Step 4: Wire navigation in the app**

In `dispatcher/tui/app.py` add the import:

```python
from dispatcher.tui.detail import ErrorMessageScreen, ProjectDetailScreen
```

Add the handler and replace the `action_project_errors` stub in
`DispatcherApp`:

```python
    def on_data_table_row_selected(
        self, event: DataTable.RowSelected
    ) -> None:
        if event.data_table.id == "projects-table":
            name = str(event.row_key.value)
            snap = self._snapshot(name)
            if snap is not None and snap.detected:
                self.push_screen(ProjectDetailScreen(snap))
        elif event.data_table.id == "errors-table":
            idx = event.cursor_row
            if 0 <= idx < len(self._shown_errors):
                self.push_screen(
                    ErrorMessageScreen(self._shown_errors[idx].body)
                )

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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_tui.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Format, lint, type-check**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add dispatcher/tui/detail.py dispatcher/tui/app.py tests/test_tui.py
git commit -m "feat(tui): project detail screen, error message modal, e-key navigation

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Docs, full verification, parity sweep

**Files:**
- Modify: `README.md`
- Modify: `COWORK_CONTEXT.md`

**Interfaces:**
- Consumes: everything above.
- Produces: shipped Stage 2.

- [ ] **Step 1: Update README**

In `README.md`, next to the existing `dispatcher serve` usage, add:

```markdown
### Terminal UI

    uv run dispatcher tui                     # tabs: Projects / Errors / Models / Contracts
    uv run dispatcher tui --config dispatcher.toml

Keys: `r` refresh · `a` toggle errors 14d/all · `e` errors for selected
project · `Enter` drill down · `Esc` back · `q` quit. Auto-refresh: 10 s.
```

(Adapt placement/wording to the file's existing structure.)

- [ ] **Step 2: Update COWORK_CONTEXT.md**

- In `## Стек`, extend the HTTP line's sibling list with:
  `- **TUI**: textual (вкладки Projects/Errors/Models/Contracts), читает
  dispatcher.core напрямую через SnapshotService`
- In `## Запуск`, add `uv run dispatcher tui` alongside `serve`.
- In `## Roadmap`, change the Stage 2 line to
  `**Stage 2 (done, 2026-07-05)**: TUI (textual) поверх dispatcher.core
  (SnapshotService).`
- In `## Документы`, add the Stage 2 spec and plan paths.

- [ ] **Step 3: Full verification**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest -v`
Expected: everything clean and green (all test files, including the
untouched Stage 1 suites).

- [ ] **Step 4: Manual parity sweep**

Run `uv run dispatcher tui` against the real monorepo root and walk the
spec §2 web-parity checklist row by row (each row must be either covered
by a Pilot test from Tasks 3–6 or visually confirmed here). Note any
mismatch and fix before committing.

- [ ] **Step 5: Commit**

```bash
git add README.md COWORK_CONTEXT.md
git commit -m "docs: stage 2 tui usage and roadmap update

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
