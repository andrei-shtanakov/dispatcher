# Dispatcher Stage 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read-only monitoring dashboard (FastAPI + single-page HTML) over the
on-disk artifacts of atp-platform, Maestro, arbiter, spec-runner, proctor.

**Architecture:** A `core` library (pydantic models + per-project collectors +
discovery) shared by all future frontends; a FastAPI server exposing JSON API
and serving one static HTML page. Collectors read SQLite/TOML/YAML/JSONL
directly, degrade to `warnings` instead of raising, version-gate every DB read.

**Tech Stack:** Python ≥3.12, uv, pydantic v2, FastAPI, uvicorn, pyyaml,
tomllib (stdlib), sqlite3 (stdlib). Tests: pytest + anyio + httpx.

Spec: `docs/superpowers/specs/2026-07-03-dispatcher-design.md` (approved).

## Global Constraints

- Python ≥3.12. Package management ONLY via `uv` (`uv add`, `uv run`). Never pip.
- Runtime deps exactly: `fastapi`, `uvicorn`, `pydantic`, `pyyaml`. Dev deps:
  `pytest`, `anyio`, `httpx`, `ruff`, `pyrefly`.
- Line length 88. Type hints on all functions. Docstrings on public APIs.
- Dispatcher NEVER writes to monitored projects and NEVER reads `_cowork_output/`.
- SQLite sources: open `file:...?mode=ro` (uri=True) + `busy_timeout=2000` +
  one retry on `OperationalError`. `immutable=1` is FORBIDDEN. Never CREATE or
  migrate a source DB.
- Every task ends with: `uv run ruff format . && uv run ruff check . --fix`,
  `uv run pyrefly check` (fix errors), `uv run pytest` (all green), then commit.
- Работаем в git-репозитории самого dispatcher (создаётся в Task 1), коммит на
  каждую задачу.

## File Structure

```
pyproject.toml                          # deps, script entry, ruff, pytest
dispatcher/__init__.py
dispatcher/core/__init__.py
dispatcher/core/models.py               # pydantic models — общий словарь данных
dispatcher/core/discovery.py            # dispatcher.toml + автодетект проектов
dispatcher/core/contracts.py            # drift-check каталога по whitelist
dispatcher/core/collectors/__init__.py  # реестр COLLECTORS
dispatcher/core/collectors/base.py      # Collector protocol + shared helpers
dispatcher/core/collectors/spec_runner.py
dispatcher/core/collectors/arbiter.py
dispatcher/core/collectors/maestro.py
dispatcher/core/collectors/atp.py
dispatcher/core/collectors/proctor.py
dispatcher/server/__init__.py
dispatcher/server/app.py                # FastAPI: /api/*, static
dispatcher/server/static/index.html     # SPA без сборки
dispatcher/cli.py                       # `dispatcher serve`
tests/conftest.py                       # builders фейковых деревьев проектов
tests/test_models.py  tests/test_base.py  tests/test_discovery.py
tests/test_spec_runner.py  tests/test_arbiter.py  tests/test_maestro.py
tests/test_atp.py  tests/test_proctor.py  tests/test_contracts.py
tests/test_api.py  tests/test_cli.py
```

`main.py` (заглушка uv init) удаляется в Task 1.

---

### Task 1: Scaffolding + pydantic-модели

**Files:**
- Modify: `pyproject.toml`
- Delete: `main.py`
- Create: `dispatcher/__init__.py`, `dispatcher/core/__init__.py`,
  `dispatcher/core/models.py`
- Test: `tests/test_models.py`

**Interfaces:**
- Produces: все модели из `dispatcher.core.models` (см. код ниже) — их
  импортируют ВСЕ последующие задачи. Имена и поля менять нельзя.

- [ ] **Step 1: git init + pyproject**

```bash
cd /Users/Andrei_Shtanakov/labs/all_ai_orchestrators/dispatcher
git init
rm main.py
```

Replace `pyproject.toml` content:

```toml
[project]
name = "dispatcher"
version = "0.1.0"
description = "Read-only monitoring dashboard for the AI-orchestrators ecosystem"
readme = "README.md"
requires-python = ">=3.12"
dependencies = []

[project.scripts]
dispatcher = "dispatcher.cli:main"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["dispatcher"]

[tool.ruff]
line-length = 88

[tool.ruff.lint]
extend-select = ["I"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

```bash
uv add fastapi uvicorn pydantic pyyaml
uv add --dev pytest anyio httpx ruff pyrefly
uv run pyrefly init
```

- [ ] **Step 2: Write the failing test**

`tests/test_models.py`:

```python
"""Tests for core pydantic models."""

from dispatcher.core.models import (
    ConfigSummary,
    ErrorEvent,
    ModelInUse,
    OverviewEntry,
    ProjectSnapshot,
    SchemaVersionCheck,
    TaskInfo,
    TestRunSummary,
)


def test_snapshot_defaults_are_empty() -> None:
    snap = ProjectSnapshot(name="x", path="/tmp/x")
    assert snap.detected is True
    assert snap.tasks == []
    assert snap.models == []
    assert snap.errors == []
    assert snap.warnings == []
    assert snap.freshness is None
    assert snap.collected_at is not None


def test_snapshot_serializes_to_json() -> None:
    snap = ProjectSnapshot(
        name="arbiter",
        path="/x",
        models=[ModelInUse(model_id="gpt-5.5", role="routable", source="a.toml")],
        tasks=[TaskInfo(task_id="t1", status="assign", source="db")],
        test_results=[TestRunSummary(run_id="r1", name="bench", source="db")],
        configs=[ConfigSummary(path="c.toml", format="toml")],
        errors=[ErrorEvent(body="boom", source="log")],
        schema_versions=[SchemaVersionCheck(database="d.db")],
        warnings=["w"],
    )
    data = snap.model_dump(mode="json")
    assert data["models"][0]["model_id"] == "gpt-5.5"
    assert data["errors"][0]["severity"] == "ERROR"


def test_overview_entry() -> None:
    entry = OverviewEntry(name="atp-platform", detected=False)
    assert entry.counts == {}
    assert entry.path is None
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dispatcher'` (или
ImportError по models).

- [ ] **Step 4: Implement models**

`dispatcher/__init__.py`:

```python
"""Dispatcher — read-only monitoring dashboard for the ecosystem projects."""
```

`dispatcher/core/__init__.py`:

```python
"""Core library: models, discovery, collectors. UI-independent."""
```

`dispatcher/core/models.py`:

```python
"""Normalized data models shared by collectors, server, and future frontends.

These pydantic schemas are the public contract of the dispatcher API
(consumed later by the TUI and the VSCode extension).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(tz=UTC)


class ModelInUse(BaseModel):
    """An LLM model referenced by a project's config or catalog."""

    model_id: str
    vendor: str | None = None
    harness: str | None = None
    role: str = "default"  # default|fallback|routable|enrolled|catalog|review|override
    status: str | None = None  # catalog lifecycle: active|deprecated|retired
    source: str


class TaskInfo(BaseModel):
    """A task/decision/schedule row from a project's state store."""

    task_id: str
    title: str | None = None
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    cost_usd: float | None = None
    source: str


class TestRunSummary(BaseModel):
    """A test/benchmark run result."""

    run_id: str
    name: str
    passed: int | None = None
    failed: int | None = None
    total: int | None = None
    score: float | None = None
    timestamp: str | None = None
    source: str


class ContractStatus(BaseModel):
    """Sync status of a cross-repo contract (canon vs vendored copy)."""

    name: str
    canonical_path: str
    vendored_path: str = ""
    in_sync: bool | None = None  # None: cannot compare (file missing / listing)
    detail: str | None = None


class ErrorEvent(BaseModel):
    """A failure/error signal from logs or state stores."""

    timestamp: str | None = None
    service: str | None = None
    severity: str = "ERROR"
    body: str
    pipeline_id: str | None = None
    source: str


class ConfigSummary(BaseModel):
    """Masked summary of a project config file."""

    path: str
    format: str
    summary: dict[str, Any] = Field(default_factory=dict)


class SchemaVersionCheck(BaseModel):
    """Version-gate result for one source database."""

    database: str
    found: str | None = None
    expected: str | None = None
    ok: bool | None = None  # None: could not determine


class ProjectSnapshot(BaseModel):
    """Everything dispatcher knows about one project at collect time."""

    name: str
    path: str
    detected: bool = True
    collected_at: datetime = Field(default_factory=_now)
    freshness: str | None = None  # ISO timestamp of newest source mtime
    schema_versions: list[SchemaVersionCheck] = Field(default_factory=list)
    models: list[ModelInUse] = Field(default_factory=list)
    tasks: list[TaskInfo] = Field(default_factory=list)
    test_results: list[TestRunSummary] = Field(default_factory=list)
    configs: list[ConfigSummary] = Field(default_factory=list)
    errors: list[ErrorEvent] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class OverviewEntry(BaseModel):
    """Light per-project row for the overview endpoint."""

    name: str
    path: str | None = None
    detected: bool
    freshness: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class OverviewResponse(BaseModel):
    """Response of GET /api/overview."""

    projects: list[OverviewEntry]
    warnings: list[str] = Field(default_factory=list)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_models.py -v`
Expected: 3 passed.

- [ ] **Step 6: Lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix
uv run pyrefly check
git add -A
git commit -m "feat: scaffold project, add core pydantic models"
```

---

### Task 2: Shared helpers (`collectors/base.py`)

**Files:**
- Create: `dispatcher/core/collectors/__init__.py` (пока пустой docstring),
  `dispatcher/core/collectors/base.py`
- Test: `tests/test_base.py`

**Interfaces:**
- Consumes: `ErrorEvent`, `SchemaVersionCheck`, `ProjectSnapshot` из Task 1.
- Produces (используют ВСЕ коллекторы, Task 4–8):
  - `class SourceReadError(Exception)`
  - `read_rows(db_path: Path, sql: str, params: tuple = ()) -> list[dict[str, Any]]`
  - `table_names(db_path: Path) -> set[str]`
  - `version_check(database: str, found: str | None, expected: str | None) -> SchemaVersionCheck`
  - `mask_secrets(value: Any, key: str | None = None) -> Any`
  - `shallow_summary(data: dict[str, Any]) -> dict[str, Any]`
  - `read_otel_errors(logs_dir: Path, limit: int = 20) -> list[ErrorEvent]`
  - `newest_mtime(paths: Iterable[Path]) -> str | None`
  - `read_yaml(path: Path) -> dict[str, Any]`, `read_toml(path: Path) -> dict[str, Any]`
  - `class CollectContext` (dataclass: `home: Path`,
    `maestro_db: Path | None = None`, `catalog_path: Path | None = None`)
  - `class Collector(Protocol)`: `name: str`,
    `detect(self, path: Path) -> bool`,
    `collect(self, path: Path, ctx: CollectContext) -> ProjectSnapshot`

- [ ] **Step 1: Write the failing test**

`tests/test_base.py`:

```python
"""Tests for shared collector helpers."""

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from dispatcher.core.collectors.base import (
    SourceReadError,
    mask_secrets,
    newest_mtime,
    read_otel_errors,
    read_rows,
    shallow_summary,
    table_names,
    version_check,
)


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);"
        "INSERT INTO t (name) VALUES ('a'), ('b');"
    )
    conn.commit()
    conn.close()


