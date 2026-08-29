// Exercises the Orchestration-runs panel (TODO maestro-runs-panel-parity,
// #147) by running the REAL, WHOLE <script> of
// dispatcher/server/static/index.html inside a VM over the page's own
// parsed markup (tests/web/dom.js) — same discipline as
// product_proposals_harness.js: nothing is sliced and no handler is
// simulated. detail() is driven by clicking a project card; the
// `/api/projects/<name>` route is stubbed per case.
//
// Asserted here, client-side:
//   1. runs are readable off one screen: repo_key, run_id, badge, dates;
//      the title unhides.
//   2. zero runs on a CLEAN read hides the panel entirely (most projects
//      have no orchestration runs — no noise, no confident zero).
//   3. zero runs + a `runs `-prefixed degradation warning forces the panel
//      OPEN and reads as unknown, never as zero.
//   4. non-run warnings (e.g. a missing legacy db note) do NOT open it.
//   5. an HTTP failure is fail-loud (unknown must not look like «no runs»).
//   6. 404 (unknown project) keeps the panel hidden.
//   7. a hostile repo_key arrives escaped.
//   8. an unrecognized status renders with the ✖ fallback badge, and the
//      collector's status word survives verbatim.
//
// Usage: node runs_harness.js <path-to-index.html>
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Document, dispatch} = require(path.join(__dirname, 'dom.js'));
const {browserGlobals, openScreen} = require(path.join(__dirname, 'screens.js'));

