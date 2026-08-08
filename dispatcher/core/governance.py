"""Governance-collector: classify a bundle from its gate-verdicts file.

WS-005 WS-B (inbox #106). Reads ``<repo>/.steward/gate_verdicts.jsonl``
(ARCH-D1) written by steward's ``gate-check --emit-verdicts`` and classifies
the bundle into six states — ``pass | blocked | no-data | unreadable | stale |
unresolvable`` — from the file plus git facts (ARCH-D2).

Architectural constraints this module lives under:

- ARCH-C3 / FR-02: classification only. Verdicts are never computed here,
  steward is never imported and its CLI is never executed; the only
  subprocess on this path is local ``git``.
- NFR-02 (fail-closed): no read/parse/git error path may produce ``pass``.
  A missing file is ``no-data`` (learned from the read attempt itself — no
  ``exists()`` pre-check, no TOCTOU window); every other failure is
  ``unreadable`` with a reason, and unknown freshness is stale-grade.
- The line shapes are validated against the *vendored* schema copy
  (``contracts/steward-gate-verdicts/v1/``), the same runtime pattern as
  ``core/contract.py``; no sibling-repo path is ever resolved (CON-03).
- A finding's ``obligation`` is validated against the vendored gate-catalog
  vocabulary (``contracts/steward-gate-catalog/v1/``, inbox #125): the
  schema allows any string, but the catalog is the SSOT for what the value
  may say, and a value outside it means producer and vendored catalog have
  diverged — ``unreadable``, never ``pass`` (NFR-02).
"""

from __future__ import annotations

import functools
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import jsonschema
from pydantic import BaseModel, ConfigDict, ValidationError

from dispatcher.core.gate_catalog import obligation_vocabulary

_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "steward-gate-verdicts"
    / "v1"
    / "SCHEMA.json"
)
SUPPORTED_SCHEMA_VERSION = "1"
VERDICTS_REL_PATH = Path(".steward") / "gate_verdicts.jsonl"

BundleState = Literal[
    "pass", "blocked", "no-data", "unreadable", "stale", "unresolvable"
]


class VerdictHeader(BaseModel):
    """Provenance record of one gate-check run (line 1 of the file)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["header"]
    schema_version: str
    source_commit: str
    dirty: bool
    generated_at: str
    profile: str
    bundle: str


class VerdictArtifact(BaseModel):
    """One bundle-inventory record."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["artifact"]
    path: str
    node_id: str | None
    status: str
    owner_roles: list[str]


class VerdictFinding(BaseModel):
    """One gate violation fact."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["finding"]
    gate_id: str
    verdict: str
    artifact: str
    message: str
    obligation: str | None = None
    tier: str | None = None
    phase: str | None = None
    risk_model_version: str | None = None
    waiver_ref: str | None = None


class BundleFreshness(BaseModel):
    """What a git-facts provider learned; ``fresh=None`` means 'could not
    tell' and is classified stale-grade by the caller, never as fresh."""

    fresh: bool | None
    current_commit: str | None = None
    detail: str | None = None


GitFactsProvider = Callable[[Path, str, str], BundleFreshness]
"""(repo_root, bundle_rel_path, source_commit) -> BundleFreshness."""


class BundleGovernance(BaseModel):
    """The read model one observed repo's bundle classifies into."""

    state: BundleState
    reason: str | None = None
    header: VerdictHeader | None = None
    artifacts: list[VerdictArtifact] = []
    findings: list[VerdictFinding] = []
    unresolvable_findings: list[VerdictFinding] = []


class _Parsed(BaseModel):
    header: VerdictHeader
    artifacts: list[VerdictArtifact]
    findings: list[VerdictFinding]


@functools.cache
def _validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def _unreadable(reason: str) -> BundleGovernance:
    return BundleGovernance(state="unreadable", reason=reason)


def _parse_record(line: str, number: int) -> dict[str, object] | BundleGovernance:
    """One JSONL line to a schema-valid record, or the unreadable verdict."""
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return _unreadable(f"invalid JSON (line {number})")
    if not isinstance(record, dict):
        return _unreadable(f"record is not an object (line {number})")
    if not _validator().is_valid(record):
        version = record.get("schema_version")
        if (
            record.get("kind") == "header"
            and isinstance(version, str)
            and version != SUPPORTED_SCHEMA_VERSION
        ):
            # Only an actual, different version string earns the specific
            # reason; a header that is invalid some other way (missing or
            # non-string schema_version included) gets the generic one —
            # "unsupported schema_version None" would point away from the
            # real defect.
            return _unreadable(
                f"unsupported schema_version {version!r} (line {number}); "
                f"this consumer is pinned to v{SUPPORTED_SCHEMA_VERSION} "
                "(contracts/steward-gate-verdicts/v1/)"
            )
        return _unreadable(f"record does not match gate-verdicts/v1 (line {number})")
    return record


