// Exercises the launchpad root panel (spec §9, PR-C Task 6) by running the
// REAL, WHOLE <script> of dispatcher/server/static/index.html inside a VM
// over the page's own parsed markup (tests/web/dom.js) — same discipline as
// the sibling harnesses (run_console_harness.js's header explains the
// reasoning): self-contained on purpose, module-local fixtures.
//
// Four things are asserted here (task-6-brief.md's four cases):
//   1. wholesale rendering of a fixture snapshot: repo admission classes,
//      `work_id @ sha7` on ready rows, no link on `logs_available:false`
//      recent rows, the store banner tracking `store_unreadable` exactly,
//      and server row order preserved verbatim (no client-side re-sort —
//      the attention-first sort is the assembler's job, Task 2).
//   2. typed blockers (spec §9): a linked in-flight blocker
//      (`launch_busy`/`run_in_flight` WITH `request_id`) exposes the
//      existing run-view opener; an unlinked `run_in_flight` (bare
//      `run_id`) renders text with NO link; `lock_io_unreadable` /
//      `run_state_unreadable` render diagnostic text with no control at
//      all.
//   3. the sequence guard (spec §10): constructed through the REAL entry
//      points — a captured fake `setInterval` callback for the timer path,
//      `lpRefetchAfterAction()` for the action-triggered superseding path.
//      `inflight` is a counter (not a boolean): a timer tick is refused
//      while ANY fetch is in flight, but an action refetch supersedes
//      immediately with a higher `seq`. Strict application rule: a
//      response applies only if its `seq === lpState.seq` (the latest
//      issued) — asserted over BOTH resolution orders.
//   4. wholesale re-render: applying a second snapshot rebuilds every
//      container from scratch (stale child nodes are gone), never patches.
//
// Usage: node launchpad_harness.js <path-to-index.html>
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Document, dispatch} = require(path.join(__dirname, 'dom.js'));
const {browserGlobals} = require(path.join(__dirname, 'screens.js'));

const HTML_PATH = process.argv[2];
if (!HTML_PATH) {
  console.error('usage: node launchpad_harness.js <index.html>');
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

// The launchpad panel's own auto-refresh period (spec §9: "30s
// auto-refresh"). The whole page script also registers the pre-existing
// dashboard's `setInterval(refresh, 10000)` on the SAME fake timer this
// harness installs — the two are told apart by period, not by call order,
// since nothing in this harness controls which runs first.
const LP_REFRESH_MS = 30000;

// ---- fixtures ---------------------------------------------------------------

const resp = (status, body) => ({
  status, ok: status >= 200 && status < 300,
  json: () => Promise.resolve(body),
});
const ok = body => resp(200, body);

/** A structurally-valid, empty LaunchpadSnapshot (dispatcher/core/
 * launchpad.py's LaunchpadSnapshot pydantic model, field-for-field) —
 * `overrides` replaces individual fields for a given fixture. */
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
// run_console_harness.js's defaultRoutes — every whole-script harness needs
// all of them fixture'd or the unrelated dashboard code throws during boot.
function defaultRoutes(launchpadRoute) {
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
    [u => u === '/api/launchpad', launchpadRoute || (() => ok(snapshot()))],
  ];
}

const drain = async (turns = 5) => {
  for (let i = 0; i < turns; i++) await new Promise(r => setTimeout(r, 0));
};

/** A recording, non-firing setInterval/clearInterval: every registration is
 * kept (id -> {cb, period}) and NOTHING fires on its own — every firing in
 * these tests is an explicit, named call to the captured callback, which is
 * the whole point of asserting the sequence guard through the real entry
 * points rather than a simulated clock. `byPeriod` finds the launchpad
 * panel's own interval among the page's several registered ones (the
 * pre-existing dashboard `refresh()` loop registers its own, at a
 * different period, on this exact same fake timer). */
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

async function boot(launchpadRoute) {
  const document = new Document(BODY_HTML);
  const calls = [];
  const routes = defaultRoutes(launchpadRoute);
  const timers = makeIntervalRecorder();
  const ctx = {
    document, console, URL,
    setTimeout, clearTimeout,
    setInterval: timers.setInterval,
    clearInterval: timers.clearInterval,
    fetch: (url, opts) => {
      const u = String(url);
      calls.push({url: u, opts: opts || {}});
      for (const [test, make] of routes) if (test(u)) return Promise.resolve(make(u));
      return Promise.reject(new Error(`no fixture route for ${u}`));
    },
    ...browserGlobals(),
  };
  vm.createContext(ctx);
  vm.runInContext(PAGE_SCRIPT, ctx);
  await drain();
  return {ctx, document, calls, routes, timers};
}

/** Boots a fresh page and hands it to `fn`. */
async function withPage(fn, launchpadRoute) { await fn(await boot(launchpadRoute)); }

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

