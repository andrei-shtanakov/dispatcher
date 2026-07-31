// Exercises the task-authoring screen by running the REAL, WHOLE <script> of
// dispatcher/server/static/index.html inside a VM over the page's own parsed
// markup (tests/web/dom.js). Nothing is sliced by string markers, nothing is
// re-implemented, no handler is simulated, and no assertion checks that the
// source "contains" some literal: every case reaches the shipped code through
// the events a person produces — click a project card, click "Create task
// request", type a slug, fire `change`, click "Create request" — and then
// asserts on real state (the button's `disabled` flag, `taRepo`, the anchors
// actually rendered, the text actually displayed).
//
// Why the whole page script rather than an extracted module: this block reads
// and writes module-scope state owned by the rest of the file — `mgEntryRepo`,
// which `detail()` sets from a card's `data-dir`, which `refresh()` renders
// from `mgDirBasename(p.path)`. Extracting "the pure UI functions" would have
// cut exactly the wire the data-dir rule lives on. Running the whole file
// keeps that rule verifiable end to end and costs only a fixture for each API
// the page calls on load.
//
// Failure policy: a case whose expectations do not match FAILS; a case whose
// handler rejects FAILS (its rejection is awaited, never orphaned); anything
// that still escapes — a detached promise, a throwing timer callback — is
// caught by the process-level handlers below, counted, and forces a non-zero
// exit. The summary can never say "N/N passed" beside one. `--selftest=NAME`
// injects a deliberate failure of each kind so this policy is itself tested;
// tests/web/runner_selftest.js runs those in child processes and asserts the
// exit codes.
//
// Usage: node task_authoring_harness.js <path-to-index.html> [--selftest=NAME]
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Document, dispatch} = require(path.join(__dirname, 'dom.js'));

const HTML_PATH = process.argv[2];
const SELFTEST_ARG = process.argv.find(a => a.startsWith('--selftest='));
const SELFTEST = SELFTEST_ARG ? SELFTEST_ARG.split('=')[1] : null;

if (!HTML_PATH) {
  console.error(
    'usage: node task_authoring_harness.js <index.html> [--selftest=NAME]');
  process.exit(2);
}

// ---- process-level error policy -------------------------------------------

