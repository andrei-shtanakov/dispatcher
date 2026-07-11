"""Read-side work-item correlation across project snapshots.

Contracts-roadmap phase 0.5: prove cross-project drill-down on existing
keys, with no emitters and no migrations. Maestro mints `task.id` and
passes it verbatim to arbiter's `route_task`, so grouping rows by their
project-local task id reconstructs the chain
`Maestro task -> arbiter decision -> arbiter outcome`. Maestro session
logs (`logs/<ULID>/*.jsonl`) carry `task_id` and `pipeline_id` on the
same record (arbiter's `outcome.recorded` does), which links chains to
runs and traces. Correlation happens entirely on the read side.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from dispatcher.core.models import ProjectSnapshot, TaskInfo

_ISO_PREFIX = 19  # "YYYY-MM-DDTHH:MM:SS" — comparable across naive/aware
_MAX_LOG_DIRS = 20  # newest session dirs scanned for task→pipeline links


class WorkItemLink(BaseModel):
    """One project-local record participating in a work-item chain."""

    project: str
    local_id: str
    status: str  # project-local vocabulary, verbatim — no common enum yet
    title: str | None = None
    timestamp: str | None = None
    cost_usd: float | None = None
    source: str


class WorkItemChain(BaseModel):
    """All records across projects that share one work-item id."""

    work_item_id: str
    projects: list[str]
    cross_project: bool
    links: list[WorkItemLink]
    pipeline_ids: list[str] = Field(default_factory=list)


class WorkItemsResponse(BaseModel):
    """Response of GET /api/work-items."""

    items: list[WorkItemChain]
    total: int
    cross_project: int


def scan_task_pipelines(
    logs_dir: Path, max_dirs: int = _MAX_LOG_DIRS
) -> dict[str, set[str]]:
    """Map task_id → pipeline_ids from `<logs_dir>/<ULID>/*.jsonl`.

    Any record whose Attributes carry both `task_id` and `pipeline_id`
    links the two. Newest run directories first (ULID names sort
    chronologically); corrupt lines and unreadable files are skipped.
    """
    links: dict[str, set[str]] = {}
    if not logs_dir.is_dir():
        return links
    run_dirs = sorted(
        (d for d in logs_dir.iterdir() if d.is_dir()),
        key=lambda d: d.name,
        reverse=True,
    )
    for run in run_dirs[:max_dirs]:
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
                attrs = rec.get("Attributes")
                if not isinstance(attrs, dict):
                    continue
                task_id = attrs.get("task_id")
                pipeline_id = attrs.get("pipeline_id")
                if task_id and pipeline_id:
                    links.setdefault(str(task_id), set()).add(str(pipeline_id))
    return links


def build_work_items(snapshots: list[ProjectSnapshot]) -> WorkItemsResponse:
    """Group tasks from all snapshots by their shared task id.

    Chains sort cross-project first, then by most-recent activity;
    links inside a chain stay chronological (task -> decision ->
    outcome). Statuses stay in each project's local vocabulary: this
    is a lossy read-side view, not a semantic mapping.
    """
    by_id: dict[str, list[WorkItemLink]] = {}
    logs_dir: Path | None = None
    for snap in snapshots:
        if snap.name == "Maestro" and snap.path:
            logs_dir = Path(snap.path) / "logs"
        for task in snap.tasks:
            by_id.setdefault(task.task_id, []).append(_link(snap.name, task))
    pipelines = scan_task_pipelines(logs_dir) if logs_dir is not None else {}
    chains: list[WorkItemChain] = []
    for work_item_id, links in by_id.items():
        projects = sorted({link.project for link in links})
        links.sort(key=lambda link: (link.timestamp or "")[:_ISO_PREFIX])
        chains.append(
            WorkItemChain(
                work_item_id=work_item_id,
                projects=projects,
                cross_project=len(projects) > 1,
                links=links,
                pipeline_ids=sorted(pipelines.get(work_item_id, ())),
            )
        )
    chains.sort(key=_newest, reverse=True)
    chains.sort(key=lambda c: not c.cross_project)  # stable: cross first
    return WorkItemsResponse(
        items=chains,
        total=len(chains),
        cross_project=sum(1 for c in chains if c.cross_project),
    )


def _link(project: str, task: TaskInfo) -> WorkItemLink:
    return WorkItemLink(
        project=project,
        local_id=task.task_id,
        status=task.status,
        title=task.title,
        timestamp=task.started_at,
        cost_usd=task.cost_usd,
        source=task.source,
    )


def _newest(chain: WorkItemChain) -> str:
    return max((link.timestamp or "")[:_ISO_PREFIX] for link in chain.links)
