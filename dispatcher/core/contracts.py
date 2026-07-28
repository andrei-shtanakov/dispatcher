"""Cross-repo contract status: catalog drift check + schema listing.

The drift check compares the SSOT catalog canon against an EXPLICIT
whitelist of vendored copies. Never search by filename: test fixtures
elsewhere carry the same name and must not produce false drift.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from dispatcher.core.models import ContractStatus

_CANON_PROJECT = "atp-platform"
_CANON_REL = Path("method/agents-catalog.toml")
_VENDORED_WHITELIST: dict[str, Path] = {
    "arbiter": Path("config/agents-catalog.toml"),
}
_SCHEMA_PROJECT = "spec-runner"
_SCHEMA_DIR = Path("schemas")

# plan-fields drift control (PF-6). The canonical fingerprint (manifest.json)
# lives in prograph-vault; the one vendored copy is dispatcher's own package.
# Neither is a "detected project", so resolve them from the monorepo layout
# (overridable via the projects map, which keeps this hermetically testable).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PF_CONTRACT_NAME = "plan-fields-v1"  # matches roadmap items' target_contract
_PF_CANON_PROJECT = "prograph-vault"
_PF_CANON_DIR_REL = Path("authored/contracts/plan-fields/v1")
_PF_MANIFEST_REL = _PF_CANON_DIR_REL / "manifest.json"
_PF_VENDORED_REL = Path("packages/plan-fields/src/plan_fields/contract")


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def check_contracts(projects: dict[str, Path]) -> list[ContractStatus]:
    """Build contract statuses for the detected projects."""
    results: list[ContractStatus] = []
    results.extend(_catalog_drift(projects))
    results.extend(_plan_fields_drift(projects))
    results.extend(_schema_listing(projects))
    return results


def _surface_drift(root: Path, surface: list[dict]) -> str | None:
    """First surface file under `root` whose sha256 ≠ the manifest, else None.

    Returns `"<path> missing"` when a file is absent, `"<path>"` when it
    differs — the caller turns this into the drift detail.
    """
    for entry in surface:
        rel = str(entry.get("path", ""))
        recorded = entry.get("sha256")
        actual = _sha256(root / rel)
        if actual is None:
            return f"{rel} missing"
        if actual != recorded:
            return rel
    return None


def _plan_fields_drift(projects: dict[str, Path]) -> list[ContractStatus]:
    """One `contract_in_sync` verdict for the plan-fields contract (PF-6).

    Compares the vendored contract surface against the canonical
    `manifest.json` fingerprint. A stale manifest (canon files changed but the
    fingerprint was not regenerated) is an immediate error, per
    `drift-control.md` — the guard must never certify a fingerprint it no longer
    matches.
    """
    vault = projects.get(_PF_CANON_PROJECT) or (_REPO_ROOT.parent / _PF_CANON_PROJECT)
    self_root = projects.get("dispatcher") or _REPO_ROOT
    manifest_path = vault / _PF_MANIFEST_REL
    vendored_dir = self_root / _PF_VENDORED_REL

    def status(in_sync: bool | None, detail: str | None) -> ContractStatus:
        return ContractStatus(
            name=_PF_CONTRACT_NAME,
            canonical_path=str(manifest_path),
            vendored_path=str(vendored_dir),
            in_sync=in_sync,
            detail=detail,
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [status(None, "canonical manifest not available")]
    surface = manifest.get("surface", [])
    stale = _surface_drift(vault / _PF_CANON_DIR_REL, surface)
    if stale is not None:
        return [status(False, f"canonical manifest stale: {stale}")]
    drifted = _surface_drift(vendored_dir, surface)
    if drifted is not None:
        return [status(False, f"vendored copy drifts at {drifted}")]
    return [status(True, None)]


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
