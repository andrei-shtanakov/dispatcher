# Human queue, slice A1 — dispatcher's own and local sources

**Status:** 2026-09-29, r2 for the local review loop (r1 findings applied).
**Parent:** `docs/superpowers/specs/2026-09-29-human-control-plane-design.md` (§3, slice A,
step A1 of §7). This document narrows that one; where it deviates, the deviation is
named in §9.

**Citations** follow the slice-0 rule: neighbour code is cited workspace-relative and
repository-first (`maestro/maestro/database.py:70`) and is not a file of this
repository; unprefixed paths are dispatcher's own.

## 1. Goal

`GET /api/human-queue` returns every wait for a human that dispatcher can see **from
sources it already reads locally**, with completeness stated per source. It is a read
model only: no UI, no forge access, no new writer of anyone else's state. The forge
sources (labelled PRs, candidate PRs) arrive in A2; until then the view is partial **by
construction** and says so.

## 2. Non-goals

| Not in A1 | Why |
|---|---|
| Any UI (web tab, VSCode tree, status bar, TUI) | Slice B |
| Forge sources | A2; needs a `github-checker` read verb (handoff) |
| Command strings | B renders commands from the typed `act`; A1 carries arguments only |
| Governance runner `stopped_*` | Uncontracted, machine-local (parent §3.1) |
| Legacy `maestro.db` | Frozen pre-#147 file, forensics only (`core/collectors/maestro.py`); a wait in it is not actionable |
| MCP exposure | Follows B's parity work |

## 3. Sources

Five sources. Each yields waits plus one `SourceStatus`.

| `source` | Reads | Reasons produced |
|---|---|---|
| `dispatcher_runs` | own `RunStore` | `launch_unknown` |
| `maestro` | per-run `state.db` under `effective_maestro_home` | `run_needs_review`, `run_awaiting_approval` |
| `impresario` | `read_api.product_proposals(cache, "impresario")` | `loop_needs_human`, `proposal_gate`, `backlog_gate` |
| `forge_labelled_prs` | — | none in A1 (`not_connected`) |
| `forge_candidate_prs` | — | none in A1 (`not_connected`) |

### 3.1 `dispatcher_runs`

- Control plane off (`run_state_dir` unset) → `not_configured`. dispatcher then launches
  nothing, so no `launch_unknown` can originate here; this is not incompleteness.
- `RunStore.list()` returns records plus unreadable filenames. Any unreadable entry
  (including "requests directory unlistable") → `partial` with the names in `detail`;
  readable records still produce waits.
- Every record in state `launch_unknown` is one wait. Its `act` is a route to the
  existing run view, where `/resolve` lives: `{"kind": "run_view", "request_id": …}`.

**Wait age.** Today no field says when a record entered `launch_unknown`: the file
mtime is refreshed by every write, and the parent spec forbids mtime as `since`. The
store is dispatcher's own, so A1 adds one: `LaunchRecord.unknown_at: str | None`,
stamped (UTC ISO-8601) by `RunStore.mark_unknown` **only on entry**, i.e. when the
record's prior state is not `launch_unknown`. A repeated mark on a record already in
`launch_unknown` keeps whatever it has — including `None` on a record that entered the
state before the field existed. Stamping "now" there would present the time of the
repeat as the start of the wait. Records without the field read `None` →
`since: null`.

### 3.2 `maestro`

**The existing snapshot is not used.** `MaestroCollector` reads tasks only from the
newest run of each project and caps them at `ORDER BY created_at DESC LIMIT 50`
(`core/collectors/maestro.py:49`, `_collect_runs`). Both cuts drop waits. A1 adds its
own read:

1. Enumerate runs with `classified_runs(home, snap)` — the single place run status is
   decided, so the queue and the dashboard cannot disagree about a run.
