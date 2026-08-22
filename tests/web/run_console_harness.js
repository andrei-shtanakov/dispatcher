// Exercises the Run console panel (spec §5.3, TASK-1 of the Dark Factory
// control console) by running the REAL, WHOLE <script> of
// dispatcher/server/static/index.html inside a VM over the page's own
// parsed markup (tests/web/dom.js) — same discipline as the sibling
// harnesses, self-contained on purpose: each harness's loader and fixtures
// are module-local (see governance_harness.js's header for the reasoning).
//
// Three layers are asserted here:
//   1. the wire: filling the RunRequest form and clicking #rc-submit posts
//      to /api/runs/submit with the X-Action-Token and the form's values.
//   2. the pure renderer: renderReceipt() over the receipt's three-valued
//      `accepted` (true | false | null). `null` and `false` are BOTH falsy
//      in JS, so a truthiness branch would silently merge "refused, nothing
//      was created" with "the launch may have happened" — the one
//      distinction this console exists to preserve. Asserted directly here:
//      true reads as started (no error/fail wording), false reads as a
//      refusal carrying its reason, null reads as unknown (never error,
//      failed or refused) and does not carry the `err` class, and false
//      vs. null render to genuinely different markup, not merged by
//      falsiness.
//   3. the submit handler's error split (fix round 1): a non-ok response
//      with a parseable body is a genuine accepted:false refusal, but a
//      rejected fetch() or an unreadable response body is NOT — the request
//      may already have been accepted server-side with only the reply lost,
//      so that must render as `unknown`, never `err`, and must NOT spend the
//      request_id: a retry has to reuse it so the server's own idempotency
//      (RunController.submit, keyed on request_id) absorbs it instead of a
//      second launch being risked. A settled outcome (true/false), by
//      contrast, must free the id so the next submission is a fresh attempt.
//
// Usage: node run_console_harness.js <path-to-index.html>
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Document, dispatch} = require(path.join(__dirname, 'dom.js'));

const HTML_PATH = process.argv[2];
if (!HTML_PATH) {
  console.error('usage: node run_console_harness.js <index.html>');
  process.exit(2);
}

let caseFailures = 0;
let asyncErrors = 0;
let currentCase = '(startup)';
let summaryPrinted = false;
const failed = () => caseFailures + asyncErrors;

process.on('unhandledRejection', reason => {
  asyncErrors++;
  process.exitCode = 1;
  console.error(`\nUNHANDLED REJECTION during ${currentCase}:`,
    (reason && reason.stack) || reason);
});
process.on('uncaughtException', error => {
  asyncErrors++;
  process.exitCode = 1;
  console.error(`\nUNCAUGHT EXCEPTION during ${currentCase}:`,
    (error && error.stack) || error);
});
process.on('exit', () => {
  if (!summaryPrinted) {
    process.exitCode = 1;
    console.error(`\nRUN DID NOT FINISH: exited during ${currentCase} without `
      + 'reaching the summary.');
  }
});

// ---- page loading -----------------------------------------------------------

const html = fs.readFileSync(HTML_PATH, 'utf8');

function between(source, openRe, closeTag, what) {
  const open = openRe.exec(source);
  const close = source.lastIndexOf(closeTag);
  if (!open || close === -1 || close < open.index) {
    throw new Error(`${HTML_PATH}: could not find <${what}> … ${closeTag}`);
  }
  return source.slice(open.index + open[0].length, close);
}

const BODY_HTML = between(html, /<body[^>]*>/i, '</body>', 'body');
if (BODY_HTML.split('<script').length !== 2) {
  throw new Error(`${HTML_PATH}: expected exactly one <script> in <body>`);
}
const PAGE_SCRIPT = between(BODY_HTML, /<script[^>]*>/i, '</script>', 'script');

// ---- fixtures ---------------------------------------------------------------

const resp = (status, body) => ({
  status, ok: status >= 200 && status < 300,
  json: () => Promise.resolve(body),
});
const ok = body => resp(200, body);

const DEFAULT_RECEIPT = {
  request_id: 'fixture-request-id', run_id: '01ZZZ',
  accepted: true, reason: null,
};

// refresh() (index.html:651-669) fans out to exactly these eight endpoints on
// load; the run console lives at the top level and needs none of the
// detail()-only routes (onboarding, governance, product-proposals, runs).
function defaultRoutes(submitRoute) {
  return [
    [u => u.startsWith('/api/overview'), () => ok({projects: []})],
    [u => u.startsWith('/api/errors'), () => ok([])],
    [u => u.startsWith('/api/models'), () => ok([])],
    [u => u.startsWith('/api/contracts'), () => ok([])],
    [u => u.startsWith('/api/roadmap/summary'), () => ok({projects: []})],
    [u => u.startsWith('/api/roadmap'), () => ok({roadmaps: [], items: []})],
    [u => u.startsWith('/api/sync'), () => ok({
      fetch_in_flight: false,
      report: {top_line: 'ok', top_reason: null, proposals: [], hosts: []},
    })],
    [u => u.startsWith('/api/benchmarks'), () => ok({
      fetch_in_flight: false,
      report: {status: 'unconfigured', url: null, fetched_at: null,
        error: null, benchmarks: [], leaderboards: {}},
    })],
    [u => u.startsWith('/api/actions/session'), () => ok({token: 'test-token'})],
    [u => u === '/api/runs/submit', submitRoute || (() => ok(DEFAULT_RECEIPT))],
  ];
}

const drain = async (turns = 5) => {
  for (let i = 0; i < turns; i++) await new Promise(r => setTimeout(r, 0));
};

// A controllable virtual timer for setInterval/clearInterval, standing in
// for the no-op stub every sibling harness uses (runs_harness.js:175,
// governance_harness.js:176, benchmarks_harness.js:191). Those pages never
// assert on their own polling, so stubbing it away is harmless there; this
// harness's whole point is TASK-3's stop rule, and a no-op stub would make
// BOTH "stopped" assertions pass vacuously (nothing was ever scheduled)
// while only the "still polling" assertion could fail — the shape of a
// suite that looks green while proving nothing. So: real interval
// bookkeeping (id -> {cb, period, due}) over a virtual clock that only
// advances when a test calls tick(); clearInterval genuinely removes the
// entry, so a cleared interval provably stops firing rather than the test
// merely hoping so.
function makeVirtualClock() {
  let now = 0;
  let nextId = 1;
  const intervals = new Map();
  return {
    setInterval(cb, period) {
      const id = nextId++;
      intervals.set(id, {cb, period, due: now + period});
      return id;
    },
    clearInterval(id) { intervals.delete(id); },
    // Advances the clock by `ms`, firing every interval whose due time
    // falls at or before the new time, in due-time order, including an
    // interval that becomes due more than once inside a large `ms`. Each
    // firing reschedules its own next due time BEFORE invoking the
    // callback, so a callback that calls clearInterval on itself (or on
    // another interval) is observed correctly on the next loop pass.
    async tick(ms) {
      const target = now + ms;
      for (;;) {
        let dueId = null, dueAt = null;
        for (const [id, iv] of intervals) {
          if (iv.due <= target && (dueAt === null || iv.due < dueAt)) {
            dueId = id; dueAt = iv.due;
          }
        }
        if (dueId === null) break;
        const iv = intervals.get(dueId);
        now = iv.due;
        iv.due += iv.period;
        iv.cb();
        await drain(10);
      }
      now = target;
    },
  };
}

async function boot(submitRoute) {
  const document = new Document(BODY_HTML);
  const calls = [];
  const routes = defaultRoutes(submitRoute);
  const clock = makeVirtualClock();
  const ctx = {
    document, console, URL,
    setTimeout, clearTimeout,
    setInterval: clock.setInterval,
    clearInterval: clock.clearInterval,
    fetch: (url, opts) => {
      const u = String(url);
      calls.push({url: u, opts: opts || {}});
      for (const [test, make] of routes) if (test(u)) return Promise.resolve(make(u));
      return Promise.reject(new Error(`no fixture route for ${u}`));
    },
    window: {open: () => {}},
  };
  vm.createContext(ctx);
  vm.runInContext(PAGE_SCRIPT, ctx);
  await drain();
  // The page's own `setInterval(refresh, 10000)` (index.html:2663) registers
  // here too, on the SAME virtual clock as any per-run polling this harness
  // starts. A tick large enough to cross 10000ms will fire it — that is
  // correct behaviour, not a test failure; tests below stay under that
  // threshold specifically so it does not confound the polling assertions.
  return {ctx, document, calls, routes, clock};
}

