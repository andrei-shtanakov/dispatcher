// Exercises the tab shell (design §3.3, §4, §4.1) by running the REAL, WHOLE
// <script> of dispatcher/server/static/index.html inside a VM over the page's
// own parsed markup (tests/web/dom.js) plus the browser model of tests/web/
// dom.js's `makeBrowser` (location + history + hashchange) — same discipline
// as the sibling harnesses (run_console_harness.js's header explains the
// reasoning): self-contained on purpose, module-local fixtures.
//
// What is asserted here:
//   1. exactly one tabpanel is visible at a time, and the page boots on
//      Launchpad when no hash is given;
//   2. the hash is the only writer of the current screen: a direct hash opens
//      that screen, an unknown one and a malformed nested segment fall back to
//      Launchpad (the closed grammar of design §4.1);
//   3. Back/Forward walk the screens with no second code path, because a tab
//      click only assigns `location.hash`;
//   4. the keyboard contract of design §4: Left/Right move between screens,
//      Enter opens the focused one;
//   5. the ARIA wiring of every tab/panel pair, and the tab ORDER — SCREEN_IDS
//      below is a literal list precisely so the order is pinned by a test and
//      not only by the page's own registry;
//   6. design §3.3's single exception: the global `Unresolved task requests`
//      band lives outside every panel, so the outcome of a sent mutation is
//      not destroyed by switching tabs.
//
// Usage: node tabs_harness.js <path-to-index.html>
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Document, dispatch} = require(path.join(__dirname, 'dom.js'));
const {browserGlobals, openScreen} = require(path.join(__dirname, 'screens.js'));

const HTML_PATH = process.argv[2];
if (!HTML_PATH) {
  console.error('usage: node tabs_harness.js <index.html>');
  process.exit(2);
}

// The nine screens of Task 2, in registry order (`benchmarks` is Task 7's
// conditional tenth). Literal on purpose: the order of the tab strip is a
// contract of design §3.1, and a list derived from the page could not fail.
const SCREEN_IDS = [
  'launchpad', 'sync', 'projects', 'errors', 'models',
  'contracts', 'epics', 'waits', 'roadmap',
];

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

/** A structurally-valid, empty LaunchpadSnapshot (dispatcher/core/
 * launchpad.py's LaunchpadSnapshot pydantic model, field-for-field). */
function snapshot(overrides = {}) {
  return {
    snapshot_id: 'snap-base',
    generated_at: '2026-08-27T00:00:00Z',
    repositories: [],
    ready: [],
    blocked: [],
    unregistered_items: [],
    orphan_dags: [],
    active: [],
    active_truncated: false,
    recent_completed: [],
    completed_total: 0,
    next_cursor: null,
    store_unreadable: [],
    ...overrides,
  };
}

// refresh() (index.html) fans out to these endpoints on load, same set as
// launchpad_harness.js's defaultRoutes — every whole-script harness needs all
// of them fixture'd or the unrelated dashboard code throws during boot.
function defaultRoutes() {
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
    [u => u.startsWith('/api/epics'), () => ok({
      generated_at: null, registry_path: '/ws/epics.toml', registry_ok: true,
      registry_diagnostics: [], programs: {}, planes: [], rows: [], defects: [],
    })],
    [u => u.startsWith('/api/benchmarks'), () => ok({
      fetch_in_flight: false,
      report: {status: 'unconfigured', url: null, fetched_at: null,
        error: null, benchmarks: [], leaderboards: {}},
    })],
    [u => u.startsWith('/api/actions/session'), () => ok({token: 'test-token'})],
    [u => u === '/api/runs/submit', () => ok({
      request_id: 'fixture-request-id', run_id: null, accepted: true, reason: null,
    })],
    [u => u === '/api/launchpad', () => ok(snapshot())],
  ];
}

const drain = async (turns = 5) => {
  for (let i = 0; i < turns; i++) await new Promise(r => setTimeout(r, 0));
};

/** A recording, non-firing setInterval/clearInterval: every registration is
 * kept and NOTHING fires on its own, so no case here races a real clock. */
function makeIntervalRecorder() {
  const registered = new Map();
  let nextId = 1;
  return {
    setInterval(cb, period) {
      const id = nextId++;
      registered.set(id, {cb, period});
      return id;
    },
    clearInterval(id) { registered.delete(id); },
    byPeriod(period) {
      for (const iv of registered.values()) if (iv.period === period) return iv;
      return null;
    },
  };
}

/**
 * Boots the page. `opts.hash` is the address bar the page opens on;
 * `opts.routes` are prepended to `defaultRoutes()` so they override it.
 */
