// Exercises the «My turn» tab (spec 2026-09-30-my-turn-b2-design) by running
// the REAL, WHOLE <script> of dispatcher/server/static/index.html in a VM over
// the page's own parsed markup (tests/web/dom.js), opening the tab by clicking
// it, and asserting what `renderMyTurn` painted from a stubbed
// `/api/human-queue`.
//
// Asserted here, client-side:
//   1. known ages first in server order, then an "age unknown" divider;
//   2. a command is shown as text with an https PR link and its note;
//   3. a non-https PR url and a non-fragment run link are never clickable;
//   4. an incomplete queue names its sources and never reads as empty;
//   5. a failed read says "not read", never "nothing waits for you";
//   6. producer strings arrive escaped; a refused act shows its reason;
//   7. a wait without `prepared` (an older server) says so.
//
// Usage: node my_turn_harness.js <path-to-index.html>
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Document} = require(path.join(__dirname, 'dom.js'));
const {browserGlobals, openScreen} = require(path.join(__dirname, 'screens.js'));

const HTML_PATH = process.argv[2];
if (!HTML_PATH) {
  console.error('usage: node my_turn_harness.js <index.html>');
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

const HOUR = 3600000;
const iso = msAgo => new Date(Date.now() - msAgo).toISOString();

function wait(key, extra = {}) {
  return {
    key, reasons: ['pr_human_merge'], source: 'forge_labelled_prs',
    repo: 'andrei-shtanakov/deployer', ref: key, title: `wait ${key}`,
    since: null, since_basis: null, act: {kind: 'run_view', request_id: 'r'},
    prepared: {kind: 'command', text: `sh devtools/human-merge.sh deployer 1`,
      url: 'https://github.com/andrei-shtanakov/deployer/pull/1',
      note: 'Typed in, not executed.'},
    ...extra,
  };
}

function view(waits, extra = {}) {
  return {waits, sources: {}, complete: true,
    generated_at: new Date().toISOString(), ...extra};
}

function defaultRoutes(queueRoute) {
  return [
    [u => u.startsWith('/api/overview'), () => ok({projects: []})],
    [u => u.startsWith('/api/benchmarks'), () => ok({
      fetch_in_flight: false,
      report: {status: 'unconfigured', url: null, fetched_at: null,
        error: null, benchmarks: [], leaderboards: {}},
    })],
    [u => u.startsWith('/api/human-queue'), queueRoute],
  ];
}

const drain = async (turns = 5) => {
  for (let i = 0; i < turns; i++) await new Promise(r => setTimeout(r, 0));
};

async function bootOpen(queueRoute) {
  const document = new Document(BODY_HTML);
  const routes = defaultRoutes(queueRoute);
  // Launchpad drives its own fetch at boot; this harness has no fixture for
  // it and does not assert on it, so only that one expected failure is quiet.
  const quiet = {...console, error: (...args) => {
    if (!String(args[0]).startsWith('launchpad snapshot fetch failed')) {
      console.error(...args);
    }
  }};
  const ctx = {
    document, console: quiet, URL,
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
  const env = {ctx, document};
  await openScreen(env, 'my-turn');
  await drain();
  return env;
}

const body = env => env.document.querySelector('#my-turn tbody');
const text = (env, id) => env.document.getElementById(id).textContent;

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

testCase('known ages first, then the age-unknown group', async () => {
  const env = await bootOpen(() => ok(view([
    wait('old', {since: iso(72 * HOUR), since_basis: 'label added'}),
    wait('new', {since: iso(3 * HOUR), since_basis: 'label added'}),
    wait('nobody-knows'),
  ])));
  const rows = body(env).querySelectorAll('tr').map(r => r.textContent);
  check(rows.length === 4, `3 waits + 1 divider, got ${rows.length}`);
  check(rows[0].includes('3d') && rows[0].includes('wait old'),
    `oldest first with its age, got ${rows[0]}`);
  check(rows[1].includes('3h') && rows[1].includes('wait new'), `then 3h, got ${rows[1]}`);
  check(rows[2].includes('age unknown (1)'), `divider, got ${rows[2]}`);
  check(rows[3].includes('wait nobody-knows'), `unknown last, got ${rows[3]}`);
  check(text(env, 'my-turn-count') === '3 wait(s) for you',
    `count, got ${text(env, 'my-turn-count')}`);
});

testCase('a command is text with its PR link and note — nothing runs it', async () => {
  const env = await bootOpen(() => ok(view([wait('a')])));
  const html = body(env).innerHTML;
  check(html.includes('<code>sh devtools/human-merge.sh deployer 1</code>'),
    `command shown as code, got ${html}`);
  const links = body(env).querySelectorAll('a');
  check(links.length === 1
    && links[0].attributes.href === 'https://github.com/andrei-shtanakov/deployer/pull/1',
    'the PR is the one link');
  check(body(env).textContent.includes('Typed in, not executed.'), 'the note is shown');
  check(body(env).querySelectorAll('button').length === 0, 'no button runs anything');
});

testCase('only https PR links and fragment run links are clickable', async () => {
  const env = await bootOpen(() => ok(view([
    wait('js', {prepared: {kind: 'command', text: 'x',
      url: 'javascript:alert(1)', note: ''}}),
    wait('run', {prepared: {kind: 'link', url: '#launchpad/r1', note: 'run view'}}),
    wait('evil', {prepared: {kind: 'link', url: 'javascript:alert(2)', note: ''}}),
  ])));
  const hrefs = body(env).querySelectorAll('a').map(a => a.attributes.href);
  check(JSON.stringify(hrefs) === JSON.stringify(['#launchpad/r1']),
    `only the fragment link, got ${JSON.stringify(hrefs)}`);
  check(body(env).textContent.includes('unexpected link — not shown'),
    'the refused link says so');
});

testCase('an incomplete queue names its sources and is never empty', async () => {
  const env = await bootOpen(() => ok(view([], {
    complete: false,
    sources: {
      maestro: {state: 'ok', detail: null},
      forge_labelled_prs: {state: 'unavailable', detail: 'first pr-search in progress'},
      impresario: {state: 'not_configured', detail: null},
    },
  })));
  const banner = env.document.getElementById('my-turn-incomplete');
  check(banner.hidden === false, 'the banner is shown');
  check(banner.textContent.includes('forge_labelled_prs: unavailable — first pr-search in progress'),
    `names the source, got ${banner.textContent}`);
  check(!banner.textContent.includes('impresario'), 'not_configured is not a hole');
  check(body(env).textContent.includes('no known waits — queue incomplete'),
    `never "nothing waits", got ${body(env).textContent}`);
  check(text(env, 'my-turn-count') === '0 known · incomplete',
    `count, got ${text(env, 'my-turn-count')}`);
});

testCase('a complete empty queue is a real zero', async () => {
  const env = await bootOpen(() => ok(view([])));
  check(body(env).textContent.includes('nothing waits for you'), 'confident zero');
  check(env.document.getElementById('my-turn-incomplete').hidden, 'no banner');
});

testCase('a failed read says not read, never nothing waits', async () => {
  const env = await bootOpen(() => resp(503, {detail: 'down'}));
  check(text(env, 'my-turn-count') === 'not read', `got ${text(env, 'my-turn-count')}`);
  check(body(env).textContent.includes('human queue unavailable'), 'the row says so');
  check(!body(env).textContent.includes('nothing waits'), 'never a confident zero');
});

testCase('producer strings are escaped; a refusal shows its reason', async () => {
  const env = await bootOpen(() => ok(view([
    wait('x', {title: '<img src=x onerror=alert(1)>',
      prepared: {kind: 'refused', note: 'the PR\'s identifiers are unusable'}}),
  ])));
  check(body(env).querySelector('img') === null, 'no element from a title');
  check(body(env).innerHTML.includes('&lt;img'), 'the title is readable, escaped');
  check(body(env).textContent.includes('identifiers are unusable'), 'the refusal reason');
});

testCase('a wait without prepared (older server) says so', async () => {
  const w = wait('old-server');
  delete w.prepared;
  const env = await bootOpen(() => ok(view([w])));
  check(body(env).textContent.includes('server predates B2'), 'named, not blank');
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
