"""Prune an ATP eco openapi.json to the surface dispatcher consumes.

Phase-1 spec §9 + phase-2 spec §8: keep exactly the consumed GET paths,
the component schemas they transitively reference, and the security
schemes the kept operations name; write canonical bytes (sorted keys,
indent 2, trailing newline) so upstream-drift can compare file digests
directly.

Usage: prune_atp_openapi.py <full-openapi.json> <out-pruned.json>
"""

from __future__ import annotations

import json
import sys
from typing import Any

KEPT_PATHS = (
    "/api/v1/benchmarks",
    "/api/v1/benchmarks/{benchmark_id}/leaderboard",
    "/api/v1/runs/{run_id}/status",
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
    paths: dict[str, Any] = {}
    for kept_path in KEPT_PATHS:
        entry = full.get("paths", {}).get(kept_path)
        if entry is None or "get" not in entry:
            # A missing consumed route means the producer moved the surface;
            # name it instead of dying with a bare KeyError — the drift job's
            # red Regenerate step should be diagnosable from its log.
            raise SystemExit(
                f"prune: GET {kept_path} not found in the source openapi — "
                "the consumed surface moved upstream"
            )
        paths[kept_path] = {"get": entry["get"]}
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
    components: dict[str, Any] = {"schemas": {n: schemas[n] for n in sorted(kept)}}
    # Security requirement objects name schemes by key, not by $ref — carry
    # the named securitySchemes so the pruned document does not dangle
    # (phase-2: the run-status operation is Bearer-gated).
    scheme_names: set[str] = set()
    for entry in paths.values():
        for requirement in entry["get"].get("security", []):
            scheme_names.update(requirement)
    all_schemes: dict[str, Any] = full.get("components", {}).get("securitySchemes", {})
    missing_schemes = scheme_names - set(all_schemes)
    if missing_schemes:
        # Same reasoning as the missing-path branch: a named-but-undefined
        # scheme means the producer moved the surface — fail loudly so the
        # drift job's red step is diagnosable, never a dangling document.
        raise SystemExit(
            "prune: security scheme(s) named by kept operations are not in "
            f"components.securitySchemes: {', '.join(sorted(missing_schemes))}"
        )
    kept_schemes = {n: all_schemes[n] for n in sorted(scheme_names)}
    if kept_schemes:
        components["securitySchemes"] = kept_schemes
    return {
        "openapi": full["openapi"],
        "info": {"title": full["info"]["title"], "version": full["info"]["version"]},
        "paths": paths,
        "components": components,
    }


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: prune_atp_openapi.py <full-openapi.json> <out-pruned.json>",
            file=sys.stderr,
        )
        return 2
    src, dst = sys.argv[1], sys.argv[2]
    with open(src) as fh:
        full = json.load(fh)
    pruned = prune(full)
    with open(dst, "w") as fh:
        json.dump(pruned, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
