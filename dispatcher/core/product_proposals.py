"""Product-proposal gate_waiting collector: classify impresario bundles.

Inbox #129 phase 1 (spec: docs/superpowers/specs/
2026-08-12-product-proposal-gate-waiting-design.md). Reads proposal bundles
(`proposal.yaml` + `decisions/*.yaml`) out of the impresario mirror and says
which product decisions are waiting for a human — Gate A (`qg5_business`,
business_owner) and Gate B (`qg5_committee`, committee_chair).

Inbox #154 phase 3 (spec: docs/superpowers/specs/
2026-08-17-qg4-backlog-wait-design.md) adds the backlog-level QG-4 wait:
a RankedBacklog (`backlog.yaml` + sibling `decisions/*.yaml`) whose current
version has selectable items and no active QG-4 decision waits for the
`qg4_selector`. QG-4 is ONE gate over the backlog version — zero or one
wait per version, identity `(backlog_id, version)`; ANY of the QG-4
outcomes (select | defer | park | reject) extinguishes it.

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
_LOOP_STATE_SCHEMA = _CONTRACTS / "impresario-loop-state" / "v1" / "schema.json"
_BACKLOG_SCHEMA = _CONTRACTS / "impresario-ranked-backlog" / "v1" / "schema.json"
_LRD_SCHEMA = _CONTRACTS / "impresario-loop-resume-decision" / "v1" / "schema.json"

ANCHOR_FILES = (
    "contracts/product-proposal/v1/schema.json",
    "docs/semantics.md",
)

GateId = Literal["qg5_business", "qg5_committee"]
BundleState = Literal["ok", "unreadable", "unknown", "conflict"]

LoopStatus = Literal[
    "absent",  # no loop.state file — normal, NOT an error
    "running",  # valid file, stop: null
    "needs_human",  # valid file, active human wait
    "ready_for_business",  # valid file, terminal
    "failed",  # valid file, terminal
    "unknown",  # loop state untrusted (file problems, or the
    # bundle is non-ok — the caller's rule)
]
_TERMINAL_LOOP: dict[str, LoopStatus] = {
    "ready_for_business": "ready_for_business",
    "failed": "failed",
}

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
_BACKLOG_UNREADABLE_CODES = {
    "backlog-unreadable",
    "backlog-schema-invalid",
    "backlog-path-escape",
}
QG4_GATE_ID = "qg4_backlog"
_QG4_LABEL = "QG-4"
_QG4_AUTHORITY = "qg4_selector"
# The gate-decision/v1 conditional pins these as the ONLY qg4_backlog
# outcomes; semantics.md «QG-4: human select» — any of them extinguishes
# the wait, not only select.
_QG4_OUTCOMES = {"select", "defer", "park", "reject"}
# Selectable = no QG-4 outcome recorded in the item's own status yet.
# Owner-fixed policy: `under_review` is still undecided, so it keeps the
# wait open just like `new` does.
_SELECTABLE_STATUSES = {"new", "under_review"}


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


class LoopWait(BaseModel):
    """One «the researcher↔creator loop waits for a human» record."""

    loop_id: str
    # stop.iteration; wait identity = (loop_id, iteration) — a repeated
    # needs_human after resume is a NEW wait, not a duplicate.
    iteration: int
    proposal_id: str
    reason: str
    # stop.at — the actual stop time (unlike proposal_updated_at, this one
    # IS a proven wait-start moment).
    stopped_at: str
    # Deterministic tie-break + provenance, NOT part of the identity.
    bundle_path: str


class BacklogWait(BaseModel):
    """One «QG-4 over the current backlog version waits for a human» record.

    Dedup identity = (backlog_id, version): a version bump extinguishes the
    previous wait and (with selectable items) opens a NEW one. idea_ref is
    context, never identity — QG-4 is one gate over the version, not one
    per item.
    """

    backlog_id: str
    gate_id: Literal["qg4_backlog"] = QG4_GATE_ID
    gate_label: str = _QG4_LABEL
    authority: str = _QG4_AUTHORITY
    artifact_ref: str  # "backlog://<id>"
    artifact_path: str  # backlog.yaml, relative to the mirror root
    version: int
    # backlog.updated_at — the version publication moment, a real freshness
    # signal (unlike proposal_updated_at).
    backlog_updated_at: str
    # Context only (which items are still selectable), NOT part of the
    # identity and not part of artifact_ref.
    selectable_idea_refs: list[str] = Field(default_factory=list)


class BacklogBundle(BaseModel):
    """Every discovered backlog, lossless: non-ok rows keep ALL diagnostics."""

    path: str  # directory holding backlog.yaml, relative to the mirror root
    state: BundleState
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    backlog_id: str | None = None
    version: int | None = None
    updated_at: str | None = None
    # Computed ONLY for state == "ok" (0..1 records). On any other state an
    # empty list means «suppressed», never «nothing waits».
    waits: list[BacklogWait] = Field(default_factory=list)


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
    # "absent" is normal; "unknown" whenever the file is untrusted OR the
    # bundle is non-ok. loop_waits computed ONLY for state == "ok" — empty
    # on any other state means «suppressed», never «no loop wait».
    loop_status: LoopStatus = "absent"
    loop_waits: list[LoopWait] = Field(default_factory=list)


class ProductProposalsReport(BaseModel):
    """The read model of one scan of the impresario mirror."""

    mirror_path: str
    bundles: list[ProposalBundle] = Field(default_factory=list)
    waits: list[GateWait] = Field(default_factory=list)
    needs_human: list[LoopWait] = Field(default_factory=list)
    backlog_bundles: list[BacklogBundle] = Field(default_factory=list)
    backlog_waits: list[BacklogWait] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    # Any non-ok bundle (proposal or backlog) or report-level diagnostic. A
    # plain wait does NOT raise attention — waiting is expected business work.
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


@functools.cache
def _loop_state_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_LOOP_STATE_SCHEMA.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


@functools.cache
def _backlog_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_BACKLOG_SCHEMA.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


@functools.cache
def _lrd_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_LRD_SCHEMA.read_text(encoding="utf-8"))
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


def _discover(
    mirror_root: Path,
) -> tuple[list[Path], list[Path], list[Diagnostic]]:
    """Find bundle roots: directories holding a proposal.yaml or backlog.yaml.

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
    backlogs: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(
        mirror_root, topdown=True, onerror=onerror, followlinks=False
    ):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames if not _excluded(d) and not (here / d).is_symlink()
        )
        if "proposal.yaml" in filenames:
            bundles.append(here)
        if "backlog.yaml" in filenames:
            backlogs.append(here)
    bundles.sort(key=lambda p: _relpath(p, mirror_root))
    backlogs.sort(key=lambda p: _relpath(p, mirror_root))
    return bundles, backlogs, diagnostics


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
        elif _decision_validator().is_valid(data):
            records.append(data)
        elif _lrd_validator().is_valid(data):
            # A loop-resume-decision (impresario's resume authorization,
            # vendored loop-resume-decision/v1) legitimately shares the
            # decisions/ directory. It is valid mirror content that plays
            # no role in gate classification — recognized, then ignored.
            continue
        else:
            diagnostics.append(
                Diagnostic(
                    code="decision-schema-invalid",
                    message=(
                        "matches neither the vendored gate-decision/v1 nor "
                        "loop-resume-decision/v1"
                    ),
                    path=_relpath(path, mirror_root),
                )
            )
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


