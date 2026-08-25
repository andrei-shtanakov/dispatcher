"""Epics read-model — the stream axis as dispatcher shows it (ADR-ECO-010 Ф3).

Dispatcher shows STATE; Robin shows movement. So this module reads three planes of
open work — `TODO.md` items, GitHub issues, GitHub pull requests — and never mixes in
commit activity, which answers a different question ("what moved this week") and would
quietly turn a backlog count into a throughput count.

Three rules this module exists to keep, all from ADR-ECO-010 D10/D11:

1. **No total without its planes.** Every count carries which planes were read. A view
   that merges a fully-read plane with an unread one is not a total, and the reader
   cannot see the difference.
2. **`unclassified` is a bucket, not an epic.** It has no program and no `kind`, and it
   stays visible under any `kind` filter: an unmarked artifact cannot honestly be
   assigned to a program, and hiding it behind a filter turns a partial aggregate into
   a confident-looking one.
3. **`last_activity_at` only from proven semantics.** Observation time is not activity
   time. The TODO plane has no per-item change date (plan-fields carries the OBSERVING
   commit), so it contributes nothing here; merged pull requests carry `merged_at` and
   do. The field names its sources rather than implying them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.snapshot_contract import (
    WorkspaceSnapshotV1,
    carries_epic_axis,
)

UNCLASSIFIED = "unclassified"
_PLANES = ("todo", "issues", "pull_requests")


class PlaneState(BaseModel):
    """One plane's contribution to one row, with its completeness attached."""

    plane: str
    state: str  # read | unavailable
    count: int = 0
    detail: str | None = None


class EpicRow(BaseModel):
    """One epic (or the unclassified bucket) as a table row."""

    type: str = "epic"  # epic | classification_bucket
    id: str
    program: str | None = None
    kind: str | None = None
    title: str | None = None
    status: str | None = None
    moved_to: str | None = None
    planes: list[PlaneState] = Field(default_factory=list)
    defects: dict[str, int] = Field(default_factory=dict)
    last_activity_at: str | None = None
    activity_sources: list[str] = Field(default_factory=list)

    @property
    def total_read(self) -> int:
        return sum(p.count for p in self.planes if p.state == "read")


class EpicArtifact(BaseModel):
    """One artifact inside an epic, with where it was read from."""

    plane: str
    repo: str
    ref: str
    title: str | None = None
    defect: str | None = None


class EpicDetail(BaseModel):
    row: EpicRow
    artifacts: list[EpicArtifact] = Field(default_factory=list)


class DefectRow(BaseModel):
    """The reverse cut: where the fleet breaks, independent of stream."""

    defect: str
    title: str | None = None
    count: int = 0
    by_epic: dict[str, int] = Field(default_factory=dict)


class EpicsView(BaseModel):
    """The whole stream axis: rows, buckets, planes and the registry's own health."""

    generated_at: str | None = None
    registry_path: str | None = None
    registry_ok: bool = True
    registry_diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    programs: dict[str, dict[str, str]] = Field(default_factory=dict)
    planes: list[PlaneState] = Field(default_factory=list)
    rows: list[EpicRow] = Field(default_factory=list)
    defects: list[DefectRow] = Field(default_factory=list)


@dataclass(frozen=True)
class _Counted:
    """Per-epic accumulator, keyed by plane."""

    per_plane: dict[str, dict[str, int]]
    per_defect: dict[str, dict[str, int]]
    artifacts: dict[str, list[EpicArtifact]]
    activity: dict[str, str]


def registry_path(config: DispatcherConfig) -> Path | None:
    """Where `epics.toml` lives: configured, else derived from the workspace roots.

    Derivation follows the umbrella, not this repo: the registry is a fleet fact owned
    by `ai-orchestrators-workspace` and read LIVE (never vendored — a weekly-changing
    registry is stale the moment it is pinned).
    """
    configured = getattr(config, "epics_registry", None)
    if configured is not None:
        return Path(configured)
    for root in config.roots:
        candidate = root / "ai-orchestrators-workspace" / "epics.toml"
        if candidate.is_file():
            return candidate
    return None


def _manifest_path(config: DispatcherConfig) -> Path | None:
    for root in config.roots:
        candidate = root / "ai-orchestrators-workspace" / "workspace-manifest.toml"
        if candidate.is_file():
            return candidate
    return None


