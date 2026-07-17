# Vendored pin — spec-runner executor-config schema (provisional)

> Source: hand-derived from `spec-runner/src/spec_runner/config.py`
> (`ExecutorConfig`, `Persona`) @ `72db9f5` — **no upstream machine-readable
> contract exists yet.** spec-runner ships `schemas/*.schema.json` for other
> artifacts (json-result, costs, doctor-result, executor-state, status) but
> not this one. Provisional per
> `docs/superpowers/specs/2026-07-17-spec-runner-config-editor-design.md`
> DESIGN-301; promote to a real vendored copy once handoff **H-4** lands
> (spec-runner publishes `schemas/executor-config.schema.json`).
> Do not treat as authoritative — re-derive by hand if `ExecutorConfig` changes.

Covers only the `extra_executor_config` overlay fields (personas, review
parallelism, telegram/webhook, budgets, `integration_pr`/`main_branch`,
remaining hook flags). The fields already mirrored as typed
`SpecRunnerConfig` fields on the Maestro side (`maestro/models.py:1152`,
commit `0122942`) are validated separately, in
`dispatcher/core/spec_runner_config_schema.py::validate_typed_fields`.

| file | sha256 |
|---|---|
| `schema.json` | `db104691477d0f3b860bf319081d77f988fa209637e6649881454c79957d7fd8` |
