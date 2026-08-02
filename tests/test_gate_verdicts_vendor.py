"""Copy-integrity of the vendored gate-verdicts/v1 contract (guarantee A).

Offline and consumer-owned: reads only the vendored copy, never a sibling
checkout, and therefore never skips. Upstream drift is guarantee B — the
scheduled advisory workflow (`drift-steward-gate-verdicts` in
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

VENDORED_ROOT = (
    Path(__file__).parent.parent / "contracts" / "steward-gate-verdicts" / "v1"
)
PRODUCER_COMMIT = "4836345a4250735ebce9de7616a4a42b463da654"
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
    assert manifest["contract"] == "steward-gate-verdicts"
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
    """The five canon fixtures and the schema are what the collector tests
    stand on; a re-vendor that silently loses one must fail here, not there."""
    paths = set(_on_disk())
    assert {
        "SCHEMA.json",
        "README.md",
        "fixtures/clean.jsonl",
        "fixtures/findings.jsonl",
        "fixtures/malformed_line.jsonl",
        "fixtures/future_schema.jsonl",
        "fixtures/dangling_artifact.jsonl",
    } == paths
