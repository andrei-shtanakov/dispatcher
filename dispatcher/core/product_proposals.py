"""Product-proposal gate_waiting collector: classify impresario bundles.

Inbox #129 phase 1 (spec: docs/superpowers/specs/
2026-08-12-product-proposal-gate-waiting-design.md). Reads proposal bundles
(`proposal.yaml` + `decisions/*.yaml`) out of the impresario mirror and says
which product decisions are waiting for a human — Gate A (`qg5_business`,
business_owner) and Gate B (`qg5_committee`, committee_chair).

Constraints this module lives under:

- Classification only (ARCH-C3/D1): impresario is never imported, its CLI is
  never executed, and no governance model is built here — status + decision
  records are read and rendered.
- CON-03: no sibling-repo path is resolved; the mirror root is an argument.
- Fail-closed: an unreadable/invalid proposal or decision is never rendered
  as «nothing waits». Every found `proposal.yaml` yields a bundle row; waits
  are computed only for `ok` bundles, and `waits: []` on a non-ok bundle
  means «suppressed». Duplicate YAML keys are rejected (plain safe_load
  keeps the last value silently).
- Version-matched activeness: an approve extinguishes the current wait only
  when it targets the proposal's CURRENT version and is not superseded.
  After a recycle the old approve is history, not permission; an approve
  recorded before the status update already extinguishes the wait.
- Read-only: nothing under the mirror is ever created or modified.
"""

from __future__ import annotations

import functools
import json
import os
from collections import Counter
from pathlib import Path
from typing import Literal

import jsonschema
import yaml
from pydantic import BaseModel, Field

_CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
_PROPOSAL_SCHEMA = _CONTRACTS / "impresario-product-proposal" / "v1" / "schema.json"
_DECISION_SCHEMA = _CONTRACTS / "impresario-gate-decision" / "v1" / "schema.json"

ANCHOR_FILES = (
    "contracts/product-proposal/v1/schema.json",
    "docs/semantics.md",
)

GateId = Literal["qg5_business", "qg5_committee"]
BundleState = Literal["ok", "unreadable", "unknown", "conflict"]

_STATUS_GATE: dict[str, GateId] = {
    "ready_for_business": "qg5_business",
    "business_approved": "qg5_committee",
}
_GATE_LABEL = {"qg5_business": "Gate A", "qg5_committee": "Gate B"}
_GATE_AUTHORITY = {
    "qg5_business": "business_owner",
    "qg5_committee": "committee_chair",
}
# Diagnostics whose presence makes the PROPOSAL untrusted (state unreadable);
# any other bundle-level diagnostic is decision-grade (state unknown).
_UNREADABLE_CODES = {
    "proposal-unreadable",
    "proposal-schema-invalid",
    "proposal-path-escape",
}


class Diagnostic(BaseModel):
    """One structured problem; `code` is the stable API contract."""

    code: str
    message: str
    path: str | None = None


class GateWait(BaseModel):
    """One «a human is being waited for» record."""

    proposal_id: str
    gate_id: GateId
    gate_label: str
    authority: str
    artifact_ref: str
    bundle_path: str
    version: int
    # proposal.updated_at — when the proposal last changed, NOT a proven
    # wait-start time; UI labels it «Proposal updated».
    proposal_updated_at: str


class ProposalBundle(BaseModel):
    """Every discovered bundle, lossless: non-ok rows keep ALL diagnostics."""

    path: str
    state: BundleState
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    proposal_id: str | None = None
    status: str | None = None
    version: int | None = None
    updated_at: str | None = None
    # Computed ONLY for state == "ok". On any other state an empty list
    # means «suppressed», never «nothing waits».
    waits: list[GateWait] = Field(default_factory=list)


