# Maestro per-project, per-run state databases — dispatcher-side design

**Status:** accepted 2026-08-16. Inbox issue #147 (`slug: maestro-per-project-run-dbs`,
from: maestro). Producer-side canon: maestro
`docs/superpowers/specs/2026-08-15-maestro-state-layout-design.md` (revision 3),
**already landed** as maestro `a4caef0` — the window §F of that design warns about is
open now: the dashboard is currently reading the frozen legacy file.

## 1. Problem

`dispatcher/core/discovery.py` pins `_DEFAULT_MAESTRO_DB = ~/.maestro/maestro.db` and
the Maestro collector reads that single file. Maestro has moved orchestration state to
one database per run:

    ~/.maestro/projects/<host>/<owner>/<repo>/runs/<run-id>/state.db
    ~/.maestro/projects/_local/<name>-<hash>/runs/<run-id>/state.db
    ~/.maestro/projects/<...>/locks/            # per-project stage locks
    ~/.maestro/maestro.db                       # legacy, frozen, never written again

Note the issue text (`runs/<run-id>.db`) predates revision 3 of the producer design:
a run is a **directory** (`runs/<run-id>/state.db`), and `_local` keys are two path
segments, not three. This spec follows the landed code
(maestro `state_paths.py`, `repo_identity.RepoKey.as_path_parts`), not the issue text.

## 2. Requirements (issue "done when", mapped to the landed contract)

- **R1** The collector enumerates every `projects/*/*/*/runs/*/state.db` and
  `projects/_local/*/runs/*/state.db` under the maestro home, instead of reading one
  path.
- **R2** Runs are reported newest-first per project; tasks shown for a project come
  from its **newest** run, not from the legacy file.
- **R3 (load-bearing)** A run with no terminal record is shown as
  **interrupted** — never as in-progress. "Running" requires positive evidence
  (§4); it is never inferred from a missing terminal record.
- **R4** `~/.maestro/maestro.db` is reported as a **legacy** source (not migrated,
  kept for forensics). Its tasks stay visible — producer design §F: the legacy file
  is what keeps the dashboard rendering something during the transition.

## 3. Data model

Additive field on `ProjectSnapshot` (served by `GET /api/projects/{name}` and the MCP
`project` tool unchanged — pydantic additive):

```python
class OrchestrationRunInfo(BaseModel):
    repo_key: str          # "<host>/<owner>/<repo>", "_local/<name>", or "legacy"
    run_id: str | None     # runs/<id> directory name; None for the legacy db
    status: str            # §4 vocabulary
    started_at: str | None
    ended_at: str | None
    reason: str | None
    source: str            # the state.db path
```

`run_id` is the directory name (mirrors maestro `run_registry.RunInfo`); the row's
own `run_id` column is not trusted over the enumeration.

## 4. Status vocabulary and classification (fail-closed)

Mirrors maestro `run_state.classify_run` with one deliberate hardening (pid check):

| Evidence | Status |
|---|---|
| `run.outcome` not NULL | that outcome: `completed` / `cancelled` / `superseded` / `failed` |
| outcome NULL, holder attributes **this** run and holder pid is alive | `running` |
| outcome NULL, `suspended_at` set | `suspended` |
| outcome NULL, anything else | `interrupted` |
| legacy `maestro.db` (known path, no `run` row expected) | `legacy` |
| enumerated db unreadable, or `run` row missing/unreadable | `unreadable` (+ warning) |

**Attribution:** `<project>/locks/orchestrate.holder` is JSON
`{"pid": int, "run_id": str}` written under the held lock and unlinked on clean
release (maestro `service/locks.py`). Malformed/missing holder → no attribution.

**Divergence from the producer, on purpose:** maestro's own `resolve_runs` consults
the holder file alone. A holder survives a SIGKILL, so holder-alone would report a
crashed run as `running` indefinitely — exactly the lie class #147 exists to remove.
Dispatcher cannot probe the flock itself without *taking* it (a read-plane process
momentarily holding the stage lock could make a concurrent `orchestrate` refuse — an
observable mutation of producer control flow, off-limits under D1). The nearest
non-interfering evidence is the holder's recorded pid: `os.kill(pid, 0)` —
`ProcessLookupError` → dead → no attribution; `PermissionError` → alive; any other
failure → fail-closed, no attribution. Residual accepted: pid reuse can fake
liveness for one collect cycle; the error direction of every other failure mode is
toward `interrupted`, which is the safe side per R3.

