"""Regenerate manifest.json for a vendored github-checker actions/v1 copy.

Dev tool, not shipped runtime. Normally invoked by
``scripts/revendor_github_checker_actions.sh``, which passes the commit it
extracted from and the staging directory it extracted into:

    python3 scripts/vendor_manifest.py --producer-commit <sha> --root <dir>

The pin is an argument and has no default. It used to be a literal in this
file, which made it one of three copies a human had to edit in step — and
three literals changed together prove only that they agree with each other,
never that the files on disk came from the commit they name.

It hashes every file under the root (excluding the manifest itself and
PINNED.txt), writes a per-file sha256 plus a tree_sha256 computed over the
sorted (path, sha256) pairs, and records the given producer commit so the
consumer's offline tests can assert on it without touching the network.

Stdlib only, on purpose: the re-vendor script runs it as plain ``python3``,
outside any uv project.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib

ROOT = pathlib.Path("contracts/github-checker-actions/v1")
EXCLUDED_NAMES = {"PINNED.txt", "manifest.json"}


def build_manifest(root: pathlib.Path, producer_commit: str) -> dict[str, object]:
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
        "producer_commit": producer_commit,
        "surface_note": (
            "sha256 of every vendored file; excludes PINNED.txt and this manifest"
        ),
        "tree_sha256": tree_sha256,
        "surface": entries,
    }


def main() -> None:
    """Write manifest.json for the vendored actions/v1 copy at a given pin."""
    parser = argparse.ArgumentParser(description="regenerate manifest.json")
    parser.add_argument(
        "--producer-commit",
        required=True,
        help="the github-checker commit the vendored bytes were extracted from",
    )
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=ROOT,
        help="directory holding the vendored copy (default: the in-tree one)",
    )
    args = parser.parse_args()
    manifest = build_manifest(args.root, args.producer_commit)
    (args.root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
