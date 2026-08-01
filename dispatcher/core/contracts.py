"""Cross-repo contract status: catalog drift check + schema listing.

The drift check compares the SSOT catalog canon against an EXPLICIT
whitelist of vendored copies. Never search by filename: test fixtures
elsewhere carry the same name and must not produce false drift.
"""

from __future__ import annotations

import hashlib
import json
import re
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
# excluded from the fingerprinted surface (parity with the vault generator):
# drift-control meta + the vendor-only pin marker.
_PF_META = {"manifest.json", "drift-control.md", "PINNED.txt"}
_KIND_INTEGRITY = "vendored_integrity"
_KIND_DRIFT = "upstream_drift"
_KIND_LISTING = "listing"
_PIN_COMMIT_RE = re.compile(r"^commit:\s*[0-9a-f]{40}\s*$", re.M)
_PIN_SOURCE_RE = re.compile(r"^source:\s*\S.*$", re.M)


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def check_contracts(projects: dict[str, Path]) -> list[ContractStatus]:
    """Build contract statuses for the detected projects."""
    results: list[ContractStatus] = []
    results.extend(_catalog_drift(projects))
    results.extend(_plan_fields_rows(projects))
    results.extend(_schema_listing(projects))
    return results


def _valid_surface(surface: object) -> bool:
    """A manifest `surface` is a list of {path: non-empty str, sha256: str}."""
    return isinstance(surface, list) and all(
        isinstance(e, dict)
        and isinstance(e.get("path"), str)
        and bool(e.get("path"))
        and isinstance(e.get("sha256"), str)
        for e in surface
    )


def _surface_drift(root: Path, surface: list[dict]) -> str | None:
    """First surface file under `root` whose sha256 ≠ the manifest, else None.

    Assumes a validated surface (see `_valid_surface`). Returns
    `"<path> missing"` when a file is absent, `"<path>"` when it differs — the
    caller turns this into the drift detail.
    """
    for entry in surface:
        rel = str(entry["path"])
        actual = _sha256(root / rel)
        if actual is None:
            return f"{rel} missing"
        if actual != entry["sha256"]:
            return rel
    return None


def _canon_stale(canon_dir: Path, surface: list[dict]) -> str | None:
    """Reason the manifest no longer matches the live canon surface, else None.

    Compares the *whole* live surface (every file under `canon_dir` minus the
    meta files) against the manifest — by set first, so an ADDED or REMOVED
    canon file is caught even though it is absent from `surface`, then by hash.
    A stale manifest must never certify (drift-control.md).
    """
    recorded = {e["path"]: e["sha256"] for e in surface}
    live = {
        p.relative_to(canon_dir).as_posix(): _sha256(p)
        for p in canon_dir.rglob("*")
        if p.is_file() and p.relative_to(canon_dir).as_posix() not in _PF_META
    }
    if set(live) != set(recorded):
        added = sorted(set(live) - set(recorded))
        removed = sorted(set(recorded) - set(live))
        return f"surface set changed (added={added}, removed={removed})"
    for rel, digest in live.items():
        if digest != recorded[rel]:
            return rel
    return None


def _tree_sha256(surface: list[dict]) -> str:
    """The fingerprint of a surface list — same recipe as the vault generator."""
    digest = hashlib.sha256()
    for entry in surface:
        digest.update(f"{entry['path']}\0{entry['sha256']}\n".encode())
    return digest.hexdigest()


def _live_surface(root: Path) -> dict[str, str | None]:
    """Every non-meta file under `root`, by relative path, hashed."""
    return {
        p.relative_to(root).as_posix(): _sha256(p)
        for p in root.rglob("*")
        if p.is_file() and p.relative_to(root).as_posix() not in _PF_META
    }


def _pin_problem(vendored_dir: Path) -> str | None:
    """Why `PINNED.txt` fails to state a reviewable provenance, else None.

    This is provenance a reviewer can follow, not an attestation: it says
    which vault commit the copy claims to come from. Nothing here proves the
    claim — proving it needs a signature or a checkout of that commit — but a
    copy that names no revision cannot even be checked by hand.
    """
    try:
        pin = (vendored_dir / "PINNED.txt").read_text(encoding="utf-8")
    except OSError:
        return "PINNED.txt is absent, so the copy states no provenance"
    if not _PIN_SOURCE_RE.search(pin):
        return "PINNED.txt names no `source:` upstream"
    if not _PIN_COMMIT_RE.search(pin):
        return "PINNED.txt states no `commit:` as a full 40-hex sha"
    return None