// `caseFailures` counts cases whose expectations did not hold; `asyncErrors`
// counts faults that escaped a case entirely. Both are failures — they are
// tallied apart only so the summary can name which kind happened, never so
// one of them can be quietly left out of the exit code.
let caseFailures = 0;
let asyncErrors = 0;
const failed = () => caseFailures + asyncErrors;
let currentCase = '(startup)';
let summaryClaimedClean = false;
// The run is only meaningful if it REACHED the end. An `await` on a promise
// that never settles is not an error in Node: the event loop simply drains
// and the process exits 0 — mid-run, with no summary and every remaining
// case silently skipped. That is this harness's own "green while covering
// nothing" failure, and it happened here (a case that clicked Create against
// a deliberately pending fetch swallowed the rest of the suite). The exit
// hook below turns "the summary never printed" into a non-zero exit.
let summaryPrinted = false;

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
process.on('exit', code => {
  if (!summaryPrinted) {
    process.exitCode = 1;
    console.error(
      `\nRUN DID NOT FINISH: the process exited during ${currentCase} without `
      + 'reaching the summary. Every case after that one was silently skipped '
      + '— most likely an await on a promise that never settles.');
    return;
  }
  if (code !== 0 && summaryClaimedClean) {
    console.error(
      'LATE ASYNC ERROR: an async failure surfaced after the summary was '
      + 'printed — the summary above is WRONG, the exit code is right.');
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
  throw new Error(
    `${HTML_PATH}: expected exactly one <script> in <body>; this harness runs `
    + 'the whole page script and would otherwise silently skip one');
}
const PAGE_SCRIPT = between(BODY_HTML, /<script[^>]*>/i, '</script>', 'script');

// ---- fixtures --------------------------------------------------------------

const GOOD_REF = {
  number: 12, title: 'add feature X', state: 'open',
  url: 'https://github.com/acme/widget/issues/12',
  author: 'octocat', labels: ['inbox'],
};
const SECOND_REF = {
  ...GOOD_REF, number: 13, url: 'https://github.com/acme/widget/issues/13',
};
// Parses as https: — confirmed against the platform parser, not assumed —
// while carrying a double quote that would close the href attribute if the
// sink rendered it unescaped.
const XSS_URL = 'https://gh.com/a"><img src=x onerror=alert(1)>';
// What `new URL()` canonicalises it to — the only form allowed to reach an
// href. Computed by the platform parser, not hand-written.
const XSS_HREF = new URL(XSS_URL).href;

const resp = (status, body) => ({
  status, ok: status >= 200 && status < 300,
  json: () => Promise.resolve(body),
});
const ok = body => resp(200, body);

const ONBOARDING = {
  project: {description: 'a repo', description_source: 'README'},
  roadmap_position: null, next_items: [], live_tasks: [], warnings: [],
};

const overviewFor = project => ({
  projects: [{
    name: project.name, detected: true, path: project.path,
    counts: {tasks: 1, models: 0, test_results: 0, errors: 0},
    freshness: 'fresh', warnings: [],
  }],
});

function defaultRoutes(project) {
  return [
    [u => u.startsWith('/api/overview'), () => ok(overviewFor(project))],
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
    [u => u.endsWith('/onboarding'), () => ok(ONBOARDING)],
    [u => u.endsWith('/spec-runner-config'), () => resp(404, {detail: 'none'})],
  ];
}

// ---- environment -----------------------------------------------------------

const drain = async (turns = 5) => {
  for (let i = 0; i < turns; i++) await new Promise(r => setTimeout(r, 0));
};

function makeEnv(project) {
  const document = new Document(BODY_HTML);
  const routes = defaultRoutes(project);
  const windowOpens = [];
  const requests = [];

  const ctx = {
    document, console, URL,
    setTimeout, clearTimeout,
    // The page installs a 10s auto-refresh; a test run must neither keep the
    // event loop alive nor re-fetch in the middle of an assertion.
    setInterval: () => 0,
    clearInterval: () => {},
    fetch: (url, init) => {
      const u = String(url);
      requests.push({url: u, init});
      for (const [test, make] of routes) if (test(u)) return Promise.resolve(make(u));
      return Promise.reject(new Error(`no fixture route for ${u}`));
    },
    window: {open: (u, target) => windowOpens.push({url: u, target})},
  };
  vm.createContext(ctx);

  const env = {
    ctx, document, windowOpens, requests,
    /** Put a route at the front, so a case can override a default. */
    route(test, make) { routes.unshift([test, make]); },
    el(id) {
      const node = document.getElementById(id);
      if (!node) throw new Error(`#${id} is not in the page markup`);
      return node;
    },
    set(id, value) { env.el(id).value = value; },
    text(id) { return env.el(id).textContent; },
    /** Fire a real event and wait for whatever it started. */
    async fire(id, type, opts) { await env.fireNode(env.el(id), type, opts); },
    async fireNode(node, type, opts) {
      await Promise.all(dispatch(node, type, opts));
      await drain();
    },
    /**
     * Fire an event whose handler is EXPECTED to stay in flight (its fetch
     * fixture has not been resolved yet). The handler's promise is kept and
     * given a catch, so a later rejection is still attributed rather than
     * orphaned — but it is not awaited here, because awaiting a handler that
     * cannot finish would hang the whole run.
     */
    async fireInFlight(id, type, opts) {
      const pending = Promise.all(dispatch(env.el(id), type, opts));
      pending.catch(err => {
        caseFailures++;
        console.log(`  in-flight handler rejected: ${(err && err.stack) || err}`);
      });
      await drain();
      // a thunk, not the promise: returning the promise would make the
      // caller's own `await` wait for the very thing that has not happened yet
      return () => pending;
    },
    read(expression) { return vm.runInContext(expression, ctx); },
    /** The links the page actually rendered, href and text. */
    links(id) {
      return env.el(id).querySelectorAll('a')
        .map(a => `${a.getAttribute('href')} → ${a.textContent}`).join(' | ');
    },
    urls(prefix) { return requests.map(r => r.url).filter(u => u.startsWith(prefix)); },
  };
  return env;
}

/** Load the page exactly as a browser would, and let its first refresh land. */
async function boot(project) {
  const env = makeEnv(project);
  vm.runInContext(PAGE_SCRIPT, env.ctx);
  await drain();
  return env;
}

async function selectProject(env) {
  const card = env.document.querySelector('#projects .card[data-name]');
  if (!card) throw new Error('refresh() rendered no selectable project card');
  // Click the heading INSIDE the card, so the container's delegated handler
  // has to resolve the card itself via closest() — as it does for a person.
  await env.fireNode(card.querySelector('h2') || card, 'click');
}

async function openPanel(env) {
  await selectProject(env);
  await env.fire('ta-open-panel', 'click');
}

const WIDGET = {name: 'widget', path: '/repos/widget'};
// The display name the collector reports ("Maestro") is deliberately NOT the
// on-disk clone directory ("maestro"); that divergence is the entire reason
// the data-dir rule exists.
const MAESTRO = {name: 'Maestro', path: '/home/dev/repos/maestro'};

/** Drive the real slug field through a lookup with the given wire answer. */
async function lookup(env, slug, status, body) {
  env.route(u => u.startsWith('/api/issue-lookup'), () => resp(status, body));
  env.set('ta-slug', slug);
  await env.fire('ta-slug', 'change');
}

/** Drive the real Create button with the given wire answer. */
async function create(env, status, body, {force = false} = {}) {
  env.route(u => u.startsWith('/api/actions/request-task'),
    () => resp(status, body));
  env.set('ta-title', 'Add feature X');
  env.set('ta-prose', 'because Y, done when Z');
  await env.fire('ta-create', 'click', {force});
}

/** The only path to an enabled Create button: a slug the lookup says is free. */
async function freeSlugThen(env, fn) {
  await lookup(env, 'add-x', 200, {ok: true, matches: [], malformed: []});
  if (env.el('ta-create').disabled) {
    throw new Error('precondition failed: a free slug did not enable Create');
  }
  return fn(env);
}

// ---- cases -----------------------------------------------------------------

const CASES = [
  {
    name: '1. a free slug is the only lookup answer that enables Create',
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200, {ok: true, matches: [], malformed: []});
      return {
        'slug state': env.text('ta-slug-state'),
        'Create disabled': env.el('ta-create').disabled,
        'existing hidden': env.el('ta-existing').hidden,
        'Open-existing hidden': env.el('ta-open').hidden,
      };
    },
    expect: {
      'slug state': 'free',
      'Create disabled': false,
      'existing hidden': true,
      'Open-existing hidden': true,
    },
  },
  {
    name: '2. a taken slug renders the issue, disables Create, and '
      + '"Open existing" navigates to the real https URL',
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200,
        {ok: true, matches: [GOOD_REF], malformed: []});
      await env.fire('ta-open', 'click');
      return {
        'slug state': env.text('ta-slug-state'),
        'Create disabled': env.el('ta-create').disabled,
        'existing hidden': env.el('ta-existing').hidden,
        'rendered link': env.links('ta-existing'),
        'window.open': JSON.stringify(env.windowOpens),
      };
    },
    expect: {
      'slug state': 'already requested',
      'Create disabled': true,
      'existing hidden': false,
      'rendered link': 'https://github.com/acme/widget/issues/12 → '
        + 'https://github.com/acme/widget/issues/12',
      'window.open':
        '[{"url":"https://github.com/acme/widget/issues/12","target":"_blank"}]',
    },
  },
  {
    name: '3. [URL] a single match with a javascript: URL is unreadable, '
      + 'never a clickable link',
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200, {
        ok: true, malformed: [],
        matches: [{...GOOD_REF, url: 'javascript:alert(1)'}],
      });
      return {
        'slug state': env.text('ta-slug-state'),
        'Open-existing hidden': env.el('ta-open').hidden,
        'Create disabled': env.el('ta-create').disabled,
        'rendered links': env.links('ta-existing'),
      };
    },
    expect: {
      'slug state': 'cannot read: url missing or wrong type',
      'Open-existing hidden': true,
      'Create disabled': true,
      'rendered links': '',
    },
  },
  {
    name: '4. several valid matches render a conflict list and keep Create off',
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200,
        {ok: true, matches: [GOOD_REF, SECOND_REF], malformed: []});
      return {
        'slug state': env.text('ta-slug-state'),
        'Create disabled': env.el('ta-create').disabled,
        'rendered links': env.links('ta-existing'),
      };
    },
    expect: {
      'slug state': '2 issues claim this slug — conflict',
      'Create disabled': true,
      'rendered links': 'https://github.com/acme/widget/issues/12 → #12 | '
        + 'https://github.com/acme/widget/issues/13 → #13',
    },
  },
  ...[
    ['5. matches: null — the inbox was not read exhaustively',
      {ok: true, matches: null, malformed: []}],
    ['6. matches absent from the envelope entirely',
      {ok: true, malformed: []}],
    ['7. matches: {} — an object where an array belongs',
      {ok: true, matches: {}, malformed: []}],
    ['8. matches: "ab" — a length-having non-array must not reach .some()',
      {ok: true, matches: 'ab', malformed: []}],
    ['9. malformed: null with matches: [] is still not "free"',
      {ok: true, matches: [], malformed: null}],
  ].map(([name, body]) => ({
    name: `${name} → "cannot check", Create stays disabled`,
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200, body);
      return {
        'slug state': env.text('ta-slug-state'),
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {
      'slug state': 'cannot check: inbox could not be read exhaustively',
      'Create disabled': true,
    },
  })),
  {
    name: '10. one valid + one invalid match fails the WHOLE list closed',
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200, {
        ok: true, malformed: [],
        matches: [GOOD_REF, {...SECOND_REF, url: undefined}],
      });
      return {
        'slug state': env.text('ta-slug-state'),
        'Create disabled': env.el('ta-create').disabled,
        'existing hidden': env.el('ta-existing').hidden,
        'rendered links (none, not partial)': env.links('ta-existing'),
      };
    },
    expect: {
      'slug state': 'cannot read the result',
      'Create disabled': true,
      'existing hidden': true,
      'rendered links (none, not partial)': '',
    },
  },
  {
    name: '11. [URL] a javascript: URL among several matches renders nothing',
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200, {
        ok: true, malformed: [],
        matches: [GOOD_REF, {...SECOND_REF, url: 'javascript:alert(1)'}],
      });
      return {
        'slug state': env.text('ta-slug-state'),
        'rendered links': env.links('ta-existing'),
        'raw innerHTML holds the scheme':
          env.el('ta-existing').innerHTML.includes('javascript:'),
      };
    },
    expect: {
      'slug state': 'cannot read the result',
      'rendered links': '',
      'raw innerHTML holds the scheme': false,
    },
  },
  {
    name: '12. created: null → state unknown, Re-check only, Create stays off',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e =>
        create(e, 200, {ok: true, created: null, error: 'no answer'}));
      return {
        'result': env.text('ta-result'),
        'Re-check hidden': env.el('ta-recheck').hidden,
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {
      'result': 'issue may have been created; state unknown: no answer',
      'Re-check hidden': false,
      'Create disabled': true,
    },
  },
  {
    name: '13. created: true with issue: null → "reading it back failed"',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e =>
        create(e, 200, {ok: true, created: true, issue: null}));
      return {
        'result': env.text('ta-result'),
        'Re-check hidden': env.el('ta-recheck').hidden,
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {
      'result': 'issue created, but reading it back failed',
      'Re-check hidden': false,
      'Create disabled': true,
    },
  },
  {
    name: '14. a malformed issue in the create response is never rendered',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e => create(e, 200, {
        ok: true, created: true,
        issue: {number: '12', title: 't', state: 'open', author: 'o',
          url: 'https://github.com/acme/widget/issues/12'},
      }));
      return {
        'result': env.text('ta-result'),
        'rendered links': env.links('ta-result'),
      };
    },
    expect: {
      'result': 'created, but cannot read the result: number missing or wrong '
        + 'type; labels missing or wrong type',
      'rendered links': '',
    },
  },
  {
    name: '15. [URL] a javascript: URL in the create response is not rendered',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e => create(e, 200, {
        ok: true, created: true, issue: {...GOOD_REF, url: 'javascript:alert(1)'},
      }));
      return {
        'result': env.text('ta-result'),
        'rendered links': env.links('ta-result'),
        'raw innerHTML holds the scheme':
          env.el('ta-result').innerHTML.includes('javascript:'),
      };
    },
    expect: {
      'result': 'created, but cannot read the result: url missing or wrong type',
      'rendered links': '',
      'raw innerHTML holds the scheme': false,
    },
  },
  {
    name: '16. a good create response renders the real issue link',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e =>
        create(e, 200, {ok: true, created: true, issue: GOOD_REF}));
      return {
        'result': env.text('ta-result'),
        'rendered links': env.links('ta-result'),
      };
    },
    expect: {
      'result': 'created · #12',
      'rendered links': 'https://github.com/acme/widget/issues/12 → #12',
    },
  },
  {
    name: '17. created: false whose re-check FINDS the issue → "already '
      + 'exists", Create stays disabled',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e => {
        e.route(u => u.startsWith('/api/issue-lookup'),
          () => resp(200, {ok: true, matches: [GOOD_REF], malformed: []}));
        return create(e, 200, {ok: true, created: false});
      });
      return {
        'result': env.text('ta-result'),
        'slug state after the automatic re-check': env.text('ta-slug-state'),
        'Create disabled': env.el('ta-create').disabled,
        'lookups issued': env.urls('/api/issue-lookup').length,
      };
    },
    expect: {
      'result': 'a request for this slug already exists',
      'slug state after the automatic re-check': 'already requested',
      'Create disabled': true,
      'lookups issued': 2,
    },
  },
  {
    name: '17b. created: false whose re-check CANNOT find it → the screen '
      + 'says so and keeps Create off, rather than offering it under an '
      + '"already exists" line',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e => create(e, 200, {ok: true, created: false}));
      return {
        'result': env.text('ta-result'),
        'slug state after the automatic re-check': env.text('ta-slug-state'),
        'Create disabled': env.el('ta-create').disabled,
        'Re-check offered': env.el('ta-recheck').hidden,
      };
    },
    expect: {
      'result': 'a request for this slug already exists, but the re-check '
        + 'could not find it — look before filing again',
      'slug state after the automatic re-check': 'free',
      'Create disabled': true,
      'Re-check offered': false,
    },
  },
  {
    name: '18. [entry point] taRepo is the card\'s data-dir basename, never '
      + 'the display name',
    project: MAESTRO,
    async run(env) {
      await openPanel(env);
      const card = env.document.querySelector('#projects .card[data-name]');
      return {
        'card data-name (display)': card.dataset.name,
        'card data-dir (on-disk clone dir)': card.dataset.dir,
        'detail heading (display name)': env.text('detail-name'),
        'ta-repo label': env.text('ta-repo'),
        'taRepo': env.read('taRepo'),
        'panel hidden': env.el('task-authoring').hidden,
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {
      'card data-name (display)': 'Maestro',
      'card data-dir (on-disk clone dir)': 'maestro',
      'detail heading (display name)': '— Maestro',
      'ta-repo label': '— maestro',
      'taRepo': 'maestro',
      'panel hidden': false,
      'Create disabled': true,
    },
  },
  {
    name: '19. [entry point] the lookup query carries the data-dir, not the '
      + 'display name',
    project: MAESTRO,
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200, {ok: true, matches: [], malformed: []});
      return {'issue-lookup URL': env.urls('/api/issue-lookup')[0]};
    },
    expect: {'issue-lookup URL': '/api/issue-lookup?dir=maestro&slug=add-x'},
  },
  {
    name: '20. the create POST carries the data-dir AND the operator\'s own '
      + 'slug, title and prose — the slug it filed under must be the slug it '
      + 'checked',
    project: MAESTRO,
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e =>
        create(e, 200, {ok: true, created: true, issue: GOOD_REF}));
      const post = env.requests.find(
        r => r.url.startsWith('/api/actions/request-task'));
      const body = JSON.parse(post.init.body);
      return {
        'POST body dir': body.dir,
        'POST body slug': body.slug,
        'POST body slug === the slug looked up':
          env.urls('/api/issue-lookup')[0].endsWith(`&slug=${body.slug}`),
        'POST body title': body.title,
        'POST body prose': body.prose,
        'POST body keys': Object.keys(body).sort().join(','),
      };
    },
    expect: {
      'POST body dir': 'maestro',
      'POST body slug': 'add-x',
      'POST body slug === the slug looked up': true,
      'POST body title': 'Add feature X',
      'POST body prose': 'because Y, done when Z',
      'POST body keys': 'dir,prose,slug,title',
    },
  },
  {
    name: '20b. slug and title are trimmed on the way out; prose is sent '
      + 'verbatim, newlines and all',
    async run(env) {
      await openPanel(env);
      await lookup(env, '  spaced-slug  ', 200,
        {ok: true, matches: [], malformed: []});
      env.route(u => u.startsWith('/api/actions/request-task'),
        () => resp(200, {ok: true, created: true, issue: GOOD_REF}));
      env.set('ta-title', '  Padded title  ');
      env.set('ta-prose', 'line one\n\n  line two, indented  \n');
      await env.fire('ta-create', 'click');
      const body = JSON.parse(env.requests.find(
        r => r.url.startsWith('/api/actions/request-task')).init.body);
      return {
        'slug trimmed': body.slug,
        'title trimmed': body.title,
        'prose verbatim': JSON.stringify(body.prose),
      };
    },
    expect: {
      'slug trimmed': 'spaced-slug',
      'title trimmed': 'Padded title',
      'prose verbatim': '"line one\\n\\n  line two, indented  \\n"',
    },
  },
  {
    name: '21. a 5xx create → state unknown; Create must NOT be re-enabled',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e => create(e, 500, {detail: 'internal error'}));
      return {
        'result': env.text('ta-result'),
        'Re-check hidden': env.el('ta-recheck').hidden,
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {
      'result': 'issue may have been created; state unknown: internal error',
      'Re-check hidden': false,
      'Create disabled': true,
    },
  },
  {
    name: '22. a 4xx create is a refusal → Create IS re-enabled, no Re-check',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e => create(e, 422, {detail: 'bad dir'}));
      return {
        'result': env.text('ta-result'),
        'Re-check hidden': env.el('ta-recheck').hidden,
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {
      'result': 'request rejected: bad dir',
      'Re-check hidden': true,
      'Create disabled': false,
    },
  },
  {
    name: '23. a 409 create → "another action is in flight", retry allowed',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e => create(e, 409, {detail: 'locked'}));
      return {
        'result': env.text('ta-result'),
        'Create disabled': env.el('ta-create').disabled,
        'Re-check hidden': env.el('ta-recheck').hidden,
      };
    },
    expect: {
      'result': 'another action is in flight',
      'Create disabled': false,
      'Re-check hidden': true,
    },
  },
  {
    name: '24. a 200 with a `null` body must not strand the screen on "filing…"',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e => create(e, 200, null));
      return {
        'result': env.text('ta-result'),
        'Re-check hidden': env.el('ta-recheck').hidden,
      };
    },
    expect: {
      'result': 'issue may have been created; state unknown: unreadable response',
      'Re-check hidden': false,
    },
  },
  {
    name: '25. a transport failure on create → state unknown, Re-check offered',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e => {
        e.route(u => u.startsWith('/api/actions/request-task'),
          () => { throw new Error('network down'); });
        e.set('ta-title', 'Add feature X');
        e.set('ta-prose', 'because Y, done when Z');
        return e.fire('ta-create', 'click');
      });
      return {
        'result': env.text('ta-result'),
        'Re-check hidden': env.el('ta-recheck').hidden,
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {
      // the slug is named: a transport failure after the POST went out is
      // exactly when the operator needs to know WHAT may have been filed
      'result':
        'issue may have been created for "add-x"; state unknown (Error: network down)',
      'Re-check hidden': false,
      'Create disabled': true,
    },
  },
  {
    name: '26. a lookup with no project selected refuses instead of asking '
      + 'the server about a repo called "null"',
    async run(env) {
      // deliberately no openPanel(): nothing has set taRepo yet
      env.set('ta-slug', 'add-x');
      await env.fire('ta-slug', 'change');
      return {
        'slug state': env.text('ta-slug-state'),
        'lookups issued': env.urls('/api/issue-lookup').length,
        'taRepo': env.read('taRepo'),
      };
    },
    expect: {
      'slug state': 'no repo selected',
      'lookups issued': 0,
      'taRepo': null,
    },
  },
  {
    name: '27. [defence in depth] Create with no repo refuses without POSTing '
      + '— the button is disabled in the UI; this drives the guard behind it',
    async run(env) {
      await create(env, 200, {ok: true, created: true, issue: GOOD_REF},
        {force: true});
      return {
        'result': env.text('ta-result'),
        'POSTs issued': env.urls('/api/actions/request-task').length,
      };
    },
    expect: {'result': 'no repo selected', 'POSTs issued': 0},
  },
  {
    name: '28. the entry-point button explains itself when no card is selected',
    async run(env) {
      await env.fire('ta-open-panel', 'click');
      return {
        'error span': env.text('ta-open-panel-error'),
        'panel hidden': env.el('task-authoring').hidden,
      };
    },
    expect: {
      'error span':
        'no repo directory known — select a detected project card first',
      'panel hidden': true,
    },
  },
  {
    name: '29. selecting a project again clears the panel and drops taRepo',
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200,
        {ok: true, matches: [GOOD_REF], malformed: []});
      await selectProject(env);
      return {
        'panel hidden': env.el('task-authoring').hidden,
        'taRepo': env.read('taRepo'),
      };
    },
    expect: {'panel hidden': true, 'taRepo': null},
  },
  {
    name: '30. a non-ok lookup response reports the status, never "free"',
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 503, {ok: false, error: 'upstream'});
      return {
        'slug state': env.text('ta-slug-state'),
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {'slug state': 'cannot check: 503', 'Create disabled': true},
  },
  {
    name: '31. ok:false (unreadable inbox) reports the reason, never "free"',
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200,
        {ok: false, error: 'inbox unreadable', matches: null, malformed: null});
      return {
        'slug state': env.text('ta-slug-state'),
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {
      'slug state': 'cannot check: inbox unreadable',
      'Create disabled': true,
    },
  },
  {
    name: '32. [Re-check transition 1/3] unknown → Re-check → zero matches: '
      + 'the stale warning is gone and Create is enabled',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e =>
        create(e, 200, {ok: true, created: null, error: 'no answer'}));
      const warned = env.text('ta-result');
      const offered = !env.el('ta-recheck').hidden;
      env.route(u => u.startsWith('/api/issue-lookup'),
        () => resp(200, {ok: true, matches: [], malformed: []}));
      await env.fire('ta-recheck', 'click');
      return {
        'warning shown before Re-check': warned,
        'Re-check offered before': offered,
        'result cleared': env.text('ta-result'),
        'Re-check hidden after': env.el('ta-recheck').hidden,
        'existing cleared': env.el('ta-existing').innerHTML,
        'existing hidden': env.el('ta-existing').hidden,
        'Open-existing hidden': env.el('ta-open').hidden,
        'slug state': env.text('ta-slug-state'),
        // the ruling: an explicit [] from `gh issue list` is the best
        // evidence obtainable, so Create MAY come back — the contradiction
        // above is what must not
        'Create disabled': env.el('ta-create').disabled,
        'lookups issued (the click really re-queried)':
          env.urls('/api/issue-lookup').length,
      };
    },
    expect: {
      'warning shown before Re-check':
        'issue may have been created; state unknown: no answer',
      'Re-check offered before': true,
      'result cleared': '',
      'Re-check hidden after': true,
      'existing cleared': '',
      'existing hidden': true,
      'Open-existing hidden': true,
      'slug state': 'free',
      'Create disabled': false,
      'lookups issued (the click really re-queried)': 2,
    },
  },
  {
    name: '33. [Re-check transition 2/3] unknown → Re-check → one match: the '
      + 'existing issue is shown and Create stays disabled',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e =>
        create(e, 200, {ok: true, created: null, error: 'no answer'}));
      env.route(u => u.startsWith('/api/issue-lookup'),
        () => resp(200, {ok: true, matches: [GOOD_REF], malformed: []}));
      await env.fire('ta-recheck', 'click');
      return {
        'result cleared': env.text('ta-result'),
        'Re-check hidden after': env.el('ta-recheck').hidden,
        'slug state': env.text('ta-slug-state'),
        'Create disabled': env.el('ta-create').disabled,
        'Open-existing hidden': env.el('ta-open').hidden,
        'rendered link': env.links('ta-existing'),
      };
    },
    expect: {
      'result cleared': '',
      'Re-check hidden after': true,
      'slug state': 'already requested',
      'Create disabled': true,
      'Open-existing hidden': false,
      'rendered link': 'https://github.com/acme/widget/issues/12 → '
        + 'https://github.com/acme/widget/issues/12',
    },
  },
  ...[
    ['34. [Re-check transition 3/3, matches: null]', 200,
      {ok: true, matches: null, malformed: []},
      'cannot check: inbox could not be read exhaustively'],
    ['35. [Re-check transition 3/3, HTTP failure]', 503,
      {ok: false, error: 'upstream'}, 'cannot check: 503'],
  ].map(([name, status, body, slugState]) => ({
    name: `${name} unknown → Re-check → no answer: the unknown state SURVIVES `
      + '— the warning stays, Re-check stays offered, Create stays disabled',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e =>
        create(e, 200, {ok: true, created: null, error: 'no answer'}));
      env.route(u => u.startsWith('/api/issue-lookup'), () => resp(status, body));
      await env.fire('ta-recheck', 'click');
      return {
        'warning preserved': env.text('ta-result'),
        'Re-check still offered': env.el('ta-recheck').hidden,
        'slug state': env.text('ta-slug-state'),
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {
      // clearing this would DELETE the fact the operator clicked Re-check to
      // resolve, replacing "an issue may exist" with a bare "cannot check"
      'warning preserved': 'issue may have been created; state unknown: no answer',
      'Re-check still offered': false,
      'slug state': slugState,
      'Create disabled': true,
    },
  })),
  {
    name: '36. a fresh lookup clears the PREVIOUS slug\'s issue, not just '
      + 'hides it — and drops the stale "Open existing" target with it',
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200,
        {ok: true, matches: [GOOD_REF], malformed: []});
      const shown = env.links('ta-existing');
      env.route(u => u.startsWith('/api/issue-lookup'),
        () => resp(200, {ok: true, matches: [], malformed: []}));
      env.set('ta-slug', 'add-y');
      await env.fire('ta-slug', 'change');
      await env.fire('ta-open', 'click', {force: true});
      return {
        'link shown for the first slug': shown,
        'existing innerHTML after': env.el('ta-existing').innerHTML,
        'existing hidden after': env.el('ta-existing').hidden,
        'Open-existing hidden after': env.el('ta-open').hidden,
        'stale window.open fired': JSON.stringify(env.windowOpens),
      };
    },
    expect: {
      'link shown for the first slug':
        'https://github.com/acme/widget/issues/12 → '
        + 'https://github.com/acme/widget/issues/12',
      'existing innerHTML after': '',
      'existing hidden after': true,
      'Open-existing hidden after': true,
      'stale window.open fired': '[]',
    },
  },
  ...[
    ['prose exceeds the 60000-byte bound', 'prose exceeds the 60000-byte bound'],
    ['slug is not a valid slug', 'slug is not a valid slug'],
    ['ambiguous — not creating', 'ambiguous — not creating'],
    ['refused before launch: embedded null byte',
      'refused before launch: embedded null byte'],
  ].map(([error, shown], i) => ({
    name: `38.${i + 1} [N-1] a 200 whose envelope says ok:false is a REFUSAL `
      + `("${error}") — never "a request for this slug already exists"`,
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e =>
        create(e, 200, {ok: false, created: false, error}));
      return {
        'result': env.text('ta-result'),
        // nothing was created, so fixing the input and retrying is safe
        'Create disabled': env.el('ta-create').disabled,
        'Re-check hidden': env.el('ta-recheck').hidden,
        // the refusal must not have triggered the created:false re-check
        'lookups issued': env.urls('/api/issue-lookup').length,
      };
    },
    expect: {
      'result': `request rejected: ${shown}`,
      'Create disabled': false,
      'Re-check hidden': true,
      'lookups issued': 1,
    },
  })),
  {
    name: '39. [N-1] ok:false with created:null is a refusal we are unsure '
      + 'about — unknown wins, Create stays off',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e => create(e, 200,
        {ok: false, created: null, error: 'github-checker returned no JSON'}));
      return {
        'result': env.text('ta-result'),
        'Create disabled': env.el('ta-create').disabled,
        'Re-check hidden': env.el('ta-recheck').hidden,
      };
    },
    expect: {
      'result': 'issue may have been created; state unknown: '
        + 'github-checker returned no JSON',
      'Create disabled': true,
      'Re-check hidden': false,
    },
  },
  {
    name: '40. [N-1] ok:false with created:true is a contradictory envelope — '
      + 'not rendered as a success',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e => create(e, 200,
        {ok: false, created: true, issue: GOOD_REF, error: 'partial failure'}));
      return {
        'result': env.text('ta-result'),
        'rendered links': env.links('ta-result'),
        'Create disabled': env.el('ta-create').disabled,
        'Re-check hidden': env.el('ta-recheck').hidden,
      };
    },
    expect: {
      'result': 'cannot read the result: reported both a failure and a '
        + 'created issue: partial failure',
      'rendered links': '',
      'Create disabled': true,
      'Re-check hidden': false,
    },
  },
  {
    name: '41. [N-1] an ok:false refusal with no `error` still says it was '
      + 'refused, rather than inventing an outcome',
    async run(env) {
      await openPanel(env);
      await freeSlugThen(env, e => create(e, 200, {ok: false, created: false}));
      return {
        'result': env.text('ta-result'),
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {
      'result': 'request rejected: refused, no reason given',
      'Create disabled': false,
    },
  },
  // --- hole #4: the URL sinks build DOM, they do not print markup -------
  //
  // isHttpsUrl only PARSES for a scheme; the old sinks then interpolated the
  // ORIGINAL string into an href through innerHTML, so esc() was the only
  // thing between a validated-but-hostile URL and attribute breakout — and
  // nothing pinned esc(). `new URL()` reports 'https:' for a URL whose path
  // carries a double quote (asserted below, not assumed), so the payload
  // passes validation with the quote intact. Links are now built with
  // createElement, which has no markup for a payload to escape from.
  ...[
    ['42. conflict list', 2, XSS_HREF, async env => {
      const evil = {...SECOND_REF, url: XSS_URL};
      await lookup(env, 'add-x', 200,
        {ok: true, matches: [GOOD_REF, evil], malformed: []});
      return 'ta-existing';
    }],
    ['43. single-match render', 1, XSS_HREF, async env => {
      await lookup(env, 'add-x', 200,
        {ok: true, matches: [{...GOOD_REF, url: XSS_URL}], malformed: []});
      return 'ta-existing';
    }],
    ['44. create-result link', 1, XSS_HREF, async env => {
      await freeSlugThen(env, e => create(e, 200,
        {ok: true, created: true, issue: {...GOOD_REF, url: XSS_URL}}));
      return 'ta-result';
    }],
  ].map(([name, anchors, href, drive]) => ({
    name: `${name}: a quote-bearing https: URL creates no element, no inline `
      + 'handler, a canonical href, and text that is only text',
    async run(env) {
      await openPanel(env);
      const sink = await drive(env);
      const el = env.el(sink);
      const all = el.querySelectorAll('*');
      return {
        'the payload really does parse as https:':
          new URL(XSS_URL).protocol,
        'anchors built': all.filter(n => n.tagName === 'A').length,
        'img elements created': el.querySelectorAll('img').length,
        'elements carrying an inline handler': all.filter(
          n => Object.keys(n.attributes).some(a => a.startsWith('on'))).length,
        'href is the canonical parsed form':
          all.filter(n => n.tagName === 'A').pop().getAttribute('href'),
        'no attribute anywhere holds raw markup': all.every(
          n => Object.values(n.attributes).every(v => !v.includes('<'))),
        'the raw payload survives only inside text':
          el.textContent.includes('<img') === el.textContent.includes(XSS_URL),
      };
    },
    expect: {
      'the payload really does parse as https:': 'https:',
      'anchors built': anchors,
      'img elements created': 0,
      'elements carrying an inline handler': 0,
      'href is the canonical parsed form': href,
      'no attribute anywhere holds raw markup': true,
      'the raw payload survives only inside text': true,
    },
  })),
  {
    name: '44b. [merge gate] the PR link is built too — the same payload that '
      + 'passes MG_REQUIRED must not reach an href as a string',
    async run(env) {
      await selectProject(env);
      env.route(u => u.startsWith('/api/pr-detail'), () => resp(200, {
        action: 'pr-detail', dir: 'widget', ok: true,
        pr_detail: {
          number: 7, title: 'a pr', url: XSS_URL, state: 'open',
          is_draft: false, mergeable: 'MERGEABLE', head_branch: 'feat',
          head_sha: 'abcdef1234', base_branch: 'master', checks: [],
          files: [], review_threads: [], review_decision: 'APPROVED',
          allows_squash: true,
        },
      }));
      env.set('mg-pr-input', '7');
      await env.fire('mg-open-gate', 'click');
      const head = env.el('mg-head');
      const all = head.querySelectorAll('*');
      return {
        'anchors built': all.filter(n => n.tagName === 'A').length,
        'img elements created': head.querySelectorAll('img').length,
        'elements carrying an inline handler': all.filter(
          n => Object.keys(n.attributes).some(a => a.startsWith('on'))).length,
        'href is the canonical parsed form':
          all.filter(n => n.tagName === 'A').map(a => a.getAttribute('href'))[0],
      };
    },
    expect: {
      'anchors built': 1,
      'img elements created': 0,
      'elements carrying an inline handler': 0,
      'href is the canonical parsed form': XSS_HREF,
    },
  },
  {
    name: '45. [URL] `https://[oops` has the right prefix but does not parse '
      + '— a prefix check would let it through, the parser must not',
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200,
        {ok: true, matches: [{...GOOD_REF, url: 'https://[oops'}], malformed: []});
      return {
        'slug state': env.text('ta-slug-state'),
        'anchors rendered': env.el('ta-existing').querySelectorAll('a').length,
        'Open-existing hidden': env.el('ta-open').hidden,
        // the premise, asserted rather than assumed
        'and it DOES start with https://':
          'https://[oops'.startsWith('https://'),
      };
    },
    expect: {
      'slug state': 'cannot read: url missing or wrong type',
      'anchors rendered': 0,
      'Open-existing hidden': true,
      'and it DOES start with https://': true,
    },
  },
  {
    name: '46. [minor] a non-empty `malformed` means the inbox was NOT read '
      + 'exhaustively — `matches: []` beside it is not "free"',
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200, {
        ok: true, matches: [],
        malformed: [{number: 9, why: 'no slug line'}],
      });
      return {
        'slug state': env.text('ta-slug-state'),
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {
      'slug state': 'cannot check: 1 unreadable candidate(s) in the inbox',
      'Create disabled': true,
    },
  },
  {
    name: '47. [NEW-2] two lookups racing: a stale answer must not overwrite '
      + 'the newer one — a taken slug must never end up reading "free"',
    async run(env) {
      await openPanel(env);
      // the FIRST lookup is slow and would answer "free"
      let releaseStale;
      const stale = new Promise(r => { releaseStale = r; });
      env.route(u => u.includes('slug=first'), () => stale);
      env.route(u => u.includes('slug=second'),
        () => resp(200, {ok: true, matches: [GOOD_REF], malformed: []}));

      env.set('ta-slug', 'first');
      await env.fire('ta-slug', 'change');       // in flight, unanswered
      const midFlight = env.text('ta-slug-state');
      env.set('ta-slug', 'second');
      await env.fire('ta-slug', 'change');       // answers immediately: taken
      const afterSecond = env.text('ta-slug-state');
      releaseStale(resp(200, {ok: true, matches: [], malformed: []}));
      await new Promise(r => setTimeout(r, 5));
      return {
        'state while the first was in flight': midFlight,
        'state after the second answered': afterSecond,
        'state after the stale answer landed': env.text('ta-slug-state'),
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {
      'state while the first was in flight': '',
      'state after the second answered': 'already requested',
      'state after the stale answer landed': 'already requested',
      'Create disabled': true,
    },
  },
  {
    name: '48. [NEW-2] while the create POST is in flight the whole form is '
      + 'locked — and a change forced through anyway is refused, not raced',
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200, {ok: true, matches: [], malformed: []});
      let releasePost;
      const pending = new Promise(r => { releasePost = r; });
      env.route(u => u.startsWith('/api/actions/request-task'), () => pending);
      env.set('ta-title', 'Add feature X');
      env.set('ta-prose', 'because Y, done when Z');
      const settle = await env.fireInFlight('ta-create', 'click');

      const locked = ['ta-slug', 'ta-title', 'ta-prose']
        .map(id => env.el(id).disabled).join(',');
      // force the event a locked field could not produce, to prove the
      // handler refuses on its own rather than relying on the lock
      env.route(u => u.startsWith('/api/issue-lookup'),
        () => resp(200, {ok: true, matches: [], malformed: []}));
      env.set('ta-slug', 'add-y');
      await env.fire('ta-slug', 'change', {force: true});
      const during = {
        result: env.text('ta-result'),
        state: env.text('ta-slug-state'),
        disabled: env.el('ta-create').disabled,
        lookups: env.urls('/api/issue-lookup').length,
      };

      releasePost(resp(200, {ok: true, created: null, error: 'no answer'}));
      await settle();
      await drain();
      return {
        'fields locked during the POST (slug,title,prose)': locked,
        'result survived the forced change': during.result,
        'the forced lookup was refused': during.state,
        'Create stayed disabled': during.disabled,
        'no second lookup went out': during.lookups,
        'fields unlocked afterwards': ['ta-slug', 'ta-title', 'ta-prose']
          .map(id => env.el(id).disabled).join(','),
        'result after the POST answered': env.text('ta-result'),
        'Create disabled after the POST answered': env.el('ta-create').disabled,
      };
    },
    expect: {
      'fields locked during the POST (slug,title,prose)': 'true,true,true',
      'result survived the forced change': 'filing…',
      'the forced lookup was refused': 'filing in progress — wait for the result',
      'Create stayed disabled': true,
      'no second lookup went out': 1,
      'fields unlocked afterwards': 'false,false,false',
      'result after the POST answered':
        'issue may have been created; state unknown: no answer',
      'Create disabled after the POST answered': true,
    },
  },
  {
    name: '49. [NEW-2] a POST answer that lands after the screen moved on is '
      + 'NEVER discarded — it names the submitted slug and keeps Create off',
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200, {ok: true, matches: [], malformed: []});
      let releasePost;
      const pending = new Promise(r => { releasePost = r; });
      env.route(u => u.startsWith('/api/actions/request-task'), () => pending);
      env.set('ta-title', 'Add feature X');
      env.set('ta-prose', 'because Y, done when Z');
      const settle = await env.fireInFlight('ta-create', 'click');
      // the operator re-opens the panel mid-flight: a new generation, and the
      // POST for "add-x" is now about a screen state nobody is looking at
      await env.fire('ta-open-panel', 'click');
      releasePost(resp(200, {ok: true, created: true, issue: GOOD_REF}));
      await settle();
      await drain();
      return {
        'the outcome was not silently dropped': env.text('ta-result'),
        'Re-check offered': env.el('ta-recheck').hidden,
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {
      'the outcome was not silently dropped':
        'a request for "add-x" was sent and its outcome landed after the '
        + 'screen moved on — re-check that slug before filing it again',
      'Re-check offered': false,
      'Create disabled': true,
    },
  },
  {
    name: '50. [NEW-2] a lookup answer is applied only if the SLUG it was '
      + 'issued for is still on screen, not merely the generation',
    async run(env) {
      await openPanel(env);
      let release;
      const slow = new Promise(r => { release = r; });
      env.route(u => u.includes('slug=first'), () => slow);
      env.set('ta-slug', 'first');
      await env.fire('ta-slug', 'change');
      // the operator keeps typing: the field changes without a new `change`,
      // so the generation is UNCHANGED and only the slug snapshot can catch it
      env.set('ta-slug', 'first-and-more');
      release(resp(200, {ok: true, matches: [], malformed: []}));
      await drain();
      return {
        'slug state (the stale "free" must not be applied)':
          env.text('ta-slug-state'),
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {
      'slug state (the stale "free" must not be applied)': '',
      'Create disabled': true,
    },
  },
  {
    name: '37. a lookup answering 200 with a `null` body fails closed on '
      + 'purpose, not as a raw TypeError',
    async run(env) {
      await openPanel(env);
      await lookup(env, 'add-x', 200, null);
      return {
        'slug state': env.text('ta-slug-state'),
        'Create disabled': env.el('ta-create').disabled,
      };
    },
    expect: {
      'slug state': 'cannot check: unreadable response',
      'Create disabled': true,
    },
  },
];

// ---- deliberate failures, for testing the runner itself --------------------
//
// Each of these is a REAL case run through the REAL loop below; only the
// injected fault differs. tests/web/runner_selftest.js spawns one child
// process per scenario so these failures never pollute the real run.

const SELFTEST_CASES = {
  clean: null,
  reject: {
    name: 'selftest: a detached rejected promise must redden the run',
    async run(env) {
      await openPanel(env);
      Promise.reject(new Error('selftest: detached rejection'));
      await drain();
      return {'the case itself': 'passes'};
    },
    expect: {'the case itself': 'passes'},
  },
  throw: {
    name: 'selftest: a throwing timer callback must redden the run',
    async run(env) {
      await openPanel(env);
      setTimeout(() => { throw new Error('selftest: thrown callback'); }, 0);
      await drain();
      return {'the case itself': 'passes'};
    },
    expect: {'the case itself': 'passes'},
  },
  assert: {
    name: 'selftest: an ordinary assertion mismatch must redden the run',
    async run(env) {
      await openPanel(env);
      return {'ta-repo label': env.text('ta-repo')};
    },
    expect: {'ta-repo label': 'not what the page shows'},
  },
  casethrow: {
    name: 'selftest: a case that throws is a FAILED case, not an absent one',
    async run() { throw new Error('selftest: the case body threw'); },
    expect: {},
  },
  hang: {
    name: 'selftest: a case that awaits a promise which never settles must '
      + 'not let the process exit 0 mid-run',
    async run() {
      await new Promise(() => {});  // never settles
      return {};
    },
    expect: {},
  },
  crash: {
    name: 'selftest: a crash outside every case must redden the run',
    async run() { return {}; },
    expect: {},
  },
};

// ---- runner ----------------------------------------------------------------

async function runCase(c) {
  currentCase = c.name;
  let env;
  try {
    env = await boot(c.project || WIDGET);
  } catch (err) {
    caseFailures++;
    console.log(`\n[FAIL] ${c.name}`);
    console.log(`  the page would not load: ${(err && err.stack) || err}`);
    return;
  }
  let got;
  try {
    got = await c.run(env);
  } catch (err) {
    caseFailures++;
    console.log(`\n[FAIL] ${c.name}`);
    console.log(`  the case threw: ${(err && err.stack) || err}`);
    return;
  }
  const keys = Object.keys(c.expect);
  const ok = keys.every(k => got[k] === c.expect[k]);
  if (!ok) caseFailures++;
  console.log(`\n[${ok ? 'PASS' : 'FAIL'}] ${c.name}`);
  for (const k of keys) {
    const mark = got[k] === c.expect[k] ? '=' : '!=';
    console.log(
      `  ${k}: ${JSON.stringify(got[k])} ${mark} ${JSON.stringify(c.expect[k])}`);
  }
}

async function main() {
  if (SELFTEST !== null && !(SELFTEST in SELFTEST_CASES)) {
    throw new Error(`unknown --selftest scenario: ${SELFTEST}`);
  }
  const injected = SELFTEST ? SELFTEST_CASES[SELFTEST] : null;
  const cases = injected ? [...CASES, injected] : CASES;
  for (const c of cases) await runCase(c);
  if (SELFTEST === 'crash') {
    throw new Error('selftest: main() itself failed after the cases ran');
  }
  return cases.length;
}

main().then(async total => {
  currentCase = '(drain)';
  // Let anything still in flight surface BEFORE the summary, so the summary
  // cannot describe a run that has not finished failing yet.
  await drain(10);
  console.log(
    `\ncases: ${total} · failed cases: ${caseFailures} · async errors `
    + `(unhandled rejections / uncaught exceptions): ${asyncErrors}`);
  if (failed() === 0) {
    summaryClaimedClean = true;
    console.log(`\n${total}/${total} cases passed, no async errors`);
  } else {
    console.log(
      `\nFAILED: ${total - caseFailures}/${total} cases passed, ${asyncErrors} `
      + 'async error(s) — see the [FAIL] blocks and any '
      + 'UNHANDLED/UNCAUGHT reports above');
  }
  summaryPrinted = true;
  process.exitCode = failed() === 0 ? 0 : 1;
}).catch(err => {
  summaryPrinted = true;
  console.error('\nHARNESS CRASHED:', (err && err.stack) || err);
  process.exitCode = 1;
});
