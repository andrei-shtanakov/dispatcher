// Exercises the Run-status row of the Benchmarks panel (phase-2 spec
// 2026-08-16 §7) by running the REAL, WHOLE <script> of
// dispatcher/server/static/index.html inside a VM over the page's own
// parsed markup (tests/web/dom.js) — same discipline as the other
// harnesses: nothing is sliced and no handler is simulated. Since Task 7
// (tabbed-ui) the row lives inside the CONDITIONAL `#screen-benchmarks`
// panel, so boot() opens that screen by clicking its tab — the fixture
// profile is configured, and dispatch() refuses a control nobody can see.
//
// Asserted here, client-side:
//   1. an ok report renders the producer's status word VERBATIM, the
//      task index/count, the score and the shallow component list.
//   2. a token_* state renders the server's reason as a configuration
//      answer (its exact wording, e.g. «chmod 600»), never as a missing
//      run.
//   3. not_found keeps the server's two-sided wording («run not found,
//      or not owned by this token»).
//   4. a network failure is fail-loud, never «no such run».
//   5. an invalid input (empty / 0) prompts and performs NO fetch.
//   6. the button is disabled while a request is in flight (no stacking)
//      and re-enabled after; exactly one request per click.
//   7. hostile producer strings arrive escaped.
//
// Usage: node run_status_harness.js <path-to-index.html>
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Document, dispatch} = require(path.join(__dirname, 'dom.js'));
const {browserGlobals, openScreen} = require(path.join(__dirname, 'screens.js'));

const HTML_PATH = process.argv[2];
if (!HTML_PATH) {
  console.error('usage: node run_status_harness.js <index.html>');
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

// ---- page loading ----------------------------------------------------------

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

// ---- fixtures --------------------------------------------------------------

const resp = (status, body) => ({
  status, ok: status >= 200 && status < 300,
  json: () => Promise.resolve(body),
});
const ok = body => resp(200, body);

const BENCHMARKS_OK = {
  fetch_in_flight: false,
  report: {
    status: 'ok', url: 'https://bench.example/api',
    fetched_at: '2026-08-16T10:00:00Z', error: null,
    benchmarks: [], leaderboards: {},
  },
};

const RUN_OK = {
  status: 'ok', run_id: 42, fetched_at: '2026-08-16T10:01:00Z', error: null,
  run: {
    id: 42, status: 'completed', current_task_index: 3, tasks_count: 3,
    total_score: 87.5,
    score_semantics: {kind: 'aggregated_evaluation', quality_signal: true},
    score_components: {contains: 91.7, regex: 83.3},
  },
};
const TOKEN_INSECURE = {
  status: 'token_file_insecure', run_id: 42, fetched_at: null,
  error: 'token file is group/other-accessible (0644): /x/atp-token — chmod 600',
  run: null,
};
const NOT_FOUND = {
  status: 'not_found', run_id: 7, fetched_at: '2026-08-16T10:01:00Z',
  error: 'HTTP 404 (https://bench.example/api/v1/runs/7/status) — run not '
    + 'found, or not owned by this token',
  run: null,
};
const HOSTILE = {
  status: 'ok', run_id: 42, fetched_at: '2026-08-16T10:01:00Z', error: null,
  run: {
    id: 42, status: '<img src=x onerror=alert(1)>', current_task_index: 1,
    tasks_count: 3, total_score: null,
    score_semantics: {}, score_components: {},
  },
};

function defaultRoutes(runRoute) {
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
    [u => u.startsWith('/api/benchmarks/runs/'), runRoute],
    [u => u.startsWith('/api/benchmarks'), () => ok(BENCHMARKS_OK)],
  ];
}

const drain = async (turns = 5) => {
  for (let i = 0; i < turns; i++) await new Promise(r => setTimeout(r, 0));
};

async function boot(runRoute) {
  const document = new Document(BODY_HTML);
  const calls = [];
  const routes = defaultRoutes(runRoute);
  const ctx = {
    document, console, URL,
    setTimeout, clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {},
    fetch: url => {
      const u = String(url);
      if (u.startsWith('/api/benchmarks/runs/')) calls.push(u);
      for (const [test, make] of routes) if (test(u)) return Promise.resolve(make(u));
      return Promise.reject(new Error(`no fixture route for ${u}`));
    },
    ...browserGlobals(),
  };
  vm.createContext(ctx);
  vm.runInContext(PAGE_SCRIPT, ctx);
  await drain();
  const env = {ctx, document, calls};
  // `calls` only records `/api/benchmarks/runs/…`, so opening the screen
  // (which reloads `/api/benchmarks`) cannot disturb the request counts.
  await openScreen(env, 'benchmarks');
  return env;
}

