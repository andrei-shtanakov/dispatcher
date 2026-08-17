"""Copy-integrity of BOTH vendored impresario contracts (guarantee A).

Offline and consumer-owned: reads only the vendored copies, never a sibling
checkout, and therefore never skips. Upstream drift is guarantee B — the
scheduled advisory workflow — and is deliberately not asserted here.

PRODUCER_COMMIT stays a hand-maintained literal on purpose — it is the
independent assertion about what the manifests should say; a test that reads
the value it checks proves nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

CONTRACTS_ROOT = Path(__file__).parent.parent / "contracts"
PRODUCER_COMMIT = "a9d11fa75bb101d2919dc9f99e075270de5d7976"
_EXCLUDED_NAMES = {"PINNED.txt", "manifest.json"}
VENDORED = {
    "impresario-product-proposal": CONTRACTS_ROOT
    / "impresario-product-proposal"
    / "v1",
    "impresario-gate-decision": CONTRACTS_ROOT / "impresario-gate-decision" / "v1",
    "impresario-loop-state": CONTRACTS_ROOT / "impresario-loop-state" / "v1",
    "impresario-ranked-backlog": CONTRACTS_ROOT / "impresario-ranked-backlog" / "v1",
    "impresario-loop-resume-decision": CONTRACTS_ROOT
    / "impresario-loop-resume-decision"
    / "v1",
}
EXPECTED_SURFACES = {
    "impresario-product-proposal": {
        "schema.json",
        "fixtures/valid/pp-001.yaml",
        "fixtures/invalid/status-ready-for-committee.yaml",
        "fixtures/invalid/status-recycle.yaml",
        "fixtures/invalid/version-zero.yaml",
    },
    "impresario-gate-decision": {
        "schema.json",
        "fixtures/valid/gd-approve.yaml",
        "fixtures/valid/gd-recycle.yaml",
        "fixtures/valid/gd-select.yaml",
        "fixtures/invalid/agent-authority.yaml",
        "fixtures/invalid/qg4-approve.yaml",
        "fixtures/invalid/recycle-without-return-to.yaml",
    },
    "impresario-loop-state": {
        "schema.json",
        "fixtures/invalid/bad-hash.json",
        "fixtures/invalid/empty-reason.json",
        "fixtures/invalid/extra-field.json",
        "fixtures/invalid/missing-at.json",
        "fixtures/invalid/unknown-verdict.json",
        "fixtures/valid/failed.json",
        "fixtures/valid/needs-human.json",
        "fixtures/valid/ready.json",
        "fixtures/valid/running.json",
    },
    "impresario-ranked-backlog": {
        "schema.json",
        "fixtures/valid/bl-portfolio.yaml",
        "fixtures/invalid/excluded-blocker-without-ref.yaml",
        "fixtures/invalid/item-unknown-score.yaml",
        "fixtures/invalid/item-with-blocker.yaml",
    },
    "impresario-loop-resume-decision": {
        "schema.json",
        "fixtures/valid/lrd-001.yaml",
        "fixtures/valid/lrd-002-supersedes.yaml",
        "fixtures/invalid/lrd-empty-reason.yaml",
        "fixtures/invalid/lrd-extra-field.yaml",
        "fixtures/invalid/lrd-foreign-supersedes.yaml",
        "fixtures/invalid/lrd-missing-iteration.yaml",
        "fixtures/invalid/lrd-non-human-actor.yaml",
        "fixtures/invalid/lrd-zero-budget.yaml",
    },
}


def _manifest(root: Path) -> dict:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _on_disk(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and p.name not in _EXCLUDED_NAMES
    }


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_manifest_names_the_pin_and_contract(name: str) -> None:
    manifest = _manifest(VENDORED[name])
    assert manifest["producer_commit"] == PRODUCER_COMMIT
    assert manifest["contract"] == name
    assert manifest["contract_version"] == 1


def test_both_manifests_share_one_producer_commit() -> None:
    """The anti-mix guarantee: the five contracts can never silently sit at
    different upstream commits — pin and provenance are machine-readable."""
    pins = {_manifest(root)["producer_commit"] for root in VENDORED.values()}
    assert pins == {PRODUCER_COMMIT}


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_every_vendored_file_matches_its_manifest_hash(name: str) -> None:
    root = VENDORED[name]
    listed = {e["path"]: e["sha256"] for e in _manifest(root)["surface"]}
    assert listed == _on_disk(root)


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_tree_hash_is_reproducible_from_the_surface_list(name: str) -> None:
    manifest = _manifest(VENDORED[name])
    entries = sorted(manifest["surface"], key=lambda e: e["path"])
    recomputed = hashlib.sha256(
        "".join(f"{e['path']}:{e['sha256']}\n" for e in entries).encode()
    ).hexdigest()
    assert recomputed == manifest["tree_sha256"]


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_pinned_txt_names_the_same_commit(name: str) -> None:
    pinned = (VENDORED[name] / "PINNED.txt").read_text(encoding="utf-8")
    match = re.search(r"^commit: ([0-9a-f]{40})$", pinned, re.MULTILINE)
    assert match is not None
    assert match.group(1) == PRODUCER_COMMIT


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_the_expected_surface_is_present(name: str) -> None:
    """The schema + upstream fixtures the collector tests stand on; a
    re-vendor that silently loses one must fail here, not there."""
    assert set(_on_disk(VENDORED[name])) == EXPECTED_SURFACES[name]
