"""Upstream drift for atp-benchmark-api/v1 (guarantee B, spec §9).

Compares the sha256 of the VENDORED pruned openapi.json against a freshly
REGENERATED pruned openapi.json (produced by the caller — this script only
compares and reports). The directory tree hash is copy-integrity's artifact
and is deliberately not consulted here.

Exit codes: 0 no drift · 1 drift · 2 unavailable (missing input — fix the
observation, never assume "in sync").

Usage: atp_openapi_drift_report.py <regenerated-pruned-openapi.json>
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

VENDORED = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "atp-benchmark-api"
    / "v1"
    / "openapi.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: atp_openapi_drift_report.py <regenerated.json>")
        return 2
    regenerated = Path(sys.argv[1])
    if not VENDORED.is_file():
        print(f"unavailable: vendored copy missing at {VENDORED}")
        return 2
    if not regenerated.is_file():
        print(f"unavailable: regenerated file missing at {regenerated}")
        return 2
    ours, theirs = _sha256(VENDORED), _sha256(regenerated)
    if ours == theirs:
        print(f"no drift: {ours}")
        return 0
    print(f"DRIFT: vendored {ours} != regenerated {theirs}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
