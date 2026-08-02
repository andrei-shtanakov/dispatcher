# WS-005 WS-C: governance panel — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read-only web panel showing each observed repo's governance-bundle state on the project detail screen, fed exclusively by `collect_governance` — inbox issue #108, slug `ws005-governance-panel`.

**Architecture:** One new GET endpoint (`/api/projects/{name}/governance`) added through `read_api` (the shared lookup layer), returning the WS-B `BundleGovernance` model verbatim; `detail()` in the SPA fetches it and renders via a new `renderGovernance()`; client rendering is tested under the existing Node harness pattern (a new `governance_harness.js` calling the real function from the executed script); BEH-09 is a route-enumeration test; the cross-repo smoke installs the real steward CLI at the contract pin (mirror of `install_pinned_checker.sh` / DESIGN-405 level 3) and drives `gate-check --emit-verdicts` → HTTP → rendered state.

**Tech Stack:** FastAPI + pydantic (existing app), vanilla JS SPA (`static/index.html`), Node 22 harness (`tests/web/dom.js`), bash + uv for the pinned install, GitHub Actions.

## Global Constraints

- ARCH-C4: the panel consumes ONLY `collect_governance` — neither the endpoint nor the JS reads `.steward/gate_verdicts.jsonl` or resolves its path.
- ARCH-C2 / BEH-09: the governance surface is GET-only; a route-enumeration test asserts it.
- M-01: no damaged/stale/unresolvable class may render as pass — asserted at both layers (API state string; JS render output).
- M-02 / FR-05: findings (gate_id + message per artifact) and header provenance (`generated_at`, `source_commit`) visible on the one screen.
- Node and (for the smoke) the pinned `gate-check` binary are HARD prerequisites — tests fail, never skip (repo discipline from PR #98 / task-authoring).
- uv only; ruff + pyrefly clean; 88 cols; tests for every piece.
- Branch `feat/ws005-governance-panel`; PR via `gh pr create`; no merging.

---

### Task 1: `read_api.governance` + GET endpoint + BEH-09 route test

**Files:**
- Modify: `dispatcher/core/read_api.py` (new function after `onboarding`)
- Modify: `dispatcher/server/app.py` (route after `project_onboarding`)
- Test: `tests/test_governance_api.py` (new)

**Interfaces:**
- Consumes: `collect_governance(repo_root) -> BundleGovernance`, `read_api.project(cache, name) -> ProjectSnapshot` (has `.path`), `ReadLookupError`.
- Produces: `read_api.governance(cache: SnapshotService, name: str) -> BundleGovernance`; route `GET /api/projects/{name}/governance` (404 on unknown name). Task 2's JS fetches this route.

- [ ] **Step 1: Failing tests** — `tests/test_governance_api.py`:

```python
"""GET /api/projects/{name}/governance (WS-005 WS-C, inbox #108).

The endpoint is a pass-through of the WS-B read model (ARCH-C4): tests seed
REAL tmp git repos so freshness comes from the true provider — no fixture
here may accidentally test a mock of the thing the panel exists to show.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from conftest import make_arbiter

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.governance import VERDICTS_REL_PATH
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio

FIXTURES = (
    Path(__file__).parent.parent
    / "contracts"
    / "steward-gate-verdicts"
    / "v1"
    / "fixtures"
)
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}
BUNDLE = "workstreams/WS-005-gate-verdicts/spec"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


def _seed_verdicts(project_root: Path, fixture: str) -> None:
    """Make the detected project a real git repo whose verdicts file's
    header names its actual HEAD, so the live git-facts provider says fresh."""
    bundle = project_root / BUNDLE
    bundle.mkdir(parents=True)
    (bundle / "10-requirements.md").write_text("r\n")
    (bundle / "15-behaviour-spec.md").write_text("b\n")
    _git(project_root, "init", "--quiet")
    _git(project_root, "add", "-A")
    _git(project_root, "commit", "--quiet", "-m", "seed")
    head = _git(project_root, "rev-parse", "HEAD")
    lines = (FIXTURES / fixture).read_text().splitlines()
    header = json.loads(lines[0])
    header["source_commit"] = head
    lines[0] = json.dumps(header)
    target = project_root / VERDICTS_REL_PATH
    target.parent.mkdir()
    target.write_text("\n".join(lines) + "\n")


def _client(tmp_path: Path) -> httpx.AsyncClient:
    make_arbiter(tmp_path)
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_no_verdicts_file_is_no_data_not_404(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/arbiter/governance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "no-data"


async def test_clean_fixture_in_a_real_repo_is_pass_with_provenance(
    tmp_path: Path,
) -> None:
    _seed_verdicts(tmp_path / "arbiter", "clean.jsonl")
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/arbiter/governance")
    data = resp.json()
    assert data["state"] == "pass"
    assert data["header"]["generated_at"]
    assert len(data["header"]["source_commit"]) == 40
    assert len(data["artifacts"]) == 2


async def test_findings_fixture_is_blocked_with_findings(tmp_path: Path) -> None:
    _seed_verdicts(tmp_path / "arbiter", "findings.jsonl")
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/arbiter/governance")
    data = resp.json()
    assert data["state"] == "blocked"
    assert {f["artifact"] for f in data["findings"]} == {
        "10-requirements.md",
        "15-behaviour-spec.md",
    }


async def test_malformed_fixture_is_unreadable_never_pass(tmp_path: Path) -> None:
    """M-01 at the API layer, on the canon damage fixture."""
    _seed_verdicts(tmp_path / "arbiter", "malformed_line.jsonl")
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/arbiter/governance")
    data = resp.json()
    assert data["state"] == "unreadable"
    assert data["reason"]


async def test_unknown_project_is_404(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/nope/governance")
    assert resp.status_code == 404


async def test_governance_surface_is_get_only(tmp_path: Path) -> None:
    """BEH-09/ARCH-C2 stated structurally: every route whose path mentions
    governance accepts GET (+ implicit HEAD) and nothing else."""
    make_arbiter(tmp_path)
    app = create_app(DispatcherConfig(roots=(tmp_path,)))
    governance_routes = [
        r for r in app.routes if "governance" in getattr(r, "path", "")
    ]
    assert governance_routes, "the governance route must exist to be constrained"
    for route in governance_routes:
        assert set(route.methods) <= {"GET", "HEAD"}, route.path
```

- [ ] **Step 2: Run — expect failures** (`uv run pytest tests/test_governance_api.py -v`): 404s / missing route.

- [ ] **Step 3: Implement.** `read_api.py` (imports `collect_governance`, `BundleGovernance` from `dispatcher.core.governance`):

```python
def governance(cache: SnapshotService, name: str) -> BundleGovernance:
    """WS-005 WS-C: bundle state for one project (ARCH-C4 pass-through)."""
    snap = project(cache, name)
    return collect_governance(Path(snap.path))
```

`app.py`, after `project_onboarding` (same shape):

```python
    @app.get("/api/projects/{name}/governance", response_model=BundleGovernance)
    def project_governance(name: str) -> BundleGovernance:
        """WS-005 WS-C: read-only bundle state (ARCH-C2: GET only)."""
        try:
            return read_api.governance(cache, name)
        except read_api.ReadLookupError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
```

- [ ] **Step 4: Run tests → all pass; `uv run ruff format . && uv run ruff check . && uv run pyrefly check`.**

- [ ] **Step 5: Commit** `feat(governance): GET /api/projects/{name}/governance (BEH-09 GET-only)`.

---

### Task 2: Web panel — `renderGovernance()` + Node harness

**Files:**
- Modify: `dispatcher/server/static/index.html` (`renderGovernance` near `renderOnboarding`; fetch in `detail()`; `<div id="governance"></div>` inside the detail section markup)
- Create: `tests/web/governance_harness.js`
- Test: `tests/test_governance_js.py` (mirror of `tests/test_task_authoring_js.py`, hard-requires node)

**Interfaces:**
- Consumes: route from Task 1; `esc()`, `get()` helpers already in the script.
- Produces: `renderGovernance(g)` → HTML string (pure, testable); `detail()` populates `#governance`.

- [ ] **Step 1: Markup + render function.** In the detail section markup add `<h3 id="governance-title" hidden>Governance bundle</h3><div id="governance"></div>`. Render (pure function, no fetch inside — same style as `renderOnboarding`):

```javascript
function governanceBadge(state) {
  // Non-pass classes MUST NOT read as pass (M-01): one badge per state,
  // pass is the only green word on the panel.
  const marks = {
    "pass": "✅ pass", "blocked": "⛔ blocked", "no-data": "∅ no-data",
    "unreadable": "✖ unreadable", "stale": "⌛ stale",
    "unresolvable": "❓ unresolvable",
  };
  return marks[state] || `✖ ${state}`;
}

function renderGovernance(g) {
  const badge = `<p><b>${esc(governanceBadge(g.state))}</b>${
    g.reason ? ` <span class="dim">${esc(g.reason)}</span>` : ""}</p>`;
  const provenance = g.header ? `<p class="dim">emitted ${
    esc(g.header.generated_at)} @ ${esc(g.header.source_commit.slice(0, 12))}
    · profile ${esc(g.header.profile)} · bundle ${esc(g.header.bundle)}</p>` : "";
  const blockedPaths = new Set(
    (g.findings || []).map(f => f.artifact)
  );
  const artifacts = (g.artifacts || []).length ? `
    <ul>${g.artifacts.map(a => `
      <li>${blockedPaths.has(a.path) ? "⛔" : "·"} ${esc(a.path)}
        · ${esc(a.status)}${a.node_id ? ` · ${esc(a.node_id)}` : ""}</li>`
    ).join("")}</ul>` : "";
  const findings = (g.findings || []).length ? `
    <ul>${g.findings.map(f => `
      <li>${esc(f.gate_id)} [${esc(f.verdict)}] ${esc(f.artifact)}:
        ${esc(f.message)}${
        (g.unresolvable_findings || []).some(u => u.artifact === f.artifact)
          ? " <b>(outside the inventory)</b>" : ""}</li>`
    ).join("")}</ul>` : "";
  return badge + provenance + artifacts + findings;
}
```

In `detail()`, after the spec-runner-config block:

```javascript
  const gvTitle = document.getElementById("governance-title");
  const gvPanel = document.getElementById("governance");
  gvTitle.hidden = true;
  gvPanel.textContent = "";
  try {
    const gv = await get(
      "/api/projects/" + encodeURIComponent(name) + "/governance"
    );
    gvTitle.hidden = false;
    gvPanel.innerHTML = renderGovernance(gv);
  } catch {
    // unknown project already surfaced by the onboarding block above
  }
```

- [ ] **Step 2: Harness.** `tests/web/governance_harness.js` — load `index.html` with the same VM/dom bootstrap `task_authoring_harness.js` uses (read that file first, reuse its loader verbatim), then run cases that call the REAL `renderGovernance` from the executed script with the six state payloads and assert (this is where M-01/M-02 live client-side):

- `pass` payload (header present) → output contains `pass`, `generated_at` value, first 12 chars of `source_commit`;
- each of `blocked/no-data/unreadable/stale/unresolvable` → output does NOT contain the substring `"✅"` and DOES contain its own state name; `unreadable` with a reason shows the reason;
- `blocked` with two findings → both `gate_id`s and messages present; artifact rows for blocked paths marked `⛔`, clean ones not (BEH-07: clean shown apart);
- `unresolvable` → the dangling finding is annotated `(outside the inventory)`;
- XSS guard: a finding message `<img src=x onerror=1>` arrives escaped (no raw `<img` in output).

- [ ] **Step 3: Runner.** `tests/test_governance_js.py` — copy the structure of `tests/test_task_authoring_js.py` (`_run`, hard `assert shutil.which("node")`), pointing at `governance_harness.js`.

- [ ] **Step 4: Run** `uv run pytest tests/test_governance_js.py tests/test_task_authoring_js.py -v` (the second proves the shared loader still works). All pass.

- [ ] **Step 5: Commit** `feat(governance): web panel — six-state rendering (BEH-01/07 UI, M-01/M-02)`.

---

### Task 3: Cross-repo live smoke with the real steward binary

**Files:**
- Create: `scripts/install_pinned_steward.sh` (mirror of `install_pinned_checker.sh`: reads pin from `contracts/steward-gate-verdicts/v1/manifest.json`, installs `steward @ git+https://github.com/andrei-shtanakov/steward@<pin>` into an isolated venv, prints bin dir)
- Modify: `.github/workflows/ci.yml` (test job: `scripts/install_pinned_steward.sh >> "$GITHUB_PATH"` next to the checker install)
- Test: `tests/test_governance_live_smoke.py`

**Interfaces:**
- Consumes: steward CLI `gate-check <spec_dir> --profile <yaml-or-name> --emit-verdicts` (writes `<repo>/.steward/gate_verdicts.jsonl`, needs live git provenance); profile YAML shape as in steward's `tests/verdicts/test_emitter.py` `_PROFILE`.
- Produces: the end-to-end proof: real emitter at the pin → real file → HTTP → state.

- [ ] **Step 1: Script.** Copy `install_pinned_checker.sh`, change MANIFEST path, PRODUCER_URL to steward, package name `steward`, default venv dir `dispatcher-pinned-steward`. `chmod +x`.

- [ ] **Step 2: Smoke test.** Hard-require the binary (fail, don't skip — PR #98 discipline). Seed a real git repo (reuse `_seed`-style helper: bundle `spec/` with the two frontmatter'd md files from steward's emitter test, plus a `profile.yaml` OUTSIDE the bundle dir), run `gate-check spec --profile profile.yaml --emit-verdicts` with `cwd=repo` and the venv bin prepended to PATH; assert the file exists; then `create_app` over the repo's parent as root and GET the governance endpoint; assert `state` is the exact value observed at the pin (determine empirically during implementation, document it in the test docstring; it MUST be one of `pass`/`blocked` — a clean committed tree can be neither stale nor unreadable, and asserting the exact one keeps the smoke honest).

- [ ] **Step 3: CI.** Add the install step to `.github/workflows/ci.yml` test job.

- [ ] **Step 4: Run locally** with `PATH="$(scripts/install_pinned_steward.sh):$PATH" uv run pytest tests/test_governance_live_smoke.py -v`.

- [ ] **Step 5: Commit** `test(governance): live smoke — real gate-check at the contract pin`.

---

### Task 4: Full gate, TODO flip, PR

- [ ] **Step 1:** `uv run ruff format . && uv run ruff check . && uv run pyrefly check`, then full suite `PATH="$(scripts/install_pinned_checker.sh):$PATH" PATH="$(scripts/install_pinned_steward.sh):$PATH" uv run pytest` — everything green, nothing skipped.
- [ ] **Step 2:** Push, `gh pr create` (body: ARCH-C2/C4 evidence, M-01/M-02 coverage map, smoke provenance). Flip the `TODO.md` item to `[x] … — PR #<N>` and push.
- [ ] **Step 3:** Copilot review loop; do not merge.

## Self-review notes

- BEH-01 UI (Task 2 pass case + Task 1 provenance assertions), BEH-07 UI (Task 2 blocked case, clean artifacts shown apart), BEH-09 (Task 1 route test), M-01 (Task 1 API + Task 2 render, canon damage fixtures), M-02 (Task 2 findings-with-messages case), FR-05 (provenance line), ARCH-C4 (endpoint calls only `collect_governance`; JS calls only the endpoint), NFR-01 (still disk-only), smoke (Task 3). TUI/MCP surfaces intentionally not extended: the issue asks for the panel; MCP whitelist is pinned at 14 tools (FR-05 memory) and extending it is its own decision.
- Names used across tasks: `renderGovernance`, `governanceBadge`, `#governance`, `#governance-title`, `read_api.governance`, route `/api/projects/{name}/governance` — consistent.
