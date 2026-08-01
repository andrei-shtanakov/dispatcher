"""The advisory upstream-drift reporter (PF-6, guarantee B).

Three outcomes must stay distinguishable, because collapsing any two of them
is how an advisory check goes quietly useless:

- **no drift** — canon was read and matches the vendored copy;
- **drift** — canon was read and differs; a human decides whether to re-vendor;
- **unavailable** — canon could not be read at all. NOT the same as "no drift",
  and not silently green: an unreadable upstream is unknown, and unknown that
  looks green is the failure this whole workstream keeps removing.

The report also records provenance — the resolved commit, the remote it came
from, and the tree hash actually recomputed from the files on disk — so the
run says which upstream it looked at, not merely that it looked.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from upstream_drift_report import DRIFT, NO_DRIFT, UNAVAILABLE, compare

_FILES = {"schema.json": '{"x":1}', "fixtures/valid/basic.md": "- [x] a\n"}
_PROVENANCE = {
    "commit": "db6c7a6fd646cfa48d1453b7cfe7f05c3df0ea00",
    "remote": "https://github.com/andrei-shtanakov/prograph-vault",
    "ref": "master",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _manifest(files: dict[str, str]) -> dict:
    surface = [{"path": r, "sha256": _sha(files[r])} for r in sorted(files)]
    digest = hashlib.sha256()
    for entry in surface:
        digest.update(f"{entry['path']}\0{entry['sha256']}\n".encode())
    return {"tree_sha256": digest.hexdigest(), "surface": surface}


def _write(root: Path, files: dict[str, str], *, manifest: dict | None) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    if manifest is not None:
        root.mkdir(parents=True, exist_ok=True)
        (root / "manifest.json").write_text(json.dumps(manifest))
    return root


def _both(tmp_path: Path, canon: dict[str, str], vendored: dict[str, str]):
    return (
        _write(tmp_path / "canon", canon, manifest=_manifest(canon)),
        _write(tmp_path / "vendored", vendored, manifest=_manifest(vendored)),
    )


def test_matching_canon_reports_no_drift(tmp_path: Path) -> None:
    canon, vendored = _both(tmp_path, _FILES, _FILES)
    result = compare(canon, vendored, _PROVENANCE)
    assert result.outcome == NO_DRIFT
    assert result.exit_code == 0


def test_a_changed_canon_file_is_drift_and_names_it(tmp_path: Path) -> None:
    canon, vendored = _both(tmp_path, {**_FILES, "schema.json": '{"x":2}'}, _FILES)
    result = compare(canon, vendored, _PROVENANCE)
    assert result.outcome == DRIFT
    assert result.exit_code == 1
    assert "schema.json" in result.summary


def test_an_added_canon_file_is_drift(tmp_path: Path) -> None:
    canon, vendored = _both(tmp_path, {**_FILES, "extra.md": "new\n"}, _FILES)
    result = compare(canon, vendored, _PROVENANCE)
    assert result.outcome == DRIFT
    assert "extra.md" in result.summary


def test_an_absent_canon_is_unavailable_not_no_drift(tmp_path: Path) -> None:
    vendored = _write(tmp_path / "vendored", _FILES, manifest=_manifest(_FILES))
    result = compare(tmp_path / "nowhere", vendored, _PROVENANCE)
    assert result.outcome == UNAVAILABLE
    assert result.exit_code == 2


def test_an_unreadable_canon_manifest_is_unavailable(tmp_path: Path) -> None:
    canon, vendored = _both(tmp_path, _FILES, _FILES)
    (canon / "manifest.json").write_text("{not json")
    result = compare(canon, vendored, _PROVENANCE)
    assert result.outcome == UNAVAILABLE


def test_a_stale_canon_manifest_is_drift_not_silence(tmp_path: Path) -> None:
    """Upstream changed a file and did not regenerate its fingerprint.

    Comparing manifest-to-manifest would report "no drift" here, because both
    fingerprints still agree — while the actual canon files no longer match
    either of them. The hash is recomputed from the files on disk for exactly
    this case.
    """
    canon, vendored = _both(tmp_path, _FILES, _FILES)
    (canon / "schema.json").write_text('{"x":999}')
    result = compare(canon, vendored, _PROVENANCE)
    assert result.outcome == DRIFT


def test_the_summary_records_which_upstream_was_read(tmp_path: Path) -> None:
    """ "No drift" is worth nothing without saying no drift *from what*."""
    canon, vendored = _both(tmp_path, _FILES, _FILES)
    result = compare(canon, vendored, _PROVENANCE)
    assert _PROVENANCE["commit"] in result.summary
    assert _PROVENANCE["remote"] in result.summary
    assert _PROVENANCE["ref"] in result.summary
    # the tree hash recomputed from the files, not copied out of the manifest
    assert _manifest(_FILES)["tree_sha256"] in result.summary


def test_the_summary_never_tells_the_reader_to_edit_the_hash(
    tmp_path: Path,
) -> None:
    """A red advisory means "a re-vendor PR is due", never "update expected".

    The cheapest way to make a drift check useless is to teach its readers
    that the fix is to change the number it compares against.
    """
    canon, vendored = _both(tmp_path, {**_FILES, "schema.json": '{"x":2}'}, _FILES)
    summary = compare(canon, vendored, _PROVENANCE).summary.lower()
    assert "re-vendor" in summary
    assert "update the expected" not in summary
