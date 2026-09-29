# DarkFactory human control plane — halt, human queue, "My turn"

**Status:** 2026-09-29, r2 — direction document, revised after review r1 the same
day. Not a task plan. Each slice below gets its own spec+plan pair before code (local
polish loop, one PR per pair). Facts marked **verified** were checked by execution on
2026-09-29; everything else is a claim to verify before the slice that depends on it.

**Correction, r2:** r1 claimed a squash merge of a candidate PR "creates no signature".
The code does not support that: `devtools/human-merge.sh` accepts `--squash`
(`devtools/tests/test_human_merge.py:117`), and the signature is judged from the merge
actor and the policy (`devtools/governance/approval_facts.py:169`, `:401`), not from the
commit shape. §4 now rests the no-merge-button decision on the missing approval checks
instead.

**Citations** follow the slice-0 rule: neighbour code is cited workspace-relative and
repository-first (`devtools/merge-pr.sh:145`) and is not a file of this repository.

## 1. Problem

Under ADR-ECO-011 the agent merges by default. Human authority is concentrated in a few
acts: merging a PR the policy reserves for a human, returning work, and stopping the
factory. Today none of the three has a place:

- **Observation is scattered.** What waits for a human lives in at least six places,
  each in its own shape: PRs labelled `human-merge-required`
  (`devtools/merge-pr.sh:145`), candidate PRs of bundle approval (§I12), governance
  runner states `waiting_human_merge` / `stopped_*`
  (`devtools/governance/runner.py:661`), maestro tasks in `NEEDS_REVIEW` /
  `AWAITING_APPROVAL`, dispatcher's own `launch_unknown` requests
  (`core/run_store.py:35`), and impresario `needs_human` stops. The S7 control run
  needed nine human acts; a human found each of them by knowing where to look.
- **Nothing stops the factory.** There is no fleet-wide halt. Run-scoped controls exist
  (`/api/runs/{id}/verb`); `maestro stop` kills a scheduler, not a run (slice-0 §6).
  `devtools/merge-pr.sh` states the constraint itself: the script is a rule, not a
  boundary, and a real boundary is possible only on the forge side.

Control here means **observation first**: seeing what the factory is doing, what it has
done without a human, and what it is waiting for. Buttons come second, and each has to
act under the right identity.

## 2. Principles (inherited, not new)

1. **dispatcher derives the waits; it does not own them** (slice-0 §3.1). The queue is a
   projection over sources that already exist, with no new registry of "tasks for
   humans". dispatcher does own records nobody else owns — `RunRequest` today, the halt
   request in §6.3 — but never the wait state or the effective halt.
2. **Unknown is not zero** (NFR-02). A source that could not be read, or was read only
   in part, renders as *unknown* and makes the queue *incomplete*, never silently
   shorter. "Every local source read" is not "the whole fleet visible": completeness is
   stated per source and per host.
3. **Human acts run under the human's identity.** The web process never holds the agent
   token (slice-0 §7.4); dispatcher never performs an act whose validity depends on the
   actor through a path that lacks that act's own checks.
4. **Effective state is read back, not asserted.** Like `accepted` / `merged`, a halt is
   "on" only when the forge says so.

## 3. Slice A — the human queue (read model)

`GET /api/human-queue` → a list of waits plus per-source completeness.

    HumanWait
      key         stable wait identity (per kind, see §3.2)
      reasons[]   pr_human_merge | bundle_candidate | run_needs_review |
                  run_awaiting_approval | launch_unknown | loop_needs_human |
                  proposal_gate
      repo        manifest key
      ref         PR URL | todo:// | request_id | run_id/task_id
      title       one line, from the source
      since       ISO time the wait began, or null
      since_basis what `since` is (e.g. "loop stop.at"); null when since is null
      act         typed action + validated arguments (§4), never a shell string

    HumanQueueView
      waits[]
      sources     {name: ok | truncated(detail) | unavailable(detail)}
      complete    bool — false if any source is not `ok`
      generated_at