/** Advances page's virtual clock, firing any interval callbacks now due. */
async function tick(page, ms) { await page.clock.tick(ms); }

/** Boots a fresh page and hands it to `fn` — mirrors the plan's pseudocode
 * naming; equivalent to `await fn(await boot())`. */
async function withPage(fn, submitRoute) { await fn(await boot(submitRoute)); }

function el(page, selector) {
  const node = page.document.querySelector(selector);
  if (!node) throw new Error(`${selector} is not in the page markup`);
  return node;
}
function fill(page, selector, value) { el(page, selector).value = value; }
function text(page, selector) { return el(page, selector).innerHTML; }
async function click(page, selector) {
  await Promise.all(dispatch(el(page, selector), 'click'));
  await drain();
}
/** Calls the page's own renderReceipt(), not a copy of it. `blocked` (fix
 * round 2) selects the accepted:null copy — server-declared (true) vs.
 * transport-level (false/omitted) — the same explicit flag the real
 * submit handler passes; unused by the accepted:true/false branches. */
function receiptHtml(page, receipt, blocked) {
  return vm.runInContext(
    `renderReceipt(${JSON.stringify(receipt)}, ${JSON.stringify(!!blocked)})`,
    page.ctx);
}
/** Fills the form with values that pass both the client and server checks. */
function fillValid(page) {
  fill(page, '#rc-repository', 'deployer');
  fill(page, '#rc-revision', 'a'.repeat(40));
  fill(page, '#rc-tasks', 'tasks.yaml');
  fill(page, '#rc-work-id', 'todo://deployer/entrypoint-token-boundary-match');
}
/** Calls the page's own renderRunView(), not a copy of it. */
function runViewHtml(page, view) {
  return vm.runInContext(`renderRunView(${JSON.stringify(view)})`, page.ctx);
}
/** Registers a fixture for GET /api/runs/{requestId} and opens it exactly
 * the way an operator does: type the request_id, click #rc-open. */
async function openView(page, requestId, view) {
  const url = `/api/runs/${requestId}`;
  page.routes.push([u => u === url, () => ok(view)]);
  fill(page, '#rc-request-id', requestId);
  await click(page, '#rc-open');
}
/** Like el(), but returns null instead of throwing — for asserting a
 * control is genuinely absent from the markup, not merely unfound by a
 * typo'd selector. */
function maybeEl(page, selector) { return page.document.querySelector(selector); }
/** Calls the page's own renderResolution(), not a copy of it. */
function resolutionHtml(page, res) {
  return vm.runInContext(`renderResolution(${JSON.stringify(res)})`, page.ctx);
}
/** Calls the page's own renderVerbOutcome(), not a copy of it. */
function verbOutcomeHtml(page, outcome) {
  return vm.runInContext(`renderVerbOutcome(${JSON.stringify(outcome)})`, page.ctx);
}
/** Opens an ordinary (non-launch_unknown) materialized+running view — the
 * home of Task 5's status/retry/approve controls — exactly as an operator
 * does: type the request_id, click #rc-open. */
async function openMaterialized(page, requestId = 'req-materialized') {
  await openView(page, requestId, {record: {state: 'materialized', run_id: '01AAA'},
    run: {status: 'running'}, warnings: []});
}
/** Sets a <select>'s value and fires 'change', exactly as an operator
 * choosing an option does. dom.js models a control as a bare `.value`
 * property with no `<option>` children/selectedIndex/options collection
 * (see the harness's TASK-4 section below), so this — not simulating a
 * click on some option node — is the only way a test here can drive a
 * <select>; page code must never read `.options`/`.selectedIndex` for the
 * same reason: this harness cannot exercise that branch, so a bug behind
 * it would pass silently. */
async function select(page, selector, value) {
  fill(page, selector, value);
  await Promise.all(dispatch(el(page, selector), 'change'));
  await drain();
}
/** Opens a fresh launch_unknown view and drives #rc-resolve to an
 * ambiguous (2+ candidate) answer, exactly as an operator would: open,
 * click "Attempt resolution", land on the picker. */
async function openAmbiguous(page, requestId, candidates) {
  await openView(page, requestId,
    {record: {state: 'launch_unknown', run_id: null}, run: null, warnings: []});
  page.routes.push([u => u === `/api/runs/${requestId}/resolve`, () => ok({
    request_id: requestId, adopted_run_id: null, candidates,
    reason: `${candidates.length} new runs correlate equally well; `
      + 'attribution is ambiguous in principle',
  })]);
  await click(page, '#rc-resolve');
}

// ---- case runner -----------------------------------------------------------

const cases = [];
const testCase = (name, fn) => cases.push({name, fn});
function check(cond, message) {
  if (!cond) {
    caseFailures++;
    console.log(`  [FAIL] ${message}`);
  }
}

// ---- cases -----------------------------------------------------------------

testCase('submitting the form posts to /api/runs/submit with the token', async () => {
  const page = await boot();
  fill(page, '#rc-repository', 'deployer');
  fill(page, '#rc-revision', 'a'.repeat(40));
  fill(page, '#rc-tasks', 'tasks.yaml');
  fill(page, '#rc-work-id', 'todo://deployer/entrypoint-token-boundary-match');
  await click(page, '#rc-submit');
  const call = page.calls.find(c => c.url === '/api/runs/submit');
  check(!!call, 'submit posted to /api/runs/submit');
  if (!call) return;
  check(call.opts.headers['X-Action-Token'] === 'test-token',
    `the action token is sent (got: ${call.opts.headers['X-Action-Token']})`);
  check(call.opts.headers['Content-Type'] === 'application/json',
    'JSON content-type is sent');
  const body = JSON.parse(call.opts.body);
  check(body.repository === 'deployer' && body.tasks === 'tasks.yaml',
    `body carries the form (got: ${JSON.stringify(body)})`);
  check(body.revision === 'a'.repeat(40), 'body carries revision');
  check(body.work_id === 'todo://deployer/entrypoint-token-boundary-match',
    'body carries work_id');
});

testCase('accepted:true renders as started and shows the run id', async () => {
  const page = await boot();
  const html = receiptHtml(page,
    {request_id: 'r1', run_id: '01AAA', accepted: true, reason: null});
  check(html.includes('01AAA'), `run_id is on screen (got: ${html})`);
  check(!/error|fail/i.test(html), 'no error/fail wording for accepted:true');
});

testCase('accepted:false renders as a refusal WITH the reason', async () => {
  const page = await boot();
  const html = receiptHtml(page,
    {request_id: 'r2', run_id: null, accepted: false, reason: 'busy: deployer'});
  check(html.includes('busy: deployer'), `the reason is shown (got: ${html})`);
});

testCase('accepted:null is unknown, never error/failed/refused (boundary B2)',
  async () => {
    const page = await boot();
    const html = receiptHtml(page, {
      request_id: 'r3', run_id: null, accepted: null,
      reason: 'launch_unknown: no run appeared within 120s',
    });
    check(!/error|failed|refused/i.test(html),
      `no error/failed/refused wording (got: ${html})`);
    check(/unknown/i.test(html), 'reads as unknown');
    check(!html.includes('rc-result err'),
      'accepted:null must not carry the err class');
  });

testCase('false and null are genuinely distinguished, not merged by falsiness',
  async () => {
    const page = await boot();
    const asFalse = receiptHtml(page,
      {request_id: 'r4', run_id: null, accepted: false, reason: 'refused'});
    const asNull = receiptHtml(page,
      {request_id: 'r5', run_id: null, accepted: null, reason: 'unknown'});
    check(asFalse !== asNull, 'false and null must not render identically');
    check(asFalse.includes('rc-result err'), 'false renders with the err class');
    check(asNull.includes('rc-result unknown'),
      'null renders with its own unknown class');
  });

