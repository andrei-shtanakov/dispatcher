// Exercises the epics panel (ADR-ECO-010 Ф3) by running the REAL, WHOLE
// <script> of dispatcher/server/static/index.html inside a VM over the page's
// own parsed markup (tests/web/dom.js) — same discipline as the other
// harnesses here, and self-contained on purpose.
//
// What is asserted is one thing, in three states: a plane's COUNT may only be
// read as complete when the plane says it is. `partial` is where the first cut
// of this panel went wrong at the server — it reported `read` for a fleet it had
// half-observed — so the client must not undo the fix by rendering a partial
// count as a plain number, nor throw the count away as if nothing were read.
//
// Usage: node epics_harness.js <path-to-index.html>
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const {Document} = require(path.join(__dirname, 'dom.js'));
const {browserGlobals, openScreen} = require(path.join(__dirname, 'screens.js'));

const HTML_PATH = process.argv[2];
if (!HTML_PATH) {
  console.error('usage: node epics_harness.js <index.html>');
  process.exit(2);
}

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
const PAGE_SCRIPT = between(BODY_HTML, /<script[^>]*>/i, '</script>', 'script');

let failures = 0;
let asyncErrors = 0;
let currentCase = '(startup)';
let summaryPrinted = false;
function check(name, condition, detail) {
  if (condition) return;
  failures++;
  console.error(`FAIL ${name}${detail ? `: ${detail}` : ''}`);
}

// The same three crash guards every sibling harness carries
// (launchpad_harness.js and the rest). They matter more here since the cases
// became asynchronous: without the `exit` guard, an await on a promise that
// never settles would drain the event loop and exit 0 — a green suite over
// zero coverage. `tests/test_epics_js.py` bounds the same failure in wall
// time with a subprocess timeout.
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

// The page is a tab shell now: #epics lives inside the hidden
// `#screen-epics` tabpanel until a person opens it. Every case here boots on
// the Epics screen, opened through the real tab button (tests/web/screens.js)
// — asserting against markup nobody can see is exactly what the tab shell
// made possible, so it is closed off here at the source.
async function boot() {
  const document = new Document(BODY_HTML);
  const ctx = {
    document, console, URL,
    setTimeout, clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {},
    fetch: () => Promise.reject(new Error('the epics harness drives renderEpics directly')),
    ...browserGlobals(),
  };
  vm.createContext(ctx);
  vm.runInContext(PAGE_SCRIPT, ctx);
  const page = {ctx, document};
  await openScreen(page, 'epics');
  return page;
}

const plane = (name, state, count, detail) => ({plane: name, state, count, detail: detail || null});

function view(planes, extra) {
  return Object.assign({
    generated_at: null,
    registry_path: '/ws/epics.toml',
    registry_ok: true,
    registry_diagnostics: [],
    classification_diagnostics: [],
    programs: {eco: {title: 'Ecosystem', kind: 'ecosystem'}},
    planes,
    rows: [{
      type: 'epic', id: 'eco.ops', program: 'eco', kind: 'ecosystem',
      title: 'Ops', status: 'standing', moved_to: null,
      planes, defects: {}, last_activity_at: null, activity_sources: [],
    }],
    defects: [],
  }, extra || {});
}

const cells = document => Array.from(
  document.querySelector('#epics tbody').querySelectorAll('td')
).map(td => td.textContent);

