"""Invariant: the package never imports the dispatcher application (ADR-ECO-005 D9)."""

from __future__ import annotations

from pathlib import Path

import plan_fields


def test_package_does_not_reference_dispatcher():
    src = Path(plan_fields.__file__).parent
    offenders: list[str] = []
    for py in src.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        if "import dispatcher" in text or "from dispatcher" in text:
            offenders.append(str(py.relative_to(src)))
    assert not offenders, f"dispatcher import found in: {offenders}"
