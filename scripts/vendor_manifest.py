"""Regenerate manifest.json for the vendored github-checker actions/v1 pin.

Dev tool, not shipped runtime. Run after re-vendoring
``contracts/github-checker-actions/v1`` from a new producer commit:

    uv run python scripts/vendor_manifest.py

It hashes every file under the vendored root (excluding the manifest itself
and PINNED.txt), writes a per-file sha256 plus a tree_sha256 computed over
the sorted (path, sha256) pairs, and pins the producer commit inline so the
consumer's tests can assert on it without touching the network.
"""

import hashlib
import json
import pathlib

ROOT = pathlib.Path("contracts/github-checker-actions/v1")
PRODUCER_COMMIT = "ef03fefcded37676b19ef1c6f88b956a09a26d3f"
EXCLUDED_NAMES = {"PINNED.txt", "manifest.json"}


def build_manifest(root: pathlib.Path) -> dict[str, object]:
    """Compute the per-file and tree-level hashes for the vendored surface.

    The tree hash is derived from the same sorted (path, sha256) pairs
    that the per-file entries carry, so any test that recomputes it from
    the manifest's own `surface` list reproduces this exact value.
    """
    surface = sorted(
        p for p in root.rglob("*") if p.is_file() and p.name not in EXCLUDED_NAMES
    )
    entries = [
        {
            "path": str(p.relative_to(root)),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in surface
    ]
    tree_sha256 = hashlib.sha256(
        "".join(f"{e['path']}:{e['sha256']}\n" for e in entries).encode()
    ).hexdigest()
    return {
        "contract": "github-checker-actions",
        "contract_version": 1,
        "producer_commit": PRODUCER_COMMIT,
        "surface_note": (
            "sha256 of every vendored file; excludes PINNED.txt and this manifest"
        ),
        "tree_sha256": tree_sha256,
        "surface": entries,
    }


def main() -> None:
    """Write manifest.json for the vendored actions/v1 pin."""
    manifest = build_manifest(ROOT)
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
