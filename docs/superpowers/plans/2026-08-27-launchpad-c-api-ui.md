# Launchpad PR-C: /api/launchpad + Submit v2 + UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** the launchpad becomes real — one snapshot endpoint over B2's
classifier, submit v2 with structured `{code, detail, current}` errors, and
the root UI panel where launching is one confirmed click.

**Architecture:** three layers over what B1/B2 shipped. (1) An assembler
module (`launchpad.py`) reads every store-global source ONCE per assembly,
captures each repository's surfaces once, and derives the §4.1 snapshot
from `classify_inventory` — the same classifier submit uses, pinned by the
§5 adapter property test over frozen injected sources. (2) Submit v2
replaces the field-typing API: the client sends `{snapshot_id, repo_key,
work_id, request_id, seen_revision}`; the server recovers the DAG path from
canon and re-validates inside ONE guard section that also reserves — the
existing `submit()` body is refactored into named reusable stages
(replay / guarded-admission / spawn) so v2 composes them without ever
nesting the non-reentrant guard. Every non-receipt answer is a structured
code; classified 409s persist and replay verbatim. (3) The UI gains a
`#launchpad` root section (repository rows, typed blockers, Ready rows with
two-step confirm, Active/Recent lists, both escape forms); the existing run
view stays as drill-down. Tails from B1/B2 land first so later tasks build
on clean ground.

**Tech Stack:** Python 3.13, FastAPI + pydantic, uv/pytest; vanilla-JS
single-file UI (`dispatcher/server/static/index.html`) tested by node VM
harnesses (`tests/web/*_harness.js` over `dom.js` — NOTE: the sandbox has
no `AbortController`, no visibility API, and inert `addEventListener`; the
plan's JS designs within those limits).

**Spec:** `docs/superpowers/specs/2026-08-26-launchpad-design.md` —
§4 (data surfaces), §5 (one classifier, two adapters), §8.2 (replay),
§9 (UI), §10 (testing), §12 (named limits). B1 = merged #200,
B2 = merged #204; this plan consumes their shipped interfaces as-is.

## Global Constraints

- **One classifier, two adapters (spec §5):** the assembler and submit v2
  call the SAME `classify_inventory`/`classify_repo` through the SAME
  extracted capture functions. The property test targets the adapters over
  frozen injected sources, not the classifier.
- **Every source read once per assembly (spec §4.1):** store-global reads
  (`RunStore.list()`, `classified_runs`) happen exactly once per snapshot
  and are threaded into per-repo capture as parameters — never re-read per
  repo.
- **Clients classify by `code`, never by bare HTTP status (spec §4.2).**
  `current` in a 409 is for the operator's message only; the UI never
  splices it into the snapshot — it refetches whole.
- **Immutable replay (spec §8.2):** a repeated `request_id` with a matching
  fingerprint replays the persisted decision field-for-field without
  re-classification; a mismatch is `request_id_conflict`. This binds EVERY
  prior state, including `reserved`.
- **The wire vocabulary IS `admission.py`'s constants** (its own docstring:
  "the single vocabulary shared by receipts now and 409s in PR-C") —
  `launch_busy`, never a second spelling. Task 1 corrects the spec table's
  stray `lock_busy`.
- **Fail-closed with named facts:** anything unreadable is a named
  blocker/error; per-repo breakage never hides the neighbours' rows —
  EXCEPT store-global unreadable records, which block every row exactly as
  shipped submit already does (`list_unreadable` joins every repo's
  `runs_unreadable` — parity beats blast-radius comfort).
- **UI discipline (spec §9):** `snapshot_id` is opaque — staleness is
  guarded by a client request-sequence. §9 offers "one fetch in flight at
  a time (or abort the previous)"; with no AbortController in the sandbox
  this plan implements the ABORT alternative semantically: the TIMER path
  never overlaps (refused while anything is in flight), an ACTION refetch
  MAY supersede an in-flight timer fetch with a higher seq, and the
  sequence guard discards the superseded response instead of aborting its
  transport. 409 → text + whole refetch; transport uncertainty is a
  first-class row state retrying the SAME `request_id`.
- **Permission-denied in tests is modeled by injected reader/stat errors,
  never chmod (spec §10).** Live-smoke baseline (3 standing failures) is
  not this plan's regression surface.
- Package hygiene: `uv` only; `ruff format`+`ruff check`+`pyrefly check`
  clean after every task; line length 88; web harnesses run under plain
  `node`, no new npm dependencies.

---

### Task 1: Tails first — types layering, spec touch-ups, M7 comment

**Files:**
- Create: `dispatcher/core/inventory_types.py`
- Modify: `dispatcher/core/dag_subset.py` (verdict dataclasses move OUT)
- Modify: `dispatcher/core/inventory.py` (dataclasses move OUT; re-export)
- Modify: `dispatcher/core/admission.py` (imports move to the types module)
- Modify: `docs/superpowers/specs/2026-08-26-launchpad-design.md`
- Test: `tests/test_layering.py` (new)

**Interfaces:**
- Consumes: existing `PlanItem`, `DagFileInfo`, `InventorySurface`
  (`inventory.py`) and `Accepted`, `Rejected`, `DagSubsetVerdict`
  (`dag_subset.py`).