// -- fix round 1: transport failure vs. a definitive refusal -----------------
// A non-ok response with a parseable body is a fact: the server received the
// request and refused it (accepted:false is honest). A rejected fetch() or
// an unreadable response body is NOT a fact either way — the request may
// never have arrived, or it may have arrived, been accepted, and produced a
// run, with only the reply lost. That second case must render as `unknown`,
// never `err`, and must not spend the request_id: a retry has to reuse it so
// it lands on the server's idempotency record (RunController.submit)
// instead of risking a second launch.

testCase('a rejected fetch() renders as unknown, never as a refusal', async () => {
  const page = await boot(() => Promise.reject(new Error('network down')));
  fillValid(page);
  await click(page, '#rc-submit');
  const html = el(page, '#rc-receipt').innerHTML;
  check(!html.includes('rc-result err'),
    `a transport failure must not carry the err class (got: ${html})`);
  check(html.includes('rc-result unknown'),
    'a transport failure renders with the unknown class');
  check(!/refused/i.test(html), 'a transport failure is not worded as a refusal');
});

testCase('an unreadable response body renders as unknown, never as a refusal',
  async () => {
    const page = await boot(() => ({
      status: 200, ok: true,
      json: () => Promise.reject(new Error('unexpected token')),
    }));
    fillValid(page);
    await click(page, '#rc-submit');
    const html = el(page, '#rc-receipt').innerHTML;
    check(!html.includes('rc-result err'),
      `an unreadable body must not carry the err class (got: ${html})`);
    check(html.includes('rc-result unknown'),
      'an unreadable body renders with the unknown class');
  });

testCase('a non-ok response with a parsed body still renders as a refusal',
  async () => {
    const page = await boot(() => resp(403, {detail: 'bad or missing action token'}));
    fillValid(page);
    await click(page, '#rc-submit');
    const html = el(page, '#rc-receipt').innerHTML;
    check(html.includes('rc-result err'),
      `a definitive server refusal keeps the err class (got: ${html})`);
    check(html.includes('bad or missing action token'),
      'the refusal reason from the server is shown');
  });

testCase('a retry after a transport failure reuses the same request_id',
  async () => {
    const page = await boot(() => Promise.reject(new Error('network down')));
    fillValid(page);
    await click(page, '#rc-submit');
    await click(page, '#rc-submit');
    const submits = page.calls.filter(c => c.url === '/api/runs/submit');
    check(submits.length === 2, `two attempts were made (got ${submits.length})`);
    if (submits.length !== 2) return;
    const first = JSON.parse(submits[0].opts.body).request_id;
    const second = JSON.parse(submits[1].opts.body).request_id;
    check(first === second, 'a retry after an unresolved attempt must reuse '
      + `request_id (got ${first} vs ${second})`);
  });

testCase('a new submission after a settled outcome mints a fresh request_id',
  async () => {
    const page = await boot(() => ok(
      {request_id: 'server-echo', run_id: '01BBB', accepted: true, reason: null}));
    fillValid(page);
    await click(page, '#rc-submit');
    await click(page, '#rc-submit');
    const submits = page.calls.filter(c => c.url === '/api/runs/submit');
    check(submits.length === 2, `two attempts were made (got ${submits.length})`);
    if (submits.length !== 2) return;
    const first = JSON.parse(submits[0].opts.body).request_id;
    const second = JSON.parse(submits[1].opts.body).request_id;
    check(first !== second, 'a settled outcome must not pin the next '
      + 'submission to the same request_id');
  });

// -- TASK-2: client-side validation that mirrors the server's refusals ------
// validateRunRequest() only saves a round trip on the two mistakes an
// operator makes constantly (dispatcher/core/run_request.py: revision must
// be 40 hex chars, tasks must be repo-relative with no '..'). It is not a
// safety layer, so the third case below matters as much as the first two:
// an over-eager check that refuses a good request is worse than no check.

testCase('a bad revision never reaches the wire, and says what the server says',
  async () => {
    const page = await boot();
    fill(page, '#rc-repository', 'deployer');
    fill(page, '#rc-revision', 'HEAD');
    fill(page, '#rc-tasks', 'tasks.yaml');
    await click(page, '#rc-submit');
    check(!page.calls.some(c => c.url === '/api/runs/submit'), 'not sent');
    check(/40-hex/.test(text(page, '#rc-receipt')),
      `says 40-hex, as the server does (got: ${text(page, '#rc-receipt')})`);
  });

testCase('".." in tasks is caught client-side', async () => {
  const page = await boot();
  fillValid(page);
  fill(page, '#rc-tasks', '../outside.yaml');
  await click(page, '#rc-submit');
  check(!page.calls.some(c => c.url === '/api/runs/submit'), 'not sent');
});

testCase('a valid-looking request IS sent — the client must not invent refusals',
  async () => {
    const page = await boot();
    fillValid(page);
    await click(page, '#rc-submit');
    check(page.calls.some(c => c.url === '/api/runs/submit'), 'sent');
  });

testCase('a client-side rejection leaves rcPendingRequestId untouched',
  async () => {
    const page = await boot();
    fill(page, '#rc-repository', 'deployer');
    fill(page, '#rc-revision', 'HEAD');
    fill(page, '#rc-tasks', 'tasks.yaml');
    await click(page, '#rc-submit');
    check(!page.calls.some(c => c.url === '/api/runs/submit'), 'not sent');
    const pending = vm.runInContext('rcPendingRequestId', page.ctx);
    check(pending === null,
      `nothing was sent, so no request_id should be pinned (got: ${pending})`);
  });

// -- TASK-3: the run view — dispatcher's record joined to maestro's run ----
// record.state (dispatcher's launch machine) and run.status (maestro's own
// classification) are different axes (spec §3.2) and must show as two
// facts, never merged into one badge — the cases below assert both are on
// screen independently, that a missing run reads as "no run yet" rather
// than borrowing "running"'s wording, and that an UNREADABLE run directory
// (run:null WITH a warning) is visibly different from an ABSENT one
// (run:null, no warning): the warning must reach the screen.

testCase('renderRunView shows both axes, not conflated into one badge',
  async () => {
    const page = await boot();
    const html = runViewHtml(page, {
      record: {state: 'materialized', run_id: '01AAA'},
      run: {run_id: '01AAA', status: 'running'}, warnings: [],
    });
    check(/materialized/i.test(html), `record.state is shown (got: ${html})`);
    check(/running/i.test(html), `run.status is shown (got: ${html})`);
  });

testCase('no run yet: the record still shows, the run half says so plainly',
  async () => {
    const page = await boot();
    const html = runViewHtml(page, {
      record: {state: 'launching', run_id: null}, run: null, warnings: [],
    });
    check(/launching/i.test(html), `record.state is shown (got: ${html})`);
    check(!/running/i.test(html),
      `no stray "running" wording with no run (got: ${html})`);
  });

testCase('unreadable is NOT absent: the collector warning reaches the screen',
  async () => {
    const page = await boot();
    const html = runViewHtml(page, {
      record: {state: 'materialized', run_id: '01AAA'}, run: null,
      warnings: ['runs enumeration: cannot list /x: Permission denied'],
    });
    check(/Permission denied/.test(html), `the warning is shown (got: ${html})`);
  });

// -- the stop rule: poll while anything is unsettled on EITHER axis --------
// An earlier plan draft said "stop on materialized" in one place and "stop
// on a terminal state" in another; neither alone is right. materialized is
// where dispatcher's machine finishes and maestro's run begins — stopping
// there would freeze the run half of the display on whatever it said the
// moment the directory appeared (usually "running"), forever. So: stop only
// once BOTH axes are settled.
//
// Every test below ticks by exactly 5000ms — under BOTH the page's own
// global `setInterval(refresh, 10000)` (index.html:2663) and this view's
// own 5000ms poll period (index.html rcPollView). That is a real coupling,
// not an arbitrary number: it keeps the global refresh's eight fetches out
// of `page.calls` while still crossing this view's own period exactly
// once. If either period changes — the view's to >=10000, or a tick here
// grows to >=10000 — these counts start measuring the wrong interval and
// the tests would need re-deriving, not just re-running.