class ProductProposalsReport(BaseModel):
    """The read model of one scan of the impresario mirror."""

    mirror_path: str
    bundles: list[ProposalBundle] = Field(default_factory=list)
    waits: list[GateWait] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    # Any non-ok bundle or report-level diagnostic. A plain GateWait does
    # NOT raise attention — waiting is expected business work.
    attention: bool = False


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (fail-closed)."""


def _mapping_no_duplicates(
    loader: _StrictLoader, node: yaml.MappingNode
) -> dict[object, object]:
    seen: set[str] = set()
    for key_node, _value_node in node.value:
        key = repr(loader.construct_object(key_node, deep=True))
        if key in seen:
            raise yaml.YAMLError(f"duplicate mapping key {key}")
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


def _preserve_timestamp_string(loader: _StrictLoader, node: yaml.ScalarNode) -> str:
    """Keep ISO timestamp strings as strings, not datetime objects."""
    return loader.construct_scalar(node)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_no_duplicates
)
# Override timestamp tag to preserve strings for schema validation
_StrictLoader.add_constructor("tag:yaml.org,2002:timestamp", _preserve_timestamp_string)


def _strict_load(text: str) -> object:
    """Parse YAML, rejecting duplicate mapping keys at any depth."""
    return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 — SafeLoader subclass


@functools.cache
def _proposal_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_PROPOSAL_SCHEMA.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


@functools.cache
def _decision_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_DECISION_SCHEMA.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def _relpath(path: Path, mirror_root: Path) -> str:
    try:
        return path.relative_to(mirror_root).as_posix()
    except ValueError:
        return path.as_posix()


def _inside(path: Path, root: Path) -> bool:
    """True when `path` still resolves inside `root` (symlink escape guard)."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _excluded(name: str) -> bool:
    return name.startswith((".", "_")) or name == "contracts"


def _discover(mirror_root: Path) -> tuple[list[Path], list[Diagnostic]]:
    """Find proposal-bundle roots: every directory holding a proposal.yaml.

    Exclusions apply to ANY path segment; directory symlinks are pruned; an
    enumeration failure becomes a walk-error diagnostic via onerror — the
    default os.walk behaviour is to swallow it, which would read as
    «0 bundles» (spec: fail-loud).
    """
    diagnostics: list[Diagnostic] = []

    def onerror(err: OSError) -> None:
        target = getattr(err, "filename", None) or str(mirror_root)
        diagnostics.append(
            Diagnostic(
                code="walk-error",
                message=f"{type(err).__name__}: {err}",
                path=_relpath(Path(target), mirror_root),
            )
        )

    bundles: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(
        mirror_root, topdown=True, onerror=onerror, followlinks=False
    ):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames if not _excluded(d) and not (here / d).is_symlink()
        )
        if "proposal.yaml" in filenames:
            bundles.append(here)
    bundles.sort(key=lambda p: _relpath(p, mirror_root))
    return bundles, diagnostics


