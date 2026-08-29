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

/** Boot option that overrides `/api/launchpad` with `snapshot(overrides)` —
 * a `withPage(fn, snapshotRoute({active: [...]}))` shorthand for cases that
 * only care about one or two LaunchpadSnapshot arrays. */
function snapshotRoute(overrides = {}) {
  return {routes: [[u => u === '/api/launchpad', () => ok(snapshot(overrides))]]};
}

// Task 3 (tabbed-ui): an ActiveRow (dispatcher/core/launchpad.py) linked to a
// real request — the drill-down case — versus an unlinked maestro run (bare
// run_id, request_id: null) that offers none.
const ACTIVE_LINKED = {
  request_id: 'rc-active-1', repo_key: 'github.com/andrei-shtanakov/deployer',
  work_id: 'wi-1', state: 'materialized', run_id: '01AAA',
  run_status: 'running', attention: false, updated_at: '2026-08-29T00:00:00Z',
};
const ACTIVE_UNLINKED = {
  request_id: null, repo_key: 'github.com/andrei-shtanakov/deployer',
  work_id: null, state: 'unlinked-run', run_id: '01BBB',
  run_status: 'running', attention: false, updated_at: '2026-08-29T00:00:00Z',
};

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
    // GET /api/runs/{request_id} (openRunView/rcPollView) — Task 3's
    // drill-down cases hit this for whatever id the row/hash names.
    [u => u.startsWith('/api/runs/') && u !== '/api/runs/submit', () => ok({
      record: {state: 'materialized', run_id: '01AAA'}, run: null, warnings: [],
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
function maybeEl(page, selector) { return page.document.querySelector(selector); }
function htmlOf(page, selector) { return el(page, selector).innerHTML; }
async function click(page, selector) {
  await Promise.all(dispatch(el(page, selector), 'click'));
  await drain();
}
function callsTo(page, url) { return page.calls.filter(c => c.url === url).length; }

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

// ---- Task 3: Launchpad as the main screen, run view as drill-down ---------

testCase('the manual form sits collapsed inside the Launchpad screen', async () => {
  await withPage(page => {
    const rc = el(page, '#run-console');
    check(!!rc.closest('#screen-launchpad'),
      'run console must live inside the launchpad panel');
    const details = rc.querySelector('details');
    check(!!details, 'the manual form must be collapsed (a <details>)');
    check(!!details && !!details.querySelector('#rc-repo-key'),
      'the repo_key input must sit inside the collapsed block');
  });
});

testCase('clicking an active run opens the run view and sets the hash', async () => {
  await withPage(async page => {
    const row = el(page, '#lp-active [data-lp-request-id="rc-active-1"]');
    await Promise.all(dispatch(row, 'click'));
    await drain();
    check(page.ctx.location.hash === '#launchpad/rc-active-1',
      `hash is the drill-down, got ${page.ctx.location.hash}`);
    // Exact count, not `.some()` (fix round 1, finding 1): navigate()'s own
    // synchronous hashchange already opens the run via onScreenShown — a
    // direct call from lpOpenRun on top of that would double-fetch and a
    // `.some()` assertion is exactly what let that regression through.
    check(callsTo(page, '/api/runs/rc-active-1') === 1,
      `the run view fetched the run exactly once (got ${callsTo(page, '/api/runs/rc-active-1')})`);
  }, snapshotRoute({active: [ACTIVE_LINKED]}));
});

testCase('the drill-down control is reachable by keyboard and activating it opens the run',
  async () => {
  await withPage(async page => {
    const btn = el(page, '#lp-active button.lp-open-run-btn[data-lp-request-id="rc-active-1"]');
    check(!btn.disabled && btn.tagName === 'BUTTON',
      'a real, focusable <button> carries the drill-down, not a bare row');
    check(!!btn.attributes['aria-label'] && btn.attributes['aria-label'].length > 0,
      'the control names which run it opens');
    await Promise.all(dispatch(btn, 'click'));
    await drain();
    check(page.ctx.location.hash === '#launchpad/rc-active-1',
      `hash is the drill-down, got ${page.ctx.location.hash}`);
    check(callsTo(page, '/api/runs/rc-active-1') === 1,
      'activating the control opened the run view exactly once');
  }, snapshotRoute({active: [ACTIVE_LINKED]}));
});

testCase('an unlinked active run offers no drill-down', async () => {
  await withPage(page => {
    check(!maybeEl(page, '#lp-active [data-lp-request-id]'),
      'an unlinked row must not be clickable into a run view');
  }, snapshotRoute({active: [ACTIVE_UNLINKED]}));
});

testCase('a direct drill-down hash opens the run view on load', async () => {
  await withPage(page => {
    check(callsTo(page, '/api/runs/rc-deep') === 1,
      `the drill-down hash fetched the run on boot exactly once (got ${callsTo(page, '/api/runs/rc-deep')})`);
  }, {hash: '#launchpad/rc-deep'});
});

testCase('a request_id that cannot survive the hash grammar opens directly, '
  + 'without writing the hash', async () => {
  const UNROUTABLE_ID = 'weird id';   // a space fails HASH_RE's sub-segment
  await withPage(async page => {
    const before = page.ctx.location.hash;
    const row = el(page, `#lp-active [data-lp-request-id="${UNROUTABLE_ID}"]`);
    await Promise.all(dispatch(row, 'click'));
    await drain();
    check(page.ctx.location.hash === before,
      `an unroutable request_id must not write the hash, got ${page.ctx.location.hash}`);
    check(callsTo(page, `/api/runs/${encodeURIComponent(UNROUTABLE_ID)}`) === 1,
      'the run view still opened directly, exactly once');
  }, snapshotRoute({active: [{...ACTIVE_LINKED, request_id: UNROUTABLE_ID}]}));
});

// ---- Task 3 ruling: #lp-pending gets drill-down too (spec §5.3), guarded --
//
// A pending row's own request_id only becomes known once a launch attempt
// is actually sent (Confirm) — an "open", never-submitted confirmation
// carries requestId: null. All three cases here drive the REAL flow
// (toggle a Ready row open, click Confirm, force the post-action refetch
// that drops the row out of ready[] so it renders as a #lp-pending orphan)
// rather than reaching into lpState by hand.
const PENDING_READY_ROW = {
  repo_key: 'github.com/o/r', work_id: 'w1', dag_path: 'dag.yaml',
  seen_revision: 'a'.repeat(40),
};
/** Boot route: Ready on the FIRST /api/launchpad fetch, gone on every one
 * after — the shape every orphaned-pending case here needs. */
function pendingOrphanRoutes() {
  let servedFirst = false;
  return {routes: [[u => u === '/api/launchpad', () => {
    if (!servedFirst) { servedFirst = true; return ok(snapshot({ready: [PENDING_READY_ROW]})); }
    return ok(snapshot());
  }]]};
}
/** Confirms the Ready row with a submit that never reaches the server —
 * lpConfirmLaunch's catch path — then forces the refetch that drops the row
 * out of ready[], landing the resulting "unknown" entry in #lp-pending. */
async function openUnknownPendingRow(page) {
  overrideRoute(page, '/api/runs/submit', () => {
    throw new Error('simulated transport failure');
  });
  await click(page, '#lp-ready tr.lp-ready-row');
  await click(page, '#lp-ready .lp-confirm');
  page.ctx.lpRefetchAfterAction();
  await drain();
}

testCase('clicking a pending row with a request_id opens the run view and sets the hash',
  async () => {
  await withPage(async page => {
    await openUnknownPendingRow(page);
    const row = maybeEl(page, '#lp-pending [data-lp-request-id]');
    check(!!row, 'the unknown pending entry carries the drill-down attribute');
    if (!row) return;
    const requestId = row.dataset.lpRequestId;
    await click(page, `#lp-pending [data-lp-request-id="${requestId}"]`);
    check(page.ctx.location.hash === `#launchpad/${requestId}`,
      `hash is the drill-down, got ${page.ctx.location.hash}`);
    check(callsTo(page, `/api/runs/${requestId}`) === 1,
      `the run view fetched the run exactly once (got ${callsTo(page, `/api/runs/${requestId}`)})`);
  }, pendingOrphanRoutes());
});

testCase('the pending row\'s own drill-down button is reachable by keyboard '
  + 'and activating it opens the run', async () => {
  await withPage(async page => {
    await openUnknownPendingRow(page);
    const btn = maybeEl(page, '#lp-pending button.lp-open-run-btn[data-lp-request-id]');
    check(!!btn, 'the pending entry carries a real, focusable drill-down button');
    if (!btn) return;
    check(!btn.disabled && btn.tagName === 'BUTTON',
      'a real <button>, not a bare row, is the keyboard-reachable control');
    const requestId = btn.dataset.lpRequestId;
    await Promise.all(dispatch(btn, 'click'));
    await drain();
    check(page.ctx.location.hash === `#launchpad/${requestId}`,
      `hash is the drill-down, got ${page.ctx.location.hash}`);
    check(callsTo(page, `/api/runs/${requestId}`) === 1,
      'activating the control opened the run view exactly once');
  }, pendingOrphanRoutes());
});

testCase('clicking a button inside a pending row does not navigate — its own '
  + 'action still fires', async () => {
  await withPage(async page => {
    await openUnknownPendingRow(page);
    const before = page.ctx.location.hash;
    const submitsBefore = page.calls.filter(c => c.url === '/api/runs/submit').length;
    await click(page, '#lp-pending .lp-retry');
    check(page.ctx.location.hash === before,
      `a click on Retry must not navigate, got ${page.ctx.location.hash}`);
    check(page.calls.filter(c => c.url === '/api/runs/submit').length > submitsBefore,
      "Retry's own action (re-submit) still fired");
  }, pendingOrphanRoutes());
});

testCase('an orphaned pending entry with no request_id yet offers no drill-down',
  async () => {
  await withPage(async page => {
    await click(page, '#lp-ready tr.lp-ready-row');   // "open" confirm: requestId null
    page.ctx.lpRefetchAfterAction();
    await drain();
    const pendingHtml = htmlOf(page, '#lp-pending');
    check(pendingHtml.includes('github.com/o/r'),
      `the open confirmation survives as a #lp-pending orphan (got: ${pendingHtml})`);
    check(!maybeEl(page, '#lp-pending [data-lp-request-id]'),
      'an open confirm with no request_id yet offers no drill-down');
  }, pendingOrphanRoutes());
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
