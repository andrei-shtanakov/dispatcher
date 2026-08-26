# Launchpad — slice 1 of the Dark Factory control plane

**Status:** draft for review (owner)
**Date:** 2026-08-26
**Prior art:** slice 0 (`2026-08-22-dark-factory-control-plane-slice0-design.md`) —
the launch/receipt/verb/resolution contour this slice sits on top of.

## 1. Problem

Slice 0's console works and was accepted, but its form is an API with input
boxes: the operator copies a 40-hex sha out of a terminal, remembers which
YAML file is the DAG, and retypes a backlog slug. Every one of those values
is machine-derivable. The owner's verdict on first contact: *"при таком
интерфейсе его точно никто использовать не будет"* — and the interface is
the reason slice 1 exists.

Slice 1 replaces the form with an inventory: what is ready to launch, what
is running or needs a human, what recently finished — and a launch that is
one confirmed click, with every field recovered by the server from canon.

**One safety gap doubles the motivation.** The durable `RepoKey` lock is
released at `materialized` (`run_store.py:275-279`) — it protects the launch
window and unresolved ambiguity, not the run's lifetime. Today nothing stops
a second submit while a Mode-1 run is alive in the same checkout; the pilots
never hit this only because launches were manual and singular. A Ready list
with buttons turns that hole into one click. Slice 1 therefore ships a
single-live-run gate as a precondition of shipping Ready buttons at all
(§7): it makes spec §5.4.1's "working agreement" a mechanism.

## 2. Scope and non-goals

**In scope:** the `@dag` plan-fields tag (canon first, then vendored); an
inventory/classification layer; `GET /api/launchpad`; submit v2 with
re-validation under the lock; the single-live-run gate; an audited escape
for vanished runs; the launchpad UI.

**Non-goals, named:**

- **No DAG generation.** Authoring stays manual (slice 0 §2.2 unchanged).
- **No push / PR from the contour.**
- **No reliable Mode-1 liveness.** `interrupted` cannot distinguish a
  working process from an abandoned one (the holder file is written only by
  the Mode-2 service tick). The gate blocks both until terminal evidence or
  explicit operator resolution — the price of safe uncertainty, stated, not
  masked.
- **No multi-checkout support.** One checkout per `RepoKey`, as in slice 0.
- **No logs for unlinked runs.** `/logs` is keyed by `request_id`; a
  by-`run_id` endpoint would widen L1. Unlinked runs render id and status
  only.

## 3. Source of truth: the `@dag` plan-fields tag (L0)

Ready inventory derives from the operational plan, not from scanning for
YAML. An item is launchable because the plan says so:

```
- [ ] Exclude `.envrc` from the build context @owner:... @id:envrc-context-ignore @dag:dags/envrc-context-ignore.yaml
```

`@dag` is deliberately explicit even though the path is derivable from
`@id`: it asserts "an executable DAG has been prepared for this item". A
stray `dags/<id>.yaml` appearing on disk must not make an item Ready.

### 3.1 Contract change (canonical first)

The plan-fields contract v3 is owned by the KB (prograph-vault; consumers
vendor a pinned copy). `@dag` is added there first, as an additive minor
revision, through the vault's own PR process. **dispatcher's shipping code
does not recognize a provisional `@dag` before the revised contract is
merged and re-vendored** — otherwise C-as-source-of-truth degrades into one
consumer's private convention.

Contract content:

- Tag `@dag:<value>`, on the checkbox line (the line-based parser reads no
  continuations — a fixture pins that a continuation-line tag is invisible).
- Value grammar: `^dags/[a-z0-9][a-z0-9._-]*\.yaml$` — relative,
  normalized, no `..`, no quotes, no spaces. Traversal dies in the grammar,
  before any filesystem.
- Co-occurrence rule: `@dag` is valid only on a line that also carries
  `@id`, and its value MUST equal `dags/<id>.yaml`. Both live on one line,
  so this is checkable line-locally. A shared fixture pins that the `@id`
  grammar and the permitted filename stem agree — the equality is only
  well-defined while they do.
- Ready *semantics* do not belong to the contract. The contract says what
  the tag is; dispatcher decides what follows.

Fixtures: valid; `@dag` without `@id`; name ≠ id; traversal; quoted value;
continuation line; the shared `@id`-stem grammar fixture.