- Produces: ALL SIX shapes importable from
  `dispatcher.core.inventory_types` — field-identical, frozen, no
  behaviour change, and the module imports NOTHING beyond stdlib
  dataclasses/typing and `run_identity` (for `RepoKey`). `dag_subset.py`
  and `inventory.py` import the shapes from there and re-export them
  (existing importers keep working). `admission.py` imports only
  `inventory_types` + `DAG_RE` from `plan_fields.parser` (import-light:
  parser pulls canonical/epic/scrape, never jsonschema/yaml).

- [ ] **Step 1: Write the failing layering test**

```python
"""Import-graph discipline: the pure classifier must not pull IO modules.

B2's final review (M5): admission.py ("pure functions, no IO by
construction") imported inventory.py (subprocess, os) for its dataclasses,
making yaml+plan_fields an import-time dependency of run_controller and
defeating roadmap.py's guarded lazy import. The verdict dataclasses move
too: without that, the types module would import dag_subset and pull yaml
right back in.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).parent.parent


def _imports_of(module_rel: str) -> set[str]:
    tree = ast.parse((ROOT / module_rel).read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_admission_does_not_import_io_modules():
    imports = _imports_of("dispatcher/core/admission.py")
    assert "dispatcher.core.inventory" not in imports
    assert "dispatcher.core.dag_subset" not in imports


def test_types_module_imports_nothing_heavy():
    imports = _imports_of("dispatcher/core/inventory_types.py")
    forbidden = {
        "subprocess", "os", "yaml",
        "dispatcher.core.inventory", "dispatcher.core.dag_subset",
    }
    assert not (imports & forbidden)
```

- [ ] **Step 2: Run to RED** — `uv run pytest tests/test_layering.py -v`

- [ ] **Step 3: Move the shapes**

`inventory_types.py` (docstring: "Frozen captured-fact shapes shared by
the IO capture, the pure classifier and the subset discriminator —
import-light by design, B2 review M5"): move `Accepted`, `Rejected`,
`DagSubsetVerdict` (from `dag_subset.py`) and `PlanItem`, `DagFileInfo`,
`InventorySurface` (from `inventory.py`) verbatim with docstrings.
`dag_subset.py` and `inventory.py` import-and-re-export them so
`tests/test_dag_subset.py`, `tests/test_inventory_capture.py` and every
other existing importer keep working unchanged. `admission.py` switches
its imports to `inventory_types`.

- [ ] **Step 4: Run to GREEN** —
  `uv run pytest tests/test_layering.py tests/test_inventory_capture.py tests/test_classify_inventory.py tests/test_inventory_end_to_end.py tests/test_admission.py tests/test_dag_subset.py -v`

- [ ] **Step 5: Spec touch-ups (recorded rulings — text only)**

In `docs/superpowers/specs/2026-08-26-launchpad-design.md`:
1. §5.1 condition 3: "…(`dag_duplicate` otherwise, on both items)" →
   "…(`dag_duplicate` otherwise, **on both open items**; a closed
   co-claimant is named inside the open item's reason — a closed item
   appears in no launchpad list)".
