"""Cross-repo contract status: catalog drift check + schema listing.

The drift check compares the SSOT catalog canon against an EXPLICIT
whitelist of vendored copies. Never search by filename: test fixtures
elsewhere carry the same name and must not produce false drift.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from dispatcher.core.models import ContractStatus

_CANON_PROJECT = "atp-platform"
_CANON_REL = Path("method/agents-catalog.toml")
_VENDORED_WHITELIST: dict[str, Path] = {
    "arbiter": Path("config/agents-catalog.toml"),
}
_SCHEMA_PROJECT = "spec-runner"
_SCHEMA_DIR = Path("schemas")


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def check_contracts(projects: dict[str, Path]) -> list[ContractStatus]:
    """Build contract statuses for the detected projects."""
    results: list[ContractStatus] = []
    results.extend(_catalog_drift(projects))
    results.extend(_schema_listing(projects))
    return results


def _catalog_drift(projects: dict[str, Path]) -> list[ContractStatus]:
    canon_root = projects.get(_CANON_PROJECT)
    canon = None if canon_root is None else canon_root / _CANON_REL
    canon_hash = None if canon is None else _sha256(canon)
    results: list[ContractStatus] = []
    for project, rel in _VENDORED_WHITELIST.items():
        root = projects.get(project)
        if root is None:
            continue
        vendored = root / rel
        vendored_hash = _sha256(vendored)
        in_sync = (
            None
            if canon_hash is None or vendored_hash is None
            else canon_hash == vendored_hash
        )
        detail = None
        if canon_hash is None:
            detail = "canon not available"
        elif vendored_hash is None:
            detail = "vendored copy missing"
        results.append(
            ContractStatus(
                name="agents-catalog",
                canonical_path="" if canon is None else str(canon),
                vendored_path=str(vendored),
                in_sync=in_sync,
                detail=detail,
            )
        )
    return results


def _schema_listing(projects: dict[str, Path]) -> list[ContractStatus]:
    root = projects.get(_SCHEMA_PROJECT)
    if root is None:
        return []
    schema_dir = root / _SCHEMA_DIR
    if not schema_dir.is_dir():
        return []
    return [
        ContractStatus(
            name=f.name,
            canonical_path=str(f),
            detail="published schema",
        )
        for f in sorted(schema_dir.glob("*.json"))
    ]
