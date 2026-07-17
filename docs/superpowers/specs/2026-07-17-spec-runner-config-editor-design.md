# Design — Spec-runner config editor (Maestro `project.yaml`)

> **Context (2026-07-17):** this design was not preceded by a full discovery-brief
> Gate cycle like the sync/roadmap iteration. It grew directly out of a stakeholder
> conversation (this session) that resolved a new conflict — **X-02** — analogous to
> how X-01 was resolved on 2026-07-14. It extends `spec/discovery-brief-customer.md`'s
> `FR-04` (project onboarding view) territory with a concrete, narrower capability:
> viewing and editing the spec-runner execution profile Maestro uses per project.
> Owner role: architect (approval = merge of this PR).

## 0. Trigger and prerequisite

Maestro landed `extra_executor_config: dict[str, Any] | None` on `SpecRunnerConfig`
today (commit `0122942`, "SpecRunnerConfig: proxy model fields + extra_executor_config
escape hatch (#82)") — a deep-merge overlay on top of `to_executor_config()`. Combined
with the three newly-typed fields (`claude_model`, `review_command`, `review_model`),
every field previously undocumented as silently-defaulted
(`prograph-vault/authored/notes/2026-07-17-maestro-specrunnerconfig-gaps-handoff.md`)
is now reachable — personas, `review_parallel`/`review_roles`, `telegram_*`,
`webhook_*`, budgets, and the remaining hook flags all round-trip through
`extra_executor_config`. This design assumes that passthrough as already shipped; it
does not depend on any further Maestro change.

## 1. Stakeholder decision — X-02

**Conflict:** the standing invariant ("dispatcher never mutates observed repos
itself" — `COWORK_CONTEXT.md` §Жёсткие инварианты, `core/actions.py` docstring) is
written for git-plumbing-only actions (`pull`, `create-pr`, both delegated whole to
`github-checker`). A config editor requires dispatcher itself to produce file
*content* — a diff against `project.yaml`'s `spec_runner:` block — before any PR
exists to create.

**Resolution (product owner, this session, 2026-07-17):**

> Was: dispatcher does not mutate observed repos.
> Now: dispatcher makes no background/autonomous writes and never writes to a
> default branch; it may perform whitelisted **content-change actions** only via
> explicit human click, through branch + diff preview + PR, with audit and schema
> validation.

This adds a second action class alongside the existing sync actions, not a general
edit permission:

- **sync actions** (existing, unchanged): `pull`, `create-pr` — delegated whole to
  `github-checker`, dispatcher never touches file content.
- **content PR actions** (new, this design): `update-spec-runner-config` —
  dispatcher itself renders a schema-validated diff limited to one YAML block of one
  file, then hands off branch/commit/push/PR to `github-checker`, same as today.

`OUT-01`/`OUT-02` (no task execution, no orchestration, no arbitrary
push/merge/file edits) are unaffected — this whitelist entry is the only exception,
scoped to exactly one block of one file type.

## 2. High-level diagram

```text
┌───────────────────────────── dispatcher ─────────────────────────────────┐
│ contracts/executor-config/v0-provisional/   (pinned, stopgap — see §3)   │
│ core/spec_runner_config.py                                               │
│   read project.yaml → effective config (typed fields ⊕ extra overlay,   │
│   mirrors Maestro's own _deep_merge) + per-field "explicit vs default"  │
│ core/spec_runner_config_actions.py  (NEW — separate from core/actions.py)│
│   validate(candidate) -> against pinned schema, reject before diff       │
│   build_diff(project, candidate) -> unified diff of `spec_runner:` block │
│   ActionRunner-like guard: one in-flight per repo, own lock, own audit   │
│   run("update-spec-runner-config", repo_dir, candidate)                 │
│     → writes only the spec_runner: block → github-checker open-pr <dir> │
│ server/app.py:                                                          │
│   GET  /api/projects/{name}/spec-runner-config                         │
│   POST /api/actions/update-spec-runner-config                          │
│   ├─► web  new "Config" screen: typed fields + personas/review/telegram/│
│   │        webhook/budgets/hooks sub-forms, diff preview, PR button    │
│   └─► tui  M2 (FR-06 parity), out of this design's M1                  │
└───────────────────────────────────────────────────────────────────────────┘
        PR creation delegated ──► github-checker open-pr (existing, unchanged)
```

## 3. Components

### DESIGN-301: Provisional pinned schema (stopgap, not a Python-class vendor)

spec-runner already publishes machine-readable contracts under `schemas/*.schema.json`
(`json-result`, `costs`, `doctor-result`, `executor-state`, `status`) but has none yet
for `ExecutorConfig`/`Persona`. Vendoring the Python dataclasses directly would be
fragile (renames, added fields, and behavior like `get_model_for_role()` don't
round-trip through a copy-paste). Instead:

- Hand-derive a JSON Schema for the fields this editor exposes (typed `ExecutorConfig`
  fields + `Persona`), pin it at `contracts/executor-config/v0-provisional/schema.json`
  with a header recording `source: spec-runner@<sha>, hand-derived, no upstream
  contract yet — provisional`.
- File a handoff note (`prograph-vault/authored/notes/`) asking spec-runner to publish
  `schemas/executor-config.schema.json` generated from `ExecutorConfig.model_json_schema()`
  (matching their own convention). Once that lands, swap the provisional pin for the
  real vendored copy (same promotion pattern as `contracts/github-checker-snapshot/v1/`,
  ADR-ECO-003).
- Validation against the provisional schema still runs before every diff — it is a
  stopgap in provenance, not in rigor.

### DESIGN-302: Read-model (`core/spec_runner_config.py`)

New collector: per Maestro-managed project, read `project.yaml`, extract
`spec_runner:` → `ProjectConfig.spec_runner` shape. Compute an **effective view**
mirroring Maestro's own merge: typed fields as declared, `extra_executor_config`
deep-merged on top, each field tagged `explicit` (set in YAML) or `default`
(pydantic default, silently in effect). This is pure read, ships independent of the
editor — same risk class as the existing `/api/models` collector.

### DESIGN-303: Validation

Two tiers:
- Typed fields (`max_retries`, `task_timeout_minutes`, `claude_command`,
  `auto_commit`, hook flags, `commands.test/lint`, `claude_model`, `review_command`,
  `review_model`) — validated against Maestro's own `SpecRunnerConfig` field types
  (mirrored, not re-derived — these are stable and few).
- `extra_executor_config` keys (personas, `review_parallel`/`review_roles`,
  `telegram_*`, `webhook_*`, budgets, remaining hook flags) — validated against the
  DESIGN-301 provisional schema, since Maestro's own model does not (and by design
  will not) type-check this dict; a malformed key here fails silently at Maestro's
  next run otherwise.

Validation runs before any diff is built. A failure never produces a branch or PR
(`ActionRejectedError`-equivalent, 422).

### DESIGN-304: Content-PR action runner (`core/spec_runner_config_actions.py`)

Deliberately **not** the same module/class as `core/actions.py`'s `ActionRunner` —
different mutation shape (content diff vs. pure git-plumbing invocation), own
one-in-flight lock, own audit logger (`dispatcher.actions.spec_runner_config`) so
the two action classes can be reasoned about and tested independently.

Flow: `validate(candidate)` → `build_diff(project_dir, candidate)` (unified diff,
`spec_runner:` block only) → write that block to the on-disk `project.yaml` in the
target workspace → `github-checker open-pr <dir>` (existing subcommand, unchanged;
it already wraps a dirty worktree into branch+push+`gh pr create`) → return
`ActionOutcome`-shaped result (PR URL, ok/error) → audit line for every attempt,
including rejected/busy ones, matching the existing `core/actions.py` logging shape.

Guards, all reused from the existing whitelist-action pattern: explicit human click
only (never called by refresh/poll logic), CSRF token, one in-flight action per
repo, full audit line per attempt.

**Open question (verify at implementation time):** confirm `github-checker open-pr`
creates its own branch from a dirty worktree rather than requiring the caller to
have already checked one out — the existing `pull`/`create-pr` actions assume this,
but this design is the first caller that *itself* dirties the tree before invoking
it. If that assumption is wrong, `open-pr` needs a small github-checker-side
adjustment (new handoff, not this design's work).

### DESIGN-305: API

- `GET /api/projects/{name}/spec-runner-config` → effective config + per-field
  explicit/default tags (DESIGN-302). Read-only, no action guard.
- `POST /api/actions/update-spec-runner-config` `{repo_dir, candidate}` → validates
  (DESIGN-303), builds diff, returns diff preview OR (on confirm) executes
  (DESIGN-304). Mirrors the existing `POST /api/actions/*` shape.

### DESIGN-306: Web UI — Config screen (M1)

Per-project screen: typed fields as a form; personas as a role→
(system_prompt/model/focus) table; review settings, telegram, webhook, budgets,
hook flags as grouped sub-forms mapping into `extra_executor_config`. Diff preview
before the PR button is enabled — human sees exactly what will be proposed, not just
the form values.

### DESIGN-307: AI-agent value suggestions (M1, scoped)

Given the project description and roadmap context (already available via existing
`/api/roadmap`/`/api/projects/{name}`), an agent pre-fills empty fields in the editor
form with suggested values before the human reviews. Scope for this iteration is
**recommendation only** — no validation, no field explanation, no autonomous
drafting beyond pre-filling the form (explicitly deferred per this session's
decision: those other assist modes are "not important yet"). The human edits and
approves before anything reaches DESIGN-304; the agent never touches the PR path
directly.

### DESIGN-308: TUI parity (M2, deferred)

Config screen mirrored in the TUI, closing this feature's slice of `FR-06`
(terminal/IDE parity). Out of this design's M1 acceptance.

## 4. Error handling / degradation

| failure | behaviour |
|---|---|
| candidate fails schema validation (DESIGN-303) | 422, no diff/branch/PR created, audit line records rejection |
| `project.yaml` changed on disk between form render and submit | reject with "reload required" (detect via mtime/hash captured at render time), never silent overwrite |
| repo already has an action in flight (either class) | 409, per-class lock (sync-action lock and content-PR lock are independent — a `pull` in flight on repo X does not block a config edit on repo X, and vice versa; each still serializes within its own class) |
| `github-checker open-pr` fails (auth, network, `gh` missing) | surfaced as-is, no auto-retry, matches existing `pull`/`create-pr` behavior |
| provisional schema (DESIGN-301) itself drifts from spec-runner's real `ExecutorConfig` | dispatcher has no way to detect this until the handoff schema lands — documented risk, not solved by this design |

## 5. Testing

- Golden fixtures: `project.yaml` before/after for a representative diff (typed
  field change + `extra_executor_config` addition), asserting the diff touches only
  the `spec_runner:` block.
- Validation unit tests: valid/invalid typed fields, valid/invalid
  `extra_executor_config` shapes (bad persona shape, unknown top-level key, wrong
  type) against the DESIGN-301 provisional schema.
- Concurrency test: two submissions against the same repo → second gets 409 from the
  content-PR lock while the first is in flight; a simultaneous `pull` on the same
  repo is unaffected (different lock).
- Audit line assertions: rejected, busy, and successful attempts each produce one
  line, matching the existing `core/actions.py` test pattern.
- `github-checker open-pr` invocation itself is mocked/stubbed in dispatcher's test
  suite, consistent with how `pull`/`create-pr` are tested today.

## 6. Documentation updates required

Tracked here so they become concrete tasks in the implementation plan, not
forgotten:

- `CLAUDE.md` — replace "neighbors read-only, never edit" framing with: ad-hoc edits
  remain forbidden; the **running dispatcher application** may perform whitelisted
  PR-only content-edit actions (this is a statement about the shipped tool's runtime
  behavior, not a relaxation of the *development-time* rule that a coding session
  must not hand-edit neighbor repos).
- `spec/discovery-brief-customer.md` — amend `NFR-01`/`CON-02` wording, append
  resolved conflict **X-02** (mirroring how X-01 is recorded).
- `spec/discovery-brief-engineer.md` — remove "dispatcher stays view-only" as a
  blanket invariant; keep it scoped to sync actions only.
- `docs/superpowers/specs/2026-07-14-sync-roadmap-design.md` — do **not** rewrite
  DESIGN-204's meaning retroactively; add a forward reference/new section noting
  this design's content-PR class as a sibling, not a replacement.
- `core/actions.py` docstring — its opening claim ("Dispatcher never mutates
  observed repos itself") becomes false at the whole-application level once
  DESIGN-304 ships; update to scope the claim to sync actions and cross-reference
  `core/spec_runner_config_actions.py`.

## 7. Out of scope

- Editing anything in `project.yaml` outside the `spec_runner:` block, or any file
  other than `project.yaml`.
- Per-task/per-workstream config (Maestro has no such concept today — one profile
  per project, confirmed by explore: no config DB table, no CLI override, source of
  truth is the single checked-in `project.yaml`). A per-workstream override would be
  a separate, larger Maestro-side feature and a separate design.
- Any AI-agent behavior beyond value suggestion (validation-by-agent, chat-driven
  drafting, field explanations) — explicitly deferred.
- Direct commits to a default branch, background writes, or any action not in the
  two whitelists (sync, content-PR).

## 8. Traceability

| Item | Design |
|---|---|
| X-02 (this session, content-PR whitelist) | §1, DESIGN-304 |
| Extends brief FR-04 (onboarding view) scope | DESIGN-302, 305, 306 |
| Prerequisite: Maestro `extra_executor_config` (commit `0122942`) | §0 |
| Prior handoff: `2026-07-17-maestro-specrunnerconfig-gaps-handoff.md` | §0, now resolved by Maestro's own change |
| New handoff: spec-runner `schemas/executor-config.schema.json` | DESIGN-301 |

## 9. Milestones

- **M1:** DESIGN-301 (provisional), 302, 303, 304, 305, 306, 307 — read + edit +
  AI-assisted suggestions, web only, end-to-end via PR.
- **M2:** DESIGN-308 (TUI parity, FR-06 slice); promote DESIGN-301's provisional
  schema to a real vendored copy once the spec-runner handoff lands.

## 10. Handoffs

| ID | Repo | What |
|---|---|---|
| H-4 | spec-runner | publish `schemas/executor-config.schema.json` (generated from `ExecutorConfig.model_json_schema()`), matching the existing `schemas/*.schema.json` convention |
| H-5 (conditional) | github-checker | only if the DESIGN-304 open question resolves unfavorably — `open-pr` would need to accept an already-dirty worktree it did not itself create |
