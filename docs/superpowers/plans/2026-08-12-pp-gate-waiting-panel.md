# PP gate_waiting PR-2: web panel + Node harness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The per-project «Product proposals» web panel over `GET /api/projects/{name}/product-proposals`, with the fetch-race guard and Node-harness tests — PR-2 of spec `docs/superpowers/specs/2026-08-12-product-proposal-gate-waiting-design.md` (inbox #129). Phase-1 acceptance closes here.

**Architecture:** Same pattern as the governance panel: a section in `server/static/index.html` filled by `detail()` on project selection; the panel reads ONLY the API and never re-classifies. Non-`ok` bundles render as «classification suppressed …», so `waits: []` can never be mistaken for «0 gates waiting»; 404 hides the section, 200-with-diagnostics shows an error; a generation counter drops stale responses.

**Tech Stack:** vanilla JS inside the single `index.html`, Node 22 harness (`tests/web/`), pytest wrapper.

## Global Constraints

- **Prerequisite: PR-1 is merged** (`feat/pp-gate-waiting-collector`); the endpoint and its response shape exist on master.
- Node is a HARD prerequisite of the JS test: a missing `node` FAILS the test, it never skips.
- A local mirror path is NEVER turned into an href: `artifact_ref` and the relative bundle path render as copy-friendly text (spec «Web panel»).
- «0 gates waiting» and «0 bundles» are explicit, distinct labels.
- Every non-`ok` bundle shows a «classification suppressed — <state>» wording (spec refinement).
- Git workflow: branch `feat/pp-gate-waiting-panel`, PR via `gh pr create`, no direct pushes to master, no merging (the user merges).
- `uv run ruff format .`, `uv run ruff check .`, `pyrefly check` before every commit that touches Python.

---

### Task 1: Branch from the merged master

**Files:** none (git only)

- [ ] **Step 1: Sync and branch**

```bash
git switch master && git pull --ff-only
git switch -c feat/pp-gate-waiting-panel
```

Verify PR-1 landed: `uv run pytest tests/test_product_proposals_api.py -q`
must pass on this branch before anything else.

---

### Task 2: Node harness + pytest wrapper (failing first)

**Files:**
- Create: `tests/web/product_proposals_harness.js`
- Create: `tests/test_product_proposals_js.py`

**Interfaces:**
- Consumes: `tests/web/dom.js` (exists), the shipped `dispatcher/server/static/index.html`.
- Produces: the executable acceptance for the panel; Task 3 implements against it.

- [ ] **Step 1: Write the harness**

`tests/web/product_proposals_harness.js` — same discipline as
`governance_harness.js` (whole real `<script>` in a VM over the page's own
markup; self-contained fixtures):

```javascript
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
//      renders into the new panel;
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
};
const report = (extra = {}) => ({
  mirror_path: '/repos/impresario', bundles: [], waits: [],
  diagnostics: [], attention: false, ...extra,
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

function overviewProjects(names) {
  return names.map(name => ({
    name, detected: true, path: `/repos/${name}`,
    counts: {tasks: 0, models: 0, test_results: 0, errors: 0},
    freshness: 'fresh', warnings: [],
  }));
}

function defaultRoutes(names, ppRoute) {
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
    [u => u.endsWith('/product-proposals'), ppRoute],
  ];
}

const drain = async (turns = 5) => {
  for (let i = 0; i < turns; i++) await new Promise(r => setTimeout(r, 0));
};

async function boot(ppRoute, names = ['impresario']) {
  const document = new Document(BODY_HTML);
  const routes = defaultRoutes(names, ppRoute);
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
```

- [ ] **Step 2: Write the pytest wrapper**

`tests/test_product_proposals_js.py` — the `test_governance_js.py`
discipline verbatim:

```python
"""Runs the product-proposals panel client JS under Node (tests/web/).

Same discipline as tests/test_governance_js.py: the harness parses the
shipped index.html, runs its WHOLE <script> in a VM over the
dependency-free DOM (tests/web/dom.js) and drives the real
`renderProductProposals` / `detail()` code — nothing is sliced or simulated.

Node is a HARD prerequisite: a missing `node` FAILS this test, it does not
skip — a skip is how a suite goes green while covering nothing.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

WEB = Path(__file__).parent / "web"
HARNESS = WEB / "product_proposals_harness.js"
INDEX_HTML = (
    Path(__file__).parent.parent / "dispatcher" / "server" / "static" / "index.html"
)

_MISSING_NODE = (
    "node is a required prerequisite of this test suite for verifying the "
    "product-proposals panel's client JS — install Node (CI pins Node 22 via "
    "actions/setup-node in ci.yml's `test` job). Without it the panel "
    "acceptance is UNVERIFIED, and that must FAIL, not skip."
)


def test_product_proposals_panel_js() -> None:
    node = shutil.which("node")
    assert node is not None, _MISSING_NODE
    result = subprocess.run(
        [node, str(HARNESS), str(INDEX_HTML)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"harness failed\n--- stdout ---\n{result.stdout}"
        f"\n--- stderr ---\n{result.stderr}"
    )
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_product_proposals_js.py -v`
Expected: FAIL — `#product-proposals is not in the page markup` (the panel
does not exist yet).