def _pf_vendored_integrity(vendored_dir: Path) -> ContractStatus:
    """A: the vendored surface matches the manifest travelling with it.

    Reads nothing outside dispatcher and never returns "cannot compare": the
    copy is ours, so an unreadable manifest is a broken copy, not an unknown.
    `None` here would be the old skip wearing a new hat.
    """
    manifest_path = vendored_dir / "manifest.json"

    def status(in_sync: bool | None, detail: str | None) -> ContractStatus:
        return ContractStatus(
            name=_PF_CONTRACT_NAME,
            canonical_path=str(manifest_path),
            vendored_path=str(vendored_dir),
            kind=_KIND_INTEGRITY,
            in_sync=in_sync,
            detail=detail,
        )

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError:
        return status(False, "vendored manifest.json is absent")
    except json.JSONDecodeError as exc:
        return status(False, f"vendored manifest.json is unreadable: {exc.msg}")
    surface = manifest.get("surface", [])
    if not _valid_surface(surface):
        return status(False, "vendored manifest surface is malformed")
    recorded = manifest.get("tree_sha256")
    if not isinstance(recorded, str) or _tree_sha256(surface) != recorded:
        return status(
            False, "manifest tree_sha256 does not fingerprint its own surface"
        )
    live = _live_surface(vendored_dir)
    listed = {str(e["path"]): str(e["sha256"]) for e in surface}
    if set(live) != set(listed):
        unlisted = sorted(set(live) - set(listed))
        absent = sorted(set(listed) - set(live))
        return status(
            False,
            f"surface set differs from the manifest "
            f"(unfingerprinted={unlisted}, missing={absent})",
        )
    for rel, digest in sorted(live.items()):
        if digest is None:
            # No hash exists to disagree with. Calling this a fingerprint
            # mismatch sends a reviewer hunting a difference that is not there;
            # the fix is a permissions problem, not a re-vendor.
            return status(False, f"vendored file is unreadable: {rel}")
        if digest != listed[rel]:
            return status(False, f"vendored file differs from its fingerprint: {rel}")
    pin = _pin_problem(vendored_dir)
    if pin is not None:
        return status(False, pin)
    return status(True, None)


def _pf_upstream_drift(vault: Path | None, vendored_dir: Path) -> ContractStatus:
    """B: an observation about canon, never a fallback to a sibling on disk.

    Only answers when a canon checkout is handed over explicitly. Reaching for
    `../prograph-vault` is what made the same dispatcher commit answer
    differently on two machines and vanish into a skip on CI.
    """
    manifest_path = None if vault is None else vault / _PF_MANIFEST_REL

    def status(in_sync: bool | None, detail: str | None) -> ContractStatus:
        return ContractStatus(
            name=_PF_CONTRACT_NAME,
            canonical_path="" if manifest_path is None else str(manifest_path),
            vendored_path=str(vendored_dir),
            kind=_KIND_DRIFT,
            in_sync=in_sync,
            detail=detail,
        )

    if vault is None or manifest_path is None:
        return status(None, "no canon checkout provided; upstream drift unknown")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return status(None, "canonical manifest not available")
    surface = manifest.get("surface", [])
    if not _valid_surface(surface):
        return status(None, "malformed manifest surface")
    stale = _canon_stale(vault / _PF_CANON_DIR_REL, surface)
    if stale is not None:
        return status(False, f"canonical manifest stale: {stale}")
    drifted = _surface_drift(vendored_dir, surface)
    if drifted is not None:
        return status(False, f"vendored copy drifts at {drifted}")
    return status(True, None)


def _plan_fields_rows(projects: dict[str, Path]) -> list[ContractStatus]:
    """Both PF-6 verdicts, side by side and never folded together."""
    self_root = projects.get("dispatcher") or _REPO_ROOT
    vendored_dir = self_root / _PF_VENDORED_REL
    return [
        _pf_vendored_integrity(vendored_dir),
        _pf_upstream_drift(projects.get(_PF_CANON_PROJECT), vendored_dir),
    ]


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
                kind=_KIND_DRIFT,
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
            kind=_KIND_LISTING,
            detail="published schema",
        )
        for f in sorted(schema_dir.glob("*.json"))
    ]
