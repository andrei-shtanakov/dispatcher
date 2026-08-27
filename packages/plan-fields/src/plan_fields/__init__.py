"""plan-fields — offline parser + validator for the plan-fields v3 contract.

Standalone by design: no import of the dispatcher application. The v3 contract
(schema, registries, fixtures) is vendored under ``contract/`` (pinned copy), and the
epics/v1 contract it delegates the stream axis to is vendored beside it under
``contract_epics/`` — the parser compiles the epic/defect grammar out of that copy, so
the delegation is executable rather than documented.
"""

from __future__ import annotations

from plan_fields.canonical import canonical_dumps, canonicalize
from plan_fields.epic import parse_defect, parse_epic
from plan_fields.fleet import (
    AmbiguousIdentityError,
    ManifestIndex,
    checkout_map,
    manifest_index,
    resolve_checkout,
)
from plan_fields.fleet_api import (
    LegacyDiagnostic,
    RepoInput,
    check_fleet,
    check_legacy_fleet,
    parse_fleet,
)
from plan_fields.parser import parse_dag, parse_owner, parse_todo
from plan_fields.registry import EpicsRegistry, apply_registry, load_registry
from plan_fields.scrape import ScrapedItem, last_tag_is_quoted, scrape_items
from plan_fields.validator import load_schema, run_conformance, validate_document

__version__ = "0.10.0"

__all__ = [
    "AmbiguousIdentityError",
    "EpicsRegistry",
    "LegacyDiagnostic",
    "ManifestIndex",
    "RepoInput",
    "ScrapedItem",
    "__version__",
    "apply_registry",
    "canonical_dumps",
    "canonicalize",
    "check_fleet",
    "check_legacy_fleet",
    "checkout_map",
    "last_tag_is_quoted",
    "load_registry",
    "load_schema",
    "manifest_index",
    "parse_dag",
    "parse_defect",
    "parse_epic",
    "parse_fleet",
    "parse_owner",
    "parse_todo",
    "resolve_checkout",
    "run_conformance",
    "scrape_items",
    "validate_document",
]