### 3.1 Sources, v1 vs later

| Source | Where | v1? | Note |
|---|---|---|---|
| Open PRs labelled `human-merge-required`, fleet-wide | forge | A2 | Machine-independent; label is the ratified signal |
| Candidate PRs (§I12 branch form) | forge | A2 | Branch form owned by `devtools/approval_branches.sh`; vendor the pattern, don't import |
| `launch_unknown` requests | own `run_store` | A1 | Already ours |
| maestro tasks `NEEDS_REVIEW` / `AWAITING_APPROVAL` | maestro run DBs | A1 | **Not** the existing snapshot — see below |
| impresario `needs_human` | existing collector | A1 | `stopped_at` is a proven wait start |
| proposal gates | existing collector | A1 | no proven wait start (§3.2) |
| governance runner `waiting_human_merge` / `stopped_*` | `~/.local/state` of whichever machine runs it | later | Machine-local, uncontracted. Reading it by path breaks CON-03 and is blind to the VPS runner. Needs a published run-state contract from devtools (handoff) |

**The maestro snapshot cannot feed the queue.** The collector reads
`ORDER BY created_at DESC LIMIT 50` (`core/collectors/maestro.py:49`) — built for a
panel, not for completeness. An old task still in `NEEDS_REVIEW` falls outside the
window, and filtering the snapshot would produce a falsely short queue. A1 needs its own
query for waiting tasks (by status, no recency window), per run DB, with each unreadable
DB reported as `unavailable` for that source.

**A1 is a partial queue by construction** — forge sources arrive in A2. Until then the
view says so (`sources` lists the forge rows as not yet connected), rather than
presenting A1 as v1 complete.

Most of the runner's human waits surface through the forge rows anyway: a
`waiting_human_merge` run has an open PR with the label. The gap is `stopped_*`, which
has no PR. That is the handoff's argument, not a reason to read the path.

### 3.2 Wait identity and age

- **Key.** One human act = one wait. A PR matching both the label and the candidate
  pattern is one wait with two `reasons`, keyed by PR. A repeated `needs_human` after
  resume is a new wait — the existing `(loop_id, iteration)` identity
  (`core/product_proposals.py:138`) already says so. maestro waits key by
  `(run_id, task_id, status)`.
- **Age.** `since` is filled only from a proven wait-start time: loop `stopped_at`
  (`core/product_proposals.py:143`), the label/review-request event on the PR timeline
  (to verify in A2). It is **null** where no such time exists: proposal gates (the code
  forbids reading `proposal_updated_at` as one, `core/product_proposals.py:129`) and
  maestro tasks (`TaskInfo` has `started_at` / `completed_at`, no time of entering
  `NEEDS_REVIEW`, `core/models.py:39`). No substitution by PR creation time, file
  mtime, or first-seen time. A null-age wait stays in the list, sorted into its own
  group "age unknown", not into a made-up place among the old and new.

### 3.3 Forge read

Listing labelled / candidate / merged-by PRs across the fleet is a new read. It goes
through `github-checker` like every other forge call (`core/actions.py:1-24`), so it
needs a read verb there (handoff) with pagination and per-repo partial failure in the
envelope. A read takes no lock. Transport failure → `unavailable`, never `[]`.

## 4. Slice B — "My turn" (render)

One view, three surfaces, identical words (FR-06 parity):

- **web** — a tab: waits with known age sorted by `since`, then the "age unknown" group;
  per wait the prepared command and a link to the PR / run view.
- **VSCode** (`vscode-ext`) — tree view `dispatcherMyTurn` grouped by reason; status-bar
  item `⏳ N` where N = known unique waits; `⏳ N · ?` when `complete` is false; red when
  any wait with known age exceeds the threshold. Overdue and incomplete are independent
  and can show together.
- **TUI** — same list.

