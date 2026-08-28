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
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.snapshot_contract import (
    EpicClassificationV2,
    Snapshot,
    WorkspaceSnapshotV2,
    carries_epic_axis,
)
from dispatcher.core.sync import STALE_AFTER_SECONDS

UNCLASSIFIED = "unclassified"
_PLANES = ("todo", "issues", "pull_requests")

#: A plane is `read` only when every producer that should have contributed did, and
#: recently. `partial` is the third state the first cut of this module lacked: it
#: reported `read` for a fleet it had only half-observed and put the reason in a
#: human-readable `detail`, which no consumer branches on. State is the machine-readable
#: field — web, MCP and Robin all decide by it — so an incompleteness that lives only in
#: prose is an incompleteness nobody downstream can see.
PLANE_STATES = ("read", "partial", "unavailable")


class PlaneState(BaseModel):
    """One plane's contribution to one row, with its completeness attached."""

    plane: str
    state: str  # read | partial | unavailable
    count: int = 0
    detail: str | None = None

    @property
    def is_complete(self) -> bool:
        """Whether this count may be read as the whole truth for its plane."""
        return self.state == "read"


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
        """Sum over COMPLETE planes only — a partial plane's count is a lower bound."""
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
    #: Per-ARTIFACT findings (EP-UNKNOWN, EP-MOVED, EP-DEFECT-UNKNOWN), deliberately
    #: separate from the registry's own: a typo in one issue's trailer says nothing
    #: about the registry, and folding it into `registry_diagnostics` would flip
    #: `registry_ok` to false and send an operator to fix the wrong file.
    classification_diagnostics: list[dict[str, Any]] = Field(default_factory=list)
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


def _workspace_root(manifest: Path) -> Path:
    """The root the manifest was found under — NOT `roots[0]`.

    `DispatcherConfig.roots` is a list, and the manifest may live under any of them.
    Scanning the first root regardless would silently read a different workspace: repos
    would resolve to nothing, their TODO items would vanish from the counts, and the
    plane would still report `read`. A wrong number that calls itself complete is worse
    than an `unavailable`.
    """
    return manifest.parent.parent


def _todo_plane(
    config: DispatcherConfig, registry: Any
) -> tuple[PlaneState, _Counted, list[dict[str, Any]]]:
    """Parse every checked-out `TODO.md` and classify its open items.

    Only OPEN, non-tombstoned items count: a closed item carries no obligation, and
    including it would make the backlog look larger the more work the fleet finished.

    The registry findings travel OUT with the counts. They were being computed and
    dropped on the floor: `apply_registry` returned EP-UNKNOWN for a typo'd tag, the
    item was downgraded into the bucket, and the reason never reached a surface — so a
    misspelled epic looked exactly like an unmarked one, and the fix for it was invisible.
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
            [],
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
    for name, root in sorted(checkout_map(_workspace_root(manifest), index).items()):
        todo = root / "TODO.md"
        if todo.is_file():
            inputs.append(RepoInput(name, todo.read_text(encoding="utf-8")))
    if not inputs:
        return (
            PlaneState(
                plane="todo", state="unavailable", detail="no TODO.md checked out"
            ),
            counted,
            [],
        )
    doc = parse_fleet(inputs, index)
    findings = apply_registry(
        doc, registry
    )  # downgrades unknown/retired epics in place
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
    return PlaneState(plane="todo", state="read", count=total), counted, list(findings)


def _repo_key(repo: Any) -> str:
    """Fleet-wide identity of one repository, stable across producers.

    The REMOTE is the identity; the directory name is one host's spelling of it. Two
    machines of the same owner check the same repo out under different paths (and, on a
    case-insensitive filesystem, under different casings), so keying on `dir` would file
    one repository under two names and count everything inside it twice.
    """
    return repo.remote or repo.dir


def _classify(
    classification: EpicClassificationV2, registry: Any, subject: str
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve one artifact's tag to an aggregate key, plus the finding it produced.

    Returns ``(None, ...)`` for an artifact that must not be counted at all — an
    unretrieved body. Everything else lands somewhere visible: an epic the registry
    knows, or the bucket. Nothing may fall between them, and that is what broke: the
    GitHub plane keyed straight off the producer's string without asking the registry,
    so a typo'd epic became a row that does not exist, vanished from the bucket too,
    and still counted into the plane total. The rows stopped summing to the total, in
    an aggregate whose entire promise is that they do.
    """
    state = classification.classification
    if state == "unavailable":
        # the body was never retrieved: counting it as unmarked would invent a fact
        # about an artifact nobody read
        return None, None
    epic = classification.epic
    if state != "tagged" or not isinstance(epic, str):
        return UNCLASSIFIED, None
    if registry is None:
        # no registry reachable: the typo guard is absent, and saying so beats
        # pretending every tag checked out
        return epic, None
    final, code = registry.resolve(epic)
    if code is None:
        return epic, None
    # Same rule as the TODO plane's `apply_registry`: a value the registry does not
    # recognise cannot be counted into any stream, so it goes to the bucket and the
    # reason is NAMED. Both planes must agree here — two planes disagreeing about what
    # a retired id means would split one stream across two rows in half the surfaces.
    message = (
        "epic is absent from the registry"
        if code == "EP-UNKNOWN"
        else f"epic is retired; the registry moves it to {final}"
    )
    return UNCLASSIFIED, {
        "code": code,
        "severity": "error",
        "message": message,
        "subject_uri": subject,
        "raw": epic,
    }


