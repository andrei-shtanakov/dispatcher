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


def collect_product_proposals(mirror_root: Path) -> ProductProposalsReport:
    """Scan the impresario mirror and classify every proposal bundle.

    Filled in by Tasks 5–7 of the implementation plan; this stub keeps the
    module importable while the pieces land test-first.
    """
    raise NotImplementedError
