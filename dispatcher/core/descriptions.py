"""Project description extraction (DESIGN-801).

README-first (richer for onboarding), metadata fallback (terse but
authoritative) — the trade-off is named in the design spec. Reads ONLY
under the given project path; every failure degrades to the next source,
never raises.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from dispatcher.core.models import DescriptionSource

_MAX_SOURCE_BYTES = 256 * 1024
_TRIM_LIMIT = 360
_README_NAMES = ("readme.md", "readme.rst", "readme")  # priority order
_NOISE_PREFIXES = ("#", "[![", "![", "<")  # headings, badges, HTML/comments
_UNDERLINE_CHARS = set("=-~^\"'`*+")


def extract_project_description(
    path: Path,
) -> tuple[str | None, DescriptionSource | None]:
    """First meaningful README paragraph, else pyproject/package.json."""
    for text, source in (
        (_from_readme(path), "readme"),
        (_from_pyproject(path), "pyproject"),
        (_from_package_json(path), "package.json"),
    ):
        if text:
            return text, source
    return None, None


def _from_readme(path: Path) -> str | None:
    file = _find_readme(path)
    if file is None:
        return None
    text = _read_limited(file)
    return _first_paragraph(text) if text else None


def _find_readme(path: Path) -> Path | None:
    try:
        entries = {p.name.lower(): p for p in path.iterdir() if p.is_file()}
    except OSError:
        return None
    for name in _README_NAMES:
        if name in entries:
            return entries[name]
    return None


def _read_limited(file: Path) -> str | None:
    try:
        if file.stat().st_size > _MAX_SOURCE_BYTES:
            return None
        return file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _first_paragraph(text: str) -> str | None:
    para: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if para:
                break
            continue
        if _is_underline(stripped):
            if len(para) == 1:
                # setext/rst heading: the underline retroactively marks the
                # single collected line as a TITLE, not a paragraph
                para = []
                continue
            if para:
                break
            continue
        if stripped.startswith(_NOISE_PREFIXES):
            if para:
                break
            continue
        para.append(stripped)
    joined = " ".join(para).strip()
    return _trim(joined) if joined else None


def _is_underline(stripped: str) -> bool:
    """rst/setext title underlines and md rules: one repeated punct char."""
    return len(set(stripped)) == 1 and stripped[0] in _UNDERLINE_CHARS


def _trim(text: str) -> str:
    if len(text) <= _TRIM_LIMIT:
        return text
    head, _, _ = text[:_TRIM_LIMIT].rpartition(" ")
    return (head or text[:_TRIM_LIMIT]).rstrip() + "…"


def _from_pyproject(path: Path) -> str | None:
    text = _read_limited(path / "pyproject.toml")
    if text is None:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    desc = data.get("project", {}).get("description")
    return _clean_meta(desc)


def _from_package_json(path: Path) -> str | None:
    text = _read_limited(path / "package.json")
    if text is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    desc = data.get("description") if isinstance(data, dict) else None
    return _clean_meta(desc)


def _clean_meta(desc: object) -> str | None:
    if not isinstance(desc, str) or not desc.strip():
        return None
    return _trim(desc.strip())