testCase('polling stops once both axes are settled (terminal record, '
  + 'completed run)', async () => {
  await withPage(async page => {
    await openView(page, 'req-1', {record: {state: 'terminal', run_id: '01AAA'},
      run: {status: 'completed'}, warnings: []});
    const before = page.calls.length;
    await tick(page, 5000);
    check(page.calls.length === before,
      `no polling after terminal (before=${before}, after=${page.calls.length})`);
  });
});

testCase('a materialized record with a LIVE run keeps polling — the record '
  + 'is done, the run is not', async () => {
  await withPage(async page => {
    await openView(page, 'req-2', {record: {state: 'materialized', run_id: '01AAA'},
      run: {status: 'running'}, warnings: []});
    const before = page.calls.length;
    await tick(page, 5000);
    check(page.calls.length > before, 'still polling while the run is live '
      + `(before=${before}, after=${page.calls.length})`);
  });
});

testCase('launch_unknown is settled until an operator acts — do not poll '
  + 'into it', async () => {
  await withPage(async page => {
    await openView(page, 'req-3', {record: {state: 'launch_unknown', run_id: null},
      run: null, warnings: []});
    const before = page.calls.length;
    await tick(page, 5000);
    check(page.calls.length === before, 'no polling while awaiting the '
      + `operator (before=${before}, after=${page.calls.length})`);
  });
});

testCase('a materialized record with a gone/unreadable run directory (run: '
  + 'null) keeps polling rather than freezing silently', async () => {
  await withPage(async page => {
    await openView(page, 'req-4', {record: {state: 'materialized', run_id: '01AAA'},
      run: null, warnings: ['runs enumeration: cannot list /x: Permission denied']});
    const before = page.calls.length;
    await tick(page, 5000);
    check(page.calls.length > before, 'still polling with run:null under a '
      + `materialized record (before=${before}, after=${page.calls.length})`);
  });
});

// -- fix round 1: a stale in-flight poll must not clobber a newer view -----
// The operator has req-1 open and polling; an interval-fired poll for
// req-1 is in flight; before it lands, they open req-2. That poll's
// response — here a definitive 404 — must be dropped: it must not
// overwrite req-2's rendered view and must not clear req-2's OWN timer.
// The staleness check that makes this true has to run before EVERY branch
// that touches the DOM or the timer (ok, non-ok, and the network-failure
// catch), not only after a successful parse — the bug this regresses was
// exactly that: the guard existed, just not on the non-ok path.

testCase('a stale in-flight poll (non-ok response) does not clobber a '
  + "newer view or clear the newer view's own timer", async () => {
  await withPage(async page => {
    await openView(page, 'req-1', {record: {state: 'materialized', run_id: '01AAA'},
      run: {status: 'running'}, warnings: []});

    // From here on, req-1's fetches hang until resolveStale() is called —
    // standing in for "the interval-fired poll is still in flight".
    let resolveStale;
    const stalePromise = new Promise(resolve => { resolveStale = resolve; });
    page.routes.unshift([u => u === '/api/runs/req-1', () => stalePromise]);

    // Fires req-1's own poll interval; it calls fetch() and now sits
    // awaiting `stalePromise` — genuinely in flight, not yet settled.
    await tick(page, 5000);

    // The operator opens req-2 (also unsettled) while req-1's poll hangs.
    // This clears req-1's timer, renders req-2, and starts req-2's OWN
    // timer.
    await openView(page, 'req-2', {record: {state: 'materialized', run_id: '01BBB'},
      run: {status: 'running'}, warnings: []});
    const viewAfterOpen = el(page, '#rc-run-view').innerHTML;

    // Now let req-1's stale poll land, as a definitive refusal.
    resolveStale(resp(404, {detail: 'no such request: req-1'}));
    await drain(10);

    check(el(page, '#rc-run-view').innerHTML === viewAfterOpen,
      "a stale response for req-1 must not overwrite req-2's view");

    // req-2's own timer must still be alive: tick past its period and
    // confirm req-2 (not req-1) was polled again.
    const before = page.calls.filter(c => c.url === '/api/runs/req-2').length;
    await tick(page, 5000);
    const after = page.calls.filter(c => c.url === '/api/runs/req-2').length;
    check(after > before, "req-2's polling must survive the stale req-1 "
      + `response landing late (before=${before}, after=${after})`);
  });
});

// -- TASK-4: the launch_unknown resolution flow, and the only home of ------
// run-end (B1, B2) ------------------------------------------------------
//
// B1: run-end (`maestro run-end <run_id> --outcome ...`) ends a run that
// cannot observe itself ending. Rendered as a peer of ordinary controls it
// invites ending a healthy run with one click; it must appear ONLY inside
// this flow, and ONLY once correlation is genuinely ambiguous (2+
// candidates) — never for zero, never for exactly one (already resolved).
//
// B2: launch_unknown / accepted:null is a third state, not an error — the
// launch may have happened. Consequences asserted below: #rc-submit is
// blocked while it stands open, resolution is offered instead of an error
// box, and the copy never says error/failed.
//
// The three "absence" cases below are the easy ones to fake (a test that
// never actually renders the block passes vacuously) — each first proves
// the surrounding content DID render before trusting that run-end did not.

testCase('B1: run-end is nowhere near the normal controls (a settled, '
  + 'ordinary materialized+running view)', async () => {
  const page = await boot();
  const html = runViewHtml(page, {record: {state: 'materialized', run_id: '01AAA'},
    run: {status: 'running'}, warnings: []});
  // Prove the view actually rendered — otherwise "no run-end" would pass
  // whether or not renderRunView worked at all.
  check(/materialized/i.test(html), `record.state rendered (got: ${html})`);
  check(/running/i.test(html), `run.status rendered (got: ${html})`);
  check(!/run-end/i.test(html), `run-end must not appear here (got: ${html})`);
});

testCase('B2: launch_unknown blocks resubmission and offers resolution '
  + 'instead of an error', async () => {
  await withPage(async page => {
    await openView(page, 'req-1', {record: {state: 'launch_unknown', run_id: null},
      run: null, warnings: []});
    check(el(page, '#rc-submit').disabled === true, 'submit blocked while unknown');
    check(maybeEl(page, '#rc-resolve') !== null, 'resolution is offered');
    check(!/error|failed/i.test(text(page, '#rc-run-view')),
      `not worded as an error (got: ${text(page, '#rc-run-view')})`);
  });
});

testCase('opening an ordinary (non-unknown) view leaves #rc-submit enabled',
  async () => {
    await withPage(async page => {
      await openView(page, 'req-ok', {record: {state: 'materialized', run_id: '01AAA'},
        run: {status: 'running'}, warnings: []});
      check(el(page, '#rc-submit').disabled === false,
        'submit stays enabled for an ordinary view');
    });
  });

// -- fix round 1: B2 on the DIRECT path (no #rc-open anywhere) -------------
// The gap review found: the view-driven gate above only ever fires if the
// operator navigates to the request via #rc-open. The path that actually
// happens — submit, see "unknown", click submit again — went through
// unblocked, because the submit handler's own `finally` re-enabled
// unconditionally.
//
// Fix round 1a blocked on EVERY unresolved attempt (rcPendingRequestId).
// That was too broad and was narrowed in fix round 1b: `accepted: null`
// arises two different ways, and only the SERVER-declared one (a 200
// response whose own body says accepted:null — a record was reserved and
// launched, spec §5.2.1) should block; a TRANSPORT-level null (the fetch
// itself rejected, or the body was unreadable — no response reached us at
// all) must NOT block, because the pinned request_id's retry is the only
// safe recovery from it, and blocking it would make that pinning dead
// code. `rcSubmitBlocked` names the distinction directly; the tests below
// exercise both sides of it.