// ---- fixtures used by several cases -----------------------------------------

const REPO_READY = {
  repo_key: 'github.com/andrei-shtanakov/deployer', repository: 'deployer',
  default_branch: 'master', seen_revision: 'a'.repeat(40),
  admission: 'ready', blockers: [],
};
const REPO_BLOCKED_LINKED = {
  repo_key: 'github.com/andrei-shtanakov/maestro', repository: 'maestro',
  default_branch: 'master', seen_revision: 'b'.repeat(40),
  admission: 'blocked',
  blockers: [{code: 'launch_busy', request_id: 'rc-linked', run_id: null, detail: null}],
};
const REPO_UNREADABLE = {
  repo_key: 'github.com/andrei-shtanakov/broken', repository: 'broken',
  default_branch: '', seen_revision: null,
  admission: 'unreadable',
  blockers: [{code: 'repo_unresolved', request_id: null, run_id: null,
    detail: 'not a checkout'}],
};

// ---- case 1: wholesale rendering of a fixture snapshot ----------------------

testCase('renders repo rows with admission classes', async () => {
  await withPage(async page => {
    page.ctx.lpRender(snapshot({
      repositories: [REPO_READY, REPO_BLOCKED_LINKED, REPO_UNREADABLE],
    }));
    const body = htmlOf(page, '#lp-repos');
    check(body.includes('deployer') && body.includes('maestro')
      && body.includes('broken'), `all three repos render (got: ${body})`);
    const badges = page.document.querySelectorAll('#lp-repos .badge');
    check(badges.length === 3, `one admission badge per repo (got ${badges.length})`);
    check(badges[0].className.split(/\s+/).includes('ok'),
      `ready repo's badge carries a distinct class (got: ${badges[0].className})`);
    check(badges[1].className !== badges[0].className,
      "blocked repo's admission class differs from ready's "
      + `(got: ${badges[1].className} vs ${badges[0].className})`);
    check(badges[2].className !== badges[0].className
      && badges[2].className !== badges[1].className,
      "unreadable repo's admission class differs from both "
      + `(got: ${badges[2].className})`);
  });
});

testCase('ready rows show work_id @ sha7', async () => {
  await withPage(async page => {
    page.ctx.lpRender(snapshot({
      ready: [{repo_key: 'github.com/andrei-shtanakov/deployer',
        work_id: 'todo://deployer/some-work-item', dag_path: 'tasks/dag.yaml',
        seen_revision: 'c'.repeat(40)}],
    }));
    const body = htmlOf(page, '#lp-ready');
    check(body.includes('todo://deployer/some-work-item @ ccccccc'),
      `ready row shows "work_id @ sha7" (got: ${body})`);
    check(!body.includes('c'.repeat(40)),
      'only the short sha7 is shown, not the full 40-hex');
  });
});

testCase('recent rows: logs_available=false renders NO link, true renders one',
  async () => {
  await withPage(async page => {
    page.ctx.lpRender(snapshot({
      recent_completed: [
        {request_id: 'r-nolog', repo_key: 'github.com/andrei-shtanakov/deployer',
          work_id: 'w1', run_id: '01AAA', revision: 'd'.repeat(40),
          outcome: 'completed', updated_at: '2026-08-27T00:00:00Z',
          logs_available: false},
        {request_id: 'r-haslog', repo_key: 'github.com/andrei-shtanakov/deployer',
          work_id: 'w2', run_id: '01BBB', revision: 'e'.repeat(40),
          outcome: 'completed', updated_at: '2026-08-27T00:01:00Z',
          logs_available: true},
      ],
    }));
    const rows = page.document.querySelectorAll('#lp-recent tbody tr');
    check(rows.length === 2, `two recent rows render (got ${rows.length})`);
    // Task 3 (tabbed-ui): the drill-down is a whole-row `data-lp-request-id`
    // attribute, not a per-cell `<a>` — see index.html's lpRecentRowHtml.
    check(!rows[0].hasAttribute('data-lp-request-id'),
      `logs_available:false row offers no drill-down (got: ${rows[0].innerHTML})`);
    check(rows[1].getAttribute('data-lp-request-id') === 'r-haslog',
      `logs_available:true row carries the drill-down (got: ${rows[1].innerHTML})`);
  });
});

testCase('store banner appears iff store_unreadable is non-empty', async () => {
  await withPage(async page => {
    page.ctx.lpRender(snapshot({store_unreadable: []}));
    check(el(page, '#lp-store-banner').hidden === true,
      'banner hidden when store_unreadable is empty');
    page.ctx.lpRender(snapshot({store_unreadable: ['corrupt-record-1']}));
    check(el(page, '#lp-store-banner').hidden === false,
      'banner shown when store_unreadable is non-empty');
    check(htmlOf(page, '#lp-store-banner').includes('corrupt-record-1'),
      'the note itself is on screen');
  });
});