async function check_run(env, runId) {
  env.document.getElementById('run-status-input').value = runId;
  const btn = env.document.getElementById('run-status-check');
  await Promise.all(dispatch(btn, 'click'));
  await drain();
}

function resultText(env) {
  return env.document.getElementById('run-status-result').textContent;
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

testCase('an ok run is readable off one screen, status word verbatim', async () => {
  const env = await boot(() => ok(RUN_OK));
  await check_run(env, '42');
  const text = resultText(env);
  check(text.includes('run 42'), `run id on screen (got: ${text})`);
  check(text.includes('completed'), 'producer status word verbatim');
  check(text.includes('3/3'), 'task index/count on screen');
  check(text.includes('87.5'), 'total_score on screen');
  check(text.includes('contains: 91.7'), 'a score component on screen');
  check(text.includes('quality_signal'), 'semantics reachable on screen');
});

testCase('a token state is a configuration answer, not a missing run', async () => {
  const env = await boot(() => ok(TOKEN_INSECURE));
  await check_run(env, '42');
  const text = resultText(env);
  check(text.includes('token_file_insecure'), 'the state name is on screen');
  check(text.includes('chmod 600'), 'the server reason arrives verbatim');
  check(!text.includes('not found'), 'a token problem never reads as a missing run');
});

testCase('not_found keeps the two-sided wording', async () => {
  const env = await boot(() => ok(NOT_FOUND));
  await check_run(env, '7');
  const text = resultText(env);
  check(text.includes('run not found, or not owned by this token'),
    `two-sided 404 wording preserved (got: ${text})`);
});

testCase('a network failure is fail-loud, never «no such run»', async () => {
  const env = await boot(() => Promise.reject(new Error('ECONNREFUSED')));
  await check_run(env, '42');
  const text = resultText(env);
  check(text.includes('ECONNREFUSED'), 'the failure itself is on screen');
  check(!env.document.getElementById('run-status-check').disabled,
    'the button is re-enabled after a failure');
});

testCase('an HTTP failure names the status code', async () => {
  const env = await boot(() => resp(500, {detail: 'boom'}));
  await check_run(env, '42');
  check(resultText(env).includes('run-status endpoint failed: 500'),
    'the status code is on screen');
});

testCase('invalid input prompts and performs NO fetch', async () => {
  const env = await boot(() => ok(RUN_OK));
  for (const bad of ['', '0', '-3', 'abc', '1.5']) {
    await check_run(env, bad);
    check(resultText(env).includes('enter a run id'),
      `bad input ${JSON.stringify(bad)} prompts`);
  }
  check(env.calls.length === 0,
    `no run-status request was made (got: ${env.calls})`);
});

testCase('in-flight: button disabled, one request per click, then re-enabled',
  async () => {
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    const env = await boot(() => gate.then(() => ok(RUN_OK)));
    env.document.getElementById('run-status-input').value = '42';
    const btn = env.document.getElementById('run-status-check');
    dispatch(btn, 'click');
    await drain();
    check(btn.disabled === true, 'button disabled while the request runs');
    // The SECOND click, issued while the request is still in flight. The
    // DOM stub mirrors the browser here: dispatch() on a disabled control
    // is suppressed (returns no handler promises) — which is exactly the
    // no-stacking guarantee the disabled button provides.
    const second = dispatch(btn, 'click');
    check(second.length === 0, 'a click on the disabled button is suppressed');
    await drain();
    check(env.calls.length === 1,
      `no second request while in flight (got: ${env.calls.length})`);
    release();
    await drain();
    check(btn.disabled === false, 'button re-enabled after the response');
    check(env.calls.length === 1, `exactly one request (got: ${env.calls.length})`);
    check(resultText(env).includes('run 42'), 'the released response rendered');
  });

testCase('hostile producer strings arrive escaped', async () => {
  const env = await boot(() => ok(HOSTILE));
  await check_run(env, '42');
  const box = env.document.getElementById('run-status-result');
  check(!box.innerHTML.includes('<img'), 'raw markup does not survive esc()');
  check(box.innerHTML.includes('&lt;img'), 'the status word is readable, escaped');
});

// ---- main ------------------------------------------------------------------

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
