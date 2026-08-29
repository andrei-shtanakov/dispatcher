// Tests the browser model, not the application: tests/web/dom.js's
// location/history/hashchange must behave like a browser, because the page's
// router is built on exactly these three behaviours.
'use strict';

const path = require('path');
const {makeBrowser} = require(path.join(__dirname, 'dom.js'));

let failures = 0;
const check = (cond, msg) => {
  if (!cond) { failures++; console.log(`  [FAIL] ${msg}`); }
};

// 1. assigning location.hash changes it and delivers hashchange
{
  const b = makeBrowser();
  const seen = [];
  b.window.addEventListener('hashchange', () => seen.push(b.location.hash));
  b.location.hash = '#sync';
  check(b.location.hash === '#sync', 'hash is #sync after assignment');
  check(seen.length === 1, `one hashchange delivered, got ${seen.length}`);
}

// 2. a bare value is normalised to a leading '#'
{
  const b = makeBrowser();
  b.location.hash = 'projects';
  check(b.location.hash === '#projects', `normalised, got ${b.location.hash}`);
}

// 3. assigning the SAME value delivers nothing
{
  const b = makeBrowser('#epics');
  let n = 0;
  b.window.addEventListener('hashchange', () => n++);
  b.location.hash = '#epics';
  check(n === 0, `same-value assignment is silent, got ${n} events`);
}

// 4. pushState/replaceState change the hash WITHOUT a hashchange
{
  const b = makeBrowser('#launchpad');
  let n = 0;
  b.window.addEventListener('hashchange', () => n++);
  b.history.pushState(null, '', '#waits');
  check(b.location.hash === '#waits', `pushState moved hash, got ${b.location.hash}`);
  check(n === 0, `pushState is silent, got ${n} events`);
}

// 5. back()/forward() walk the entries the page pushed and DO fire
{
  const b = makeBrowser('#launchpad');
  const seen = [];
  b.window.addEventListener('hashchange', () => seen.push(b.location.hash));
  b.location.hash = '#sync';
  b.location.hash = '#errors';
  b.history.back();
  check(b.location.hash === '#sync', `back lands on #sync, got ${b.location.hash}`);
  b.history.forward();
  check(b.location.hash === '#errors', `forward returns, got ${b.location.hash}`);
  check(seen.length === 4, `two moves + back + forward = 4, got ${seen.length}`);
}

// 6. dispatch() carries an init payload onto the event
{
  const {Document, dispatch} = require(path.join(__dirname, 'dom.js'));
  const doc = new Document('<button id="b"></button>');
  const seen = [];
  doc.getElementById('b').addEventListener('keydown', e => seen.push(e.key));
  dispatch(doc.getElementById('b'), 'keydown', {init: {key: 'ArrowRight'}});
  check(seen[0] === 'ArrowRight', `init reached the event, got ${seen[0]}`);
}

// 7. removeEventListener actually detaches
{
  const b = makeBrowser();
  let n = 0;
  const fn = () => n++;
  b.window.addEventListener('hashchange', fn);
  b.window.removeEventListener('hashchange', fn);
  b.location.hash = '#models';
  check(n === 0, `detached listener silent, got ${n}`);
}

// 8. matches(): a comma-separated selector LIST matches if ANY branch does
{
  const {Document} = require(path.join(__dirname, 'dom.js'));
  const doc = new Document('<button id="b"></button>');
  const b = doc.getElementById('b');
  check(b.matches('button, input, a'), 'a normal selector list still matches its own branch');
  check(!b.matches('input, a'), 'a selector list matches only if some branch does');
}

// 9. matches(): a comma INSIDE an attribute value is not a list separator
// (fix round 1, finding 3 — a naive `.split(',')` broke this).
{
  const {Document} = require(path.join(__dirname, 'dom.js'));
  const doc = new Document('<div id="d" data-x="a,b"></div>');
  const d = doc.getElementById('d');
  check(d.matches('[data-x="a,b"]'), 'a comma inside an attribute value still matches');
}

// 10. matches(): a trailing comma must not match EVERYTHING (an empty
// branch, dropped rather than handed to matchesCompound('') to match on).
{
  const {Document} = require(path.join(__dirname, 'dom.js'));
  const doc = new Document('<div id="d"></div>');
  const d = doc.getElementById('d');
  check(!d.matches('button,'), 'a trailing comma must not match every element');
}

console.log(failures ? `\nFAILED: ${failures}` : '\nOK: browser model');
process.exit(failures ? 1 : 0);