**Actions are prepared, not executed.** `act` is a typed action with validated
arguments (repo, PR number, expected head SHA); the command string is built from those,
never copied from PR titles or bodies. For a human merge it is
`devtools/human-merge.sh … --expect-head <sha>`, with the execution environment stated
(local vs Remote SSH / VPS). VSCode "Run in terminal" opens a visible terminal with the
command pre-filled and **not executed**. Pressing Enter does not prove identity; the
script's own checks (actor in the approver allowlist, pinned/current policy, PR head)
do.

**No merge button in v1.** The existing merge gate (`core/actions.py:574`) does not
carry the approval contract that `devtools/human-merge.sh` enforces — actor against the
policy's approver list, policy pin vs current version, head pin. Routing human-reserved
PRs through it would skip exactly those checks. A button may come later as an
invocation of the script itself; that is open decision 4.

## 5. Slice C — the factory floor (observation beyond the queue)

What the factory is doing and has done without a human:

- **active now** — dispatcher RunRequests in flight, maestro runs `running`, from the
  existing collectors;
- **agent acts, last 24h** — PRs merged with `merged_by: ai-prosto` fleet-wide (the
  ADR-ECO-011 D3 audit signal), via the same `github-checker` read verb;
- **time in state** — waits and runs by how long they have been where they are (with the
  same null rule as §3.2); a stuck item is the observation, not an alert rule dispatcher
  invents. Money cost is out of scope here;
- **halt state** per repo (slice D).

