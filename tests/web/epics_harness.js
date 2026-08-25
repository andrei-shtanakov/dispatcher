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
function check(name, condition, detail) {
  if (condition) return;
  failures++;
  console.error(`FAIL ${name}${detail ? `: ${detail}` : ''}`);
}

function boot() {
  const document = new Document(BODY_HTML);
  const ctx = {
    document, console, URL,
    setTimeout, clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {},
    fetch: () => Promise.reject(new Error('the epics harness drives renderEpics directly')),
    window: {open: () => {}},
  };
  vm.createContext(ctx);
  vm.runInContext(PAGE_SCRIPT, ctx);
  return {ctx, document};
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

// ---- case 1: a complete plane renders its number, plainly ------------------
{
  const {ctx, document} = boot();
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
  const {ctx, document} = boot();
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
  const {ctx, document} = boot();
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

// ---- case 4: a tag finding is reported without blaming the registry --------
{
  const {ctx, document} = boot();
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

if (failures) {
  console.error(`\n${failures} epics-panel check(s) failed`);
  process.exit(1);
}
console.log('epics harness: all checks passed');
