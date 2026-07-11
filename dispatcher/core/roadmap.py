"""Roadmap read-model: computed status over ecosystem evidence.

Roadmap intent lives in human-authored YAML (canonical location:
`prograph-vault/authored/roadmaps/*.yaml`); dispatcher only renders the
truth it can compute. Status is never a manual checkbox: it is derived
from a small closed set of typed evidence rules. An item whose evidence
cannot be expressed with these rules stays `unknown` — prose
`expected_evidence` entries document intent and are never machine-checked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from dispatcher.core.collectors.base import SourceReadError, read_rows
from dispatcher.core.contracts import check_contracts
from dispatcher.core.correlation import build_work_items
from dispatcher.core.models import ProjectSnapshot

_DONE = ("implemented", "verified")
_RULE_KINDS = ("implementation", "verification")


class EvidenceResult(BaseModel):
    """Outcome of one typed evidence rule for one roadmap item."""

    rule: str
    kind: str  # implementation | verification
    passed: bool
    detail: str


class RoadmapItemView(BaseModel):
    """One roadmap item with its computed status."""

    id: str
    title: str
    phase: str | None = None
    owner_project: str | None = None
    target_contract: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    expected_evidence: list[str] = Field(default_factory=list)
    computed_status: str
    evidence: list[EvidenceResult] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    source: str


class RoadmapResponse(BaseModel):
    """Response of GET /api/roadmap."""

    roadmaps: list[str]
    items: list[RoadmapItemView]
    warnings: list[str] = Field(default_factory=list)


def default_roadmap_dirs(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    """Canonical roadmap location relative to each configured root."""
    return tuple(root / "prograph-vault" / "authored" / "roadmaps" for root in roots)


def build_roadmap(
    dirs: tuple[Path, ...], snapshots: list[ProjectSnapshot]
) -> RoadmapResponse:
    """Load roadmap YAML files and compute per-item status from evidence."""
    raw_items, roadmaps, warnings = _load_yaml_items(dirs)
    ctx = _EvidenceContext(snapshots)
    views: dict[str, RoadmapItemView] = {}
    for raw, source in raw_items:
        view = _evaluate_item(raw, source, ctx)
        if view.id in views:
            warnings.append(f"duplicate roadmap item id: {view.id} ({source})")
            continue
        views[view.id] = view
    _apply_blocked(views)
    items = sorted(views.values(), key=lambda v: (v.phase or "", v.id))
    return RoadmapResponse(roadmaps=roadmaps, items=items, warnings=warnings)


_SELF_ROOT = Path(__file__).resolve().parents[2]


class _EvidenceContext:
    """Lazily computed shared inputs for rule evaluation.

    `dispatcher` is not one of its own collected projects, but roadmap
    items about the dashboard itself need to attest dispatcher files, so
    it resolves to this package's own repo root.
    """

    def __init__(self, snapshots: list[ProjectSnapshot]) -> None:
        self.snapshots = {s.name: s for s in snapshots}
        self._chains: dict[str, int] | None = None
        self._contracts: dict[str, bool | None] | None = None

    def chain_links(self, work_item_id: str) -> int:
        if self._chains is None:
            result = build_work_items(list(self.snapshots.values()))
            self._chains = {c.work_item_id: len(c.links) for c in result.items}
        return self._chains.get(work_item_id, 0)

    def contract_in_sync(self, name: str) -> bool | None:
        if self._contracts is None:
            projects = {
                s.name: Path(s.path)
                for s in self.snapshots.values()
                if s.detected and s.path
            }
            self._contracts = {c.name: c.in_sync for c in check_contracts(projects)}
        return self._contracts.get(name)

    def project_path(self, name: str) -> Path | None:
        if name == "dispatcher":
            return _SELF_ROOT
        snap = self.snapshots.get(name)
        if snap is None or not snap.detected or not snap.path:
            return None
        return Path(snap.path)


def _load_yaml_items(
    dirs: tuple[Path, ...],
) -> tuple[list[tuple[dict, str]], list[str], list[str]]:
    items: list[tuple[dict, str]] = []
    roadmaps: list[str] = []
    warnings: list[str] = []
    seen_any = False
    for d in dirs:
        if not d.is_dir():
            continue
        seen_any = True
        for path in sorted(d.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text())
            except (OSError, yaml.YAMLError) as err:
                warnings.append(f"cannot read roadmap {path.name}: {err}")
                continue
            if not isinstance(data, dict):
                warnings.append(f"roadmap {path.name}: top level must be a mapping")
                continue
            name = data.get("roadmap") or path.stem
            if str(name) not in roadmaps:
                roadmaps.append(str(name))
            for raw in data.get("items") or []:
                if isinstance(raw, dict):
                    items.append((raw, path.name))
                else:
                    warnings.append(f"roadmap {path.name}: non-mapping item skipped")
    if not seen_any:
        warnings.append("no roadmap directory found")
    return items, roadmaps, warnings


def _evaluate_item(raw: dict, source: str, ctx: _EvidenceContext) -> RoadmapItemView:
    rules = [r for r in raw.get("evidence_rules") or [] if isinstance(r, dict)]
    evidence = [_run_rule(rule, ctx) for rule in rules]
    return RoadmapItemView(
        id=str(raw.get("id", "?")),
        title=str(raw.get("title", "")),
        phase=_opt_str(raw.get("phase")),
        owner_project=_opt_str(raw.get("owner_project")),
        target_contract=_opt_str(raw.get("target_contract")),
        depends_on=[str(d) for d in raw.get("depends_on") or []],
        expected_evidence=[str(e) for e in raw.get("expected_evidence") or []],
        computed_status=_status_from_evidence(evidence),
        evidence=evidence,
        source=source,
    )


def _status_from_evidence(evidence: list[EvidenceResult]) -> str:
    """MVP status ladder: unknown / planned / implemented / verified.

    `blocked` is applied afterwards from dependencies; `drift` arrives
    post-MVP as a projection of the contracts checker.
    """
    if not evidence:
        return "unknown"
    impl = [e for e in evidence if e.kind == "implementation"]
    verif = [e for e in evidence if e.kind == "verification"]
    if not impl or not all(e.passed for e in impl):
        return "planned"
    if verif and all(e.passed for e in verif):
        return "verified"
    return "implemented"


def _apply_blocked(views: dict[str, RoadmapItemView]) -> None:
    """Downgrade planned items whose dependencies are not implemented+.

    Evidence wins over dependencies: an item that is already implemented
    or verified is never marked blocked.
    """
    for view in views.values():
        if view.computed_status != "planned":
            continue
        blockers = [
            dep
            for dep in view.depends_on
            if dep not in views or views[dep].computed_status not in _DONE
        ]
        if blockers:
            view.computed_status = "blocked"
            view.blockers = blockers


def _run_rule(rule: dict, ctx: _EvidenceContext) -> EvidenceResult:
    name = str(rule.get("rule", ""))
    kind = str(rule.get("kind", "implementation"))
    if kind not in _RULE_KINDS:
        kind = "implementation"
    handler = _RULES.get(name)
    if handler is None:
        return EvidenceResult(
            rule=name or "(missing)",
            kind=kind,
            passed=False,
            detail=f"unknown rule: {name!r}",
        )
    try:
        passed, detail = handler(rule, ctx)
    except SourceReadError as err:
        passed, detail = False, str(err)
    except Exception as err:  # noqa: BLE001 — YAML is user-authored;
        # a malformed rule must degrade to a failed check, not take
        # down /api/roadmap.
        passed, detail = False, f"rule error: {err}"
    return EvidenceResult(rule=name, kind=kind, passed=passed, detail=detail)


def _rule_project_detected(rule: dict, ctx: _EvidenceContext) -> tuple[bool, str]:
    project = str(rule.get("project", ""))
    if ctx.project_path(project) is not None:
        return True, f"project {project} detected"
    return False, f"project {project} not detected"


def _safe_join(root: Path, rel: str) -> Path | None:
    """Resolve `root/rel`, rejecting absolute paths and `..` escapes.

    Roadmap YAML is human-authored canon, but its paths are rendered
    through the API — defense in depth against probing the host
    filesystem outside the project root.
    """
    candidate = Path(rel)
    if candidate.is_absolute():
        return None
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root.resolve()):
        return None
    return resolved


def _rule_file_exists(rule: dict, ctx: _EvidenceContext) -> tuple[bool, str]:
    project = str(rule.get("project", ""))
    rel = str(rule.get("path", ""))
    root = ctx.project_path(project)
    if root is None:
        return False, f"project {project} not detected"
    target = _safe_join(root, rel)
    if target is None:
        return False, f"path escapes project root: {rel}"
    if target.exists():
        return True, f"{project}/{rel} exists"
    return False, f"{project}/{rel} missing"


def _rule_sqlite_has_row(rule: dict, ctx: _EvidenceContext) -> tuple[bool, str]:
    project = str(rule.get("project", ""))
    rel = str(rule.get("db", ""))
    query = str(rule.get("query", ""))
    root = ctx.project_path(project)
    if root is None:
        return False, f"project {project} not detected"
    db = _safe_join(root, rel)
    if db is None:
        return False, f"db path escapes project root: {rel}"
    # EXISTS caps the result at one row regardless of the inner query.
    rows = read_rows(db, f"SELECT EXISTS ({query.rstrip(';')}) AS present")
    if rows and rows[0]["present"]:
        return True, f"{rel}: row found"
    return False, f"{rel}: no rows"


def _rule_contract_in_sync(rule: dict, ctx: _EvidenceContext) -> tuple[bool, str]:
    name = str(rule.get("name", ""))
    state = ctx.contract_in_sync(name)
    if state is True:
        return True, f"contract {name} in sync"
    if state is None:
        return False, f"contract {name} not comparable"
    return False, f"contract {name} out of sync"


def _rule_work_item_chain(rule: dict, ctx: _EvidenceContext) -> tuple[bool, str]:
    work_item_id = str(rule.get("work_item_id", ""))
    min_links = int(rule.get("min_links", 1))
    links = ctx.chain_links(work_item_id)
    if links >= min_links:
        return True, f"chain {work_item_id}: {links} link(s)"
    return False, f"chain {work_item_id}: {links} link(s), need {min_links}"


_RULES = {
    "project_detected": _rule_project_detected,
    "file_exists": _rule_file_exists,
    "sqlite_has_row": _rule_sqlite_has_row,
    "contract_in_sync": _rule_contract_in_sync,
    "work_item_chain": _rule_work_item_chain,
}


def _opt_str(value: Any) -> str | None:
    return None if value is None else str(value)