This slice overlaps `todo://dispatcher/agent-merge-observability` (waiting on "first
incident"). The feed here is the cheap `merged_by` read ADR-ECO-011 OQ-1 already
accepted, not that item's actor-aware evidence; the item stays as is.

## 6. Slice D — the halt ("stop-crane")

### 6.1 Two layers, stated precisely

**Enforced layer: a forge ruleset.** Per repository, a ruleset `darkfactory-halt` on
`~DEFAULT_BRANCH` with rule `update` (restrict updates), bypass = repository admin role
only. Once active, GitHub refuses updates of the default branch to every actor without
bypass, whichever script, runner, or direct API call they use.

Verified 2026-09-29 on `andrei-shtanakov/dispatcher`:
- `ai-prosto` has `write`, not `admin` → no bypass under an admin-only list;
- the repository is public → rulesets are available on this plan;
- two rulesets are already active (`Default Branch Restriction`, `governance-gate`);
  the existing one bypasses the admin role plus three Integrations — the halt ruleset
  must **not** copy that list, or Integrations keep merging through a halt;
- the agent profile (`GH_CONFIG_DIR=~/.config/review`) can read effective branch rules
  via `GET /repos/{o}/{r}/rules/branches/master`.

**Detecting the halt needs its own contract.** The branch-rules endpoint returns only
*active* rules (with `ruleset_id`); a disabled or missing ruleset is simply absent. So
"no halt rule in the answer" cannot distinguish *off* from *never configured* or
*misconfigured*. The halt read (github-checker verb, handoff) identifies the ruleset by
name/id through the rulesets API, paginates, and checks target branch, rule, and bypass
list. Its states: `on` / `off` (confirmed disabled) / `missing` / `misconfigured` /
`unknown` (read failed). Only `on` and `off` are healthy.

**Cooperative layer: admission checks.** The enforced layer stops *landing*, not
*working*: agents would keep burning budget on branches that cannot merge. Actors read
the halt before **admitting new work** and refuse on `on` **or on anything other than
confirmed `off`**: `devtools/merge-pr.sh`, `devtools/governance/runner.py`,
dispatcher's `RunController` (before submit), spec-runner and maestro pre-launch.

The unit of admission is a **new run**. A run admitted before the halt drains,
including its internal steps and retries; its merge is still refused by the enforced
layer. Stopping new tasks *inside* an admitted run is a separate draining contract, not
v1.

**Limits of the cooperative layer, stated rather than hidden.** One source removes
two independent flags, but not timing: a halt can land between an actor's read and its
launch, a read can be stale, GitHub can be unreachable (→ `unknown` → refuse). The
enforced layer is what covers that window for merges.

### 6.2 What the halt does not do

- It does not kill runs in progress (maestro has no run-scoped stop, slice-0 §6; killing
  processes on another machine from a web process is not a control this plane should
  hold).
- It does not stop admins: bypass lets an admin update the branch during a halt.
- It does not cover integration branches — only the default branch.
- It does not stop Dependabot opening PRs or working on its branches; it stops their
  landing.

So: **nothing new lands on a default branch except by an admin, nothing new is
admitted; admitted runs drain to their branches.** The factory floor (slice C) shows
what is still draining.

### 6.3 dispatcher's part

- Toggle per repo and fleet-wide, by explicit human click, via a `github-checker` verb
  (handoff), locked per repo like merge. Requires a reason; lifting too.
- **Principal.** The toggle runs under the forge profile of the dispatcher process. That
  is one identity, not the identity of whoever opened the page: v1 assumes a
  single-operator, local dispatcher and records this as a limit. A multi-operator
  dispatcher needs its own authority design before this slice ships there.
- dispatcher persists only the **halt request** (who, when, reason, scope, fleet
  composition at request time). Effective state is read back per §6.1.
- A fleet-wide halt is N independent operations. Confirmed halts are kept on partial
  failure; only failed repos are retried; no automatic lift to "roll back" a partial
  halt. Repos added to the fleet after the request show as deviations, not as covered.

### 6.4 D0 — verify by execution before D1

On a sandbox repository:
1. PR merge by a `write` actor is refused while the ruleset is active;
2. direct push by a `write` actor is refused;
3. an Integration / app actor is refused (bypass list without Integrations);
4. an admin can still update the branch (bypass works as described);
5. after `disabled`, merges work again;
6. the halt read reports `on` / `off` / `missing` / `misconfigured` correctly;
7. background default-branch writers (`post-merge-sync`, snapshot publish) — decide
   whether halting them is desired, since they stop too.

## 7. Order and handoffs

| Step | Where | Depends on |
|---|---|---|
| A1 partial queue: own + maestro (full query) + impresario + proposals | dispatcher | — |
| B "My turn" web + VSCode + TUI | dispatcher | A1 |
| A2 forge sources (label, candidate) | dispatcher | github-checker read verb |
| C factory floor | dispatcher | github-checker read verb |
| D0 sandbox verification (§6.4) | owner / dispatcher | — |
| D1 halt toggle + read-back | dispatcher | github-checker halt read/write verbs, D0 |
| D2 admission checks | devtools, spec-runner, maestro | D1 (inbox issues) |
| runner state contract | devtools | inbox issue; adds `stopped_*` to the queue |

Handoffs are inbox issues (ADR-ECO-006):
- `github-checker` — PR read (label / candidate / merged_by, pagination, partial
  errors); halt read/write with ruleset identification and result confirmation;
- `devtools` — admission unit of the runner and halt check in `merge-pr.sh`; export of
  `stopped_*` state;
- `maestro`, `spec-runner` — fail-closed pre-launch check; retries/resume of an admitted
  run.

None of them blocks A1 or B.

## 8. Owner decisions

Recommended by review r1 and **ratified by the owner on 2026-09-29**:

| # | Question | Decision for v1 |
|---|---|---|
| 1 | Halt granularity | Repo + fleet. Workstream pause later, separately, as a cooperative control (a workstream spans repos; a repo halt spans workstreams) |
| 2 | Who lifts a halt | Any admin, with a reason (binding to the initiator creates a dependency on their availability, and another admin could flip the ruleset directly anyway) |
| 3 | Dependabot | Its landing is blocked; PR creation continues. Do not promise a full Dependabot stop |
| 4 | Merge from "My turn" | Prepare the command only; no execution by the server |
| 5 | Red threshold | 24h, configurable, global; per-reason overrides later. A product hypothesis, not a measured SLA. Unknown age shown separately, never as zero |
