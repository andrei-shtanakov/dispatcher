"""Stream axis (`@epic` / `@defect`) — the epics/v1 half of a plan-fields node.

The grammar is NOT restated here. It is compiled at import time from the vendored
`contract_epics/classification.schema.json`, so "plan-fields delegates epic grammar to
epics/v1" is executable rather than documented: editing the pinned copy changes what this
module accepts, and a re-vendor is the only way to change it.

Layer boundary (ADR-ECO-010 D9, mirroring how v2 treats ``repo:<manifest-key>`` owners):
this module sees ONE repo, so it can prove a tag is present and well-formed and nothing
more. ``tagged`` from here means *well-formed and present*; whether the epic exists in the
registry is the fleet layer's question (`registry.py`), which may downgrade it to
``invalid`` with EP-UNKNOWN / EP-MOVED. A parser that claimed registry membership it never
read would be asserting, not parsing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

_CONTRACT_DIR = Path(__file__).parent / "contract_epics"
_CLASSIFICATION_SCHEMA = _CONTRACT_DIR / "classification.schema.json"

Classification = Literal["tagged", "missing", "invalid", "unavailable"]


def _compiled(defs: dict[str, Any], name: str) -> re.Pattern[str]:
    """Compile one grammar from the pinned contract, failing loudly if it moved.

    A missing `$def` means the vendored copy is not the contract this code was written
    against. Falling back to a hardcoded pattern here would silently recreate the second
    copy of the regex that the delegation exists to prevent, so this raises instead.
    """
    try:
        pattern = defs[name]["pattern"]
    except KeyError as exc:  # pragma: no cover - guards a broken vendored copy
        raise RuntimeError(
            f"vendored epics contract has no ${{defs}}/{name}.pattern "
            f"({_CLASSIFICATION_SCHEMA}); re-vendor from the pin"
        ) from exc
    return re.compile(pattern)


_DEFS = json.loads(_CLASSIFICATION_SCHEMA.read_text(encoding="utf-8"))["$defs"]
EPIC_RE = _compiled(_DEFS, "EpicId")
DEFECT_RE = _compiled(_DEFS, "DefectSlug")
CLASSIFICATION_STATES: tuple[str, ...] = tuple(_DEFS["ClassificationState"]["enum"])


def parse_epic(
    values: tuple[str, ...],
) -> tuple[str | None, Classification, str | None]:
    """Classify one item's `@epic` occurrences.

    Returns ``(epic, classification, diagnostic_code)``. Two occurrences are EP-MULTIPLE
    even when the values are identical: a duplicate is a defect in the record, not a
    consensus, and collapsing it would hide an edit that meant to replace, not repeat.
    """
    if not values:
        return None, "missing", "EP-MISSING"
    if len(values) > 1:
        return None, "invalid", "EP-MULTIPLE"
    value = values[0]
    if not EPIC_RE.fullmatch(value):
        return None, "invalid", "EP-GRAMMAR"
    return value, "tagged", None


def parse_defect(values: tuple[str, ...]) -> tuple[str | None, str | None]:
    """Classify one item's `@defect` occurrences — orthogonal to the epic.

    Returns ``(defect, diagnostic_code)``. The defect axis fails on its own: a bad
    `@defect` never changes the item's `epic_classification`, because the two answer
    different questions ("which stream" vs "what broke") and folding them would delete
    in-epic defects from the defect-class counts.
    """
    if not values:
        return None, None
    if len(values) > 1:
        return None, "EP-DEFECT-MULTIPLE"
    value = values[0]
    if not DEFECT_RE.fullmatch(value):
        return None, "EP-DEFECT-GRAMMAR"
    return value, None


EPIC_MESSAGES = {
    "EP-MISSING": "item {node_id} carries no @epic",
    "EP-MULTIPLE": "item carries {count} @epic tags ({values})",
    "EP-GRAMMAR": "@epic {value} does not match <program>.<epic> (epics/v1)",
    "EP-DEFECT-GRAMMAR": "@defect {value} does not match the defect slug grammar (epics/v1)",
    "EP-DEFECT-MULTIPLE": "item carries {count} @defect tags ({values})",
}

EPIC_SEVERITY = {
    # EP-MISSING is the one code the adoption period defers (ADR-ECO-010 D8); the
    # policy date lives in the registry, so escalation is the fleet layer's call.
    "EP-MISSING": "warning",
    "EP-MULTIPLE": "error",
    "EP-GRAMMAR": "error",
    "EP-DEFECT-GRAMMAR": "error",
    "EP-DEFECT-MULTIPLE": "error",
}
