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
//   4. the keyboard contract of design §4: Left/Right move FOCUS along the
//      strip without opening anything (manual activation, roving tabindex),
//      Enter/Space opens the focused one;
//   5. the ARIA wiring of every tab/panel pair, and the tab ORDER — SCREEN_IDS
//      below is a literal list precisely so the order is pinned by a test and
//      not only by the page's own registry;
//   6. design §3.3's single exception: the global `Unresolved task requests`
//      band lives outside every panel, so the outcome of a sent mutation is
//      not destroyed by switching tabs;
//   7. Task 5's per-screen loaders: opening a screen fetches ONLY that
//      screen's endpoints, the 10s timer refreshes only the active screen,
//      a screen whose endpoint is broken does not blank its neighbour, and
//      Roadmap still folds its `Contract` column out of /api/contracts.
//   8. Task 7's CONDITIONAL tenth tab: `Benchmarks` is in the strip when and
//      only when the eco-profile is configured, one boot request decides it,
//      no other screen pays for it, and losing the profile takes the operator
//      off a screen whose tab has just gone.
//
// Usage: node tabs_harness.js <path-to-index.html>
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Document, dispatch} = require(path.join(__dirname, 'dom.js'));
const {browserGlobals, openScreen, overrideRoute} = require(path.join(__dirname, 'screens.js'));

const HTML_PATH = process.argv[2];
if (!HTML_PATH) {
  console.error('usage: node tabs_harness.js <index.html>');
  process.exit(2);
}

// The nine UNCONDITIONAL screens, in registry order. Literal on purpose: the
// order of the tab strip is a contract of design §3.1, and a list derived
// from the page could not fail. `benchmarks` is Task 7's conditional tenth
// and is deliberately NOT here: this list is what an unconfigured stand
// shows, so the count assertions below stay meaningful.
const SCREEN_IDS = [
  'launchpad', 'sync', 'projects', 'errors', 'models',
  'contracts', 'epics', 'waits', 'roadmap',
];
// The case that makes the paragraph above true rather than merely intended:
// `the visible tabs are exactly this list, in this order` (Task 7 block).
// Every OTHER use of SCREEN_IDS below is order-insensitive.
// Every panel the page ships, conditional one included: structural
// assertions (ARIA wiring, one-visible-panel, `#ta-outcomes` placement) hold
// for the tenth screen whether or not its tab is currently offered.
const ALL_SCREEN_IDS = [...SCREEN_IDS, 'benchmarks'];

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

