// Runs the SHIPPED task-authoring JS -- sliced verbatim out of
// dispatcher/server/static/index.html -- under a stub DOM with a scripted
// fetch queue. There is no browser/JS test runner wired into this Python
// project's own test suite, so this drives the REAL client code (not a
// reimplementation of its logic) through vm.runInContext: line-slice the
// actual <script> content by locating stable markers, eval it, then invoke
// the real exported functions and the real (anonymous) click-handler
// closures, and read the real DOM-stub state back out.
//
// Invoked from tests/test_task_authoring_js.py via `node <this> <index.html>`
// so it runs inside the Python test suite everyone already runs, rather than
// requiring a second toolchain to be set up on its own. `node` itself is a
// hard prerequisite of that Python test -- its absence FAILS the suite, it
// does not skip it (a skip is how a suite goes green while covering
// nothing). Exit code 0 = all cases passed; non-zero = at least one case
// (or an unhandled rejection) failed, and the failure detail was already
// printed to stdout.
//
// Add a case here per rule the operator-console section relies on -- this
// list must not silently drift from dispatcher/server/static/index.html.
'use strict';
const fs = require('fs');
const vm = require('vm');

const HTML = process.argv[2];
if (!HTML) {
  console.error('usage: node task_authoring_harness.js <path-to-index.html>');
  process.exit(2);
}
const lines = fs.readFileSync(HTML, 'utf8').split('\n');
const slice = (a, b) => lines.slice(a - 1, b).join('\n'); // 1-based inclusive
const at = needle => lines.findIndex(l => l.includes(needle)) + 1; // 1-based

// esc / isHttpsUrl are shared top-of-file helpers this block depends on but
// does not itself define; sliced as one contiguous chunk from `const esc =`
// through the end of `isHttpsUrl`'s closing brace.
const escStart = at('const esc =');
const isHttpsUrlEnd = (() => {
  const start = at('function isHttpsUrl(v)');
  return start + lines.slice(start - 1).findIndex(l => l === '}');
})();
const taStart = at('let taRepo = null;');
const taEnd = at("getElementById('ta-open-panel').addEventListener")
  + lines.slice(at("getElementById('ta-open-panel').addEventListener") - 1)
      .findIndex(l => l === '});');

const helpers = slice(escStart, isHttpsUrlEnd);
const taBlock = slice(taStart, taEnd);

// sanity: the slices must be the real thing, not a drifted line range
for (const [name, text, needle] of [
  ['helpers esc', helpers, 'const esc ='],
  ['helpers isHttpsUrl', helpers, 'function isHttpsUrl(v)'],
  ['helpers scheme allowlist', helpers, "protocol === 'https:'"],
  ['taBlock', taBlock, 'async function taCheckSlug'],
  ['taBlock envelope/type guard', taBlock, '!Array.isArray(outcome.matches)'],
  ['taBlock multi-match item guard', taBlock, 'taRefProblems(m).length > 0'],
  ['taBlock 5xx guard', taBlock, 'res.status >= 500'],
  ['taBlock non-object body guard', taBlock, "typeof outcome !== 'object'"],
  ['taBlock taRepo guard', taBlock, 'if (!taRepo)'],
  ['taBlock end', taBlock, "getElementById('ta-open-panel')"],
]) {
  if (!text.includes(needle)) throw new Error(`slice ${name} missed ${needle}`);
}

function makeDom() {
  const els = {};
  const el = () => ({
    textContent: '', innerHTML: '', hidden: false, disabled: false, value: '',
    listeners: {},
    addEventListener(ev, fn) { this.listeners[ev] = fn; },
    scrollIntoView() {},
  });
  return {
    getElementById: id => (els[id] = els[id] || el()),
    _els: els,
  };
}

function respond(status, body) {
  return Promise.resolve({
    status, ok: status >= 200 && status < 300,
    json: () => Promise.resolve(body),
  });
}

