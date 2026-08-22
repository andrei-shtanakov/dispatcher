# Dark Factory control plane, slice 0 — dispatcher-side design

**Status:** 2026-08-22, corrected after implementation. The slice-0 server half shipped
in #167/#168/#169; five things this spec asserted turned out to be wrong or unbuilt, and
each is corrected in place below rather than left for a reader to trip over. Every
correction is marked **Correction, 2026-08-22** and says what was believed before.

The corrections, in descending order of consequence: §4.2.1 — a run's repository is
chosen by the `tasks.yaml`, not by the request; §2.1/§9/§10 — the UI was never planned
and pass 1 cannot be accepted without it; §6 — `stop` is not a run-scoped verb; §5.2.1 —
one of three named lock-release conditions had no mechanism; §3.1 — the record's join
was asserted but not implemented.

No inbox issue: this is dispatcher's own work, not a cross-repo request.

Slice 0 is the smallest contour that lets a real backlog item be implemented by an
orchestrated run **started from the dispatcher UI**, without opening a terminal in the
target repository. It is deliberately not a platform: everything that can be deferred
without breaking that sentence is deferred.

**Citations.** Neighbour code is cited workspace-relative and repository-first —
`maestro/maestro/cli.py:1040`, `deployer/src/deployer/verify.py:368` — and is **not a
file of this repository**. Unprefixed paths (`core/actions.py`,
`packages/plan-fields/…`) are dispatcher's own. The distinction is load-bearing here
because each repository has its own `cli.py`, and an unowned path sends the reader
looking for a file that does not exist (the rule disputatio adopted after Copilot
caught exactly this on its PR #33).

## 1. Problem

The ecosystem can already do every step of a work item's life, and cannot do the life.
Authoring (discovery, disputatio), execution (maestro, spec-runner), acceptance
(steward), and observation (dispatcher) each work; carrying one item through them is a
human opening one terminal per repository and remembering the joins.

The bookkeeping makes this worse rather than better. One unit of work exists today as up
to five records — a `TODO.md` item (`todo://<repo>/<id>`), an ADR-ECO-006 inbox issue, a
PR, a gate verdict, and a maestro run — in five identifier spaces, joined only by prose
that a human writes and another human must find. Records are created by rule and retired
by memory, so the open set drifts towards "satisfied but still open". This document
carries no count for that drift on purpose: the number moves daily, and an undated one
inside a design document is a claim no later reader can re-verify.

dispatcher was built to be the place where this is visible. It is not yet the place
where it is *driven*, and its own work competes for attention inside the same flat list
it renders.

## 2. Scope

### 2.1 In slice 0

A durable, idempotent request to start one maestro Mode-1 run against one repository at
one revision, issued from the dispatcher UI, executed by a controller that owns the
process, with run state read back from maestro's own store.

**Correction, 2026-08-22: the UI half was never planned and is not built.** The
implementation (#167, #168, #169) delivers the server half — the request contract, the
store, the controller, the verbs, the HTTP surface and the read-back — and nothing under
`dispatcher/server/static/`. Today a request is issued by fetching the CSRF token from
`/api/actions/session` and posting to `/api/runs/submit` by hand. Narrowing slice 0 to
the server half is defensible; it is the hard half. What was not defensible was leaving
the narrowing unstated while §9's acceptance criterion assumed the UI existed. **Pass 1
cannot be accepted until the UI lands**, and it is owed its own plan.

### 2.2 Non-goals (explicit, each with its reason)

| Not in slice 0 | Why |
|---|---|
| `plan → tasks.yaml` compiler | `maestro run` consumes a task DAG, not prose. Writing the compiler turns a day into a workstream; hand-written DAGs are established practice (`maestro/examples/maestro-builds-maestro.yaml`) |
| Mode 2 (`orchestrate project.yaml`) and the workstream command family | A second FSM. One contract must not promise control over two |
| Unified work-item model and dependency graph | Needs something to project. It arrives in slice 1 **as a view**, never as a registry |
| Import of `TODO.md` / issues into a dispatcher-owned store | Would make dispatcher either a writer of 21 neighbour repositories or the fourth diverging registry. See §3.1 |
| Merge from the UI | ADR-ECO-008 D1 is not enabled; merge stays on its existing path |
| `merge_authority` field | ADR-ECO-008 D5 assigned it to the orchestrator's config and no producer implemented it. A field with no reader is an invitation to fill it with a guess. Slice 0 behaves as `human`, which is what D6 requires when the source is absent |
| Authoring through discovery / disputatio | Slice 3 |

## 3. Layers and ownership

    dispatcher UI  ──── durable RunRequest ───▶  RunController
                                                     │ allowlisted argv
                                                     ▼
                                                  maestro  (owns the run)
                                                     │
                                                     ▼
                                              spec-runner / coding agents

### 3.1 dispatcher derives; it does not own

A work item is a **projection** keyed by `todo://<repo>/<id>` over sources that already
exist: the `TODO.md` item (parsed by the shared `plan_fields` package), the inbox issue
(its body already carries machine-readable `slug:` / `from:` fields), the PR, the
steward verdict, and the maestro run.

dispatcher persists exactly one thing nobody else owns: the `RunRequest` and its
outcome. This is the same constraint `core/governance.py` already lives under (CON-03,
"no sibling-repo path is ever resolved") applied to writes.

For slice 0 the join between the five identities lives **in the RunRequest record** —
concretely, the durable record persists `work_id`, `revision`, `tasks` and both
`spec_ref`/`plan_ref` path-and-commit pairs alongside the state machine, not just
`request_id` and `run_id`. **Correction, 2026-08-22:** the first implementation of this
spec stored only the state machine and dropped the request body, which left this
paragraph's central claim — the reason dispatcher owns a store at all — true of nothing.
Neither the plan's own review nor any per-task review caught it; the whole-branch review
did.
Requiring `work_id` to be carried natively by spec-runner's SpecMeta, steward's
`gate-verdicts/v1`, and maestro's run DB would begin this work with three cross-repo
contract requests — the exact traffic the pilot exists to avoid. Which of the five must
carry it natively is a question for the friction log, answered by evidence after pass 1.

### 3.2 maestro owns the run lifecycle

After the receipt, canonical state is read from
`<maestro-home>/projects/<host>/<owner>/<repo>/runs/<run-id>/`, never mirrored into
dispatcher's own store. dispatcher renders maestro's FSM; it does not restate it.

`<maestro-home>` is **not** the literal `~/.maestro`: maestro resolves it as
`$MAESTRO_HOME` when set and `~/.maestro` otherwise
(`maestro/maestro/state_paths.py:26-29`), and every run path is built from that one
function. Because `RunController` starts maestro as a child process, the environment it
passes decides where the run materializes — so the controller **sets `MAESTRO_HOME`
explicitly in the child environment and resolves its own reads from the same value**,
rather than inheriting whatever the web process happened to hold. A controller that
launches under one root and watches another would see no run appear and report
`launch_unknown` (§5.2.1) for every healthy launch: the pre-launch snapshot of §5.2 and
the materialization watch of §5.3 are only meaningful against the same root the child
used.

This matters beyond tidiness: maestro's `TaskStatus.NEEDS_REVIEW` — the Mode-1 status a
verifier assigns (`maestro/maestro/cli.py:682`), and a different thing from the Mode-2
`WorkstreamStatus.NEEDS_REVIEW` — carries a ratified approval rule. A dispatcher-side
"rework" button that bypassed it would recreate the two-policy-engines defect that
`approval-facts` was designed to prevent.

## 4. `RunRequest` v0

```jsonc
{
  "request_id": "<uuid4>",              // idempotency key, client-generated
  "work_id":    "todo://deployer/entrypoint-token-boundary-match",
  "repository": "deployer",             // manifest key, validated (§4.1)
  "revision":   "<40-hex commit sha>",  // full sha, never a ref
  "tasks":      "path/to/tasks.yaml",   // repo-relative, validated (§4.2)
  "spec_ref":   {"path": "docs/superpowers/specs/....md", "commit": "<sha>"},
  "plan_ref":   {"path": "docs/superpowers/plans/....md", "commit": "<sha>"}
}
```

`run_id` is **not** a request field — see §5.1. `pr_ref` and `verdict_ref` are not
fields either: they come into existence after the run and belong to the outcome record,
not to an immutable request body.

`spec_ref` / `plan_ref` are structural, not `path@sha` strings, and are optional. Their
`commit` defaults to `revision`; a value that differs must be recorded as given rather
than normalized, so "the plan is older than the code" stays visible instead of becoming
three identical fields and one quietly different one.

### 4.1 `repository` validation

Resolved against the workspace manifest and the checkout's remote identity. Never
accepted as a free string, and never used to build a path before it validates.

### 4.2 `tasks` and `revision` validation

`tasks` is repo-relative and validated **inside the checkout of `revision`** by asking
git rather than the filesystem: `git cat-file -e <revision>:<tasks>`. A git object path
cannot contain `..` traversal or follow a symlink out of the repository, so the
resolve-then-assert dance the first draft of this section described is unnecessary.
`revision` is a full 40-hex sha — a ref would make the request non-reproducible on
retry, which defeats idempotency at the source.

#### 4.2.1 `tasks` also *names its own repository*, and it wins

**Correction, 2026-08-22 — this was the largest defect in the first version of this
spec.** §3.2 below reasons carefully about `MAESTRO_HOME` deciding *where* a run
materializes, and never asks what decides *under which key*. It is not the controller's
working directory.

Mode-1 identity comes from the DAG itself. `ProjectConfig.repo` is a **required** field
(`maestro/maestro/models.py:871`), and `identity_from_config`
(`maestro/maestro/repo_identity.py:103-135`) reads `repo_url:` when the config declares
one, otherwise resolves `repo:` as a local checkout and takes *that* checkout's
`origin`. `bootstrap_run` calls it first (`maestro/maestro/run_bootstrap.py:68`).
The child's `cwd` plays no part.

So `request.repository` and the repository maestro actually publishes under are two
independent namings, and if nothing reconciles them:

- the `revision` guard above governs a checkout maestro never touches;
- the per-`RepoKey` lock of §5.4 — "the only thing standing between slice 0 and two
  agent-driven runs mutating one checkout" — is taken on the wrong repository;
- `runs/` is watched in the wrong place, so a healthy launch reads as `launch_unknown`,
  which is exactly the §3.2 failure arriving through a door §3.2 never looked at;
- an agent-driven run edits a checkout nobody requested, while the receipt names the one
  that was.

This is not exotic. A `tasks.yaml` is hand-authored (§2.2 defers the compiler), lives in
one repository, and names its target by absolute path
(`maestro/examples/maestro-builds-maestro.yaml:18`). Copying a DAG between repositories
and forgetting the `repo:` line produces it.

**Therefore:** validation reads the task file at the revision, parses it, and refuses
unless its `repo_url:`, or the `origin` of the checkout its `repo:` names, resolves to
the same `RepoKey` as `request.repository`. An absent, unparseable or unresolvable
`repo:` is refused too — maestro would exit non-zero on it anyway
(`maestro/maestro/cli.py:591-593`, the `IdentityError` handler), and refusing costs
nothing while a failed launch costs the lock.

## 5. Launch lifecycle

### 5.1 maestro allocates `run_id`; the controller does not

An earlier draft had the controller mint a ULID and pass it to `maestro run tasks.yaml
--run <ULID>`. **This does not work.** In `maestro/maestro/run_bootstrap.py:71`, `if
resume or run_id_override is not None:` routes into the resolver of *existing* runs, and
`_run_by_id` raises `NoResumableRun` for an id that is not already present
(`maestro/maestro/run_bootstrap.py:53`). `--run` means "act on this run", never "create
this run". A new id is minted by maestro only on the fresh path, by its own `ulid.new()`
(`maestro/maestro/run_bootstrap.py:103`).

Allocation therefore stays with the lifecycle owner. The controller learns `run_id`
after the fact.

### 5.2 States

    reserved ──▶ launching ──▶ materialized(run_id) ──▶ terminal
                     │               ▲
                     └──▶ launch_unknown
                              │      └── adoption, only when correlation is unambiguous
                              └── otherwise: operator-chosen run_id, ended via `run-end`

- **reserved** — the durable record is written **before** any process starts. It carries
  a pre-launch snapshot of `runs/` for the target `RepoKey` and the wall-clock launch
  window. Without the write there is no crash-safe evidence that a launch was attempted;
  without the snapshot there is nothing to correlate against afterwards.
- **launching** — the maestro process has been started.
- **materialized(run_id)** — the run directory has appeared under `runs/`, and its id is
  recorded against `request_id`.
- **terminal** — maestro recorded an outcome; the outcome record is written.
- **launch_unknown** — the controller died between `launching` and `materialized`, or
  the run never appeared. Never silently equal to "no run": it is a distinct, reported
  state with its own receipt value (§5.3) and its own exits (§5.2.1).

A repeated `request_id` continues or returns the existing record. It never starts a
second process.

#### 5.2.1 Leaving `launch_unknown`

A named state with no exit is the same defect as an `accepted` that also covers "we do
not know", one level up. And the orphan does not clear itself: with no outcome row and
no lock holder, `classify_run` returns `interrupted`
(`maestro/maestro/run_state.py:60-70`) — non-terminal for good. Orphans then accumulate,
and every command that resolves a run without `--run` starts refusing with
`AmbiguousRun` (`maestro/maestro/run_registry.py:190`).

There are two exits. Only the first is automatic, and only under a strict condition.

**Adoption — exactly one candidate, or nothing.** The controller may record an orphan
against its `request_id` when comparing `runs/` against the reservation's pre-launch
snapshot yields **exactly one** new run, matching on `RepoKey` and falling inside the
recorded launch window. Zero candidates and two-or-more candidates both remain
`launch_unknown`. "The run whose timestamp fits best" is never adopted: a heuristic
adoption attributes work to a request that may not have produced it, and every later
control verb would then act on a stranger's run.

**Operator resolution.** Outside that single unambiguous case, the operator names the
exact `run_id` and ends it with `run-end --outcome cancelled|superseded`
(`maestro/maestro/cli.py:1973`), whose docstring describes precisely this residue — the
two endings a run cannot observe about itself. Automatically ending a run that merely
fits the launch window is forbidden for the same reason automatic adoption is: fitting
the window is not evidence of identity.

The durable `RepoKey` lock (§5.4) is **not** released by entering `launch_unknown`. It
is released only on one of:

- a successful adoption;
- a confirmed `run-end` for the named run;
- a launch that is **known** not to have produced a run — see below.

A lock dropped on uncertainty would let the next request launch a second run into the
same tree, which is the failure the lock exists to prevent.

**Correction, 2026-08-22.** The first version of this list ended with "an explicit
operator resolution of the conflict", and that condition had no mechanism anywhere: no
endpoint, no verb, no message telling an operator it was needed. In practice the lock
had to be freed by deleting a file under `run_state_dir/locks/` by hand, which is not a
condition a spec can name and then leave unbuilt.

It is replaced by a case that is decidable rather than delegated. When the child process
has exited **and** its exit code is non-zero **and** a second look at `runs/` still
shows nothing, no run can appear later — the only publisher is dead, and publication
happens in `bootstrap_run` before the scheduler starts. That is knowledge, so §5.3's
`accepted: false` applies, the record goes terminal, and the lock is released with
maestro's own stderr tail as the reason. `null` stays for the genuine timeout, where the
child is still alive and the answer is honestly unknown.

That removes most of what the deleted condition was for. What remains — a lock whose
holder cannot be identified because the lock file itself is torn — is deliberately *not*
self-healing: `release_lock` refuses rather than freeing a lock it cannot prove it owns.
Recovering from that is an operator action on the filesystem, and this spec says so
plainly instead of implying an interface exists.

### 5.3 `accepted` is bound to maestro's own publication point

maestro publishes a run atomically: `run_publish.create_run` builds the directory under
`<project>/.staging/<run_id>`, *outside* `runs/`, and renames it into `runs/` only after
the database is closed. The rename is therefore a real materialization boundary defined
by the producer, not a guess by an observer.

    {"request_id": "...", "run_id": "...", "accepted": true, "reason": null}

`accepted` is **three-valued**, and the values are not interchangeable:

| value | meaning | `run_id` |
|---|---|---|
| `true` | the rename was observed | present |
| `false` | refused before any run could exist — failed validation, `busy` (§5.4), a non-zero exit before publication | `null` |
| `null` | `launch_unknown` (§5.2.1): a run may or may not exist | `null` until adoption |

`false` is a claim, and it may be made only when the controller actually knows that no
run was created. Unknown is `null`.

This is not a new convention; it is the one this codebase already settled on. `merged`
is `False` "only when it actually answered (a parsed gate refusal), `None` on a
transport failure — unknown, not a claimed non-merge, and equally not a claimed merge"
(`core/actions.py:580-584`). A control plane that collapsed `null` into `false` would be
asserting "no run exists" at the exact moment it cannot know that — and the caller's
natural reaction to `false`, retrying, is the one action §5.2.1 forbids.

### 5.4 Invariant: at most one launch in flight per `RepoKey`

**maestro does not enforce this on the path slice 0 uses.** The guard exists — the fresh
path raises `RunIsLive` instead of minting a second id
(`maestro/maestro/run_bootstrap.py:97-102`) — but it fires only for a run that
`classify_run` calls `running`, which requires a `.holder` file naming that run
(`maestro/maestro/run_state.py:66`). The holder is written by `ScopedLock`, and
`ScopedLock` is constructed in exactly one place in the codebase:
`maestro/maestro/service/tick.py:133`, the unattended service path. A plain `maestro run
tasks.yaml` never takes that lock, so it is never `running`, so `RunIsLive` never fires
for it. Two concurrent CLI runs of one repository are permitted today, and they would
work the same tree.

The controller's lock is therefore not a fast local echo of a producer rule. It is the
only thing standing between slice 0 and two agent-driven runs mutating one checkout.
Anyone who reads `RunIsLive` and concludes the lock is redundant would be deleting the
guarantee, not a duplicate of it.

Recovery needs the same invariant: because maestro allocates the id, a controller
returning from `launch_unknown` can only correlate against its pre-launch snapshot
(§5.2.1), and that correlation can be unambiguous only while one launch per repository
is in flight.

The lock is held **durably** — a process-local lock is released by exactly the restart
that creates the problem — and a second request for a busy repository is refused with
`busy` (`accepted: false`, §5.3) rather than queued: a queue lengthens the ambiguity
window instead of closing it. Its release conditions are in §5.2.1.

#### 5.4.1 What the lock does not cover, and the pilot invariant that fills the gap

The lock binds `RunController` and nothing else. It cannot stop a human running
`maestro` in a terminal, a second controller instance, or a scheduled service tick from
starting a run against the same `RepoKey`. Claiming checkout isolation or reliable
attribution from the lock alone would therefore be false, and §5.2.1's correlation rule
would be resting on a premise nothing upholds.

Slice 0 closes the gap by declaration rather than by mechanism, and says which it is:

> **Pilot invariant.** For the duration of the pass, `RunController` is the only
> permitted point of *fresh* launch for the pilot `RepoKey`. A manual `maestro run`
> against it is out of bounds. Reads — `status`, logs — are not.

This is a working agreement, not an enforced property, and §10 lists it as one.
Enforcing it would mean the controller taking maestro's own stage lock: the right answer
eventually, the wrong one now, because it would make slice 0 depend on a cross-repo
agreement about lock ownership — and the pilot exists to earn the evidence for such an
agreement, not to assume it.

## 6. Mode 1 only

`maestro/maestro/cli.py` exposes two modes: `run tasks.yaml` (Mode 1,
`maestro/maestro/cli.py:1040`) and `orchestrate project.yaml` (Mode 2,
`maestro/maestro/cli.py:1911`). The workstream family (`workstreams`,
`workstream-continue`, `workstream-rework`, `workstream-quarantine`, …) serves Mode 2.

Slice 0's allowlist is Mode-1 only:

    submit (maestro run) · status · retry · approve · run-end

**Correction, 2026-08-22: `stop` is not on this list and the first version was wrong to
put it there.** `maestro stop [OPTIONS]` takes no `--run` and no positional argument —
its help reads "Stop the running scheduler. Sends a termination signal to the scheduler
process" (`maestro/maestro/cli.py:1194`). It is not addressed to a run at all. Offering
it as an action on one request's run would put a control in the UI that ends every other
run the same scheduler is managing, so fixing its argv would have been worse than the
bug. Verified by running `maestro stop --help`, not by reading the source — the
first version of this section was derived by reading, and was wrong about two verbs.

The other correction from the same check: `approve` **and** `retry` each take a required
positional task id plus `--run` (`maestro/maestro/cli.py:1207`, `:1154`); `run-end`
takes a positional run id plus `--outcome` and no `--run` (`:1973`); `status` takes
`--run` alone (`:1117`).

Two of these are easy to mistake for one another. `approve`
(`maestro/maestro/cli.py:1207`) releases a task sitting in `AWAITING_APPROVAL` because
it declares `requires_approval`; a task in `TaskStatus.NEEDS_REVIEW` is cleared by
`retry` instead, whose retryable set is `{FAILED, NEEDS_REVIEW}`
(`maestro/maestro/cli.py:940`). A UI control labelled "approve" wired to a review
outcome would be driving the wrong verb at the wrong status.

`run-end` sits in the source beside the Mode-2 commands but is addressed by `run_id` and
belongs to neither mode exclusively (`maestro/maestro/cli.py:1973`). §5.2.1 depends on
it.

Workstream verbs stay outside the slice. Admitting them would give one request type
control over two different state machines, which is the same defect as §3.2 in a
different costume. A Mode-2 request type may be added later as its own contract.

## 7. Executors

### 7.1 Long work: `RunController` (new)

An orchestrated run is long-lived, must survive the web request and a dispatcher
restart, carries durable idempotency, and has its own state and controls.

`ActionRunner` cannot host it and should not be stretched to: it is synchronous by
construction — `subprocess.run(argv, capture_output=True, timeout=_ACTION_TIMEOUT)`
(`core/actions.py:498`) with `_ACTION_TIMEOUT = 120` (`core/actions.py:51`) and a
process-local busy set guarded by `threading.Lock()` (`core/actions.py:351`). Adding
`submit` to its allowlist would produce a web request holding a process open until it
times out, not a control plane.

The web process sends a short command to the controller and receives a receipt. The
controller owns the maestro process.

### 7.2 Short forge actions: existing `ActionRunner`

Opening the PR is a short, synchronous, forge-side action — exactly what `ActionRunner`
is already good at, including its refusal discipline (pre-fork argv checks,
control-character rejection, `--if-head` guarding on merge, `core/actions.py:586`).

The division is: **long work → RunController; short forge actions → ActionRunner.** One
new long-running mechanism, not two.

### 7.3 The steward verdict has its own producer path, not an `ActionRunner` verb

Collecting the verdict does **not** join that list, and the reason is structural rather
than a question of duration. `core/governance.py` lives under ARCH-C3 / FR-02:
"classification only. Verdicts are never computed here, steward is never imported and
its CLI is never executed" (`core/governance.py:10-12`). A `steward gate-check` verb
inside `ActionRunner` would make the dispatcher process the producer of the very
evidence dispatcher renders — the observer holding the key to the observed action, in
the one place this codebase has already ruled it out.

The producer path stays separate and unchanged: steward's own `gate-check
--emit-verdicts` writes `<repo>/.steward/gate_verdicts.jsonl` (ARCH-D1), driven by the
repository's own gate machinery. dispatcher reads it through `collect_governance`
(`core/governance.py:233`) against the vendored schema copy, fail-closed under NFR-02.
Slice 0 adds no new writer of that file and no new caller of steward.

### 7.4 Credential boundary

The `RunController` holds the credentials the coding agents need. The dispatcher web
process does not. This is the already-ratified principle — an observer that holds the
key to the observed action stops being independent — applied to launching rather than
merging.

## 8. Closing the loop

Completion of a task DAG does not by itself produce a branch, a PR, or a verdict. Slice
0 states each explicitly:

1. **Branch and commit** — the pilot `tasks.yaml` must carry the work to a branch and a
   commit. A DAG that ends with a dirty tree has produced nothing the contour can carry.
2. **PR** — opened by the contour as a short action (§7.2), not by hand.
3. **Verdict** — produced by the repository's own steward gate run and *read* by
   dispatcher, never computed by it (§7.3).
4. **`[x]` in `TODO.md`** — written by the **agent inside the run**, in two phases
   (§8.1).

### 8.1 Two-phase closure of the `TODO.md` item

The tidy version — "the completion mark rides in the same commit as the fix" — cannot be
written as stated: the PR number does not exist when that commit is made. A mark that
omits the number is exactly the memory-retired record §1 complains about, and a mark
that invents one is worse.

So the DAG closes the item in two phases, both from inside the run, both in the
repository that owns the file:

1. **With the fix commit** — the item is annotated, not ticked. It stays `- [ ]` and
   gains the branch carrying the work. Nothing claims completion while nothing is
   merged.
2. **After the PR exists** — a follow-up commit on the same branch flips the box and
   records the number: `- [x] … (#<pr>)`. The number is knowable only at this point, and
   the commit travels inside the same PR, so the closing mark is reviewed together with
   the change it closes and becomes true at merge, when the branch's content becomes the
   repository's.

dispatcher must not write a neighbour repository (§3.1); a repository editing its own
plan file is legitimate. Phase 2 also leaves the pass something checkable: an `[x]`
carrying no PR number is a mark written by memory, and the pilot treats it as a finding.

## 9. Acceptance — pass 1

Pilot item: **`todo://deployer/entrypoint-token-boundary-match`** — L1
`entrypoint_in_command` compares substrings, so short names like `app` false-pass.

Chosen because it is a genuine false-pass defect in a named function
(`deployer/src/deployer/verify.py:368`, `if target.entrypoint in haystack:`),
deterministic, and fixable inside one repository with no neighbour half.

Its `@owner:repo:deployer` marks accountability, **not** execution authority.
plan-fields carries no execution-authority axis at all: `@owner` classifies a principal,
and where the contract says "Ownership and movement are orthogonal" it means by
*movement* the triggers and blockers that decide whether an item may proceed — never who
may execute it (`packages/plan-fields/src/plan_fields/contract/README.md:84`). An owner
tag therefore cannot grant execution rights to anyone, and the item is admitted to the
pilot by this spec.

### 9.1 The fix is not a one-liner, and the pass must not be scoped as one

There is **no existing failing test**. The pass has to author the red test itself, and
three facts make the change larger than the TODO line suggests:

- the substring comparison is **documented as deliberate**: "Substring match covers exec
  form, shell form, and `[project.scripts]` names alike; deliberately conservative"
  (`deployer/src/deployer/verify.py:361-362`). The fix changes a stated contract, so the
  docstring is part of the change, not collateral;
- `haystack` is the raw argument text (`["python", "app.py"]`), not parsed tokens, so
  "token boundary" has to be defined against both JSON-exec and shell forms. `\bapp\b`
  still matches inside `app.py`, because `.` is not a word character — the boundary
  model is a decision, not a regex swap;
- adjacent behaviour is pinned: `test_scripts_name_entrypoint_matches` accepts
  entrypoint `serve` against `CMD ["serve"]`
  (`deployer/tests/test_verify_static.py:860`), and the check appears in the golden
  corpus (`deployer/corpus/golden/golden.json:199`), which a behaviour change may
  require regenerating.

Named here because pass 1 measures the contour, not the fix. A pilot whose cost is
underestimated inside its own acceptance criterion measures the wrong thing.

### 9.2 Where the red/green evidence comes from

deployer's CI runs **no tests**: its only workflow is the governance caller
(`deployer/.github/workflows/governance.yml`), a pinned reference to the umbrella's
reusable gate. So the forge produces no red-then-green signal, and the steward verdict
speaks to governance, not to the suite.

The contour must therefore carry that evidence itself: the DAG runs `uv run pytest`
inside the run, before and after the change, and the result is part of what the run
records. Reading the acceptance criterion below as "the PR check shows the test went
green" would be reading a signal deployer does not emit.

> Pass 1 is accepted when `todo://deployer/entrypoint-token-boundary-match` goes plan →
> `tasks.yaml` → `RunRequest` from the UI → maestro run → regression test red before /
> green after → PR → steward verdict → all of it visible in dispatcher, with a terminal
> opened only to read logs.

Steps that are still manual in pass 1, named so the result is not oversold:

- authoring the plan and the `tasks.yaml` (no compiler — §2.2);
- choosing the revision;
- reading run logs outside the UI (§10).

## 10. Known gaps, deliberately left open

- **Logs are read outside dispatcher.** The acceptance criterion claims "visible in
  dispatcher" while permitting the one detailed channel to sit outside it. This is a
  temporary gap, not a design position; it is cheap to close because maestro already
  writes logs to a known per-run directory, so surfacing them is a read.
- **The one-launch invariant is a working agreement, not a mechanism** (§5.4.1). The
  durable lock binds `RunController` only; nothing prevents a terminal, a second
  controller instance, or a service tick from launching against the same `RepoKey`.
  Closing this means the controller taking maestro's own stage lock, which slice 0
  deliberately does not reach for.
- **No `merge_authority` anywhere.** Until ADR-ECO-008 D1 is enabled the contour behaves
  as `human`.
- **The join lives in dispatcher** (§3.1) until evidence says which producers must carry
  `work_id`.

## 11. Pilot working rule

While this contour is being built, findings go to a single friction log rather than to
cross-repo issues (precedent: research-bench Stage A, whose 13-item friction log became
Stage B's input). Three exceptions are filed immediately:

- a blocking dependency that stopped the pass;
- a security or authority defect;
- a neighbour-repository change **on the critical path of the pass**.

The third is narrow on purpose: the pass after this one is cross-repo by construction,
and a broad reading would restart the flood the log exists to prevent. (The seam
originally earmarked for pass 2 — ATP's `DIGESTS.json` consumed by maestro — shipped
through the ordinary PR process on 2026-08-22, atp-platform #301 and maestro #208, so
pass 2 needs a new subject. Choosing it is roadmap work, not a question this spec
answers.)

## 12. References (verified 2026-08-22)

Every line below was read at the stated anchor on 2026-08-22. Line numbers are cheap to
break and expensive to trust once broken; re-verify before citing this table onward.

| Claim | Source |
|---|---|
| `--run` selects, never creates | `maestro/maestro/run_bootstrap.py:71` (branch), `:53` (`_run_by_id` refusal) |
| maestro mints the id on the fresh path | `maestro/maestro/run_bootstrap.py:103` (`ulid.new()`) |
| atomic run publication via staging + rename | `maestro/maestro/run_publish.py:45-73` (`create_run`) |
| `RunIsLive` guards only an observed-live run | `maestro/maestro/run_bootstrap.py:97-102` |
| liveness is read from the lock-holder file | `maestro/maestro/run_state.py:60-70` (`classify_run`), `:66` |
| the stage lock is taken only by the service tick | `maestro/maestro/service/tick.py:133` (sole `ScopedLock` site) |
| an unresolved orphan is `interrupted`, never terminal | `maestro/maestro/run_state.py:70` |
| several non-terminal runs make resolution ambiguous | `maestro/maestro/run_registry.py:190` (`AmbiguousRun`) |
| `run-end` records the endings a run cannot observe | `maestro/maestro/cli.py:1973` |
| Mode 1 / Mode 2 split | `maestro/maestro/cli.py:1040` (`run`), `:1911` (`orchestrate`) |
| `approve` is for `AWAITING_APPROVAL`; `retry` clears `NEEDS_REVIEW` | `maestro/maestro/cli.py:1207`, `:940` |
| Mode-1 identity comes from the DAG's own `repo:`/`repo_url:` | `maestro/maestro/models.py:871`, `maestro/maestro/repo_identity.py:103-135`, `maestro/maestro/run_bootstrap.py:68` |
| `stop` is process-scoped, not run-scoped | `maestro/maestro/cli.py:1194` (`def stop_command() -> None`) |
| `approve`/`retry` take a positional task id | `maestro/maestro/cli.py:1207`, `:1154` |
| Mode-1 `TaskStatus.NEEDS_REVIEW` is verifier-assigned | `maestro/maestro/cli.py:682` |
| ActionRunner is synchronous, 120 s, process-local lock | `dispatcher/core/actions.py:51,351,498` |
| existing broker pattern with `--if-head` | `dispatcher/core/actions.py:586` |
| three-valued answer precedent (`merged` true/false/null) | `dispatcher/core/actions.py:580-584` |
| dispatcher classifies verdicts and never computes them | `dispatcher/core/governance.py:10-12` (ARCH-C3 / FR-02), `:233` |
| no sibling-repo path resolution | `dispatcher/core/governance.py:19` (CON-03) |
| ownership ⟂ movement, where movement = triggers/blockers | `packages/plan-fields/src/plan_fields/contract/README.md:84` |
| the pilot defect and its deliberate docstring | `deployer/src/deployer/verify.py:368`; `:361-362` |
| pilot behaviour pinned by test and golden | `deployer/tests/test_verify_static.py:860`; `deployer/corpus/golden/golden.json:199` |
| the pilot item itself | `deployer/TODO.md:84` |
| hand-written DAGs are established practice | `maestro/examples/maestro-builds-maestro.yaml` |
| `merge_authority` has no producer | ADR-ECO-008 D5 / D6; `dispatcher/TODO.md:150` |
