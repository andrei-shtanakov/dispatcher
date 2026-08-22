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

async function boot(submitRoute) {
  const document = new Document(BODY_HTML);
  const calls = [];
  const routes = defaultRoutes(submitRoute);
  const ctx = {
    document, console, URL,
    setTimeout, clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {},
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
  return {ctx, document, calls};
}

function el(page, selector) {
  const node = page.document.querySelector(selector);
  if (!node) throw new Error(`${selector} is not in the page markup`);
  return node;
}
function fill(page, selector, value) { el(page, selector).value = value; }
async function click(page, selector) {
  await Promise.all(dispatch(el(page, selector), 'click'));
  await drain();
}
/** Calls the page's own renderReceipt(), not a copy of it. */
function receiptHtml(page, receipt) {
  return vm.runInContext(
    `renderReceipt(${JSON.stringify(receipt)})`, page.ctx);
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

async function fillMinimalForm(page) {
  fill(page, '#rc-repository', 'deployer');
  fill(page, '#rc-revision', 'a'.repeat(40));
  fill(page, '#rc-tasks', 'tasks.yaml');
  fill(page, '#rc-work-id', 'todo://deployer/entrypoint-token-boundary-match');
}

testCase('a rejected fetch() renders as unknown, never as a refusal', async () => {
  const page = await boot(() => Promise.reject(new Error('network down')));
  await fillMinimalForm(page);
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
    await fillMinimalForm(page);
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
    await fillMinimalForm(page);
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
    await fillMinimalForm(page);
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
    await fillMinimalForm(page);
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