2. §6.1: remove `repo_url:` from the Mode-2 marker pair; state the ruled
   truth: "`workstreams:` present marks Mode-2 (`OrchestratorConfig`
   requires it, `ProjectConfig` lacks it). `repo_url:` is legal Mode-1
   remote-URL repo naming — submit's shipped, test-pinned semantics
   (`_reconcile_repo`) accept it with precedence over `repo:`, which is a
   checkout path (ruling on PR #202/#204)."
3. §7: align the linked-unreadable sentence with the owner's B1-review
   override — a linked run with unreadable state classifies
   `run_state_unreadable` (linkage is metadata in `detail`), never
   `run_in_flight`.
4. §4.2 admission-code list: `lock_busy` → `launch_busy` (the vocabulary
   is `admission.py`'s constants; B1's persisted records already carry
   `launch_busy` and a renamed wire code would break stored-replay
   field-equality).
5. §4.1 `recent_completed` example: annotate `run_id` as nullable — a
   terminal `admission-rejected` or `vanished-acknowledged` record has an
   outcome to show (§8.3: "recent_completed shows the outcome") and may
   have no run.

- [ ] **Step 6: M7 comment in inventory.py**

Above `_hash_object`: "git hash-object --stdin applies no gitattributes
filters, while ls-tree returns the FILTERED blob: on a repo with
`* text=auto` or a clean filter this comparison reports a permanent
dag_dirty — fail-closed direction, named limit (B2 review M7)."

- [ ] **Step 7: Hygiene + full suite + commit**

```bash
uv run pytest && uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/inventory_types.py dispatcher/core/inventory.py \
        dispatcher/core/dag_subset.py dispatcher/core/admission.py \
        tests/test_layering.py docs/superpowers/specs/2026-08-26-launchpad-design.md
git commit -m "refactor(core): inventory types module; spec touch-ups from B1/B2 rulings"
```

---

### Task 2: Capture extraction + snapshot assembler

**Files:**
- Modify: `dispatcher/core/run_controller.py` (extract capture stages)
- Create: `dispatcher/core/launchpad.py`
- Create: `tests/test_launchpad_assembler.py`

**Interfaces:**
- Consumes: `capture_inventory` (`inventory.py`), `classify_inventory` /
  `classify_repo` / `CapturedInputs` (`admission.py`), `RunStore.list()`,
  `classified_runs` (`collectors/maestro.py`).
- Produces — **extraction in `run_controller.py`** (behaviour-preserving;
  the existing methods become thin delegates and every existing test stays
  green):

```python
def read_lock_state(store, key, request_id=None): ...   # was _read_lock_state

def capture_run_facts(
    store, key, runs_root, home,
    *, records, list_unreadable, classified, scratch_warnings,
) -> tuple[tuple[RunFact, ...], tuple[str, ...]]:
    """The body of the old _capture_run_facts with its GLOBAL reads
    hoisted to parameters: `records`/`list_unreadable` from ONE
    `store.list()`, `classified`/`scratch_warnings` from ONE
    `classified_runs` walk. Submit passes fresh single reads; the
    assembler passes the same objects to every repo — spec §4.1's
    "every source read once" made structural."""
```

  The method `RunController._capture_run_facts(store, key)` remains and
  performs the two global reads itself, then delegates — submit's
  behaviour is bit-identical.

- Produces — **`launchpad.py`** (pydantic models consumed by Tasks 3/5/6):

```python
class BlockerView(BaseModel):
    code: str
    request_id: str | None = None
    run_id: str | None = None
    detail: str | None = None

class RepoRow(BaseModel):
    repo_key: str            # canonical text form
    repository: str          # display label (manifest name)
    default_branch: str
    seen_revision: str | None    # full 40-hex; None on capture failure
    admission: str               # "ready" | "blocked" | "unreadable"
    blockers: list[BlockerView]

class ReadyRow(BaseModel):
    repo_key: str
    work_id: str
    dag_path: str
    seen_revision: str

class BlockedRow(BaseModel):
    repo_key: str
    work_id: str
    dag_path: str | None
    reason_code: str
    reason: str

class UnregisteredRow(BaseModel):
    repo_key: str
    work_id: str
    reason_code: str         # "no_dag_tag"

class OrphanRow(BaseModel):
    repo_key: str
    dag_path: str

class ActiveRow(BaseModel):
    request_id: str | None   # None = unlinked maestro run
    repo_key: str
    work_id: str | None
    state: str               # record state, or "unlinked-run"
    run_id: str | None
    run_status: str | None   # from classified_runs when a run exists
    attention: bool
    updated_at: str          # ISO from the record file's mtime;
                             # for unlinked runs, the run dir's mtime

class CompletedRow(BaseModel):
    request_id: str
    repo_key: str
    work_id: str
    run_id: str | None       # nullable: admission-rejected / tombstones
    revision: str
    outcome: str
    updated_at: str
    logs_available: bool

class LaunchpadSnapshot(BaseModel):
    snapshot_id: str         # uuid4 hex — opaque, unique per assembly
    generated_at: str
    repositories: list[RepoRow]
    ready: list[ReadyRow]
    blocked: list[BlockedRow]
    unregistered_items: list[UnregisteredRow]
    orphan_dags: list[OrphanRow]
    active: list[ActiveRow]
    active_truncated: bool
    recent_completed: list[CompletedRow]
    completed_total: int
    next_cursor: str | None
    store_unreadable: list[str]   # unreadable record names — global banner

def assemble_snapshot(
    controller: RunController,
    *, recent_limit: int = 20, cursor: str | None = None,
) -> LaunchpadSnapshot: ...
```

Assembly rules (each is a test):
- **Global reads once:** `store.list()` and `classified_runs` are called
  exactly once per assembly (instrumented in tests for BOTH), their
  results threaded into `capture_run_facts(...)` per repo.
- Per manifest repository: resolve the checkout; `capture_inventory` once;
  `read_lock_state` + `capture_run_facts` once; one `CapturedInputs` into
  `classify_inventory`. Unresolvable checkout → `admission="unreadable"`,
  one blocker `{code: "repo_unresolved", detail}`, loop continues.
- `list_unreadable` names join EVERY repo's `runs_unreadable` (exactly as
  shipped submit does — parity §5) AND surface once in
  `snapshot.store_unreadable` for the UI's global banner.
- **The one global store read** is a new
  `RunStore.list_with_mtime() -> tuple[list[tuple[LaunchRecord, str]],
  list[str]]` (record, ISO-mtime pairs + unreadable names); `list()`
  becomes a thin delegate that drops the mtimes, so the instrumentation
  counter sits on `list_with_mtime` and counts BOTH spellings. The
  assembler calls it once, then derives the two views itself: bare
  records (threaded into `capture_run_facts`) and the mtime map (for
  ActiveRow/CompletedRow `updated_at`). A record whose stat fails inside
  the helper gets mtime `""` (sorted last, never an exception) — the
  record itself still counts. Atomic-replace transitions refresh mtime,
  so it is a durable "last transition" timestamp. For unlinked runs, the
  run directory's mtime, same "" degradation.
- `attention` = `state == "unknown"` or `run_status in {"NEEDS_REVIEW",
  "AWAITING_APPROVAL"}`. `active` = every non-terminal record + unlinked
  non-terminal runs (`request_id: None, state: "unlinked-run"`), sorted
  (attention DESC, updated_at DESC), capped at 200 with
  `active_truncated=True` when trimmed.
- `recent_completed` = ALL terminal records (admission-rejected and
  tombstones included — their `outcome` is the row's point, `run_id`
  nullable per the Task 1 spec annotation), ordered
  `(updated_at DESC, request_id DESC)`; `completed_total` = count of all
  terminal; `next_cursor` = base64 of `"{updated_at}\x00{request_id}"` of
  the last returned row (None when exhausted); a passed `cursor` resumes
  strictly after that pair; an unparseable cursor → `ValueError` for the
  endpoint to map to 422 `invalid_request`.
