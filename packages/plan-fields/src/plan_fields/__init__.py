"""plan-fields — offline parser + validator for the plan-fields v1 contract.

Standalone by design: no import of the dispatcher application. The v1 contract
(schema, registries, fixtures) is vendored under ``contract/`` (pinned copy).
"""

from __future__ import annotations

from plan_fields.canonical import canonical_dumps, canonicalize
from plan_fields.parser import parse_todo
from plan_fields.scrape import ScrapedItem, scrape_items
from plan_fields.validator import load_schema, run_conformance, validate_document

__version__ = "0.2.0"

__all__ = [
    "ScrapedItem",
    "__version__",
    "canonical_dumps",
    "canonicalize",
    "load_schema",
    "parse_todo",
    "run_conformance",
    "scrape_items",
    "validate_document",
]
