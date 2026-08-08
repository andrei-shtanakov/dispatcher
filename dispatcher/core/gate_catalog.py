"""Vendored steward gate-catalog v2: the canonical obligation vocabulary.

Inbox #125 (ADR-ECO-006, D7 of steward's gate-id-catalog design). Steward's
``profiles/gate-catalog.yaml`` is the SSOT for gate identity; its
``obligation_vocabulary`` is what a gate-verdicts finding's ``obligation``
field may say. This module reads only the *vendored* copy
(``contracts/steward-gate-catalog/v1/``) — no sibling-repo path is ever
resolved (CON-03), same runtime pattern as ``core/governance.py`` reading
the vendored gate-verdicts schema.

A missing or unparseable vendored copy is a broken installation, not a
bundle state: loading raises instead of degrading, exactly as the vendored
``SCHEMA.json`` does on the verdicts path.
"""

from __future__ import annotations

import functools
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

_CATALOG_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "steward-gate-catalog"
    / "v1"
    / "gate-catalog.yaml"
)
SUPPORTED_CATALOG_VERSION = 2


class GateEntry(BaseModel):
    """One catalog gate: its obligation, lifecycle status and stages."""

    model_config = ConfigDict(extra="forbid")

    obligation: str
    status: str
    title: str
    stages: list[str]
    applicable_roles: list[str] | None = None
    # Deprecated gates carry `since` plus exactly one of `replaced_by` /
    # `replacement: none` (catalog stability policy); none exist in v1 yet.
    since: str | None = None
    replaced_by: str | None = None
    replacement: str | None = None


class GateCatalog(BaseModel):
    """The typed catalog: vocabularies plus the per-gate entries."""

    model_config = ConfigDict(extra="forbid")

    version: int
    obligation_vocabulary: list[str]
    stage_vocabulary: list[str]
    gates: dict[str, GateEntry]


@functools.cache
def load_catalog() -> GateCatalog:
    """The vendored catalog, parsed and version-checked once per process.

    A catalog whose ``version`` is not the supported one raises: byte-copying
    a future catalog into the v1 directory would otherwise silently change
    what this consumer claims to understand.
    """
    data = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8"))
    catalog = GateCatalog.model_validate(data)
    if catalog.version != SUPPORTED_CATALOG_VERSION:
        raise ValueError(
            f"vendored gate-catalog declares version {catalog.version}; "
            f"this consumer is pinned to v{SUPPORTED_CATALOG_VERSION} "
            "(contracts/steward-gate-catalog/v1/)"
        )
    return catalog


def obligation_vocabulary() -> frozenset[str]:
    """The canonical set a finding's ``obligation`` value may come from."""
    return frozenset(load_catalog().obligation_vocabulary)