- `logs_available` = the run's log directory demonstrably present (reuse
  the existing `_logs_dir`/`_key_from_record` helpers; extract to module
  level the same delegate way if private).

- [ ] **Step 1: Write the failing tests** — fixture builds 2–3 tmp git
  repos (the `make_repo` pattern from `tests/test_inventory_capture.py`)
  and a real `RunStore`; records created through the store's own API
  (`_reserve_locked` under `guard`, `mark_*` transitions). Cases:
  1. Two repos, one ready item each → both in `ready`; both rows
     `admission="ready"`; two assemblies differ in `snapshot_id`.
  2. A live (non-terminal) record in repo A → A blocked `run_in_flight`;
     repo B still ready.
  3. Missing checkout for one repo → `admission="unreadable"`,
     `repo_unresolved`; others unaffected.
  4. 3 terminal + 2 active records, one active NEEDS_REVIEW → attention
     row first; `completed_total == 3`; `recent_limit=2` → cursor resumes
     to the 3rd exactly once; garbage cursor raises `ValueError`.
  5. Unlinked non-terminal run dir → `ActiveRow(request_id=None,
     state="unlinked-run")`; its repo blocked.
  6. An admission-rejected record → appears in `recent_completed` with
     `run_id=None` and `outcome="admission-rejected"`; counted in
     `completed_total`.
  7. `logs_available` False when the log dir is absent, True when present.
  8. Corrupt record file (bytes written directly INTO the store dir by the
     test — modeling store corruption, not permission) → its name in
     `store_unreadable` AND every repo row blocked `run_state_unreadable`
     (submit parity).
  9. Instrumented counters: exactly ONE `RunStore.list_with_mtime` call
     (which `list()` itself delegates to — the counter catches both
     spellings) and ONE `classified_runs` call for a 3-repo assembly;
     exactly one `capture_inventory` per repo. A record with a failing
     stat (injected via monkeypatched os.stat inside the helper) yields
     `updated_at == ""` sorted last, not an exception.
  10. Cap: 201 active records → 200 rows, `active_truncated=True`.

- [ ] **Step 2: RED → Step 3: extract + implement → Step 4: GREEN** —
  `uv run pytest tests/test_launchpad_assembler.py tests/test_run_controller.py -v`
  (the controller suite proves the extraction preserved submit).

- [ ] **Step 5: Hygiene + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/launchpad.py dispatcher/core/run_controller.py \
        dispatcher/core/run_store.py tests/test_launchpad_assembler.py
git commit -m "feat(core): launchpad snapshot assembler; capture stages extracted once-per-assembly"
```

---

### Task 3: `GET /api/launchpad` endpoint

**Files:**
- Modify: `dispatcher/server/app.py`
- Test: `tests/test_launchpad_api.py`

**Interfaces:**
- Consumes: `assemble_snapshot` (Task 2), the app's existing controller
  wiring (same as `/api/runs/submit`).
- Produces: `GET /api/launchpad?cursor=…&recent_limit=…` →
  `LaunchpadSnapshot` (response_model). `recent_limit` is clamped:
  default 20, **maximum 100** (a larger ask returns 422
  `invalid_request` — the bounded tail is a named bound, not a suggestion).
  Bad cursor → 422 `invalid_request`. `ControlPlaneOff` → 409
  `{code: "control_plane_off", detail, current: null}`. This task defines
  the one shared structured-error helper Task 4 reuses:

```python
def _structured(status: int, code: str, detail: str,
                current: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"code": code, "detail": detail,
                                 "current": current})
```

- [ ] **Step 1: Write failing API tests** (httpx TestClient, the
  `tests/test_run_api.py` pattern): snapshot round-trip over a real tmp
  workspace; pagination via cursor; `recent_limit=101` → 422
  `invalid_request`; garbage cursor → 422; control plane off → 409
  `control_plane_off`; body validates as `LaunchpadSnapshot`.
- [ ] **Step 2: RED → implement → GREEN** —
  `uv run pytest tests/test_launchpad_api.py -v`
- [ ] **Step 3: Hygiene + commit**

```bash
git add dispatcher/server/app.py tests/test_launchpad_api.py
git commit -m "feat(api): GET /api/launchpad snapshot endpoint with bounded tail"
```

---

### Task 4: Submit v2 — staged refactor, canon-recovered fields, structured errors

**Files:**
- Modify: `dispatcher/core/run_controller.py`
- Modify: `dispatcher/core/run_request.py` (SubmitV2 model)
- Modify: `dispatcher/server/app.py` (route body switch + error mapping)
- Test: `tests/test_submit_v2.py`; Modify: `tests/test_run_api.py`

**Interfaces:**
- Consumes: `capture_inventory` + `classify_inventory`, the Task 2
  extracted capture functions, `RunStore` fingerprint/replay machinery.
- Produces:

```python
class SubmitV2(BaseModel):
    snapshot_id: str          # audit echo only — never authority
    repo_key: str             # canonical "<host>/<owner>/<repo>" | "_local/<repo>"
    work_id: str
    request_id: str = Field(pattern=_REQUEST_ID_RE.pattern)
    seen_revision: str        # full 40-hex the operator saw

class AdmissionRefused(Exception):
    def __init__(self, status: int, code: str, detail: str,
                 current: dict | None = None): ...

RunController.submit_v2(self, body: SubmitV2) -> LaunchReceipt
    # raises AdmissionRefused for every structured non-receipt outcome