def _todo_plane(config: DispatcherConfig, registry: Any) -> tuple[PlaneState, _Counted]:
    """Parse every checked-out `TODO.md` and classify its open items.

    Only OPEN, non-tombstoned items count: a closed item carries no obligation, and
    including it would make the backlog look larger the more work the fleet finished.
    """
    counted = _Counted({}, {}, {}, {})
    manifest = _manifest_path(config)
    if manifest is None:
        return (
            PlaneState(
                plane="todo",
                state="unavailable",
                detail="workspace-manifest.toml not found; repo identity unresolvable",
            ),
            counted,
        )
    from plan_fields import (
        RepoInput,
        apply_registry,
        checkout_map,
        manifest_index,
        parse_fleet,
    )

    index = manifest_index(manifest)
    inputs: list[RepoInput] = []
    for name, root in sorted(checkout_map(config.roots[0], index).items()):
        todo = root / "TODO.md"
        if todo.is_file():
            inputs.append(RepoInput(name, todo.read_text(encoding="utf-8")))
    if not inputs:
        return (
            PlaneState(
                plane="todo", state="unavailable", detail="no TODO.md checked out"
            ),
            counted,
        )
    doc = parse_fleet(inputs, index)
    apply_registry(doc, registry)  # downgrades unknown/retired epics in place
    total = 0
    for node in doc["nodes"]:
        if node["declared_status"] != "open" or node["tombstone"]:
            continue
        total += 1
        key = node["epic"] if node["epic_classification"] == "tagged" else UNCLASSIFIED
        counted.per_plane.setdefault(key, {}).setdefault("todo", 0)
        counted.per_plane[key]["todo"] += 1
        if node["defect"]:
            counted.per_defect.setdefault(key, {}).setdefault(node["defect"], 0)
            counted.per_defect[key][node["defect"]] += 1
        counted.artifacts.setdefault(key, []).append(
            EpicArtifact(
                plane="todo",
                repo=node["repo"],
                ref=node["node_id"],
                title=node["title"],
                defect=node["defect"],
            )
        )
    return PlaneState(plane="todo", state="read", count=total), counted


def _github_planes(
    snapshots: list[WorkspaceSnapshotV1],
) -> tuple[list[PlaneState], _Counted]:
    """Issues and pull requests, from published snapshots only.

    Dispatcher never calls the GitHub API (ADR-ECO-004 D1), so these planes exist only
    as far as github-checker publishes them. A producer still on snapshot v1 publishes
    no epic classification at all: that is `unavailable`, not zero, and the distinction
    is the whole reason the classification is four-state.
    """
    counted = _Counted({}, {}, {}, {})
    if not snapshots:
        return (
            [
                PlaneState(plane=p, state="unavailable", detail="no published snapshot")
                for p in ("issues", "pull_requests")
            ],
            counted,
        )
    v1_hosts = [s.host for s in snapshots if not carries_epic_axis(s)]
    usable = [s for s in snapshots if carries_epic_axis(s)]
    if not usable:
        detail = (
            "producer publishes snapshot v1 (no epic classification): "
            + ", ".join(sorted(v1_hosts))
        )
        return (
            [
                PlaneState(plane=p, state="unavailable", detail=detail)
                for p in ("issues", "pull_requests")
            ],
            counted,
        )

    totals = {"issues": 0, "pull_requests": 0}
    for snapshot in usable:
        for repo in snapshot.repos:
            github = repo.github or {}
            for plane, field in (("issues", "issues"), ("pull_requests", "pulls")):
                for item in github.get(field) or []:
                    classification = item.get("epic") or {}
                    state = classification.get("classification")
                    if state == "unavailable":
                        # the body was never retrieved: counting it as unmarked would
                        # invent a fact about an artifact nobody read
                        continue
                    totals[plane] += 1
                    tagged_epic = classification.get("epic")
                    # `tagged` without an epic string cannot happen in a conforming
                    # producer, but a consumer that trusts that would key the whole
                    # aggregate on None the day it does.
                    key = (
                        tagged_epic
                        if state == "tagged" and isinstance(tagged_epic, str)
                        else UNCLASSIFIED
                    )
                    counted.per_plane.setdefault(key, {}).setdefault(plane, 0)
                    counted.per_plane[key][plane] += 1
                    defect = classification.get("defect")
                    if defect:
                        counted.per_defect.setdefault(key, {}).setdefault(defect, 0)
                        counted.per_defect[key][defect] += 1
                    counted.artifacts.setdefault(key, []).append(
                        EpicArtifact(
                            plane=plane,
                            repo=repo.dir,
                            ref=f"{repo.dir}#{item.get('number')}",
                            title=item.get("title"),
                            defect=defect,
                        )
                    )
            for merged in (github.get("merged") or {}).get("prs") or []:
                classification = merged.get("epic") or {}
                if classification.get("classification") != "tagged":
                    continue
                key = classification["epic"]
                stamp = merged.get("merged_at")
                if stamp and stamp > counted.activity.get(key, ""):
                    counted.activity[key] = stamp

    planes = [PlaneState(plane=p, state="read", count=totals[p]) for p in totals]
    if v1_hosts:
        for plane in planes:
            plane.detail = (
                "partial: hosts still on snapshot v1 contribute nothing — "
                + ", ".join(sorted(v1_hosts))
            )
    return planes, counted


def _merge(*counted: _Counted) -> _Counted:
    per_plane: dict[str, dict[str, int]] = {}
    per_defect: dict[str, dict[str, int]] = {}
    artifacts: dict[str, list[EpicArtifact]] = {}
    activity: dict[str, str] = {}
    for c in counted:
        for key, planes in c.per_plane.items():
            for plane, n in planes.items():
                per_plane.setdefault(key, {}).setdefault(plane, 0)
                per_plane[key][plane] += n
        for key, defects in c.per_defect.items():
            for defect, n in defects.items():
                per_defect.setdefault(key, {}).setdefault(defect, 0)
                per_defect[key][defect] += n
        for key, items in c.artifacts.items():
            artifacts.setdefault(key, []).extend(items)
        for key, stamp in c.activity.items():
            if stamp > activity.get(key, ""):
                activity[key] = stamp
    return _Counted(per_plane, per_defect, artifacts, activity)