def _json_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """object_pairs_hook rejecting duplicate keys (plain json.loads keeps
    the last value silently — the strict-YAML discipline, applied to JSON)."""
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON key {key!r}")
        obj[key] = value
    return obj


def _load_loop_state(
    bundle_dir: Path, mirror_root: Path, proposal: dict[str, object] | None
) -> tuple[LoopStatus, LoopWait | None, list[Diagnostic]]:
    """Read <bundle>/loop.state fail-closed (spec «Classification»).

    Absent is normal. Any problem yields decision-grade diagnostics (the
    caller's non-ok rule then forces "unknown"). A wait comes only from a
    fully validated stop.verdict == needs_human whose proposal_id matches
    the bundle's proposal — the ONLY producer cross-check replicated here;
    the other LOOPSTATE_* checks stay with impresario's validator.
    """
    path = bundle_dir / "loop.state"
    rel = _relpath(path, mirror_root)

    def problem(
        code: str, message: str
    ) -> tuple[LoopStatus, LoopWait | None, list[Diagnostic]]:
        return "unknown", None, [Diagnostic(code=code, message=message, path=rel)]

    if not _inside(path, mirror_root):
        return problem(
            "loop-state-path-escape",
            "resolves outside the mirror root; not read",
        )
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return "absent", None, []
    except (OSError, UnicodeDecodeError) as err:
        return problem("loop-state-unreadable", f"{type(err).__name__}: {err}")
    try:
        data = json.loads(text, object_pairs_hook=_json_no_duplicates)
    except ValueError as err:  # JSONDecodeError and duplicate keys alike
        return problem("loop-state-unreadable", f"{type(err).__name__}: {err}")
    if not isinstance(data, dict):
        return problem("loop-state-unreadable", "not a JSON object")
    if proposal is None:
        # No trusted subject: schema validation and the membership check
        # are semantic classification — skipped; the caller's non-ok rule
        # owns the final loop_status.
        return "unknown", None, []
    if not _loop_state_validator().is_valid(data):
        return problem(
            "loop-state-schema-invalid",
            "does not match the vendored loop-state/v1 (incompatible "
            "shape; the file carries no version field)",
        )
    loop_pid = str(data["proposal_id"])
    bundle_pid = str(proposal["proposal_id"])
    if loop_pid != bundle_pid:
        return problem(
            "loop-state-proposal-mismatch",
            f"{rel} belongs to {loop_pid!r}, but this bundle's proposal "
            f"is {bundle_pid!r}",
        )
    stop = data["stop"]
    if stop is None:
        return "running", None, []
    assert isinstance(stop, dict)  # schema-guaranteed
    verdict = str(stop["verdict"])
    if verdict in _TERMINAL_LOOP:
        return _TERMINAL_LOOP[verdict], None, []
    wait = LoopWait(
        loop_id=str(data["loop_id"]),
        iteration=int(stop["iteration"]),  # type: ignore[arg-type]
        proposal_id=loop_pid,
        reason=str(stop["reason"]),
        stopped_at=str(stop["at"]),
        bundle_path=_relpath(bundle_dir, mirror_root) or ".",
    )
    return "needs_human", wait, []


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


