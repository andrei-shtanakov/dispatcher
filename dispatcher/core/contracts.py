"""Cross-repo contract status: catalog drift check + schema listing.

The drift check compares the SSOT catalog canon against an EXPLICIT
whitelist of vendored copies. Never search by filename: test fixtures
elsewhere carry the same name and must not produce false drift.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
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
_PF_CONTRACT_NAME = "plan-fields-v3"  # matches ADR-ECO-005a contract identity
_EPICS_CONTRACT_NAME = "epics-v1"  # ADR-ECO-010; delegated grammar, vendored beside it
# What the vendored manifest must declare itself to be. Checked by the product,
# not only by a test against the real file: the PINNED.txt cross-reference
# takes its authority FROM the manifest, so a manifest that keeps a
# self-consistent surface while declaring another contract — or none — would
# disarm that cross-check without failing anything else.
_PF_MANIFEST_CONTRACT = "plan-fields"
_PF_MANIFEST_VERSION = 3
_PF_CANON_PROJECT = "prograph-vault"
_PF_CANON_DIR_REL = Path("authored/contracts/plan-fields/v3")
_PF_MANIFEST_REL = _PF_CANON_DIR_REL / "manifest.json"
_PF_VENDORED_REL = Path("packages/plan-fields/src/plan_fields/contract")
# The epics/v1 contract is vendored ALONGSIDE plan-fields v3, which delegates the
# stream-axis grammar to it. Two vendored copies mean two of each guarantee: a
# second contract sitting in the tree with neither integrity nor drift coverage
# would be exactly the "checked by nothing" hole this module was hardened against.
_EPICS_VENDORED_REL = Path("packages/plan-fields/src/plan_fields/contract_epics")
_EPICS_CANON_DIR_REL = Path("authored/contracts/epics/v1")
# Excluded from the fingerprinted surface (parity with the vault generator):
# drift-control meta + the vendor-only pin marker. The exclusion is
# legitimate — a manifest cannot hash itself, and policy/provenance are not
# contract — but "excluded from the fingerprint" had quietly become "checked
# by nothing", and that cost twice: a PINNED.txt naming a commit nobody
# verified, and a drift-control.md describing the folded check #99 removed,
# shipped inside the package while both guarantees stayed green.
#
# So every exclusion now declares how it IS checked, and `_PF_META` is derived
# from those tables rather than written out — a fourth excluded file cannot
# exist until someone says which check covers it.
_PF_META_HASHES = {
    # Recorded when vendored; a re-vendor that updates the file and forgets
    # this line fails its own test rather than certifying a stale copy.
    "drift-control.md": (
        "d0dcc015f94fe34106d3e055a427a83fb4fb0a5a2eb057716f2d6a32a3dc89cb"
    ),
}
_EPICS_META_HASHES = {
    "drift-control.md": (
        "5d3e074e9cf647fb2bb3ee3d35afd5397680d8b4a8a819e9777560ccb1b4b9b5"
    ),
}
# Checked by shape and cross-reference instead of by hash: `manifest.json`
# cannot contain its own digest, and `PINNED.txt` is provenance whose value is
# whether it agrees with the manifest, not whether it is byte-frozen.
_PF_META_STRUCTURAL = {"manifest.json", "PINNED.txt"}
_PF_META = set(_PF_META_HASHES) | _PF_META_STRUCTURAL
_KIND_INTEGRITY = "vendored_integrity"
_KIND_DRIFT = "upstream_drift"
_KIND_LISTING = "listing"


@dataclass(frozen=True)
class _VendoredContract:
    """One pinned contract copy and everything both guarantees need about it.

    The verdicts used to read module constants directly, which silently made
    "the vendored contract" mean "the only one". Naming the copies in a table is
    what lets a second one be covered instead of merely present.
    """

    name: str
    manifest_contract: str
    manifest_version: int
    vendored_rel: Path
    canon_dir_rel: Path
    meta_hashes: dict[str, str]

    @property
    def manifest_rel(self) -> Path:
        return self.canon_dir_rel / "manifest.json"

    @property
    def meta(self) -> set[str]:
        return set(self.meta_hashes) | _PF_META_STRUCTURAL


_VENDORED_CONTRACTS = (
    _VendoredContract(
        name=_PF_CONTRACT_NAME,
        manifest_contract=_PF_MANIFEST_CONTRACT,
        manifest_version=_PF_MANIFEST_VERSION,
        vendored_rel=_PF_VENDORED_REL,
        canon_dir_rel=_PF_CANON_DIR_REL,
        meta_hashes=_PF_META_HASHES,
    ),
    _VendoredContract(
        name=_EPICS_CONTRACT_NAME,
        manifest_contract="epics",
        manifest_version=1,
        vendored_rel=_EPICS_VENDORED_REL,
        canon_dir_rel=_EPICS_CANON_DIR_REL,
        meta_hashes=_EPICS_META_HASHES,
    ),
)


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


def _canon_stale(
    canon_dir: Path, surface: list[dict], meta: set[str] | None = None
) -> str | None:
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
        if p.is_file() and p.relative_to(canon_dir).as_posix() not in (meta or _PF_META)
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


def _live_surface(root: Path, meta: set[str] | None = None) -> dict[str, str | None]:
    """Every non-meta file under `root`, by relative path, hashed."""
    return {
        p.relative_to(root).as_posix(): _sha256(p)
        for p in root.rglob("*")
        if p.is_file() and p.relative_to(root).as_posix() not in (meta or _PF_META)
    }


def _pin_problem(vendored_dir: Path, contract: str) -> str | None:
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
    source = _PIN_SOURCE_RE.search(pin)
    if source is None:
        return "PINNED.txt names no `source:` upstream"
    # Cross-reference, not shape: a well-formed pin pointing at some other
    # contract is exactly as wrong as a missing one, and only the manifest
    # knows which contract this copy is supposed to be.
    if contract not in source.group(0):
        return (
            f"PINNED.txt `source:` does not name the contract the manifest "
            f"declares ({contract})"
        )
    if not _PIN_COMMIT_RE.search(pin):
        return "PINNED.txt states no `commit:` as a full 40-hex sha"
    return None


def _pf_vendored_integrity(
    spec: _VendoredContract, vendored_dir: Path
) -> ContractStatus:
    """A: the vendored surface matches the manifest travelling with it.

    Reads nothing outside dispatcher and never returns "cannot compare": the
    copy is ours, so an unreadable manifest is a broken copy, not an unknown.
    `None` here would be the old skip wearing a new hat.
    """
    manifest_path = vendored_dir / "manifest.json"

    def status(in_sync: bool | None, detail: str | None) -> ContractStatus:
        return ContractStatus(
            name=spec.name,
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
    declared = manifest.get("contract")
    if declared != spec.manifest_contract:
        return status(
            False,
            f"vendored manifest declares contract {declared!r}, "
            f"not {spec.manifest_contract!r}",
        )
    version = manifest.get("contract_version")
    if version != spec.manifest_version:
        return status(
            False,
            f"vendored manifest declares contract version {version!r}, "
            f"not {spec.manifest_version!r}",
        )
    surface = manifest.get("surface", [])
    if not _valid_surface(surface):
        return status(False, "vendored manifest surface is malformed")
    recorded = manifest.get("tree_sha256")
    if not isinstance(recorded, str) or _tree_sha256(surface) != recorded:
        return status(
            False, "manifest tree_sha256 does not fingerprint its own surface"
        )
    live = _live_surface(vendored_dir, spec.meta)
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
    for name, expected in sorted(spec.meta_hashes.items()):
        actual = _sha256(vendored_dir / name)
        if actual is None:
            return status(False, f"excluded file is absent or unreadable: {name}")
        if actual != expected:
            return status(False, f"excluded file differs from its record: {name}")
    pin = _pin_problem(vendored_dir, spec.manifest_contract)
    if pin is not None:
        return status(False, pin)
    return status(True, None)


def _pf_upstream_drift(
    vault: Path | None, spec: _VendoredContract, vendored_dir: Path
) -> ContractStatus:
    """B: an observation about canon, never a fallback to a sibling on disk.

    Only answers when a canon checkout is handed over explicitly. Reaching for
    `../prograph-vault` is what made the same dispatcher commit answer
    differently on two machines and vanish into a skip on CI.
    """
    manifest_path = None if vault is None else vault / spec.manifest_rel

    def status(in_sync: bool | None, detail: str | None) -> ContractStatus:
        return ContractStatus(
            name=spec.name,
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
    stale = _canon_stale(vault / spec.canon_dir_rel, surface, spec.meta)
    if stale is not None:
        return status(False, f"canonical manifest stale: {stale}")
    drifted = _surface_drift(vendored_dir, surface)
    if drifted is not None:
        return status(False, f"vendored copy drifts at {drifted}")
    return status(True, None)


def _plan_fields_rows(projects: dict[str, Path]) -> list[ContractStatus]:
    """Both PF-6 verdicts, per vendored contract, side by side and never folded.

    Two rows per copy, not one verdict per copy: integrity is offline and provable,
    upstream drift needs canon and may honestly be unknown. Collapsing them would
    let "no canon available" read as "in sync" — the failure #99 removed.
    """
    self_root = projects.get("dispatcher") or _REPO_ROOT
    vault = projects.get(_PF_CANON_PROJECT)
    rows: list[ContractStatus] = []
    for spec in _VENDORED_CONTRACTS:
        vendored_dir = self_root / spec.vendored_rel
        rows.append(_pf_vendored_integrity(spec, vendored_dir))
        rows.append(_pf_upstream_drift(vault, spec, vendored_dir))
    return rows


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