testCase('fix round 1 — B2 direct path: a SERVER-declared accepted:null '
  + 'blocks #rc-submit, no #rc-open anywhere', async () => {
  const page = await boot(() => ok({request_id: 'server-echo', run_id: null,
    accepted: null, reason: 'launch_unknown: no run appeared within 120s'}));
  fillValid(page);
  await click(page, '#rc-submit');
  check(el(page, '#rc-receipt').innerHTML.includes('rc-result unknown'),
    'sanity: this is genuinely the accepted:null path');
  check(el(page, '#rc-submit').disabled === true,
    'submit is blocked while the SERVER says this is unresolved — the '
    + 'direct path, no view was ever opened');
});

testCase('fix round 1 — B2 direct path: submit → accepted:true leaves '
  + '#rc-submit enabled', async () => {
  const page = await boot(() => ok(
    {request_id: 'server-echo', run_id: '01ZZZ', accepted: true, reason: null}));
  fillValid(page);
  await click(page, '#rc-submit');
  check(el(page, '#rc-submit').disabled === false,
    'a settled outcome (accepted:true) leaves submit enabled');
});

// Fix round 2: the two accepted:null origins get DIFFERENT copy, and the
// re-review found round 1's version said the same (server-declared)
// sentence for both — wrong for the transport case, whose button beside
// it is deliberately enabled. Each test below asserts the actual sentence
// each origin gets, not just the shared "unknown" class name — the two
// receipts differ ONLY in their words, so the class name alone proves
// nothing about which copy rendered.

testCase('fix round 2: the SERVER-declared receipt (blocked) names the '
  + 'request_id and says to resolve it — the only case where that is true',
  async () => {
    const page = await boot();
    const html = receiptHtml(page, {request_id: 'rc-abc123', run_id: null,
      accepted: null, reason: 'launch_unknown: no run appeared within 120s'},
      true);
    check(html.includes('rc-abc123'),
      `the request_id is on screen so it can be opened (got: ${html})`);
    check(/resolve/i.test(html),
      `tells the operator to resolve it (got: ${html})`);
    check(/rc-open|open run/i.test(html),
      `points at the way out (got: ${html})`);
  });

testCase('fix round 2: the TRANSPORT-level receipt (not blocked) says '
  + 'retrying is safe, never "resolve", and does not point at #rc-open — '
  + 'a request that never reached the server 404s there', async () => {
    const page = await boot();
    const html = receiptHtml(page, {request_id: 'rc-xyz789', run_id: null,
      accepted: null, reason: 'no response reached the browser: network down'},
      false);
    check(html.includes('rc-xyz789'),
      `the request_id is named, since it will be reused (got: ${html})`);
    check(/safe/i.test(html), `says retrying is safe (got: ${html})`);
    check(!/resolve/i.test(html), 'must NOT say to resolve — there is '
      + `nothing to resolve for a request that never arrived (got: ${html})`);
    check(!/#rc-open|open run/i.test(html), 'must NOT point at #rc-open — '
      + `that leads to a 404 for a request that never reached the server `
      + `(got: ${html})`);
  });

// Fix round 1b: the distinction itself, pinned directly — this is the
// whole point of the narrowing, and nothing else states it. Both origins
// pin the id (the same one that was actually sent, not just "some id");
// only the server-declared one blocks.

testCase('fix round 1b: a transport-level null does NOT block, a '
  + 'server-declared null DOES — and both pin the id that was sent',
  async () => {
    // Transport-level: fetch() itself rejects. No response reached us at
    // all, so the pinned id's retry is the recovery — must not block.
    const transportPage = await boot(() => Promise.reject(new Error('network down')));
    fillValid(transportPage);
    await click(transportPage, '#rc-submit');
    check(el(transportPage, '#rc-submit').disabled === false,
      'a transport-level null (no response reached us) does not block — '
      + 'the pinned id is the recovery, not /resolve');
    const transportSent = JSON.parse(
      transportPage.calls.find(c => c.url === '/api/runs/submit').opts.body
    ).request_id;
    const transportPinned = vm.runInContext('rcPendingRequestId', transportPage.ctx);
    check(transportPinned === transportSent, 'the id pinned afterward is '
      + `the SAME one that was sent (sent ${transportSent}, pinned ${transportPinned})`);

    // Server-declared: the response arrived, parsed fine, and its OWN
    // body says accepted:null — a record exists; resubmitting is useless,
    // /resolve is the real recovery.
    const serverPage = await boot(() => ok({request_id: 'server-echo',
      run_id: null, accepted: null,
      reason: 'launch_unknown: no run appeared within 120s'}));
    fillValid(serverPage);
    await click(serverPage, '#rc-submit');
    check(el(serverPage, '#rc-submit').disabled === true,
      'a server-declared accepted:null DOES block — resolution is the '
      + 'only useful next step, not a resubmit');
    const serverSent = JSON.parse(
      serverPage.calls.find(c => c.url === '/api/runs/submit').opts.body
    ).request_id;
    const serverPinned = vm.runInContext('rcPendingRequestId', serverPage.ctx);
    check(serverPinned === serverSent, 'pinned here too, and it is the '
      + `SAME id that was sent (sent ${serverSent}, pinned ${serverPinned})`);
  });

testCase('fix round 1: resolving the SAME request a direct submit is '
  + 'blocked on unblocks #rc-submit', async () => {
  await withPage(async page => {
    // Direct submit, SERVER-declared accepted:null: unresolved, submit
    // blocked, no #rc-open involved yet. Grab the request_id the page
    // itself minted.
    fillValid(page);
    await click(page, '#rc-submit');
    check(el(page, '#rc-submit').disabled === true, 'blocked after the '
      + 'server-declared unresolved direct submit (sanity)');
    const pendingId = vm.runInContext('rcPendingRequestId', page.ctx);
    check(typeof pendingId === 'string' && pendingId.length > 0,
      `a request_id was minted and pinned (got: ${pendingId})`);

    // The operator takes the way out: opens that SAME request_id and
    // resolves it as an unambiguous adoption.
    page.routes.push([u => u === `/api/runs/${pendingId}`, () => ok(
      {record: {state: 'launch_unknown', run_id: null}, run: null, warnings: []})]);
    fill(page, '#rc-request-id', pendingId);
    await click(page, '#rc-open');
    page.routes.unshift([u => u === `/api/runs/${pendingId}`, () => ok(
      {record: {state: 'materialized', run_id: '01LATE'},
       run: {status: 'running'}, warnings: []})]);
    page.routes.push([u => u === `/api/runs/${pendingId}/resolve`, () => ok({
      request_id: pendingId, adopted_run_id: '01LATE', candidates: ['01LATE'],
      reason: 'exactly one new run correlated; adopted',
    })]);
    await click(page, '#rc-resolve');

    check(el(page, '#rc-submit').disabled === false, 'resolving the SAME '
      + 'request the direct submit was blocked on unblocks #rc-submit — '
      + "there is no longer anything left unresolved");
  }, () => ok({request_id: 'server-echo', run_id: null, accepted: null,
    reason: 'launch_unknown: no run appeared within 120s'}));
});

// Fix round 2, re-review Minor 1: the clearing guard reads
// `if (requestId === rcPendingRequestId)` — nothing pinned that resolving
// a DIFFERENT request_id leaves rcSubmitBlocked (and rcPendingRequestId)
// alone, which is exactly the sort of scoping a later refactor drops
// silently without a test naming it.