testCase('rendering preserves the server row order verbatim (no client re-sort)',
  async () => {
  await withPage(async page => {
    const repos = [
      {...REPO_READY, repo_key: 'z-last', repository: 'z-last'},
      {...REPO_READY, repo_key: 'a-first', repository: 'a-first'},
      {...REPO_READY, repo_key: 'm-middle', repository: 'm-middle'},
    ];
    page.ctx.lpRender(snapshot({repositories: repos}));
    const names = page.document.querySelectorAll('#lp-repos tbody tr')
      .map(tr => tr.textContent);
    check(names[0].includes('z-last') && names[1].includes('a-first')
      && names[2].includes('m-middle'),
      `server order preserved, not alphabetized (got: ${JSON.stringify(names)})`);
  });
});

// ---- case 2: typed blockers --------------------------------------------------

testCase('typed blocker: linked in-flight exposes the run-view opener', async () => {
  await withPage(async page => {
    page.ctx.lpRender(snapshot({repositories: [REPO_BLOCKED_LINKED]}));
    const link = page.document.querySelector('#lp-repos a.lp-open-run');
    check(!!link, 'a linked blocker renders an opener link');
    if (!link) return;
    page.routes.push([u => u === '/api/runs/rc-linked', () => ok(
      {record: {state: 'materialized', run_id: '01AAA'},
        run: {status: 'running'}, warnings: []})]);
    await click(page, '#lp-repos a.lp-open-run');
    check(callsTo(page, '/api/runs/rc-linked') === 1,
      'clicking the linked blocker opens the run view (fetches its record)');
    check(el(page, '#rc-request-id').value === 'rc-linked',
      'the run console reflects the opened request_id');
  });
});

testCase('typed blocker: unlinked run_in_flight renders text, no link', async () => {
  await withPage(async page => {
    const repo = {...REPO_READY, admission: 'blocked', blockers: [
      {code: 'run_in_flight', request_id: null, run_id: '01UNLINKED', detail: null},
    ]};
    page.ctx.lpRender(snapshot({repositories: [repo]}));
    const cell = el(page, '#lp-repos');
    check(cell.innerHTML.includes('01UNLINKED'),
      `the bare run_id is on screen (got: ${cell.innerHTML})`);
    check(!page.document.querySelector('#lp-repos a.lp-open-run'),
      'an unlinked run_in_flight blocker renders NO run-view link');
  });
});

testCase('typed blocker: run_vanished / lock_malformed render placeholder anchors',
  async () => {
  await withPage(async page => {
    const repo = {...REPO_READY, admission: 'blocked', blockers: [
      {code: 'run_vanished', request_id: 'rc-vanished', run_id: '01VAN', detail: null},
      {code: 'lock_malformed', request_id: null, run_id: null, detail: 'empty lock'},
    ]};
    page.ctx.lpRender(snapshot({repositories: [repo]}));
    const anchors = page.document.querySelectorAll('#lp-repos a.lp-blocker-anchor');
    check(anchors.length === 2,
      `both codes render a placeholder anchor (got ${anchors.length})`);
    check(anchors.some(a => a.dataset.code === 'run_vanished'),
      'run_vanished anchor is tagged by code');
    check(anchors.some(a => a.dataset.code === 'lock_malformed'),
      'lock_malformed anchor is tagged by code');
  });
});

testCase('typed blocker: unreadable codes render diagnostic text, no control',
  async () => {
  await withPage(async page => {
    const repo = {...REPO_READY, admission: 'unreadable', blockers: [
      {code: 'lock_io_unreadable', request_id: null, run_id: null,
        detail: 'permission denied'},
      {code: 'run_state_unreadable', request_id: null, run_id: null,
        detail: 'state.db unreadable'},
    ]};
    page.ctx.lpRender(snapshot({repositories: [repo]}));
    const cell = el(page, '#lp-repos');
    check(cell.innerHTML.includes('permission denied')
      && cell.innerHTML.includes('state.db unreadable'),
      `both diagnostic details are on screen (got: ${cell.innerHTML})`);
    check(!cell.querySelector('a'),
      'unreadable codes offer no control (no <a> in the repos panel at all)');
  });
});

// ---- case 3: the sequence guard (spec §10) -----------------------------------

/** Registers a controllable-promise route for GET /api/launchpad, returning
 * a `release(body)` function that settles the NEXT unresolved call this
 * route serves. Each call to the route gets its own deferred promise, so a
 * B-then-A or A-then-B resolution order is driven purely by which
 * `release` a test calls first — never by fetch call order. */