```

**Prerequisite refactor (behaviour-preserving, proven by the existing
suite):** split `submit()`'s body into three named stages it then calls
in order —

```python
def _replay_existing(self, store, request_id, *, raw_repository, work_id,
                     revision) -> LaunchReceipt | AdmissionRefused | None:
    """The §8.2 branch: fingerprint identity for EVERY existing state
    (reserved included — checked here, before any workspace resolution),
    admission_rejected → the PERSISTED structured refusal as an
    AdmissionRefused VALUE (v1 renders it into a receipt string; v2
    raises it verbatim), receipt states → the LaunchReceipt replay.
    None = no record, proceed.

    Identity dimensions differ by caller and MUST NOT be conflated:
    v1 passes raw_repository=request.repository and the raw-vs-stored
    comparison runs as today; v2 has no raw repository field, passes
    raw_repository=None, and the raw check is SKIPPED for it — identity
    is then carried entirely by the fingerprint, whose repo dimension is
    the CANONICAL repo_key TEXT — and `fingerprint_of(repo_key: str,
    work_id, revision)` already takes exactly that text
    (run_store.py:91), so v2 needs NO parsing at all: after a pure shape
    check of `body.repo_key` (2–3 non-empty safe segments, the
    `safe_path_parts` charset; malformed → 422 invalid_request, not
    persisted), the candidate fingerprint is
    fingerprint_of(body.repo_key, work_id, seen_revision) — computed
    before any manifest or filesystem access, as §8.2 demands. A
    v1-created record replayed through v2 compares canonically — never a
    false request_id_conflict from comparing a manifest name to a key.
    Tests pin the cross-version replay, the malformed-repo_key 422, and
    that a v2 repeat naming a DIFFERENT repo_key conflicts."""

def _repo_admission(self, store, key) -> RepoAdmission:
    """read_lock_state + capture_run_facts + classify_repo over one
    captured set — called INSIDE the caller's guard section. v1 and v2
    do NOT share one guard-block function (their in-guard work differs:
    v2 adds inventory capture and the item gate); they share this verdict
    primitive, the capture functions and the classifier — which is where
    §5's equivalence actually lives."""

def _spawn_reserved(self, store, record, validated, catalog,
                    runs) -> LaunchReceipt:
    """The launch tail: everything submit() does today AFTER the guard
    releases — spawn maestro, mark transitions, receipt. The signature is
    dictated by the existing tail's real inputs (store, the reserved
    record, ValidatedRequest with checkout+key, the catalog path, the
    runs dir) — the implementer lifts the code as-is, renaming nothing.
    `_catalog_path()` stays where it is today: resolved and error-mapped
    BEFORE reservation, in both v1 (RunRejectedError → refusal receipt)
    and v2 (RunRejectedError → 422 invalid_request, not persisted — a
    missing catalog is an environment fact, not an attempt decision)."""
```

`submit()` (v1) = `_replay_existing` (rendering an `AdmissionRefused`
value into today's `f"{code}: {detail}"` receipt string — bit-identical
wire behaviour) → `validate_request` + `_catalog_path()` → its own
`store.guard(key)` block (calling `_repo_admission` +
persist-refusal-or-`_reserve_locked`, exactly today's block reshaped) →
`_spawn_reserved`. Every existing `tests/test_run_controller.py` test
must pass unchanged after the refactor — that is the extraction's gate.

**`submit_v2` flow** (each numbered row is a test):
1. `store.get` raising `RunStoreError` (hostile request_id) → 422
   `invalid_request`, not persisted.
2. `_replay_existing` first — BEFORE any resolution: reserved-state
   fingerprint mismatch → 409 `request_id_conflict`; admission_rejected →
   409 with persisted fields, **zero classifier calls** (instrumented);
   receipt states → 200 replay.
3. `repo_key` not in the manifest / checkout absent → 409
   `repo_unresolved` (NOT persisted: transient workspace drift, §4.2).
4. Checkout resolves to a different `RepoKey` → 422 `identity_mismatch`
   (not persisted — a workspace fact, not an attempt decision).
5. v2's OWN `store.guard(key)` block (one guard, no nesting):
   `capture_inventory` on the checkout, `_repo_admission`, then the item
   decisions in this order —
   a. no open item with `item_id == work_id` → `item_unregistered`
      (an id-less/closed-only match is the same code; a CLOSED item with
      that id → `item_closed`);
   b. item present but `dag_raw is None` → `item_unregistered`;
   c. item's decision blocked → its `reason_code`
      (`dag_invalid`/`dag_duplicate`/`dag_dirty`) with
      `current={"reason": …}`;
   d. only THEN `seen_revision != inv.head_revision` → `revision_moved`,
      `current={"seen_revision": head}` — revision is checked LAST so a
      nonexistent item can never be persisted as `revision_moved`;
   e. repo blockers (classify_repo inside the same captured set) → the
      blocker's code with `current={"blockers": […]}`.
   Every item/classifier refusal PERSISTS via
   `store.record_admission_rejection(…, current=…)` before raising —
   replay is forever. Capture-level `OSError` inside the block → 409
   `repo_unresolved`, NOT persisted (environment, not a decision).
6. Clean → the gate returns the recovered internal fields
   (`tasks=<item's dag_path>`, `repository=<manifest name>`,
   `revision=seen_revision`); still INSIDE the same guard v2 builds the
   internal `RunRequest` from them and runs `validate_request` (git-level
   checks read the checkout — a failure → 422 `invalid_request`, not
   persisted) yielding the `ValidatedRequest`; `_reserve_locked` follows
   in the same guard; after release
   `_spawn_reserved(store, record, validated, catalog, runs)` launches.
   (`_catalog_path()` was resolved before the guard, next to replay.) The receipt's three-valued
   `accepted` keeps its slice-0 meaning — it now speaks only about the
   launch phase.
