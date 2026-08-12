"""Advisory upstream-drift report for steward-roles-catalog/v1 (guarantee B).

Answers one question — "has `profiles/roles.yaml` in steward moved away from
the copy vendored here?" — and answers it *about a named upstream*: the
resolved commit, the remote it came from, and the hash recomputed from the
file actually checked out.

This is an observation, not a gate. It runs on a schedule and on demand,
never on a dispatcher pull request. Guarantee A — that the vendored copy
matches its own manifest — is `tests/test_roles_catalog_vendor.py`, and it
needs no network.

The machinery is `gate_catalog_drift_report.py`'s, parameterized by
`ContractSpec` rather than kept as a second copy in step by hand: same
comparison, same classification (0 no drift · 1 drift · 2 unavailable, where
unavailable is red too — an upstream nobody could read is unknown, never
"no drift"). Only the contract's identity differs, including the shape
probe: a mapping carrying `version` and `roles` — a moved upstream layout
must classify as UNAVAILABLE, never as DRIFT.

Nothing here re-vendors anything, and nothing here rewrites an expected
hash. A red run means a human owes a deliberate re-vendor PR via
`scripts/revendor_steward_roles_catalog.sh` — reading what upstream changed
and why.

Run:  python scripts/roles_catalog_drift_report.py <upstream-roles-file> \
          [--vendored <dir>] [--upstream-root <repo>] [--ref <ref>]
Exit: 0 no drift, 1 drift, 2 upstream unavailable.
"""

from __future__ import annotations

import sys

from gate_catalog_drift_report import ContractSpec, main

ROLES_CATALOG = ContractSpec(
    vendored_rel="contracts/steward-roles-catalog/v1",
    surface_file="roles.yaml",
    upstream_path="profiles/roles.yaml",
    probe_keys=("version", "roles"),
    display_name="the roles catalog",
    revendor_script="scripts/revendor_steward_roles_catalog.sh",
)

if __name__ == "__main__":
    sys.exit(main(spec=ROLES_CATALOG, description=__doc__))