2. Only **non-terminal** runs feed the queue: status `running`, `suspended`, or
   `interrupted`. This is a **product policy, not a maestro constraint**: maestro does
   let an operator select a terminal run by id (`maestro/maestro/run_registry.py:205`,
   `maestro/maestro/run_bootstrap.py:86`) and its task verbs check the task status, not
   the run's `outcome` — `retry` / `approve` move such a task to `READY` without
   looking at the run (`maestro/maestro/cli.py:1316-1321`, `:1359`). So the wait *can*
   be cleared. The policy's reason is scope, not impossibility: the queue lists waits
   of **active work**, and an `outcome` is a recorded decision that the run is over
   (`completed`, `failed`, or closed by `run-end` as `cancelled` / `superseded`).
   Reopening a finished run is a deliberate operator choice, not something the queue
   should prompt for every leftover task. Such leftovers belong to the factory-floor
   view (parent §5).
3. For each non-terminal run:
   `SELECT id, title, status, agent_type FROM tasks
   WHERE status IN ('needs_review', 'awaiting_approval') ORDER BY id` — no recency
   window, no limit.
4. Status values are maestro's `TaskStatus` strings `needs_review` /
   `awaiting_approval` (`maestro/maestro/models.py:51`, `:57`).

Completeness: any enumeration warning from `classified_runs` (the `runs `/`run `
prefixed warnings), any run classified `unreadable`, and any failed task query →
`partial`, detail listing each. An absent `projects/` directory is a clean zero
(`ok`), as in the collector.

**A run directory without `state.db` must not vanish from the queue silently.**
`classified_runs` skips it today (`core/collectors/maestro.py:201-205`:
`if not db.is_file(): continue`). maestro stages a new run under
`<project>/.staging/<id>` and renames it into `runs/<id>` whole
(`maestro/maestro/run_publish.py:70`), yet dispatcher's control plane treats such a
directory as a real transient — "mid-materialization, or a launch that died between
mkdir and the db write" — and maps it to `RUN_IN_FLIGHT` for admission
(`core/run_controller.py:245-251`, `:338-350`). A warning inside the shared walk would
therefore change admission and launchpad behaviour, which A1 must not do. So
`classified_runs` gains a keyword `report_missing_state: bool = False`; only the queue
passes `True`, and then each such directory adds a warning
`run <id>: no state.db in <dir> (in flight or damaged)`. Every other caller is
unchanged. For the queue it means `partial` — honest whether the directory is
transient or damaged, since a wait inside it cannot be read either way.

`act`: the slice-0 verb map (slice-0 spec §6) — `needs_review` is cleared by `retry`,
`awaiting_approval` by `approve`. If a dispatcher `LaunchRecord` holds this `run_id`
under the same `repo_key`, the act is `{"kind": "run_view", "request_id": …}` (the run
view already drives verbs with the correct checkout). Otherwise
`{"kind": "maestro_verb", "verb": "retry" | "approve", "task_id", "run_id",
"repo_key"}`; B decides how to render it (it needs a checkout to run from, which
`repo_key` alone does not give — slice-0 finding on verb `cwd`).

**Wait age.** `since: null` for both reasons. maestro's `tasks` table carries
`created_at`, `started_at`, `completed_at` and no time of entering a status
(`maestro/maestro/database.py:70-94`). Asking maestro for a transition timestamp is a
possible later handoff, not an A1 dependency.

### 3.3 `impresario`

Delegates to the existing pass-through `read_api.product_proposals(cache, "impresario")`
(`core/read_api.py:108`); nothing is re-classified.

- Report diagnostic `mirror-not-detected` → `not_configured`. `SnapshotService`
  always lists every collector and marks an absent one `detected=False`
  (`core/service.py:91-95`), and `read_api.product_proposals` answers that with this
  diagnostic (`core/read_api.py:119`). It is how "this dispatcher does not observe
  impresario" arrives; `ReadLookupError` is handled the same way but is unreachable in
  practice.
- Report diagnostic `mirror-anchors-missing` → `unavailable`: the mirror was detected
  but its anchors vanished before the scan (`core/product_proposals.py:860`).

Known limit: a mirror whose anchors are gone *before* discovery is not detected at all,
and so reads as `not_configured`, not as `unavailable`. dispatcher has no declaration
of which projects it is expected to observe; closing this needs one, and is out of A1
(§7).
- `report.attention` true (a non-ok bundle or a report-level diagnostic) → `partial`;
  waits of `ok` bundles are still returned. A non-ok bundle's empty waits mean
  "suppressed", never "nothing waits" (`core/product_proposals.py:198-207`).

