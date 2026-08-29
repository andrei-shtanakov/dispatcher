// Exercises the Benchmarks panel (inbox-driven ATP benchmark view, PR-2) by
// running the REAL, WHOLE <script> of dispatcher/server/static/index.html
// inside a VM over the page's own parsed markup (tests/web/dom.js) — the
// same discipline as product_proposals_harness.js: nothing is sliced by
// string markers and no handler is simulated. `refresh()` fires on load and
// drives `renderBenchmarks(...)` from a stubbed `/api/benchmarks` response;
// cases override that one route and assert DOM state.
//
// Asserted here, client-side:
//   1. `unconfigured` hides the whole section.
//   2. an `ok` report with zero benchmarks is a CONFIDENT «0 benchmarks», not
//      an empty list.
//   3. `unavailable` with no error yet (never fetched) reads as
//      «not fetched yet», never as a confident zero.
//   4/5. `unavailable`/`unreadable` WITH an error: the list says «unknown»
//      (never «0 benchmarks»); the error text itself lands on
//      #benchmarks-status, not in the list.
//   6. clicking a benchmark with an `ok`, empty leaderboard renders
//      «0 entries».
//   7. clicking a benchmark whose leaderboard is `unavailable` renders
//      «leaderboard unknown», never «0 entries».
//   8. a hostile producer string (benchmark name) arrives escaped: no
//      element is created from it, the rendered markup is readable escaped.
//
// Usage: node benchmarks_harness.js <path-to-index.html>
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Document, dispatch} = require(path.join(__dirname, 'dom.js'));
const {browserGlobals, openScreen} = require(path.join(__dirname, 'screens.js'));

const HTML_PATH = process.argv[2];
if (!HTML_PATH) {
  console.error('usage: node benchmarks_harness.js <index.html>');
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

const UNCONFIGURED = {
  fetch_in_flight: false,
  report: {
    status: 'unconfigured', url: null, fetched_at: null, error: null,
    benchmarks: [], leaderboards: {},
  },
};
const OK_EMPTY = {
  fetch_in_flight: false,
  report: {
    status: 'ok', url: 'https://bench.example/api',
    fetched_at: '2026-08-15T10:00:00Z', error: null,
    benchmarks: [], leaderboards: {},
  },
};
const NOT_FETCHED_YET = {
  fetch_in_flight: false,
  report: {
    status: 'unavailable', url: null, fetched_at: null, error: null,
    benchmarks: [], leaderboards: {},
  },
};
const UNAVAILABLE_WITH_ERROR = {
  fetch_in_flight: false,
  report: {
    status: 'unavailable', url: 'https://bench.example/api',
    fetched_at: '2026-08-15T09:00:00Z', error: 'connection refused',
    benchmarks: [], leaderboards: {},
  },
};
const UNREADABLE_WITH_ERROR = {
  fetch_in_flight: false,
  report: {
    status: 'unreadable', url: 'https://bench.example/api',
    fetched_at: '2026-08-15T09:00:00Z', error: 'invalid JSON body',
    benchmarks: [], leaderboards: {},
  },
};
const BENCH = {
  id: 7, name: 'atp-core', version: '1.2', tasks_count: 42,
  tags: ['core', 'regression'],
};
const OK_ONE_BENCH_EMPTY_LEADERBOARD = {
  fetch_in_flight: false,
  report: {
    status: 'ok', url: 'https://bench.example/api',
    fetched_at: '2026-08-15T10:00:00Z', error: null,
    benchmarks: [BENCH],
    leaderboards: {'7': {status: 'ok', rows: [], error: null}},
  },
};
const OK_ONE_BENCH_UNAVAILABLE_LEADERBOARD = {
  fetch_in_flight: false,
  report: {
    status: 'ok', url: 'https://bench.example/api',
    fetched_at: '2026-08-15T10:00:00Z', error: null,
    benchmarks: [BENCH],
    leaderboards: {'7': {status: 'unavailable', rows: [], error: 'timeout'}},
  },
};
const HOSTILE_NAME = '<img src=x onerror=alert(1)>';
const OK_HOSTILE_NAME = {
  fetch_in_flight: false,
  report: {
    status: 'ok', url: 'https://bench.example/api',
    fetched_at: '2026-08-15T10:00:00Z', error: null,
    benchmarks: [
      {id: 1, name: HOSTILE_NAME, version: '1', tasks_count: 1, tags: []},
    ],
    leaderboards: {},
  },
};

function defaultRoutes(benchRoute) {
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
    [u => u.startsWith('/api/benchmarks'), benchRoute],
  ];
}

const drain = async (turns = 5) => {
  for (let i = 0; i < turns; i++) await new Promise(r => setTimeout(r, 0));
};

async function boot(benchRoute) {
  const document = new Document(BODY_HTML);
  const routes = defaultRoutes(benchRoute);
  const ctx = {
    document, console, URL,
    setTimeout, clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {},
    fetch: url => {
      const u = String(url);
      for (const [test, make] of routes) if (test(u)) return Promise.resolve(make(u));
      return Promise.reject(new Error(`no fixture route for ${u}`));
    },
    ...browserGlobals(),
  };
  vm.createContext(ctx);
  vm.runInContext(PAGE_SCRIPT, ctx);
  await drain();
  return {ctx, document};
}

