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
    // Added with the loader-outcome cases: `/api/waits` was the one LOADERS
    // endpoint this list missed, so every case that opened Waits without
    // overriding the route was really exercising the FAILURE path (a
    // missing route is a rejected fetch) while looking like a good read.
    [u => u.startsWith('/api/waits'), () => ok(WAITS_VIEW)],
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

/** A recording, non-firing setInterval/setTimeout: every registration is
 * kept and NOTHING fires on its own, so no case here races a real clock.
 *
 * setTimeout is recorded for the same reason the interval is (fix round 2):
 * the page's ONE setTimeout is the 800 ms reload after a Sync host-action,
 * and a case must be able to fire it on demand rather than sleep through it.
 * `drain()` above uses the harness's own module-scope setTimeout, not this
 * one, so recording here does not stall the run. */
function makeIntervalRecorder() {
  const registered = new Map();
  const timeouts = new Map();
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
    setTimeout(cb, delay) {
      const id = nextId++;
      timeouts.set(id, {cb, delay});
      return id;
    },
    clearTimeout(id) { timeouts.delete(id); },
    /** The recorded callback scheduled for exactly `delay` ms, or null. */
    timeoutByDelay(delay) {
      for (const t of timeouts.values()) if (t.delay === delay) return t;
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
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
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
function fill(page, selector, value) { el(page, selector).value = value; }

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

// ---- terminal review 7: the nested segment is NOT a general form --------
//
// `HASH_RE` is one pattern for every screen, and `parseHash` used to check
// only that the screen existed — so `#sync/foo` opened Sync and ran its
// loader on an address spec §4.1 does not define. Nothing read the segment,
// so nothing visibly broke; the point is that a closed grammar is the thing
// keeping a hash value from becoming a surface, and seven screens were
// accepting 200 arbitrary characters they had no use for.
//
// Driven through the real router (a boot hash), not by calling parseHash.
const SUBLESS_SCREENS = ['sync', 'errors', 'models', 'contracts', 'epics',
  'waits', 'roadmap'];

testCase('a nested segment on a screen that has none falls back to Launchpad',
  async () => {
  for (const id of SUBLESS_SCREENS) {
    await withPage(page => {
      check(!el(page, '#screen-launchpad').hidden,
        `#${id}/foo is an unknown address, so it falls back to launchpad`);
      check(el(page, `#screen-${id}`).hidden,
        `and #screen-${id} did not open`);
    }, {hash: `#${id}/foo`});
  }
});

testCase('the two screens §4.1 DOES give nested state still take it', async () => {
  // The other half: tightening the grammar must not cost the addresses the
  // spec names. Launchpad's segment is consumed (the drill-down opens);
  // Projects' is reserved and unread, so the assertion is that the SCREEN
  // still opens rather than falling back — which is exactly what would
  // break if the marker were dropped from it.
  await withPage(page => {
    check(!el(page, '#screen-launchpad').hidden, 'launchpad opened');
    check(!el(page, '#rc-run-view').hidden,
      'and its drill-down opened from the segment');
  }, {hash: '#launchpad/rc-1'});
  await withPage(page => {
    check(!el(page, '#screen-projects').hidden,
      '#projects/<directory> still resolves to Projects');
  }, {hash: '#projects/deployer'});
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

// -- terminal review, PR #220: #rc-open (Manual) is a drill-down opener too -
// Only the row/button openers were exercised above. #rc-open used to call
// openRunView() directly, bypassing the hash entirely — so a run opened
// through the Manual form could not be restored by reload, and Back/Forward
// never knew it had been open at all. It now routes through lpOpenRun like
// every other opener, so the same Back/Forward contract must hold for it.

testCase('Back/Forward restore a run view opened via #rc-open (Manual), '
  + 'not just a row click', async () => {
  await withPage(async page => {
    fill(page, '#rc-request-id', 'rc-open-manual-1');
    await click(page, '#rc-open');
    check(page.ctx.location.hash === '#launchpad/rc-open-manual-1',
      `hash is the drill-down, got ${page.ctx.location.hash}`);
    check(htmlOf(page, '#rc-run-view') !== '',
      'precondition: #rc-open painted the run view');

    page.ctx.history.back();
    await drain();

    check(page.ctx.location.hash === '#launchpad',
      `Back landed on the bare screen, got ${page.ctx.location.hash}`);
    check(htmlOf(page, '#rc-run-view') === '',
      `#launchpad and #launchpad/<id> must not render alike, got `
      + `${htmlOf(page, '#rc-run-view')}`);

    page.ctx.history.forward();
    await drain();

    check(page.ctx.location.hash === '#launchpad/rc-open-manual-1',
      `Forward restores the drill-down opened via #rc-open, got `
      + `${page.ctx.location.hash}`);
    check(htmlOf(page, '#rc-run-view') !== '',
      'the run view opened via #rc-open is repainted, not lost to Back/Forward');
  }, {hash: '#launchpad'});
});

// ---- Whole-branch review, ITEM 2: `#updated` belongs to the ACTIVE screen -
//
// the screen-timer path awaited a loader and stamped unconditionally, so a slow
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

// ---- terminal review, PR #220: per-screen LOAD GENERATION (design §9) -----
//
// "переключение экрана инвалидирует поздние ответы прошлого там, где они
// могут менять общий DOM". Re-entering a screen issues a SECOND request
// while the first is still airborne (onScreenShown → runLoader), and
// a screen entry overlaps the 10s tick the same way. Whichever answered LAST
// used to paint.
//
// The pre-existing slow-loader case above cannot see this: it LEAVES a
// screen and never comes back, so it never has two responses of ONE loader
// to order, and after a return `applyOutcome`'s screen-id re-check
// matches by construction (the active screen IS sync again). Every case
// below returns to the origin screen, or overlaps entry with a tick, and
// settles the OLDER response LAST — the only ordering that fails.

/** A route whose every call PARKS: `pending[i]` resolves the i-th request,
 * so a case can answer requests in any order it likes — which is the whole
 * point here. `pending.length` doubles as "how many reads were issued". */
function deferrable() {
  const pending = [];
  return {
    pending,
    route: () => new Promise(resolve => pending.push(resolve)),
    /** Answers the i-th request (0-based) with `body`, 200 OK. */
    settle(i, body) { pending[i](ok(body)); },
  };
}

/** Like `overrideRoute`, but by PREFIX — for the loaders whose URL carries a
 * query string (Epics' `kind`), which an exact match would miss. */
function overridePrefix(page, prefix, make) {
  page.routes.unshift([u => u.startsWith(prefix), make]);
}

/** Clicks a tab WITHOUT waiting for its loader — `openScreen` is fine too,
 * but naming the intent keeps the parked-loader cases readable. */
async function clickTab(page, id) {
  await Promise.all(dispatch(el(page, `#tab-${id}`), 'click'));
  await drain();
}

/** The answer the operator must NOT be left looking at: a verdict, a reason,
 * and a proposal that renders as a live button over POST /api/sync/track. */
const SYNC_STALE = {
  fetch_in_flight: true,
  report: {top_line: 'sync-first', top_reason: 'stale reason',
    proposals: ['stale-newcomer'], hosts: []},
};
/** The fresher answer: clean, and with nothing to click. */
const SYNC_FRESH = {
  fetch_in_flight: false,
  report: {top_line: 'ok', top_reason: null, proposals: [], hosts: []},
};

/** Every assertion that the Sync screen shows SYNC_FRESH and no trace of
 * SYNC_STALE — shared by the two orderings below so they cannot drift. */
function checkSyncIsFresh(page, how) {
  check(el(page, '#sync-topline').textContent === 'ok',
    `${how}: the fresh verdict stands, got "${el(page, '#sync-topline').textContent}"`);
  check(el(page, '#sync-reason').textContent === '',
    `${how}: no stale reason, got "${el(page, '#sync-reason').textContent}"`);
  check(el(page, '#sync-fetch').hidden === true,
    `${how}: no stale in-flight indicator`);
  // The finding's sharpest edge: a superseded read used to re-create the
  // proposals as LIVE buttons whose click POSTs a real mutation.
  check(!maybeEl(page, '#sync-proposals button[data-track]'),
    `${how}: no stale, clickable proposal survives, got `
    + `${htmlOf(page, '#sync-proposals')}`);
}

testCase('returning to Sync: the first, slower read may not overwrite the '
  + 'second — and may not re-create a clickable proposal', async () => {
  await withPage(async page => {
    const sync = deferrable();
    overrideRoute(page, '/api/sync', sync.route);

    await clickTab(page, 'sync');
    check(sync.pending.length === 1,
      `precondition: one sync read is in flight, got ${sync.pending.length}`);

    await clickTab(page, 'models');
    await clickTab(page, 'sync');
    check(sync.pending.length === 2,
      `precondition: returning issues a SECOND read, got ${sync.pending.length}`);

    sync.settle(1, SYNC_FRESH);   // the NEWER request answers first…
    await drain();
    check(el(page, '#sync-topline').textContent === 'ok',
      `precondition: the second read painted, got `
      + `"${el(page, '#sync-topline').textContent}"`);

    sync.settle(0, SYNC_STALE);   // …and the OLDER one lands last
    await drain();

    checkSyncIsFresh(page, 'after the superseded read resolved');
  });
});

testCase('an entry read and a 10s tick overlap on Sync: the older answer, '
  + 'resolving last, still loses', async () => {
  await withPage(async page => {
    const sync = deferrable();
    overrideRoute(page, '/api/sync', sync.route);

    await clickTab(page, 'sync');
    check(sync.pending.length === 1,
      `precondition: the entry read is in flight, got ${sync.pending.length}`);
    const timer = page.timers.byPeriod(10000);
    check(!!timer, 'precondition: the 10s screen timer is registered');
    if (!timer) return;
    timer.cb();
    await drain();
    check(sync.pending.length === 2,
      `precondition: the tick issues a second read over the first, got `
      + `${sync.pending.length}`);

    sync.settle(1, SYNC_FRESH);
    await drain();
    sync.settle(0, SYNC_STALE);
    await drain();

    checkSyncIsFresh(page, 'entry vs tick');
  });
});

// Roadmap is the ALL-OR-NOTHING case: three endpoints feed one screen
// (/api/roadmap, /api/roadmap/summary, /api/contracts), so a guard applied
// per-fetch instead of once per load would let a stale summary sit above a
// fresh table. Each endpoint is parked separately and the stale load's three
// answers are settled INTERLEAVED, after the fresh load has painted.
const ROADMAP_STALE_ITEM = {...ROADMAP_ITEM, id: 'R-STALE', phase: 'M0'};
const ROADMAP_FRESH_ITEM = {...ROADMAP_ITEM, id: 'R-FRESH', phase: 'M9'};
const SUMMARY_STALE = {projects: [
  {project: 'stale-project', done: 1, total: 4, readiness: 0.25,
    lagging: true, contract_drift: true},
]};
const SUMMARY_FRESH = {projects: [
  {project: 'fresh-project', done: 4, total: 4, readiness: 1,
    lagging: false, contract_drift: false},
]};

testCase('Roadmap renders no partial stale mix: a superseded load contributes '
  + 'none of its three endpoints', async () => {
  await withPage(async page => {
    const roadmap = deferrable();
    const summary = deferrable();
    const contracts = deferrable();
    // Summary is registered LAST of the three so it sits ahead of the
    // `/api/roadmap` matcher in `page.routes` and cannot be swallowed by it.
    overrideRoute(page, '/api/roadmap', roadmap.route);
    overrideRoute(page, '/api/contracts', contracts.route);
    overrideRoute(page, '/api/roadmap/summary', summary.route);

    await clickTab(page, 'roadmap');
    check(roadmap.pending.length === 1 && summary.pending.length === 1
      && contracts.pending.length === 1,
      `precondition: the first load parked all three endpoints, got `
      + `${roadmap.pending.length}/${summary.pending.length}/${contracts.pending.length}`);

    await clickTab(page, 'models');
    await clickTab(page, 'roadmap');
    check(roadmap.pending.length === 2 && summary.pending.length === 2
      && contracts.pending.length === 2,
      `precondition: returning issued a second load of all three, got `
      + `${roadmap.pending.length}/${summary.pending.length}/${contracts.pending.length}`);

    // The SECOND load answers in full and paints.
    roadmap.settle(1, {roadmaps: ['fresh'], items: [ROADMAP_FRESH_ITEM]});
    summary.settle(1, SUMMARY_FRESH);
    contracts.settle(1, [CONTRACT_IN_SYNC]);
    await drain();
    check(/R-FRESH/.test(htmlOf(page, '#roadmap')),
      `precondition: the second load painted, got ${htmlOf(page, '#roadmap')}`);

    // The FIRST load answers afterwards, its three endpoints interleaved.
    summary.settle(0, SUMMARY_STALE);
    contracts.settle(0, []);
    roadmap.settle(0, {roadmaps: ['stale'], items: [ROADMAP_STALE_ITEM]});
    await drain();

    check(el(page, '#roadmap-names').textContent === 'fresh',
      `the roadmap names stay fresh, got "${el(page, '#roadmap-names').textContent}"`);
    check(/R-FRESH/.test(htmlOf(page, '#roadmap'))
      && !/R-STALE/.test(htmlOf(page, '#roadmap')),
      `the table stays fresh, got ${htmlOf(page, '#roadmap')}`);
    check(/fresh-project/.test(htmlOf(page, '#roadmap-summary'))
      && !/stale-project/.test(htmlOf(page, '#roadmap-summary')),
      `the summary above it stays fresh — no half-stale screen, got `
      + `${htmlOf(page, '#roadmap-summary')}`);
    // /api/contracts feeds the Contract COLUMN (renderRoadmap's syncByName),
    // so a stale empty contracts answer landing alone would empty it.
    check(/✓ in sync/.test(htmlOf(page, '#roadmap')),
      `the folded Contract column stays fresh, got ${htmlOf(page, '#roadmap')}`);
  });
});

// -- the two screens that already carried guards: unchanged behaviour -------

const EPICS_VIEW = rows => ({
  generated_at: null, registry_path: '/ws/epics.toml', registry_ok: true,
  registry_diagnostics: [], programs: {}, planes: [], rows, defects: [],
});
const EPIC_ROW = (id, kind) => ({
  id, title: id, program: 'P', kind, status: 'open', planes: [], defects: {},
  last_activity_at: null, activity_sources: [],
});

testCase('Epics: the kind filter is not repainted by the unfiltered read it '
  + 'overlapped', async () => {
  // refreshEpics never had an in-flight gate — only its own catch. The
  // `kind` buttons call it directly, so a click while a screen tick is
  // airborne is a real overlap, and the tick's UNFILTERED answer landing
  // last would silently widen the list the operator just narrowed.
  await withPage(async page => {
    const epics = deferrable();
    overridePrefix(page, '/api/epics', epics.route);

    await clickTab(page, 'epics');
    check(epics.pending.length === 1,
      `precondition: the entry read is in flight, got ${epics.pending.length}`);

    await click(page, 'button[data-epic-kind="ecosystem"]');
    check(epics.pending.length === 2,
      `precondition: the filter click issued its own read, got ${epics.pending.length}`);
    const filtered = page.calls.filter(c => c.url.includes('kind=ecosystem'));
    check(filtered.length === 1,
      `precondition: and it carried the filter, saw ${filtered.map(c => c.url).join(',')}`);

    epics.settle(1, EPICS_VIEW([EPIC_ROW('E-ECO', 'ecosystem')]));
    await drain();
    epics.settle(0, EPICS_VIEW([EPIC_ROW('E-ECO', 'ecosystem'),
      EPIC_ROW('E-EXT', 'external')]));
    await drain();

    check(/E-ECO/.test(htmlOf(page, '#epics')),
      `the filtered answer still stands, got ${htmlOf(page, '#epics')}`);
    check(!/E-EXT/.test(htmlOf(page, '#epics')),
      `the superseded unfiltered read did not widen it back, got `
      + `${htmlOf(page, '#epics')}`);
  });
});

const WAITS_VIEW = {
  todo_plane: {state: 'read', repos_read: 3, detail: null},
  edges: [], loose_refs: [], findings: [], triggers: [], absent_repos: [],
};

testCase('Waits keeps its in-flight gate: a return that issues no request of '
  + 'its own does not strand the poll painting for it', async () => {
  // `waitsInFlight` makes a second call return EARLY, on the promise that
  // "the running poll will paint". The load generation is therefore bumped
  // AFTER that gate, never before: a bump by a call that issued no request
  // would supersede the only poll in flight and nothing would ever paint.
  await withPage(async page => {
    const waits = deferrable();
    overrideRoute(page, '/api/waits', waits.route);

    await clickTab(page, 'waits');
    check(waits.pending.length === 1,
      `precondition: one waits read is in flight, got ${waits.pending.length}`);

    await clickTab(page, 'models');
    await clickTab(page, 'waits');
    check(waits.pending.length === 1,
      `the in-flight gate still suppresses the second read, got `
      + `${waits.pending.length}`);

    waits.settle(0, WAITS_VIEW);
    await drain();

    check(el(page, '#waits-plane').textContent === '0 рёбер · 3 репо',
      `the surviving poll still paints for the screen that returned, got `
      + `"${el(page, '#waits-plane').textContent}"`);
    check(el(page, '#waits-disclaimer').textContent === '',
      `and it is not left under a failure notice, got `
      + `"${el(page, '#waits-disclaimer').textContent}"`);
  });
});

// ---- terminal review, PR #220: `#updated` may only claim what was PAINTED
//
// The defect: a loader caught its own failure, painted "<screen>
// unavailable", and returned normally, so its caller could not tell
// it from a success and stamped "updated <now>" beside it — an unread
// source wearing a freshness marker (NFR-02; criterion 11 of this spec). A
// load DROPPED by the generation guard did the same: it returns without
// painting, and the caller stamped over a panel that rendered nothing.
//
// Why the existing cases could not see it: 'a broken endpoint on one screen
// leaves its neighbour alone' asserts the error text and screen isolation
// and never looks at `#updated`; 'a slow loader does not stamp #updated
// onto the screen that replaced it' looks at `#updated` only for an answer
// from a screen the operator has LEFT — the screen-id check, a different
// guard. Every case below stays ON the screen, where the id check passes.
//
// SENTINEL, never a stamp-vs-stamp comparison: both stamps are
// `toLocaleTimeString()` and land in the same second, so comparing two of
// them passes with or without the fix (that is how one case on this branch
// went vacuous).
const SENTINEL = 'sentinel-not-a-stamp';
const UNREAD = 'не прочитано';
/** Parks `#updated` on a value no code path produces, so anything the page
 * writes afterwards is visible as a change and anything it leaves alone is
 * visible as the sentinel. */
function parkUpdated(page) {
  el(page, '#updated').textContent = SENTINEL;
  el(page, '#updated').className = '';
}
const updatedText = page => el(page, '#updated').textContent;

testCase('a FAILED load says so in #updated — no timestamp beside an unread '
  + 'screen', async () => {
  await withPage(async page => {
    await openScreen(page, 'models');
    check(/^updated /.test(updatedText(page)),
      `precondition: the good read stamped, got "${updatedText(page)}"`);
    parkUpdated(page);

    overrideRoute(page, '/api/models', () => { throw new Error('transport down'); });
    page.timers.byPeriod(10000).cb();
    await drain();

    check(/models unavailable/.test(htmlOf(page, '#models')),
      `precondition: the panel names its own failure, got ${htmlOf(page, '#models')}`);
    // The exact text, not merely "it changed": "changed" would also accept a
    // fresh timestamp, which is the whole defect.
    check(updatedText(page) === UNREAD,
      `#updated says the screen was not read, got "${updatedText(page)}"`);
    check(el(page, '#updated').className === 'err',
      `and says it in the page's failure vocabulary, got `
      + `"${el(page, '#updated').className}"`);
  });
});

testCase('a load that RECOVERS clears the unread marker as well as stamping',
  async () => {
  // The field is one node with two states; a good read that put a time on
  // it but left the `err` class would ship a red timestamp.
  await withPage(async page => {
    overrideRoute(page, '/api/models', () => { throw new Error('transport down'); });
    await openScreen(page, 'models');
    check(updatedText(page) === UNREAD,
      `precondition: the failed entry marked it unread, got "${updatedText(page)}"`);

    overrideRoute(page, '/api/models', () => ok([]));
    page.timers.byPeriod(10000).cb();
    await drain();

    check(/^updated /.test(updatedText(page)),
      `the recovered read stamps, got "${updatedText(page)}"`);
    check(el(page, '#updated').className === '',
      `and takes the red off, got "${el(page, '#updated').className}"`);
  });
});

testCase('a DROPPED-as-stale load leaves #updated untouched; the newer load '
  + 'stamps when it paints', async () => {
  // The reviewer's ordering, on a FIRST entry so the panel really has
  // painted nothing: entry read and 10s tick overlap, the OLDER answer
  // lands first and is correctly dropped by the generation guard — and used
  // to stamp anyway, putting "updated <now>" over an empty Sync panel.
  await withPage(async page => {
    const sync = deferrable();
    overrideRoute(page, '/api/sync', sync.route);

    await clickTab(page, 'sync');
    check(sync.pending.length === 1,
      `precondition: the entry read is in flight, got ${sync.pending.length}`);
    page.timers.byPeriod(10000).cb();
    await drain();
    check(sync.pending.length === 2,
      `precondition: the tick issued a second read over the first, got `
      + `${sync.pending.length}`);
    // The panel's static placeholder, untouched: this screen has never
    // painted, so a stamp here would date a read that produced nothing.
    check(el(page, '#sync-topline').textContent === '…',
      `precondition: nothing has painted on this screen yet, got `
      + `"${el(page, '#sync-topline').textContent}"`);
    parkUpdated(page);

    sync.settle(0, SYNC_STALE);   // the OLDER read answers first…
    await drain();

    check(el(page, '#sync-topline').textContent === '…',
      `precondition: its render was dropped as stale, got `
      + `"${el(page, '#sync-topline').textContent}"`);
    check(updatedText(page) === SENTINEL,
      `a load that painted nothing may not stamp, got "${updatedText(page)}"`);

    sync.settle(1, SYNC_FRESH);   // …and the load still in flight paints
    await drain();

    check(el(page, '#sync-topline').textContent === 'ok',
      `precondition: the newer read painted, got `
      + `"${el(page, '#sync-topline').textContent}"`);
    check(/^updated /.test(updatedText(page)),
      `and the paint — not the drop — is what stamps, got `
      + `"${updatedText(page)}"`);
  });
});

testCase('a superseded load may not stamp AFTER the newer one already did',
  async () => {
  // The mirror ordering: newer first, older last. The stamp is already
  // correct when the superseded answer lands, so a second stamp would
  // re-date a paint that happened earlier — freshness claimed for a read
  // that contributed nothing.
  await withPage(async page => {
    const sync = deferrable();
    overrideRoute(page, '/api/sync', sync.route);

    await clickTab(page, 'sync');
    await clickTab(page, 'models');
    await clickTab(page, 'sync');
    check(sync.pending.length === 2,
      `precondition: returning issued a SECOND read, got ${sync.pending.length}`);

    sync.settle(1, SYNC_FRESH);
    await drain();
    check(/^updated /.test(updatedText(page)),
      `precondition: the newer read stamped, got "${updatedText(page)}"`);
    parkUpdated(page);

    sync.settle(0, SYNC_STALE);
    await drain();

    check(updatedText(page) === SENTINEL,
      `the superseded read leaves #updated exactly as it found it, got `
      + `"${updatedText(page)}"`);
  });
});

testCase('a SUCCESSFUL load still stamps, on every unconditional screen',
  async () => {
  // The other half of the contract: the outcome report must not make the
  // ordinary path stop stamping. One entry per LOADERS screen, so a loader
  // that forgets to return LOAD_PAINTED is caught by name.
  await withPage(async page => {
    for (const screen of ['sync', 'projects', 'errors', 'models',
      'contracts', 'epics', 'waits', 'roadmap']) {
      parkUpdated(page);
      await openScreen(page, screen);
      check(/^updated /.test(updatedText(page)),
        `${screen} stamps on a good read, got "${updatedText(page)}"`);
      check(el(page, '#updated').className === '',
        `${screen} stamps without the failure class, got `
        + `"${el(page, '#updated').className}"`);
    }
  });
});

/** Parks each screen's endpoint(s) so its loads can be answered out of
 * order. `settle(i)` answers the i-th LOAD — Roadmap's three endpoints
 * together, since one load of that screen is all three. `waits` and `epics`
 * are absent: the first cannot have two loads in flight (its gate) and the
 * second is superseded by a filter click, not a tick — both have their own
 * cases below. */
const STALE_DRILL = {
  sync: p => {
    const d = deferrable();
    overrideRoute(p, '/api/sync', d.route);
    return {count: () => d.pending.length, settle: i => d.settle(i, SYNC_FRESH)};
  },
  projects: p => {
    const d = deferrable();
    overrideRoute(p, '/api/overview', d.route);
    return {count: () => d.pending.length, settle: i => d.settle(i, {projects: []})};
  },
  errors: p => {
    const d = deferrable();
    overridePrefix(p, '/api/errors', d.route);
    return {count: () => d.pending.length, settle: i => d.settle(i, [])};
  },
  models: p => {
    const d = deferrable();
    overrideRoute(p, '/api/models', d.route);
    return {count: () => d.pending.length, settle: i => d.settle(i, [])};
  },
  contracts: p => {
    const d = deferrable();
    overrideRoute(p, '/api/contracts', d.route);
    return {count: () => d.pending.length, settle: i => d.settle(i, [])};
  },
  roadmap: p => {
    const r = deferrable(), s = deferrable(), c = deferrable();
    // Summary registered LAST so it sits ahead of the `/api/roadmap`
    // matcher and cannot be swallowed by it (same order as the
    // partial-stale-mix case above).
    overrideRoute(p, '/api/roadmap', r.route);
    overrideRoute(p, '/api/contracts', c.route);
    overrideRoute(p, '/api/roadmap/summary', s.route);
    return {
      count: () => r.pending.length,
      settle: i => {
        r.settle(i, {roadmaps: [], items: []});
        s.settle(i, {projects: []});
        c.settle(i, []);
      },
    };
  },
};

testCase('a superseded load stamps nothing — on every loader a tick can '
  + 'supersede', async () => {
  // The structural half of the DROPPED case, one entry per loader: a loader
  // that returns PAINTED from its generation guard instead of STALE would
  // otherwise only be caught on whichever screen a hand-written case picked
  // (Roadmap's guard, the all-or-nothing one, had no case at all).
  for (const screen of Object.keys(STALE_DRILL)) {
    await withPage(async page => {
      const load = STALE_DRILL[screen](page);
      await clickTab(page, screen);
      check(load.count() === 1,
        `${screen}: precondition: the entry read is in flight, got ${load.count()}`);
      page.timers.byPeriod(10000).cb();
      await drain();
      check(load.count() === 2,
        `${screen}: precondition: the tick issued a second load, got ${load.count()}`);
      parkUpdated(page);

      load.settle(0);          // the older load, whose render is dropped
      await drain();
      check(updatedText(page) === SENTINEL,
        `${screen}: a dropped load leaves #updated alone, got `
        + `"${updatedText(page)}"`);

      load.settle(1);          // the newer one, which paints
      await drain();
      check(/^updated /.test(updatedText(page)),
        `${screen}: and the paint stamps, got "${updatedText(page)}"`);
    });
  }
});

/** The endpoint to break to make each LOADERS screen fail — by PREFIX where
 * the loader's URL carries a query string. Roadmap reads three endpoints;
 * breaking one is enough, its `Promise.all` rejects. `benchmarks` is absent
 * on purpose: breaking its boot request hides the conditional tab, so that
 * screen has its own case further down. */
const BREAK_ROUTE = {
  sync: p => overrideRoute(p, '/api/sync', FAILS),
  projects: p => overrideRoute(p, '/api/overview', FAILS),
  errors: p => overridePrefix(p, '/api/errors', FAILS),
  models: p => overrideRoute(p, '/api/models', FAILS),
  contracts: p => overrideRoute(p, '/api/contracts', FAILS),
  epics: p => overridePrefix(p, '/api/epics', FAILS),
  waits: p => overrideRoute(p, '/api/waits', FAILS),
  roadmap: p => overrideRoute(p, '/api/roadmap', FAILS),
};
function FAILS() { throw new Error('transport down'); }

testCase('every LOADERS screen reports its own failure to #updated', async () => {
  // The structural half of the cover: one entry per loader, so a loader
  // whose catch returns the WRONG outcome — or none — is caught by name
  // rather than by whichever screen a case happened to pick. A fresh page
  // per screen: breaking one endpoint must not be what makes the next
  // screen fail (Roadmap also reads /api/contracts).
  for (const screen of Object.keys(BREAK_ROUTE)) {
    await withPage(async page => {
      BREAK_ROUTE[screen](page);
      await openScreen(page, screen);
      check(updatedText(page) === UNREAD,
        `${screen}: a failed read is marked unread, got "${updatedText(page)}"`);
      check(el(page, '#updated').className === 'err',
        `${screen}: and carries the failure class, got `
        + `"${el(page, '#updated').className}"`);
    });
  }
});

// ---- fix round 1: the loaders reached DIRECTLY, not via the screen timer
//
// The Errors filters, the Epics `kind` buttons, the benchmark selector and
// the post-track Sync reload call a loader without going through
// the screen timer, so they used to throw the outcome away — the same
// defect one path over, on the most-clicked control of the Errors screen.
// Every case below drives a REAL control, never the loader by hand.

/** A configured profile with one benchmark, so the selector button
 * (`renderBenchmarks`'s `button[data-bench]`) actually exists to click. */
const BENCH_WITH_ROW = {
  fetch_in_flight: false,
  report: {status: 'ok', url: 'https://bench.example/api',
    fetched_at: '2026-08-29T00:00:00Z', error: null,
    benchmarks: [{id: 'b1', name: 'swe', version: '1', tasks_count: 3, tags: []}],
    leaderboards: {}},
};

/** Every control that reaches a loader DIRECTLY — the five call sites that
 * used to drop the outcome — with the screen it belongs to, the endpoint
 * that makes it fail, and the real click that drives it. One table so the
 * failure and success cases below cover all five by name, and a sixth
 * added later is one entry rather than two new cases. */
const DIRECT_CONTROLS = {
  'errors: the days filter': {
    screen: 'errors', breaks: '/api/errors', prefix: true,
    open: p => openScreen(p, 'errors'),
    act: p => click(p, '#errors-toggle'),
  },
  'errors: the service filter': {
    screen: 'errors', breaks: '/api/errors', prefix: true,
    open: p => openScreen(p, 'errors'),
    act: async p => {
      fill(p, '#errors-service', 'dispatcher');
      await Promise.all(dispatch(el(p, '#errors-service'), 'change'));
      await drain();
    },
  },
  'errors: clearing the project filter': {
    // The whole real flow: the filter is recorded by a card click on the
    // Projects screen, and the ✕ only exists once it is.
    screen: 'errors', breaks: '/api/errors', prefix: true,
    boot: projectsRoute,
    open: async p => {
      await openScreen(p, 'projects');
      await click(p, '#projects .card[data-name]');
      await openScreen(p, 'errors');
    },
    act: p => click(p, '#errors-clear'),
  },
  'epics: the kind filter': {
    screen: 'epics', breaks: '/api/epics', prefix: true,
    open: p => openScreen(p, 'epics'),
    act: p => click(p, 'button[data-epic-kind="ecosystem"]'),
  },
  'benchmarks: the benchmark selector': {
    screen: 'benchmarks', breaks: '/api/benchmarks',
    boot: {routes: [[u => u === '/api/benchmarks', () => ok(BENCH_WITH_ROW)]]},
    open: p => openScreen(p, 'benchmarks'),
    act: p => click(p, '#benchmarks-list button[data-bench]'),
  },
  'sync: the post-track reload': {
    // The proposal button POSTs a real mutation and then reloads the
    // screen; a reload that fails must not leave the pre-POST timestamp
    // standing over a topline that now reads "не прочитано".
    screen: 'sync', breaks: '/api/sync',
    boot: {routes: [
      [u => u === '/api/sync/track', () => ok({})],
      ...syncWithProposalRoute.routes,
    ]},
    open: p => openScreen(p, 'sync'),
    act: p => click(p, '#sync-proposals button[data-track]'),
  },
};

const breakRoute = (page, c) =>
  (c.prefix ? overridePrefix : overrideRoute)(page, c.breaks, FAILS);

testCase('a direct loader call that FAILS leaves no success stamp', async () => {
  for (const [name, c] of Object.entries(DIRECT_CONTROLS)) {
    await withPage(async page => {
      await c.open(page);
      check(/^updated /.test(updatedText(page)),
        `${name}: precondition: the screen entry stamped, got `
        + `"${updatedText(page)}"`);
      // The sentinel stands in for that stamp, so "the old stamp survived"
      // and "a new stamp was written" stay distinguishable — two
      // `toLocaleTimeString()` values in the same second would not be.
      parkUpdated(page);

      breakRoute(page, c);
      await c.act(page);

      check(updatedText(page) === UNREAD,
        `${name}: a failed click is marked unread, got "${updatedText(page)}"`);
      check(el(page, '#updated').className === 'err',
        `${name}: and carries the failure class, got `
        + `"${el(page, '#updated').className}"`);
    }, c.boot || {});
  }
});

testCase('a direct loader call that SUCCEEDS stamps normally', async () => {
  for (const [name, c] of Object.entries(DIRECT_CONTROLS)) {
    await withPage(async page => {
      await c.open(page);
      parkUpdated(page);
      await c.act(page);
      check(/^updated /.test(updatedText(page)),
        `${name}: a good click stamps, got "${updatedText(page)}"`);
      check(el(page, '#updated').className === '',
        `${name}: without the failure class, got `
        + `"${el(page, '#updated').className}"`);
    }, c.boot || {});
  }
});

// ---- fix round 2: the loader passed BY NAME to setTimeout ---------------
//
// `setTimeout(loadSync, 800)` — the delayed reload after a successful Sync
// host-action — was the ninth call site, and the one two sweeps missed,
// because a function passed by NAME does not match a grep for `loadSync(`.
// The operator runs a host-action, the reload 800 ms later fails, loadSync
// clears its panel and says "не прочитано" — and the previous read's
// timestamp stayed above it.

/** A live host with a behind-only `sync-first` verdict: the one shape that
 * renders a `button.act[data-act="pull"]` (renderSync's `actions`). */
const SYNC_WITH_HOST_ACTION = {
  fetch_in_flight: false,
  report: {
    top_line: 'sync-first', top_reason: null, proposals: [],
    hosts: [{
      host: 'h1', source: 'live', age_seconds: 0, stale: false,
      gh_error: null, error: null,
      verdicts: [{repo: 'alpha', verdict: 'sync-first', reason: '',
        branch: 'master', ahead: 0, behind: 2, dirty: false, is_kb: false}],
    }],
  },
};
const hostActionRoutes = {routes: [
  [u => u === '/api/sync', () => ok(SYNC_WITH_HOST_ACTION)],
  [u => u.startsWith('/api/actions/pull'),
    () => ok({ok: true, detail: 'fast-forwarded'})],
]};

/** Runs the host-action and returns the recorded 800 ms reload, unfired.
 * The action is driven through the real button, and the reload is left for
 * the caller to fire so the case controls when — and against what — it
 * lands. */
async function runHostAction(page) {
  await openScreen(page, 'sync');
  const btn = el(page, '#sync-hosts')
    .querySelector('button.act[data-act="pull"][data-dir="alpha"]');
  check(!!btn, 'precondition: the behind-only row offers a pull button');
  if (!btn) return null;
  await Promise.all(dispatch(btn, 'click'));
  await drain();
  check(el(page, '#sync-hosts')
    .querySelector('.act-result[data-for="alpha"]').textContent
    === '✓ fast-forwarded',
    'precondition: the action itself succeeded');
  const reload = page.timers.timeoutByDelay(800);
  check(!!reload, 'precondition: a successful action schedules the reload');
  return reload;
}

testCase("a host-action's delayed reload that FAILS leaves no success stamp",
  async () => {
  await withPage(async page => {
    const reload = await runHostAction(page);
    if (!reload) return;
    // The stamp the defect left standing: it belongs to the read that
    // painted the screen BEFORE the action, and the sentinel stands in for
    // it so a survivor and a fresh stamp stay distinguishable.
    parkUpdated(page);

    overrideRoute(page, '/api/sync', FAILS);
    reload.cb();
    await drain();

    check(el(page, '#sync-topline').textContent === 'не прочитано',
      `precondition: the reload failed and the panel says so, got `
      + `"${el(page, '#sync-topline').textContent}"`);
    check(updatedText(page) === UNREAD,
      `the delayed reload reports its failure, got "${updatedText(page)}"`);
    check(el(page, '#updated').className === 'err',
      `and carries the failure class, got "${el(page, '#updated').className}"`);
  }, hostActionRoutes);
});

testCase("a host-action's delayed reload that SUCCEEDS stamps", async () => {
  await withPage(async page => {
    const reload = await runHostAction(page);
    if (!reload) return;
    parkUpdated(page);

    reload.cb();
    await drain();

    check(el(page, '#sync-topline').textContent === 'sync-first',
      `precondition: the reload repainted the screen, got `
      + `"${el(page, '#sync-topline').textContent}"`);
    check(/^updated /.test(updatedText(page)),
      `the reload stamps, got "${updatedText(page)}"`);
    check(el(page, '#updated').className === '',
      `without the failure class, got "${el(page, '#updated').className}"`);
  }, hostActionRoutes);
});

testCase("a host-action's delayed reload landing on ANOTHER screen stamps "
  + 'nothing', async () => {
  // 800 ms is long enough to change tabs in. The reload belongs to Sync.
  await withPage(async page => {
    const reload = await runHostAction(page);
    if (!reload) return;
    await clickTab(page, 'models');
    parkUpdated(page);

    reload.cb();
    await drain();

    check(updatedText(page) === SENTINEL,
      `the Sync reload may not stamp over Models, got "${updatedText(page)}"`);
  }, hostActionRoutes);
});

testCase('a direct call whose answer arrives after the operator has LEFT '
  + 'stamps nothing', async () => {
  // The direct callers get the same screen-id check as the timer path: a
  // filter answer that lands on another screen is not that screen's news.
  await withPage(async page => {
    await openScreen(page, 'errors');
    const errors = deferrable();
    overridePrefix(page, '/api/errors', errors.route);
    await click(page, '#errors-toggle');
    check(errors.pending.length === 1,
      `precondition: the filter click issued a read, got ${errors.pending.length}`);

    await clickTab(page, 'models');
    parkUpdated(page);
    errors.settle(0, []);
    await drain();

    check(updatedText(page) === SENTINEL,
      `the Errors filter may not stamp over Models, got "${updatedText(page)}"`);
  });
});

testCase('the boot probe stamps nothing — it is not a screen load', async () => {
  // The one direct call that must keep discarding: it decides whether the
  // conditional tab exists, and it answers while the operator is on
  // Launchpad. `#updated` is blank at that point and must stay blank until
  // a screen actually paints.
  await withPage(async page => {
    check(!el(page, '#screen-launchpad').hidden,
      'precondition: the probe answered while Launchpad was open');
    check(!el(page, '#tab-benchmarks').hidden,
      'precondition: and it did decide the conditional tab');
    check(callsTo(page, '/api/benchmarks') === 1,
      `precondition: exactly the one probe request, got `
      + `${callsTo(page, '/api/benchmarks')}`);
    // The Launchpad's own snapshot is what stamps here, so the assertion is
    // that the field never says the Benchmarks screen was read: the probe
    // paints a panel the operator is not looking at.
    check(!/^не прочитано/.test(updatedText(page)),
      `the probe did not mark a screen unread, got "${updatedText(page)}"`);
    check(el(page, '#screen-benchmarks').hidden,
      'and the screen it read is still closed');
  }, configuredBenchmarksRoutes);
});

testCase('a FAILED load on the screen the operator LEFT does not mark the '
  + 'screen they moved to', async () => {
  // The screen-id re-check still comes first: an unread marker belongs to
  // the screen that could not be read, not to whatever is on screen when
  // its failure finally lands.
  await withPage(async page => {
    let failSync = null;
    overrideRoute(page, '/api/sync', () => new Promise((_, reject) => {
      failSync = () => reject(new Error('transport down'));
    }));
    await clickTab(page, 'sync');
    check(!!failSync, 'precondition: the sync read is in flight');

    await clickTab(page, 'epics');
    check(!el(page, '#screen-epics').hidden, 'precondition: epics is active');
    parkUpdated(page);

    failSync();
    await drain();

    check(updatedText(page) === SENTINEL,
      `the screen the operator left may not mark the one they are on, got `
      + `"${updatedText(page)}"`);
  });
});

// -- the two screens that already carried their own guards ------------------
//
// Both are in LOADERS and both had a guard BEFORE this change (refreshEpics
// its own catch, refreshWaits its in-flight gate on top of one), which is
// exactly why they need their own cases: the outcome report had to be added
// to them without changing what they do.

testCase('Epics keeps its behaviour: a failed read marks #updated unread, a '
  + 'superseded one leaves it alone', async () => {
  await withPage(async page => {
    await openScreen(page, 'epics');
    parkUpdated(page);
    overridePrefix(page, '/api/epics', () => { throw new Error('transport down'); });
    page.timers.byPeriod(10000).cb();
    await drain();
    check(/epics unavailable/.test(el(page, '#epics-registry').textContent),
      `precondition: the panel still names its own failure, got `
      + `"${el(page, '#epics-registry').textContent}"`);
    check(updatedText(page) === UNREAD,
      `a failed epics read is not fresh, got "${updatedText(page)}"`);

    // …and the `kind` filter's supersession still ends in exactly one stamp,
    // from the answer that actually painted.
    const epics = deferrable();
    overridePrefix(page, '/api/epics', epics.route);
    page.timers.byPeriod(10000).cb();
    await drain();
    await click(page, 'button[data-epic-kind="ecosystem"]');
    check(epics.pending.length === 2,
      `precondition: the filter click overlapped the tick, got ${epics.pending.length}`);
    epics.settle(1, EPICS_VIEW([EPIC_ROW('E-ECO', 'ecosystem')]));
    await drain();
    parkUpdated(page);
    epics.settle(0, EPICS_VIEW([EPIC_ROW('E-EXT', 'external')]));
    await drain();
    check(!/E-EXT/.test(htmlOf(page, '#epics')),
      `precondition: the superseded answer was dropped, got ${htmlOf(page, '#epics')}`);
    check(updatedText(page) === SENTINEL,
      `and dropping it stamped nothing, got "${updatedText(page)}"`);
  });
});

testCase('Waits keeps its behaviour: the in-flight gate stamps nothing, the '
  + 'poll it deferred to stamps when it paints', async () => {
  // `waitsInFlight` makes the second call return without issuing a request
  // at all — the one loader that can report "nothing happened here" without
  // a generation being superseded. It must land in the same bucket: no
  // stamp (it painted nothing) and no unread marker (nothing failed, and a
  // poll is a moment from painting).
  await withPage(async page => {
    const waits = deferrable();
    overrideRoute(page, '/api/waits', waits.route);

    await clickTab(page, 'waits');
    check(waits.pending.length === 1,
      `precondition: one waits read is in flight, got ${waits.pending.length}`);

    // Models paints and stamps on the way past, so the sentinel is parked
    // AFTER that switch and BEFORE the return — the return is the call the
    // gate suppresses, and the only write to `#updated` this case may see.
    await clickTab(page, 'models');
    parkUpdated(page);
    await clickTab(page, 'waits');
    check(waits.pending.length === 1,
      `precondition: the gate suppressed the second read, got ${waits.pending.length}`);
    check(updatedText(page) === SENTINEL,
      `the suppressed call neither stamps nor marks unread, got `
      + `"${updatedText(page)}"`);

    waits.settle(0, WAITS_VIEW);
    await drain();
    check(el(page, '#waits-plane').textContent === '0 рёбер · 3 репо',
      `precondition: the surviving poll painted, got `
      + `"${el(page, '#waits-plane').textContent}"`);
    check(/^updated /.test(updatedText(page)),
      `and the paint stamps, got "${updatedText(page)}"`);
  });
});

testCase('a failed Waits read marks #updated unread', async () => {
  await withPage(async page => {
    await openScreen(page, 'waits');
    parkUpdated(page);
    overrideRoute(page, '/api/waits', () => { throw new Error('transport down'); });
    page.timers.byPeriod(10000).cb();
    await drain();
    check(/waits unavailable/.test(el(page, '#waits-disclaimer').textContent),
      `precondition: the panel names its own failure, got `
      + `"${el(page, '#waits-disclaimer').textContent}"`);
    check(updatedText(page) === UNREAD,
      `a failed waits read is not fresh, got "${updatedText(page)}"`);
  });
});

testCase('the conditional Benchmarks loader reports its outcome too', async () => {
  // The tenth screen is in LOADERS like the rest, and its catch deliberately
  // leaves the operator standing on the tab (a transport blip is not an
  // answer about configuration) — which makes the timestamp the ONLY thing
  // that can tell them the panel in front of them was not re-read.
  await withPage(async page => {
    await openScreen(page, 'benchmarks');
    check(/^updated /.test(updatedText(page)),
      `precondition: a good benchmarks read stamps, got "${updatedText(page)}"`);
    parkUpdated(page);

    overrideRoute(page, '/api/benchmarks', () => { throw new Error('transport down'); });
    page.timers.byPeriod(10000).cb();
    await drain();

    check(!el(page, '#screen-benchmarks').hidden,
      'precondition: the failure still leaves the operator on the screen');
    check(updatedText(page) === UNREAD,
      `and the screen is marked unread rather than freshly stamped, got `
      + `"${updatedText(page)}"`);
  }, configuredBenchmarksRoutes);
});

testCase('a superseded Benchmarks load stamps nothing either', async () => {
  // Kept out of the STALE_DRILL loop because this screen cannot be parked
  // from boot: the tab only exists once the boot request has answered. So
  // it is opened on a good answer first, and only then made deferrable.
  await withPage(async page => {
    await openScreen(page, 'benchmarks');
    const bench = deferrable();
    overrideRoute(page, '/api/benchmarks', bench.route);
    const timer = page.timers.byPeriod(10000);
    timer.cb();
    await drain();
    timer.cb();
    await drain();
    check(bench.pending.length === 2,
      `precondition: two loads are in flight, got ${bench.pending.length}`);
    parkUpdated(page);

    bench.settle(0, BENCH_CONFIGURED);
    await drain();
    check(updatedText(page) === SENTINEL,
      `the dropped load leaves #updated alone, got "${updatedText(page)}"`);

    bench.settle(1, BENCH_CONFIGURED);
    await drain();
    check(/^updated /.test(updatedText(page)),
      `and the load that paints stamps, got "${updatedText(page)}"`);
  }, configuredBenchmarksRoutes);
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

// ---- ENFORCEMENT: there is exactly ONE way to run a loader ----------------
//
// Why a case and not a convention. The same defect — a call site that runs a
// loader and drops its outcome, leaving an unread screen under a "updated
// <time>" freshness marker — was found and fixed FOUR times running: the
// failure path, then eight direct calls, then a ninth passed BY NAME to
// `setTimeout` (invisible to a grep for `loadSync(`), then the Launchpad.
// Every fix was correct and every one left the next instance alive, because
// a reviewer noticing was the only thing between a new call site and the old
// mistake. The page's shape now makes the mistake unavailable — `runLoader`
// takes the screen id ONCE and uses it for both the registry lookup and the
// outcome — and this case makes writing the old way go RED IN CI instead of
// surviving until someone notices.
//
// It reads the SHIPPED script: the same `PAGE_SCRIPT` every other case in
// this file executes, not a copy that could drift out from under it.

/**
 * Blanks comments (to spaces, newlines kept so line numbers survive) and
 * marks every character that sits inside a string, template or regex
 * literal. Both halves are load-bearing: without comment blanking this
 * file's own prose — which names `loadSync(` a dozen times — would be
 * findings, and without literal marking the `"` inside the page's
 * `/[&<>"']/g` would open a phantom string and derail everything after it.
 *
 * Template `${…}` holes are scanned as CODE, not swallowed as literal, so a
 * loader called from inside an interpolation is still visible.
 */
function scanScript(src) {
  const chars = src.split('');
  const literal = new Array(src.length).fill(false);
  const unterminated = [];   // starts of quoted literals that never closed
  // KNOWN GAP, deliberately left: `+` and `-` are here so that `x = a +
  // /re/.test(s)` lexes, which costs the postfix case — in `n++ / 2` the
  // `+` before the `/` reads as "a regex may start here", so the division
  // masks to end of line and anything after it on that line is invisible to
  // this case. Dropping `+`/`-` would break real regex detection on the
  // page as it stands, and no `++ /` or `-- /` appears in it; this comment
  // is here so the next reader recognises the gap instead of rediscovering
  // it as a surprise.
  const REGEX_PREV = new Set([...'(,=:[!&|?{};+-*%~^<>']);
  const REGEX_WORD = new Set(['return', 'typeof', 'case', 'in', 'of', 'delete',
    'void', 'instanceof', 'new', 'do', 'else', 'yield', 'await', 'throw']);
  // A stack of frames so a template's `${…}` returns to code and back:
  // {k:'code', depth} | {k:'tpl'}.
  const stack = [{k: 'code', depth: 0}];
  let prev = '';   // last significant char seen in code — regex-vs-divide
  let word = '';   // identifier just closed in code — same decision
  let i = 0;
  const mark = (a, b) => { for (let k = a; k < b; k++) literal[k] = true; };

  while (i < src.length) {
    const top = stack[stack.length - 1];
    const c = src[i];
    const d = src[i + 1];

    if (top.k === 'tpl') {
      if (c === '\\') { mark(i, i + 2); i += 2; continue; }
      if (c === '`') { mark(i, i + 1); stack.pop(); prev = '`'; word = ''; i++; continue; }
      if (c === '$' && d === '{') {
        mark(i, i + 2);
        stack.push({k: 'code', depth: 0});
        prev = '{'; word = ''; i += 2; continue;
      }
      mark(i, i + 1); i++; continue;
    }

    // -- code frame
    if (c === '/' && d === '/') {
      while (i < src.length && src[i] !== '\n') { chars[i] = ' '; i++; }
      continue;
    }
    if (c === '/' && d === '*') {
      const end = src.indexOf('*/', i + 2);
      const stop = end === -1 ? src.length : end + 2;
      for (; i < stop; i++) if (src[i] !== '\n') chars[i] = ' ';
      continue;
    }
    if (c === '"' || c === "'") {
      const start = i;
      i++;
      // Bail at a raw newline. One inside a quoted literal is a SYNTAX ERROR
      // in JS, so this loses nothing legal — a line continuation is `\` +
      // newline and the escape branch eats it — and it is what keeps an
      // UNTERMINATED quote from masking every remaining line of the page as
      // string, which would sail this case through green while hiding
      // whatever came after. The unterminated literal is recorded instead of
      // swallowed, so the case can name the line.
      while (i < src.length && src[i] !== c && src[i] !== '\n') {
        i += src[i] === '\\' ? 2 : 1;
      }
      if (src[i] === c) i++; else unterminated.push(start);
      mark(start, Math.min(i, src.length));
      prev = c; word = ''; continue;
    }
    if (c === '`') { mark(i, i + 1); stack.push({k: 'tpl'}); i++; continue; }
    if (c === '/' && (prev === '' || REGEX_PREV.has(prev) || REGEX_WORD.has(word))) {
      const start = i;
      i++;
      let inClass = false;
      while (i < src.length) {
        const r = src[i];
        if (r === '\\') { i += 2; continue; }
        if (r === '[') inClass = true;
        else if (r === ']') inClass = false;
        else if (r === '/' && !inClass) break;
        else if (r === '\n') break;   // not a regex after all; bail out
        i++;
      }
      i++;
      while (i < src.length && /[a-z]/.test(src[i])) i++;   // flags
      mark(start, Math.min(i, src.length));
      prev = '/'; word = ''; continue;
    }
    if (c === '{') { top.depth++; prev = c; word = ''; i++; continue; }
    if (c === '}') {
      if (top.depth === 0 && stack.length > 1) {
        // The `}` that closes a template's `${…}` is part of the LITERAL,
        // like the `${` that opened it — not a code brace. Marking it is
        // what keeps `bodySpan`'s brace matching honest: without this, the
        // first interpolation inside a function body closed that body's
        // count early and the span stopped short, silently exempting
        // everything after it. `lpRenderFetchError` is the live example.
        mark(i, i + 1);
        stack.pop(); prev = '}'; word = ''; i++; continue;
      }
      top.depth--; prev = c; word = ''; i++; continue;
    }
    if (/[A-Za-z0-9_$]/.test(c)) {
      const start = i;
      while (i < src.length && /[A-Za-z0-9_$]/.test(src[i])) i++;
      word = src.slice(start, i);
      prev = src[i - 1];
      continue;
    }
    if (!/\s/.test(c)) { prev = c; word = ''; }
    i++;
  }
  if (stack.length !== 1 || stack[0].k !== 'code') {
    throw new Error('scanScript: the page script did not close every literal '
      + '— the scan is unreliable, so this case must not pass');
  }
  return {code: chars.join(''), literal, unterminated};
}

// The page script's first line, in index.html's own numbering, so a finding
// names a line the reader can open rather than a script-relative offset.
const SCRIPT_LINE0 = html.slice(0, html.indexOf(PAGE_SCRIPT)).split('\n').length;
const fileLine = idx => SCRIPT_LINE0 + PAGE_SCRIPT.slice(0, idx).split('\n').length - 1;
const sourceLine = idx => {
  const start = PAGE_SCRIPT.lastIndexOf('\n', idx) + 1;
  const end = PAGE_SCRIPT.indexOf('\n', idx);
  return PAGE_SCRIPT.slice(start, end === -1 ? undefined : end).trim();
};

/** The first match of `re` that is NOT inside a string/template/regex
 * literal. Every declaration lookup goes through this: a page string that
 * happens to contain `function loadErrors(` would otherwise move the
 * exemption onto the literal and report the REAL declaration as a finding
 * — a false positive, but a baffling one. */
function firstRealMatch(scan, re) {
  for (const m of scan.code.matchAll(re)) if (!scan.literal[m.index]) return m;
  return null;
}

/** How a loader may be declared. `function load*()` and the arrow form both
 * count: `const loadFoo = () => …` is a loader the moment it is written, and
 * a governed-names list that only knew the `function` form would quietly not
 * govern it. */
const declRe = name =>
  new RegExp(`\\b(?:function\\s+${name}\\s*\\(|(?:const|let|var)\\s+${name}\\s*=)`, 'g');

/** The `{ … }` span of `<kind> <name>`, by brace matching over scanned code
 * (so braces inside comments and literals cannot throw the count off), or
 * `null` when the page has no such declaration. Null rather than a throw
 * because a page that has LOST the entry point must produce a readable list
 * of findings — that is exactly the state this case is meant to describe. */
function bodySpan(scan, kind, name) {
  const at = firstRealMatch(scan, new RegExp(`${kind}\\s+${name}\\s*[=({]`, 'g'));
  if (!at) return null;
  const open = scan.code.indexOf('{', at.index);
  if (open === -1) return null;
  let depth = 0;
  for (let i = open; i < scan.code.length; i++) {
    if (scan.literal[i]) continue;
    if (scan.code[i] === '{') depth++;
    else if (scan.code[i] === '}' && --depth === 0) return [at.index, i + 1];
  }
  return null;
}

/** Every identifier this case governs: the loaders the registry names, plus
 * anything DECLARED in loader shape — in EITHER form, `function loadFoo()`
 * or `const loadFoo = () => …` — so a new loader is governed the moment it
 * is written, registered or not. */
function governedNames(scan, registry) {
  const names = new Set();
  for (const m of scan.code.slice(...registry).matchAll(/(\w+)\s*:\s*([A-Za-z_$][\w$]*)/g)) {
    if (m[2] !== 'null') names.add(m[2]);
  }
  const shape = '(?:load|refresh)[A-Z][\\w$]*';
  const decls = new RegExp(
    `\\b(?:function\\s+(${shape})\\s*\\(|(?:const|let|var)\\s+(${shape})\\s*=)`, 'g');
  for (const m of scan.code.matchAll(decls)) {
    if (!scan.literal[m.index]) names.add(m[1] || m[2]);
  }
  return [...names].sort();
}

// Scanned inside the case, not at module load: a page missing `runLoader`
// (or with an unbalanced literal) must fail THIS case with a list of call
// sites, not abort the whole harness before any case runs.
testCase('no call site reaches a loader except through runLoader', () => {
  const SCAN = scanScript(PAGE_SCRIPT);
  const within = (i, span) => span !== null && i >= span[0] && i < span[1];
  const REGISTRY = bodySpan(SCAN, 'const', 'LOADERS');
  const RUN_LOADER = bodySpan(SCAN, 'function', 'runLoader');
  const LP_ISSUE_FETCH = bodySpan(SCAN, 'function', 'lpIssueFetch');
  const APPLY_OUTCOME = bodySpan(SCAN, 'function', 'applyOutcome');
  // The Launchpad's two report points. It has no registry entry by design
  // (`launchpad: null`), so it cannot reach `applyOutcome` through
  // `runLoader` — it drives its own fetch and reports the result itself.
  // These two are the whole of that exemption, named rather than implied.
  const LP_SEATS = [
    bodySpan(SCAN, 'function', 'lpRenderFetchError'),
    bodySpan(SCAN, 'function', 'lpApplySnapshot'),
  ];
  check(REGISTRY !== null, 'precondition: the LOADERS registry is in the page');
  check(RUN_LOADER !== null,
    'the single entry point `runLoader` is gone from the page — without it '
    + 'every call site is back to pairing a loader with applyOutcome by hand, '
    + 'which is the bug this case exists to prevent');
  if (REGISTRY === null) return;
  const names = governedNames(SCAN, REGISTRY);
  // A scan that found nothing would pass silently — the one way this case
  // could go vacuous.
  check(names.length >= 9,
    `precondition: the scan found the loaders (got ${names.length}: ${names})`);

  const findings = [];
  // ---- the scan's own preconditions, checked rather than asserted --------
  //
  // This case is only as good as the mask it builds, so it says out loud
  // when it cannot vouch for one. Two checks, and between them they cover
  // the whole "the page has an unterminated literal" family:
  //
  //   - the PARSER. A page with an unbalanced quote or backtick is not
  //     legal JS, and no mask over it means anything. Delegating to
  //     `vm.Script` costs one parse and catches every shape of the problem
  //     — including two stray backticks that re-pair each other, which the
  //     scanner's own bookkeeping cannot tell from a legitimate template.
  //   - the SCANNER's own record of quoted literals that hit a newline
  //     unclosed, which the parser check cannot localise: it gives the LINE.
  //
  // Written this way after getting it wrong: an earlier version of this
  // file claimed in a comment that the scanner "refuses to pass on an
  // unclosed literal". It did not — the string loop ran to EOF, the case
  // passed GREEN, and the only thing that went red was 71 neighbouring
  // cases, for reasons that looked unrelated. A stated safety property that
  // does not hold by its stated mechanism is worse than no claim at all,
  // because the next person changes the code around it and trusts it.
  try {
    new vm.Script(PAGE_SCRIPT);
  } catch (err) {
    findings.push('the shipped page does not parse, so the scan below means '
      + `nothing: ${err.message}`);
  }
  for (const at of SCAN.unterminated) {
    findings.push(`unterminated string literal at index.html:${fileLine(at)} — `
      + sourceLine(at));
  }

  for (const name of names) {
    // The declaration's own occurrence is the one mention outside the
    // registry that is not a call. Looked up literal-aware and in both
    // declaration forms, so neither a string containing `function
    // loadErrors(` nor an arrow-declared loader moves the exemption.
    const def = firstRealMatch(SCAN, declRe(name));
    const defAt = def ? def.index : -1;
    for (const m of SCAN.code.matchAll(new RegExp(`\\b${name}\\b`, 'g'))) {
      const i = m.index;
      if (SCAN.literal[i]) continue;                  // inside a literal
      if (defAt !== -1 && i >= defAt && i < defAt + def[0].length) continue;
      if (within(i, REGISTRY)) continue;              // the registry entry
      findings.push(`${name} at index.html:${fileLine(i)} — ${sourceLine(i)}`);
    }
  }
  // The other half of the pairing: `applyOutcome` is what a call site used
  // to have to remember, so the places allowed to reach it are counted —
  // `runLoader` for the nine registry screens, and the Launchpad's own two
  // report points, which exist because it has no registry entry to be run
  // through. Three, named; a fourth is a new way to report an outcome.
  const applyDef = firstRealMatch(SCAN, declRe('applyOutcome'));
  const APPLY_CALLERS = [RUN_LOADER, ...LP_SEATS];
  for (const m of SCAN.code.matchAll(/\bapplyOutcome\b/g)) {
    const i = m.index;
    if (SCAN.literal[i]) continue;
    if (i >= applyDef.index && i < applyDef.index + applyDef[0].length) continue;
    if (APPLY_CALLERS.some(span => within(i, span))) continue;
    findings.push(`applyOutcome at index.html:${fileLine(i)} — ${sourceLine(i)}`);
  }
  // And what `applyOutcome` alone is allowed to do. Routing the Launchpad
  // through `applyOutcome` (rather than its own `activeScreen()` check plus
  // a direct `markUpdatedUnread()`) left these two with exactly one caller
  // each, which makes the invariant worth pinning: `#updated`'s two states
  // are written from ONE place, so the screen-id gate and the `err`-class
  // handling cannot be half-copied to a second site and drift.
  for (const writer of ['stampUpdated', 'markUpdatedUnread']) {
    const def = firstRealMatch(SCAN, declRe(writer));
    for (const m of SCAN.code.matchAll(new RegExp(`\\b${writer}\\b`, 'g'))) {
      const i = m.index;
      if (SCAN.literal[i]) continue;
      if (def && i >= def.index && i < def.index + def[0].length) continue;
      if (within(i, APPLY_OUTCOME)) continue;
      findings.push(`${writer} at index.html:${fileLine(i)} — ${sourceLine(i)}`);
    }
  }
  // And the Launchpad's own entry point, which the registry deliberately
  // does NOT hold (`launchpad: null` — its 30s cycle, its own `lpState.seq`
  // and the §9.1 hidden-polling exception are not the screen timer's).
  // `lpIssueFetch` is its single funnel, so the snapshot URL may be spelt
  // in exactly one place; a second `fetch("/api/launchpad")` elsewhere would
  // be a second cycle with its own supersession, which is the same class of
  // defect one screen over.
  for (const m of SCAN.code.matchAll(/\/api\/launchpad/g)) {
    if (within(m.index, LP_ISSUE_FETCH)) continue;
    findings.push(`/api/launchpad at index.html:${fileLine(m.index)} — `
      + sourceLine(m.index));
  }

  check(findings.length === 0,
    'a loader must be run ONLY as runLoader("<screen>"), and the Launchpad '
    + 'snapshot fetched ONLY in lpIssueFetch — the pairing of a loader with '
    + 'applyOutcome was hand-written at every call site for four rounds of '
    + 'the same bug, and a call site that forgets the second half leaves an '
    + 'unread screen wearing a freshness stamp. `#updated` itself is written '
    + 'only by applyOutcome, so its screen-id gate cannot be half-copied '
    + 'elsewhere. The ONLY exemptions are the LOADERS registry, each '
    + 'function\'s own declaration, runLoader and the Launchpad\'s two report '
    + 'points (lpRenderFetchError, lpApplySnapshot — it has no registry entry '
    + 'to be run through) for applyOutcome, and applyOutcome for the two '
    + '#updated writers. Widening this allowlist means accepting a second way '
    + 'to run a loader or a second writer of the shared freshness marker, so '
    + 'say why in the code. Found:\n    '
    + findings.join('\n    '));
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
