"""Invariant: __version__ matches pyproject.toml (they drifted once, I3)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import plan_fields


def test_dunder_version_matches_pyproject():
    pyproject = Path(plan_fields.__file__).parent.parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert plan_fields.__version__ == data["project"]["version"]
