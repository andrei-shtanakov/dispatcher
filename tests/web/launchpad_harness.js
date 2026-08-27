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
    window: {open: () => {}},
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
    check(!rows[0].innerHTML.includes('<a'),
      `logs_available:false row has no link (got: ${rows[0].innerHTML})`);
    check(rows[1].innerHTML.includes('<a'),
      `logs_available:true row has a link (got: ${rows[1].innerHTML})`);
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
