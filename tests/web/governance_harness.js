// Exercises the governance panel (WS-005 WS-C) by running the REAL, WHOLE
// <script> of dispatcher/server/static/index.html inside a VM over the page's
// own parsed markup (tests/web/dom.js) — the same discipline as
// task_authoring_harness.js, self-contained on purpose: that harness's
// loader and fixtures are module-local, and importing another test's
// internals would couple the two suites' lifecycles.
//
// Two layers are asserted here:
//   1. the wire: clicking a project card fetches /governance and puts the
//      rendered state on screen (visibility included);
//   2. the pure renderer: renderGovernance() over each of the six states —
//      this is where M-01 (no damaged class reads as pass) and M-02 (the
//      blocker is readable off one screen) live client-side.
//
// Usage: node governance_harness.js <path-to-index.html>
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Document, dispatch} = require(path.join(__dirname, 'dom.js'));

const HTML_PATH = process.argv[2];
if (!HTML_PATH) {
  console.error('usage: node governance_harness.js <index.html>');
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

// ---- page loading (same shape as task_authoring_harness.js) ---------------

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

const HEADER = {
  kind: 'header', schema_version: '1',
  source_commit: 'abababababababababababababababababababab',
  dirty: false, generated_at: '2026-08-02T12:00:00+00:00',
  profile: 'team-exp', bundle: 'workstreams/WS-005-gate-verdicts/spec',
};
const ART = (p, status = 'approved') => ({
  kind: 'artifact', path: p, node_id: null, status, owner_roles: ['qa'],
});
const FINDING = (artifact, message, gate = 'GC-STALE') => ({
  kind: 'finding', gate_id: gate, verdict: 'fail', artifact, message,
  obligation: null, tier: null, phase: null,
  risk_model_version: null, waiver_ref: null,
});
const gov = (state, extra = {}) => ({
  state, reason: null, header: null,
  artifacts: [], findings: [], unresolvable_findings: [], ...extra,
});

const PASS = gov('pass', {
  header: HEADER,
  artifacts: [ART('10-requirements.md'), ART('15-behaviour-spec.md')],
});
const BLOCKED = gov('blocked', {
  header: HEADER,
  artifacts: [ART('10-requirements.md'), ART('15-behaviour-spec.md')],
  findings: [
    FINDING('15-behaviour-spec.md', 'upstream changed since approval'),
    FINDING('10-requirements.md', 'no traces_to link', 'GC-TRACE-EMPTY'),
  ],
});
const UNRESOLVABLE = gov('unresolvable', {
  header: HEADER,
  reason: 'findings reference artifacts outside the inventory: 99-ghost.md',
  artifacts: [ART('10-requirements.md')],
  findings: [FINDING('99-ghost.md', 'ghost', 'GC-COMPLETENESS')],
  unresolvable_findings: [FINDING('99-ghost.md', 'ghost', 'GC-COMPLETENESS')],
});

function defaultRoutes(governance) {
  return [
    [u => u.startsWith('/api/overview'), () => ok({projects: [{
      name: 'widget', detected: true, path: '/repos/widget',
      counts: {tasks: 0, models: 0, test_results: 0, errors: 0},
      freshness: 'fresh', warnings: [],
    }]})],
    [u => u.startsWith('/api/errors'), () => ok([])],
    [u => u.startsWith('/api/models'), () => ok([])],
    [u => u.startsWith('/api/contracts'), () => ok([])],
    [u => u.startsWith('/api/roadmap/summary'), () => ok({projects: []})],
    [u => u.startsWith('/api/roadmap'), () => ok({roadmaps: [], items: []})],
    [u => u.startsWith('/api/sync'), () => ok({
      fetch_in_flight: false,
      report: {top_line: 'ok', top_reason: null, proposals: [], hosts: []},
    })],
    [u => u.startsWith('/api/actions/session'), () => ok({token: 'test-token'})],
    [u => u.startsWith('/api/spec-runner-config/suggest-availability'),
      () => ok({available: false, detail: 'n/a'})],
    [u => u.endsWith('/onboarding'), () => ok({
      project: {description: 'a repo', description_source: 'README'},
      roadmap_position: null, next_items: [], live_tasks: [], warnings: [],
    })],
    [u => u.endsWith('/spec-runner-config'), () => resp(404, {detail: 'none'})],
    [u => u.endsWith('/governance'), () => ok(governance)],
  ];
}

const drain = async (turns = 5) => {
  for (let i = 0; i < turns; i++) await new Promise(r => setTimeout(r, 0));
};

async function boot(governance) {
  const document = new Document(BODY_HTML);
  const routes = defaultRoutes(governance);
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
    window: {open: () => {}},
  };
  vm.createContext(ctx);
  vm.runInContext(PAGE_SCRIPT, ctx);
  await drain();
  return {ctx, document};
}