function controllableLaunchpadRoute(page) {
  const pendingReleases = [];
  page.routes.unshift([u => u === '/api/launchpad', () => {
    let release;
    const p = new Promise(r => { release = r; });
    pendingReleases.push(release);
    return p;
  }]);
  return {
    releaseNth(n, body) { pendingReleases[n](ok(body)); },
  };
}

async function sequenceGuardScenario(page, resolveOrder) {
  const timer = page.timers.byPeriod(LP_REFRESH_MS);
  check(!!timer, 'lpScheduleRefresh registered a 30s interval the harness can '
    + `drive (registered periods: none matched ${LP_REFRESH_MS})`);
  if (!timer) return;

  const before = callsTo(page, '/api/launchpad');
  const control = controllableLaunchpadRoute(page);
  const SNAP_A = snapshot({snapshot_id: 'snap-A', repositories: [
    {...REPO_READY, repository: 'from-A'}]});
  const SNAP_B = snapshot({snapshot_id: 'snap-B', repositories: [
    {...REPO_READY, repository: 'from-B'}]});

  timer.cb();                              // fetch A: timer tick
  await drain();
  page.ctx.lpRefetchAfterAction();         // fetch B: superseding action refetch
  await drain();
  timer.cb();                              // a second timer tick while both are
  await drain();                           // in flight — must be refused outright

  const showsA = () => htmlOf(page, '#lp-repos').includes('from-A');
  const showsB = () => htmlOf(page, '#lp-repos').includes('from-B');

  if (resolveOrder === 'B-then-A') {
    control.releaseNth(1, SNAP_B);
    await drain();
    check(showsB() && !showsA(), `B applied first (got: ${htmlOf(page, '#lp-repos')})`);
    control.releaseNth(0, SNAP_A);
    await drain();
    check(showsB() && !showsA(),
      `stale A never overwrote B, over two ticks (got: ${htmlOf(page, '#lp-repos')})`);
  } else {
    control.releaseNth(0, SNAP_A);
    await drain();
    check(!showsA(),
      `superseded A must NEVER apply, not even temporarily (got: ${htmlOf(page, '#lp-repos')})`);
    control.releaseNth(1, SNAP_B);
    await drain();
    check(showsB() && !showsA(),
      `B applied once it resolved (got: ${htmlOf(page, '#lp-repos')})`);
  }

  const after = callsTo(page, '/api/launchpad');
  check(after - before === 2,
    `exactly two requests reached fetch this sub-scenario (got ${after - before})`);
}

testCase('sequence guard: B resolves first — stays on B over two ticks', async () => {
  await withPage(async page => { await sequenceGuardScenario(page, 'B-then-A'); });
});

testCase('sequence guard: A resolves first — A never applies, not even briefly',
  async () => {
  await withPage(async page => { await sequenceGuardScenario(page, 'A-then-B'); });
});

// ---- case 4: wholesale re-render, never patched ------------------------------

testCase('applying a second snapshot rebuilds containers — stale nodes gone',
  async () => {
  await withPage(async page => {
    page.ctx.lpRender(snapshot({repositories: [REPO_READY],
      ready: [{repo_key: 'r', work_id: 'first-work-item', dag_path: 'd',
        seen_revision: 'f'.repeat(40)}]}));
    const firstRow = page.document.querySelector('#lp-repos tbody tr');
    check(!!firstRow, 'first render produced a repo row');
    check(htmlOf(page, '#lp-ready').includes('first-work-item'),
      'first render produced a ready row');

    page.ctx.lpRender(snapshot({repositories: [], ready: []}));
    // dom.js's innerHTML setter (tests/web/dom.js) does not null out an old
    // child's `.parentNode` on replacement, so that alone can't prove a
    // rebuild happened. What DOES prove it: the row is no longer reachable
    // from the document at all — a PATCH would keep reusing the same node
    // identity in place; a wholesale rebuild produces fresh elements every
    // time, so the exact old object is gone from a fresh query.
    const survivors = page.document.querySelectorAll('#lp-repos tbody tr');
    check(!survivors.includes(firstRow),
      'the stale repo row is gone from the tree, not reused in place (patched)');
    check(!htmlOf(page, '#lp-ready').includes('first-work-item'),
      'the stale ready row is gone from a wholesale re-render');
    const reposBody = page.document.querySelector('#lp-repos tbody');
    check(!!reposBody && reposBody.children.length <= 1,
      'an empty snapshot rebuilds to an empty (or placeholder-only) table, '
      + `not a leftover row (got ${reposBody && reposBody.children.length} children)`);
  });
});

// ---- Task 7: the launch flow (spec §9/§10) -----------------------------------
//
// task-7-brief.md's four NAMED §10 scenarios come first, then the six
// behaviours. The stale-snapshot sequence guard (§10's second named
// scenario) is case 3 above, unmodified — Task 7 adds nothing to it.