testCase('fix round 2: resolving a DIFFERENT request_id leaves the '
  + "direct submit's block (and its pinned id) alone", async () => {
  await withPage(async page => {
    // Direct submit of request A: server-declared, blocks #rc-submit.
    fillValid(page);
    await click(page, '#rc-submit');
    check(el(page, '#rc-submit').disabled === true,
      'blocked by request A (sanity)');
    const pinnedA = vm.runInContext('rcPendingRequestId', page.ctx);
    check(typeof pinnedA === 'string' && pinnedA.length > 0,
      `A is pinned (got: ${pinnedA})`);

    // Open and fully resolve an UNRELATED request B — not A, and never
    // pinned by this tab's own submit.
    page.routes.push([u => u === '/api/runs/req-B', () => ok(
      {record: {state: 'launch_unknown', run_id: null}, run: null, warnings: []})]);
    fill(page, '#rc-request-id', 'req-B');
    await click(page, '#rc-open');
    page.routes.unshift([u => u === '/api/runs/req-B', () => ok(
      {record: {state: 'materialized', run_id: '01OTHER'},
       run: {status: 'running'}, warnings: []})]);
    page.routes.push([u => u === '/api/runs/req-B/resolve', () => ok({
      request_id: 'req-B', adopted_run_id: '01OTHER', candidates: ['01OTHER'],
      reason: 'exactly one new run correlated; adopted',
    })]);
    await click(page, '#rc-resolve');

    check(el(page, '#rc-submit').disabled === true, 'resolving B must NOT '
      + 'unblock submit — A is a different request and is still unresolved');
    const stillPinned = vm.runInContext('rcPendingRequestId', page.ctx);
    check(stillPinned === pinnedA, 'A is still pinned, untouched by '
      + `resolving the unrelated B (got: ${stillPinned})`);
  }, () => ok({request_id: 'server-echo', run_id: null, accepted: null,
    reason: 'launch_unknown: no run appeared within 120s'}));
});

// -- renderResolution: the pure renderer over the three shapes §5.2.1 can --
// answer with. Each case first proves the OTHER expected content rendered,
// so an absence assertion cannot pass on a block that failed to render.

testCase('resolution: zero candidates says so plainly — no picker, no '
  + 'run-end', async () => {
  const page = await boot();
  const html = resolutionHtml(page, {request_id: 'r', adopted_run_id: null,
    candidates: [],
    reason: 'no new run to correlate; the launch may never have started'});
  check(/no new run/i.test(html),
    `states there is nothing to end, in the server's words (got: ${html})`);
  check(!/<select/i.test(html), `no empty picker either (got: ${html})`);
  check(!/run-end/i.test(html), `no run-end with nothing to end (got: ${html})`);
});

testCase('resolution: exactly one candidate is adopted and named — no '
  + 'run-end (already resolved)', async () => {
  const page = await boot();
  const html = resolutionHtml(page, {request_id: 'r', adopted_run_id: '01LATE',
    candidates: ['01LATE'], reason: 'exactly one new run correlated; adopted'});
  check(html.includes('01LATE'), `the adopted run is named (got: ${html})`);
  check(!/run-end/i.test(html),
    `already resolved automatically, nothing to end (got: ${html})`);
});

testCase('resolution: two or more candidates list BOTH and offer run-end '
  + '— the only place it lives', async () => {
  const page = await boot();
  const html = resolutionHtml(page, {request_id: 'r', adopted_run_id: null,
    candidates: ['01AAA', '01BBB'], reason: 'ambiguous'});
  check(html.includes('01AAA') && html.includes('01BBB'),
    `both candidates are listed (got: ${html})`);
  check(/run-end/i.test(html), `run-end is offered here (got: ${html})`);
});

// -- the wiring: #rc-resolve and #rc-end actually reach /resolve ----------

testCase('#rc-resolve posts {} to /resolve, with the action token', async () => {
  await withPage(async page => {
    await openView(page, 'req-5', {record: {state: 'launch_unknown', run_id: null},
      run: null, warnings: []});
    page.routes.push([u => u === '/api/runs/req-5/resolve', () => ok({
      request_id: 'req-5', adopted_run_id: null, candidates: [],
      reason: 'no new run to correlate',
    })]);
    await click(page, '#rc-resolve');
    const call = page.calls.find(c => c.url === '/api/runs/req-5/resolve');
    check(!!call, 'posted to /api/runs/req-5/resolve');
    if (!call) return;
    check(call.opts.headers['X-Action-Token'] === 'test-token', 'token sent');
    check(JSON.parse(call.opts.body ?? '{}').run_id === undefined
      && JSON.parse(call.opts.body ?? '{}').outcome === undefined,
      'body carries neither run_id nor outcome — this is the adopt-attempt shape');
  });
});

testCase('an adopted resolution (exactly one candidate) refreshes the view '
  + 'deliberately — nothing polls into a settled launch_unknown on its own',
  async () => {
    await withPage(async page => {
      await openView(page, 'req-6', {record: {state: 'launch_unknown', run_id: null},
        run: null, warnings: []});
      // The refetch after resolving must see the NEW state, or this test
      // could not tell "refreshed" from "coincidentally re-rendered".
      page.routes.unshift([u => u === '/api/runs/req-6', () => ok(
        {record: {state: 'materialized', run_id: '01LATE'},
         run: {status: 'running'}, warnings: []})]);
      page.routes.push([u => u === '/api/runs/req-6/resolve', () => ok({
        request_id: 'req-6', adopted_run_id: '01LATE', candidates: ['01LATE'],
        reason: 'exactly one new run correlated; adopted',
      })]);
      const before = page.calls.filter(c => c.url === '/api/runs/req-6').length;
      await click(page, '#rc-resolve');
      const after = page.calls.filter(c => c.url === '/api/runs/req-6').length;
      check(after > before,
        `adoption triggers a fresh GET of the view (before=${before}, after=${after})`);
      check(/materialized/i.test(el(page, '#rc-run-view').innerHTML),
        'the refreshed view shows the new (post-adoption) state');
      check(el(page, '#rc-submit').disabled === false,
        'submit unblocks once the view is no longer launch_unknown');
    });
  });

testCase('zero candidates does NOT auto-refresh — the view stays open for '
  + 'a retry', async () => {
  await withPage(async page => {
    await openView(page, 'req-7', {record: {state: 'launch_unknown', run_id: null},
      run: null, warnings: []});
    page.routes.push([u => u === '/api/runs/req-7/resolve', () => ok({
      request_id: 'req-7', adopted_run_id: null, candidates: [],
      reason: 'no new run to correlate',
    })]);
    const before = page.calls.filter(c => c.url === '/api/runs/req-7').length;
    await click(page, '#rc-resolve');
    const after = page.calls.filter(c => c.url === '/api/runs/req-7').length;
    check(after === before, 'no fresh GET fired — nothing was resolved '
      + `(before=${before}, after=${after})`);
    check(el(page, '#rc-resolve').disabled === false,
      '#rc-resolve is re-enabled so the operator can try again');
  });
});

// -- #rc-end: disabled until BOTH a run and an outcome are chosen ---------

testCase('the run-end button stays disabled until run_id AND outcome are '
  + 'both chosen', async () => {
  await withPage(async page => {
    await openAmbiguous(page, 'req-8', ['01AAA', '01BBB']);
    check(el(page, '#rc-end').disabled === true, 'disabled with neither chosen');
    await select(page, '#rc-end-run', '01AAA');
    check(el(page, '#rc-end').disabled === true,
      'still disabled with only a run chosen');
    await select(page, '#rc-end-outcome', 'cancelled');
    check(el(page, '#rc-end').disabled === false,
      'enabled once both a run and an outcome are chosen');
  });
});

testCase('#rc-end posts {run_id, outcome} together, and refreshes the '
  + 'view once the run is ended', async () => {
  await withPage(async page => {
    await openAmbiguous(page, 'req-9', ['01AAA', '01BBB']);
    await select(page, '#rc-end-run', '01AAA');
    await select(page, '#rc-end-outcome', 'cancelled');
    page.routes.unshift([u => u === '/api/runs/req-9', () => ok(
      {record: {state: 'terminal', run_id: '01AAA'},
       run: {status: 'cancelled'}, warnings: []})]);
    page.routes.push([u => u === '/api/runs/req-9/resolve', () => ok({
      request_id: 'req-9', adopted_run_id: '01AAA', candidates: ['01AAA', '01BBB'],
      reason: 'operator ended 01AAA as cancelled; lock released',
    })]);
    await click(page, '#rc-end');
    const call = page.calls.find(c => c.url === '/api/runs/req-9/resolve'
      && JSON.parse(c.opts.body).run_id === '01AAA');
    check(!!call, 'posted {run_id, outcome} to /resolve');
    if (call) {
      const body = JSON.parse(call.opts.body);
      check(body.run_id === '01AAA' && body.outcome === 'cancelled',
        `both fields sent together (got: ${JSON.stringify(body)})`);
    }
    check(/terminal/i.test(el(page, '#rc-run-view').innerHTML),
      'the view refreshed to show the run is now terminal');
    check(!/run-end/i.test(el(page, '#rc-run-view').innerHTML),
      'run-end is gone once the state is no longer launch_unknown');
  });
});

