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
