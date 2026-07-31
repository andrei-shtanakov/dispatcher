"""Pin verification for the vendored github-checker actions/v1 contract.

This is offline-only: it reads the vendored copy under `contracts/` and
never touches `../github-checker`. The vendoring *procedure* (documented in
`scripts/vendor_manifest.py` and re-run to produce this copy) is what must
provably extract the pinned commit's blobs; these tests only guard against
the copy quietly drifting from its own recorded manifest afterward.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

VENDORED_ROOT = (
    Path(__file__).parent.parent / "contracts" / "github-checker-actions" / "v1"
)
PRODUCER_COMMIT = "ef03fefcded37676b19ef1c6f88b956a09a26d3f"
_EXCLUDED_NAMES = {"PINNED.txt", "manifest.json"}


def _load_manifest() -> dict:
    return json.loads((VENDORED_ROOT / "manifest.json").read_text())


def test_the_vendored_surface_matches_its_manifest() -> None:
    """A pinned copy nobody re-hashes is a copy that drifted quietly."""
    manifest = _load_manifest()
    assert manifest["producer_commit"] == PRODUCER_COMMIT
    for entry in manifest["surface"]:
        blob = (VENDORED_ROOT / entry["path"]).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == entry["sha256"], entry["path"]


def test_the_manifest_covers_every_vendored_file() -> None:
    """Per-file hashes are worthless if a file can be added without one."""
    listed = {e["path"] for e in _load_manifest()["surface"]}
    on_disk = {
        str(p.relative_to(VENDORED_ROOT))
        for p in VENDORED_ROOT.rglob("*")
        if p.is_file() and p.name not in _EXCLUDED_NAMES
    }
    assert listed == on_disk


def test_all_thirty_four_fixtures_are_present() -> None:
    """The normative surface includes all 34 fixtures, not a subset."""
    assert len(list((VENDORED_ROOT / "fixtures").glob("*.json"))) == 34


def test_the_tree_hash_is_recomputed_not_merely_stored() -> None:
    """Per-file hashes and coverage still leave `tree_sha256` unchecked: it
    could be anything and every other pin test would pass. Recompute it with
    the same canonical algorithm the manifest was built with."""
    manifest = _load_manifest()
    entries = sorted(manifest["surface"], key=lambda e: e["path"])
    recomputed = hashlib.sha256(
        "".join(f"{e['path']}:{e['sha256']}\n" for e in entries).encode()
    ).hexdigest()
    assert recomputed == manifest["tree_sha256"]


def test_readme_carries_the_three_state_rule() -> None:
    """The README is normative: schema without it is shape without meaning."""
    readme = (VENDORED_ROOT / "README.md").read_text()
    assert "three-state rule" in readme


def test_the_manifest_declares_the_contract_it_pins() -> None:
    """`contract_version` is vendored but was asserted by nothing: a future
    re-vendor that forgot to bump it would pass every other pin guard."""
    manifest = json.loads((VENDORED_ROOT / "manifest.json").read_text())
    assert manifest["contract"] == "github-checker-actions"
    assert manifest["contract_version"] == 1
    schema = json.loads((VENDORED_ROOT / "actions.schema.json").read_text())
    # the schema's own version const must agree with what the manifest claims
    assert schema["$defs"]["verb_pull"]["properties"]["schema_version"]["const"] == 1
