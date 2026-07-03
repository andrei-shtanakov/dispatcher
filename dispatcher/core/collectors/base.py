"""Collector protocol and shared read-only data-access helpers."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

from dispatcher.core.models import ErrorEvent, ProjectSnapshot, SchemaVersionCheck

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


def read_rows(
    db_path: Path, sql: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
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
        except sqlite3.OperationalError as err:
            last_err = err
            if attempt + 1 < _RETRIES:
                time.sleep(0.2)
        except sqlite3.Error as err:
            raise SourceReadError(f"{db_path.name}: {err}") from err
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
    if key is not None and _KEY_RE.search(key):
        return "***"
    if isinstance(value, dict):
        return {k: mask_secrets(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_secrets(v) for v in value]
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