def _backlog_wait(
    artifact_path: str,
    backlog: dict[str, object],
    decisions: list[dict[str, object]],
) -> BacklogWait | None:
    """The version-matched active-QG-4 rule (issue #154, spec «Semantics»).

    QG-4 is one gate over the backlog VERSION: the wait exists while the
    current version has selectable items and no active decision, and ANY of
    the QG-4 outcomes extinguishes it. A decision for another version is
    history, not permission — a version bump reopens the gate.
    """
    items = backlog["items"]
    assert isinstance(items, list)  # schema-guaranteed
    selectable = [
        str(item["idea_ref"])
        for item in items
        if isinstance(item, dict) and item["status"] in _SELECTABLE_STATUSES
    ]
    if not selectable:
        return None
    backlog_id = str(backlog["id"])
    version = int(backlog["version"])  # type: ignore[arg-type]
    ref = f"backlog://{backlog_id}"
    superseded = {
        target for r in decisions if (target := _supersedes_target(r)) is not None
    }
    for record in decisions:
        subject = record["subject"]
        assert isinstance(subject, dict)  # schema-guaranteed
        if (
            record["gate_id"] == QG4_GATE_ID
            and record["decision"] in _QG4_OUTCOMES
            and subject["kind"] == "ranked_backlog"
            and subject["ref"] == ref
            and subject["version"] == version
            and str(record["decision_id"]) not in superseded
        ):
            return None
    return BacklogWait(
        backlog_id=backlog_id,
        artifact_ref=ref,
        artifact_path=artifact_path,
        version=version,
        backlog_updated_at=str(backlog["updated_at"]),
        selectable_idea_refs=selectable,
    )