- [ ] **Step 4: Commit the failing acceptance**

```bash
git add tests/web/product_proposals_harness.js tests/test_product_proposals_js.py
git commit -m "test: Node harness acceptance for the product-proposals panel (red)"
```

---

### Task 3: Implement the panel in index.html

**Files:**
- Modify: `dispatcher/server/static/index.html`

**Interfaces:**
- Consumes: `GET /api/projects/{name}/product-proposals` (PR-1); existing helpers `esc()`, `detail()`, and the `taGen` generation-counter precedent.
- Produces: `renderProductProposals(report)`, `productProposalBadge(state)`, elements `#product-proposals-title` / `#product-proposals`, module state `ppGen`.

- [ ] **Step 1: Add the section markup**

In `dispatcher/server/static/index.html`, directly after the governance
section markup

```html
  <h3 id="governance-title" hidden>Governance bundle</h3>
  <div id="governance"></div>
```

add:

```html
  <h3 id="product-proposals-title" hidden>Product proposals</h3>
  <div id="product-proposals"></div>
```

- [ ] **Step 2: Add the module state**

Next to the existing `let taGen = 0;` declaration in the page script add:

```javascript
// Inbox #129 PR-2: generation counter for the product-proposals panel —
// a late response for the PREVIOUS project must never render here.
let ppGen = 0;
```

- [ ] **Step 3: Add the renderers (right after `renderGovernance`)**

```javascript
// Inbox #129 phase 1: read-only rendering of product-proposal gate waits.
// Producer decides — dispatcher renders; this layer never re-classifies.
function productProposalBadge(state) {
  // Non-ok states MUST read as «classification suppressed», never as
  // «0 gates waiting»: a non-ok bundle's empty waits are suppressed, not
  // absent (spec: fail-closed invariant 1). "✅ ok" is the only green word.
  const marks = {
    "ok": "✅ ok",
    "unreadable": "✖ classification suppressed — unreadable",
    "unknown": "❓ classification suppressed — unknown",
    "conflict": "⛔ classification suppressed — conflict",
  };
  return marks[state] || `✖ classification suppressed — ${state}`;
}

function renderProductProposals(r) {
  const reportDiags = (r.diagnostics || []).length ? `
    <ul>${r.diagnostics.map(d => `
      <li><b>⚠ ${esc(d.code)}</b>: ${esc(d.message)}${
        d.path ? ` · ${esc(d.path)}` : ""}</li>`).join("")}</ul>` : "";
  const bundles = r.bundles || [];
  const suppressed = bundles.filter(b => b.state !== "ok").length;
  const note = suppressed ? `<p><b>⚠ ${suppressed} bundle(s): classification
    suppressed</b> — their waits are unknown, not zero</p>` : "";
  let waits = "";
  if ((r.waits || []).length) {
    waits = `<table><tr><th>Proposal</th><th>Gate</th><th>Authority</th>
      <th>Version</th><th>Proposal updated</th><th>Bundle</th></tr>${
      r.waits.map(w => `<tr>
        <td>${esc(w.artifact_ref)}</td>
        <td>${esc(w.gate_label)} (${esc(w.gate_id)})</td>
        <td><b>${esc(w.authority)}</b></td>
        <td>v${esc(String(w.version))}</td>
        <td>${esc(w.proposal_updated_at)}</td>
        <td>${esc(w.bundle_path)}</td></tr>`).join("")}</table>`;
  } else if (!bundles.length) {
    if (!(r.diagnostics || []).length) waits = `<p class="dim">0 bundles</p>`;
  } else if (!suppressed) {
    waits = `<p class="dim">0 gates waiting</p>`;
  }
  const rows = bundles.length ? `
    <ul>${bundles.map(b => `
      <li>${esc(b.path)} · ${esc(productProposalBadge(b.state))}${
        b.status ? ` · ${esc(b.status)}` : ""}${
        b.version != null ? ` · v${esc(String(b.version))}` : ""}${
        (b.diagnostics || []).length ? `
        <ul>${b.diagnostics.map(d => `
          <li class="dim">${esc(d.code)}: ${esc(d.message)}${
            d.path ? ` · ${esc(d.path)}` : ""}</li>`).join("")}</ul>` : ""}</li>`
    ).join("")}</ul>` : "";
  return reportDiags + note + waits + rows;
}
```

- [ ] **Step 4: Wire it into `detail()`**

In `detail()`, right after the governance block (the `try { const gv = await
get(... "/governance"); … } catch { … }` statement) and before
`section.scrollIntoView(...)`, add:

