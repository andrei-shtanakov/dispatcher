"""Copy-integrity of the vendored atp-benchmark-api/v1 (guarantee A).

Offline and consumer-owned: reads only the vendored copy, never a sibling
checkout, and therefore never skips. Upstream drift is guarantee B and is
deliberately not asserted here: the two guarantees answer different
questions and must not share one test (the dispatcher #99 lesson).

PRODUCER_COMMIT below stays a hand-maintained literal on purpose — it is the
independent assertion about what the manifest should say; a test that reads
the value it checks proves nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

VENDORED_ROOT = Path(__file__).parent.parent / "contracts" / "atp-benchmark-api" / "v1"
PRODUCER_COMMIT = "da3a264e0ec73811b5f066e47a343f2e91600b91"
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
    assert manifest["contract"] == "atp-benchmark-api"
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


def test_prune_fails_loudly_on_an_undefined_security_scheme() -> None:
    """A kept operation naming a scheme absent from securitySchemes means
    the producer moved the surface — the prune must die diagnosably, never
    emit a dangling document (Copilot review PR #151)."""
    from typing import Any

    import pytest
    from prune_atp_openapi import KEPT_PATHS, prune

    schemes: dict[str, Any] = {}
    paths: dict[str, Any] = {
        p: {"get": {"responses": {"200": {"description": "ok"}}}} for p in KEPT_PATHS
    }
    paths[KEPT_PATHS[-1]]["get"]["security"] = [{"GhostScheme": []}]
    full: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": paths,
        "components": {"schemas": {}, "securitySchemes": schemes},
    }
    with pytest.raises(SystemExit, match="GhostScheme"):
        prune(full)
    schemes["GhostScheme"] = {"type": "http"}
    pruned = prune(full)
    assert pruned["components"]["securitySchemes"] == {"GhostScheme": {"type": "http"}}


def test_vendored_openapi_names_no_undefined_security_scheme() -> None:
    """The shipped pruned document itself must not dangle."""
    import json

    doc = json.loads((VENDORED_ROOT / "openapi.json").read_text())
    defined = set(doc.get("components", {}).get("securitySchemes", {}))
    for entry in doc["paths"].values():
        for requirement in entry["get"].get("security", []):
            assert set(requirement) <= defined


def test_the_expected_surface_is_present() -> None:
    """The openapi.json + fixtures are what `dispatcher.core.benchmarks`
    stands on; a re-vendor that gained or lost a file must fail here, not
    there."""
    assert set(_on_disk()) == {
        "openapi.json",
        "fixtures/benchmarks.json",
        "fixtures/leaderboard.json",
        "fixtures/leaderboard-empty.json",
        "fixtures/run-status-completed.json",
        "fixtures/run-status-in-progress.json",
    }
