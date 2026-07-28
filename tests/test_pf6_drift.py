"""PF-6 executable half: plan-fields contract drift check (ADR-ECO-005).

The vault owns the canonical fingerprint (manifest.json); this check is the
dispatcher companion that compares a vendored copy against it and emits one
`contract_in_sync` verdict named `plan-fields-v1` (linking to the roadmap items'
target_contract).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from dispatcher.core.contracts import check_contracts

_CANON_REL = "authored/contracts/plan-fields/v1"
_VENDORED_REL = "packages/plan-fields/src/plan_fields/contract"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write_contract(root: Path, files: dict[str, str]) -> None:
    """Write a contract surface + a manifest fingerprint of it at `root`."""
    cdir = root / _CANON_REL
    for rel, body in files.items():
        p = cdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    surface = [{"path": r, "sha256": _sha(files[r])} for r in sorted(files)]
    h = hashlib.sha256()
    for e in surface:
        h.update(f"{e['path']}\0{e['sha256']}\n".encode())
    manifest = {
        "contract": "plan-fields",
        "contract_version": 1,
        "tree_sha256": h.hexdigest(),
        "surface": surface,
    }
    (cdir / "manifest.json").write_text(json.dumps(manifest))


def _write_vendored(root: Path, files: dict[str, str]) -> None:
    vdir = root / _VENDORED_REL
    for rel, body in files.items():
        p = vdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def _pf(results):
    return next(r for r in results if r.name == "plan-fields-v1")


_SURFACE = {"schema.json": '{"x":1}', "fixtures/valid/basic.md": "- [x] a\n"}


def _projects(tmp_path: Path) -> dict[str, Path]:
    return {"prograph-vault": tmp_path / "vault", "dispatcher": tmp_path / "self"}


def test_in_sync_when_vendored_matches_manifest(tmp_path: Path) -> None:
    _write_contract(tmp_path / "vault", _SURFACE)
    _write_vendored(tmp_path / "self", _SURFACE)
    row = _pf(check_contracts(_projects(tmp_path)))
    assert row.in_sync is True and row.detail is None


def test_drift_when_a_vendored_file_differs(tmp_path: Path) -> None:
    _write_contract(tmp_path / "vault", _SURFACE)
    tampered = {**_SURFACE, "schema.json": '{"x":2}'}  # consumer edited canon copy
    _write_vendored(tmp_path / "self", tampered)
    row = _pf(check_contracts(_projects(tmp_path)))
    assert row.in_sync is False
    assert "schema.json" in (row.detail or "")


def test_drift_when_a_vendored_file_is_missing(tmp_path: Path) -> None:
    _write_contract(tmp_path / "vault", _SURFACE)
    _write_vendored(tmp_path / "self", {"schema.json": '{"x":1}'})  # no fixtures/
    row = _pf(check_contracts(_projects(tmp_path)))
    assert row.in_sync is False
    assert "missing" in (row.detail or "")


def test_canon_manifest_absent_is_not_comparable(tmp_path: Path) -> None:
    _write_vendored(tmp_path / "self", _SURFACE)  # vendored exists, no vault canon
    row = _pf(check_contracts(_projects(tmp_path)))
    assert row.in_sync is None
    assert "not available" in (row.detail or "")


def test_stale_manifest_on_content_change_is_an_error(tmp_path: Path) -> None:
    # canon surface changed but the fingerprint was not regenerated: the guard
    # must never certify a fingerprint it no longer matches (drift-control.md).
    _write_contract(tmp_path / "vault", _SURFACE)
    (tmp_path / "vault" / _CANON_REL / "schema.json").write_text('{"x":999}')
    _write_vendored(tmp_path / "self", _SURFACE)
    row = _pf(check_contracts(_projects(tmp_path)))
    assert row.in_sync is False
    assert "stale" in (row.detail or "")


def test_stale_manifest_on_added_canon_file_is_an_error(tmp_path: Path) -> None:
    # a NEW canon file with no manifest regen is also stale — the guard checks
    # the whole live surface by set, not only the files listed in the manifest.
    _write_contract(tmp_path / "vault", _SURFACE)
    (tmp_path / "vault" / _CANON_REL / "extra.md").write_text("new surface file\n")
    _write_vendored(tmp_path / "self", _SURFACE)
    row = _pf(check_contracts(_projects(tmp_path)))
    assert row.in_sync is False
    assert "stale" in (row.detail or "") and "extra.md" in (row.detail or "")


def test_malformed_manifest_is_not_comparable(tmp_path: Path) -> None:
    _write_contract(tmp_path / "vault", _SURFACE)
    mpath = tmp_path / "vault" / _CANON_REL / "manifest.json"
    mpath.write_text(json.dumps({"surface": ["not-a-dict"]}))  # corrupt
    _write_vendored(tmp_path / "self", _SURFACE)
    row = _pf(check_contracts(_projects(tmp_path)))
    assert row.in_sync is None
    assert "malformed" in (row.detail or "")


def test_real_vendored_copy_is_in_sync_with_canon() -> None:
    """Dogfood: dispatcher's own vendored plan-fields copy matches canon under
    the monorepo layout (fallback paths), so PF-6's guard starts green and the
    roadmap items targeting plan-fields-v1 are not flipped to drift."""
    repo_root = Path(__file__).resolve().parents[1]
    canon_manifest = repo_root.parent / "prograph-vault" / _CANON_REL / "manifest.json"
    if not canon_manifest.exists():
        pytest.skip("prograph-vault not checked out (standalone layout)")
    row = _pf(check_contracts({}))  # empty → uses monorepo-layout fallbacks
    assert row.in_sync is True, row.detail