const READY_ITEM = {
  repo_key: 'github.com/andrei-shtanakov/deployer',
  work_id: 'todo://deployer/some-work-item', dag_path: 'tasks/dag.yaml',
  seen_revision: 'a'.repeat(40),
};
function readySnapshot(overrides = {}) {
  return snapshot({ready: [READY_ITEM], ...overrides});
}
/** Sets a control's value and fires 'input', exactly as a real keystroke
 * does — dom.js's `dispatch()` bubbles it to the delegated listener on
 * #launchpad the same way it bubbles 'click' (tests/web/dom.js). */
async function typeInto(page, selector, value) {
  const node = el(page, selector);
  node.value = value;
  await Promise.all(dispatch(node, 'input'));
  await drain();
}
function submitBodies(page) {
  return page.calls.filter(c => c.url === '/api/runs/submit')
    .map(c => JSON.parse(c.opts.body));
}

// ---- §10 named scenario 1: lost response → unknown → Retry same id → -------
// ---- read-back finds the record ---------------------------------------------

testCase('§10: lost response → unknown → Retry same id → read-back finds the record',
  async () => {
  await withPage(async page => {
    page.routes.unshift([u => u === '/api/runs/submit',
      () => Promise.reject(new Error('connection reset'))]);
    await click(page, '#lp-ready tr.lp-ready-row');
    await click(page, '#lp-ready .lp-confirm');
    check(/launch outcome unknown/.test(htmlOf(page, '#lp-ready')),
      `a lost response renders the row as unknown (got: ${htmlOf(page, '#lp-ready')})`);

    const firstBody = submitBodies(page)[0];
    check(!!(firstBody && firstBody.request_id), 'the attempt minted a request_id');
    if (!firstBody) return;

    // Retry — still lost. This proves id reuse, NOT resolution: transport
    // uncertainty is only resolved by a DEFINITE HTTP answer (spec §9).
    await click(page, '#lp-ready .lp-retry');
    const bodies = submitBodies(page);
    check(bodies.length === 2, `retry hit the wire again (got ${bodies.length})`);
    if (bodies.length === 2) {
      check(bodies[1].request_id === firstBody.request_id,
        `Retry resends the SAME request_id (got ${bodies[1].request_id} `
        + `vs ${firstBody.request_id})`);
    }
    check(/launch outcome unknown/.test(htmlOf(page, '#lp-ready')),
      'still unknown after a second lost response');

    // Read-back: 404 first (KEEPS unknown), then 200 (resolves it).
    page.routes.unshift([u => u === `/api/runs/${firstBody.request_id}`,
      () => resp(404, {detail: 'no such request'})]);
    await click(page, '#lp-ready .lp-check-status');
    check(/launch outcome unknown/.test(htmlOf(page, '#lp-ready')),
      'a 404 read-back KEEPS the unknown state');

    page.routes.unshift([u => u === `/api/runs/${firstBody.request_id}`,
      () => ok({record: {state: 'materialized', run_id: '01AAA'},
        run: {status: 'running'}, warnings: []})]);
    const before = callsTo(page, '/api/launchpad');
    await click(page, '#lp-ready .lp-check-status');
    check(!/launch outcome unknown/.test(htmlOf(page, '#lp-ready')),
      `a 200 read-back resolves the unknown state (got: ${htmlOf(page, '#lp-ready')})`);
    check(callsTo(page, '/api/launchpad') === before + 1,
      'a resolved read-back triggers exactly one whole-snapshot refetch');
  }, () => ok(readySnapshot()));
});

// ---- §10 named scenario 2: the stale-snapshot sequence guard — see case 3 --
// (kept from Task 6, unmodified; nothing to add here.)

// ---- §10 named scenario 3: a Ready row vanishing under an open confirm -----

testCase('§10: a Ready row vanishing under an open confirmation → Confirm '
  + 'disabled with cause, the expanded state is preserved', async () => {
  await withPage(async page => {
    await click(page, '#lp-ready tr.lp-ready-row');
    check(!!maybeEl(page, '#lp-ready .lp-confirm'), 'row expanded with a Confirm control');
    check(el(page, '#lp-ready .lp-confirm').disabled === false, 'Confirm starts enabled');

    page.routes.unshift([u => u === '/api/launchpad', () => ok(snapshot({ready: []}))]);
    await page.ctx.lpRefetchAfterAction();
    await drain();

    check(/nothing ready/.test(htmlOf(page, '#lp-ready')),
      'the vanished item is no longer listed in #lp-ready itself');
    const confirmBtn = maybeEl(page, '#lp-pending .lp-confirm');
    check(!!confirmBtn, 'the open confirmation reappears in #lp-pending, not silently dropped');
    if (!confirmBtn) return;
    check(confirmBtn.disabled === true, 'Confirm is disabled once the row is gone');
    check(/no longer in the Ready list/.test(htmlOf(page, '#lp-pending')),
      `the cause is shown (got: ${htmlOf(page, '#lp-pending')})`);
    check(htmlOf(page, '#lp-pending').includes('some-work-item'),
      'the expanded item is still named — typed/expanded state is preserved');
  }, () => ok(readySnapshot()));
});

