"""The advisory drift reporter for steward-roles-catalog/v1 (guarantee B).

The machinery is `gate_catalog_drift_report.py`'s, parameterized by
`ContractSpec`; its shared classification paths (unreadable manifest, missing
surface entry, unparseable YAML, …) are asserted in
`tests/test_gate_catalog_drift.py` and not repeated here. This suite asserts
what the roles spec changes: the shape probe (`version` + `roles`), the file
and re-vendor script named in reports, and that the real vendored copy
recognizes itself.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from gate_catalog_drift_report import DRIFT, NO_DRIFT, UNAVAILABLE, compare
from roles_catalog_drift_report import ROLES_CATALOG

_ROLES = (
    "version: 1\n"
    'slug_pattern: "^[a-z][a-z0-9-]{1,31}$"\n'
    "roles:\n"
    "  - {slug: owner, display: Solo owner}\n"
)
_PROVENANCE = {
    "commit": "b79c858dc5f5dc7651f15a1cdf3bcd51a1de2d16",
    "remote": "https://github.com/andrei-shtanakov/steward",
    "ref": "master",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write_upstream(tmp_path: Path, body: str) -> Path:
    upstream = tmp_path / "upstream" / "profiles" / "roles.yaml"
    upstream.parent.mkdir(parents=True)
    upstream.write_text(body)
    return upstream


def _write_vendored(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "vendored"
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "contract": "steward-roles-catalog",
        "contract_version": 1,
        "producer_commit": _PROVENANCE["commit"],
        "surface": [{"path": "roles.yaml", "sha256": _sha(body)}],
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


def test_matching_upstream_reports_no_drift(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path, _ROLES)
    vendored = _write_vendored(tmp_path, _ROLES)
    result = compare(upstream, vendored, _PROVENANCE, ROLES_CATALOG)
    assert result.outcome == NO_DRIFT
    assert result.exit_code == 0
    assert result.vendored_pin == _PROVENANCE["commit"]


def test_changed_upstream_content_is_drift_naming_this_contract(
    tmp_path: Path,
) -> None:
    """The report must point at THIS contract's file and re-vendor script,
    not the gate catalog's whose machinery it borrows."""
    upstream = _write_upstream(tmp_path, _ROLES + "  - {slug: qa, display: QA}\n")
    vendored = _write_vendored(tmp_path, _ROLES)
    result = compare(upstream, vendored, _PROVENANCE, ROLES_CATALOG)
    assert result.outcome == DRIFT
    assert result.exit_code == 1
    assert "roles.yaml" in result.summary
    assert "revendor_steward_roles_catalog.sh" in result.summary
    assert "gate-catalog" not in result.summary


def test_upstream_that_is_not_the_roles_catalog_is_unavailable(
    tmp_path: Path,
) -> None:
    """A moved layout hashes whatever the stale path points at; the shape
    probe must turn that into UNAVAILABLE, never a drift claim. The decoy
    here is the gate catalog itself — the sibling most likely to land on a
    confused path, and one that the gate-catalog spec's own probe accepts."""
    upstream = _write_upstream(
        tmp_path, "version: 1\nobligation_vocabulary:\n  - quality\n"
    )
    vendored = _write_vendored(tmp_path, _ROLES)
    result = compare(upstream, vendored, _PROVENANCE, ROLES_CATALOG)
    assert result.outcome == UNAVAILABLE
    assert result.exit_code == 2


def test_the_real_vendored_copy_recognizes_itself_as_no_drift(
    tmp_path: Path,
) -> None:
    """The reporter against the repo's actual vendored directory, with the
    vendored file itself standing in for upstream: shape probe, manifest
    lookup and hash all run on the real artifacts."""
    real = Path(__file__).parent.parent / "contracts" / "steward-roles-catalog" / "v1"
    upstream = _write_upstream(
        tmp_path, (real / "roles.yaml").read_text(encoding="utf-8")
    )
    result = compare(upstream, real, _PROVENANCE, ROLES_CATALOG)
    assert result.outcome == NO_DRIFT