async function openDetail(env) {
  const card = env.document
    .querySelectorAll('#projects .card[data-name]')[0];
  if (!card) throw new Error('refresh() rendered no selectable project card');
  await Promise.all(dispatch(card.querySelector('h2') || card, 'click'));
  await drain();
}

function screenText(env, id) {
  const node = env.document.getElementById(id);
  if (!node) throw new Error(`#${id} is not in the page markup`);
  return node.visible ? node.textContent : '';
}

function render(env, payload) {
  return vm.runInContext(
    `renderGovernance(${JSON.stringify(payload)})`, env.ctx);
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

testCase('clicking a card fetches /governance and shows the state', async () => {
  const env = await boot(PASS);
  await openDetail(env);
  const text = screenText(env, 'governance');
  check(text.includes('pass'), `panel shows the state (got: ${text})`);
  check(text.includes('2026-08-02T12:00:00+00:00'),
    'provenance: generated_at is on screen (FR-05)');
  check(text.includes(HEADER.source_commit.slice(0, 12)),
    'provenance: source_commit prefix is on screen (FR-05)');
  const title = env.document.getElementById('governance-title');
  check(title.visible, 'the section title unhides once data lands');
});

testCase('no-data keeps the pass badge off screen', async () => {
  const env = await boot(gov('no-data', {reason: 'no gate_verdicts.jsonl'}));
  await openDetail(env);
  const text = screenText(env, 'governance');
  check(!text.includes('✅'), 'no green badge for no-data (M-01)');
  check(text.includes('no-data'), 'the state is named');
  check(text.includes('no gate_verdicts.jsonl'), 'the reason is shown');
});

testCase('every non-pass state renders without the pass badge', async () => {
  const env = await boot(PASS);
  const states = [
    gov('blocked'), gov('no-data'), gov('stale', {reason: 'r'}),
    gov('unreadable', {reason: 'invalid JSON (line 3)'}),
    UNRESOLVABLE,
  ];
  for (const payload of states) {
    const out = render(env, payload);
    check(!out.includes('✅'),
      `${payload.state}: no green badge in the output (M-01)`);
    check(out.includes(payload.state), `${payload.state}: state is named`);
  }
  const unreadable = render(env,
    gov('unreadable', {reason: 'invalid JSON (line 3)'}));
  check(unreadable.includes('invalid JSON (line 3)'),
    'unreadable: the reason is part of the output');
});

testCase('blocked: findings and artifact marks are readable (M-02, BEH-07)', async () => {
  const env = await boot(PASS);
  const out = render(env, BLOCKED);
  check(out.includes('GC-STALE') && out.includes('GC-TRACE-EMPTY'),
    'both gate ids are in the output');
  check(out.includes('upstream changed since approval'),
    'the finding message is readable off this one screen (M-02)');
  // BEH-07: clean artifacts shown apart from blocked ones — here every
  // artifact has a finding, so every artifact row carries the block mark.
  const rows = out.split('<li>').slice(1);
  const artifactRows = rows.filter(r => r.includes('approved'));
  check(artifactRows.length === 2 && artifactRows.every(r => r.includes('⛔')),
    'artifact rows with findings carry the block mark');
});

testCase('pass artifacts carry no block mark (BEH-07, clean side)', async () => {
  const env = await boot(PASS);
  const out = render(env, PASS);
  const rows = out.split('<li>').slice(1);
  check(rows.length === 2 && rows.every(r => !r.includes('⛔')),
    'clean artifact rows are unmarked');
});

testCase('unresolvable: the dangling finding is annotated', async () => {
  const env = await boot(PASS);
  const out = render(env, UNRESOLVABLE);
  check(out.includes('(outside the inventory)'),
    'the dangling finding is called out');
  check(out.includes('99-ghost.md'), 'the ghost path is named');
});

testCase('a hostile finding message arrives escaped', async () => {
  const env = await boot(PASS);
  const payload = gov('blocked', {
    header: HEADER,
    artifacts: [ART('10-requirements.md')],
    findings: [FINDING('10-requirements.md', '<img src=x onerror=alert(1)>')],
  });
  const out = render(env, payload);
  check(!out.includes('<img'), 'raw markup does not survive esc()');
  check(out.includes('&lt;img'), 'the message is still readable, escaped');
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