// ---- §10 named scenario 4: repeat submit, same request_id, IDENTICAL body --

testCase('§10: repeat submit with the same request_id sends an IDENTICAL body',
  async () => {
  await withPage(async page => {
    page.routes.unshift([u => u === '/api/runs/submit',
      () => Promise.reject(new Error('network blip'))]);
    await click(page, '#lp-ready tr.lp-ready-row');
    await click(page, '#lp-ready .lp-confirm');
    await click(page, '#lp-ready .lp-retry');
    const calls = page.calls.filter(c => c.url === '/api/runs/submit');
    check(calls.length === 2, `two attempts reached the wire (got ${calls.length})`);
    if (calls.length !== 2) return;
    check(calls[0].opts.body === calls[1].opts.body,
      `retry sends an IDENTICAL body, byte for byte (got: ${calls[0].opts.body} `
      + `vs ${calls[1].opts.body})`);
  }, () => ok(readySnapshot()));
});

// ---- behaviour 1: two-step launch --------------------------------------------

testCase('behaviour 1: clicking a Ready row expands the two-step confirm; '
  + 'Confirm POSTs the v2 body', async () => {
  await withPage(async page => {
    check(!maybeEl(page, '#lp-ready .lp-confirm'), 'no confirm control before the row is clicked');
    await click(page, '#lp-ready tr.lp-ready-row');
    const expanded = htmlOf(page, '#lp-ready');
    check(expanded.includes('some-work-item') && expanded.includes('aaaaaaa'),
      `expanded confirm names work_id @ sha7 (got: ${expanded})`);

    await click(page, '#lp-ready .lp-confirm');
    const body = submitBodies(page)[0];
    check(!!body, 'Confirm posted to /api/runs/submit');
    if (!body) return;
    check(body.repo_key === READY_ITEM.repo_key && body.work_id === READY_ITEM.work_id,
      `body names repo_key/work_id (got: ${JSON.stringify(body)})`);
    check(body.seen_revision === READY_ITEM.seen_revision, 'body carries seen_revision');
    check(body.snapshot_id === 'snap-base', 'body carries snapshot_id');
    check(typeof body.request_id === 'string' && body.request_id.length > 0,
      'body carries a generated request_id');
  }, () => ok(readySnapshot()));
});

testCase('behaviour 1: a settled 2xx receipt clears the pending entry — a '
  + 'fresh attempt mints a new request_id', async () => {
  await withPage(async page => {
    await click(page, '#lp-ready tr.lp-ready-row');
    await click(page, '#lp-ready .lp-confirm');
    check(!maybeEl(page, '#lp-ready .lp-confirm')
      && !maybeEl(page, '#lp-ready .lp-retry') && !maybeEl(page, '#lp-ready .lp-check-status'),
      'a settled 2xx receipt leaves no open/unknown controls behind');

    await click(page, '#lp-ready tr.lp-ready-row');
    await click(page, '#lp-ready .lp-confirm');
    const bodies = submitBodies(page);
    check(bodies.length === 2, `two independent attempts were sent (got ${bodies.length})`);
    if (bodies.length !== 2) return;
    check(bodies[0].request_id !== bodies[1].request_id,
      `a settled attempt does not pin the next one to the same id `
      + `(got ${bodies[0].request_id} vs ${bodies[1].request_id})`);
  }, () => ok(readySnapshot()));
});

// ---- behaviour 2: transport uncertainty --------------------------------------

testCase('behaviour 2: a whole-snapshot refetch alone does not resolve an '
  + 'unknown row — only Retry or read-back can', async () => {
  await withPage(async page => {
    page.routes.unshift([u => u === '/api/runs/submit',
      () => Promise.reject(new Error('dropped'))]);
    await click(page, '#lp-ready tr.lp-ready-row');
    await click(page, '#lp-ready .lp-confirm');
    check(/launch outcome unknown/.test(htmlOf(page, '#lp-ready')), 'unknown after the drop');

    const timer = page.timers.byPeriod(LP_REFRESH_MS);
    check(!!timer, 'the 30s refresh timer is registered');
    if (timer) { timer.cb(); await drain(); }

    check(/launch outcome unknown/.test(htmlOf(page, '#lp-ready')),
      'still unknown after an unrelated whole-snapshot refetch — spec §9: '
      + '"a full refetch alone cannot resolve it"');
  }, () => ok(readySnapshot()));
});

// ---- behaviour 3: structured errors ------------------------------------------