// ---- fixtures -------------------------------------------------------------

const GOOD_REF = {
  number: 12, title: 'add feature X', state: 'open',
  url: 'https://github.com/acme/widget/issues/12',
  author: 'octocat', labels: ['inbox'],
};

function tokenAwareFetch(mainResponse) {
  return url => {
    if (String(url).startsWith('/api/actions/session')) {
      return respond(200, {token: 'test-token'});
    }
    return mainResponse(url);
  };
}

const CASES = [
  {
    name: '1. free slug enables Create (the ONLY combo that may)',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      ctx.fetch = () => respond(200, {ok: true, matches: [], malformed: []});
      await vm.runInContext('taCheckSlug()', ctx);
    },
    check: document => ({
      'ta-slug-state.textContent': document.getElementById('ta-slug-state').textContent,
      'ta-create.disabled': document.getElementById('ta-create').disabled,
    }),
    expect: {
      'ta-slug-state.textContent': 'free',
      'ta-create.disabled': false,
    },
  },
  {
    name: '2. taken slug shows the issue and offers Open existing',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      ctx.fetch = () => respond(200, {ok: true, matches: [GOOD_REF], malformed: []});
      await vm.runInContext('taCheckSlug()', ctx);
    },
    check: document => ({
      'ta-slug-state.textContent': document.getElementById('ta-slug-state').textContent,
      'ta-create.disabled': document.getElementById('ta-create').disabled,
      'ta-open.hidden': document.getElementById('ta-open').hidden,
    }),
    expect: {
      'ta-slug-state.textContent': 'already requested',
      'ta-create.disabled': true,
      'ta-open.hidden': false,
    },
  },
  {
    // "every sink" #1 (single-match render + Open existing button).
    name: '3. [URL] single match with a javascript: URL is unreadable, not clickable',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      const evil = {...GOOD_REF, url: 'javascript:alert(1)'};
      ctx.fetch = () => respond(200, {ok: true, matches: [evil], malformed: []});
      await vm.runInContext('taCheckSlug()', ctx);
    },
    check: document => ({
      'ta-slug-state.textContent': document.getElementById('ta-slug-state').textContent,
      'ta-open.hidden': document.getElementById('ta-open').hidden,
      'ta-create.disabled': document.getElementById('ta-create').disabled,
    }),
    expect: {
      'ta-slug-state.textContent': 'cannot read: url missing or wrong type',
      'ta-open.hidden': true,
      'ta-create.disabled': true,
    },
  },
  {
    name: '4. several VALID matches show a conflict and keep Create disabled',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      const ref2 = {...GOOD_REF, number: 13, url: 'https://github.com/acme/widget/issues/13'};
      ctx.fetch = () => respond(200, {ok: true, matches: [GOOD_REF, ref2], malformed: []});
      await vm.runInContext('taCheckSlug()', ctx);
    },
    check: document => ({
      'ta-slug-state.textContent': document.getElementById('ta-slug-state').textContent,
      'ta-create.disabled': document.getElementById('ta-create').disabled,
      'ta-existing.innerHTML': document.getElementById('ta-existing').innerHTML,
    }),
    expect: {
      'ta-slug-state.textContent': '2 issues claim this slug — conflict',
      'ta-create.disabled': true,
      'ta-existing.innerHTML':
        '<a href="https://github.com/acme/widget/issues/12" target="_blank">#12</a> · ' +
        '<a href="https://github.com/acme/widget/issues/13" target="_blank">#13</a>',
    },
  },
  {
    // C-2 (Critical), input 1 of 3: literal null.
    name: '5. [C-2] matches: null must NOT enable Create or say "free"',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      ctx.fetch = () => respond(200, {ok: true, matches: null, malformed: []});
      await vm.runInContext('taCheckSlug()', ctx);
    },
    check: document => ({
      'ta-slug-state.textContent': document.getElementById('ta-slug-state').textContent,
      'ta-create.disabled': document.getElementById('ta-create').disabled,
    }),
    expect: {
      'ta-slug-state.textContent': 'cannot check: inbox could not be read exhaustively',
      'ta-create.disabled': true,
    },
  },
  {
    // C-2 (Critical), input 2 of 3: field absent entirely (undefined).
    name: '6. [C-2] matches field ABSENT entirely must NOT enable Create',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      ctx.fetch = () => respond(200, {ok: true, malformed: []}); // no `matches` key at all
      await vm.runInContext('taCheckSlug()', ctx);
    },
    check: document => ({
      'ta-slug-state.textContent': document.getElementById('ta-slug-state').textContent,
      'ta-create.disabled': document.getElementById('ta-create').disabled,
    }),
    expect: {
      'ta-slug-state.textContent': 'cannot check: inbox could not be read exhaustively',
      'ta-create.disabled': true,
    },
  },
  {
    // C-2 (Critical), input 3 of 3: an object where an array is expected.
    name: '7. [C-2] matches: {} (object, not array) must NOT enable Create',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      ctx.fetch = () => respond(200, {ok: true, matches: {}, malformed: []});
      await vm.runInContext('taCheckSlug()', ctx);
    },
    check: document => ({
      'ta-slug-state.textContent': document.getElementById('ta-slug-state').textContent,
      'ta-create.disabled': document.getElementById('ta-create').disabled,
    }),
    expect: {
      'ta-slug-state.textContent': 'cannot check: inbox could not be read exhaustively',
      'ta-create.disabled': true,
    },
  },
  {
    // Ordering proof: if business logic (matches.length / .some()) ran
    // BEFORE the Array.isArray type check, a string is `length`-having and
    // array-*like* enough to reach `matches.some(...)`, which strings do
    // NOT implement -> TypeError -> unhandled rejection, never a message.
    // Type-check-first means this is caught cleanly instead.
    name: '8. [ordering] matches: "ab" (string, length 2) must not crash, and ' +
      'must land in the SAME unknown state as null/absent/object',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      ctx.fetch = () => respond(200, {ok: true, matches: 'ab', malformed: []});
      await vm.runInContext('taCheckSlug()', ctx);
    },
    check: document => ({
      'ta-slug-state.textContent': document.getElementById('ta-slug-state').textContent,
      'ta-create.disabled': document.getElementById('ta-create').disabled,
    }),
    expect: {
      'ta-slug-state.textContent': 'cannot check: inbox could not be read exhaustively',
      'ta-create.disabled': true,
    },
    // (unhandled-rejection count is asserted globally at the end of the run)
  },
  {
    // C-2 (Critical), malformed variant: matches: [] (confirmed empty) but
    // malformed: null (not exhaustive) is STILL an unknown state overall.
    name: '9. [C-2] malformed: null with matches: [] must NOT enable Create',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      ctx.fetch = () => respond(200, {ok: true, matches: [], malformed: null});
      await vm.runInContext('taCheckSlug()', ctx);
    },
    check: document => ({
      'ta-slug-state.textContent': document.getElementById('ta-slug-state').textContent,
      'ta-create.disabled': document.getElementById('ta-create').disabled,
    }),
    expect: {
      'ta-slug-state.textContent': 'cannot check: inbox could not be read exhaustively',
      'ta-create.disabled': true,
    },
  },
  {
    // C-1 (Critical): a bad item in a multi-match list must fail the WHOLE
    // list closed -- the good item must NOT render as a normal conflict
    // entry with the bad one silently dropped (a partial list looks
    // authoritative and is not).
    name: '10. [C-1] one valid + one invalid item -> nothing rendered as a normal conflict',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      const bad = {...GOOD_REF, number: 13, url: undefined};
      ctx.fetch = () => respond(200, {ok: true, matches: [GOOD_REF, bad], malformed: []});
      await vm.runInContext('taCheckSlug()', ctx);
    },
    check: document => ({
      'ta-slug-state.textContent': document.getElementById('ta-slug-state').textContent,
      'ta-create.disabled': document.getElementById('ta-create').disabled,
      'ta-existing.hidden': document.getElementById('ta-existing').hidden,
      'ta-existing.innerHTML (must be empty, not partial)':
        document.getElementById('ta-existing').innerHTML,
    }),
    expect: {
      'ta-slug-state.textContent': 'cannot read the result',
      'ta-create.disabled': true,
      'ta-existing.hidden': true,
      'ta-existing.innerHTML (must be empty, not partial)': '',
    },
  },
  {
    // "every sink" #2 (multi-match render): C-1 + URL scheme gate combined.
    name: '11. [C-1 + URL] multi-match with a javascript: URL -> cannot read the result',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      const evil = {...GOOD_REF, number: 13, url: 'javascript:alert(1)'};
      ctx.fetch = () => respond(200, {ok: true, matches: [GOOD_REF, evil], malformed: []});
      await vm.runInContext('taCheckSlug()', ctx);
    },
    check: document => ({
      'ta-slug-state.textContent': document.getElementById('ta-slug-state').textContent,
      'ta-existing.innerHTML': document.getElementById('ta-existing').innerHTML,
    }),
    expect: {
      'ta-slug-state.textContent': 'cannot read the result',
      // must never contain the raw scheme -- proves the list was NOT
      // rendered at all, not just that esc() was applied to it
      'ta-existing.innerHTML': '',
    },
  },
  {
    name: '12. created: null renders "state unknown" and offers only Re-check',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      document.getElementById('ta-title').value = 'Add feature X';
      document.getElementById('ta-prose').value = 'because Y, done when Z';
      ctx.fetch = tokenAwareFetch(() => respond(200, {ok: true, created: null, error: 'no answer'}));
      document.getElementById('ta-create').listeners.click();
      await new Promise(r => setTimeout(r, 20));
    },
    check: document => ({
      'ta-result.textContent': document.getElementById('ta-result').textContent,
      'ta-recheck.hidden': document.getElementById('ta-recheck').hidden,
      'ta-create.disabled': document.getElementById('ta-create').disabled,
    }),
    expect: {
      'ta-result.textContent': 'issue may have been created; state unknown: no answer',
      'ta-recheck.hidden': false,
      'ta-create.disabled': true,
    },
  },
  {
    name: '13. created: true, issue: null renders "reading it back failed"',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      document.getElementById('ta-title').value = 'Add feature X';
      document.getElementById('ta-prose').value = 'because Y, done when Z';
      ctx.fetch = tokenAwareFetch(() => respond(200, {ok: true, created: true, issue: null}));
      document.getElementById('ta-create').listeners.click();
      await new Promise(r => setTimeout(r, 20));
    },
    check: document => ({
      'ta-result.textContent': document.getElementById('ta-result').textContent,
      'ta-recheck.hidden': document.getElementById('ta-recheck').hidden,
    }),
    expect: {
      'ta-result.textContent': 'issue created, but reading it back failed',
      'ta-recheck.hidden': false,
    },
  },
  {
    name: '14. malformed issue in the CREATE response -> cannot read the result',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      document.getElementById('ta-title').value = 'Add feature X';
      document.getElementById('ta-prose').value = 'because Y, done when Z';
      const badIssue = {
        number: '12', title: 'add feature X', state: 'open',
        url: 'https://github.com/acme/widget/issues/12', author: 'octocat',
      }; // number is a string, labels missing
      ctx.fetch = tokenAwareFetch(() => respond(200, {ok: true, created: true, issue: badIssue}));
      document.getElementById('ta-create').listeners.click();
      await new Promise(r => setTimeout(r, 20));
    },
    check: document => ({'ta-result.textContent': document.getElementById('ta-result').textContent}),
    expect: {
      'ta-result.textContent':
        'created, but cannot read the result: number missing or wrong type; ' +
        'labels missing or wrong type',
    },
  },
  {
    // "every sink" #3 (create-result render).
    name: '15. [URL] create response with a javascript: URL -> cannot read the result',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      document.getElementById('ta-title').value = 'Add feature X';
      document.getElementById('ta-prose').value = 'because Y, done when Z';
      const evilIssue = {...GOOD_REF, url: 'javascript:alert(1)'};
      ctx.fetch = tokenAwareFetch(() => respond(200, {ok: true, created: true, issue: evilIssue}));
      document.getElementById('ta-create').listeners.click();
      await new Promise(r => setTimeout(r, 20));
    },
    check: document => ({
      'ta-result.textContent': document.getElementById('ta-result').textContent,
      'ta-result.innerHTML (must not contain the scheme)':
        document.getElementById('ta-result').innerHTML,
    }),
    expect: {
      'ta-result.textContent': 'created, but cannot read the result: url missing or wrong type',
      'ta-result.innerHTML (must not contain the scheme)': '',
    },
  },
  {
    // data-dir rule: the entry point must copy the on-disk clone dirname
    // (mgEntryRepo, itself sourced from data-dir/mgDirBasename(p.path)),
    // never a display name -- see mutation proof #3 in the harness runner.
    name: '16. entry point uses data-dir basename, never the display name',
    drive: async (ctx, document) => {
      vm.runInContext("mgEntryRepo = 'maestro'", ctx);
      document.getElementById('ta-open-panel').listeners.click();
    },
    check: document => ({
      'ta-repo.textContent': document.getElementById('ta-repo').textContent,
      'task-authoring.hidden': document.getElementById('task-authoring').hidden,
    }),
    expect: {
      'ta-repo.textContent': '— maestro',
      'task-authoring.hidden': false,
    },
  },
  {
    // I-1 (Important): a 5xx may have reached the runner past the whitelist
    // gate -- whether it landed is unknown, so Create must stay disabled
    // and only Re-check is offered (contrast with case 18).
    name: '17. [I-1] 500 response -> state unknown, Re-check only, Create NOT re-enabled',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      document.getElementById('ta-title').value = 'Add feature X';
      document.getElementById('ta-prose').value = 'because Y, done when Z';
      ctx.fetch = tokenAwareFetch(() => respond(500, {detail: 'internal error'}));
      document.getElementById('ta-create').listeners.click();
      await new Promise(r => setTimeout(r, 20));
    },
    check: document => ({
      'ta-result.textContent': document.getElementById('ta-result').textContent,
      'ta-recheck.hidden': document.getElementById('ta-recheck').hidden,
      'ta-create.disabled': document.getElementById('ta-create').disabled,
    }),
    expect: {
      'ta-result.textContent': 'issue may have been created; state unknown: internal error',
      'ta-recheck.hidden': false,
      'ta-create.disabled': true,
    },
  },
  {
    // I-1 contrast case: a 422 is a REFUSAL (never reached the runner) --
    // safe to re-enable Create, no Re-check needed.
    name: '18. [I-1] 422 response -> request rejected, Create IS re-enabled',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      document.getElementById('ta-title').value = 'Add feature X';
      document.getElementById('ta-prose').value = 'because Y, done when Z';
      ctx.fetch = tokenAwareFetch(() => respond(422, {detail: 'bad dir'}));
      document.getElementById('ta-create').listeners.click();
      await new Promise(r => setTimeout(r, 20));
    },
    check: document => ({
      'ta-result.textContent': document.getElementById('ta-result').textContent,
      'ta-recheck.hidden': document.getElementById('ta-recheck').hidden,
      'ta-create.disabled': document.getElementById('ta-create').disabled,
    }),
    expect: {
      'ta-result.textContent': 'request rejected: bad dir',
      'ta-recheck.hidden': true,
      'ta-create.disabled': false,
    },
  },
  {
    name: '19. [minor] 200 with `null` body must not strand the screen',
    drive: async (ctx, document) => {
      vm.runInContext("taRepo = 'widget'", ctx);
      document.getElementById('ta-slug').value = 'add-x';
      document.getElementById('ta-title').value = 'Add feature X';
      document.getElementById('ta-prose').value = 'because Y, done when Z';
      ctx.fetch = tokenAwareFetch(() => respond(200, null));
      document.getElementById('ta-create').listeners.click();
      await new Promise(r => setTimeout(r, 20));
    },
    check: document => ({
      'ta-result.textContent': document.getElementById('ta-result').textContent,
      'ta-recheck.hidden': document.getElementById('ta-recheck').hidden,
    }),
    expect: {
      'ta-result.textContent': 'issue may have been created; state unknown: unreadable response',
      'ta-recheck.hidden': false,
    },
  },
  {
    name: '20. [minor] taRepo === null refuses instead of sending dir=null',
    drive: async (ctx, document) => {
      let fetchCalled = false;
      ctx.fetch = () => { fetchCalled = true; return respond(200, {ok: true, matches: []}); };
      document.getElementById('ta-slug').value = 'add-x';
      await vm.runInContext('taCheckSlug()', ctx);
      ctx._fetchCalledOnLookup = fetchCalled;

      fetchCalled = false;
      document.getElementById('ta-title').value = 'Add feature X';
      document.getElementById('ta-prose').value = 'because Y, done when Z';
      document.getElementById('ta-create').listeners.click();
      await new Promise(r => setTimeout(r, 20));
      ctx._fetchCalledOnCreate = fetchCalled;
    },
    check: (document, ctx) => ({
      'ta-slug-state.textContent (lookup)': document.getElementById('ta-slug-state').textContent,
      'fetch called on lookup': ctx._fetchCalledOnLookup,
      'ta-result.textContent (create)': document.getElementById('ta-result').textContent,
      'fetch called on create': ctx._fetchCalledOnCreate,
    }),
    expect: {
      'ta-slug-state.textContent (lookup)': 'no repo selected',
      'fetch called on lookup': false,
      'ta-result.textContent (create)': 'no repo selected',
      'fetch called on create': false,
    },
  },
];