function textOf(env, id) {
  const node = env.document.getElementById(id);
  if (!node) throw new Error(`#${id} is not in the page markup`);
  return node.textContent;
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

testCase('unconfigured hides the whole section', async () => {
  const env = await boot(() => ok(UNCONFIGURED));
  const section = env.document.getElementById('benchmarks-section');
  check(section.hidden === true, 'benchmarks-section carries hidden');
});

testCase('ok + empty benchmarks is a confident «0 benchmarks»', async () => {
  const env = await boot(() => ok(OK_EMPTY));
  const section = env.document.getElementById('benchmarks-section');
  check(section.hidden === false, 'section is visible once configured');
  check(textOf(env, 'benchmarks-list').includes('0 benchmarks'),
    `list says 0 benchmarks (got: ${textOf(env, 'benchmarks-list')})`);
});

testCase('unavailable, never fetched yet: no confident zero', async () => {
  const env = await boot(() => ok(NOT_FETCHED_YET));
  const listText = textOf(env, 'benchmarks-list');
  check(listText.includes('not fetched yet'),
    `list says not fetched yet (got: ${listText})`);
  check(!listText.includes('0 benchmarks'),
    'an unfetched report must not read as a confident zero');
});

testCase('unavailable with an error: unknown, never a confident zero', async () => {
  const env = await boot(() => ok(UNAVAILABLE_WITH_ERROR));
  const listText = textOf(env, 'benchmarks-list');
  const statusText = textOf(env, 'benchmarks-status');
  check(listText.includes('unknown'),
    `list says unknown (got: ${listText})`);
  check(!listText.includes('0 benchmarks'),
    'a failed fetch must not read as a confident zero');
  check(statusText.includes('connection refused'),
    `the error text lands on #benchmarks-status (got: ${statusText})`);
  check(!listText.includes('connection refused'),
    'the error text must not also land in the list');
});

testCase('unreadable with an error: unknown, never a confident zero', async () => {
  const env = await boot(() => ok(UNREADABLE_WITH_ERROR));
  const listText = textOf(env, 'benchmarks-list');
  const statusText = textOf(env, 'benchmarks-status');
  check(listText.includes('unknown'),
    `list says unknown (got: ${listText})`);
  check(!listText.includes('0 benchmarks'),
    'an unreadable report must not read as a confident zero');
  check(statusText.includes('invalid JSON body'),
    `the error text lands on #benchmarks-status (got: ${statusText})`);
});

testCase('clicking a benchmark with an ok, empty leaderboard: «0 entries»', async () => {
  const env = await boot(() => ok(OK_ONE_BENCH_EMPTY_LEADERBOARD));
  const btn = env.document.querySelector('#benchmarks-list button[data-bench]');
  if (!btn) throw new Error('no benchmark button rendered');
  await Promise.all(dispatch(btn, 'click'));
  await drain();
  const boxText = textOf(env, 'benchmarks-leaderboard');
  check(boxText.includes('0 entries'),
    `leaderboard box says 0 entries (got: ${boxText})`);
});

testCase('clicking a benchmark with an unavailable leaderboard: «leaderboard unknown»', async () => {
  const env = await boot(() => ok(OK_ONE_BENCH_UNAVAILABLE_LEADERBOARD));
  const btn = env.document.querySelector('#benchmarks-list button[data-bench]');
  if (!btn) throw new Error('no benchmark button rendered');
  await Promise.all(dispatch(btn, 'click'));
  await drain();
  const boxText = textOf(env, 'benchmarks-leaderboard');
  check(boxText.includes('leaderboard unknown'),
    `leaderboard box says leaderboard unknown (got: ${boxText})`);
  check(!boxText.includes('0 entries'),
    'a failed leaderboard fetch must not read as a confident zero');
});

testCase('a selected benchmark with NO leaderboard entry: unknown, not empty', async () => {
  // Zero-state rule (Copilot review PR #153): an empty box after selecting
  // a benchmark reads like «0 entries» — a missing entry must say unknown.
  const NO_LB_ENTRY = {
    fetch_in_flight: false,
    report: {
      status: 'ok', url: 'https://bench.example/api',
      fetched_at: '2026-08-15T10:00:00Z', error: null,
      benchmarks: [BENCH],
      leaderboards: {},
    },
  };
  const env = await boot(() => ok(NO_LB_ENTRY));
  const btn = env.document.querySelector('#benchmarks-list button[data-bench]');
  if (!btn) throw new Error('no benchmark button rendered');
  await Promise.all(dispatch(btn, 'click'));
  await drain();
  const boxText = textOf(env, 'benchmarks-leaderboard');
  check(boxText.includes('leaderboard unknown'),
    `missing entry says leaderboard unknown (got: ${boxText})`);
  check(!boxText.includes('0 entries'),
    'a missing leaderboard entry must not read as a confident zero');
});

testCase('a hostile benchmark name arrives escaped, no element created', async () => {
  const env = await boot(() => ok(OK_HOSTILE_NAME));
  const list = env.document.getElementById('benchmarks-list');
  check(list.innerHTML.includes('&lt;img'),
    `the hostile name is readable, escaped (got: ${list.innerHTML})`);
  check(!list.innerHTML.includes('<img'), 'raw markup does not survive esc()');
  check(list.querySelector('img') === null,
    'no <img> element was created from the producer string');
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