testCase('behaviour 3: a structured {code,detail} error renders "code: '
  + 'detail" text with `current` shown INSIDE that message (plan §3), then '
  + 'ONE whole-snapshot refetch',
  async () => {
  await withPage(async page => {
    page.routes.unshift([u => u === '/api/runs/submit', () => resp(409, {
      code: 'revision_moved', detail: 'HEAD moved since the operator saw it',
      current: {seen_revision: 'z'.repeat(40)},
    })]);
    const before = callsTo(page, '/api/launchpad');
    await click(page, '#lp-ready tr.lp-ready-row');
    await click(page, '#lp-ready .lp-confirm');
    const html = htmlOf(page, '#lp-ready');
    check(html.includes('revision_moved: HEAD moved since the operator saw it'),
      `renders "code: detail" (got: ${html})`);
    check(html.includes('current') && html.includes('z'.repeat(40)),
      `\`current\` is shown INSIDE the message text, not dropped (got: ${html})`);
    check(!maybeEl(page, '#lp-ready .lp-confirm') && !maybeEl(page, '#lp-ready .lp-retry'),
      'the pending entry is cleared — a structured error is a settled answer');
    check(callsTo(page, '/api/launchpad') === before + 1,
      'exactly ONE whole-snapshot refetch follows the error');
  }, () => ok(readySnapshot()));
});

// ---- behaviour 4: re-validation of open confirmations after a refetch -------
// (the "row left Ready" case is §10 named scenario 3 above; this covers the
// other named cause — seen_revision changing while the row stays Ready.)

testCase('behaviour 4: seen_revision changing under an open confirmation '
  + 'disables Confirm with the new revision named', async () => {
  await withPage(async page => {
    await click(page, '#lp-ready tr.lp-ready-row');
    check(el(page, '#lp-ready .lp-confirm').disabled === false, 'starts enabled');

    const moved = {...READY_ITEM, seen_revision: 'b'.repeat(40)};
    page.routes.unshift([u => u === '/api/launchpad', () => ok(snapshot({ready: [moved]}))]);
    await page.ctx.lpRefetchAfterAction();
    await drain();

    const confirmBtn = el(page, '#lp-ready .lp-confirm');
    check(confirmBtn.disabled === true, 'Confirm disables when seen_revision moved');
    check(htmlOf(page, '#lp-ready').includes('bbbbbbb'),
      `the cause names the new revision (got: ${htmlOf(page, '#lp-ready')})`);
  }, () => ok(readySnapshot()));
});

// ---- behaviour 5: the audited escape forms -----------------------------------

const REPO_VANISHED = {
  repo_key: 'github.com/andrei-shtanakov/proctor-a', repository: 'proctor-a',
  default_branch: 'master', seen_revision: 'c'.repeat(40), admission: 'blocked',
  blockers: [{code: 'run_vanished', request_id: 'rc-vanished-1', run_id: '01VAN', detail: null}],
};
const REPO_LOCK_MALFORMED = {
  repo_key: 'github.com/andrei-shtanakov/kapelle', repository: 'kapelle',
  default_branch: 'master', seen_revision: 'd'.repeat(40), admission: 'blocked',
  blockers: [{code: 'lock_malformed', request_id: null, run_id: null, detail: 'empty lock'}],
};

testCase('behaviour 5: acknowledge-vanished — retyped confirm_run_id (never '
  + 'prefilled) + required reason; success refetches', async () => {
  await withPage(async page => {
    await click(page, '#lp-repos a.lp-blocker-anchor');
    const confirmInput = el(page, '.lp-escape-form input[data-escape-field="confirm"]');
    check(confirmInput.value === '', 'confirm_run_id is NEVER prefilled');
    check(el(page, '.lp-escape-submit').disabled === true,
      'Submit starts disabled — nothing typed yet');

    await typeInto(page, '.lp-escape-form input[data-escape-field="confirm"]', 'wrong-run-id');
    check(el(page, '.lp-escape-submit').disabled === true,
      'still disabled — the retyped value must match exactly');
    await typeInto(page, '.lp-escape-form input[data-escape-field="confirm"]', '01VAN');
    check(el(page, '.lp-escape-submit').disabled === true,
      'still disabled — reason is required too');
    await typeInto(page, '.lp-escape-form input[data-escape-field="reason"]',
      'confirmed via maestro logs');
    check(el(page, '.lp-escape-submit').disabled === false,
      'enabled once both the confirmation and the reason are filled');

    page.routes.unshift([u => u === '/api/runs/rc-vanished-1/acknowledge-vanished',
      () => ok({request_id: 'rc-vanished-1', repo_key: REPO_VANISHED.repo_key,
        state: 'terminal', run_id: null, outcome: 'vanished-acknowledged'})]);
    const before = callsTo(page, '/api/launchpad');
    await click(page, '.lp-escape-submit');
    const call = page.calls.find(c => c.url === '/api/runs/rc-vanished-1/acknowledge-vanished');
    check(!!call, 'submitted to the acknowledge-vanished route');
    if (call) {
      const body = JSON.parse(call.opts.body);
      check(body.confirm_run_id === '01VAN' && body.reason === 'confirmed via maestro logs',
        `body carries the retyped confirmation and reason (got: ${JSON.stringify(body)})`);
    }
    check(!maybeEl(page, '.lp-escape-form'), 'success closes the form');
    check(callsTo(page, '/api/launchpad') === before + 1, 'success triggers a whole refetch');
  }, () => ok(snapshot({repositories: [REPO_VANISHED]})));
});