const HTML_PATH = process.argv[2];
if (!HTML_PATH) {
  console.error('usage: node runs_harness.js <index.html>');
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

const snap = (extra = {}) => ({
  name: 'Maestro', path: '/repos/maestro', detected: true,
  collected_at: '2026-08-16T10:00:00Z', freshness: null,
  schema_versions: [], models: [], tasks: [], runs: [], test_results: [],
  configs: [], errors: [], warnings: [], ...extra,
});
const RUN = {
  repo_key: 'github.com/acme/app', run_id: '01NEW', status: 'interrupted',
  started_at: '2026-08-12T00:00:00', ended_at: null, reason: null,
  source: '/x/state.db',
};
const LEGACY = {
  repo_key: 'legacy', run_id: null, status: 'legacy',
  started_at: null, ended_at: null, reason: null, source: '/x/maestro.db',
};
const WITH_RUNS = snap({runs: [RUN, LEGACY]});
const CLEAN_ZERO = snap();
const DEGRADED = snap({
  warnings: ['runs enumeration: cannot list /x/projects: denied'],
});
const NON_RUN_WARNING = snap({
  warnings: ['maestro.db not found (~/.maestro/maestro.db; '
    + 'set maestro_db in dispatcher.toml)'],
});
const HOSTILE = snap({
  runs: [{...RUN, repo_key: '<img src=x onerror=alert(1)>'}],
});
const UNKNOWN_STATUS = snap({runs: [{...RUN, status: 'weird-new-state'}]});

function overviewProjects(names) {
  return names.map(name => ({
    name, detected: true, path: `/repos/${name}`,
    counts: {tasks: 0, models: 0, test_results: 0, errors: 0},
    freshness: 'fresh', warnings: [],
  }));
}

function defaultRoutes(names, projectRoute) {
  return [
    [u => u.startsWith('/api/overview'),
      () => ok({projects: overviewProjects(names)})],
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
    [u => u.startsWith('/api/spec-runner-config/suggest-availability'),
      () => ok({available: false, detail: 'n/a'})],
    [u => u.endsWith('/onboarding'), () => ok({
      project: {description: 'a repo', description_source: 'README'},
      roadmap_position: null, next_items: [], live_tasks: [], warnings: [],
    })],
    [u => u.endsWith('/spec-runner-config'), () => resp(404, {detail: 'none'})],
    [u => u.endsWith('/governance'), () => ok({
      state: 'no-data', reason: 'no gate_verdicts.jsonl', header: null,
      artifacts: [], findings: [], unresolvable_findings: [],
    })],
    [u => u.endsWith('/product-proposals'),
      () => resp(404, {detail: {code: 'not-impresario-mirror', message: 'n'}})],
    // The plain snapshot route MUST come after every subpath route: it is
    // the runs panel's source.
    [u => /\/api\/projects\/[^/]+$/.test(u), projectRoute],
  ];
}

const drain = async (turns = 5) => {
  for (let i = 0; i < turns; i++) await new Promise(r => setTimeout(r, 0));
};

async function boot(projectRoute, names = ['Maestro']) {
  const document = new Document(BODY_HTML);
  const routes = defaultRoutes(names, projectRoute);
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
  const page = {ctx, document};
  // The page is a tab shell now: the project cards this harness clicks live
  // inside the hidden `#screen-projects` tabpanel, and dom.js refuses to
  // dispatch on what a person cannot see. Open the screen the way a person
  // does — through the real tab button (tests/web/screens.js).
  await openScreen(page, 'projects');
  await drain();
  return page;
}

async function openDetail(env, index = 0) {
  const card = env.document
    .querySelectorAll('#projects .card[data-name]')[index];
  if (!card) throw new Error('refresh() rendered no selectable project card');
  await Promise.all(dispatch(card.querySelector('h2') || card, 'click'));
  await drain();
}

function screenText(env, id) {
  const node = env.document.getElementById(id);
  if (!node) throw new Error(`#${id} is not in the page markup`);
  return node.visible ? node.textContent : '';
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

testCase('runs are readable off one screen', async () => {
  const env = await boot(() => ok(WITH_RUNS));
  await openDetail(env);
  const text = screenText(env, 'runs');
  check(text.includes('github.com/acme/app'), `repo_key on screen (got: ${text})`);
  check(text.includes('01NEW'), 'run_id on screen');
  check(text.includes('interrupted / unknown'),
    'no-terminal-record renders as interrupted / unknown, never in-progress');
  check(text.includes('2026-08-12T00:00:00'), 'started_at on screen');
  check(text.includes('legacy (frozen pre-#147 file)'),
    'the legacy db is labeled legacy');
  const title = env.document.getElementById('runs-title');
  check(title.visible, 'the section title unhides once runs land');
});

testCase('zero runs on a clean read hides the panel', async () => {
  const env = await boot(() => ok(CLEAN_ZERO));
  await openDetail(env);
  const title = env.document.getElementById('runs-title');
  check(!title.visible, 'no runs + clean enumeration = hidden, no noise');
  check(screenText(env, 'runs') === '', 'no panel content');
});

testCase('degraded enumeration forces the panel open as unknown', async () => {
  const env = await boot(() => ok(DEGRADED));
  await openDetail(env);
  const title = env.document.getElementById('runs-title');
  check(title.visible, 'a degradation warning must not hide as «no runs»');
  const text = screenText(env, 'runs');
  check(text.includes('run enumeration degraded'), 'the degradation note is shown');
  check(text.includes('unknown, not zero'), 'unknown ≠ zero wording');
  check(text.includes('cannot list /x/projects'), 'the warning itself is shown');
});

testCase('non-run warnings do not open the panel', async () => {
  const env = await boot(() => ok(NON_RUN_WARNING));
  await openDetail(env);
  const title = env.document.getElementById('runs-title');
  check(!title.visible,
    'a missing-legacy-db note is not a runs degradation signal');
});

testCase('an HTTP failure is fail-loud, never «no runs»', async () => {
  const env = await boot(() => resp(500, {detail: 'boom'}));
  await openDetail(env);
  const title = env.document.getElementById('runs-title');
  check(title.visible, 'the panel surfaces the failure');
  check(screenText(env, 'runs').includes('runs endpoint failed: 500'),
    'the status code is on screen');
});

testCase('404 (unknown project) keeps the panel hidden', async () => {
  const env = await boot(() => resp(404, {detail: 'unknown project'}));
  await openDetail(env);
  const title = env.document.getElementById('runs-title');
  check(!title.visible, 'a 404 is already surfaced by the onboarding block');
  check(screenText(env, 'runs') === '', 'no panel content on 404');
});

testCase('a hostile repo_key arrives escaped', async () => {
  const env = await boot(() => ok(HOSTILE));
  await openDetail(env);
  const node = env.document.getElementById('runs');
  check(!node.innerHTML.includes('<img'), 'raw markup does not survive esc()');
  check(node.innerHTML.includes('&lt;img'),
    'the repo_key is still readable, escaped');
});

testCase('an unrecognized status gets the ✖ fallback, verbatim', async () => {
  const env = await boot(() => ok(UNKNOWN_STATUS));
  await openDetail(env);
  const text = screenText(env, 'runs');
  check(text.includes('✖ weird-new-state'),
    'a new producer status is visible and not silently green');
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