```javascript
  const ppTitle = document.getElementById("product-proposals-title");
  const ppPanel = document.getElementById("product-proposals");
  ppTitle.hidden = true;
  ppPanel.textContent = "";
  // Spec «Web panel»: 404 (not this kind of project / unknown project)
  // hides the section; a 200 report — including mirror-not-detected and
  // mirror-anchors-missing — is RENDERED, error and all. The generation
  // token drops a late response for the previous project.
  const myPp = ++ppGen;
  try {
    const resp = await fetch(
      "/api/projects/" + encodeURIComponent(name) + "/product-proposals"
    );
    if (myPp === ppGen && resp.ok) {
      const report = await resp.json();
      if (myPp === ppGen) {
        ppTitle.hidden = false;
        ppPanel.innerHTML = renderProductProposals(report);
      }
    } else if (myPp === ppGen && resp.status !== 404) {
      ppTitle.hidden = false;
      ppPanel.textContent = "product-proposals endpoint failed: " + resp.status;
    }
  } catch (err) {
    // A network failure is fail-loud (unknown must not look like «no
    // waits») — unlike a 404, which states «not this kind of project».
    if (myPp === ppGen) {
      ppTitle.hidden = false;
      ppPanel.textContent = String(err);
    }
  }
```

- [ ] **Step 5: Run the harness**

Run: `uv run pytest tests/test_product_proposals_js.py -v`
Expected: PASS (all 7 cases).

- [ ] **Step 6: Run the neighbouring JS suites (same file, shared script)**

Run: `uv run pytest tests/test_governance_js.py tests/test_task_authoring_js.py -v`
Expected: PASS. The governance harness has no `/product-proposals` fixture
route, so the new panel's fetch REJECTS there — the panel code above
catches it and renders the error text into its own section, which those
suites do not assert on. An unhandled rejection in either suite means the
new code let a promise escape — fix the panel code, not the harness.

- [ ] **Step 7: Full suite + commit**

```bash
uv run pytest -q
git add dispatcher/server/static/index.html
git commit -m "feat: product-proposals web panel — waits table, suppressed wording, race guard"
```

---

### Task 4: Close the acceptance — TODO, gates, PR

**Files:**
- Modify: `TODO.md`

- [ ] **Step 1: Close the TODO item**

In `TODO.md`, change the checkbox of the
`@id:product-proposal-gate-waiting` item to `[x]` and append the PR numbers
to the checkbox line's title text (keep all inline tags on that same line),
e.g.:

```markdown
- [x] Read-only `gate_waiting`: какие product-решения ждут человека — фаза 1, contract-backed состояния — PR #<PR-1>, #<PR-2> @owner:github:andrei-shtanakov @id:product-proposal-gate-waiting
```

Update the «Прогресс:» line in the body to state both PRs and that
phase-1 acceptance (web + Node harness) is closed; leave the phase-2 note
(blocked on impresario) as is.

- [ ] **Step 2: Full verification gate**

```bash
uv run ruff format . && uv run ruff check . && pyrefly check
IMPRESARIO_PINNED_DIR="$(scripts/checkout_pinned_impresario.sh --from ../impresario)" uv run pytest -q
```

Expected: everything green, including the three JS suites and the live smoke.

- [ ] **Step 3: Commit, push, open the PR**

```bash
git add TODO.md
git commit -m "docs(todo): close product-proposal-gate-waiting — phase-1 acceptance done"
git push -u origin feat/pp-gate-waiting-panel
gh pr create --title "feat: product-proposals web panel (inbox #129, PR-2)" --body "$(cat <<'EOF'
## Summary
- per-project «Product proposals» panel over `GET /api/projects/{name}/product-proposals` (same pattern as the governance panel; reads ONLY the API)
- non-ok bundles render as «classification suppressed — <state>»: `waits: []` can never read as «0 gates waiting»; «0 gates waiting» vs «0 bundles» are distinct labels
- 404 hides the section; 200 mirror diagnostics (`mirror-not-detected`, `mirror-anchors-missing`) render as a visible error
- fetch-race guard (`ppGen`): a late response for the previous project never renders
- Node-harness acceptance (`tests/web/product_proposals_harness.js`, hard prerequisite — fails without node)

Closes the phase-1 acceptance of inbox #129 (spec `docs/superpowers/specs/2026-08-12-product-proposal-gate-waiting-design.md`); the TODO item is checked with both PR numbers. Issue #129 is closed by a human after this merges.

## Test plan
- [ ] `uv run pytest tests/test_product_proposals_js.py -v`
- [ ] `uv run pytest tests/test_governance_js.py tests/test_task_authoring_js.py -v` (shared page script)
- [ ] full: `IMPRESARIO_PINNED_DIR="$(scripts/checkout_pinned_impresario.sh --from ../impresario)" uv run pytest -q`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Read the GitHub Copilot review**

Fix valid comments with new commits, answer invalid ones with reasoning —
never apply blindly; iterate until no open comments remain. Do NOT merge —
the user merges. After the merge: remind the user that issue #129 can be
closed, and that the follow-ups recorded in the spec (governance-panel race
guard retrofit; TUI/VSCode/MCP parity; phase-2 `needs_human`) are NOT part
of this delivery.

## Self-review notes (already applied)

- Spec coverage: waits table with «Proposal updated» labelling, suppressed
  wording, distinct zero-labels, 404-vs-200 split, race guard, no-href rule,
  escaping — each has a harness case; the panel never touches the
  filesystem and adds no non-GET calls.
- The governance harness interaction (no `/product-proposals` route → the
  panel catches its own rejection) is stated in Task 3 Step 6 rather than
  left to be discovered.