// Opening the Epics tab is asynchronous (it goes through a real click and the
// hashchange the browser delivers), so the previously top-level case blocks
// now run inside this one async IIFE. Its body is deliberately left at the
// original indentation: re-indenting a hundred untouched lines would bury the
// actual change of this task in a cosmetic diff.
(async () => {

// ---- case 1: a complete plane renders its number, plainly ------------------
{
  currentCase = 'case 1: a complete plane';
  const {ctx, document} = await boot();
  ctx.renderEpics(view([
    plane('todo', 'read', 3),
    plane('issues', 'read', 2),
    plane('pull_requests', 'read', 0),
  ]));
  const text = cells(document).join('|');
  check('read renders the count', /(^|\|)3(\||$)/.test(text), text);
  check('read carries no partial marker', !text.includes('≥'), text);
  check('summary line shows plain counts',
    document.getElementById('epics-planes').textContent.includes('todo: 3'),
    document.getElementById('epics-planes').textContent);
}

// ---- case 2: an unread plane is a dash, never a zero -----------------------
{
  currentCase = 'case 2: an unread plane';
  const {ctx, document} = await boot();
  ctx.renderEpics(view([
    plane('todo', 'read', 3),
    plane('issues', 'unavailable', 0, 'no published snapshot'),
    plane('pull_requests', 'unavailable', 0, 'no published snapshot'),
  ]));
  const text = cells(document).join('|');
  check('unavailable renders a dash', text.includes('—'), text);
  const summary = document.getElementById('epics-planes').textContent;
  check('summary names it unavailable, not 0',
    summary.includes('issues: unavailable'), summary);
}

// ---- case 3: partial is neither a plain number nor a dash ------------------
{
  currentCase = 'case 3: partial';
  const {ctx, document} = await boot();
  const why = 'hosts still on snapshot v1 contribute nothing: h-old';
  ctx.renderEpics(view([
    plane('todo', 'read', 3),
    plane('issues', 'partial', 2, why),
    plane('pull_requests', 'partial', 0, why),
  ]));
  const text = cells(document).join('|');
  check('partial keeps the count as a lower bound', text.includes('≥2'), text);
  check('partial does not render a bare number',
    !/(^|\|)2(\||$)/.test(text), text);
  const summary = document.getElementById('epics-planes').textContent;
  check('summary marks partial', summary.includes('issues: ≥2 (partial)'), summary);
  const html = document.querySelector('#epics tbody').innerHTML;
  check('partial names its reason on hover', html.includes('h-old'), html);
}

// ---- case 4: a state this client has never heard of falls to the safe side --
{
  currentCase = 'case 4: an unknown plane state';
  const {ctx, document} = await boot();
  ctx.renderEpics(view([
    plane('todo', 'read', 3),
    plane('issues', 'sampled', 2, 'a state the server grew after this client shipped'),
    plane('pull_requests', 'read', 0),
  ]));
  const text = cells(document).join('|');
  check('an unknown state does not render as a measured count',
    !/(^|\|)2(\||$)/.test(text), text);
  check('an unknown state renders as not-read', text.includes('—'), text);
  const html = document.querySelector('#epics tbody').innerHTML;
  check('a server-supplied reason wins over any generic wording',
    html.includes('a state the server grew'), html);

  // ...and with no reason supplied, the wording points at THIS page
  const bare = await boot();
  bare.ctx.renderEpics(view([
    plane('todo', 'read', 1),
    plane('issues', 'sampled', 2),
    plane('pull_requests', 'read', 0),
  ]));
  const bareHtml = bare.document.querySelector('#epics tbody').innerHTML;
  check('an unrecognised state blames the client, not the fleet',
    bareHtml.includes('out of date'), bareHtml);
}

// ---- case 4b: `unavailable` without a detail is not called "unknown" -------
{
  currentCase = 'case 4b: unavailable without a detail';
  const {ctx, document} = await boot();
  ctx.renderEpics(view([
    plane('todo', 'read', 1),
    plane('issues', 'unavailable', 0),
    plane('pull_requests', 'unavailable', 0),
  ]));
  const html = document.querySelector('#epics tbody').innerHTML;
  check('a known gap is not reported as an unknown state',
    !html.includes('unknown plane state'), html);
}

// ---- case 5: a tag finding is reported without blaming the registry --------
{
  currentCase = 'case 5: a tag finding';
  const {ctx, document} = await boot();
  ctx.renderEpics(view(
    [plane('todo', 'read', 1), plane('issues', 'read', 1), plane('pull_requests', 'read', 0)],
    {classification_diagnostics: [
      {code: 'EP-UNKNOWN', severity: 'error', message: 'absent', subject_uri: 'owner/demo#7', raw: 'eco.typo'},
    ]},
  ));
  const reg = document.getElementById('epics-registry');
  check('tag findings are surfaced', reg.textContent.includes('EP-UNKNOWN'), reg.textContent);
  check('a tag typo does not mark the registry broken',
    reg.className !== 'err' && reg.textContent.includes('clean'), reg.textContent);
}

currentCase = '(summary)';
summaryPrinted = true;
if (failures || asyncErrors) {
  console.error(`\n${failures} epics-panel check(s) failed `
    + `\u00b7 ${asyncErrors} async error(s)`);
  process.exit(1);
}
console.log('epics harness: all checks passed');

})().catch(err => {
  summaryPrinted = true;
  console.error('\nHARNESS CRASHED:', (err && err.stack) || err);
  process.exit(1);
});