def build_view(
    config: DispatcherConfig,
    snapshots: list[WorkspaceSnapshotV1] | None = None,
    *,
    kind: str | None = None,
    generated_at: str | None = None,
) -> EpicsView:
    """Assemble the epics view over every plane this dispatcher can honestly read."""
    from plan_fields import load_registry

    path = registry_path(config)
    if path is None:
        return EpicsView(
            generated_at=generated_at,
            registry_ok=False,
            registry_diagnostics=[
                {
                    "code": "EP-REG-POLICY-INVALID",
                    "severity": "error",
                    "message": "epics.toml not found in any workspace root",
                    "subject_key": None,
                }
            ],
            planes=[
                PlaneState(plane=p, state="unavailable", detail="no registry")
                for p in _PLANES
            ],
        )

    registry = load_registry(path)
    todo_plane, todo_counted = _todo_plane(config, registry)
    gh_planes, gh_counted = _github_planes(snapshots or [])
    counted = _merge(todo_counted, gh_counted)
    planes = [todo_plane, *gh_planes]
    plane_state = {p.plane: p for p in planes}

    rows: list[EpicRow] = []
    for epic_id, entry in sorted(registry.epics.items()):
        program = epic_id.split(".", 1)[0]
        if kind and registry.programs.get(program, {}).get("kind") != kind:
            continue
        counts = counted.per_plane.get(epic_id, {})
        rows.append(
            EpicRow(
                id=epic_id,
                program=program,
                kind=registry.programs.get(program, {}).get("kind"),
                title=entry.get("title"),
                status=entry.get("status"),
                moved_to=entry.get("moved_to"),
                planes=[
                    PlaneState(
                        plane=p,
                        state=plane_state[p].state,
                        count=counts.get(p, 0),
                        detail=plane_state[p].detail,
                    )
                    for p in _PLANES
                ],
                defects=dict(sorted(counted.per_defect.get(epic_id, {}).items())),
                last_activity_at=counted.activity.get(epic_id),
                activity_sources=["merged_pr"] if epic_id in counted.activity else [],
            )
        )

    # The bucket is ALWAYS present, and never filtered out by `kind`: an unmarked
    # artifact belongs to no program, so hiding it behind a program filter would show
    # a partial aggregate with nothing saying it is partial. It is emitted even at
    # zero — a row that disappears when it reaches zero looks identical to a row that
    # was never computed.
    bucket_counts = counted.per_plane.get(UNCLASSIFIED, {})
    rows.append(
        EpicRow(
            type="classification_bucket",
            id=UNCLASSIFIED,
            title="Без эпика",
            planes=[
                PlaneState(
                    plane=p,
                    state=plane_state[p].state,
                    count=bucket_counts.get(p, 0),
                    detail=plane_state[p].detail,
                )
                for p in _PLANES
            ],
            defects=dict(sorted(counted.per_defect.get(UNCLASSIFIED, {}).items())),
        )
    )

    defect_rows: dict[str, DefectRow] = {}
    for key, defects in counted.per_defect.items():
        for defect, n in defects.items():
            row = defect_rows.setdefault(
                defect,
                DefectRow(
                    defect=defect,
                    title=registry.defect_classes.get(defect, {}).get("title"),
                ),
            )
            row.count += n
            row.by_epic[key] = row.by_epic.get(key, 0) + n

    return EpicsView(
        generated_at=generated_at,
        registry_path=str(path),
        registry_ok=not any(d["severity"] == "error" for d in registry.diagnostics),
        registry_diagnostics=[dict(d) for d in registry.diagnostics],
        programs={
            k: {"title": v.get("title", k), "kind": v.get("kind", "?")}
            for k, v in sorted(registry.programs.items())
        },
        planes=planes,
        rows=rows,
        defects=sorted(defect_rows.values(), key=lambda r: (-r.count, r.defect)),
    )


def build_detail(
    config: DispatcherConfig,
    epic_id: str,
    snapshots: list[WorkspaceSnapshotV1] | None = None,
) -> EpicDetail | None:
    """One epic's row plus every artifact behind it, across planes."""
    view = build_view(config, snapshots)
    row = next((r for r in view.rows if r.id == epic_id), None)
    if row is None:
        return None
    from plan_fields import load_registry

    path = registry_path(config)
    registry = load_registry(path) if path else None
    todo_plane, todo_counted = (
        _todo_plane(config, registry) if registry else (None, _Counted({}, {}, {}, {}))
    )
    _, gh_counted = _github_planes(snapshots or [])
    counted = _merge(todo_counted, gh_counted)
    artifacts = sorted(
        counted.artifacts.get(epic_id, []), key=lambda a: (a.plane, a.repo, a.ref)
    )
    return EpicDetail(row=row, artifacts=artifacts)