7. `GuardBusyError` → 409 `guard_busy`, not persisted (the guard never
   touched a lock file — B1 semantics).

Route (`app.py`): `POST /api/runs/submit` reads raw JSON once; any legacy
key among `{"revision","tasks","repository","spec_ref","plan_ref"}` → 400
`legacy_body` with the pointer text. Then `SubmitV2` validation (422
`invalid_request` with pydantic's messages), `submit_v2`, and
`AdmissionRefused` → `_structured(status, code, detail, current)`.
`ControlPlaneOff` → 409 `control_plane_off`.

- [ ] **Step 1 (refactor): run the full controller suite, split `submit`,
  run again — zero diffs in outcomes.** Commit the refactor separately:
  `git commit -m "refactor(core): submit split into replay/admit/spawn stages"`
- [ ] **Step 2: Write the failing v2 tests** — one per numbered row, plus:
  legacy body → 400; schema garbage → 422; a record created through v1
  (raw repository stored) replays through v2 without a false
  `request_id_conflict` (canonical-fingerprint path); replayed 409 FIELD-WISE equal
  after the workspace changed (edit the DAG between attempts; compare
  code+detail+current, not JSON bytes) with an instrumented classifier
  asserting zero calls on replay; a clean v2 submit produces a receipt and
  the record carries `tasks == "dags/<work_id>.yaml"` recovered from
  canon; `spec_ref`/`plan_ref` are absent from v2 (recovered refs are a
  future concern — the record's fields stay empty, pinned).
- [ ] **Step 3: RED → implement → GREEN** —
  `uv run pytest tests/test_submit_v2.py tests/test_run_controller.py tests/test_run_api.py -v`
  (`tests/test_run_api.py`'s submit-shape tests are UPDATED to the v2 body
  in this task — count them in the report).
- [ ] **Step 4: Hygiene + commit**

```bash
git add dispatcher/core/run_controller.py dispatcher/core/run_request.py \
        dispatcher/server/app.py tests/test_submit_v2.py tests/test_run_api.py
git commit -m "feat(api): submit v2 — canon-recovered fields, structured 409 taxonomy"
```

---

### Task 5: The §5 adapter property test — frozen sources

**Files:**
- Create: `tests/test_adapter_equivalence.py`

**Interfaces:**
- Consumes: `assemble_snapshot` (Task 2), `submit_v2` (Task 4),
  `classify_inventory` (wrapped), the extracted capture functions
  (monkeypatch seam).

- [ ] **Step 1: Write the test over FROZEN sources.** Build one real tmp
  workspace; run the real captures ONCE in the test itself to produce a
  frozen `(inventory_surface, lock_state, run_facts, runs_unreadable)`
  set; monkeypatch `launchpad`'s and `run_controller`'s capture seams to
  return exactly those objects; wrap `classify_inventory` to record
  `(inputs, decision)` per call. Then:
  (a) assemble a snapshot; (b) drive `submit_v2` for the same repo,
  with `_spawn_reserved` stubbed to a no-op fake receipt (the property is
  about admission, not spawning). Assert: the recorded `CapturedInputs`
  from both adapters compare EQUAL (frozen dataclass equality — no
  volatile fields left by construction), and the decisions agree.
  Two scenarios:
  1. clean repo → snapshot lists the item in `ready` AND submit_v2
     reaches the reserve (no `AdmissionRefused` before the spawn stub);
  2. live run injected into the frozen facts → snapshot row blocked
     `run_in_flight` AND submit_v2 raises `AdmissionRefused(code=
     "run_in_flight")` — same code as the row's blocker.
- [ ] **Step 2: RED-verify the test's teeth** — temporarily hand the
  submit seam a run-facts set with the live run dropped (one local edit,
  not committed); scenario 2's "same code" assertion must fail; revert.
  Note in the docstring that this RED was performed.
- [ ] **Step 3: GREEN + hygiene + commit**

```bash
git add tests/test_adapter_equivalence.py
git commit -m "test(core): §5 adapter equivalence over frozen captured sources"
```

---

### Task 6: UI — launchpad root panel, snapshot rendering, drill-down

**Files:**
- Modify: `dispatcher/server/static/index.html`
- Create: `tests/web/launchpad_harness.js`
- Create: `tests/test_launchpad_js.py`

**Interfaces:**
- Consumes: `GET /api/launchpad` (Task 3 shapes verbatim).
- Produces (JS seams Task 7 builds on):
  - `lpState = {seq: 0, applied: 0, inflight: 0, snapshot: null,
    pending: Object.create(null)}` — `inflight` is a COUNTER, not a
    boolean: an action refetch may legitimately overlap a timer fetch, and
    a boolean cleared by whichever settles first would reopen the timer
    path while the other request is still airborne (round-5 review
    finding). Increment on issue, decrement on settle (fulfilled or
    rejected); the timer path refuses while `inflight > 0`. — `pending` keyed by
    `rowKey = repo_key + "\u0000" + work_id` (spelled as the escape in
    source — repo_key's charset cannot contain it, so the join is
    unambiguous), each entry
    `{request_id, seen_revision, snapshot_id, status}`; state lives
    OUTSIDE the DOM, so wholesale rerenders cannot lose an attempt.
  - `lpFetchSnapshot()` — stamps `seq` and issues the request. While one
    fetch is `inflight`, a TIMER tick is skipped (refused outright), but
    an ACTION-triggered refetch (after a submit/escape settles — spec §9's
    refetch discipline) is a SUPERSEDING fetch: it is issued immediately
    with a higher seq. The application rule is strict supersession — a
    response applies ONLY if its seq equals the LATEST issued
    (`seq === lpState.seq`): a superseded response never applies, not
    even temporarily, whichever order the two resolve in. So: at most one
    timer fetch, but an action may legitimately create a second in-flight
    request; no AbortController (the sandbox has none) — supersede-and-
    discard replaces it.
  - `lpRefetchAfterAction()` — the superseding-fetch entry point
    submit/escape handlers call (issues immediately, higher seq).
  - `lpRender(snapshot)` — pure wholesale render of all sections.

UI structure (spec §9): new `<section id="launchpad">` FIRST in the body
(root panel): `#lp-store-banner` (from `store_unreadable`), `#lp-repos`,
`#lp-ready`, `#lp-active`, `#lp-recent`, `#lp-diagnostics` (unregistered +
orphans, collapsed `<details>`). The existing `#run-console` section stays
as the drill-down + manual form (rewired in Task 7). Typed blockers per
row:
- `launch_busy`/`run_in_flight` WITH `request_id` → a link invoking the
  existing run-view opener;
- unlinked `run_in_flight` (bare `run_id`) → id + status text, NO link;
- `run_vanished` → anchor to the acknowledge form (form in Task 7);
- `lock_malformed` → anchor to the release-malformed form (Task 7);
- `lock_io_unreadable`/`run_state_unreadable` → diagnostic text, no
  action control.

Refresh cadence: `lpScheduleRefresh()` uses `setInterval` ONLY when
`typeof setInterval === "function"` (the harness sandbox may omit timers;
the browser has them); the visibility pause is attempted via a guarded
`document.addEventListener("visibilitychange", …)` — inert in the harness
(`dom.js`'s addEventListener is a no-op) and carries the named comment
that the Node DOM cannot model the visibility API (spec §9's honest gap).

- [ ] **Step 1: Write the failing harness** (`launchpad_harness.js`,
  `run_console_harness.js` conventions: whole-script VM, module-local
  fixtures, stubbed `fetch` returning controllable promises). Cases:
  1. A snapshot fixture renders: repo rows with admission classes; ready
     rows show `work_id @ sha7`; recent rows with `logs_available=false`
     render NO link; the store banner appears iff `store_unreadable` is
     non-empty; **rendering preserves the server's row order verbatim**
     (the attention-first SORT is the assembler's job, pinned in Task 2 —
     the harness pins only non-reordering).
  2. Typed blockers: linked in-flight exposes the run-view opener hook;
     unlinked renders text without a link; unreadable codes render no
     action control.
  3. **Sequence guard (spec §10):** construct the two-in-flight state
     through the REAL entry points — the harness's fake `setInterval`
     captures the timer callback; invoke it once (fetch A issued, its
     promise held unresolved); then call `lpRefetchAfterAction()` (the
     entry submit/escape handlers use) — fetch B issued with a higher seq
     while A is in flight. BOTH resolution orders are asserted (two
     sub-scenarios over fresh state): (a) B resolves first with snapshot
     B, then A with snapshot A — the render still shows B over TWO ticks;
     (b) A resolves FIRST with snapshot A, then B with snapshot B — A
     must NEVER apply, not even temporarily (assert the render never
     showed A's content between the two resolutions: strict
     `seq === lpState.seq` application). Additionally, invoke the timer
     callback a second time while a fetch is unresolved — the timer path
     must refuse. Assert EXACTLY two requests hit the stubbed fetch per
     sub-scenario.
  4. Wholesale render: applying a snapshot rebuilds the containers from
     the fixture (assert stale child nodes are gone), never patches.
- [ ] **Step 2: RED** — `uv run pytest tests/test_launchpad_js.py -v`
  (python wrapper mirrors `tests/test_run_console_js.py`).
- [ ] **Step 3: Implement the panel + GREEN.**
- [ ] **Step 4: Hygiene + commit**

```bash
git add dispatcher/server/static/index.html tests/web/launchpad_harness.js tests/test_launchpad_js.py
git commit -m "feat(ui): launchpad root panel — snapshot render, typed blockers, sequence guard"
```

---

### Task 7: UI — launch flow, transport uncertainty, escapes, manual form

**Files:**
- Modify: `dispatcher/server/static/index.html`
- Modify: `tests/web/launchpad_harness.js`,
  `tests/web/run_console_harness.js`
- Test: `tests/test_launchpad_js.py` (extend)

**Interfaces:**
- Consumes: Task 6's `lpState`/`pending` seams; `POST /api/runs/submit`
  v2 (Task 4 body + taxonomy); the shipped escape endpoints:
  `POST /api/runs/{request_id}/acknowledge-vanished`
  `{confirm_run_id, reason, display_name?}`;
  `POST /api/locks/release-malformed`
  `{repo_key, confirm_repo_key, reason, display_name?}`.

Behaviors (spec §9; each a harness case):
1. **Two-step launch:** clicking a Ready row expands
   ``Launch `<work_id>` @ `<sha7>`? [Confirm]``; Confirm POSTs the v2
   body; `request_id` is generated ONCE per attempt (`pending[rowKey]`)
   and reused on Retry; a settled definite answer (2xx receipt or any
   structured error body) clears the pending entry.
2. **Transport uncertainty:** a rejected fetch / unreadable body sets
   `pending[rowKey].status = "unknown"` — the row shows *launch outcome
   unknown*, offers Retry (SAME `request_id`) and a one-shot read-back
   (`GET /api/runs/{request_id}`); a read-back 404 KEEPS the unknown state
   (message: the record may not be visible yet or the request never
   arrived — Retry is safe either way, idempotency absorbs it); ONLY a
   definite HTTP answer (read-back 200, or a retry's settled response)
   resolves the state. No auto-polling, no auto-clear: resolution is
   operator-driven. A pending row whose Ready row vanished from the next
   snapshot renders in a `#lp-pending` list (state lives in `lpState`, not
   the DOM — the wholesale rerender cannot lose it).
3. **Structured errors:** any `{code, detail}` body renders as a text
   message (`code: detail`), then ONE whole-snapshot refetch; `current` is
   shown inside the message text only, never spliced into the snapshot.
4. **Re-validation of open confirmations after a refetch:** if the row
   left Ready, gained a blocker, or changed `seen_revision`, typed state
   is preserved but Confirm is disabled with the cause shown.
5. **Escape forms:** acknowledge-vanished (retyped `confirm_run_id` —
   never prefilled — plus required `reason`) and release-malformed
   (retyped `confirm_repo_key`); success → whole refetch; error → text
   message per rule 3.
6. **Manual (advanced) form:** the reworked `#run-console` section takes
   `repo_key`, `work_id`, optional `request_id`; silently attaches the
   current snapshot's `snapshot_id` and `seen_revision` once the repo is
   chosen; generates `request_id` once per attempt; goes through the SAME
   v2 submit. The legacy field-typing inputs (revision/tasks/spec/plan
   refs) are REMOVED. The run-view opener (`request_id` → Open run) stays.

- [ ] **Step 1: Extend the harness — the four named §10 scenarios first**
  (lost response → unknown → Retry same id → read-back finds the record;
  the stale-snapshot sequence guard [Task 6 — keep]; a Ready row vanishing
  under an open confirmation → Confirm disabled with cause, typed reason
  preserved; repeat submit with the same `request_id` hits the wire with
  an IDENTICAL body), then cases 1–6.
- [ ] **Step 2: RED → implement → GREEN** —
  `uv run pytest tests/test_launchpad_js.py tests/test_run_console_js.py -v`
  (`run_console_harness.js` is UPDATED here: assert the v2 wire shape;
  keep the three-valued `accepted` render assertions — they still hold for
  the launch phase; keep the transport-uncertainty request_id-reuse
  assertions — they now live against the v2 flow).
- [ ] **Step 3: Hygiene + commit**

```bash
git add dispatcher/server/static/index.html tests/web/launchpad_harness.js \
        tests/web/run_console_harness.js tests/test_launchpad_js.py
git commit -m "feat(ui): launch flow — two-step confirm, transport uncertainty, escapes, manual form"
```

---

### Task 8: Docs, TODO, full-suite gate

**Files:**
- Modify: `TODO.md`
- Test: full suite

- [ ] **Step 1: TODO.md Launchpad section** — close `@id:launchpad-b2`
  with "(PR #204)"; reword `@id:launchpad-c` to "in flight — this PR";
  strike the inherited-tail lines this plan discharges (spec touch-ups
  §5.1/§6.1/§7/§4.2/§4.1 — Task 1; layering M5 — Task 1; message-split of
  submit capture-phase refusals — superseded by the v2 taxonomy). Add two
  new checkboxes:
  - `@id:launchpad-perf-capture` — capture spawns ≤3 git subprocesses per
    dags/ file per submit AND per repo per assembly (B2 M8); optimize when
    measured to hurt, with the assembler's once-per-assembly global reads
    already in place.
  - `@id:launchpad-live-acceptance` — §10's slice acceptance: one live run
    of a real backlog item through the panel, recording work_id, full
    seen_revision, exactly one request_id, the runtime-created run_branch,
    the default branch unmoved, and the terminal outcome in Recent
    completed. Owner-driven, after merge.
- [ ] **Step 2: Full suite + hygiene** — `uv run pytest`; green except the
  standing 3 live-smoke baseline failures and possibly the two recorded
  flakes. Anything else red belongs to this branch.
- [ ] **Step 3: Commit**

```bash
git add TODO.md
git commit -m "docs: launchpad TODO refresh — B2 closed, C in flight, acceptance item"
```

---

## What this plan deliberately does NOT do

- **No authentication** — `actor` stays `local-unauthenticated` (§12.3).
- **No maestro-side changes**: Mode-1 liveness stays unprovable (§12.1);
  the DAG-content TOCTOU window stays narrowed-not-closed (§12.2).
- **No capture-cost optimization beyond once-per-assembly global reads**
  (B2 M8 — recorded as `@id:launchpad-perf-capture`).
- **No visibility-API coverage in the Node harness** — a named comment
  (spec §9/§12.6), not a stub; likewise no AbortController — §9's
  "abort the previous" is implemented as supersede-and-discard (higher
  seq wins, the stale response is dropped by the sequence guard), by
  design, not as a shim.
- **The §10 live slice acceptance** is owner-driven after merge
  (`@id:launchpad-live-acceptance`), not a task here.