Waits:

| Collector record | reason | key | since / basis |
|---|---|---|---|
| `LoopWait` | `loop_needs_human` | `impresario-loop:{loop_id}:{iteration}` | `stopped_at` / `"impresario loop stop.at"` |
| `GateWait` | `proposal_gate` | `impresario-gate:{proposal_id}:{gate_id}:{version}` | null — `proposal_updated_at` is explicitly not a wait start (`core/product_proposals.py:129`) |
| `BacklogWait` | `backlog_gate` | `impresario-qg4:{backlog_id}:{version}` | null — see below |

`backlog_updated_at` is the version publication time, which the collector calls a real
freshness signal (`core/product_proposals.py:166`). Whether it is also the start of the
QG-4 wait depends on whether an item can become selectable inside one version. A1 does
not decide that; it records null and names the question (§8).

`act`: `{"kind": "open_artifact", "path": <bundle or artifact path, mirror-relative>}`.
The decision is recorded in impresario, not by dispatcher.

### 3.4 Forge placeholders

Both forge sources appear in `sources` as `not_connected` with a detail naming A2. They
are listed, not omitted, so a reader of the API cannot mistake A1's queue for the whole
one.

## 4. Model

```python
SourceState = Literal["ok", "partial", "unavailable", "not_configured", "not_connected"]

class SourceStatus(BaseModel):
    state: SourceState
    detail: str | None = None

Reason = Literal[
    "launch_unknown", "run_needs_review", "run_awaiting_approval",
    "loop_needs_human", "proposal_gate", "backlog_gate",
]

class RunViewAct(BaseModel):
    kind: Literal["run_view"] = "run_view"
    request_id: str

class MaestroVerbAct(BaseModel):
    kind: Literal["maestro_verb"] = "maestro_verb"
    verb: Literal["retry", "approve"]
    task_id: str
    run_id: str
    repo_key: str

class OpenArtifactAct(BaseModel):
    kind: Literal["open_artifact"] = "open_artifact"
    path: str

Act = Annotated[RunViewAct | MaestroVerbAct | OpenArtifactAct, Field(discriminator="kind")]

class HumanWait(BaseModel):
    key: str
    reasons: list[Reason]
    source: str
    repo: str | None      # repo_key / project name; None when the source has none
    ref: str              # request_id, "run_id/task_id", loop/proposal/backlog id
    title: str
    since: str | None
    since_basis: str | None   # null exactly when since is null
    act: Act

class HumanQueueView(BaseModel):
    waits: list[HumanWait]
    sources: dict[str, SourceStatus]
    complete: bool
    generated_at: str
```

### 4.1 Invariants

- `complete` is true **iff** every source is `ok` or `not_configured`. In A1 it is
  therefore always false (two forge sources are `not_connected`) — intended.
- `since` and `since_basis` are both null or both set; a set `since` parses as
  ISO-8601 with a timezone. An adapter whose source time does not parse emits null
  rather than an unsortable string.
- Key formats (a public contract: A2 and B dedupe and address waits by them):
  `dispatcher-run:{request_id}`, `maestro:{repo_key}:{run_id}:{task_id}:{status}`,
  `impresario-loop:{loop_id}:{iteration}`,
  `impresario-gate:{proposal_id}:{gate_id}:{version}`,
  `impresario-qg4:{backlog_id}:{version}`. The maestro key includes the status, so a
  task that moves from `awaiting_approval` to `needs_review` is a new wait.
- `key` is unique in `waits`. Two records with one key merge into one wait: `reasons`
  is the sorted union; the rest comes from the first record in source order. In A1 no
  two sources can produce the same key (keys are source-prefixed); the rule exists for
  A2 and is tested now so A2 inherits it.
- Order: waits with `since` ascending (oldest first), then waits with `since: null`
  ordered by `key`. The API does not interleave unknown ages among known ones.
- The endpoint answers 200 in every source state. Unavailability is content (same rule
  as `/api/waits`, spec 2026-08-26-waits-graph-design §3.1). An exception inside one
  source adapter becomes that source's `unavailable`, never a 500 and never a silent
  drop of the other sources.