def test_read_rows_returns_dicts(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    _make_db(db)
    rows = read_rows(db, "SELECT id, name FROM t ORDER BY id")
    assert rows == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_read_rows_is_readonly(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    _make_db(db)
    with pytest.raises(SourceReadError):
        read_rows(db, "INSERT INTO t (name) VALUES ('c')")


def test_read_rows_missing_db(tmp_path: Path) -> None:
    with pytest.raises(SourceReadError):
        read_rows(tmp_path / "nope.db", "SELECT 1")


def test_read_rows_retries_through_lock(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    _make_db(db)
    locker = sqlite3.connect(db)
    locker.execute("BEGIN EXCLUSIVE")

    def release() -> None:
        time.sleep(0.3)
        locker.rollback()
        locker.close()

    t = threading.Thread(target=release)
    t.start()
    rows = read_rows(db, "SELECT count(*) AS n FROM t")
    t.join()
    assert rows[0]["n"] == 2


def test_table_names(tmp_path: Path) -> None:
    db = tmp_path / "x.db"
    _make_db(db)
    assert "t" in table_names(db)


def test_version_check() -> None:
    assert version_check("d", "1", "1").ok is True
    assert version_check("d", "2", "1").ok is False
    assert version_check("d", None, "1").ok is None


def test_mask_secrets_by_key_and_value() -> None:
    data = {
        "api_key": "abc123",
        "nested": {"bot_token": "xyz", "host": "ok"},
        "url": "nats://user:pass@host:4222",
        "auth": "Bearer sk-live-verysecret",
        "plain": "hello",
    }
    masked = mask_secrets(data)
    assert masked["api_key"] == "***"
    assert masked["nested"]["bot_token"] == "***"
    assert masked["nested"]["host"] == "ok"
    assert "user:pass" not in masked["url"]
    assert "verysecret" not in masked["auth"]
    assert masked["plain"] == "hello"


def test_shallow_summary_collapses_containers() -> None:
    out = shallow_summary({"a": 1, "b": [1, 2, 3], "c": {"x": 1}})
    assert out["a"] == 1
    assert out["b"] == "<3 items>"
    assert out["c"] == "<1 items>"


def test_read_otel_errors(tmp_path: Path) -> None:
    run = tmp_path / "logs" / "01AAAAAAAAAAAAAAAAAAAAAAAA"
    run.mkdir(parents=True)
    lines = [
        {"SeverityNumber": 9, "Body": "info", "Resource": {}},
        {
            "SeverityNumber": 17,
            "SeverityText": "ERROR",
            "Body": "boom",
            "Timestamp": "1719999999000000000",
            "Attributes": {"pipeline_id": "01AAA"},
            "Resource": {"service.name": "maestro"},
        },
        "not json at all",
    ]
    (run / "maestro-1.jsonl").write_text(
        "\n".join(
            item if isinstance(item, str) else json.dumps(item) for item in lines
        )
    )
    events = read_otel_errors(tmp_path / "logs")
    assert len(events) == 1
    assert events[0].body == "boom"
    assert events[0].service == "maestro"
    assert events[0].pipeline_id == "01AAA"
    assert events[0].timestamp is not None and events[0].timestamp.startswith("2024")


def test_read_otel_errors_missing_dir(tmp_path: Path) -> None:
    assert read_otel_errors(tmp_path / "no-logs") == []


def test_newest_mtime(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert newest_mtime([f, tmp_path / "missing"]) is not None
    assert newest_mtime([tmp_path / "missing"]) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_base.py -v`
Expected: FAIL — ImportError.

- [ ] **Step 3: Implement**

`dispatcher/core/collectors/__init__.py`:

```python
"""Per-project collectors. Registry is populated in later tasks."""
```

`dispatcher/core/collectors/base.py`:

```python
"""Collector protocol and shared read-only data-access helpers."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from dispatcher.core.models import ErrorEvent, ProjectSnapshot, SchemaVersionCheck

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - py>=3.11 always has it
    raise

SEVERITY_ERROR = 17  # OTel SeverityNumber: 17..20 == ERROR..FATAL
_RETRIES = 2
_KEY_RE = re.compile(r"(?i)(token|secret|password|passwd|api_?key|credential)")
_URL_CRED_RE = re.compile(r"://[^/@\s:]+:[^/@\s]+@")
_TOKEN_VALUE_RE = re.compile(
    r"(?i)(sk-[A-Za-z0-9_\-]{6,}|ghp_[A-Za-z0-9]{6,}"
    r"|xox[a-z]-[A-Za-z0-9\-]{6,}|bearer\s+\S+)"
)


class SourceReadError(Exception):
    """A source file/database could not be read."""


@dataclass(frozen=True)
class CollectContext:
    """Cross-project context passed to every collector."""

    home: Path
    maestro_db: Path | None = None
    catalog_path: Path | None = None


class Collector(Protocol):
    """One monitored project type."""

    name: str

    def detect(self, path: Path) -> bool:
        """Return True if `path` is this collector's project root."""
        ...

    def collect(self, path: Path, ctx: CollectContext) -> ProjectSnapshot:
        """Build a snapshot; never raises — degrade to snapshot.warnings."""
        ...


def read_rows(db_path: Path, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Run a read-only query with busy-timeout and one retry on lock.

    Raises SourceReadError on any failure. Never writes, never creates.
    """
    uri = f"file:{db_path}?mode=ro"
    last_err: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout = 2000")
                rows = conn.execute(sql, params).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()
        except sqlite3.Error as err:
            last_err = err
            if attempt + 1 < _RETRIES:
                time.sleep(0.2)
    raise SourceReadError(f"{db_path.name}: {last_err}")


def table_names(db_path: Path) -> set[str]:
    """Return table names of a SQLite database (read-only)."""
    rows = read_rows(db_path, "SELECT name FROM sqlite_master WHERE type='table'")
    return {row["name"] for row in rows}


def version_check(
    database: str, found: str | None, expected: str | None
) -> SchemaVersionCheck:
    """Compare a found schema version against the expected one."""
    ok = None if found is None or expected is None else found == expected
    return SchemaVersionCheck(database=database, found=found, expected=expected, ok=ok)


def mask_secrets(value: Any, key: str | None = None) -> Any:
    """Recursively mask secrets by key name and by value pattern."""
    if isinstance(value, dict):
        return {k: mask_secrets(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
    if key is not None and _KEY_RE.search(key):
        return "***"
    if isinstance(value, str):
        value = _URL_CRED_RE.sub("://***:***@", value)
        return _TOKEN_VALUE_RE.sub("***", value)
    return value


def shallow_summary(data: dict[str, Any]) -> dict[str, Any]:
    """Keep scalars, collapse containers to '<N items>' placeholders."""
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, (dict, list)):
            out[k] = f"<{len(v)} items>"
        else:
            out[k] = v
    return out


def _otel_timestamp(raw: Any) -> str | None:
    try:
        return datetime.fromtimestamp(int(raw) / 1e9, tz=UTC).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def read_otel_errors(logs_dir: Path, limit: int = 20) -> list[ErrorEvent]:
    """Read ERROR+ records from `<logs_dir>/<ULID>/<service>-<pid>.jsonl`.

    Newest run directories first (ULID names sort chronologically).
    Corrupt lines and unreadable files are skipped silently.
    """
    events: list[ErrorEvent] = []
    if not logs_dir.is_dir():
        return events
    run_dirs = sorted(
        (d for d in logs_dir.iterdir() if d.is_dir()),
        key=lambda d: d.name,
        reverse=True,
    )
    for run in run_dirs:
        if len(events) >= limit:
            break
        for jf in sorted(run.glob("*.jsonl")):
            try:
                text = jf.read_text(errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(rec, dict):
                    continue
                try:
                    severity = int(rec.get("SeverityNumber", 0))
                except (TypeError, ValueError):
                    continue
                if severity < SEVERITY_ERROR:
                    continue
                attrs = rec.get("Attributes") or {}
                resource = rec.get("Resource") or {}
                events.append(
                    ErrorEvent(
                        timestamp=_otel_timestamp(rec.get("Timestamp")),
                        service=resource.get("service.name"),
                        severity=str(rec.get("SeverityText", "ERROR")),
                        body=str(rec.get("Body", "")),
                        pipeline_id=attrs.get("pipeline_id"),
                        source=str(jf),
                    )
                )
    return events[:limit]


def newest_mtime(paths: Iterable[Path]) -> str | None:
    """ISO timestamp of the newest existing path, or None."""
    stamps = [p.stat().st_mtime for p in paths if p.exists()]
    if not stamps:
        return None
    return datetime.fromtimestamp(max(stamps), tz=UTC).isoformat()


def read_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML mapping; raises SourceReadError on failure."""
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as err:
        raise SourceReadError(f"{path.name}: {err}") from err
    return data if isinstance(data, dict) else {}


def read_toml(path: Path) -> dict[str, Any]:
    """Load a TOML mapping; raises SourceReadError on failure."""
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as err:
        raise SourceReadError(f"{path.name}: {err}") from err
```

Note: тест timestamp `1719999999000000000` → 2024-07-03Z, потому startswith("2024").

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_base.py -v`
Expected: 11 passed.

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix
uv run pyrefly check
git add -A && git commit -m "feat: shared collector helpers (ro-sqlite, masking, otel)"
```

---

### Task 3: Fixture builders (`tests/conftest.py`)

**Files:**
- Create: `tests/conftest.py`

**Interfaces:**
- Produces (используют Task 4–11): функции-билдеры фейковых проектов, каждая
  создаёт дерево в `tmp_path` и возвращает `Path` корня проекта:
  `make_spec_runner(root)`, `make_arbiter(root)`, `make_maestro(root)`,
  `make_atp(root)`, `make_proctor(root)`, `make_maestro_home(root)` (возвращает
  путь к `maestro.db`), `write_otel_error_log(project_root)`; fixture
  `anyio_backend`.
- Билдеры создают SQLite ровно с теми колонками, которые читают коллекторы
  (см. SQL в Task 4–8).

- [ ] **Step 1: Write conftest**

`tests/conftest.py`:

```python
"""Builders for miniature fake project trees used by collector tests."""

import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _db(path: Path, script: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(script)
    conn.commit()
    conn.close()


def write_otel_error_log(project_root: Path) -> None:
    """One run dir with one ERROR record."""
    run = project_root / "logs" / "01BBBBBBBBBBBBBBBBBBBBBBBB"
    run.mkdir(parents=True, exist_ok=True)
    rec = {
        "Timestamp": "1751500000000000000",
        "SeverityNumber": 17,
        "SeverityText": "ERROR",
        "Body": "subprocess failed",
        "Attributes": {"pipeline_id": "01BBB"},
        "Resource": {"service.name": "svc"},
    }
    (run / "svc-1.jsonl").write_text(json.dumps(rec) + "\n")


def make_spec_runner(root: Path) -> Path:
    p = root / "spec-runner"
    (p / "src" / "spec_runner").mkdir(parents=True)
    _db(
        p / "spec" / ".executor-state.db",
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY, status TEXT NOT NULL,
            started_at TEXT, completed_at TEXT);
        CREATE TABLE attempts (
            id INTEGER PRIMARY KEY, task_id TEXT, timestamp TEXT,
            success INTEGER, error TEXT, error_kind TEXT, error_stage TEXT);
        CREATE TABLE executor_meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO tasks VALUES
            ('T-1', 'completed', '2026-07-01T10:00:00', '2026-07-01T10:30:00'),
            ('T-2', 'in_progress', '2026-07-03T09:00:00', NULL);
        INSERT INTO attempts (task_id, timestamp, success, error, error_kind,
            error_stage)
            VALUES ('T-1', '2026-07-01T10:10:00', 0, 'lint failed', 'lint',
            'verify');
        INSERT INTO executor_meta VALUES ('total_completed', '1');
        """,
    )
    (p / "executor.config.yaml").write_text(
        "review_model: gpt-5-codex\nmax_retries: 2\napi_key: supersecret\n"
    )
    (p / "schemas").mkdir()
    (p / "schemas" / "status.schema.json").write_text("{}")
    write_otel_error_log(p)
    return p


def make_arbiter(root: Path) -> Path:
    p = root / "arbiter"
    (p / "arbiter-core").mkdir(parents=True)
    (p / "config").mkdir()
    (p / "config" / "agents.toml").write_text(
        '["claude_code@claude-sonnet-4-6"]\ndisplay_name = "CC"\n\n'
        '["codex_cli@gpt-5.5"]\ndisplay_name = "CX"\n\n'
        "[aider]\ndisplay_name = 'Aider'\n"
    )
    (p / "config" / "invariants.toml").write_text("[budget]\nmax_usd = 10\n")
    (p / "config" / "agents-catalog.toml").write_text("# vendored copy\nx = 1\n")
    _db(
        p / "arbiter.db",
        """
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY, applied_at TEXT);
        CREATE TABLE decisions (
            id INTEGER PRIMARY KEY, task_id TEXT, timestamp TEXT,
            chosen_agent TEXT, action TEXT, confidence REAL);
        CREATE TABLE benchmark_runs (
            run_id TEXT PRIMARY KEY, benchmark_id TEXT, agent_id TEXT,
            ts TEXT, score REAL, total_cost_usd REAL);
        INSERT INTO schema_version VALUES (1, '2026-04-06');
        INSERT INTO decisions (task_id, timestamp, chosen_agent, action,
            confidence)
            VALUES ('T-9', '2026-07-02T10:00:00',
            'claude_code@claude-sonnet-4-6', 'assign', 0.9);
        INSERT INTO benchmark_runs VALUES
            ('R-1', 'code-review', 'codex_cli@gpt-5.5',
             '2026-07-02T09:00:00', 0.83, 1.2);
        """,
    )
    write_otel_error_log(p)
    return p


def make_maestro(root: Path) -> Path:
    p = root / "Maestro"
    (p / "maestro").mkdir(parents=True)
    (p / "pyproject.toml").write_text("[project]\nname = 'maestro'\n")
    (p / "executor.config.yaml").write_text("review_model: gpt-5-codex\n")
    write_otel_error_log(p)
    return p


def make_maestro_home(root: Path) -> Path:
    """Fake ~/.maestro/maestro.db; returns the db path."""
    db = root / "home" / ".maestro" / "maestro.db"
    _db(
        db,
        """
        CREATE TABLE schema_migrations (
            version INTEGER PRIMARY KEY, name TEXT, applied_at TEXT);
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT, status TEXT, agent_type TEXT,
            created_at TEXT, started_at TEXT, completed_at TEXT);
        CREATE TABLE task_costs (
            id INTEGER PRIMARY KEY, task_id TEXT,
            estimated_cost_usd REAL);
        INSERT INTO schema_migrations VALUES (2, 'v2', '2026-04-19');
        INSERT INTO tasks VALUES
            ('M-1', 'Build feature', 'completed', 'claude_code',
             '2026-04-19T10:00:00', '2026-04-19T10:01:00',
             '2026-04-19T10:30:00');
        INSERT INTO task_costs (task_id, estimated_cost_usd)
            VALUES ('M-1', 0.42);
        """,
    )
    return db


def make_atp(root: Path) -> Path:
    p = root / "atp-platform"
    (p / "atp").mkdir(parents=True)
    (p / "method").mkdir()
    (p / "method" / "agents-catalog.toml").write_text(
        '[models."claude-sonnet-4-6"]\nvendor = "anthropic"\n'
        'status = "active"\n\n'
        '[models."gpt-5.5"]\nvendor = "openai"\nstatus = "active"\n\n'
        "[[agents]]\nharness = 'claude_code'\nmodel = 'claude-sonnet-4-6'\n"
        "routable = true\n\n"
        "[[agents]]\nharness = 'deepseek'\nmodel = 'deepseek-chat'\n"
        "routable = false\n"
    )
    (p / "atp.config.yaml").write_text(
        "default_llm_model: gpt-4o-mini\nparallel_workers: 4\n"
        "dashboard_secret_key: hushhush\n"
    )
    _db(
        p / ".atp-dashboard.db",
        """
        CREATE TABLE alembic_version (version_num VARCHAR PRIMARY KEY);
        CREATE TABLE test_executions (
            id INTEGER PRIMARY KEY, test_name TEXT, started_at TEXT,
            success BOOLEAN, score FLOAT, status TEXT,
            total_runs INTEGER, successful_runs INTEGER);
        CREATE TABLE benchmark_runs (
            id INTEGER PRIMARY KEY, agent_name TEXT, status TEXT,
            total_score FLOAT, started_at TEXT);
        INSERT INTO alembic_version VALUES ('f1a2b3c4d5e6');
        INSERT INTO test_executions (test_name, started_at, success, score,
            status, total_runs, successful_runs)
            VALUES ('suite/smoke', '2026-07-01T08:00:00', 1, 0.95,
            'completed', 3, 3);
        INSERT INTO benchmark_runs (agent_name, status, total_score,
            started_at)
            VALUES ('codex_cli@gpt-5.5', 'completed', 0.83,
            '2026-07-02T09:00:00');
        """,
    )
    results = p / "results" / "experiment"
    results.mkdir(parents=True)
    (results / "experiment_results.json").write_text("{}")
    bench = p / "_bench_output" / "r07"
    bench.mkdir(parents=True)
    (bench / "sweep.db").write_bytes(b"")
    return p


def make_proctor(root: Path) -> Path:
    p = root / "proctor"
    (p / "config").mkdir(parents=True)
    (p / "config" / "proctor.yaml").write_text(
        "llm:\n  default_model: claude-sonnet-4-20250514\n"
        "  fallback_model: ollama/llama3.2\n"
        "telegram:\n  bot_token: tg-secret\n"
    )
    _db(
        p / "data" / "state.db",
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, status TEXT, created_at TEXT,
            updated_at TEXT);
        CREATE TABLE schedules (
            id TEXT PRIMARY KEY, type TEXT, expression TEXT,
            enabled INTEGER, next_run TEXT, last_run TEXT);
        INSERT INTO tasks VALUES
            ('P-1', 'pending', '2026-07-01T07:00:00', '2026-07-01T07:00:00');
        INSERT INTO schedules VALUES
            ('S-1', 'cron', '0 9 * * *', 1, '2026-07-04T09:00:00',
             '2026-07-03T09:00:00');
        """,
    )
    logs = p / "logs"
    logs.mkdir()
    (logs / "scheduler-trigger.log").write_text(
        "2026-07-01 INFO started\n2026-07-02 ERROR trigger failed: boom\n"
    )
    return p
```

- [ ] **Step 2: Sanity-run + commit**

Run: `uv run pytest -v` (существующие тесты по-прежнему зелёные, conftest
импортируется без ошибок).

```bash
uv run ruff format . && uv run ruff check . --fix
uv run pyrefly check
git add -A && git commit -m "test: fixture builders for fake project trees"
```

---

### Task 4: Коллектор spec-runner

**Files:**
- Create: `dispatcher/core/collectors/spec_runner.py`
- Test: `tests/test_spec_runner.py`

**Interfaces:**
- Consumes: helpers из Task 2, `make_spec_runner` из Task 3.
- Produces: `class SpecRunnerCollector` с `name = "spec-runner"`; регистрация
  в реестре — в Task 9.

- [ ] **Step 1: Write the failing test**

`tests/test_spec_runner.py`:

```python
"""Tests for the spec-runner collector."""

from pathlib import Path

from dispatcher.core.collectors.base import CollectContext
from dispatcher.core.collectors.spec_runner import SpecRunnerCollector

from conftest import make_spec_runner


def _ctx(tmp_path: Path) -> CollectContext:
    return CollectContext(home=tmp_path / "home")


def test_detect(tmp_path: Path) -> None:
    p = make_spec_runner(tmp_path)
    c = SpecRunnerCollector()
    assert c.detect(p) is True
    assert c.detect(tmp_path) is False


def test_collect_happy_path(tmp_path: Path) -> None:
    p = make_spec_runner(tmp_path)
    snap = SpecRunnerCollector().collect(p, _ctx(tmp_path))
    assert snap.name == "spec-runner"
    assert {t.task_id for t in snap.tasks} == {"T-1", "T-2"}
    assert any("lint failed" in e.body for e in snap.errors)  # failed attempt
    assert any(e.body == "subprocess failed" for e in snap.errors)  # otel
    assert any(m.model_id == "gpt-5-codex" for m in snap.models)
    cfg = snap.configs[0]
    assert cfg.summary["api_key"] == "***"
    assert snap.schema_versions[0].ok is True
    assert snap.freshness is not None
    assert snap.warnings == []


def test_collect_without_db(tmp_path: Path) -> None:
    p = make_spec_runner(tmp_path)
    (p / "spec" / ".executor-state.db").unlink()
    snap = SpecRunnerCollector().collect(p, _ctx(tmp_path))
    assert snap.tasks == []
    assert any("executor-state" in w for w in snap.warnings)


def test_collect_with_unexpected_schema(tmp_path: Path) -> None:
    import sqlite3

    p = make_spec_runner(tmp_path)
    db = p / "spec" / ".executor-state.db"
    db.unlink()
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE something_else (id INTEGER)")
    conn.commit()
    conn.close()
    snap = SpecRunnerCollector().collect(p, _ctx(tmp_path))
    assert snap.schema_versions[0].ok is False
    assert snap.tasks == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_spec_runner.py -v` — FAIL (ImportError).

- [ ] **Step 3: Implement**

`dispatcher/core/collectors/spec_runner.py`:

```python
"""Collector for spec-runner: executor state DB, config, schemas, logs."""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.collectors.base import (
    CollectContext,
    SourceReadError,
    mask_secrets,
    newest_mtime,
    read_otel_errors,
    read_rows,
    read_yaml,
    shallow_summary,
    table_names,
    version_check,
)
from dispatcher.core.models import (
    ConfigSummary,
    ErrorEvent,
    ModelInUse,
    ProjectSnapshot,
    TaskInfo,
)

_EXPECTED_TABLES = {"tasks", "attempts", "executor_meta"}


class SpecRunnerCollector:
    """Reads spec-runner's on-disk executor state."""

    name = "spec-runner"

    def detect(self, path: Path) -> bool:
        return (path / "src" / "spec_runner").is_dir()

    def collect(self, path: Path, ctx: CollectContext) -> ProjectSnapshot:
        snap = ProjectSnapshot(name=self.name, path=str(path))
        db = path / "spec" / ".executor-state.db"
        self._collect_db(db, snap)
        self._collect_config(path / "executor.config.yaml", snap)
        snap.errors.extend(read_otel_errors(path / "logs"))
        snap.freshness = newest_mtime(
            [db, path / "executor.config.yaml", path / "logs"]
        )
        return snap

    def _collect_db(self, db: Path, snap: ProjectSnapshot) -> None:
        if not db.is_file() or db.stat().st_size == 0:
            snap.warnings.append("executor-state.db missing or empty")
            return
        try:
            tables = table_names(db)
            present = tables & _EXPECTED_TABLES
            snap.schema_versions.append(
                version_check(
                    db.name,
                    ",".join(sorted(present)),
                    ",".join(sorted(_EXPECTED_TABLES)),
                )
            )
            if not _EXPECTED_TABLES <= tables:
                return
            rows = read_rows(
                db,
                "SELECT task_id, status, started_at, completed_at FROM tasks "
                "ORDER BY started_at DESC LIMIT 50",
            )
            snap.tasks = [
                TaskInfo(
                    task_id=r["task_id"],
                    status=r["status"],
                    started_at=r["started_at"],
                    completed_at=r["completed_at"],
                    source=str(db),
                )
                for r in rows
            ]
            fails = read_rows(
                db,
                "SELECT task_id, timestamp, error, error_kind, error_stage "
                "FROM attempts WHERE success = 0 "
                "ORDER BY timestamp DESC LIMIT 20",
            )
            snap.errors.extend(
                ErrorEvent(
                    timestamp=r["timestamp"],
                    service=self.name,
                    body=(
                        f"{r['task_id']}: {r['error'] or r['error_kind'] or 'failed'}"
                        f" (stage={r['error_stage']})"
                    ),
                    source=str(db),
                )
                for r in fails
            )
        except SourceReadError as err:
            snap.warnings.append(str(err))

    def _collect_config(self, cfg: Path, snap: ProjectSnapshot) -> None:
        if not cfg.is_file():
            return
        try:
            data = read_yaml(cfg)
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        snap.configs.append(
            ConfigSummary(
                path=str(cfg),
                format="yaml",
                summary=mask_secrets(shallow_summary(data)),
            )
        )
        model = data.get("review_model")
        if isinstance(model, str):
            snap.models.append(
                ModelInUse(model_id=model, role="review", source=str(cfg))
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_spec_runner.py -v` — 4 passed.

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
uv run pytest
git add -A && git commit -m "feat: spec-runner collector"
```

---

### Task 5: Коллектор arbiter

**Files:**
- Create: `dispatcher/core/collectors/arbiter.py`
- Test: `tests/test_arbiter.py`

**Interfaces:**
- Consumes: helpers Task 2, `make_arbiter` Task 3.
- Produces: `class ArbiterCollector` с `name = "arbiter"`.

- [ ] **Step 1: Write the failing test**

`tests/test_arbiter.py`:

```python
"""Tests for the arbiter collector."""

from pathlib import Path

from dispatcher.core.collectors.arbiter import ArbiterCollector
from dispatcher.core.collectors.base import CollectContext

from conftest import make_arbiter


def _ctx(tmp_path: Path) -> CollectContext:
    return CollectContext(home=tmp_path / "home")


def test_detect(tmp_path: Path) -> None:
    p = make_arbiter(tmp_path)
    assert ArbiterCollector().detect(p) is True
    assert ArbiterCollector().detect(tmp_path) is False


def test_collect_happy_path(tmp_path: Path) -> None:
    p = make_arbiter(tmp_path)
    snap = ArbiterCollector().collect(p, _ctx(tmp_path))
    ver = snap.schema_versions[0]
    assert (ver.found, ver.expected, ver.ok) == ("1", "1", True)
    assert snap.tasks[0].task_id == "T-9"
    assert snap.tasks[0].status == "assign"
    assert snap.test_results[0].run_id == "R-1"
    assert snap.test_results[0].score == 0.83
    routable = {(m.harness, m.model_id) for m in snap.models}
    assert ("claude_code", "claude-sonnet-4-6") in routable
    assert ("codex_cli", "gpt-5.5") in routable
    assert ("aider", "aider") in routable
    assert any(e.body == "subprocess failed" for e in snap.errors)
    assert snap.warnings == []


def test_collect_without_db(tmp_path: Path) -> None:
    p = make_arbiter(tmp_path)
    (p / "arbiter.db").unlink()
    snap = ArbiterCollector().collect(p, _ctx(tmp_path))
    assert snap.tasks == []
    assert any("arbiter.db" in w for w in snap.warnings)
    assert len(snap.models) == 3  # config still readable
```

- [ ] **Step 2: Run to verify FAIL** — `uv run pytest tests/test_arbiter.py -v`

- [ ] **Step 3: Implement**

`dispatcher/core/collectors/arbiter.py`:

```python
"""Collector for arbiter: routing decisions, benchmarks, agent policy."""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.collectors.base import (
    CollectContext,
    SourceReadError,
    mask_secrets,
    newest_mtime,
    read_otel_errors,
    read_rows,
    read_toml,
    shallow_summary,
    version_check,
)
from dispatcher.core.models import (
    ConfigSummary,
    ModelInUse,
    ProjectSnapshot,
    TaskInfo,
    TestRunSummary,
)

_EXPECTED_SCHEMA = "1"


class ArbiterCollector:
    """Reads arbiter's decision DB and agent policy configs."""

    name = "arbiter"

    def detect(self, path: Path) -> bool:
        return (path / "config" / "agents.toml").is_file() and (
            path / "arbiter-core"
        ).is_dir()

    def collect(self, path: Path, ctx: CollectContext) -> ProjectSnapshot:
        snap = ProjectSnapshot(name=self.name, path=str(path))
        db = path / "arbiter.db"
        self._collect_db(db, snap)
        self._collect_agents(path / "config" / "agents.toml", snap)
        self._collect_invariants(path / "config" / "invariants.toml", snap)
        snap.errors.extend(read_otel_errors(path / "logs"))
        snap.freshness = newest_mtime([db, path / "config", path / "logs"])
        return snap

    def _collect_db(self, db: Path, snap: ProjectSnapshot) -> None:
        if not db.is_file() or db.stat().st_size == 0:
            snap.warnings.append("arbiter.db missing or empty")
            return
        try:
            ver = read_rows(db, "SELECT MAX(version) AS v FROM schema_version")
            found = None if ver[0]["v"] is None else str(ver[0]["v"])
            snap.schema_versions.append(
                version_check(db.name, found, _EXPECTED_SCHEMA)
            )
            decisions = read_rows(
                db,
                "SELECT task_id, timestamp, chosen_agent, action, confidence "
                "FROM decisions ORDER BY timestamp DESC LIMIT 50",
            )
            snap.tasks = [
                TaskInfo(
                    task_id=r["task_id"],
                    title=f"{r['chosen_agent']} (conf={r['confidence']:.2f})",
                    status=r["action"],
                    started_at=r["timestamp"],
                    source=str(db),
                )
                for r in decisions
            ]
            runs = read_rows(
                db,
                "SELECT run_id, benchmark_id, agent_id, ts, score, "
                "total_cost_usd FROM benchmark_runs ORDER BY ts DESC LIMIT 50",
            )
            snap.test_results = [
                TestRunSummary(
                    run_id=r["run_id"],
                    name=f"{r['benchmark_id']} @ {r['agent_id']}",
                    score=r["score"],
                    timestamp=r["ts"],
                    source=str(db),
                )
                for r in runs
            ]
        except SourceReadError as err:
            snap.warnings.append(str(err))

    def _collect_agents(self, cfg: Path, snap: ProjectSnapshot) -> None:
        if not cfg.is_file():
            snap.warnings.append("config/agents.toml missing")
            return
        try:
            data = read_toml(cfg)
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        for key in data:
            harness, _, model = key.partition("@")
            snap.models.append(
                ModelInUse(
                    model_id=model or harness,
                    harness=harness,
                    role="routable",
                    source=str(cfg),
                )
            )

    def _collect_invariants(self, cfg: Path, snap: ProjectSnapshot) -> None:
        if not cfg.is_file():
            return
        try:
            data = read_toml(cfg)
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        snap.configs.append(
            ConfigSummary(
                path=str(cfg),
                format="toml",
                summary=mask_secrets(shallow_summary(data)),
            )
        )
```

- [ ] **Step 4: Run to verify PASS** — `uv run pytest tests/test_arbiter.py -v`

- [ ] **Step 5: Lint, typecheck, full tests, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
uv run pytest
git add -A && git commit -m "feat: arbiter collector"
```

---

### Task 6: Коллектор Maestro

**Files:**
- Create: `dispatcher/core/collectors/maestro.py`
- Test: `tests/test_maestro.py`

**Interfaces:**
- Consumes: helpers Task 2; `make_maestro`, `make_maestro_home` Task 3;
  `ctx.maestro_db`, `ctx.catalog_path`.
- Produces: `class MaestroCollector` с `name = "Maestro"`.

- [ ] **Step 1: Write the failing test**

`tests/test_maestro.py`:

```python
"""Tests for the Maestro collector."""

from pathlib import Path

from dispatcher.core.collectors.base import CollectContext
from dispatcher.core.collectors.maestro import MaestroCollector

from conftest import make_atp, make_maestro, make_maestro_home


def test_detect(tmp_path: Path) -> None:
    p = make_maestro(tmp_path)
    assert MaestroCollector().detect(p) is True
    assert MaestroCollector().detect(tmp_path) is False


def test_collect_happy_path(tmp_path: Path) -> None:
    p = make_maestro(tmp_path)
    db = make_maestro_home(tmp_path)
    atp = make_atp(tmp_path)
    ctx = CollectContext(
        home=tmp_path / "home",
        maestro_db=db,
        catalog_path=atp / "method" / "agents-catalog.toml",
    )
    snap = MaestroCollector().collect(p, ctx)
    ver = snap.schema_versions[0]
    assert (ver.found, ver.expected, ver.ok) == ("2", "2", True)
    task = snap.tasks[0]
    assert task.task_id == "M-1"
    assert task.cost_usd == 0.42
    routable = {(m.harness, m.model_id) for m in snap.models}
    assert ("claude_code", "claude-sonnet-4-6") in routable
    assert ("deepseek", "deepseek-chat") not in routable  # routable=false
    running = [c for c in snap.configs if c.format == "pid"]
    assert running[0].summary == {"running": False}
    assert any(e.body == "subprocess failed" for e in snap.errors)
    assert snap.warnings == []


def test_collect_without_home_db(tmp_path: Path) -> None:
    p = make_maestro(tmp_path)
    ctx = CollectContext(home=tmp_path / "home", maestro_db=None)
    snap = MaestroCollector().collect(p, ctx)
    assert snap.tasks == []
    assert any("maestro.db" in w for w in snap.warnings)
```

- [ ] **Step 2: Run to verify FAIL** — `uv run pytest tests/test_maestro.py -v`

- [ ] **Step 3: Implement**

`dispatcher/core/collectors/maestro.py`:

```python
"""Collector for Maestro: task DB in ~/.maestro, catalog models, logs."""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.collectors.base import (
    CollectContext,
    SourceReadError,
    mask_secrets,
    newest_mtime,
    read_otel_errors,
    read_rows,
    read_toml,
    read_yaml,
    shallow_summary,
    version_check,
)
from dispatcher.core.models import (
    ConfigSummary,
    ModelInUse,
    ProjectSnapshot,
    TaskInfo,
)

_EXPECTED_SCHEMA = "2"


class MaestroCollector:
    """Reads Maestro's CLI state DB and routable models from the catalog."""

    name = "Maestro"

    def detect(self, path: Path) -> bool:
        return (path / "maestro").is_dir() and (path / "pyproject.toml").is_file()

    def collect(self, path: Path, ctx: CollectContext) -> ProjectSnapshot:
        snap = ProjectSnapshot(name=self.name, path=str(path))
        self._collect_db(ctx.maestro_db, snap)
        self._collect_catalog_models(ctx.catalog_path, snap)
        self._collect_config(path / "executor.config.yaml", snap)
        snap.errors.extend(read_otel_errors(path / "logs"))
        sources = [path / "executor.config.yaml", path / "logs"]
        if ctx.maestro_db is not None:
            sources.append(ctx.maestro_db)
        snap.freshness = newest_mtime(sources)
        return snap

    def _collect_db(self, db: Path | None, snap: ProjectSnapshot) -> None:
        if db is None or not db.is_file():
            snap.warnings.append(
                "maestro.db not found (~/.maestro/maestro.db; "
                "set maestro_db in dispatcher.toml)"
            )
            return
        try:
            ver = read_rows(db, "SELECT MAX(version) AS v FROM schema_migrations")
            found = None if ver[0]["v"] is None else str(ver[0]["v"])
            snap.schema_versions.append(
                version_check(db.name, found, _EXPECTED_SCHEMA)
            )
            costs = {
                r["task_id"]: r["cost"]
                for r in read_rows(
                    db,
                    "SELECT task_id, SUM(estimated_cost_usd) AS cost "
                    "FROM task_costs GROUP BY task_id",
                )
            }
            rows = read_rows(
                db,
                "SELECT id, title, status, agent_type, created_at, "
                "started_at, completed_at FROM tasks "
                "ORDER BY created_at DESC LIMIT 50",
            )
            snap.tasks = [
                TaskInfo(
                    task_id=r["id"],
                    title=f"{r['title']} [{r['agent_type']}]",
                    status=r["status"],
                    started_at=_opt_str(r["started_at"]),
                    completed_at=_opt_str(r["completed_at"]),
                    cost_usd=costs.get(r["id"]),
                    source=str(db),
                )
                for r in rows
            ]
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        pid = db.parent / "maestro.pid"
        snap.configs.append(
            ConfigSummary(
                path=str(pid), format="pid", summary={"running": pid.is_file()}
            )
        )

    def _collect_catalog_models(
        self, catalog: Path | None, snap: ProjectSnapshot
    ) -> None:
        if catalog is None or not catalog.is_file():
            snap.warnings.append("agents catalog not available (atp-platform?)")
            return
        try:
            data = read_toml(catalog)
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        for agent in data.get("agents", []):
            if not agent.get("routable"):
                continue
            snap.models.append(
                ModelInUse(
                    model_id=str(agent.get("model", "?")),
                    harness=str(agent.get("harness", "?")),
                    role="routable",
                    source=str(catalog),
                )
            )

    def _collect_config(self, cfg: Path, snap: ProjectSnapshot) -> None:
        if not cfg.is_file():
            return
        try:
            data = read_yaml(cfg)
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        snap.configs.append(
            ConfigSummary(
                path=str(cfg),
                format="yaml",
                summary=mask_secrets(shallow_summary(data)),
            )
        )


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)
```

- [ ] **Step 4: Run to verify PASS**, then lint/typecheck/full tests

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: maestro collector"
```

---

### Task 7: Коллектор atp-platform

**Files:**
- Create: `dispatcher/core/collectors/atp.py`
- Test: `tests/test_atp.py`

**Interfaces:**
- Consumes: helpers Task 2, `make_atp` Task 3.
- Produces: `class AtpCollector` с `name = "atp-platform"`.

- [ ] **Step 1: Write the failing test**

`tests/test_atp.py`:

```python
"""Tests for the atp-platform collector."""

from pathlib import Path

from dispatcher.core.collectors.atp import AtpCollector
from dispatcher.core.collectors.base import CollectContext

from conftest import make_atp


def _ctx(tmp_path: Path) -> CollectContext:
    return CollectContext(home=tmp_path / "home")


def test_detect(tmp_path: Path) -> None:
    p = make_atp(tmp_path)
    assert AtpCollector().detect(p) is True
    assert AtpCollector().detect(tmp_path) is False


def test_collect_happy_path(tmp_path: Path) -> None:
    p = make_atp(tmp_path)
    snap = AtpCollector().collect(p, _ctx(tmp_path))
    ver = snap.schema_versions[0]
    assert (ver.found, ver.expected, ver.ok) == (
        "f1a2b3c4d5e6",
        "f1a2b3c4d5e6",
        True,
    )
    names = {t.name for t in snap.test_results}
    assert "suite/smoke" in names
    assert any(n.startswith("benchmark:") for n in names)
    assert "experiment_results.json" in names
    assert "_bench_output/r07/sweep.db" in names
    smoke = next(t for t in snap.test_results if t.name == "suite/smoke")
    assert (smoke.passed, smoke.failed, smoke.total) == (3, 0, 3)
    catalog_models = {m.model_id for m in snap.models if m.role == "catalog"}
    assert catalog_models == {"claude-sonnet-4-6", "gpt-5.5"}
    roles = {(m.model_id, m.role) for m in snap.models}
    assert ("claude-sonnet-4-6", "routable") in roles
    assert ("deepseek-chat", "enrolled") in roles
    assert ("gpt-4o-mini", "default") in roles
    cfg = snap.configs[0]
    assert cfg.summary["dashboard_secret_key"] == "***"
    assert snap.warnings == []


def test_collect_without_dashboard_db(tmp_path: Path) -> None:
    p = make_atp(tmp_path)
    (p / ".atp-dashboard.db").unlink()
    snap = AtpCollector().collect(p, _ctx(tmp_path))
    assert any("atp-dashboard" in w for w in snap.warnings)
    assert any(m.role == "catalog" for m in snap.models)
```

- [ ] **Step 2: Run to verify FAIL** — `uv run pytest tests/test_atp.py -v`

- [ ] **Step 3: Implement**

`dispatcher/core/collectors/atp.py`:

```python
"""Collector for atp-platform: test results, SSOT catalog, config."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from dispatcher.core.collectors.base import (
    CollectContext,
    SourceReadError,
    mask_secrets,
    newest_mtime,
    read_otel_errors,
    read_rows,
    read_toml,
    read_yaml,
    shallow_summary,
    version_check,
)
from dispatcher.core.models import (
    ConfigSummary,
    ModelInUse,
    ProjectSnapshot,
    TestRunSummary,
)

_EXPECTED_ALEMBIC = "f1a2b3c4d5e6"


class AtpCollector:
    """Reads ATP dashboard DB, experiment results, and the SSOT catalog."""

    name = "atp-platform"

    def detect(self, path: Path) -> bool:
        return (path / "atp").is_dir() and (
            path / "method" / "agents-catalog.toml"
        ).is_file()

    def collect(self, path: Path, ctx: CollectContext) -> ProjectSnapshot:
        snap = ProjectSnapshot(name=self.name, path=str(path))
        db = path / ".atp-dashboard.db"
        self._collect_db(db, snap)
        self._collect_experiment(path, snap)
        self._collect_bench_output(path, snap)
        self._collect_catalog(path / "method" / "agents-catalog.toml", snap)
        self._collect_config(path / "atp.config.yaml", snap)
        snap.errors.extend(read_otel_errors(path / "logs"))
        snap.freshness = newest_mtime(
            [db, path / "method" / "agents-catalog.toml", path / "atp.config.yaml"]
        )
        return snap

    def _collect_db(self, db: Path, snap: ProjectSnapshot) -> None:
        if not db.is_file() or db.stat().st_size == 0:
            snap.warnings.append(".atp-dashboard.db missing or empty")
            return
        try:
            ver = read_rows(db, "SELECT version_num FROM alembic_version")
            found = ver[0]["version_num"] if ver else None
            snap.schema_versions.append(
                version_check(db.name, found, _EXPECTED_ALEMBIC)
            )
            tests = read_rows(
                db,
                "SELECT id, test_name, started_at, success, score, status, "
                "total_runs, successful_runs FROM test_executions "
                "ORDER BY started_at DESC LIMIT 50",
            )
            for r in tests:
                total = r["total_runs"]
                good = r["successful_runs"]
                snap.test_results.append(
                    TestRunSummary(
                        run_id=str(r["id"]),
                        name=r["test_name"],
                        passed=good,
                        failed=None if total is None else total - (good or 0),
                        total=total,
                        score=r["score"],
                        timestamp=_opt_str(r["started_at"]),
                        source=str(db),
                    )
                )
            runs = read_rows(
                db,
                "SELECT id, agent_name, status, total_score, started_at "
                "FROM benchmark_runs ORDER BY started_at DESC LIMIT 50",
            )
            snap.test_results.extend(
                TestRunSummary(
                    run_id=str(r["id"]),
                    name=f"benchmark: {r['agent_name']} [{r['status']}]",
                    score=r["total_score"],
                    timestamp=_opt_str(r["started_at"]),
                    source=str(db),
                )
                for r in runs
            )
        except SourceReadError as err:
            snap.warnings.append(str(err))

    def _collect_experiment(self, path: Path, snap: ProjectSnapshot) -> None:
        exp = path / "results" / "experiment" / "experiment_results.json"
        if not exp.is_file():
            return
        stamp = datetime.fromtimestamp(exp.stat().st_mtime, tz=UTC).isoformat()
        snap.test_results.append(
            TestRunSummary(
                run_id="experiment",
                name=exp.name,
                timestamp=stamp,
                source=str(exp),
            )
        )

    def _collect_bench_output(self, path: Path, snap: ProjectSnapshot) -> None:
        """List `_bench_output/**/*.db` artifacts (ad-hoc schemas: name+mtime only)."""
        bench = path / "_bench_output"
        if not bench.is_dir():
            return
        for db in sorted(bench.rglob("*.db")):
            stamp = datetime.fromtimestamp(db.stat().st_mtime, tz=UTC).isoformat()
            snap.test_results.append(
                TestRunSummary(
                    run_id=str(db.relative_to(path)),
                    name=str(db.relative_to(path)),
                    timestamp=stamp,
                    source=str(db),
                )
            )

    def _collect_catalog(self, catalog: Path, snap: ProjectSnapshot) -> None:
        try:
            data = read_toml(catalog)
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        for model_id, spec in data.get("models", {}).items():
            snap.models.append(
                ModelInUse(
                    model_id=model_id,
                    vendor=spec.get("vendor"),
                    status=spec.get("status"),
                    role="catalog",
                    source=str(catalog),
                )
            )
        for agent in data.get("agents", []):
            snap.models.append(
                ModelInUse(
                    model_id=str(agent.get("model", "?")),
                    harness=str(agent.get("harness", "?")),
                    role="routable" if agent.get("routable") else "enrolled",
                    source=str(catalog),
                )
            )

    def _collect_config(self, cfg: Path, snap: ProjectSnapshot) -> None:
        if not cfg.is_file():
            return
        try:
            data = read_yaml(cfg)
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        snap.configs.append(
            ConfigSummary(
                path=str(cfg),
                format="yaml",
                summary=mask_secrets(shallow_summary(data)),
            )
        )
        model = data.get("default_llm_model")
        if isinstance(model, str):
            snap.models.append(
                ModelInUse(model_id=model, role="default", source=str(cfg))
            )


def _opt_str(value: object) -> str | None:
    return None if value is None else str(value)
```

- [ ] **Step 4: Run to verify PASS**, lint/typecheck/full tests

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: atp collector"`

---

### Task 8: Коллектор proctor

**Files:**
- Create: `dispatcher/core/collectors/proctor.py`
- Test: `tests/test_proctor.py`

**Interfaces:**
- Consumes: helpers Task 2, `make_proctor` Task 3.
- Produces: `class ProctorCollector` с `name = "proctor"`.

- [ ] **Step 1: Write the failing test**

`tests/test_proctor.py`:

```python
"""Tests for the proctor collector."""

from pathlib import Path

from dispatcher.core.collectors.base import CollectContext
from dispatcher.core.collectors.proctor import ProctorCollector

from conftest import make_proctor


def _ctx(tmp_path: Path) -> CollectContext:
    return CollectContext(home=tmp_path / "home")


def test_detect(tmp_path: Path) -> None:
    p = make_proctor(tmp_path)
    assert ProctorCollector().detect(p) is True
    assert ProctorCollector().detect(tmp_path) is False


def test_collect_happy_path(tmp_path: Path) -> None:
    p = make_proctor(tmp_path)
    snap = ProctorCollector().collect(p, _ctx(tmp_path))
    ids = {t.task_id for t in snap.tasks}
    assert ids == {"P-1", "S-1"}
    sched = next(t for t in snap.tasks if t.task_id == "S-1")
    assert sched.status == "enabled"
    assert "cron 0 9 * * *" in (sched.title or "")
    roles = {(m.model_id, m.role) for m in snap.models}
    assert ("claude-sonnet-4-20250514", "default") in roles
    assert ("ollama/llama3.2", "fallback") in roles
    cfg = snap.configs[0]
    assert cfg.summary["telegram"] == "<1 items>"
    assert any("trigger failed" in e.body for e in snap.errors)
    assert snap.schema_versions[0].ok is True
    assert snap.warnings == []


def test_collect_without_state_db(tmp_path: Path) -> None:
    p = make_proctor(tmp_path)
    (p / "data" / "state.db").unlink()
    snap = ProctorCollector().collect(p, _ctx(tmp_path))
    assert snap.tasks == []
    assert any("state.db" in w for w in snap.warnings)
```

- [ ] **Step 2: Run to verify FAIL** — `uv run pytest tests/test_proctor.py -v`

- [ ] **Step 3: Implement**

`dispatcher/core/collectors/proctor.py`:

```python
"""Collector for proctor: task/schedule state, LLM config, text logs."""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.collectors.base import (
    CollectContext,
    SourceReadError,
    mask_secrets,
    newest_mtime,
    read_rows,
    read_yaml,
    shallow_summary,
    table_names,
    version_check,
)
from dispatcher.core.models import (
    ConfigSummary,
    ErrorEvent,
    ModelInUse,
    ProjectSnapshot,
    TaskInfo,
)

_EXPECTED_TABLES = {"tasks", "schedules"}
_LOG_ERRORS_LIMIT = 20


class ProctorCollector:
    """Reads proctor's state DB, proctor.yaml, and plain-text logs."""

    name = "proctor"

    def detect(self, path: Path) -> bool:
        return (path / "config" / "proctor.yaml").is_file()

    def collect(self, path: Path, ctx: CollectContext) -> ProjectSnapshot:
        snap = ProjectSnapshot(name=self.name, path=str(path))
        db = path / "data" / "state.db"
        self._collect_db(db, snap)
        self._collect_config(path / "config" / "proctor.yaml", snap)
        snap.errors.extend(_text_log_errors(path / "logs"))
        snap.freshness = newest_mtime(
            [db, path / "config" / "proctor.yaml", path / "logs"]
        )
        return snap

    def _collect_db(self, db: Path, snap: ProjectSnapshot) -> None:
        if not db.is_file() or db.stat().st_size == 0:
            snap.warnings.append("data/state.db missing or empty")
            return
        try:
            tables = table_names(db)
            present = tables & _EXPECTED_TABLES
            snap.schema_versions.append(
                version_check(
                    db.name,
                    ",".join(sorted(present)),
                    ",".join(sorted(_EXPECTED_TABLES)),
                )
            )
            if not _EXPECTED_TABLES <= tables:
                return
            rows = read_rows(
                db,
                "SELECT id, status, created_at, updated_at FROM tasks "
                "ORDER BY updated_at DESC LIMIT 50",
            )
            snap.tasks = [
                TaskInfo(
                    task_id=r["id"],
                    status=r["status"],
                    started_at=r["created_at"],
                    source=str(db),
                )
                for r in rows
            ]
            schedules = read_rows(
                db,
                "SELECT id, type, expression, enabled, next_run, last_run "
                "FROM schedules LIMIT 50",
            )
            snap.tasks.extend(
                TaskInfo(
                    task_id=r["id"],
                    title=f"{r['type']} {r['expression']} (next={r['next_run']})",
                    status="enabled" if r["enabled"] else "disabled",
                    started_at=r["last_run"],
                    source=str(db),
                )
                for r in schedules
            )
        except SourceReadError as err:
            snap.warnings.append(str(err))

    def _collect_config(self, cfg: Path, snap: ProjectSnapshot) -> None:
        try:
            data = read_yaml(cfg)
        except SourceReadError as err:
            snap.warnings.append(str(err))
            return
        snap.configs.append(
            ConfigSummary(
                path=str(cfg),
                format="yaml",
                summary=mask_secrets(shallow_summary(data)),
            )
        )
        llm = data.get("llm") or {}
        for key, role in (("default_model", "default"), ("fallback_model", "fallback")):
            model = llm.get(key)
            if isinstance(model, str):
                snap.models.append(
                    ModelInUse(model_id=model, role=role, source=str(cfg))
                )


def _text_log_errors(logs_dir: Path, limit: int = _LOG_ERRORS_LIMIT) -> list[ErrorEvent]:
    """Grep plain-text logs for ERROR lines (proctor has no OTel dirs)."""
    events: list[ErrorEvent] = []
    if not logs_dir.is_dir():
        return events
    for log in sorted(logs_dir.glob("*.log")):
        try:
            lines = log.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if "ERROR" in line:
                events.append(
                    ErrorEvent(service="proctor-a", body=line, source=str(log))
                )
    return events[-limit:]
```

- [ ] **Step 4: Run to verify PASS**, lint/typecheck/full tests

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: proctor collector"`

---

### Task 9: Реестр коллекторов + discovery

**Files:**
- Modify: `dispatcher/core/collectors/__init__.py`
- Create: `dispatcher/core/discovery.py`
- Test: `tests/test_discovery.py`

**Interfaces:**
- Consumes: все 5 коллекторов, билдеры Task 3.
- Produces:
  - `COLLECTORS: list[Collector]` в `dispatcher.core.collectors`
  - `@dataclass(frozen=True) DispatcherConfig`: `roots: tuple[Path, ...]`,
    `maestro_db: Path`, `port: int = 8787`
  - `load_config(config_path: Path | None = None) -> DispatcherConfig`
  - `@dataclass(frozen=True) DiscoveredProject`: `name: str`, `path: Path`,
    `collector: Collector`
  - `discover(roots, collectors) -> tuple[list[DiscoveredProject], list[str]]`

- [ ] **Step 1: Write the failing test**

`tests/test_discovery.py`:

```python
"""Tests for config loading and project discovery."""

from pathlib import Path

from dispatcher.core.collectors import COLLECTORS
from dispatcher.core.discovery import DispatcherConfig, discover, load_config

from conftest import make_arbiter, make_atp, make_proctor, make_spec_runner


def test_collectors_registry() -> None:
    names = {c.name for c in COLLECTORS}
    assert names == {"atp-platform", "Maestro", "arbiter", "spec-runner", "proctor"}


def test_load_config_from_file(tmp_path: Path) -> None:
    cfg = tmp_path / "dispatcher.toml"
    cfg.write_text(
        f'roots = ["{tmp_path}"]\nport = 9999\n'
        f'maestro_db = "{tmp_path}/m.db"\n'
    )
    conf = load_config(cfg)
    assert conf.roots == (tmp_path,)
    assert conf.port == 9999
    assert conf.maestro_db == tmp_path / "m.db"


def test_load_config_defaults(tmp_path: Path) -> None:
    conf = load_config(tmp_path / "absent.toml")
    assert len(conf.roots) == 1  # monorepo fallback
    assert conf.port == 8787
    assert conf.maestro_db.name == "maestro.db"


def test_discover_finds_projects(tmp_path: Path) -> None:
    make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    make_atp(tmp_path)
    found, warnings = discover((tmp_path,), COLLECTORS)
    assert {d.name for d in found} == {"arbiter", "spec-runner", "atp-platform"}
    assert warnings == []


def test_discover_missing_root(tmp_path: Path) -> None:
    found, warnings = discover((tmp_path / "nope",), COLLECTORS)
    assert found == []
    assert len(warnings) == 1


def test_discover_dedupes_by_name(tmp_path: Path) -> None:
    make_proctor(tmp_path)
    root2 = tmp_path / "second"
    root2.mkdir()
    make_proctor(root2)
    found, _ = discover((tmp_path, root2), COLLECTORS)
    assert [d.name for d in found] == ["proctor"]


def test_config_is_frozen(tmp_path: Path) -> None:
    conf = DispatcherConfig(roots=(tmp_path,), maestro_db=tmp_path / "m.db")
    try:
        conf.port = 1  # type: ignore[misc]
        raise AssertionError("should be frozen")
    except AttributeError:
        pass
```

- [ ] **Step 2: Run to verify FAIL** — `uv run pytest tests/test_discovery.py -v`

- [ ] **Step 3: Implement**

`dispatcher/core/collectors/__init__.py` (replace):

```python
"""Per-project collectors and their registry."""

from dispatcher.core.collectors.arbiter import ArbiterCollector
from dispatcher.core.collectors.atp import AtpCollector
from dispatcher.core.collectors.base import CollectContext, Collector
from dispatcher.core.collectors.maestro import MaestroCollector
from dispatcher.core.collectors.proctor import ProctorCollector
from dispatcher.core.collectors.spec_runner import SpecRunnerCollector

COLLECTORS: list[Collector] = [
    AtpCollector(),
    MaestroCollector(),
    ArbiterCollector(),
    SpecRunnerCollector(),
    ProctorCollector(),
]

__all__ = ["COLLECTORS", "CollectContext", "Collector"]
```

`dispatcher/core/discovery.py`:

```python
"""Dispatcher configuration and project auto-discovery."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dispatcher.core.collectors.base import Collector

DEFAULT_PORT = 8787
_DEFAULT_MAESTRO_DB = Path.home() / ".maestro" / "maestro.db"


def _monorepo_fallback_root() -> Path:
    """Parent of the dispatcher project — monorepo-layout convenience only.

    Standalone installs must list roots explicitly in dispatcher.toml.
    """
    return Path(__file__).resolve().parents[2].parent


@dataclass(frozen=True)
class DispatcherConfig:
    """Runtime configuration (dispatcher.toml)."""

    roots: tuple[Path, ...]
    maestro_db: Path = field(default_factory=lambda: _DEFAULT_MAESTRO_DB)
    port: int = DEFAULT_PORT


@dataclass(frozen=True)
class DiscoveredProject:
    """A detected project and the collector that owns it."""

    name: str
    path: Path
    collector: Collector


def load_config(config_path: Path | None = None) -> DispatcherConfig:
    """Load dispatcher.toml; absent file yields defaults."""
    data: dict = {}
    path = config_path or Path("dispatcher.toml")
    if path.is_file():
        data = tomllib.loads(path.read_text())
    roots = tuple(Path(p).expanduser() for p in data.get("roots", []))
    if not roots:
        roots = (_monorepo_fallback_root(),)
    maestro_db = Path(
        data.get("maestro_db", str(_DEFAULT_MAESTRO_DB))
    ).expanduser()
    return DispatcherConfig(
        roots=roots,
        maestro_db=maestro_db,
        port=int(data.get("port", DEFAULT_PORT)),
    )


def discover(
    roots: tuple[Path, ...], collectors: list[Collector]
) -> tuple[list[DiscoveredProject], list[str]]:
    """Scan roots; first match per collector wins across all roots."""
    found: list[DiscoveredProject] = []
    warnings: list[str] = []
    matched: set[str] = set()
    for root in roots:
        if not root.is_dir():
            warnings.append(f"root not found: {root}")
            continue
        try:
            children = sorted(d for d in root.iterdir() if d.is_dir())
        except OSError as err:
            warnings.append(f"cannot list {root}: {err}")
            continue
        for candidate in [root, *children]:
            for collector in collectors:
                if collector.name in matched:
                    continue
                try:
                    hit = collector.detect(candidate)
                except OSError:
                    continue
                if hit:
                    matched.add(collector.name)
                    found.append(
                        DiscoveredProject(
                            name=collector.name,
                            path=candidate,
                            collector=collector,
                        )
                    )
    return found, warnings
```

- [ ] **Step 4: Run to verify PASS**, lint/typecheck/full tests

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: collector registry and discovery"`

---

### Task 10: Contracts drift-check

**Files:**
- Create: `dispatcher/core/contracts.py`
- Test: `tests/test_contracts.py`

**Interfaces:**
- Consumes: `ContractStatus` (Task 1), билдеры Task 3.
- Produces: `check_contracts(projects: dict[str, Path]) -> list[ContractStatus]`
  — принимает `{имя проекта: корень}` найденных проектов.

- [ ] **Step 1: Write the failing test**

`tests/test_contracts.py`:

```python
"""Tests for the contract drift checker."""

from pathlib import Path

from dispatcher.core.contracts import check_contracts

from conftest import make_arbiter, make_atp, make_spec_runner


def test_drift_detected(tmp_path: Path) -> None:
    atp = make_atp(tmp_path)
    arb = make_arbiter(tmp_path)  # vendored copy differs from canon
    results = check_contracts({"atp-platform": atp, "arbiter": arb})
    catalog = next(r for r in results if r.name == "agents-catalog")
    assert catalog.in_sync is False


def test_in_sync(tmp_path: Path) -> None:
    atp = make_atp(tmp_path)
    arb = make_arbiter(tmp_path)
    canon = (atp / "method" / "agents-catalog.toml").read_text()
    (arb / "config" / "agents-catalog.toml").write_text(canon)
    results = check_contracts({"atp-platform": atp, "arbiter": arb})
    catalog = next(r for r in results if r.name == "agents-catalog")
    assert catalog.in_sync is True


def test_canon_missing(tmp_path: Path) -> None:
    arb = make_arbiter(tmp_path)
    results = check_contracts({"arbiter": arb})
    catalog = next(r for r in results if r.name == "agents-catalog")
    assert catalog.in_sync is None


def test_schema_listing(tmp_path: Path) -> None:
    sr = make_spec_runner(tmp_path)
    results = check_contracts({"spec-runner": sr})
    schemas = [r for r in results if r.detail == "published schema"]
    assert [s.name for s in schemas] == ["status.schema.json"]
```

- [ ] **Step 2: Run to verify FAIL** — `uv run pytest tests/test_contracts.py -v`

- [ ] **Step 3: Implement**

`dispatcher/core/contracts.py`:

```python
"""Cross-repo contract status: catalog drift check + schema listing.

The drift check compares the SSOT catalog canon against an EXPLICIT
whitelist of vendored copies. Never search by filename: test fixtures
elsewhere carry the same name and must not produce false drift.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from dispatcher.core.models import ContractStatus

_CANON_PROJECT = "atp-platform"
_CANON_REL = Path("method/agents-catalog.toml")
_VENDORED_WHITELIST: dict[str, Path] = {
    "arbiter": Path("config/agents-catalog.toml"),
}
_SCHEMA_PROJECT = "spec-runner"
_SCHEMA_DIR = Path("schemas")


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def check_contracts(projects: dict[str, Path]) -> list[ContractStatus]:
    """Build contract statuses for the detected projects."""
    results: list[ContractStatus] = []
    results.extend(_catalog_drift(projects))
    results.extend(_schema_listing(projects))
    return results


def _catalog_drift(projects: dict[str, Path]) -> list[ContractStatus]:
    canon_root = projects.get(_CANON_PROJECT)
    canon = None if canon_root is None else canon_root / _CANON_REL
    canon_hash = None if canon is None else _sha256(canon)
    results: list[ContractStatus] = []
    for project, rel in _VENDORED_WHITELIST.items():
        root = projects.get(project)
        if root is None:
            continue
        vendored = root / rel
        vendored_hash = _sha256(vendored)
        in_sync = (
            None
            if canon_hash is None or vendored_hash is None
            else canon_hash == vendored_hash
        )
        detail = None
        if canon_hash is None:
            detail = "canon not available"
        elif vendored_hash is None:
            detail = "vendored copy missing"
        results.append(
            ContractStatus(
                name="agents-catalog",
                canonical_path="" if canon is None else str(canon),
                vendored_path=str(vendored),
                in_sync=in_sync,
                detail=detail,
            )
        )
    return results


def _schema_listing(projects: dict[str, Path]) -> list[ContractStatus]:
    root = projects.get(_SCHEMA_PROJECT)
    if root is None:
        return []
    schema_dir = root / _SCHEMA_DIR
    if not schema_dir.is_dir():
        return []
    return [
        ContractStatus(
            name=f.name,
            canonical_path=str(f),
            detail="published schema",
        )
        for f in sorted(schema_dir.glob("*.json"))
    ]
```

- [ ] **Step 4: Run to verify PASS**, lint/typecheck/full tests

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: contract drift check"`

---

### Task 11: FastAPI server

**Files:**
- Create: `dispatcher/server/__init__.py`, `dispatcher/server/app.py`,
  `dispatcher/server/static/index.html` (минимальный, полноценный UI — Task 12)
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `COLLECTORS`, `discover`, `DispatcherConfig`, `check_contracts`,
  модели Task 1.
- Produces: `create_app(config: DispatcherConfig) -> FastAPI` — используется
  CLI (Task 12). Endpoints: `GET /api/overview` → `OverviewResponse`,
  `GET /api/projects/{name}` → `ProjectSnapshot` (404 если неизвестен),
  `GET /api/errors?limit=N`, `GET /api/models`, `GET /api/contracts`,
  `GET /` → index.html.

- [ ] **Step 1: Write the failing test**

`tests/test_api.py`:

```python
"""Integration tests for the HTTP API over a fixtures root."""

from pathlib import Path

import httpx
import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.server.app import create_app

from conftest import make_arbiter, make_atp, make_maestro_home, make_spec_runner

pytestmark = pytest.mark.anyio


def _client(tmp_path: Path) -> httpx.AsyncClient:
    make_atp(tmp_path)
    make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    db = make_maestro_home(tmp_path)
    config = DispatcherConfig(roots=(tmp_path,), maestro_db=db)
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_overview(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/overview")
    assert resp.status_code == 200
    data = resp.json()
    by_name = {p["name"]: p for p in data["projects"]}
    assert by_name["arbiter"]["detected"] is True
    assert by_name["arbiter"]["counts"]["tasks"] == 1
    assert by_name["Maestro"]["detected"] is False  # no project dir in root
    assert by_name["proctor"]["detected"] is False


async def test_project_detail_and_404(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        ok = await client.get("/api/projects/arbiter")
        missing = await client.get("/api/projects/unknown")
    assert ok.status_code == 200
    assert ok.json()["tasks"][0]["task_id"] == "T-9"
    assert missing.status_code == 404


async def test_errors_feed(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/errors", params={"limit": 5})
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) <= 5
    assert any(e["body"] == "subprocess failed" for e in events)


async def test_models_and_contracts(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        models = (await client.get("/api/models")).json()
        contracts = (await client.get("/api/contracts")).json()
    assert any(
        m["project"] == "arbiter" and m["role"] == "routable" for m in models
    )
    catalog = next(c for c in contracts if c["name"] == "agents-catalog")
    assert catalog["in_sync"] is False  # fixture vendored copy differs


async def test_index_served(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert "Dispatcher" in resp.text
```

- [ ] **Step 2: Run to verify FAIL** — `uv run pytest tests/test_api.py -v`

- [ ] **Step 3: Implement**

`dispatcher/server/__init__.py`:

```python
"""HTTP server: JSON API + static dashboard."""
```

Placeholder-страница `dispatcher/server/static/index.html` (полный UI — Task 12):

```html
<!doctype html>
<title>Dispatcher</title>
<h1>Dispatcher</h1>
```

`dispatcher/server/app.py`:

```python
"""FastAPI application: read-only JSON API over collector snapshots."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

from dispatcher.core.collectors import COLLECTORS, CollectContext
from dispatcher.core.contracts import check_contracts
from dispatcher.core.discovery import DispatcherConfig, discover
from dispatcher.core.models import (
    ContractStatus,
    ErrorEvent,
    OverviewEntry,
    OverviewResponse,
    ProjectSnapshot,
)

_CACHE_TTL_SECONDS = 5.0
_STATIC_DIR = Path(__file__).parent / "static"


class _SnapshotCache:
    """Collect-on-demand cache so a polling UI does not hammer the disk."""

    def __init__(self, config: DispatcherConfig) -> None:
        self._config = config
        self._at = 0.0
        self._data: tuple[list[ProjectSnapshot], list[str]] | None = None

    def get(self) -> tuple[list[ProjectSnapshot], list[str]]:
        now = time.monotonic()
        if self._data is not None and now - self._at < _CACHE_TTL_SECONDS:
            return self._data
        self._data = self._collect()
        self._at = now
        return self._data

    def _collect(self) -> tuple[list[ProjectSnapshot], list[str]]:
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


def create_app(config: DispatcherConfig) -> FastAPI:
    """Build the API app for the given configuration."""
    app = FastAPI(title="Dispatcher", version="0.1.0")
    cache = _SnapshotCache(config)

    @app.get("/api/overview", response_model=OverviewResponse)
    def overview() -> OverviewResponse:
        snapshots, warnings = cache.get()
        entries = [
            OverviewEntry(
                name=s.name,
                path=s.path or None,
                detected=s.detected,
                freshness=s.freshness,
                counts={
                    "tasks": len(s.tasks),
                    "models": len(s.models),
                    "test_results": len(s.test_results),
                    "errors": len(s.errors),
                },
                warnings=s.warnings,
            )
            for s in snapshots
        ]
        return OverviewResponse(projects=entries, warnings=warnings)

    @app.get("/api/projects/{name}", response_model=ProjectSnapshot)
    def project_detail(name: str) -> ProjectSnapshot:
        snapshots, _ = cache.get()
        for snap in snapshots:
            if snap.name == name:
                return snap
        raise HTTPException(status_code=404, detail=f"unknown project: {name}")

    @app.get("/api/errors", response_model=list[ErrorEvent])
    def errors(limit: int = 100) -> list[ErrorEvent]:
        snapshots, _ = cache.get()
        merged = [e for s in snapshots for e in s.errors]
        merged.sort(key=lambda e: e.timestamp or "", reverse=True)
        return merged[:limit]

    @app.get("/api/models")
    def models() -> list[dict[str, Any]]:
        snapshots, _ = cache.get()
        return [
            {"project": s.name, **m.model_dump()}
            for s in snapshots
            for m in s.models
        ]

    @app.get("/api/contracts", response_model=list[ContractStatus])
    def contracts() -> list[ContractStatus]:
        snapshots, _ = cache.get()
        projects = {
            s.name: Path(s.path) for s in snapshots if s.detected and s.path
        }
        return check_contracts(projects)

    app.mount("/", StaticFiles(directory=_STATIC_DIR, html=True), name="static")
    return app
```

- [ ] **Step 4: Run to verify PASS** — `uv run pytest tests/test_api.py -v`

- [ ] **Step 5: Lint, typecheck, full tests, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
uv run pytest
git add -A && git commit -m "feat: fastapi server with overview/detail/errors/models/contracts"
```

---

### Task 12: HTML-дашборд + CLI + README

**Files:**
- Modify: `dispatcher/server/static/index.html` (полный UI), `README.md`
- Create: `dispatcher/cli.py`
- Test: `tests/test_cli.py`; проверка UI — существующий
  `test_index_served` (обновить проверку на маркер `id="projects"`).

**Interfaces:**
- Consumes: `create_app`, `load_config`.
- Produces: console script `dispatcher` (`main()` в `dispatcher/cli.py`).

- [ ] **Step 1: Write the failing test**

`tests/test_cli.py`:

```python
"""Tests for the CLI argument parsing."""

from pathlib import Path

from dispatcher.cli import build_parser


def test_serve_defaults() -> None:
    args = build_parser().parse_args(["serve"])
    assert args.command == "serve"
    assert args.port is None
    assert args.config is None


def test_serve_overrides(tmp_path: Path) -> None:
    cfg = tmp_path / "d.toml"
    args = build_parser().parse_args(
        ["serve", "--port", "9000", "--config", str(cfg)]
    )
    assert args.port == 9000
    assert args.config == cfg
```

Обновить в `tests/test_api.py`:

```python
async def test_index_served(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert 'id="projects"' in resp.text
```

- [ ] **Step 2: Run to verify FAIL** — `uv run pytest tests/test_cli.py tests/test_api.py::test_index_served -v`

- [ ] **Step 3: Implement CLI**

`dispatcher/cli.py`:

```python
"""Command-line entry point: `dispatcher serve`."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from dispatcher.core.discovery import load_config
from dispatcher.server.app import create_app


def build_parser() -> argparse.ArgumentParser:
    """CLI argument parser (separate for testability)."""
    parser = argparse.ArgumentParser(prog="dispatcher")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the dashboard server")
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--config", type=Path, default=None)
    return parser


def main() -> None:
    """Entry point for the `dispatcher` console script."""
    args = build_parser().parse_args()
    config = load_config(args.config)
    port = args.port if args.port is not None else config.port
    uvicorn.run(create_app(config), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Implement the dashboard page**

Replace `dispatcher/server/static/index.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dispatcher</title>
<style>
  :root { color-scheme: light dark;
    --bg: #f6f7f9; --card: #fff; --ink: #1c2733; --dim: #68788c;
    --line: #e3e8ee; --bad: #c0392b; --ok: #1e8e5a; --warn: #b26a00; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #12161b; --card: #1b222b; --ink: #e7edf3;
      --dim: #8fa0b3; --line: #2a3441; } }
  * { box-sizing: border-box; margin: 0; }
  body { background: var(--bg); color: var(--ink);
    font: 14px/1.5 ui-sans-serif, system-ui, sans-serif; padding: 24px; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: var(--dim); margin-bottom: 20px; }
  .grid { display: grid; gap: 12px;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
  .card { background: var(--card); border: 1px solid var(--line);
    border-radius: 10px; padding: 14px; cursor: pointer; }
  .card.off { opacity: .55; cursor: default; }
  .card h2 { font-size: 15px; margin-bottom: 6px; }
  .counts { color: var(--dim); font-size: 13px; }
  .warn { color: var(--warn); font-size: 12px; margin-top: 6px; }
  .fresh { color: var(--dim); font-size: 12px; margin-top: 6px; }
  section { margin-top: 28px; }
  section > h2 { font-size: 16px; margin-bottom: 10px; }
  .tablewrap { overflow-x: auto; background: var(--card);
    border: 1px solid var(--line); border-radius: 10px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 8px 12px;
    border-bottom: 1px solid var(--line); white-space: nowrap; }
  th { color: var(--dim); font-weight: 600; }
  tr:last-child td { border-bottom: none; }
  .err { color: var(--bad); }
  .ok { color: var(--ok); }
  #detail { white-space: pre-wrap; font-family: ui-monospace, monospace;
    font-size: 12px; background: var(--card); border: 1px solid var(--line);
    border-radius: 10px; padding: 14px; max-height: 480px; overflow: auto; }
</style>
</head>
<body>
<h1>Dispatcher</h1>
<div class="sub">Ecosystem overview · auto-refresh 10s ·
  <span id="updated"></span></div>

<div class="grid" id="projects"></div>

<section><h2>Errors</h2>
  <div class="tablewrap"><table id="errors">
    <thead><tr><th>Time</th><th>Service</th><th>Message</th></tr></thead>
    <tbody></tbody></table></div></section>

<section><h2>Models</h2>
  <div class="tablewrap"><table id="models">
    <thead><tr><th>Project</th><th>Model</th><th>Harness</th><th>Role</th>
      <th>Vendor</th><th>Status</th></tr></thead>
    <tbody></tbody></table></div></section>

<section><h2>Contracts</h2>
  <div class="tablewrap"><table id="contracts">
    <thead><tr><th>Name</th><th>Canon</th><th>Vendored</th><th>Sync</th>
      </tr></thead><tbody></tbody></table></div></section>

<section><h2>Project detail <span id="detail-name"></span></h2>
  <div id="detail">click a project card…</div></section>

<script>
const esc = s => String(s ?? "").replace(/[&<>"']/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const get = async p => (await fetch(p)).json();

async function refresh() {
  try {
    const [ov, errors, models, contracts] = await Promise.all([
      get("/api/overview"), get("/api/errors?limit=50"),
      get("/api/models"), get("/api/contracts")]);
    document.getElementById("projects").innerHTML = ov.projects.map(p => `
      <div class="card ${p.detected ? "" : "off"}"
           ${p.detected ? `onclick="detail('${esc(p.name)}')"` : ""}>
        <h2>${esc(p.name)}</h2>
        ${p.detected
          ? `<div class="counts">tasks ${p.counts.tasks ?? 0} ·
               models ${p.counts.models ?? 0} ·
               tests ${p.counts.test_results ?? 0} ·
               <span class="${p.counts.errors ? "err" : ""}">
                 errors ${p.counts.errors ?? 0}</span></div>
             <div class="fresh">${esc(p.freshness ?? "freshness unknown")}</div>`
          : `<div class="counts">not detected</div>`}
        ${(p.warnings || []).map(w => `<div class="warn">⚠ ${esc(w)}</div>`)
          .join("")}
      </div>`).join("");
    document.querySelector("#errors tbody").innerHTML = errors.map(e => `
      <tr><td>${esc(e.timestamp ?? "—")}</td><td>${esc(e.service ?? "—")}</td>
      <td class="err">${esc(e.body)}</td></tr>`).join("")
      || `<tr><td colspan="3" class="ok">no errors 🎉</td></tr>`;
    document.querySelector("#models tbody").innerHTML = models.map(m => `
      <tr><td>${esc(m.project)}</td><td>${esc(m.model_id)}</td>
      <td>${esc(m.harness ?? "—")}</td><td>${esc(m.role)}</td>
      <td>${esc(m.vendor ?? "—")}</td><td>${esc(m.status ?? "—")}</td></tr>`)
      .join("");
    document.querySelector("#contracts tbody").innerHTML = contracts.map(c => `
      <tr><td>${esc(c.name)}</td><td>${esc(c.canonical_path)}</td>
      <td>${esc(c.vendored_path || "—")}</td>
      <td class="${c.in_sync === false ? "err" : "ok"}">
        ${c.in_sync === null ? esc(c.detail ?? "n/a")
          : c.in_sync ? "✓ in sync" : "✗ drift"}</td></tr>`).join("");
    document.getElementById("updated").textContent =
      "updated " + new Date().toLocaleTimeString();
  } catch (err) {
    document.getElementById("updated").textContent = "refresh failed: " + err;
  }
}

async function detail(name) {
  document.getElementById("detail-name").textContent = "— " + name;
  const snap = await get("/api/projects/" + encodeURIComponent(name));
  document.getElementById("detail").textContent =
    JSON.stringify(snap, null, 2);
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
```

- [ ] **Step 5: README**

Replace `README.md`:

```markdown
# Dispatcher

Read-only monitoring dashboard for the AI-orchestrators ecosystem
(atp-platform, Maestro, arbiter, spec-runner, proctor). Reads on-disk
artifacts directly — monitored projects don't need to be running or even
installed; missing ones simply don't show up.

## Run

    uv run dispatcher serve            # http://127.0.0.1:8787
    uv run dispatcher serve --port 9000 --config /path/dispatcher.toml

## Configure (optional `dispatcher.toml`)

    roots = ["/Users/you/labs/all_ai_orchestrators"]
    maestro_db = "~/.maestro/maestro.db"
    port = 8787

Without a config, dispatcher scans its own parent directory (monorepo
layout). Standalone installs must list `roots` explicitly.

## API

`/api/overview`, `/api/projects/{name}`, `/api/errors?limit=N`,
`/api/models`, `/api/contracts` — pydantic-typed JSON; this is the same
contract the future TUI and VSCode extension consume.

## Design

See `docs/superpowers/specs/2026-07-03-dispatcher-design.md`.
```

- [ ] **Step 6: Run all tests, verify PASS**

Run: `uv run pytest -v` — все зелёные (включая обновлённый
`test_index_served`).

- [ ] **Step 7: Smoke-run против реальной монорепы**

```bash
uv run dispatcher serve --port 8787 &
sleep 2
curl -s http://127.0.0.1:8787/api/overview | head -c 2000
curl -s http://127.0.0.1:8787/api/contracts
kill %1
```

Expected: JSON с реальными проектами (atp-platform, Maestro, arbiter,
spec-runner, proctor — detected: true), никаких 500.

- [ ] **Step 8: Lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
uv run pytest
git add -A && git commit -m "feat: html dashboard, cli entry point, readme"
```

---

## Post-plan follow-ups (вне Stage 1, не делать без запроса)

- Регистрация dispatcher в COWORK_CONTEXT.md / реестре экосистемы + CI.
- TUI (Stage 2), VSCode extension (Stage 3).
- Стабильный read-model/status.json у проектов-владельцев (снятие связанности
  с приватными схемами БД).