// -- TASK-5: the three Mode-1 verb controls (status/retry/approve), and ----
// the acceptance pass (spec §6) ---------------------------------------------
//
// B1, asserted here a second time (the first was Task 4's "run-end is
// nowhere near the normal controls"): now that renderRunView actually
// builds a non-empty #rc-view-actions for the ordinary case, that absence
// assertion is no longer trivially true — the case below proves the THREE
// controls positively rendered before trusting that run-end and stop did
// not. `stop` never had a home anywhere in this page (`maestro stop` ends
// the scheduler process, not a run), so its absence is checked directly
// too, not merely inferred from run-end's.

testCase('Task 5: exactly the three verb controls render for an ordinary '
  + 'view — never run-end, never stop (B1)', async () => {
  const page = await boot();
  const html = runViewHtml(page, {record: {state: 'materialized', run_id: '01AAA'},
    run: {status: 'running'}, warnings: []});
  // Positive: prove the controls actually rendered before trusting any
  // absence below.
  check(/id="rc-verb-status"/.test(html), `status control rendered (got: ${html})`);
  check(/id="rc-verb-retry"/.test(html), `retry control rendered (got: ${html})`);
  check(/id="rc-verb-approve"/.test(html), `approve control rendered (got: ${html})`);
  // Negative, now meaningful because the positive checks above proved the
  // block rendered.
  check(!/id="rc-end"/.test(html), `no run-end control here (got: ${html})`);
  check(!/run-end/i.test(html), `no run-end wording here (got: ${html})`);
  check(!/\bstop\b/i.test(html), `no stop control or wording anywhere (got: ${html})`);
});

testCase("Task 5: launch_unknown still shows only Task 4's resolution flow "
  + '— the verb controls do not leak into it (B1, the other direction)',
  async () => {
    const page = await boot();
    const html = runViewHtml(page, {record: {state: 'launch_unknown', run_id: null},
      run: null, warnings: []});
    check(/rc-resolve/.test(html), `resolution flow rendered (got: ${html})`);
    check(!/id="rc-verb-status"/.test(html)
      && !/id="rc-verb-retry"/.test(html)
      && !/id="rc-verb-approve"/.test(html),
      `verb controls must not appear while launch_unknown (got: ${html})`);
  });

// approve and retry each demand a task id before they will fire — neither
// is sent, and each says what is missing in the SERVER's own words
// (run_controller.py:666-681), so approve and retry read as two distinct
// rules rather than one vague "needs an id" message repeated twice.

testCase('Task 5: approve without a task id is not sent, and says what is '
  + "missing in the server's own words", async () => {
  await withPage(async page => {
    await openMaterialized(page, 'req-verb-approve');
    await click(page, '#rc-verb-approve');
    check(!page.calls.some(c => c.url.endsWith('/verb')),
      'approve without a task id must not reach the wire');
    const msg = text(page, '#rc-verb-result');
    check(/task_id/i.test(msg), `says what is missing (got: ${msg})`);
    check(/AWAITING_APPROVAL/.test(msg),
      `names the state approve releases, verbatim (got: ${msg})`);
    check(/retry instead/.test(msg),
      `points at retry for NEEDS_REVIEW, verbatim (got: ${msg})`);
  });
});

testCase('Task 5: retry without a task id is not sent, and says what is '
  + "missing in the server's own words", async () => {
  await withPage(async page => {
    await openMaterialized(page, 'req-verb-retry');
    await click(page, '#rc-verb-retry');
    check(!page.calls.some(c => c.url.endsWith('/verb')),
      'retry without a task id must not reach the wire');
    const msg = text(page, '#rc-verb-result');
    check(/task_id/i.test(msg), `says what is missing (got: ${msg})`);
    check(/FAILED/.test(msg) && /NEEDS_REVIEW/.test(msg),
      `names the states retry clears, verbatim (got: ${msg})`);
    check(/approve instead/.test(msg),
      `points at approve for AWAITING_APPROVAL, verbatim (got: ${msg})`);
  });
});

// This exact confusion (approve vs. retry) already cost a fix round on the
// server side — the labels below must distinguish the two verbs, not just
// carry two different names for the same idea.

testCase("Task 5: approve's and retry's labels distinguish which state "
  + 'each one clears — not just two different button names', async () => {
  const page = await boot();
  const html = runViewHtml(page, {record: {state: 'materialized', run_id: '01AAA'},
    run: {status: 'running'}, warnings: []});
  const approveLabel =
    /id="rc-verb-approve"[^>]*>([^<]*)</.exec(html)?.[1] ?? '';
  const retryLabel =
    /id="rc-verb-retry"[^>]*>([^<]*)</.exec(html)?.[1] ?? '';
  check(approveLabel !== '' && retryLabel !== '',
    `both labels were found (approve: '${approveLabel}', retry: '${retryLabel}')`);
  check(/AWAITING_APPROVAL/.test(approveLabel) && !/FAILED|NEEDS_REVIEW/.test(approveLabel),
    `approve's label names its own state only (got: ${approveLabel})`);
  check(/FAILED|NEEDS_REVIEW/.test(retryLabel) && !/AWAITING_APPROVAL/.test(retryLabel),
    `retry's label names its own state only (got: ${retryLabel})`);
});

// The wiring: each control posts to /verb with the action token, and the
// body carries exactly what that verb needs — no task_id key at all for
// status, {verb, task_id} for approve/retry.

testCase('Task 5: #rc-verb-status posts {verb:"status"} with no task_id key',
  async () => {
    await withPage(async page => {
      await openMaterialized(page, 'req-verb-2');
      page.routes.push([u => u === '/api/runs/req-verb-2/verb', () => ok(
        {verb: 'status', run_id: '01AAA', ok: true, stdout: 'running', stderr: ''})]);
      await click(page, '#rc-verb-status');
      const call = page.calls.find(c => c.url === '/api/runs/req-verb-2/verb');
      check(!!call, 'posted to /api/runs/req-verb-2/verb');
      if (!call) return;
      check(call.opts.headers['X-Action-Token'] === 'test-token', 'token sent');
      const body = JSON.parse(call.opts.body);
      check(body.verb === 'status', `verb is status (got: ${JSON.stringify(body)})`);
      check(!('task_id' in body), `no task_id key for status (got: ${JSON.stringify(body)})`);
    });
  });

testCase('Task 5: #rc-verb-approve posts {verb:"approve", task_id} once a '
  + 'task id is filled in', async () => {
  await withPage(async page => {
    await openMaterialized(page, 'req-verb-3');
    fill(page, '#rc-verb-task-id', 'task-42');
    page.routes.push([u => u === '/api/runs/req-verb-3/verb', () => ok(
      {verb: 'approve', run_id: '01AAA', ok: true, stdout: '', stderr: ''})]);
    await click(page, '#rc-verb-approve');
    const call = page.calls.find(c => c.url === '/api/runs/req-verb-3/verb');
    check(!!call, 'posted to /api/runs/req-verb-3/verb');
    if (!call) return;
    const body = JSON.parse(call.opts.body);
    check(body.verb === 'approve' && body.task_id === 'task-42',
      `verb and task_id both sent (got: ${JSON.stringify(body)})`);
  });
});

testCase('Task 5: #rc-verb-retry posts {verb:"retry", task_id} once a task '
  + 'id is filled in', async () => {
  await withPage(async page => {
    await openMaterialized(page, 'req-verb-4');
    fill(page, '#rc-verb-task-id', 'task-99');
    page.routes.push([u => u === '/api/runs/req-verb-4/verb', () => ok(
      {verb: 'retry', run_id: '01AAA', ok: true, stdout: '', stderr: ''})]);
    await click(page, '#rc-verb-retry');
    const call = page.calls.find(c => c.url === '/api/runs/req-verb-4/verb');
    check(!!call, 'posted to /api/runs/req-verb-4/verb');
    if (!call) return;
    const body = JSON.parse(call.opts.body);
    check(body.verb === 'retry' && body.task_id === 'task-99',
      `verb and task_id both sent (got: ${JSON.stringify(body)})`);
  });
});

