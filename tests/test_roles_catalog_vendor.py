"""Copy-integrity of the vendored steward roles-catalog/v1 (guarantee A).

Offline and consumer-owned: reads only the vendored copy, never a sibling
checkout, and therefore never skips. Upstream drift is guarantee B — the
scheduled advisory workflow (`drift-steward-roles-catalog` in
`.github/workflows/upstream-drift.yml`) — and is deliberately not asserted
here: the two guarantees answer different questions and must not share one
test (the dispatcher #99 lesson).

PRODUCER_COMMIT below stays a hand-maintained literal on purpose — it is the
independent assertion about what the manifest should say; a test that reads
the value it checks proves nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import yaml

VENDORED_ROOT = (
    Path(__file__).parent.parent / "contracts" / "steward-roles-catalog" / "v1"
)
PRODUCER_COMMIT = "b79c858dc5f5dc7651f15a1cdf3bcd51a1de2d16"
_EXCLUDED_NAMES = {"PINNED.txt", "manifest.json"}


def _manifest() -> dict:
    return json.loads((VENDORED_ROOT / "manifest.json").read_text())


def _on_disk() -> dict[str, str]:
    return {
        str(p.relative_to(VENDORED_ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in VENDORED_ROOT.rglob("*")
        if p.is_file() and p.name not in _EXCLUDED_NAMES
    }


def test_manifest_names_the_pin_and_contract() -> None:
    manifest = _manifest()
    assert manifest["producer_commit"] == PRODUCER_COMMIT
    assert manifest["contract"] == "steward-roles-catalog"
    assert manifest["contract_version"] == 1


def test_every_vendored_file_matches_its_manifest_hash() -> None:
    """Both directions at once: no extra file, no missing file, no changed
    byte — a dict comparison states set equality and hash equality together."""
    listed = {e["path"]: e["sha256"] for e in _manifest()["surface"]}
    assert listed == _on_disk()


def test_tree_hash_is_reproducible_from_the_surface_list() -> None:
    manifest = _manifest()
    entries = sorted(manifest["surface"], key=lambda e: e["path"])
    recomputed = hashlib.sha256(
        "".join(f"{e['path']}:{e['sha256']}\n" for e in entries).encode()
    ).hexdigest()
    assert recomputed == manifest["tree_sha256"]


def test_pinned_txt_names_the_same_commit() -> None:
    pinned = (VENDORED_ROOT / "PINNED.txt").read_text()
    match = re.search(r"^commit: ([0-9a-f]{40})$", pinned, re.MULTILINE)
    assert match is not None
    assert match.group(1) == PRODUCER_COMMIT


def test_the_expected_surface_is_present() -> None:
    """The one catalog file is the vendored surface; a re-vendor that gained
    or lost a file must fail here, not at a consumer."""
    assert set(_on_disk()) == {"roles.yaml"}


def test_v1_composition_is_pinned() -> None:
    """The content this consumer stands on: v1's six role slugs and the slug
    grammar (DEC-007 §1 — composition is pinned to `version`, so any
    composition change upstream must arrive with a version bump and update
    this assertion deliberately). Verdict records and the governance panel
    reference these same slugs; a re-vendor that changes the set must be a
    reviewed decision, not a silent byte swap."""
    catalog = yaml.safe_load((VENDORED_ROOT / "roles.yaml").read_text())
    assert catalog["version"] == 1
    assert catalog["slug_pattern"] == "^[a-z][a-z0-9-]{1,31}$"
    assert [role["slug"] for role in catalog["roles"]] == [
        "product",
        "architects",
        "qa",
        "tech-lead",
        "stream-owner",
        "owner",
    ]