async function boot(opts = {}) {
  const document = new Document(BODY_HTML);
  const calls = [];
  const routes = [...(opts.routes || []), ...defaultRoutes()];
  const timers = makeIntervalRecorder();
  const ctx = {
    document, console, URL,
    setTimeout, clearTimeout,
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
    fetch: (url, opts2) => {
      const u = String(url);
      calls.push({url: u, opts: opts2 || {}});
      for (const [test, make] of routes) if (test(u)) return Promise.resolve(make(u));
      return Promise.reject(new Error(`no fixture route for ${u}`));
    },
    ...browserGlobals(opts.hash),
  };
  vm.createContext(ctx);
  vm.runInContext(PAGE_SCRIPT, ctx);
  await drain();
  return {ctx, document, calls, routes, timers};
}

/** Boots a fresh page and hands it to `fn`. */
async function withPage(fn, opts = {}) { await fn(await boot(opts)); }

/** Overrides a route on an already-booted page. */
function overrideRoute(page, url, make) {
  page.routes.unshift([u => u === url, make]);
}

function el(page, selector) {
  const node = page.document.querySelector(selector);
  if (!node) throw new Error(`${selector} is not in the page markup`);
  return node;
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

// ---- cases ------------------------------------------------------------------

testCase('boots on Launchpad when no hash is given', async () => {
  await withPage(page => {
    check(!el(page, '#screen-launchpad').hidden, 'launchpad panel is visible');
    check(el(page, '#screen-sync').hidden, 'sync panel is hidden');
    check(el(page, '#tab-launchpad').attributes['aria-selected'] === 'true',
      'launchpad tab is aria-selected');
  });
});

testCase('exactly one panel is visible at a time', async () => {
  await withPage(async page => {
    await openScreen(page, 'epics');
    const visible = SCREEN_IDS.filter(
      id => !page.document.getElementById(`screen-${id}`).hidden);
    check(visible.length === 1 && visible[0] === 'epics',
      `one visible panel, got ${visible.join(',') || 'none'}`);
  });
});

testCase('a direct hash opens that screen', async () => {
  await withPage(page => {
    check(!el(page, '#screen-waits').hidden, 'waits opened from the hash');
  }, {hash: '#waits'});
});

testCase('an unknown hash falls back to Launchpad', async () => {
  await withPage(page => {
    check(!el(page, '#screen-launchpad').hidden, 'fell back to launchpad');
  }, {hash: '#no-such-screen'});
});

testCase('a malformed nested segment falls back to Launchpad', async () => {
  await withPage(page => {
    check(!el(page, '#screen-launchpad').hidden, 'fell back to launchpad');
  }, {hash: '#projects/../etc/passwd'});
});

testCase('back and forward walk the screens', async () => {
  await withPage(async page => {
    await openScreen(page, 'models');
    await openScreen(page, 'contracts');
    page.ctx.history.back();
    await drain();
    check(!el(page, '#screen-models').hidden, 'back landed on models');
    page.ctx.history.forward();
    await drain();
    check(!el(page, '#screen-contracts').hidden, 'forward returned to contracts');
  });
});

testCase('arrow keys move between screens, Enter opens one', async () => {
  await withPage(async page => {
    dispatch(el(page, '#tab-launchpad'), 'keydown', {init: {key: 'ArrowRight'}});
    await drain();
    check(!el(page, '#screen-sync').hidden, 'ArrowRight moved to sync');
    dispatch(el(page, '#tab-sync'), 'keydown', {init: {key: 'ArrowLeft'}});
    await drain();
    check(!el(page, '#screen-launchpad').hidden, 'ArrowLeft moved back');
    dispatch(el(page, '#tab-epics'), 'keydown', {init: {key: 'Enter'}});
    await drain();
    check(!el(page, '#screen-epics').hidden, 'Enter opened the focused tab');
  });
});

testCase('every tab carries its ARIA wiring', async () => {
  await withPage(page => {
    for (const id of SCREEN_IDS) {
      const tab = el(page, `#tab-${id}`);
      const panel = el(page, `#screen-${id}`);
      check(tab.attributes.role === 'tab', `${id}: role=tab`);
      check(tab.attributes['aria-controls'] === `screen-${id}`,
        `${id}: aria-controls points at the panel`);
      check(panel.attributes.role === 'tabpanel', `${id}: role=tabpanel`);
      check(panel.attributes['aria-labelledby'] === `tab-${id}`,
        `${id}: panel is labelled by its tab`);
    }
  });
});

testCase('the unresolved-requests band lives outside every panel', async () => {
  await withPage(page => {
    const band = el(page, '#ta-outcomes');
    for (const id of SCREEN_IDS) {
      check(!band.closest(`#screen-${id}`),
        `#ta-outcomes must not sit inside screen-${id}`);
    }
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
