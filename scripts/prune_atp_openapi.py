"""Prune an ATP eco openapi.json to the surface dispatcher consumes.

Spec §9: keep exactly the two consumed GET paths and the component schemas
they transitively reference; write canonical bytes (sorted keys, indent 2,
trailing newline) so upstream-drift can compare file digests directly.

Usage: prune_atp_openapi.py <full-openapi.json> <out-pruned.json>
"""

from __future__ import annotations

import json
import sys
from typing import Any

KEPT_PATHS = (
    "/api/v1/benchmarks",
    "/api/v1/benchmarks/{benchmark_id}/leaderboard",
)


def _collect_refs(node: Any, found: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[1])
        for value in node.values():
            _collect_refs(value, found)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, found)


def prune(full: dict[str, Any]) -> dict[str, Any]:
    paths = {p: {"get": full["paths"][p]["get"]} for p in KEPT_PATHS}
    schemas: dict[str, Any] = full.get("components", {}).get("schemas", {})
    kept: set[str] = set()
    frontier: set[str] = set()
    _collect_refs(paths, frontier)
    while frontier:
        name = frontier.pop()
        if name in kept or name not in schemas:
            continue
        kept.add(name)
        _collect_refs(schemas[name], frontier)
    return {
        "openapi": full["openapi"],
        "info": {"title": full["info"]["title"], "version": full["info"]["version"]},
        "paths": paths,
        "components": {"schemas": {n: schemas[n] for n in sorted(kept)}},
    }


def main() -> None:
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as fh:
        full = json.load(fh)
    pruned = prune(full)
    with open(dst, "w") as fh:
        json.dump(pruned, fh, indent=2, sort_keys=True)
        fh.write("\n")


if __name__ == "__main__":
    main()