// The per-screen loaders (index.html's LOADERS) reach these endpoints, one
// screen at a time, same set as launchpad_harness.js's defaultRoutes — every
// whole-script harness needs all of them fixture'd, because any case here may
// open any tab and a missing route is a rejected fetch, not a skipped one.
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
    const visible = ALL_SCREEN_IDS.filter(
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

// ---- keyboard: design §4's MANUAL activation --------------------------------
//
// "Left/Right перемещают фокус, Enter/Space открывают экран". The shipped
// handler used to navigate() on arrow, and to compute the neighbour from
// `route.screen` rather than the focused tab — two separate breaches, each
// pinned by its own case below. `page.document.activeElement` is real only
// because dom.js models focus (dom_selftest.js cases 11-13); before that it
// was a no-op and every one of these assertions would have been vacuous.

/** Sends `key` to the tab that currently has focus, as a person would. */
async function press(page, key) {
  dispatch(page.document.activeElement, 'keydown', {init: {key}});
  await drain();
}
/** The id of the focused tab, or null when focus is not on one. */
function focusedTab(page) {
  const active = page.document.activeElement;
  const id = active && active.attributes && active.attributes.id;
  return id && id.startsWith('tab-') ? id.slice(4) : null;
}
/** The ids of every tab carrying `tabindex="0"` — a roving tabindex keeps
 * this at exactly one, so a list is what makes "exactly one" checkable. */
function tabStops(page) {
  return ALL_SCREEN_IDS.filter(
    id => page.document.getElementById(`tab-${id}`).attributes.tabindex === '0');
}
/** The screen whose panel is showing. */
function openScreenId(page) {
  return ALL_SCREEN_IDS.find(
    id => !page.document.getElementById(`screen-${id}`).hidden) || null;
}

testCase('arrow keys move FOCUS along the strip and open nothing', async () => {
  await withPage(async page => {
    el(page, '#tab-launchpad').focus();
    await press(page, 'ArrowRight');
    check(focusedTab(page) === 'sync', `ArrowRight focused sync, got ${focusedTab(page)}`);
    check(openScreenId(page) === 'launchpad',
      `launchpad is still the open screen, got ${openScreenId(page)}`);
    await press(page, 'ArrowLeft');
    check(focusedTab(page) === 'launchpad',
      `ArrowLeft focused launchpad, got ${focusedTab(page)}`);
    check(openScreenId(page) === 'launchpad', 'still on launchpad');
  });
});

// The terminal review's exact scenario: Tab to `Epics` WITHOUT activating it,
// then ArrowRight. The old handler read `route.screen` (launchpad) and opened
// `Sync`; the neighbour must be `Waits`, right of the FOCUSED tab.
testCase('arrows move relative to the focused tab, not the active screen',
  async () => {
    await withPage(async page => {
      el(page, '#tab-epics').focus();
      await press(page, 'ArrowRight');
      check(focusedTab(page) === 'waits',
        `focus went right of epics, got ${focusedTab(page)}`);
      check(openScreenId(page) === 'launchpad',
        `launchpad is still open, got ${openScreenId(page)}`);
    });
  });

testCase('arrowing across the strip fires no loader', async () => {
  await withPage(async page => {
    const before = page.calls.length;
    el(page, '#tab-launchpad').focus();
    for (let i = 0; i < 5; i++) await press(page, 'ArrowRight');
    check(focusedTab(page) === 'contracts',
      `five ArrowRights reached contracts, got ${focusedTab(page)}`);
    check(page.calls.length === before,
      `no fetch while arrowing, got ${page.calls.length - before}`);
    check(openScreenId(page) === 'launchpad', 'and no screen opened');
  });
});

testCase('Enter on the focused tab opens it, Space too', async () => {
  await withPage(async page => {
    el(page, '#tab-epics').focus();
    await press(page, 'Enter');
    check(openScreenId(page) === 'epics',
      `Enter opened epics, got ${openScreenId(page)}`);
    el(page, '#tab-models').focus();
    await press(page, ' ');
    check(openScreenId(page) === 'models',
      `Space opened models, got ${openScreenId(page)}`);
  });
});

testCase('the roving tabindex follows focus, and activation resets it to the '
  + 'selected tab', async () => {
  await withPage(async page => {
    check(tabStops(page).join(',') === 'launchpad',
      `one tab stop at boot, got [${tabStops(page).join(',')}]`);
    el(page, '#tab-launchpad').focus();
    await press(page, 'ArrowRight');
    check(tabStops(page).join(',') === 'sync',
      `the tab stop moved with focus, got [${tabStops(page).join(',')}]`);
    check(el(page, '#tab-launchpad').attributes.tabindex === '-1',
      'the screen that is still open is out of the tab order while unfocused');
    await press(page, 'Enter');
    check(tabStops(page).join(',') === 'sync',
      `after activation only the selected tab stops, got [${tabStops(page).join(',')}]`);
    // The selected tab and the tab stop are set in one loop, so they cannot
    // drift: open a third screen by CLICK and the stop follows the selection.
    await openScreen(page, 'roadmap');
    check(tabStops(page).join(',') === 'roadmap',
      `a click moved the tab stop too, got [${tabStops(page).join(',')}]`);
  });
});

// The static markup, BEFORE the script runs — a fresh Document over
// BODY_HTML with no PAGE_SCRIPT. Every other case here boots the page, and
// applyRoute() rewrites tabindex on its first call, so the shipped file's own
// values are unfalsifiable from a booted page: without this case the initial
// markup could say anything and the suite would stay green.
testCase('the shipped markup already carries the roving tabindex', () => {
  const raw = new Document(BODY_HTML);
  const stops = ALL_SCREEN_IDS.filter(
    id => raw.getElementById(`tab-${id}`).attributes.tabindex === '0');
  check(stops.join(',') === 'launchpad',
    `only the default screen's tab is a tab stop in markup, got [${stops.join(',')}]`);
  const wrong = ALL_SCREEN_IDS.filter(
    id => !['0', '-1'].includes(raw.getElementById(`tab-${id}`).attributes.tabindex));
  check(wrong.length === 0,
    `every tab declares a tabindex in markup, missing on [${wrong.join(',')}]`);
});

testCase('focus wraps at both ends of the strip', async () => {
  await withPage(async page => {
    el(page, '#tab-launchpad').focus();
    await press(page, 'ArrowLeft');
    check(focusedTab(page) === 'roadmap',
      `ArrowLeft off the first tab wraps to the last, got ${focusedTab(page)}`);
    await press(page, 'ArrowRight');
    check(focusedTab(page) === 'launchpad',
      `ArrowRight off the last wraps to the first, got ${focusedTab(page)}`);
    check(openScreenId(page) === 'launchpad', 'wrapping opened nothing');
  });
});

// Wrap-around WITH the conditional tenth tab lives in the Task 7 block below,
// next to the `configuredBenchmarksRoutes` fixture it needs.

testCase('every tab carries its ARIA wiring', async () => {
  await withPage(page => {
    for (const id of ALL_SCREEN_IDS) {
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
    for (const id of ALL_SCREEN_IDS) {
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

// ---- Whole-branch review, ITEM 1: the drill-down must also CLOSE ----------
//
// The open half was covered above; nothing covered the close, and nothing
// closed it. Two things follow from a view that stays open after the route
// stopped naming it: the 5s run poll keeps firing from behind another screen
// (a second, unnamed exception to §9.1's active-only polling, and unlike the
// real one it has no stop condition while the run is live), and `#launchpad`
// renders whatever `#launchpad/<id>` last opened — so Back does not close the
// drill-down (acceptance criterion 3).
//
// The default `/api/runs/` fixture is deliberately UNSETTLED
// (`state: 'materialized'`, `run: null`), which is exactly what arms the 5s
// timer — a settled fixture would make these cases pass for the wrong reason.

const RC_POLL_MS = 5000;

testCase('leaving Launchpad stops the run poll', async () => {
  await withPage(async page => {
    const armed = page.timers.byPeriod(RC_POLL_MS);
    check(!!armed, 'precondition: an unsettled drill-down armed the 5s poll');

    await openScreen(page, 'sync');

    check(page.timers.byPeriod(RC_POLL_MS) === null,
      'leaving the screen unregisters the run poll');
    // Belt and braces: even if a tick were somehow still delivered, the
    // closed view must have nothing left to fetch. Ticking the CAPTURED
    // callback is what a still-registered timer would do.
    const before = callsTo(page, '/api/runs/rc-active-1');
    if (armed) armed.cb();
    await drain();
    check(callsTo(page, '/api/runs/rc-active-1') === before,
      `a tick from another screen fetches nothing, went ${before} -> `
      + `${callsTo(page, '/api/runs/rc-active-1')}`);
  }, {hash: '#launchpad/rc-active-1'});
});

testCase('Back out of the drill-down closes it', async () => {
  await withPage(async page => {
    const row = el(page, '#lp-active [data-lp-request-id="rc-active-1"]');
    await Promise.all(dispatch(row, 'click'));
    await drain();
    check(htmlOf(page, '#rc-run-view') !== '',
      'precondition: the drill-down painted the run view');

    page.ctx.history.back();
    await drain();

    check(page.ctx.location.hash === '#launchpad',
      `Back landed on the bare screen, got ${page.ctx.location.hash}`);
    check(htmlOf(page, '#rc-run-view') === '',
      `#launchpad and #launchpad/<id> must not render alike, got `
      + `${htmlOf(page, '#rc-run-view')}`);
    check(page.timers.byPeriod(RC_POLL_MS) === null,
      'and the run poll went with it');
  }, {hash: '#launchpad', ...snapshotRoute({active: [ACTIVE_LINKED]})});
});

testCase('re-entering the drill-down re-opens it', async () => {
  await withPage(async page => {
    const row = el(page, '#lp-active [data-lp-request-id="rc-active-1"]');
    await Promise.all(dispatch(row, 'click'));
    await drain();
    page.ctx.history.back();
    await drain();
    check(htmlOf(page, '#rc-run-view') === '', 'precondition: Back closed it');

    page.ctx.history.forward();
    await drain();

    check(page.ctx.location.hash === '#launchpad/rc-active-1',
      `Forward returned to the drill-down, got ${page.ctx.location.hash}`);
    check(htmlOf(page, '#rc-run-view') !== '',
      'the run view is painted again');
    // Exact count: one fetch per open, and the close in between must not
    // have left a poll running that adds one of its own.
    check(callsTo(page, '/api/runs/rc-active-1') === 2,
      `two opens, two fetches (got ${callsTo(page, '/api/runs/rc-active-1')})`);
  }, {hash: '#launchpad', ...snapshotRoute({active: [ACTIVE_LINKED]})});
});

// ---- Whole-branch review, ITEM 2: `#updated` belongs to the ACTIVE screen -
//
// `loadActiveScreen` awaited a loader and stamped unconditionally, so a slow
// screen's answer could stamp "updated <now>" onto the screen the operator
// had already switched to, before that screen had painted anything.

testCase('a slow loader does not stamp #updated onto the screen that '
  + 'replaced it', async () => {
  await withPage(async page => {
    let releaseSync = null;
    overrideRoute(page, '/api/sync', () => new Promise(resolve => {
      releaseSync = () => resolve(ok({
        fetch_in_flight: false,
        report: {top_line: 'ok', top_reason: null, proposals: [], hosts: []},
      }));
    }));
    // Sync's loader is now stuck mid-flight; openScreen would hang on it, so
    // the switch is driven through the same real tab buttons by hand.
    await Promise.all(dispatch(el(page, '#tab-sync'), 'click'));
    await drain();
    check(!!releaseSync, 'precondition: the sync read is in flight');

    await Promise.all(dispatch(el(page, '#tab-epics'), 'click'));
    await drain();
    check(!el(page, '#screen-epics').hidden, 'precondition: epics is active');
    check(/^updated /.test(el(page, '#updated').textContent),
      `precondition: the ACTIVE screen did stamp, got `
      + `"${el(page, '#updated').textContent}"`);
    // A SENTINEL, not the epics stamp itself: both stamps are
    // `toLocaleTimeString()` and would land in the same second, so comparing
    // the two strings would pass whether or not the guard exists.
    const SENTINEL = 'sentinel-not-a-stamp';
    el(page, '#updated').textContent = SENTINEL;

    if (releaseSync) releaseSync();
    await drain();

    check(el(page, '#updated').textContent === SENTINEL,
      `the screen the operator left must not restamp #updated, got `
      + `"${el(page, '#updated').textContent}"`);
  });
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

// ---- Task 5: per-screen loaders — only the active screen is fetched ------
//
// The trap these cases exist for: the Roadmap table's `Contract` column is
// folded from /api/contracts (renderRoadmap's syncByName), NOT from
// /api/roadmap. A split along endpoint lines instead of screen lines empties
// that column with every other assertion still green.

/** A contract row the Roadmap column can actually fold: renderRoadmap
 * ignores every `kind` but `upstream_drift`. */
const CONTRACT_IN_SYNC = {
  name: 'gate-catalog', kind: 'upstream_drift',
  canonical_path: '/canon/gate-catalog.toml',
  vendored_path: '/vendor/gate-catalog.toml',
  in_sync: true, detail: null,
};
const ROADMAP_ITEM = {
  phase: 'M1', id: 'R-1', title: 'a roadmap item', owner_project: 'dispatcher',
  computed_status: 'implemented', status_label: 'implemented',
  implementation_is_attested_only: false, target_contract: 'gate-catalog',
  blockers: [], evidence: [], last_seen: '2026-08-29',
};

/** Boot opts whose roadmap item names a contract only /api/contracts can
 * resolve — matched EXACTLY (`u === '/api/roadmap'`) so the prepended route
 * cannot also swallow `/api/roadmap/summary`. */
const roadmapWithContractRoute = {routes: [
  [u => u === '/api/roadmap', () => ok({roadmaps: ['r'], items: [ROADMAP_ITEM]})],
  [u => u === '/api/contracts', () => ok([CONTRACT_IN_SYNC])],
]};
const contractsRoute = {routes: [
  [u => u === '/api/contracts', () => ok([CONTRACT_IN_SYNC])],
]};
/** One detected project, plus a blanket 404 for every per-project endpoint
 * detail() reaches for — this suite is about the Errors filter, not the
 * detail panel, and an unrouted fetch would surface as a rejection. */
const projectsRoute = {routes: [
  [u => u.startsWith('/api/overview'), () => ok({projects: [{
    name: 'widget', detected: true, path: '/repos/widget',
    counts: {tasks: 0, models: 0, test_results: 0, errors: 0},
    freshness: 'fresh', warnings: [],
  }]})],
  [u => u.startsWith('/api/projects/'), () => resp(404, {detail: 'none'})],
]};

const urlsSince = (page, before) => page.calls.slice(before).map(c => c.url);

testCase('opening a screen fetches only its own endpoints', async () => {
  await withPage(async page => {
    const before = page.calls.length;
    await openScreen(page, 'models');
    const after = urlsSince(page, before);
    // Exact count, not `.some()`: `.some()` has already let a double-fetch
    // defect through this branch once, and the whole point of this task is
    // what is NOT fetched.
    check(after.length === 1 && after[0].startsWith('/api/models'),
      `only /api/models is fetched for the models screen, saw ${after.join(',') || 'nothing'}`);
  });
});

testCase('the periodic timer only refreshes the active screen', async () => {
  await withPage(async page => {
    await openScreen(page, 'contracts');
    const before = page.calls.length;
    const timer = page.timers.byPeriod(10000);
    check(!!timer, 'a 10000ms screen timer is registered');
    if (!timer) return;
    timer.cb();
    await drain();
    const urls = urlsSince(page, before);
    check(urls.length === 1 && urls[0].startsWith('/api/contracts'),
      `only contracts refreshed, saw ${urls.join(',') || 'nothing'}`);
  });
});

testCase('a Launchpad tick fetches nothing — that screen owns its own timer',
  async () => {
  await withPage(async page => {
    const timer = page.timers.byPeriod(10000);
    check(!!timer, 'the screen timer is registered on the launchpad boot too');
    if (!timer) return;
    const before = page.calls.length;
    timer.cb();
    await drain();
    check(page.calls.length === before,
      `LOADERS.launchpad is null, so the screen timer fetches nothing, saw ${urlsSince(page, before).join(',')}`);
  });
});

testCase('a broken endpoint on one screen leaves its neighbour alone', async () => {
  await withPage(async page => {
    overrideRoute(page, '/api/models', () => resp(500, {}));
    await openScreen(page, 'models');
    await openScreen(page, 'contracts');
    check(/in sync|drift|n\/a/.test(htmlOf(page, '#contracts')),
      `contracts rendered despite the models failure, got ${htmlOf(page, '#contracts')}`);
    check(/models unavailable/.test(htmlOf(page, '#models')),
      `the models screen names its own failure, got ${htmlOf(page, '#models')}`);
  }, contractsRoute);
});

testCase('the roadmap screen still fills its Contract column', async () => {
  await withPage(async page => {
    const before = page.calls.length;
    await openScreen(page, 'roadmap');
    const urls = urlsSince(page, before);
    check(urls.filter(u => u === '/api/contracts').length === 1,
      `the roadmap loader fetches /api/contracts exactly once, saw ${urls.join(',')}`);
    check(/✓ in sync/.test(htmlOf(page, '#roadmap')),
      `the Contract column is not empty, got ${htmlOf(page, '#roadmap')}`);
    check(urls.filter(u => u === '/api/roadmap/summary').length === 1,
      `and its summary exactly once, saw ${urls.join(',')}`);
  }, roadmapWithContractRoute);
});

/** A sync report with something to go stale: a verdict in the topline, a
 * reason, an in-flight indicator, and a CLICKABLE proposal. */
const syncWithProposalRoute = {routes: [
  [u => u === '/api/sync', () => ok({
    fetch_in_flight: true,
    report: {top_line: 'sync-first', top_reason: 'behind origin',
      proposals: ['newcomer'], hosts: []},
  })],
]};

testCase('a failed sync read stops every node renderSync owns from asserting '
  + 'the old state', async () => {
  await withPage(async page => {
    await openScreen(page, 'sync');
    check(/sync-first/.test(el(page, '#sync-topline').textContent),
      'precondition: the good read painted a verdict');
    check(!!maybeEl(page, '#sync-proposals button[data-track]'),
      'precondition: the good read painted a clickable proposal');
    overrideRoute(page, '/api/sync', () => { throw new Error('transport down'); });
    page.timers.byPeriod(10000).cb();
    await drain();
    // NFR-02: no node may keep claiming the previous read's answer, and a
    // stale proposal is worse than a stale label — it can be ACTED on.
    check(!/sync-first/.test(el(page, '#sync-topline').textContent),
      `no stale verdict survives, got "${el(page, '#sync-topline').textContent}"`);
    check(el(page, '#sync-reason').textContent === '',
      `the stale reason is cleared, got "${el(page, '#sync-reason').textContent}"`);
    check(el(page, '#sync-fetch').hidden === true,
      'the stale in-flight indicator is cleared');
    check(!maybeEl(page, '#sync-proposals button[data-track]'),
      `no clickable stale proposal survives, got ${htmlOf(page, '#sync-proposals')}`);
    check(/sync failed/.test(el(page, '#sync-hosts').textContent),
      `the failure itself is named, got "${el(page, '#sync-hosts').textContent}"`);
  }, syncWithProposalRoute);
});

testCase('a project card records the Errors filter without fetching the '
  + 'hidden Errors screen', async () => {
  await withPage(async page => {
    await openScreen(page, 'projects');
    const before = page.calls.length;
    await click(page, '#projects .card[data-name]');
    check(urlsSince(page, before).every(u => !u.startsWith('/api/errors')),
      `nothing is fetched for a hidden screen, saw ${urlsSince(page, before).join(',')}`);
    const beforeOpen = page.calls.length;
    await openScreen(page, 'errors');
    const errorCalls = urlsSince(page, beforeOpen)
      .filter(u => u.startsWith('/api/errors'));
    check(errorCalls.length === 1 && errorCalls[0].includes('project=widget'),
      `opening Errors fetches once, carrying the recorded filter, saw ${errorCalls.join(',') || 'nothing'}`);
  }, projectsRoute);
});

// ---- Task 7: the conditional tenth tab -----------------------------------
//
// The tab BUTTON stays in the static markup and is toggled with `hidden` —
// nothing else on this page renders navigation dynamically. A hidden button
// is out of the accessibility tree and dispatch() refuses to click it, so
// "present in the tablist" is measured over VISIBLE tabs, not over the DOM.

const visibleTabs = page =>
  page.document.querySelectorAll('#tablist button').filter(b => !b.hidden);

/** A configured eco-profile: exactly the report shape whose `status` used to
 * un-hide `#benchmarks-section` on its own (renderBenchmarks). */
const BENCH_CONFIGURED = {
  fetch_in_flight: false,
  report: {status: 'ok', url: 'https://bench.example/api',
    fetched_at: '2026-08-29T00:00:00Z', error: null,
    benchmarks: [], leaderboards: {}},
};
const BENCH_UNCONFIGURED = {
  fetch_in_flight: false,
  report: {status: 'unconfigured', url: null, fetched_at: null, error: null,
    benchmarks: [], leaderboards: {}},
};
/** Matched EXACTLY so it cannot also swallow `/api/benchmarks/runs/<id>`. */
const configuredBenchmarksRoutes = {routes: [
  [u => u === '/api/benchmarks', () => ok(BENCH_CONFIGURED)],
]};

testCase('no Benchmarks tab when the profile is unconfigured', async () => {
  await withPage(page => {
    check(el(page, '#tab-benchmarks').hidden, 'the benchmarks tab is hidden');
    check(visibleTabs(page).length === 9,
      `nine visible tabs, got ${visibleTabs(page).length}`);
    check(el(page, '#screen-benchmarks').hidden, 'and its panel stays closed');
  });
});

testCase('an unconfigured stand cannot be routed onto Benchmarks at all',
  async () => {
  // The closed grammar of §4.1 covers the conditional screen too: a stale
  // bookmark to a tab that is not offered is an unknown screen.
  await withPage(page => {
    check(!el(page, '#screen-launchpad').hidden, 'fell back to launchpad');
    check(el(page, '#screen-benchmarks').hidden, 'the panel did not open');
  }, {hash: '#benchmarks'});
});

const tabIds = page =>
  visibleTabs(page).map(b => b.attributes.id.replace(/^tab-/, ''));

testCase('the visible tabs are exactly SCREEN_IDS, in that order', async () => {
  // The one order-sensitive assertion in this file: every other use of
  // SCREEN_IDS is a set, so without this case the literal list above would
  // pin nothing and a reordered strip would ship green.
  await withPage(page => {
    check(tabIds(page).join(',') === SCREEN_IDS.join(','),
      `nine tabs in registry order, got ${tabIds(page).join(',')}`);
  });
  await withPage(page => {
    const expected = [...SCREEN_IDS, 'benchmarks'].join(',');
    check(tabIds(page).join(',') === expected,
      `the conditional tab appends, got ${tabIds(page).join(',')}`);
  }, configuredBenchmarksRoutes);
});

testCase('a configured profile adds the Benchmarks tab last', async () => {
  await withPage(async page => {
    const tabs = visibleTabs(page);
    check(tabs.length === 10, `ten visible tabs, got ${tabs.length}`);
    check(tabs.length === 10 && tabs[tabs.length - 1].attributes.id === 'tab-benchmarks',
      'benchmarks is last');
    await openScreen(page, 'benchmarks');
    check(!el(page, '#screen-benchmarks').hidden, 'the benchmarks panel opened');
    check(el(page, '#screen-launchpad').hidden,
      'and it is the only panel — launchpad closed behind it');
  }, configuredBenchmarksRoutes);
});

// The conditional tenth tab changes what "the last tab" means for the
// keyboard: wrapping is computed over screenIds(), not SCREENS, so it must
// skip `benchmarks` on an unconfigured stand (the wrap case in the keyboard
// block above) and include it here.
testCase('focus wrap-around includes Benchmarks when the profile is configured',
  async () => {
    await withPage(async page => {
      check(!el(page, '#tab-benchmarks').hidden, 'the tenth tab is offered');
      el(page, '#tab-launchpad').focus();
      await press(page, 'ArrowLeft');
      check(focusedTab(page) === 'benchmarks',
        `ArrowLeft wraps to benchmarks, got ${focusedTab(page)}`);
      await press(page, 'ArrowRight');
      check(focusedTab(page) === 'launchpad',
        `and back off the end to launchpad, got ${focusedTab(page)}`);
      el(page, '#tab-roadmap').focus();
      await press(page, 'ArrowRight');
      check(focusedTab(page) === 'benchmarks',
        `roadmap's right neighbour is now benchmarks, got ${focusedTab(page)}`);
      check(openScreenId(page) === 'launchpad', 'and still nothing opened');
    }, configuredBenchmarksRoutes);
  });

testCase('one boot request decides the tab, and no other screen pays for it',
  async () => {
  await withPage(async page => {
    check(callsTo(page, '/api/benchmarks') === 1,
      `exactly one boot request decides the tab, got ${callsTo(page, '/api/benchmarks')}`);
    const before = page.calls.length;
    await openScreen(page, 'models');
    await openScreen(page, 'roadmap');
    page.timers.byPeriod(10000).cb();
    await drain();
    check(!urlsSince(page, before).includes('/api/benchmarks'),
      `no other screen fetches /api/benchmarks, saw ${urlsSince(page, before).join(',')}`);
  }, configuredBenchmarksRoutes);
});

testCase('the active Benchmarks screen refreshes on its own loader', async () => {
  await withPage(async page => {
    await openScreen(page, 'benchmarks');
    const before = page.calls.length;
    page.timers.byPeriod(10000).cb();
    await drain();
    const urls = urlsSince(page, before);
    check(urls.length === 1 && urls[0] === '/api/benchmarks',
      `only benchmarks refreshed, saw ${urls.join(',') || 'nothing'}`);
  }, configuredBenchmarksRoutes);
});

testCase('a direct #benchmarks hash opens the screen once the answer arrives',
  async () => {
  // The boot answer that creates the tab arrives AFTER the hash is parsed,
  // so the address bar has to be re-resolved — otherwise a bookmark to the
  // conditional screen could never open it.
  await withPage(page => {
    check(!el(page, '#screen-benchmarks').hidden,
      'the bookmarked screen opened');
    check(el(page, '#tab-benchmarks').attributes['aria-selected'] === 'true',
      'and its tab is the selected one');
  }, {...configuredBenchmarksRoutes, hash: '#benchmarks'});
});

testCase('losing the profile drops the active Benchmarks screen to Launchpad',
  async () => {
  await withPage(async page => {
    await openScreen(page, 'benchmarks');
    overrideRoute(page, '/api/benchmarks', () => ok(BENCH_UNCONFIGURED));
    page.timers.byPeriod(10000).cb();
    await drain();
    check(!el(page, '#screen-launchpad').hidden, 'fell back to launchpad');
    check(el(page, '#screen-benchmarks').hidden, 'the panel closed behind it');
    check(el(page, '#tab-benchmarks').hidden, 'the tab is hidden again');
    check(visibleTabs(page).length === 9,
      `back to nine visible tabs, got ${visibleTabs(page).length}`);
  }, configuredBenchmarksRoutes);
});

testCase('a failed benchmarks read leaves the tab the operator is standing on',
  async () => {
  // A transport failure is not an answer about configuration: yanking the
  // screen out from under the operator on a blip would be worse than a
  // panel that names its own failure (which loadBenchmarks already does).
  await withPage(async page => {
    await openScreen(page, 'benchmarks');
    overrideRoute(page, '/api/benchmarks', () => { throw new Error('transport down'); });
    page.timers.byPeriod(10000).cb();
    await drain();
    check(!el(page, '#screen-benchmarks').hidden, 'the screen is still open');
    check(!el(page, '#tab-benchmarks').hidden, 'and its tab is still offered');
    check(/benchmarks unavailable/.test(el(page, '#benchmarks-status').textContent),
      `the failure itself is named, got "${el(page, '#benchmarks-status').textContent}"`);
  }, configuredBenchmarksRoutes);
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