def _github_planes(
    snapshots: list[Snapshot],
    registry: Any = None,
    load_errors: list[tuple[str, str]] | None = None,
    now: datetime | None = None,
    source_warning: str | None = None,
) -> tuple[list[PlaneState], _Counted, list[dict[str, Any]]]:
    """Issues and pull requests, from published snapshots only.

    Dispatcher never calls the GitHub API (ADR-ECO-004 D1), so these planes exist only
    as far as github-checker publishes them. A producer still on snapshot v1 publishes
    no epic classification at all: that is `unavailable`, not zero, and the distinction
    is the whole reason the classification is four-state.

    Three things degrade a plane below `read`, and each of them names itself: a producer
    on v1, a producer whose snapshot has gone stale, and nothing published at all.
    """
    counted = _Counted({}, {}, {}, {})
    findings: list[dict[str, Any]] = []
    if not snapshots:
        # "nothing was published" and "what was published could not be read" are
        # different facts about the fleet, and an operator acts on them differently.
        # Collapsing both into "no published snapshot" is the same silent-degradation
        # the four-state classification exists to prevent, one level up.
        parts: list[str] = []
        if source_warning:
            parts.append(f"snapshot source unavailable: {source_warning}")
        if load_errors:
            parts.append(
                "published snapshots unreadable: "
                + "; ".join(f"{host}: {reason}" for host, reason in sorted(load_errors))
            )
        detail = "; ".join(parts) if parts else "no published snapshot"
        return (
            [
                PlaneState(plane=p, state="unavailable", detail=detail)
                for p in ("issues", "pull_requests")
            ],
            counted,
            findings,
        )
    v1_hosts = [s.host for s in snapshots if not carries_epic_axis(s)]
    usable = [s for s in snapshots if isinstance(s, WorkspaceSnapshotV2)]
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
            findings,
        )

    stale_hosts = sorted(
        s.host for s in usable if s.age_seconds(now) > STALE_AFTER_SECONDS
    )

    # Freshest producer first, so that when two hosts saw the same artifact the value
    # kept is the more recent observation rather than whichever file sorted first.
    ordered = sorted(usable, key=lambda s: s.generated_at, reverse=True)
    seen: set[tuple[str, str, int]] = set()
    seen_merged: set[tuple[str, int]] = set()
    totals = {"issues": 0, "pull_requests": 0}
    # A gap is per REPOSITORY and per PLANE, and it is only a gap if NO producer
    # observed it: one host failing on a repo another host read fine is not a hole in
    # the fleet's picture. Collapsing that would make everything permanently
    # `partial`, and a completeness marker that is always on marks nothing.
    observed: dict[str, set[str]] = {p: set() for p in totals}
    gaps: dict[str, dict[str, str]] = {p: {} for p in totals}

    # A host-wide failure with NO repositories to pin it on cannot be closed by
    # another producer, so it is tracked apart from the per-repo gaps: when the repo
    # list is empty we do not even know what this host was supposed to cover.
    host_gaps: list[str] = []

    def _gap(planes_affected: tuple[str, ...], repo_key: str, why: str) -> None:
        for plane in planes_affected:
            gaps[plane].setdefault(repo_key, why)

    for snapshot in ordered:
        if snapshot.gh_error:
            # The producer is telling us outright that it never reached GitHub. This
            # field was read by nobody, and then — once it was — the check sat INSIDE
            # the repo loop, so an empty `repos` silently cancelled it: the loop never
            # ran, the disclaimer was dropped, and the plane went back to a confident
            # `read 0`. `repos: []` is explicitly allowed by the pin, and a guard that
            # a legal payload can skip is not a guard.
            why = f"GitHub not queried on {snapshot.host} ({snapshot.gh_error})"
            if snapshot.repos:
                # We know which repositories this host would have covered, so another
                # producer that read them closes the gap.
                for repo in snapshot.repos:
                    _gap(
                        ("issues", "pull_requests"),
                        _repo_key(repo),
                        f"{_repo_key(repo)}: {why}",
                    )
            else:
                host_gaps.append(why)
            continue
        for repo in snapshot.repos:
            key_repo = _repo_key(repo)
            github = repo.github
            if github is None:
                # No remote means there is genuinely no GitHub side to observe; a repo
                # that HAS a remote and no github block was simply not looked at.
                if repo.remote is not None:
                    _gap(
                        ("issues", "pull_requests"),
                        key_repo,
                        f"{key_repo}: no GitHub state published by {snapshot.host}",
                    )
                continue
            if github.error:
                _gap(
                    ("issues", "pull_requests"),
                    key_repo,
                    f"{key_repo}: {github.error} (on {snapshot.host})",
                )
                continue
            # `pulls` is non-nullable in the pin and defaults to empty, so its absence
            # really does mean "none open". `issues` is nullable precisely because the
            # producer needs a way to say "I did not retrieve this list" — and
            # `issues or []` erased exactly that distinction.
            observed["pull_requests"].add(key_repo)
            if github.issues is None:
                _gap(
                    ("issues",),
                    key_repo,
                    f"{key_repo}: issue list not retrieved on {snapshot.host}",
                )
            else:
                observed["issues"].add(key_repo)
            for plane, items in (
                ("issues", github.issues or []),
                ("pull_requests", github.pulls),
            ):
                for item in items:
                    # One artifact observed by two producers is ONE artifact. Two
                    # machines of the same owner is a normal configuration, not a
                    # fault, and without this the whole overlap counted twice — the
                    # backlog grew because the owner bought a laptop.
                    identity = (plane, key_repo, item.number)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    subject = f"{key_repo}#{item.number}"
                    key, finding = _classify(item.epic, registry, subject)
                    if finding is not None:
                        findings.append(finding)
                    if key is None:
                        continue
                    totals[plane] += 1
                    counted.per_plane.setdefault(key, {}).setdefault(plane, 0)
                    counted.per_plane[key][plane] += 1
                    defect = item.epic.defect
                    if defect:
                        counted.per_defect.setdefault(key, {}).setdefault(defect, 0)
                        counted.per_defect[key][defect] += 1
                    counted.artifacts.setdefault(key, []).append(
                        EpicArtifact(
                            plane=plane,
                            repo=repo.dir,
                            ref=f"{repo.dir}#{item.number}",
                            title=item.title,
                            defect=defect,
                        )
                    )
            merged = github.merged
            for pr in merged.prs if merged else []:
                if (key_repo, pr.number) in seen_merged:
                    continue
                seen_merged.add((key_repo, pr.number))
                if pr.epic.classification != "tagged" or pr.epic.epic is None:
                    continue
                final, code = (
                    registry.resolve(pr.epic.epic) if registry else (pr.epic.epic, None)
                )
                if code is not None:
                    continue  # an unknown stream has no row to carry an activity date
                if final is not None and pr.merged_at > counted.activity.get(final, ""):
                    counted.activity[final] = pr.merged_at

    reasons = []
    if load_errors:
        # A host whose snapshot would not parse is an UNOBSERVED host, exactly like a
        # host still on v1. These errors used to count only when nothing at all
        # parsed — so one readable snapshot was enough for the plane to call itself
        # complete while an unknown number of others contributed nothing.
        reasons.append(
            "published snapshots unreadable: "
            + "; ".join(f"{host}: {reason}" for host, reason in sorted(load_errors))
        )
    if v1_hosts:
        reasons.append(
            "hosts still on snapshot v1 contribute nothing: "
            + ", ".join(sorted(v1_hosts))
        )
    if stale_hosts:
        reasons.append(
            f"stale beyond {int(STALE_AFTER_SECONDS)}s, counts may lag: "
            + ", ".join(stale_hosts)
        )
    planes = []
    for plane in totals:
        unobserved = [
            why
            for key, why in sorted(gaps[plane].items())
            if key not in observed[plane]
        ]
        why = [*reasons, *host_gaps, *unobserved]
        planes.append(
            PlaneState(
                plane=plane,
                state="partial" if why else "read",
                count=totals[plane],
                detail="; ".join(why) or None,
            )
        )
    return planes, counted, findings


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
    snapshots: list[Snapshot] | None = None,
    *,
    kind: str | None = None,
    generated_at: str | None = None,
    snapshot_errors: list[tuple[str, str]] | None = None,
    now: datetime | None = None,
    snapshot_source_warning: str | None = None,
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
    todo_plane, todo_counted, todo_findings = _todo_plane(config, registry)
    gh_planes, gh_counted, gh_findings = _github_planes(
        snapshots or [],
        registry,
        snapshot_errors,
        now,
        source_warning=snapshot_source_warning,
    )
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
        classification_diagnostics=sorted(
            ({**d} for d in (*todo_findings, *gh_findings)),
            key=lambda d: (d["code"], str(d.get("subject_uri") or "")),
        ),
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
    snapshots: list[Snapshot] | None = None,
    *,
    snapshot_source_warning: str | None = None,
) -> EpicDetail | None:
    """One epic's row plus every artifact behind it, across planes."""
    view = build_view(
        config, snapshots, snapshot_source_warning=snapshot_source_warning
    )
    row = next((r for r in view.rows if r.id == epic_id), None)
    if row is None:
        return None
    from plan_fields import load_registry

    path = registry_path(config)
    registry = load_registry(path) if path else None
    todo_counted = (
        _todo_plane(config, registry)[1] if registry else _Counted({}, {}, {}, {})
    )
    _, gh_counted, _ = _github_planes(
        snapshots or [], registry, source_warning=snapshot_source_warning
    )
    counted = _merge(todo_counted, gh_counted)
    artifacts = sorted(
        counted.artifacts.get(epic_id, []), key=lambda a: (a.plane, a.repo, a.ref)
    )
    return EpicDetail(row=row, artifacts=artifacts)