An enumerated `state.db` without a readable `run` row is `unreadable`, **not**
`legacy`: producer design §D makes that state "indistinguishable from a corrupted
run" (the rename-into-place ordering guarantees a visible run directory has its
row). Only the known legacy path gets the `legacy` label.

## 5. Configuration

New optional `dispatcher.toml` key `maestro_home`. When absent, the home is derived
as `maestro_db.parent` — which keeps every existing config and test hermetic
(`maestro_db = /tmp/.../maestro.db` → enumeration under `/tmp/.../projects/`, not
the real `~/.maestro`). `maestro_db` keeps its meaning (the legacy file, forensics).

`CollectContext` gains `maestro_home: Path | None = None`; `None` disables
enumeration (embedding/tests back-compat). `SnapshotService` derives it from config.

An absent `projects/` directory is **not** a warning: normal on a machine where
maestro has not run since the layout change. Zero runs render as zero runs.

## 6. What the collector reports

Per project key, runs sorted newest-first by `(started_at, run_id)` descending
(string sort; producer design §C.1 forbids trusting ULID order within a
millisecond, so `started_at` leads and the id only breaks ties). Across projects,
groups sorted by `repo_key`. No caps — the fleet is small; a cap would be a silent
truncation.

From the **newest run per project** additionally:

- tasks (same query/limit as the legacy path; `TaskInfo.source` = that `state.db`);
- OTel errors from `runs/<id>/logs/` (logs moved under the run directory);

The legacy db contributes: its `runs` entry (`status="legacy"`), its tasks (R4),
schema-version check and `maestro.pid` summary — unchanged behavior. Freshness
covers legacy + every enumerated `state.db` + repo-local sources as before.

## 7. Out of scope (follow-ups in TODO.md)

- ~~Web/TUI/VSCode/MCP **rendering** of the `runs` field~~ — closed by
  `@id:maestro-runs-panel-parity` (see §7a).
- Retention/size reporting of `~/.maestro` (producer-side non-goal too).
- Reconciling the legacy database (never migrated, producer decision).

## 7a. Runs panel parity (follow-up, closed)

All surfaces are formatting-only renderers over `ProjectSnapshot.runs`; none
re-classifies. Cross-surface pins:

- **Badge words are identical** on web (`runBadge`), TUI (`_RUN_BADGES`) and
  VSCode (`runs.ts`): `▶ running`, `⏸ suspended (waiting on a human)`,
  `⚠ interrupted / unknown`, `✅ completed`, `⛔ failed`, `∅ cancelled`,
  `↻ superseded`, `🗄 legacy (frozen pre-#147 file)`, `✖ unreadable`; an
  unrecognized producer status renders `✖ <status>` verbatim, never silently
  green.
- **Degradation signal**: warnings the collector emits for run
  enumeration/classification carry the `run ` / `runs ` prefix (pinned by
  `test_run_warning_prefixes_are_pinned`); to make the signal complete, an
  unreadable directory during enumeration now warns
  (`runs enumeration: cannot list …`) instead of silently reading as empty.
- **Zero-state rule**: zero runs on a CLEAN read hides the section (web,
  VSCode) or renders the generic `(none)` (TUI whole-snapshot dump) — most
  projects have no orchestration runs; any `run `/`runs `-prefixed warning
  forces the section open and reads as *unknown, not zero*. A fetch failure
  is fail-loud on web/VSCode (unknown must not look like «no runs»); a 404
  hides the section (unknown project is surfaced elsewhere).
- **MCP**: no 17th tool — the `project` tool already serves the full
  snapshot, `runs` included (unlike product-proposals, where a computed
  report justified a dedicated tool); its description documents the
  fail-closed status semantics. The tool↔HTTP parity test covers the field
  by construction.

## 8. Testing

Hermetic fixtures build a fake maestro home. Covered: enumeration at both depths
(3-segment and `_local` 2-segment); newest-first order with tasks only from the
newest run; every row of the §4 table including holder-with-dead-pid,
holder-for-a-different-run and malformed holder (all → not running); unreadable
db → `unreadable` + warning; legacy labeling; `maestro_home=None` back-compat;
`maestro_home` config parsing and the `maestro_db.parent` derivation.
