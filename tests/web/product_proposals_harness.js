// Exercises the product-proposals panel (inbox #129 PR-2) by running the
// REAL, WHOLE <script> of dispatcher/server/static/index.html inside a VM
// over the page's own parsed markup (tests/web/dom.js) — the same
// discipline as governance_harness.js, self-contained on purpose.
//
// Asserted here, client-side:
//   1. the wire: selecting the impresario project fetches /product-proposals
//      and puts the wait on screen (proposal, gate, authority, «Proposal
//      updated») — readable off one screen;
//   2. a non-ok bundle NEVER reads as «0 gates waiting» (suppressed wording);
//   3. «0 gates waiting» vs «0 bundles» are distinct labels;
//   4. 404 hides the section; 200 + mirror diagnostics shows an error;
//   5. the fetch-race guard: a late response for the PREVIOUS project never
//      renders into the new panel — including when the stale call is parked
//      in an EARLIER await (governance) at the moment the race guard's
//      token is taken, not just when it is parked in the pp fetch itself;
//   6. no local path becomes an href; hostile strings arrive escaped.
//
// Usage: node product_proposals_harness.js <path-to-index.html>
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Document, dispatch} = require(path.join(__dirname, 'dom.js'));

const HTML_PATH = process.argv[2];
if (!HTML_PATH) {
  console.error('usage: node product_proposals_harness.js <index.html>');
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
const notMirror = () => resp(404,
  {detail: {code: 'not-impresario-mirror', message: 'nope'}});

const WAIT = {
  proposal_id: 'PP-101', gate_id: 'qg5_business', gate_label: 'Gate A',
  authority: 'business_owner', artifact_ref: 'proposal://PP-101',
  bundle_path: 'pilot/forconcept/pp-101', version: 6,
  proposal_updated_at: '2026-08-12T04:12:30Z',
};
const OK_BUNDLE = {
  path: 'pilot/forconcept/pp-101', state: 'ok', diagnostics: [],
  proposal_id: 'PP-101', status: 'ready_for_business', version: 6,
  updated_at: '2026-08-12T04:12:30Z', waits: [WAIT],
  loop_status: 'absent', loop_waits: [],
};
const report = (extra = {}) => ({
  mirror_path: '/repos/impresario', bundles: [], waits: [],
  diagnostics: [], attention: false, needs_human: [], ...extra,
});
const WAITING = report({bundles: [OK_BUNDLE], waits: [WAIT]});
const SUPPRESSED = report({
  attention: true,
  bundles: [{
    path: 'pilot/forconcept/pp-101', state: 'unknown',
    diagnostics: [{
      code: 'decision-unreadable',
      message: 'UnicodeDecodeError: bad byte',
      path: 'pilot/forconcept/pp-101/decisions/gd-001.yaml',
    }],
    proposal_id: 'PP-101', status: 'ready_for_business', version: 6,
    updated_at: '2026-08-12T04:12:30Z', waits: [],
    loop_status: 'unknown', loop_waits: [],
  }],
});
const ANCHORS_MISSING = report({
  attention: true,
  diagnostics: [{
    code: 'mirror-anchors-missing',
    message: 'expected impresario anchor is not a file',
    path: 'docs/semantics.md',
  }],
});
const LOOP_WAIT = {
  loop_id: 'LOOP-101', iteration: 2, proposal_id: 'PP-101',
  reason: 'решить exempt-семантику', stopped_at: '2026-08-12T04:01:21Z',
  bundle_path: 'pilot/forconcept/pp-101',
};
const LOOP_WAITING = report({
  bundles: [{...OK_BUNDLE, status: 'approved', waits: [],
    loop_status: 'needs_human', loop_waits: [LOOP_WAIT]}],
  needs_human: [LOOP_WAIT],
});
const LOOP_TERMINAL = report({
  bundles: [{...OK_BUNDLE, status: 'approved', waits: [],
    loop_status: 'ready_for_business', loop_waits: []}],
});
const BACKLOG_WAIT = {
  backlog_id: 'BL-ecosystem', gate_id: 'qg4_backlog', gate_label: 'QG-4',
  authority: 'qg4_selector', artifact_ref: 'backlog://BL-ecosystem',
  artifact_path: 'pilot/backlog.yaml', version: 4,
  backlog_updated_at: '2026-08-17T02:42:23Z',
  selectable_idea_refs: ['idea://IDEA-103', 'idea://IDEA-106'],
};
const OK_BACKLOG = {
  path: 'pilot', state: 'ok', diagnostics: [], backlog_id: 'BL-ecosystem',
  version: 4, updated_at: '2026-08-17T02:42:23Z', waits: [BACKLOG_WAIT],
};
const BACKLOG_WAITING = report({
  backlog_bundles: [OK_BACKLOG], backlog_waits: [BACKLOG_WAIT],
});
const BACKLOG_SUPPRESSED = report({
  attention: true,
  backlog_bundles: [{
    path: 'pilot', state: 'unreadable',
    diagnostics: [{
      code: 'backlog-unreadable',
      message: 'UnicodeDecodeError: bad byte',
      path: 'pilot/backlog.yaml',
    }],
    backlog_id: null, version: null, updated_at: null, waits: [],
  }],
});

function overviewProjects(names) {
  return names.map(name => ({
    name, detected: true, path: `/repos/${name}`,
    counts: {tasks: 0, models: 0, test_results: 0, errors: 0},
    freshness: 'fresh', warnings: [],
  }));
}

const DEFAULT_GOVERNANCE = () => ok({
  state: 'no-data', reason: 'no gate_verdicts.jsonl', header: null,
  artifacts: [], findings: [], unresolvable_findings: [],
});

function defaultRoutes(names, ppRoute, govRoute) {
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
    [u => u.endsWith('/governance'), govRoute || DEFAULT_GOVERNANCE],
    [u => u.endsWith('/product-proposals'), ppRoute],
  ];
}