(async () => {
  let unhandled = 0;
  process.on('unhandledRejection', e => {
    unhandled += 1;
    console.log(`    !! UNHANDLED REJECTION: ${e && e.stack || e}`);
  });

  let failures = 0;
  for (const c of CASES) {
    const document = makeDom();
    const ctx = {
      document, console, setTimeout, URL,
      fetch: () => Promise.reject(new Error('unset')),
      // ensureActionToken() is a pre-existing helper "already in the file"
      // (brief's Interfaces section) -- a dependency of the create handler,
      // not part of the task-authoring block, so it's stubbed rather than
      // sliced in.
      ensureActionToken: async () => 'test-token',
    };
    vm.createContext(ctx);
    vm.runInContext(`${helpers}\n${taBlock}`, ctx);

    await c.drive(ctx, document);
    const got = c.check(document, ctx);
    const exp = c.expect;
    let ok = true;
    for (const k of Object.keys(exp)) {
      if (got[k] !== exp[k]) ok = false;
    }
    failures += ok ? 0 : 1;
    console.log(`\n[${ok ? 'PASS' : 'FAIL'}] ${c.name}`);
    for (const k of Object.keys(exp)) {
      const mark = got[k] === exp[k] ? '=' : '!=';
      console.log(`  ${k}: ${JSON.stringify(got[k])} ${mark} expected ${JSON.stringify(exp[k])}`);
    }
  }
  await new Promise(r => setTimeout(r, 20));
  console.log(`\nunhandled rejections: ${unhandled}`);
  console.log(`\n${CASES.length - failures}/${CASES.length} cases passed`);
  process.exit(failures || unhandled ? 1 : 0);
})();