def _parse_lines(text: str) -> _Parsed | BundleGovernance:
    """The whole file to typed records, or the unreadable verdict."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the single trailing newline; anything else is a defect
    if not lines:
        return _unreadable("empty file, missing header")

    first = _parse_record(lines[0], 1)
    if isinstance(first, BundleGovernance):
        return first
    if first.get("kind") != "header":
        return _unreadable("line 1 is not a header record")

    artifacts: list[VerdictArtifact] = []
    findings: list[VerdictFinding] = []
    vocabulary = obligation_vocabulary()
    try:
        header = VerdictHeader.model_validate(first)
        for offset, line in enumerate(lines[1:], start=2):
            record = _parse_record(line, offset)
            if isinstance(record, BundleGovernance):
                return record
            kind = record.get("kind")
            if kind == "header":
                return _unreadable(f"unexpected second header (line {offset})")
            if kind == "artifact":
                artifacts.append(VerdictArtifact.model_validate(record))
            else:
                finding = VerdictFinding.model_validate(record)
                # An absent obligation is an older producer and stays valid;
                # a present value outside the vendored catalog's vocabulary
                # means the producer speaks a catalog this consumer has not
                # vendored yet — surfaced, never silently passed through.
                if (
                    finding.obligation is not None
                    and finding.obligation not in vocabulary
                ):
                    return _unreadable(
                        f"unknown obligation {finding.obligation!r} "
                        f"(line {offset}); not in the vendored gate-catalog "
                        "vocabulary (contracts/steward-gate-catalog/v1/)"
                    )
                findings.append(finding)
    except ValidationError as err:
        # Schema-valid but model-invalid means the two drifted apart; that
        # is a defect worth surfacing, still never silently and never pass.
        return _unreadable(f"record failed model validation: {err}")
    return _Parsed(header=header, artifacts=artifacts, findings=findings)


def _freshness_reason(header: VerdictHeader, facts: BundleFreshness) -> str | None:
    """A stale reason, or None when provenance is proven fresh."""
    if header.dirty:
        return "emitted from a dirty tree"
    if facts.fresh is None:
        detail = facts.detail or "git facts unavailable"
        return f"freshness unknown: {detail}"
    if not facts.fresh:
        current = facts.current_commit or "unknown"
        suffix = f" ({facts.detail})" if facts.detail else ""
        return f"emitted at {header.source_commit}, bundle is now at {current}{suffix}"
    return None


def collect_governance(
    repo_root: Path, *, git_facts: GitFactsProvider | None = None
) -> BundleGovernance:
    """Classify one observed repo's bundle from its verdicts file + git facts.

    Precedence, first match wins: no-data → unreadable → stale →
    unresolvable → blocked → pass.
    """
    path = repo_root / VERDICTS_REL_PATH
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return BundleGovernance(state="no-data", reason="no gate_verdicts.jsonl")
    except OSError as err:
        return _unreadable(f"{type(err).__name__}: {err}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as err:
        return _unreadable(f"not UTF-8: {err}")

    parsed = _parse_lines(text)
    if isinstance(parsed, BundleGovernance):
        return parsed
    header, artifacts, findings = parsed.header, parsed.artifacts, parsed.findings

    provider = git_facts if git_facts is not None else git_bundle_freshness
    facts = provider(repo_root, header.bundle, header.source_commit)
    stale_reason = _freshness_reason(header, facts)
    if stale_reason is not None:
        return BundleGovernance(
            state="stale",
            reason=stale_reason,
            header=header,
            artifacts=artifacts,
            findings=findings,
        )

    inventory = {artifact.path for artifact in artifacts}
    unresolvable = [f for f in findings if f.artifact not in inventory]
    if unresolvable:
        ghosts = ", ".join(sorted({f.artifact for f in unresolvable}))
        return BundleGovernance(
            state="unresolvable",
            reason=f"findings reference artifacts outside the inventory: {ghosts}",
            header=header,
            artifacts=artifacts,
            findings=findings,
            unresolvable_findings=unresolvable,
        )
    if findings:
        return BundleGovernance(
            state="blocked",
            header=header,
            artifacts=artifacts,
            findings=findings,
        )
    return BundleGovernance(state="pass", header=header, artifacts=artifacts)


def _git(repo_root: Path, *args: str) -> str | None:
    """One git query; ``None`` on nonzero exit or an unrunnable binary."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def git_bundle_freshness(
    repo_root: Path, bundle: str, source_commit: str
) -> BundleFreshness:
    """Compare the bundle's content at ``source_commit`` with its current state.

    Reads git only; unknown is reported as ``fresh=None``, never as
    ``fresh=True`` — the caller classifies unknown as stale-grade (NFR-02).
    A commit or bundle git *does* know about but that does not match is
    ``fresh=False``: that is a determined mismatch, not an unknown.
    """
    head = _git(repo_root, "rev-parse", "HEAD")
    if head is None:
        return BundleFreshness(
            fresh=None, detail="not a git repository (or HEAD unreadable)"
        )
    if _git(repo_root, "rev-parse", "--verify", f"{source_commit}^{{commit}}") is None:
        return BundleFreshness(
            fresh=False,
            current_commit=head,
            detail="source commit unknown to this clone",
        )
    source_tree = _git(repo_root, "rev-parse", f"{source_commit}:{bundle}")
    current_tree = _git(repo_root, "rev-parse", f"HEAD:{bundle}")
    if source_tree is None or current_tree is None or source_tree != current_tree:
        return BundleFreshness(
            fresh=False, current_commit=head, detail="bundle tree differs"
        )
    porcelain = _git(repo_root, "status", "--porcelain", "--", bundle)
    if porcelain is None or porcelain:
        return BundleFreshness(
            fresh=False,
            current_commit=head,
            detail="uncommitted changes under the bundle",
        )
    return BundleFreshness(fresh=True, current_commit=head)