const drain = async (turns = 5) => {
  for (let i = 0; i < turns; i++) await new Promise(r => setTimeout(r, 0));
};

async function boot(ppRoute, names = ['impresario'], govRoute) {
  const document = new Document(BODY_HTML);
  const routes = defaultRoutes(names, ppRoute, govRoute);
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

function render(env, payload) {
  return vm.runInContext(
    `renderProductProposals(${JSON.stringify(payload)})`, env.ctx);
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

testCase('a wait is readable off one screen', async () => {
  const env = await boot(() => ok(WAITING));
  await openDetail(env);
  const text = screenText(env, 'product-proposals');
  check(text.includes('proposal://PP-101'), `artifact_ref on screen (got: ${text})`);
  check(text.includes('Gate A'), 'gate label on screen');
  check(text.includes('business_owner'), 'authority on screen');
  check(text.includes('2026-08-12T04:12:30Z'), '«Proposal updated» value on screen');
  check(text.includes('Proposal updated'), 'the column is labelled «Proposal updated», not «since»');
  check(text.includes('pilot/forconcept/pp-101'), 'bundle path identifies the bundle');
  const title = env.document.getElementById('product-proposals-title');
  check(title.visible, 'the section title unhides once data lands');
});

testCase('non-ok bundle never reads as «0 gates waiting» (M-01 analogue)', async () => {
  const env = await boot(() => ok(SUPPRESSED));
  await openDetail(env);
  const text = screenText(env, 'product-proposals');
  check(!text.includes('0 gates waiting'),
    'suppressed classification must not read as zero waits');
  check(text.includes('classification suppressed'),
    'the suppressed wording is on screen');
  check(text.includes('decision-unreadable'),
    'the diagnostic code is on screen');
});

testCase('«0 gates waiting» and «0 bundles» are distinct labels', async () => {
  const env = await boot(() => ok(report()));
  await openDetail(env);
  check(screenText(env, 'product-proposals').includes('0 bundles'),
    'an empty healthy mirror says «0 bundles»');
  const allOk = report({
    bundles: [{...OK_BUNDLE, state: 'ok', status: 'approved', waits: []}],
  });
  const out = render(env, allOk);
  check(out.includes('0 gates waiting'),
    'an all-ok mirror with no waits says «0 gates waiting»');
  check(!out.includes('0 bundles'), 'the two labels do not blur');
});

testCase('a report-level diagnostic forbids the confident zero', async () => {
  // Zero-state rule (refined for cross-surface parity): «0 gates/loops
  // waiting» needs a FULLY classified scan — all-ok bundles AND no
  // report-level diagnostic. A degraded scan renders as incomplete.
  const env = await boot(() => ok(report({
    attention: true,
    bundles: [{...OK_BUNDLE, state: 'ok', status: 'approved', waits: []}],
    diagnostics: [{code: 'scan-degraded', message: 'one pilot dir was lost'}],
  })));
  await openDetail(env);
  const text = screenText(env, 'product-proposals');
  check(text.includes('scan-degraded'), 'the diagnostic code is shown');
  check(!text.includes('0 gates waiting'),
    'a degraded scan must not read as a confident global zero');
  check(!text.includes('0 loops waiting'), 'same for the loops label');
});

testCase('404 not-impresario-mirror hides the section', async () => {
  const env = await boot(notMirror, ['widget']);
  await openDetail(env);
  const title = env.document.getElementById('product-proposals-title');
  check(!title.visible, 'the section stays hidden on 404');
  check(screenText(env, 'product-proposals') === '',
    'no panel content for a non-impresario project');
});

testCase('mirror diagnostics (200) show an error instead of hiding', async () => {
  const env = await boot(() => ok(ANCHORS_MISSING));
  await openDetail(env);
  const text = screenText(env, 'product-proposals');
  check(text.includes('mirror-anchors-missing'), 'the diagnostic code is shown');
  const title = env.document.getElementById('product-proposals-title');
  check(title.visible, 'the section is visible, not hidden');
});

testCase('a late response for the previous project never renders (race guard)', async () => {
  let releaseSlow;
  const slow = new Promise(resolve => { releaseSlow = resolve; });
  const ppRoute = u => u.includes('impresario')
    ? slow.then(() => ok(WAITING))
    : notMirror();
  const env = await boot(ppRoute, ['impresario', 'widget']);
  // Click impresario WITHOUT awaiting its handlers: detail() is pending on
  // the hanging fetch — awaiting it here (openDetail does) would deadlock
  // the case. The un-awaited promise settles after releaseSlow(); the final
  // drain below (and the run-level drain) let it land inside this case.
  const first = env.document
    .querySelectorAll('#projects .card[data-name]')[0];
  dispatch(first.querySelector('h2') || first, 'click');
  await drain();
  await openDetail(env, 1);   // widget: resolves immediately with 404 -> hidden
  releaseSlow();              // the stale impresario response lands LAST
  await drain();
  const title = env.document.getElementById('product-proposals-title');
  check(!title.visible,
    'the stale impresario response must not unhide the widget panel');
  check(screenText(env, 'product-proposals') === '',
    'no stale wait rendered into the new panel');
});

testCase('a stale call parked in governance (not pp) must not grab a fresh token (race guard II)', async () => {
  // Final review MUST-FIX: the guard token has to be taken at detail()
  // entry, before the onboarding/spec-runner-config/governance awaits — not
  // just before the pp fetch. This delays GOVERNANCE (an EARLIER await),
  // not product-proposals, to prove the stale call is already behind ppGen
  // by the time it reaches the pp block, however late it resumes.
  let releaseGov;
  const slowGov = new Promise(resolve => { releaseGov = resolve; });
  const govRoute = u => u.includes('impresario')
    ? slowGov.then(DEFAULT_GOVERNANCE)
    : DEFAULT_GOVERNANCE();
  const ppRoute = u => u.includes('impresario') ? ok(WAITING) : notMirror();
  const env = await boot(ppRoute, ['impresario', 'widget'], govRoute);
  // Click impresario WITHOUT awaiting: detail() parks on the slow governance
  // fetch, i.e. BEFORE it ever reaches the pp block. Awaiting here would
  // deadlock the case (same discipline as the race-guard case above).
  const first = env.document
    .querySelectorAll('#projects .card[data-name]')[0];
  dispatch(first.querySelector('h2') || first, 'click');
  await drain();
  await openDetail(env, 1);   // widget: pp 404 -> hidden
  releaseGov();               // the stale impresario call resumes LAST
  await drain();
  const title = env.document.getElementById('product-proposals-title');
  check(!title.visible,
    'a stale call resuming from a delayed governance fetch must not unhide the widget panel');
  const text = screenText(env, 'product-proposals');
  check(text === '', 'no stale panel content rendered into the new panel');
  check(!text.includes('proposal://PP-101'),
    'the previous project\'s wait must not leak in via a freshly-grabbed token');
});

testCase('no local path becomes an href; hostile strings arrive escaped', async () => {
  const env = await boot(() => ok(WAITING));
  const hostile = report({
    attention: true,
    bundles: [{
      ...OK_BUNDLE,
      state: 'unknown',
      waits: [],
      diagnostics: [{
        code: 'decision-unreadable',
        message: '<img src=x onerror=alert(1)>',
        path: 'pilot/x',
      }],
    }],
  });
  const out = render(env, hostile);
  check(!out.includes('<img'), 'raw markup does not survive esc()');
  check(out.includes('&lt;img'), 'the message is still readable, escaped');
  const waiting = render(env, WAITING);
  check(!waiting.includes('<a '), 'bundle paths render as text, never links');
});

testCase('a needs_human wait is readable off one screen', async () => {
  const env = await boot(() => ok(LOOP_WAITING));
  await openDetail(env);
  const text = screenText(env, 'product-proposals');
  check(text.includes('LOOP-101'), `loop id on screen (got: ${text})`);
  check(text.includes('решить exempt-семантику'), 'reason on screen');
  check(text.includes('Stopped at'), 'the column is labelled «Stopped at»');
  check(text.includes('2026-08-12T04:01:21Z'), 'stop.at value on screen');
  check(text.includes('loop: needs_human'), 'the loop chip names the status');
  check(!text.includes('0 loops waiting'), 'no zero-label next to a live wait');
});

testCase('terminal loop: chip + «0 loops waiting», no table', async () => {
  const env = await boot(() => ok(LOOP_TERMINAL));
  await openDetail(env);
  const text = screenText(env, 'product-proposals');
  check(text.includes('loop: ready_for_business'), 'terminal chip visible');
  check(text.includes('0 loops waiting'), 'explicit zero-label present');
  check(!text.includes('Stopped at'), 'no needs_human table for a terminal loop');
});

testCase('loop: absent is visible, not hidden (owner-fixed)', async () => {
  const env = await boot(() => ok(WAITING));
  await openDetail(env);
  const text = screenText(env, 'product-proposals');
  check(text.includes('loop: absent'),
    'absent is an explicit read-model state and stays visible');
});

testCase('suppressed bundle shows loop: unknown, never «0 loops waiting»', async () => {
  const env = await boot(() => ok(SUPPRESSED));
  await openDetail(env);
  const text = screenText(env, 'product-proposals');
  check(text.includes('loop: unknown'), 'unknown chip on the suppressed bundle');
  check(!text.includes('0 loops waiting'),
    'suppressed loop classification must not read as zero loops waiting');
});

testCase('a hostile loop reason arrives escaped', async () => {
  const env = await boot(() => ok(WAITING));
  const hostile = report({
    bundles: [{...OK_BUNDLE, status: 'approved', waits: [],
      loop_status: 'needs_human',
      loop_waits: [{...LOOP_WAIT, reason: '<img src=x onerror=alert(1)>'}]}],
    needs_human: [{...LOOP_WAIT, reason: '<img src=x onerror=alert(1)>'}],
  });
  const out = render(env, hostile);
  check(!out.includes('<img'), 'raw markup does not survive esc()');
  check(out.includes('&lt;img'), 'the reason is still readable, escaped');
});

testCase('a qg4_backlog wait is readable off one screen', async () => {
  const env = await boot(() => ok(BACKLOG_WAITING));
  await openDetail(env);
  const text = screenText(env, 'product-proposals');
  check(text.includes('backlog://BL-ecosystem'),
    `backlog artifact_ref on screen (got: ${text})`);
  check(text.includes('QG-4'), 'gate label on screen');
  check(text.includes('qg4_selector'), 'authority on screen');
  check(text.includes('v4'), 'the waited-on backlog version on screen');
  check(text.includes('2026-08-17T02:42:23Z'), 'backlog updated_at on screen');
  check(text.includes('pilot/backlog.yaml'), 'artifact path identifies the file');
  check(text.includes('idea://IDEA-103'), 'selectable idea refs visible as context');
});

testCase('suppressed backlog never reads as «0 backlog gates waiting»', async () => {
  const env = await boot(() => ok(BACKLOG_SUPPRESSED));
  await openDetail(env);
  const text = screenText(env, 'product-proposals');
  check(!text.includes('0 backlog gates waiting'),
    'suppressed backlog classification must not read as zero waits');
  check(text.includes('classification suppressed'), 'suppressed wording on screen');
  check(text.includes('backlog-unreadable'), 'the diagnostic code is on screen');
});

testCase('a decided backlog reads as an explicit zero (fully classified only)', async () => {
  const env = await boot(() => ok(BACKLOG_WAITING));
  const decided = report({backlog_bundles: [{...OK_BACKLOG, waits: []}]});
  const out = render(env, decided);
  check(out.includes('0 backlog gates waiting'),
    'an all-ok decided backlog says «0 backlog gates waiting»');
  const degraded = report({
    attention: true,
    backlog_bundles: [{...OK_BACKLOG, waits: []}],
    diagnostics: [{code: 'scan-degraded', message: 'one pilot dir was lost'}],
  });
  const degradedOut = render(env, degraded);
  check(!degradedOut.includes('0 backlog gates waiting'),
    'a degraded scan must not read as a confident backlog zero');
});

testCase('a hostile backlog id arrives escaped, paths never become links', async () => {
  const env = await boot(() => ok(BACKLOG_WAITING));
  const hostile = report({
    attention: true,
    backlog_bundles: [{...OK_BACKLOG, state: 'unknown', waits: [],
      diagnostics: [{code: 'decision-schema-invalid',
        message: '<img src=x onerror=alert(1)>', path: 'pilot/x'}]}],
  });
  const out = render(env, hostile);
  check(!out.includes('<img'), 'raw markup does not survive esc()');
  check(out.includes('&lt;img'), 'the message is still readable, escaped');
  const waiting = render(env, BACKLOG_WAITING);
  check(!waiting.includes('<a '), 'backlog paths render as text, never links');
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