def _load_backlog_bundle(mirror_root: Path, backlog_dir: Path) -> BacklogBundle:
    """Classify one backlog root; the global id-conflict pass runs later."""
    rel = _relpath(backlog_dir, mirror_root) or "."
    backlog_file = backlog_dir / "backlog.yaml"
    diagnostics: list[Diagnostic] = []
    backlog: dict[str, object] | None = None

    data, diag = _load_yaml_file(backlog_file, mirror_root, "backlog")
    if diag is not None:
        diagnostics.append(diag)
    elif not isinstance(data, dict):
        diagnostics.append(
            Diagnostic(
                code="backlog-unreadable",
                message="not a YAML mapping",
                path=_relpath(backlog_file, mirror_root),
            )
        )
    elif not _backlog_validator().is_valid(data):
        diagnostics.append(
            Diagnostic(
                code="backlog-schema-invalid",
                message="does not match the vendored ranked-backlog/v1",
                path=_relpath(backlog_file, mirror_root),
            )
        )
    else:
        backlog = data

    decisions, decision_diags = _load_decisions(
        backlog_dir, mirror_root, classify=backlog is not None
    )
    diagnostics.extend(decision_diags)
    if backlog is not None and not decision_diags:
        diagnostics.extend(_supersession_integrity(decisions))

    diagnostics.sort(key=lambda d: (d.path or "", d.code, d.message))
    if any(d.code in _BACKLOG_UNREADABLE_CODES for d in diagnostics):
        state: BundleState = "unreadable"
    elif diagnostics:
        state = "unknown"
    else:
        state = "ok"

    waits: list[BacklogWait] = []
    if state == "ok" and backlog is not None:
        wait = _backlog_wait(_relpath(backlog_file, mirror_root), backlog, decisions)
        if wait is not None:
            waits.append(wait)

    return BacklogBundle(
        path=rel,
        state=state,
        diagnostics=diagnostics,
        backlog_id=str(backlog["id"]) if backlog else None,
        version=int(backlog["version"]) if backlog else None,  # type: ignore[arg-type]
        updated_at=str(backlog["updated_at"]) if backlog else None,
        waits=waits,
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
                path=_relpath(bundle_dir / "proposal.yaml", mirror_root),
            )
        )
    elif not _proposal_validator().is_valid(data):
        diagnostics.append(
            Diagnostic(
                code="proposal-schema-invalid",
                message="does not match the vendored product-proposal/v1",
                path=_relpath(bundle_dir / "proposal.yaml", mirror_root),
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

    loop_status, loop_wait, loop_diags = _load_loop_state(
        bundle_dir, mirror_root, proposal
    )
    diagnostics.extend(loop_diags)

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

    loop_waits: list[LoopWait] = []
    if state == "ok":
        if loop_wait is not None:
            loop_waits.append(loop_wait)
    else:
        # Owner-fixed rule: a non-ok bundle's loop state is unclassified —
        # unconditionally, including when the file is absent.
        loop_status = "unknown"

    return ProposalBundle(
        path=rel,
        state=state,
        diagnostics=diagnostics,
        proposal_id=str(proposal["proposal_id"]) if proposal else None,
        status=str(proposal["status"]) if proposal else None,
        version=int(proposal["version"]) if proposal else None,  # type: ignore[arg-type]
        updated_at=str(proposal["updated_at"]) if proposal else None,
        waits=waits,
        loop_status=loop_status,
        loop_waits=loop_waits,
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
            bundle.loop_waits = []
            bundle.loop_status = "unknown"
            bundle.diagnostics.append(
                Diagnostic(
                    code="proposal-id-conflict",
                    message=(
                        f"proposal_id {proposal_id} claimed by several bundles: {paths}"
                    ),
                )
            )
            bundle.diagnostics.sort(key=lambda d: (d.path or "", d.code, d.message))


def _mark_backlog_conflicts(bundles: list[BacklogBundle]) -> None:
    """Same discipline as _mark_conflicts, for the backlog identity: one
    backlog id claimed by several roots is a conflict, never arbitrary
    dedup; conflict suppresses waits."""
    by_id: dict[str, list[BacklogBundle]] = {}
    for bundle in bundles:
        if bundle.backlog_id is not None:
            by_id.setdefault(bundle.backlog_id, []).append(bundle)
    for backlog_id, group in sorted(by_id.items()):
        if len(group) < 2:
            continue
        paths = ", ".join(sorted(b.path for b in group))
        for bundle in group:
            bundle.state = "conflict"
            bundle.waits = []
            bundle.diagnostics.append(
                Diagnostic(
                    code="backlog-id-conflict",
                    message=(
                        f"backlog id {backlog_id} claimed by several roots: {paths}"
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
    bundle_dirs, backlog_dirs, report_diags = _discover(mirror_root)
    bundles = [_load_bundle(mirror_root, d) for d in bundle_dirs]
    _mark_conflicts(bundles)
    backlog_bundles = [_load_backlog_bundle(mirror_root, d) for d in backlog_dirs]
    _mark_backlog_conflicts(backlog_bundles)
    report_diags.sort(key=lambda d: (d.path or "", d.code, d.message))
    waits = sorted(
        (w for b in bundles if b.state == "ok" for w in b.waits),
        key=lambda w: (w.proposal_id, w.gate_id, w.version, w.bundle_path),
    )
    needs_human = sorted(
        (w for b in bundles if b.state == "ok" for w in b.loop_waits),
        key=lambda w: (w.loop_id, w.iteration, w.bundle_path),
    )
    backlog_waits = sorted(
        (w for b in backlog_bundles if b.state == "ok" for w in b.waits),
        key=lambda w: (w.backlog_id, w.version, w.artifact_path),
    )
    return ProductProposalsReport(
        mirror_path=str(mirror_root),
        bundles=bundles,
        waits=waits,
        needs_human=needs_human,
        backlog_bundles=backlog_bundles,
        backlog_waits=backlog_waits,
        diagnostics=report_diags,
        attention=bool(report_diags)
        or any(b.state != "ok" for b in bundles)
        or any(b.state != "ok" for b in backlog_bundles),
    )
