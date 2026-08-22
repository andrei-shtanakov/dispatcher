# Dark Factory control console (UI) — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the slice-0 control plane a console in dispatcher — issue a `RunRequest`, read its three-valued receipt, watch the run, drive the four Mode-1 verbs, and resolve `launch_unknown` — so that pass 1 can be accepted without a terminal.

**Architecture:** One new panel inside the existing single-page `index.html`, in the same vanilla-JS idiom as the panels already there: `render*` functions over server JSON, `hidden` toggling for visibility, `ensureActionToken()` for mutating POSTs. No framework, no build step, no new endpoint — the server half already exists and is merged (#167/#168/#169).

**Tech Stack:** Vanilla ES2020 in `dispatcher/server/static/index.html`; Node harnesses under `tests/web/` running the page's real `<script>` in a VM over `tests/web/dom.js`; pytest shelling out to Node.

**Spec:** `docs/superpowers/specs/2026-08-22-dark-factory-control-plane-slice0-design.md`. §2.1 and §9 both assume this console exists; the corrections PR (#170) records that it was never planned and that **pass 1 cannot be accepted until it lands**. This plan is that debt.

## Global Constraints

- **No new server endpoints.** Everything here consumes what is already merged: `POST /api/runs/submit`, `GET /api/runs/{request_id}`, `POST /api/runs/{request_id}/resolve`, `POST /api/runs/{request_id}/verb`. If a task seems to need a new route, stop and report — it means this plan misread the server.
- Mutating requests send `X-Action-Token` from `ensureActionToken()` (`index.html:616-620`) and `Content-Type: application/json`. Read requests send neither.
- The page is plain ES2020 with no bundler. Follow the surrounding idiom: `const get = async p => (await fetch(p)).json();`, `render*(data) -> html string`, `panel.innerHTML = render*(...)`, `section.hidden = …`.
- **Escape every server-supplied string** before it reaches `innerHTML`. The page has a helper for this — find it and use it; do not hand-roll a second one.
- Node harnesses run the page's **whole** real `<script>`, never a slice or a copy (`tests/web/governance_harness.js:1-14`). A harness that re-implements the function it tests proves nothing.
- The Python side of a JS test **fails** when `node` is missing; it must never skip (`tests/test_governance_js.py:25-29`: "a skip is how a suite goes green while covering nothing").
- Line length 88 for Python. Run `uv run ruff format . && uv run ruff check . --fix` and `uv run pyrefly check dispatcher tests scripts` — the **explicit paths**; the bare `pyrefly check` matches no files in a worktree under `.claude/` and reports success having checked nothing.
- Three tests fail in this environment on master too (the live-smoke cases needing absent binaries), plus two warnings from `test_benchmarks_stub_integration.py`. Expect exactly those.

## The two boundaries this console must hold

These are owner rulings, not implementation preferences. A task that violates one is wrong even if it passes its tests.

### B1. `run-end` is not a run control

It never appears beside `status`/`retry`/`approve`. It exists **only** inside the `launch_unknown` resolution flow, and only after the operator has explicitly chosen both a `run_id` from the correlated candidates and an outcome of `cancelled` or `superseded`.

The reason is in the server's own shape: `run-end` records "a decision that a run is over" — one of the two endings a run cannot observe about itself (`maestro/maestro/cli.py:1973`). Rendering it as a peer of `status` invites an operator to end a healthy run with one click, and the API cannot tell that click from a considered one.

### B2. `accepted: null` is not an error

It is a third state — *the launch may have happened* — and the console must render it as such: not red, not "failed", not a retry prompt. Its required consequences:

- the submit control for that repository is **blocked** until the state is resolved, because a second submit is the one action the recovery design forbids while attribution is ambiguous (spec §5.2.1);
- the resolution flow is offered instead;
- the copy says what is unknown and what to do, never "error".

Rendering `null` as a failure is the same defect as the server collapsing `null` into `false`, one layer up — and the server refuses to do that (spec §5.3), so the UI must not undo it.

## Out of scope

Authoring the `tasks.yaml` (no compiler — spec §2.2); Mode-2/workstream anything; merge from the UI; log streaming (spec §10 keeps logs outside dispatcher for now); a `stop` control (`maestro stop` is process-scoped, spec §6 as corrected).

## File Structure

| File | Responsibility |
| --- | --- |
| `dispatcher/server/static/index.html` (modify) | the panel's markup, `render*` functions, and event wiring — in that one file, as every other panel is |
| `tests/web/run_console_harness.js` (new) | Node harness: drives the real page script over a faked `fetch`, asserts wire + renderers |
| `tests/test_run_console_js.py` (new) | pytest entry point; fails when `node` is absent |
| `tests/test_run_api.py` (modify, part 2 only) | one server-side assertion per new UI assumption, so the console's contract is pinned on both sides |

## Task Right-Sizing

Two vertical parts, five tasks. Part 1 (Tasks 1–2) is a usable launch console on its own; Part 2 (Tasks 3–5) makes it a control console. **The plan is not done until Task 5 lands** — Part 1 alone would ship exactly the gap B2 exists to prevent: an operator who can create a `launch_unknown` and cannot leave it.

---

## Part 1 — launch and receipt

### Task 1: The panel, the form, and a submit that renders all three receipts

**Files:**
- Modify: `dispatcher/server/static/index.html`
- Create: `tests/web/run_console_harness.js`
- Create: `tests/test_run_console_js.py`

**Interfaces:**
- Produces: a `#run-console` section; `renderReceipt(receipt) -> html`; a submit handler posting to `/api/runs/submit`. Later tasks call `renderReceipt` and reuse the section.

**Design notes for the implementer:**

Read `renderRuns` (`index.html:1018`) and the merge-gate block around `index.html:616-649` first — they are the two closest existing shapes, one a renderer, one an action with a token and a result line.

The receipt is `{request_id, run_id, accepted, reason}` where `accepted` is `true | false | null`. **In JavaScript `null` and `false` are both falsy**, so `if (receipt.accepted)` and `if (!receipt.accepted)` are both wrong here and will silently merge the two states that this whole console exists to keep apart. Branch on `=== true`, `=== false`, `=== null` explicitly.

Three renderings, three different meanings:

| `accepted` | means | reads as | offers |
| --- | --- | --- | --- |
| `true` | the run directory was observed | started, with the `run_id` | the run's status |
| `false` | refused, nothing was created | refused, with the reason | fix the request and resubmit |
| `null` | the launch may or may not have happened | **unknown**, never "error" | the resolution flow (Task 4) |

- [ ] **Step 1: Write the failing harness**

Create `tests/web/run_console_harness.js`, modelled on `tests/web/runs_harness.js`. It must load the real `index.html`, run its whole `<script>` in a VM over `tests/web/dom.js`, fake `fetch` per-URL, and assert:

```js
// 1. the wire: submitting posts to /api/runs/submit with the token
await withPage(async page => {
  fill(page, '#rc-repository', 'deployer');
  fill(page, '#rc-revision', 'a'.repeat(40));
  fill(page, '#rc-tasks', 'tasks.yaml');
  fill(page, '#rc-work-id', 'todo://deployer/entrypoint-token-boundary-match');
  await click(page, '#rc-submit');
  const call = page.calls.find(c => c.url === '/api/runs/submit');
  assert(call, 'submit posted');
  assert(call.opts.headers['X-Action-Token'] === 'test-token', 'token sent');
  const body = JSON.parse(call.opts.body);
  assert(body.repository === 'deployer' && body.tasks === 'tasks.yaml',
    'body carries the form');
});

// 2. accepted:true renders as started and shows the run id
assertReceipt({accepted: true, run_id: '01AAA', reason: null},
  html => html.includes('01AAA') && !/error|fail/i.test(html));

// 3. accepted:false renders as a refusal WITH the reason
assertReceipt({accepted: false, run_id: null, reason: 'busy: deployer'},
  html => html.includes('busy: deployer'));

// 4. accepted:null is NOT an error and does NOT say failed  (boundary B2)
assertReceipt({accepted: null, run_id: null,
               reason: 'launch_unknown: no run appeared within 120s'},
  html => !/error|failed|refused/i.test(html) && /unknown/i.test(html));

// 5. the three states are actually distinguished, not merged by falsiness
const asFalse = receiptHtml({accepted: false, reason: 'refused'});
const asNull = receiptHtml({accepted: null, reason: 'unknown'});
assert(asFalse !== asNull, 'false and null must not render identically');
```

Add `tests/test_run_console_js.py` mirroring `tests/test_governance_js.py` exactly — same hard-prerequisite message shape, same "must FAIL, not skip" reasoning in the docstring.

- [ ] **Step 2: Run it and watch it fail for the right reason**

Run: `uv run pytest tests/test_run_console_js.py -v`
Expected: FAIL — the harness cannot find `#rc-submit`, because the panel does not exist yet. If it fails for any other reason, read the message before writing code.

- [ ] **Step 3: Add the panel markup**

Add a section to `index.html` beside the other top-level sections (after `#benchmarks-section` is a reasonable home). Fields: repository, revision, tasks, work_id, and optional spec_ref/plan_ref path+commit. A submit button `#rc-submit` and an empty `#rc-receipt` for the result.

Keep the markup in the page's existing style — plain labels and inputs, no new CSS framework. Reuse the existing `act-result`/`ok`/`err` classes only where they are honest: **`accepted: null` must not carry `err`.** Add one class for it rather than borrowing a wrong one.

- [ ] **Step 4: Write `renderReceipt` and the submit handler**

```js
function renderReceipt(r) {
  // `accepted` is three-valued (spec §5.3) and both `false` and `null` are
  // falsy in JS, so every branch here is an explicit identity check. A
  // truthiness test would merge "no run was created" with "a run may exist",
  // which is the one distinction this console exists to preserve.
  if (r.accepted === true)
    return `<div class="rc-result ok">✓ started — run
      <code>${esc(r.run_id)}</code></div>`;
  if (r.accepted === false)
    return `<div class="rc-result err">✗ refused — ${esc(r.reason ?? '')}</div>`;
  return `<div class="rc-result unknown">? unknown — the launch may or may not
    have produced a run. ${esc(r.reason ?? '')}
    <span class="rc-hint">Resolve it before submitting again.</span></div>`;
}
```

Use the page's own escaping helper in place of `esc` — find it rather than adding another.

The submit handler follows the merge-gate shape at `index.html:625-649`: disable the button, POST with the token, render the receipt, re-enable in `finally`.

- [ ] **Step 5: Run the harness to green**

Run: `uv run pytest tests/test_run_console_js.py -v`
Expected: PASS, all five assertions.

- [ ] **Step 6: Full suite, lint, typecheck, commit**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check dispatcher tests scripts
git add dispatcher/server/static/index.html tests/web/run_console_harness.js tests/test_run_console_js.py
git commit -m "feat(console): RunRequest form and a three-valued receipt"
```

---

### Task 2: Client-side validation that mirrors the server's refusals

**Files:**
- Modify: `dispatcher/server/static/index.html`
- Modify: `tests/web/run_console_harness.js`

**Interfaces:**
- Consumes: the form from Task 1.
- Produces: `validateRunRequest(fields) -> string | null` (a message, or `null` when the request is worth sending).

**Design notes:** the server already refuses a bad request cleanly, so this is not a safety layer and must not pretend to be. Its only job is to save a round trip on the two mistakes an operator makes constantly, with the *same words* the server would use — divergent copy between the two is worse than no client check at all.

Mirror exactly two rules, both from `dispatcher/core/run_request.py`: `revision` must be 40 hex characters, and `tasks` must be repo-relative with no leading `/` and no `..` segment. **Do not** mirror the repository-manifest check, the `git cat-file` existence check, or the `repo:` reconciliation — the client cannot know any of them, and a client that guesses will refuse valid requests.

- [ ] **Step 1: Write the failing assertions (append to the harness)**

```js
// a bad revision never reaches the wire, and says the same thing the server would
await withPage(async page => {
  fill(page, '#rc-repository', 'deployer');
  fill(page, '#rc-revision', 'HEAD');
  fill(page, '#rc-tasks', 'tasks.yaml');
  await click(page, '#rc-submit');
  assert(!page.calls.some(c => c.url === '/api/runs/submit'), 'not sent');
  assert(/40-hex/.test(text(page, '#rc-receipt')), 'says 40-hex, as the server does');
});

// `..` in tasks is caught client-side
await withPage(async page => {
  fillValid(page);
  fill(page, '#rc-tasks', '../outside.yaml');
  await click(page, '#rc-submit');
  assert(!page.calls.some(c => c.url === '/api/runs/submit'), 'not sent');
});

// a valid-looking request IS sent — the client must not invent refusals
await withPage(async page => {
  fillValid(page);
  await click(page, '#rc-submit');
  assert(page.calls.some(c => c.url === '/api/runs/submit'), 'sent');
});
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/test_run_console_js.py -v`
Expected: FAIL — the request is sent despite `HEAD`.

- [ ] **Step 3: Implement `validateRunRequest` and call it before the POST**

- [ ] **Step 4: Green, then commit**

```bash
uv run pytest tests/test_run_console_js.py -v
uv run ruff format . && uv run ruff check . --fix
git commit -am "feat(console): client-side checks for the two mistakes the server also names"
```

---

## Part 2 — status, controls, resolution

### Task 3: The run view — read a request back and show maestro's own state

**Files:**
- Modify: `dispatcher/server/static/index.html`
- Modify: `tests/web/run_console_harness.js`

**Interfaces:**
- Consumes: `GET /api/runs/{request_id}` → `RunView {record, run, warnings}`.
- Produces: `renderRunView(view) -> html`; polling that stops when **both** axes are settled (see the stop rule below).

**Design notes:** `record.state` is dispatcher's launch state machine (`reserved`/`launching`/`materialized`/`terminal`/`launch_unknown`); `run.status` is maestro's classification of the run itself (`running`/`interrupted`/`suspended`/`completed`/`cancelled`/`superseded`/`failed`/`unreadable`). **They are different axes and must be shown as two facts, not merged into one badge** — dispatcher renders maestro's FSM, it does not restate it (spec §3.2).

`view.run` is `null` when the record has no `run_id` yet, and also when the run directory is gone. `view.warnings` carries the collector's read failures — an unreadable run directory produces `run: null` *with* a warning, while an absent one produces `run: null` and no warning. Show the warnings; that distinction is why the field exists.

- [ ] **Step 1: Write the failing assertions**

```js
// the two axes are both visible and not conflated
assertView({record: {state: 'materialized', run_id: '01AAA'},
            run: {run_id: '01AAA', status: 'running'}, warnings: []},
  html => /materialized/i.test(html) && /running/i.test(html));

// no run yet: the record still shows, the run half says so plainly
assertView({record: {state: 'launching', run_id: null}, run: null, warnings: []},
  html => /launching/i.test(html) && !/running/i.test(html));

// unreadable is NOT absent: the warning must reach the screen
assertView({record: {state: 'materialized', run_id: '01AAA'}, run: null,
            warnings: ['runs enumeration: cannot list /x: Permission denied']},
  html => /Permission denied/.test(html));

// polling stops once BOTH axes are settled
await withPage(async page => {
  await openView(page, 'req-1', {record: {state: 'terminal', run_id: '01AAA'},
                                 run: {status: 'completed'}, warnings: []});
  const before = page.calls.length;
  await tick(page, 5000);
  assert(page.calls.length === before, 'no polling after terminal');
});

// …but a materialized record with a LIVE run keeps polling: the record is
// finished and the run is not, and stopping here would freeze the run half
// of the display on `running` forever.
await withPage(async page => {
  await openView(page, 'req-2', {record: {state: 'materialized', run_id: '01AAA'},
                                 run: {status: 'running'}, warnings: []});
  const before = page.calls.length;
  await tick(page, 5000);
  assert(page.calls.length > before, 'still polling while the run is live');
});

// launch_unknown is settled until an operator acts — do not poll into it
await withPage(async page => {
  await openView(page, 'req-3', {record: {state: 'launch_unknown', run_id: null},
                                 run: null, warnings: []});
  const before = page.calls.length;
  await tick(page, 5000);
  assert(page.calls.length === before, 'no polling while awaiting the operator');
});
```

- [ ] **Step 2: Run, watch it fail, implement `renderRunView` + polling, run to green**

**The stop rule, because two axes means two ways to get it wrong.** An earlier draft of
this plan said "stop on `materialized`" in one place and "stop on a terminal state" in
another, and neither was right on its own — the contradiction is what made the real
answer visible.

`materialized` is where dispatcher's launch machine *finishes*, and where maestro's run
*begins*. Stopping there freezes the run half of the display at whatever it said the
moment the directory appeared — usually `running` — and it would stay that way after the
run completed, which is the panel telling a confident lie.

So: keep polling while there is anything left to learn on **either** axis, and stop only
when both are settled.

- Keep polling while `record.state` is `reserved` or `launching` — the launch is still
  resolving.
- Keep polling while `record.state` is `materialized` **and** `run.status` is not one of
  `completed`/`cancelled`/`superseded`/`failed` — the record is done, the run is not.
- Stop on `record.state` of `terminal` or `launch_unknown`: the first is settled, and
  the second is settled *until an operator acts*, so it must not be polled into (Task 4
  drives it by hand).
- Stop when `record.state` is `materialized` and `run.status` is one of the four terminal
  values above.
- `run` being `null` under a `materialized` record is **not** a stop condition: it means
  either the directory is gone or unreadable (`warnings` says which), and both deserve to
  keep being checked rather than silently freezing.

Reuse the page's existing interval idiom rather than adding a scheduler.

**Test harness note — do not copy the no-op timer stub.** Every sibling harness
(`runs_harness.js:175`, `governance_harness.js:176`, `benchmarks_harness.js:191`) stubs
`setInterval`/`clearInterval` to no-ops, because those pages never assert on their own
polling and the page's `setInterval(refresh, 10000)` would otherwise loop the harness
forever. That stub is wrong for THIS task: the two "polling stopped" assertions above
would pass having never scheduled anything, and only "still polling while the run is
live" could fail — a vacuous suite one careless deletion away from proving nothing.
`run_console_harness.js` therefore installs a controllable virtual clock instead
(`makeVirtualClock()`): `setInterval`/`clearInterval` do real id→`{cb, period, due}`
bookkeeping, and a test-only `tick(page, ms)` advances a virtual "now", firing whichever
callbacks are due — including the page's own `setInterval(refresh, 10000)`, which
registers on the same clock and fires if a tick crosses 10000ms. That's correct
behaviour, not a test failure; the stop-rule tests stay at `tick(page, 5000)` specifically
to keep the global refresh out of the count they're asserting on.

- [ ] **Step 3: Commit**

```bash
git commit -am "feat(console): run view showing dispatcher's state and maestro's, separately"
```

---

### Task 4: The `launch_unknown` resolution flow — and the only place `run-end` lives

**Files:**
- Modify: `dispatcher/server/static/index.html`
- Modify: `tests/web/run_console_harness.js`
- Modify: `tests/test_run_api.py`

**Interfaces:**
- Consumes: `POST /api/runs/{id}/resolve` with `{}` (adopt) or `{run_id, outcome}` (end a named orphan) → `UnknownResolution {adopted_run_id, candidates, reason}`.
- Produces: `renderResolution(res) -> html`; the resolution flow; the submit block from B2.

**This task carries both boundaries. Read B1 and B2 above before writing anything.**

**Design notes:**

The server's rule: adoption happens only when correlation yields **exactly one** candidate; zero and two-or-more both stay `launch_unknown` (spec §5.2.1). The UI's job is to make that legible rather than to soften it:

- one candidate → the adopt attempt succeeds and reports which run was adopted;
- zero candidates → "no new run to correlate"; the operator has nothing to end, and the flow must say so rather than showing an empty `run_id` picker;
- two or more → the candidates are listed, and **only here** does an outcome selector plus a `run-end` button appear, per B1.

`POST /resolve` requires `run_id` and `outcome` **together or not at all** — either is a 422 (this is what #168 fixed). So the picker must not enable its button until both are chosen.

- [ ] **Step 1: Write the failing assertions**

```js
// B1: run-end is NOWHERE near the normal controls
assertView({record: {state: 'materialized', run_id: '01AAA'},
            run: {status: 'running'}, warnings: []},
  html => !/run-end/i.test(html));

// B2: launch_unknown blocks resubmission and offers resolution instead
await withPage(async page => {
  await openView(page, 'req-1', {record: {state: 'launch_unknown', run_id: null},
                                 run: null, warnings: []});
  assert(page.q('#rc-submit').disabled === true, 'submit blocked while unknown');
  assert(page.q('#rc-resolve') !== null, 'resolution offered');
  assert(!/error|failed/i.test(text(page, '#rc-run-view')), 'not an error');
});

// zero candidates: no picker, no run-end, a plain statement
assertResolution({adopted_run_id: null, candidates: [], reason: 'no new run…'},
  html => !/run-end/i.test(html) && /no new run/i.test(html));

// exactly one: adopted, and the id is named
assertResolution({adopted_run_id: '01LATE', candidates: ['01LATE'], reason: '…'},
  html => html.includes('01LATE') && !/run-end/i.test(html));

// two or more: BOTH candidates listed, run-end available — the only place it is
assertResolution({adopted_run_id: null, candidates: ['01AAA', '01BBB'],
                  reason: 'ambiguous'},
  html => html.includes('01AAA') && html.includes('01BBB') && /run-end/i.test(html));

// the end button stays disabled until run_id AND outcome are both chosen
await withPage(async page => {
  await openAmbiguous(page, ['01AAA', '01BBB']);
  assert(page.q('#rc-end').disabled === true, 'disabled with neither');
  select(page, '#rc-end-run', '01AAA');
  assert(page.q('#rc-end').disabled === true, 'still disabled with only a run');
  select(page, '#rc-end-outcome', 'cancelled');
  assert(page.q('#rc-end').disabled === false, 'enabled with both');
});
```

Add one server-side assertion to `tests/test_run_api.py` pinning the pairing the UI relies on: `POST /resolve` with `run_id` only, and with `outcome` only, are each 422. (#168 implemented both; this records that the console depends on it.)

- [ ] **Step 2: Run, watch it fail, implement, run to green**

- [ ] **Step 3: Commit**

```bash
git commit -am "feat(console): launch_unknown resolution, the only home of run-end"
```

---

### Task 5: The three run controls, and the acceptance pass

**Files:**
- Modify: `dispatcher/server/static/index.html`
- Modify: `tests/web/run_console_harness.js`
- Modify: `docs/superpowers/specs/2026-08-22-dark-factory-control-plane-slice0-design.md`

**Interfaces:**
- Consumes: `POST /api/runs/{id}/verb` with `{verb, task_id?, outcome?}` → `VerbOutcome {verb, run_id, ok, stdout, stderr}`.
- Produces: `status`, `retry`, `approve` controls; `renderVerbOutcome`.

**Design notes:** the server's allowlist is `{status, retry, approve, run-end}` — four, not five. `stop` is absent because `maestro stop` terminates the scheduler process rather than a run (spec §6 as corrected); do not add a control for it.

`approve` and `retry` each need a **task id** and act on one task, not the whole run. `approve` releases a task in `AWAITING_APPROVAL`; a task in `NEEDS_REVIEW` is cleared by `retry` instead. Label them so an operator cannot mistake one for the other — this exact confusion cost a fix round on the server side.

`run-end` must not appear here (B1).

- [ ] **Step 1: Write the failing assertions**

```js
// exactly three controls, and run-end is not among them  (B1)
assertControls(['status', 'retry', 'approve']);
assertNoControl('run-end');
assertNoControl('stop');

// approve and retry each demand a task id before they will fire
await withPage(async page => {
  await openMaterialized(page);
  await click(page, '#rc-verb-approve');
  assert(!page.calls.some(c => c.url.endsWith('/verb')), 'not sent without a task id');
  assert(/task/i.test(text(page, '#rc-verb-result')), 'says what is missing');
});

// a verb that fails renders stderr rather than a bare "failed"
assertVerbOutcome({verb: 'status', ok: false, stdout: '', stderr: 'no such run'},
  html => html.includes('no such run'));
```

- [ ] **Step 2: Run, watch it fail, implement, run to green**

- [ ] **Step 3: Update the spec's acceptance section**

The console now exists, so spec §9's "still manual in pass 1" list must lose its first entry (added by #170: "issuing the request at all — there is no UI"), and §10's first known gap must go. Do not touch anything else in the spec; a stale acceptance criterion is what this whole plan is repaying.

- [ ] **Step 4: Full suite, lint, typecheck, commit**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check dispatcher tests scripts
git commit -am "feat(console): status/retry/approve controls; pass 1 no longer needs a terminal"
```

---

## After the plan

Pass 1 becomes runnable when Task 5 lands, but is still **not** accepted — it needs the pilot `tasks.yaml` in deployer and an actual run. Two things stay outside dispatcher: authoring the DAG (no compiler, spec §2.2) and reading run logs (spec §10).

The one-launch invariant remains a working agreement rather than a mechanism (spec §5.4.1): the durable lock binds `RunController` only, and this console gives an operator a second convenient way to launch, which makes the agreement matter more, not less. Worth a line in the panel's own copy.

## Self-review

**Spec coverage.** §2.1's "issued from the dispatcher UI" — Tasks 1–2. §5.3's three-valued receipt — Task 1, with the JS falsiness trap named explicitly because `false` and `null` are both falsy. §3.2's "render maestro's FSM, don't restate it" — Task 3 keeps the two axes separate. §5.2.1's adoption rule and its three outcomes — Task 4. §6's four verbs — Task 5, with `stop` excluded and the reason given. §9/§10's acceptance debt — Task 5 Step 3.

**Boundaries.** B1 is asserted three times, in the two places it could be violated (the run view in Task 4, the controls in Task 5) and once positively (it *does* appear for the ambiguous case). B2 is asserted as three separate properties — not an error, submit blocked, resolution offered — because "renders differently" would pass on a red box saying "unknown".

**Placeholders:** none. Every code step carries real code or an exact description of the assertion to write. Two steps deliberately say "find the page's existing helper rather than adding another" — that is an instruction to read, not a gap.

**Type consistency:** `renderReceipt` (T1) is reused by T4's resolution result; `renderRunView` (T3) hosts T4's resolution block and T5's controls; `#rc-submit` (T1) is the element T4 disables. `UnknownResolution.candidates`, `VerbOutcome.stderr` and `RunView.warnings` are the server's own field names, checked against `dispatcher/core/run_controller.py` at `fdc5215`.

**Fake-DOM support, checked rather than assumed.** The assertions above lean on three
things `tests/web/dom.js` must model, and all three are confirmed:

- `disabled` is a real property, seeded from parsed markup and already asserted by an
  existing harness (`dom.js:57,85`; `tests/web/run_status_harness.js:228,259`);
- `value` likewise (`dom.js:58,86`), and harnesses already set it on inputs
  (`run_status_harness.js:171`);
- `dataset` and `classList` (`dom.js:48,89,102`).

What is **not** modelled: `<option>` children, `selectedIndex`, and `options`. An element
is an `El` with a `value` property and nothing more. So the outcome picker in Task 4 must
be driven by setting `.value` and dispatching `change` — do not write page code that reads
`select.options` or `selectedIndex`, because the harness cannot exercise it and the test
would pass by not running the branch.

If some other capability turns out to be missing, extend `dom.js` in the same commit and
say so in the report. Do not work around it by asserting on a class instead of the
property — the property is what a browser obeys, and a class that merely accompanies it is
the kind of proxy assertion that stays green after the real behaviour breaks.