## 5. Structure

- `core/human_queue.py` — the model, `proven_since()`, and
  `assemble(results, *, now) -> HumanQueueView` (pure).
- `core/human_queue_sources.py` — the adapters, each returning
  `SourceResult(name, status, waits)`: `from_run_store(config)`,
  `from_maestro(home, records)`, `from_impresario_report(report)` (pure) plus
  `from_impresario(cache)` (lookup); and `build_human_queue(config, cache, *, now)`,
  which runs each adapter under an isolation guard and calls `assemble`.
- `core/collectors/maestro.py` gains `waiting_tasks(db) -> list[dict]` (the §3.2
  query) and the `report_missing_state` keyword on `classified_runs` (default off);
  the collector's own `_TASKS_SQL` and every existing caller are untouched.
- `core/run_store.py` gains `unknown_at`.
- `server/app.py` gains the route.

The adapter boundary is a design choice for testability: `assemble` holds every
invariant of §4.1 and is tested without the filesystem.

## 6. Acceptance

1. A run with more than 50 **waiting** tasks among other tasks → all of them appear;
   a second non-terminal run older than the newest one holding another waiting task →
   it appears too. (Both cuts of the snapshot, pinned; a limit placed after the status
   filter must fail the test.)
2. A waiting task inside a run with an `outcome` → not a wait.
3. An unreadable run DB, a run whose `run` row reads but whose task query fails, a run
   directory without `state.db`, and an unlistable `runs/` directory → each makes
   `maestro` `partial`, and the healthy run's waits are still present.
4. `launch_unknown` record written by `mark_unknown` → wait with `since == unknown_at`;
   a record without `unknown_at` → `since: null`, `since_basis: null`, and it stays
   null after a repeated `mark_unknown`.
5. Control plane off → `dispatcher_runs: not_configured`, no error.
6. impresario: `LoopWait` gives `since == stopped_at`; `GateWait` and `BacklogWait`
   give null; a non-ok bundle makes the source `partial`; `mirror-not-detected` →
   `not_configured`; `mirror-anchors-missing` → `unavailable`.
7. One adapter raising → that source `unavailable`, HTTP 200, and the other sources
   keep both their status and their waits.
8. `complete` is false in A1 with both forge sources `not_connected`.
9. Two inputs with one key → one wait, reasons unioned.
10. Ordering: known ages oldest first, then unknown ages by key.

## 7. Risks

- **Cost.** The maestro read opens every non-terminal run DB on each request. Run
  counts today are small; if it grows, cache in the existing snapshot service rather
  than add a limit (a limit is exactly the defect this slice removes).
- **Undetected ≠ unavailable for impresario** (§3.3): a mirror lost before discovery
  reads as `not_configured`. Accepted for A1; the fix is an "expected projects"
  declaration, which belongs to discovery, not to the queue.
- **`interrupted` runs accumulate** (slice-0 notes: orphans stay `interrupted` until
  `run-end`). Their waits will show. That is correct — the run is not over — and the
  human act may be `run-end` instead of `retry`. B should say so; A1 only reports.

## 8. Open questions (not blocking A1)

1. QG-4: can an item become selectable within one backlog version? If not,
   `backlog_updated_at` is a proven wait start and `backlog_gate` can carry it. Question
   for impresario's semantics, as an inbox issue if the owner wants it.
2. maestro status-transition timestamp: worth a handoff only if B shows that
   unknown-age maestro waits are a real problem.

## 9. Deviations from the parent spec

- Source states: the parent listed `ok | truncated | unavailable`. A1 uses `partial`
  (covers truncation and suppressed sub-records alike) and adds `not_configured` (the
  feature is off, so nothing can wait) and `not_connected` (the source exists in the
  design but is not built yet).
- The parent's maestro row ("tasks in `NEEDS_REVIEW` / `AWAITING_APPROVAL`") is
  narrowed to non-terminal runs (§3.2, step 2) — a scope policy; waits left in
  finished runs go to slice C.
- Reason `backlog_gate` is added: the parent's source list missed impresario's QG-4
  wait.
- `act` in the parent was "a command string or a UI route"; §4 of the parent already
  moved to typed actions, and A1 follows that.
