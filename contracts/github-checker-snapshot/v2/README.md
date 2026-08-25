# Vendored pin — github-checker snapshot contract v2

> Source (authority): `github-checker/contracts/snapshot/v2/` @ `ca55187` (github-checker#23).
> Vendored: 2026-08-25 per ADR-ECO-003 pinned-contract discipline; the axis it carries is
> ADR-ECO-010 (epic classification on issues/PRs + merged-PR attribution window).
> Do not edit — update by re-copying from the source repo and refreshing hashes below.

**v1 stays vendored beside this copy on purpose.** A producer still publishing v1 is a
supported state, not a failure: its GitHub planes carry no epic classification, and the
read-model reports that as `unavailable` rather than as zero. Deleting v1 would turn a
neighbour's unhurried upgrade into this dispatcher's outage.

| file | sha256 |
|---|---|
| `snapshot.schema.json` | `b71e47e6a7626cb977189117411f1118e6dd8f6922a1bb742da0f41fbdd7e83c` |
| `fixtures/snapshot_degraded.json` | `352419f20719bcb668fcd5959cd459fdcaca1ff853b18e7a9527024c2151ba02` |
| `fixtures/snapshot_full.json` | `ba8710e36ed4acfe72e154750ffdce2a5fc50634ebb3fe7b254ced5385cd78ed` |