def _load_yaml_file(
    path: Path, mirror_root: Path, code_prefix: str
) -> tuple[object | None, Diagnostic | None]:
    """Read one YAML file fail-closed: escape guard, UTF-8, strict parse."""
    rel = _relpath(path, mirror_root)
    if not _inside(path, mirror_root):
        return None, Diagnostic(
            code=f"{code_prefix}-path-escape",
            message="resolves outside the mirror root; not read",
            path=rel,
        )
    try:
        data = _strict_load(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as err:
        return None, Diagnostic(
            code=f"{code_prefix}-unreadable",
            message=f"{type(err).__name__}: {err}",
            path=rel,
        )
    return data, None


def _load_decisions(
    bundle_dir: Path, mirror_root: Path, *, classify: bool
) -> tuple[list[dict[str, object]], list[Diagnostic]]:
    """Read decisions/*.yaml, collecting ALL problems (never just the first).

    classify=False (untrusted proposal): read/parse errors are still
    collected, but schema validation — semantic classification — is skipped.
    An absent decisions/ directory is a valid bundle with no decisions.
    Only the `.yaml` extension is contract, recognized case-insensitively
    (e.g. `.YAML`); `.yml` is out of contract and is never read.
    """
    decisions_dir = bundle_dir / "decisions"
    diagnostics: list[Diagnostic] = []
    records: list[dict[str, object]] = []
    try:
        files = sorted(
            p for p in decisions_dir.iterdir() if p.suffix.lower() == ".yaml"
        )
    except FileNotFoundError:
        return [], []
    except OSError as err:
        return [], [
            Diagnostic(
                code="decision-unreadable",
                message=f"cannot list decisions/: {type(err).__name__}: {err}",
                path=_relpath(decisions_dir, mirror_root),
            )
        ]
    for path in files:
        data, diag = _load_yaml_file(path, mirror_root, "decision")
        if diag is not None:
            diagnostics.append(diag)
            continue
        if not classify:
            continue
        if not isinstance(data, dict):
            diagnostics.append(
                Diagnostic(
                    code="decision-unreadable",
                    message="not a YAML mapping",
                    path=_relpath(path, mirror_root),
                )
            )
        elif not _decision_validator().is_valid(data):
            diagnostics.append(
                Diagnostic(
                    code="decision-schema-invalid",
                    message="does not match the vendored gate-decision/v1",
                    path=_relpath(path, mirror_root),
                )
            )
        else:
            records.append(data)
    return records, diagnostics


def _supersedes_target(record: dict[str, object]) -> str | None:
    raw = record.get("supersedes")
    if isinstance(raw, str):
        return raw.removeprefix("gate-decision://")
    return None


def _supersession_integrity(records: list[dict[str, object]]) -> list[Diagnostic]:
    """Duplicate ids, dangling supersedes, self/cyclic supersession.

    Runs only over a fully schema-valid decision set; any hit means the
    decision history is unprovable — the caller classifies unknown.
    """
    ids = [str(r["decision_id"]) for r in records]
    counts = Counter(ids)
    duplicates = sorted(i for i, n in counts.items() if n > 1)
    if duplicates:
        return [
            Diagnostic(
                code="decision-id-duplicate",
                message=f"decision_id {d} appears more than once in the bundle",
            )
            for d in duplicates
        ]
    chain = {
        str(r["decision_id"]): target
        for r in records
        if (target := _supersedes_target(r)) is not None
    }
    id_set = set(ids)
    diagnostics = [
        Diagnostic(
            code="supersedes-dangling",
            message=f"{source} supersedes {target}, which is not in the bundle",
        )
        for source, target in sorted(chain.items())
        if target not in id_set
    ]
    cycles: set[frozenset[str]] = set()
    for start in chain:
        seen: list[str] = []
        current: str | None = start
        while current in chain:
            if current in seen:
                cycles.add(frozenset(seen[seen.index(current) :]))
                break
            seen.append(current)
            current = chain[current]
    diagnostics.extend(
        Diagnostic(
            code="supersedes-cycle",
            message=f"supersession cycle: {', '.join(sorted(members))}",
        )
        for members in sorted(cycles, key=sorted)
    )
    return diagnostics


def _gate_wait(
    bundle_path: str,
    proposal: dict[str, object],
    decisions: list[dict[str, object]],
) -> GateWait | None:
    """The version-matched active-approve rule (spec «Classification»)."""
    gate_id = _STATUS_GATE.get(str(proposal["status"]))
    if gate_id is None:
        return None
    proposal_id = str(proposal["proposal_id"])
    version = int(proposal["version"])  # type: ignore[arg-type]
    ref = f"proposal://{proposal_id}"
    superseded = {
        target for r in decisions if (target := _supersedes_target(r)) is not None
    }
    for record in decisions:
        subject = record["subject"]
        assert isinstance(subject, dict)  # schema-guaranteed
        if (
            record["decision"] == "approve"
            and record["gate_id"] == gate_id
            and subject["kind"] == "product_proposal"
            and subject["ref"] == ref
            and subject["version"] == version
            and str(record["decision_id"]) not in superseded
        ):
            return None
    return GateWait(
        proposal_id=proposal_id,
        gate_id=gate_id,
        gate_label=_GATE_LABEL[gate_id],
        authority=_GATE_AUTHORITY[gate_id],
        artifact_ref=ref,
        bundle_path=bundle_path,
        version=version,
        proposal_updated_at=str(proposal["updated_at"]),
    )


def _load_bundle(mirror_root: Path, bundle_dir: Path) -> ProposalBundle:
    """Classify one bundle; the global conflict pass runs in the caller."""
    rel = _relpath(bundle_dir, mirror_root) or "."
    diagnostics: list[Diagnostic] = []
    proposal: dict[str, object] | None = None

    data, diag = _load_yaml_file(bundle_dir / "proposal.yaml", mirror_root, "proposal")
    if diag is not None:
        diagnostics.append(diag)
    elif not isinstance(data, dict):
        diagnostics.append(
            Diagnostic(
                code="proposal-unreadable",
                message="not a YAML mapping",
                path=f"{rel}/proposal.yaml",
            )
        )
    elif not _proposal_validator().is_valid(data):
        diagnostics.append(
            Diagnostic(
                code="proposal-schema-invalid",
                message="does not match the vendored product-proposal/v1",
                path=f"{rel}/proposal.yaml",
            )
        )
    else:
        proposal = data

    decisions, decision_diags = _load_decisions(
        bundle_dir, mirror_root, classify=proposal is not None
    )
    diagnostics.extend(decision_diags)
    if proposal is not None and not decision_diags:
        diagnostics.extend(_supersession_integrity(decisions))

    diagnostics.sort(key=lambda d: (d.path or "", d.code, d.message))
    if any(d.code in _UNREADABLE_CODES for d in diagnostics):
        state: BundleState = "unreadable"
    elif diagnostics:
        state = "unknown"
    else:
        state = "ok"

    waits: list[GateWait] = []
    if state == "ok" and proposal is not None:
        wait = _gate_wait(rel, proposal, decisions)
        if wait is not None:
            waits.append(wait)

    return ProposalBundle(
        path=rel,
        state=state,
        diagnostics=diagnostics,
        proposal_id=str(proposal["proposal_id"]) if proposal else None,
        status=str(proposal["status"]) if proposal else None,
        version=int(proposal["version"]) if proposal else None,  # type: ignore[arg-type]
        updated_at=str(proposal["updated_at"]) if proposal else None,
        waits=waits,
    )


def _mark_conflicts(bundles: list[ProposalBundle]) -> None:
    """Global proposal_id check AFTER all bundles parsed: every participant
    gets the identical deterministic path list; earlier diagnostics are
    preserved; conflict outranks every other state and suppresses waits."""
    by_id: dict[str, list[ProposalBundle]] = {}
    for bundle in bundles:
        if bundle.proposal_id is not None:
            by_id.setdefault(bundle.proposal_id, []).append(bundle)
    for proposal_id, group in sorted(by_id.items()):
        if len(group) < 2:
            continue
        paths = ", ".join(sorted(b.path for b in group))
        for bundle in group:
            bundle.state = "conflict"
            bundle.waits = []
            bundle.diagnostics.append(
                Diagnostic(
                    code="proposal-id-conflict",
                    message=(
                        f"proposal_id {proposal_id} claimed by several bundles: {paths}"
                    ),
                )
            )
            bundle.diagnostics.sort(key=lambda d: (d.path or "", d.code, d.message))


def collect_product_proposals(mirror_root: Path) -> ProductProposalsReport:
    """Scan the impresario mirror and classify every proposal bundle.

    Anchors are re-checked before every scan — a mirror that vanished or
    degraded after discovery is a visible diagnostic, never «0 bundles».
    """
    missing = [rel for rel in ANCHOR_FILES if not (mirror_root / rel).is_file()]
    if missing:
        return ProductProposalsReport(
            mirror_path=str(mirror_root),
            diagnostics=[
                Diagnostic(
                    code="mirror-anchors-missing",
                    message="expected impresario anchor is not a file",
                    path=rel,
                )
                for rel in missing
            ],
            attention=True,
        )
    bundle_dirs, report_diags = _discover(mirror_root)
    bundles = [_load_bundle(mirror_root, d) for d in bundle_dirs]
    _mark_conflicts(bundles)
    report_diags.sort(key=lambda d: (d.path or "", d.code, d.message))
    waits = sorted(
        (w for b in bundles if b.state == "ok" for w in b.waits),
        key=lambda w: (w.proposal_id, w.gate_id, w.version, w.bundle_path),
    )
    return ProductProposalsReport(
        mirror_path=str(mirror_root),
        bundles=bundles,
        waits=waits,
        diagnostics=report_diags,
        attention=bool(report_diags) or any(b.state != "ok" for b in bundles),
    )