**Fleet evidence (required for L0):** a one-off scan of every fleet
`TODO.md` for pre-existing `@dag:` strings that the new grammar would
reject. For the parser a new optional tag is additive; for a validator,
previously ignored text can become an error. The scan result ships in the
vault PR's evidence. The scan is dev-tooling, not a runtime or CI
dependency.

### 3.2 Binding and its honest limit

- `@dag` proves a **registered link** plan item → artifact.
- Filename equality plus ledger-wide uniqueness protect identity and
  provenance (a new item cannot adopt an old DAG's history: duplication is
  checked across the whole ledger **including `## Shipped`**).
- The DAG's own `repo:` protects the target repository (§6, checked as
  resolved `RepoKey` identity, never as a directory-name string).
- Whether the task texts actually implement the backlog item **cannot be
  proven automatically** and is not masked with metadata: it remains the
  responsibility of the DAG's author and review.

A future maestro-owned `work_id` field only becomes non-parasitic if
maestro itself starts consuming it (ledger, idempotency); until then it is
out (question 2's resolution: A + C, B rejected).

## 4. Data surfaces

### 4.1 `GET /api/launchpad`

One endpoint returns everything the panel shows. Its guarantee, stated
precisely: **not a point-in-time snapshot.** Every source is read once per
assembly, and every category is derived from that one set of captured
inputs by one classification function (§5). The result is internally
consistent and may be stale the moment it is returned — which is what
submit-time revalidation exists for.

```jsonc
{
  "snapshot_id": "…",          // opaque, unique per assembly; NOT a concurrency token
  "generated_at": "…",
  "repositories": [{
      "repo_key": "github.com/andrei-shtanakov/deployer",   // canonical id
      "repository": "deployer",                             // display label only
      "default_branch": "master",
      "seen_revision": "<full 40-hex>",                     // short forms are display-only
      "admission": "ready | blocked",
      "blockers": [                                          // list — states coexist
        {"code": "launch_busy",      "request_id": "rc-…"},
        {"code": "run_in_flight",    "request_id": "rc-…"},  // linked
        {"code": "run_in_flight",    "run_id": "01…"},       // unlinked
        {"code": "run_vanished",     "request_id": "rc-…"},
        {"code": "state_unreadable", "detail": "…"}
      ]
  }],
  "ready":              [{ "repo_key": "…", "work_id": "…", "dag_path": "…", "seen_revision": "…" }],
  "blocked":            [{ "repo_key": "…", "work_id": "…", "dag_path": "…", "reason_code": "…", "reason": "…" }],
  "unregistered_items": [{ "repo_key": "…", "work_id": "…", "reason_code": "no_dag_tag" }],
  "orphan_dags":        [{ "repo_key": "…", "dag_path": "…" }],   // diagnostics, no actions
  "active":             [{ "request_id": "rc-…|null", "repo_key": "…", "work_id": "…|null",
                           "state": "…", "run_id": "…|null", "run_status": "…|null",
                           "attention": true, "updated_at": "…" }],
  "recent_completed":   [{ "request_id": "…", "repo_key": "…", "work_id": "…", "run_id": "…",
                           "revision": "…", "outcome": "…", "updated_at": "…",
                           "logs_available": true }],
  "completed_total": 41,
  "next_cursor": "…"           // opaque; order (updated_at DESC, request_id DESC)
}
```

- `active` includes **all** non-terminal controller requests, their runs,
  and **unlinked maestro runs of the same repository** (`request_id:
  null`) — they block admission, so they must be visible. `attention`
  (`launch_unknown`, `NEEDS_REVIEW`, `AWAITING_APPROVAL`) sorts first but
  is a sort key, not a filter. `active` is capped at **200 entries**
  (attention-first, then newest); if the cap is ever hit the response says
  so (`active_truncated: true`) — the endpoint must never become unbounded,
  and hitting this cap is itself a signal the fleet needs cleanup.
- `recent_completed` is a bounded tail; `logs_available` is computed
  server-side (the run's log directory demonstrably present) — a bare
  `run_id` is not enough to promise a link (#191's lesson).

### 4.2 `POST /api/runs/submit` v2

Body: `{ snapshot_id, repo_key, work_id, request_id, seen_revision }`.

The server recovers the DAG path and every other fact from canon; the
client is not trusted with them. `snapshot_id` is an audit echo — authority
is re-verification of facts under the lock, never snapshot age.
`request_id` remains the idempotency key of one operator attempt and is
resent unchanged after transport uncertainty.

The legacy body (`revision`/`tasks` fields) gets 400 with a pointer to the
new form; the API's only consumers are this console and the tests.

**Response classes, disjoint:**

| Class | Meaning |
|---|---|
| 400/422 | schema-invalid body — a client defect |
| operational error (structured) | deployment drift, not a client bug and not admission. **409** `repo_unresolved` — the checkout is missing/moved (transient workspace drift, retry after fixing the workspace); **422** `identity_mismatch` — the checkout at the expected path resolves to a different `RepoKey` (unresolvable from a well-formed request). Both `{code, detail}` |
| 409 admission | a well-formed request that lost re-validation: `{code, detail, current}` |
| `LaunchReceipt` | an admitted launch; its `true/false/null` speak about the launch phase, exactly as in slice 0 |

409 codes: `revision_moved`, `lock_busy`, `run_in_flight`,
`state_unreadable`, `item_closed`, `item_unregistered`, `dag_invalid`,
`dag_duplicate`, `dag_dirty`, `request_id_conflict`.

`current` in a 409 is **for the operator's message only**. The UI must not
splice it into the displayed snapshot — that would recreate
mixed-generation state; it refetches `/api/launchpad` whole (§9).

## 5. One classifier, two adapters

`dispatcher/core/admission.py` — pure functions, no IO:

```python
@dataclass(frozen=True)
class CapturedInputs:
    plan_items: tuple[PlanItem, ...]        # full ledger, Shipped included
    dag_files: tuple[DagFileInfo, ...]      # listing + lstat + HEAD-blob comparison
    repo_key: RepoKey                       # from the checkout's origin remote
    head_revision: str                      # full 40-hex
    lock_holder: LockInfo | Unreadable | None   # parsed, unparseable, or absent
    launch_records: tuple[LaunchRecord, ...]  # RunStore listing — links run_ids to this repo
    runs: tuple[RunInfo, ...]               # maestro rows, or Unreadable
```

`classify_item(item, inputs)` and `classify_repo(inputs)` consume **only**
captured values — the classifier never touches a store or a disk. Both
adapters call these same functions:

- the snapshot assembler, on the inputs it captured once per assembly;
- `admit_submit`, on inputs captured fresh under the lock.

**The property test targets the adapters, not the classifier.**
`assemble_snapshot(inputs).decision == admit_submit(inputs).decision` with
instrumented sources proving both adapters handed the classifier
equivalently normalized inputs. Calling `classify_*` twice would be a
tautology; the adapter-level test is what catches logic growing before or
after the shared core.

### 5.1 Readiness conditions (the full list)

An item is Ready iff, on the captured inputs:

1. the plan item is open (`- [ ]`);
2. it carries `@id` and `@dag`, grammar-valid, `@dag == dags/<id>.yaml`;
3. no other ledger line — Shipped included — names the same DAG
   (`dag_duplicate` otherwise, on both items);
4. the file exists, is a regular file, not a symlink (`lstat`);
5. it parses as a **supported DAG subset** (§6.1);
6. the DAG's `repo:` resolves to the same `RepoKey` as the item's
   repository — resolved identity via the checkout's remote, never a
   string comparison of directory names;
7. the on-disk file equals the blob at `head_revision`
   (`dag_dirty` otherwise) — this pins launched content to
   `seen_revision` (§6.2);
8. `classify_repo` says `ready` — no blockers (§7).

Failures classify into `blocked` with the specific `reason_code`; an open
item with `@id` but no `@dag` goes to `unregistered_items`; a
grammar-valid DAG file referenced by no open item goes to `orphan_dags`
(diagnostics only).

## 6. The DAG file: subset check and content pinning

### 6.1 Supported DAG subset

dispatcher does not vendor maestro's schema. The structural predicate:
parses as YAML; top-level `repo:` string and `tasks:` list present;
`workstreams:` and `repo_url:` absent (those two are Mode-2 markers —
`OrchestratorConfig` requires them, `ProjectConfig` lacks them). This is
deliberately named a **supported subset**, not "Mode-1 validation":
authoritative validation stays with maestro at launch. Fleet-derived
fixtures — a minimal Mode-2 config modeled on `proctor-a-*.yaml` and
minimal Mode-1 configs modeled on the pilots, each carrying a provenance
comment — pin the discriminator. **CI reads local fixtures only, never
sibling repositories' real files.**

### 6.2 TOCTOU, narrowed and named

Condition 7 pins the launched content to the revision the operator saw.
The residual window — between dispatcher's comparison and maestro's own
read of the file — is serialized against other dispatcher submits by the
lock but **not against an external process or a human editing the file**.
Closing it fully would require maestro-side changes; the window is named
here as a limit, not hidden.

## 7. Single-live-run gate and repo blockers

**Contract:** *at most one run without proven terminal outcome per
`RepoKey`.* Checked against **all** known runs of the repository, not "the
latest" — history can already hold several non-terminal rows.

Fail-closed classification:

- proven terminal → does not block;
- `running`, `suspended`, `interrupted` → blocks (`run_in_flight`);
- run state absent or unreadable → blocks as **unknown**
  (`state_unreadable`), never treated as finished;
- `launch_unknown` keeps blocking through the existing durable lock
  (`launch_busy`), unchanged from slice 0.

`run_vanished` vs `state_unreadable` are distinct on purpose: the precise
predicate for vanished is — a launch record is non-terminal, carries a
`run_id`, the expected run root is computed from the record's **stored**
`RepoKey`, and that directory is absent. A stat error, permission denial,
or corrupt record is only ever `state_unreadable`: the escape hatch (§8)
must not be offered where fact-gathering is broken.

The UI folds both sources into the repository's `blockers[]`; one
`launch_unknown` visually freezes the whole repository, and that is
correct pressure — hiding the blast radius on one row would lie about the
unit of concurrency.

## 8. Store changes and the audited escapes

### 8.1 Lock = minimal preflight record

The lock file itself **is** the preflight record: created
`O_CREAT|O_EXCL`, with `{request_id, fingerprint, created_at}` written to
the same fd immediately after creation. The full `LaunchRecord` then
extends the fact. An admission rejection terminalizes the record with
`outcome="admission-rejected"` and releases the lock — "a lock with no
owning fact" is not a representable steady state.

**Honest residue:** POSIX cannot create-with-content atomically; a crash
between `O_EXCL` and the write leaves an empty lock file. That reads as
`state_unreadable` (blocking, named), and the audited escape for it is
§8.3 — fail-closed never lacks a door.

**The recovery critical section.** Checking a lock file and later acting
on its *pathname* is not compare-and-swap: between "A saw it unreadable"
and "A renamed it", B may have quarantined it and a fresh submit may have
created a healthy lock at the same path — A would then quarantine the
healthy one. Every mutation of a `RepoKey`'s lock path therefore runs
inside a per-`RepoKey` **guard file** held with an OS advisory lock
(`fcntl`/`flock`): `reserve`'s acquire-or-refuse, admission release,
transition releases, and `release-unreadable` all take the guard first.
A crash releases an advisory lock automatically, so the guard adds no new
orphan-state of its own. Under the guard, `release-unreadable` re-checks
the lock's content *and identity* (inode/stat) before quarantining.

### 8.2 Idempotency fingerprint

`reserve` stores `(repo_key, work_id, seen_revision)` as the attempt
fingerprint. A repeated `request_id` with a matching fingerprint returns
the prior result (before taking the lock, as today); a mismatch is 409
`request_id_conflict` — a reused id must not adopt another attempt's
receipt.

"The prior result" must be reproducible without re-running admission —
the workspace has moved on, and a re-classification would violate the
idempotency contract (and could even *pass* where the original failed).
An admission-rejected record therefore persists an immutable response
payload alongside the outcome:

```
response_class = "admission_rejected"
admission_code, detail, current, rejected_at
```

A repeat with a matching fingerprint replays exactly that 409; an
admitted attempt replays its `LaunchReceipt`, as slice 0 already does.

### 8.3 `acknowledge-vanished` (and the unreadable-lock sibling)

`POST /api/runs/{request_id}/acknowledge-vanished`, body
`{confirm_run_id, reason, display_name?}`:

- an administrative operation: it **first acquires the `RepoKey` lock**
  (busy → 409 `lock_busy`); the directory-absence check runs after
  acquisition, against §7's precise predicate;
- `confirm_run_id` must equal the recorded `run_id` — retyped, never
  prefilled (the retyping is the guard against a blind click);
- `actor` is **assigned by the server**: the authenticated principal if
  auth ever exists, else a configured operator, else the honest literal
  `local-unauthenticated`. `display_name` from the body is stored only as
  `self_reported`. A client-supplied actor is not an audit fact;
- `reason` is length-capped and newline-normalized — the durable ledger is
  not an unbounded store;
- effect: the record → `terminal`, `outcome="vanished-acknowledged"`, plus
  `{actor, at, prior_run_id, reason}`; written by atomic file replacement
  like every store transition. A tombstone: nothing leaves the ledger;
  `recent_completed` shows the outcome;
- existing records without the new fields must load unchanged (explicit
  migration/back-compat note in the implementation plan).

**The limit, verbatim:** *отсутствие каталога не доказывает отсутствие
процесса. Это административное снятие fail-closed блокировки с
зафиксированным риском.*

An analogous minimal operation covers the unreadable/empty **lock file**
(§8.1 residue): `POST /api/locks/release-unreadable` with
`{repo_key, confirm_repo_key, reason, display_name?}`. The lock *path* is
computed by the server from the verified `repo_key` — a client-supplied
filename would hand an administrative endpoint a client-controlled file
identifier. `confirm_repo_key` must equal `repo_key`, retyped (the same
blind-click guard as `confirm_run_id`). The operation runs inside §8.1's
guard section, re-verifies unreadability and file identity (inode/stat)
under it, refuses if the lock parses (a healthy lock is released only by
its owning transitions), else atomically moves the file into
`locks/released/`. The audit record stores the original bytes' hash, the
inode/stat metadata, and the final quarantined filename — enough to
reconstruct what was removed and prove it was the observed file. Without it the fail-closed gate could freeze
a repository with no doorway.

## 9. UI

**Framing: launchpad becomes the root panel; the existing run view stays
as drill-down.** Verbs, resolution and logs are untouched.

- **Repository headers** carry `admission` + `blockers[]`. Blocker targets
  are **typed**: `launch_busy`/`run_in_flight` with `request_id` → run
  view; unlinked `run_in_flight` with `run_id` → identifier and status
  only (no logs link — §2); `run_vanished` → the acknowledge form;
  `state_unreadable` → diagnostic text, no action.
- **Launch is two steps in the row:** click expands
  "Launch `<work_id>` @ `<sha7>`? [Confirm]"; Confirm sends submit v2.
- **Transport uncertainty is a first-class state** (the B2 lesson, now
  per-row): a failed fetch is *not* a red error. The row keeps its
  `request_id` and fingerprint, shows *launch outcome unknown*, offers
  Retry with the **same** `request_id`, attempts a read-back
  (`GET /api/runs/{request_id}`), and blocks a fresh Launch for that row
  until the attempt resolves. A full refetch alone cannot resolve it: the
  response may have been lost before the record became visible to a
  snapshot. `renderReceipt` never clears a pending attempt before a
  definite HTTP answer.
- **Staleness guard is a client request-sequence**, not
  snapshot-generation: `snapshot_id` is opaque and unordered. Each fetch
  increments a sequence; a response applies only if its sequence is not
  older than the last applied. One snapshot fetch in flight at a time
  (or abort the previous).
- **Refetch discipline:** 409 → message from `code`+`current` as text
  only, then a whole-snapshot refetch; success → refetch; no splicing.
  After a refetch, a row's expanded confirmation is **re-validated**: if
  the row left Ready, gained a blocker, or changed `seen_revision`, typed
  state (reason etc.) is preserved but Confirm is disabled with the
  cause shown — otherwise the form preserves exactly the stale permission
  to act.
- **Refresh cadence:** 30 s auto-refresh; the timer pauses while the tab
  is hidden and fires immediately on return. (The harness DOM cannot
  model the visibility API; that gap is a named comment in the harness,
  not a stub pretending to cover it.)
- **Manual (advanced):** a collapsed form that lets an operator type
  `repo_key`, `work_id`, and optionally a `request_id` for diagnostics —
  it silently attaches the current snapshot's `snapshot_id` and
  `seen_revision` after the repo is chosen, generates `request_id` once
  and keeps it until the attempt settles, and goes through the **same**
  admission. Typing snapshot fields by hand would recreate the old
  API-console; authority and admission have exactly one door.

## 10. Testing strategy

All fixes RED-verified individually, as this codebase practices.

- **Contract (L0):** the fixture set of §3.1; the fleet scan as one-off
  evidence.
- **Admission units:** one failing input per readiness condition;
  combined `blockers[]` (busy *and* in-flight together); vanished vs
  unreadable — stat errors and corrupt records must classify unreadable;
  the subset discriminator against local provenance-commented fixtures
  (Mode-2-shaped rejected, pilot-shaped accepted). Permission-denied is
  modeled by injected reader/stat errors, never by chmod (unstable across
  users and CI).
- **Property (adapter-level):** §5's instrumented equality.
- **Store:** lock-is-preflight atomicity and the empty-lock crash residue
  (empty lock → `state_unreadable`, escape works); fingerprint match →
  prior receipt, mismatch → `request_id_conflict`; `admission-rejected`
  terminalization; tombstone atomic replace; legacy-record migration;
  **replayed 409 is byte-stable** — repeat after the workspace changed
  returns the persisted payload, provably without re-classification
  (instrumented classifier asserts zero calls); **the guard section
  closes the quarantine race** — with the guard held by a stalled
  release-unreadable, a concurrent reserve waits/refuses rather than
  interleaving, and the healthy-lock-quarantined interleaving of the
  review scenario is reproduced against the UNGUARDED implementation as
  its RED.
- **Submit integration:** each 409 code with `current`; the two-process
  race (second `acquire` → `lock_busy`); `dag_dirty`; fail-closed on an
  unreadable `state.db`.
- **Acknowledge:** precise predicate; confirm mismatch; busy lock; audit
  fields; reason cap/normalization; server-assigned actor.
- **Web harness (the four named scenarios first):** lost response →
  unknown → Retry same `request_id` → read-back finds the record; a stale
  snapshot response vs the sequence guard (two ticks, not one); a Ready
  row vanishing under an open confirmation → Confirm disabled with cause,
  typed reason preserved; repeat submit with the same `request_id`. Plus:
  typed blocker targets; delegated handlers; `logs_available=false` rows
  render no link.
- **Slice acceptance — external traces, not just a green run:** one live
  run of a real backlog item through the launchpad, recording the chosen
  `work_id`, the full `seen_revision`, exactly one `request_id`, the
  runtime-created `run_branch`, the default branch unmoved, and the
  terminal outcome appearing in Recent completed.

## 11. Delivery

| PR | Content | Depends on |
|---|---|---|
| **A** (vault) | `@dag` contract revision + fixtures + fleet evidence | — |
| **B1** (dispatcher) | store rework (lock-as-preflight, fingerprint, tombstone, migration), admission module on synthetic inputs, `RunStore.list`, single-live-run gate, both audited escapes | — (parallel with A) |
| **B2** (dispatcher) | parser version + re-vendor + inventory reader (Ready / blocked / unregistered_items / orphan_dags) | A |
| **C** (dispatcher) | `/api/launchpad` + UI | B1, B2 |

Each PR runs through its own plan (writing-plans → SDD) with per-task and
whole-branch review. B1 must not recognize `@dag` in shipping code — the
canon-first rule of §3.1 holds at every stage.

## 12. Named limits (the honest list)

1. Mode-1 liveness is unprovable today; the gate blocks working and
   abandoned runs alike until terminal evidence or operator resolution.
2. The DAG-content TOCTOU window between dispatcher's check and maestro's
   read (§6.2) — narrowed by `dag_dirty`, not closed.
3. `actor` is `local-unauthenticated` until authentication exists.
4. Unlinked runs render without log links.
5. The empty-lock crash residue of §8.1, with its audited escape.
6. The visibility-driven refresh is not covered by the current Node
   harness; it is testable in principle (injected `visibilityState` plus a
   clock adapter) and is a harness gap, not a technical limit.
7. Task-text honesty (§3.2) is the DAG author's and reviewer's, not the
   machine's.