// renderVerbOutcome: the pure renderer over VerbOutcome. A failing verb
// must show the maestro CLI's own stderr, never a bare "failed".

testCase('renderVerbOutcome: a failing verb shows stderr, not a bare '
  + '"failed"', async () => {
  const page = await boot();
  const html = verbOutcomeHtml(page,
    {verb: 'status', run_id: '01AAA', ok: false, stdout: '', stderr: 'no such run'});
  check(html.includes('no such run'), `stderr is shown (got: ${html})`);
});

testCase('renderVerbOutcome: a successful verb reads as success, no error '
  + 'wording, and names the run', async () => {
  const page = await boot();
  const html = verbOutcomeHtml(page,
    {verb: 'approve', run_id: '01AAA', ok: true, stdout: 'ok', stderr: ''});
  check(html.includes('01AAA'), `run_id is on screen (got: ${html})`);
  check(!/error|fail/i.test(html), `no error/fail wording (got: ${html})`);
});

// End-to-end: a click drives the real handler through to renderVerbOutcome
// showing up in #rc-verb-result, on both the success and the server-refusal
// paths — the latter (a non-ok HTTP response with a parsed `detail`) is the
// shape `RunRejectedError`/`ControlPlaneOff` take (dispatcher/server/app.py
// run_verb), distinct from a VerbOutcome with ok:false.

testCase('Task 5: a successful #rc-verb-status click renders the outcome '
  + 'via renderVerbOutcome', async () => {
  await withPage(async page => {
    await openMaterialized(page, 'req-verb-5');
    page.routes.push([u => u === '/api/runs/req-verb-5/verb', () => ok(
      {verb: 'status', run_id: '01AAA', ok: true, stdout: 'RUNNING', stderr: ''})]);
    await click(page, '#rc-verb-status');
    const html = text(page, '#rc-verb-result');
    check(html.includes('RUNNING'), `stdout is shown (got: ${html})`);
    check(el(page, '#rc-verb-status').disabled === false,
      'the button is re-enabled once the outcome lands');
  });
});

testCase('Task 5: a server refusal (non-ok HTTP, e.g. no run to act on) '
  + "shows the server's detail, and re-enables the button", async () => {
  await withPage(async page => {
    await openMaterialized(page, 'req-verb-6');
    page.routes.push([u => u === '/api/runs/req-verb-6/verb', () => resp(422,
      {detail: 'req-verb-6 has no run to act on (state: reserved)'})]);
    await click(page, '#rc-verb-status');
    const html = text(page, '#rc-verb-result');
    check(html.includes('has no run to act on'),
      `the server's refusal detail is shown (got: ${html})`);
    check(el(page, '#rc-verb-status').disabled === false,
      'the button is re-enabled after a refusal, so the operator can retry');
  });
});

// -- Task 5 fix round 1: gate the verb controls on rec.run_id --------------
// The record already knows whether a run exists to act on (`rec.run_id`):
// `reserved`/`launching` have none yet, and `renderRunView`'s OWN "no run
// yet" line says so one line above where the controls render. Three
// clickable buttons right below that sentence would only earn the operator
// a round trip to learn what this page already knew (`RunController.control`
// refuses with "has no run to act on (state: ...)",
// run_controller.py:652-656) — disabled, with the reason, not hidden
// (reserved/launching are transient and this view polls, so appearing/
// vanishing controls would make the panel jump).
//
// Each case below proves the controls block actually rendered (maybeEl !==
// null) before trusting anything about their disabled state — the same
// "absence passes trivially on nothing rendered" trap this branch has hit
// before, now guarded on the POSITIVE (enabled/disabled) side too, not just
// on absence.

testCase('Task 5 fix round 1: verb controls are disabled, with a '
  + "server-worded reason, when the record has no run yet (state: "
  + 'launching)', async () => {
  await withPage(async page => {
    await openView(page, 'req-noRun-1', {record: {state: 'launching', run_id: null},
      run: null, warnings: []});
    check(maybeEl(page, '#rc-verb-status') !== null, 'status control rendered');
    check(maybeEl(page, '#rc-verb-retry') !== null, 'retry control rendered');
    check(maybeEl(page, '#rc-verb-approve') !== null, 'approve control rendered');
    check(el(page, '#rc-verb-status').disabled === true,
      'status is disabled while there is no run yet');
    check(el(page, '#rc-verb-retry').disabled === true,
      'retry is disabled while there is no run yet');
    check(el(page, '#rc-verb-approve').disabled === true,
      'approve is disabled while there is no run yet');
    const html = text(page, '#rc-run-view');
    check(/no run to act on/i.test(html),
      `the reason uses the server's own wording (got: ${html})`);
    check(/launching/i.test(html),
      `the current state is named in the reason (got: ${html})`);
  });
});

testCase('Task 5 fix round 1: verb controls are disabled the same way for '
  + 'state: reserved, and a disabled control cannot be clicked', async () => {
  await withPage(async page => {
    await openView(page, 'req-noRun-2', {record: {state: 'reserved', run_id: null},
      run: null, warnings: []});
    check(maybeEl(page, '#rc-verb-status') !== null, 'status control rendered');
    check(el(page, '#rc-verb-status').disabled === true,
      'status is disabled for state: reserved too');
    await click(page, '#rc-verb-status');
    check(!page.calls.some(c => c.url.endsWith('/verb')),
      'a disabled control must not reach the wire even when clicked');
  });
});

testCase('Task 5 fix round 1: verb controls are enabled once the record '
  + 'carries a run_id, and no "no run" reason is shown', async () => {
  await withPage(async page => {
    await openMaterialized(page, 'req-hasRun');
    check(maybeEl(page, '#rc-verb-status') !== null, 'status control rendered');
    check(maybeEl(page, '#rc-verb-retry') !== null, 'retry control rendered');
    check(maybeEl(page, '#rc-verb-approve') !== null, 'approve control rendered');
    check(el(page, '#rc-verb-status').disabled === false,
      'status is enabled once a run exists');
    check(el(page, '#rc-verb-retry').disabled === false,
      'retry is enabled once a run exists');
    check(el(page, '#rc-verb-approve').disabled === false,
      'approve is enabled once a run exists');
    check(!/no run to act on/i.test(text(page, '#rc-run-view')),
      'the "no run to act on" reason must not appear once a run exists');
  });
});

testCase('Task 5 fix round 1: verb controls are enabled for a terminal '
  + 'record too — the gate is run_id, not any particular state', async () => {
  await withPage(async page => {
    await openView(page, 'req-terminal', {record: {state: 'terminal', run_id: '01AAA'},
      run: {status: 'completed'}, warnings: []});
    check(maybeEl(page, '#rc-verb-status') !== null, 'status control rendered');
    check(el(page, '#rc-verb-status').disabled === false,
      'status is enabled for a terminal record with a run_id');
  });
});

// ---- main -------------------------------------------------------------------

(async () => {
  for (const c of cases) {
    currentCase = c.name;
    const before = failed();
    console.log(`case: ${c.name}`);
    try {
      await c.fn();
    } catch (err) {
      caseFailures++;
      console.log(`  [FAIL] threw: ${(err && err.stack) || err}`);
    }
    console.log(failed() === before ? '  ok' : '  FAILED');
  }
  currentCase = '(drain)';
  await drain(10);
  console.log(`\ncases: ${cases.length} · failed cases: ${caseFailures} `
    + `· async errors: ${asyncErrors}`);
  summaryPrinted = true;
  process.exitCode = failed() === 0 ? 0 : 1;
})().catch(err => {
  summaryPrinted = true;
  console.error('\nHARNESS CRASHED:', (err && err.stack) || err);
  process.exitCode = 1;
});
