"""plan-fields — offline parser + validator for the plan-fields v2 contract.

Standalone by design: no import of the dispatcher application. The v2 contract
(schema, registries, fixtures) is vendored under ``contract/`` (pinned copy).
"""

from __future__ import annotations

from plan_fields.canonical import canonical_dumps, canonicalize
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
from plan_fields.parser import parse_todo
from plan_fields.scrape import ScrapedItem, scrape_items
from plan_fields.validator import load_schema, run_conformance, validate_document

__version__ = "0.8.0"

__all__ = [
    "AmbiguousIdentityError",
    "LegacyDiagnostic",
    "ManifestIndex",
    "RepoInput",
    "ScrapedItem",
    "__version__",
    "canonical_dumps",
    "canonicalize",
    "check_fleet",
    "check_legacy_fleet",
    "checkout_map",
    "load_schema",
    "manifest_index",
    "parse_fleet",
    "parse_todo",
    "resolve_checkout",
    "run_conformance",
    "scrape_items",
    "validate_document",
]
