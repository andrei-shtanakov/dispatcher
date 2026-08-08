"""The advisory drift reporter for steward-gate-catalog/v1 (guarantee B).

Structural difference from the directory-shaped siblings: the surface is a
single file, so the comparison is one sha256 against the vendored manifest's
entry, and the probe is the file's own shape (a mapping with `version` and
`obligation_vocabulary`) — a moved upstream layout must classify as
UNAVAILABLE, never as DRIFT.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from gate_catalog_drift_report import DRIFT, NO_DRIFT, UNAVAILABLE, compare

_CATALOG = "version: 1\nobligation_vocabulary:\n  - quality\n  - approval\n"
_PROVENANCE = {
    "commit": "c26ca38f7f318b6f4849540a50a6cacf4c98f20b",
    "remote": "https://github.com/andrei-shtanakov/steward",
    "ref": "master",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write_upstream(tmp_path: Path, body: str) -> Path:
    upstream = tmp_path / "upstream" / "profiles" / "gate-catalog.yaml"
    upstream.parent.mkdir(parents=True)
    upstream.write_text(body)
    return upstream


def _write_vendored(tmp_path: Path, body: str, *, manifest: object = ...) -> Path:
    """A vendored copy carrying only what `compare()` actually reads: its
    manifest. `manifest=None` omits the file entirely (unreadable); the
    default derives one from `body` so callers get a matching pair."""
    root = tmp_path / "vendored"
    root.mkdir(parents=True, exist_ok=True)
    if manifest is ...:
        manifest = {
            "contract": "steward-gate-catalog",
            "contract_version": 1,
            "producer_commit": _PROVENANCE["commit"],
            "surface": [{"path": "gate-catalog.yaml", "sha256": _sha(body)}],
        }
    if manifest is not None:
        (root / "manifest.json").write_text(json.dumps(manifest))
    return root


def test_matching_upstream_reports_no_drift(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path, _CATALOG)
    vendored = _write_vendored(tmp_path, _CATALOG)
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == NO_DRIFT
    assert result.exit_code == 0
    assert result.vendored_pin == _PROVENANCE["commit"]


def test_changed_upstream_content_is_drift_naming_the_file(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path, _CATALOG + "stage_vocabulary: [release]\n")
    vendored = _write_vendored(tmp_path, _CATALOG)
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == DRIFT
    assert result.exit_code == 1
    assert "gate-catalog.yaml" in result.summary
    assert "revendor_steward_gate_catalog.sh" in result.summary


def test_missing_upstream_file_is_unavailable_not_drift(tmp_path: Path) -> None:
    vendored = _write_vendored(tmp_path, _CATALOG)
    result = compare(tmp_path / "nope" / "gate-catalog.yaml", vendored, _PROVENANCE)
    assert result.outcome == UNAVAILABLE
    assert result.exit_code == 2


def test_upstream_that_is_not_the_catalog_is_unavailable(tmp_path: Path) -> None:
    """A moved layout hashes whatever the stale path points at; the shape
    probe must turn that into UNAVAILABLE, never a drift claim."""
    upstream = _write_upstream(tmp_path, "roles:\n  - name: dev\n")
    vendored = _write_vendored(tmp_path, _CATALOG)
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == UNAVAILABLE


def test_unparseable_upstream_yaml_is_unavailable(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path, "version: [unclosed\n")
    vendored = _write_vendored(tmp_path, _CATALOG)
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == UNAVAILABLE


def test_missing_vendored_manifest_is_unavailable(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path, _CATALOG)
    vendored = _write_vendored(tmp_path, _CATALOG, manifest=None)
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == UNAVAILABLE


def test_manifest_without_the_surface_entry_is_unavailable(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path, _CATALOG)
    vendored = _write_vendored(
        tmp_path,
        _CATALOG,
        manifest={
            "producer_commit": _PROVENANCE["commit"],
            "surface": [{"path": "other.yaml", "sha256": _sha(_CATALOG)}],
        },
    )
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == UNAVAILABLE


def test_the_real_vendored_copy_recognizes_itself_as_no_drift(tmp_path: Path) -> None:
    """The reporter against the repo's actual vendored directory, with the
    vendored file itself standing in for upstream: shape probe, manifest
    lookup and hash all run on the real artifacts."""
    real = Path(__file__).parent.parent / "contracts" / "steward-gate-catalog" / "v1"
    upstream = _write_upstream(
        tmp_path, (real / "gate-catalog.yaml").read_text(encoding="utf-8")
    )
    result = compare(upstream, real, _PROVENANCE)
    assert result.outcome == NO_DRIFT