testCase('behaviour 5: release-malformed — a structured error keeps the '
  + 'form open with typed values intact (rule 3 discipline)', async () => {
  await withPage(async page => {
    await click(page, '#lp-repos a.lp-blocker-anchor');
    await typeInto(page, '.lp-escape-form input[data-escape-field="confirm"]',
      REPO_LOCK_MALFORMED.repo_key);
    await typeInto(page, '.lp-escape-form input[data-escape-field="reason"]',
      'checked the lock by hand');

    page.routes.unshift([u => u === '/api/locks/release-malformed',
      () => resp(409, {detail: 'guard_busy: timed out acquiring the lock'})]);
    const before = callsTo(page, '/api/launchpad');
    await click(page, '.lp-escape-submit');

    check(!!maybeEl(page, '.lp-escape-form'), 'the form stays open after an error');
    check(htmlOf(page, '#lp-repos').includes('guard_busy: timed out acquiring the lock'),
      'the error text is shown');
    check(el(page, '.lp-escape-form input[data-escape-field="confirm"]').value
      === REPO_LOCK_MALFORMED.repo_key, 'the retyped confirmation is preserved');
    check(el(page, '.lp-escape-form input[data-escape-field="reason"]').value
      === 'checked the lock by hand', 'the typed reason is preserved');
    check(callsTo(page, '/api/launchpad') === before,
      'an error does NOT refetch — only success does (rule 3)');
  }, () => ok(snapshot({repositories: [REPO_LOCK_MALFORMED]})));
});

// Behaviour 6 (the manual/advanced form) lives in run_console_harness.js —
// #run-console is that harness's page, not this one's.

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

// ---- gate pass-3: outcomes survive the row's disappearance -------------------

testCase('gate-3a: a settled receipt stays visible after the Ready row '
  + 'vanishes on refetch', async () => {
  let servedFirst = false;
  await withPage(async page => {
    // Override submit to return a real run_id, then confirm: the refetch
    // triggered by the settled receipt serves a snapshot where the item
    // is GONE from ready (the real post-launch state) — the receipt (the
    // only place run_id appears) must survive that.
    page.routes.unshift([u => u === '/api/runs/submit', () => ok({
      request_id: 'rq-gate3a', run_id: '01RUN', accepted: true, reason: null,
    })]);
    await click(page, '#lp-ready tr.lp-ready-row');
    await click(page, '#lp-ready .lp-confirm');
    const outcomesHtml = htmlOf(page, '#lp-pending');
    check(outcomesHtml.includes('01RUN'),
      `the receipt's run_id survives the vanished row (got: ${outcomesHtml})`);
  }, () => {
    // First fetch (boot): the item is Ready. Every later fetch (the
    // post-receipt refetch): the item is gone — launched.
    if (!servedFirst) { servedFirst = true; return ok(readySnapshot()); }
    return ok(snapshot());
  });
});

testCase('gate-3b: an active row with a request_id links to the run view',
  async () => {
  await withPage(async page => {
    // Task 3 (tabbed-ui): the drill-down is a whole-row `data-lp-request-id`
    // attribute, not a per-cell `<a class="lp-open-run">` — see index.html's
    // lpActiveRowHtml.
    check(!!page.document.querySelector('#lp-active [data-lp-request-id="rq-act1"]'),
      'active rows with a request_id carry the run-view opener');
    const linked = page.document.querySelectorAll('#lp-active [data-lp-request-id]');
    check(linked.length === 1,
      `only the linked row carries the drill-down attribute (got ${linked.length})`);
  }, () => ok(snapshot({active: [
    {request_id: 'rq-act1', repo_key: 'github.com/o/r', work_id: 'w1',
     state: 'materialized', run_id: '01LINKED', run_status: 'RUNNING',
     attention: false, updated_at: '2026-08-27T00:00:01Z'},
    {request_id: null, repo_key: 'github.com/o/r', work_id: null,
     state: 'unlinked-run', run_id: '01UNLINKED', run_status: 'RUNNING',
     attention: false, updated_at: '2026-08-27T00:00:02Z'},
  ]})));
});
