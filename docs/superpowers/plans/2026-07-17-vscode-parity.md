# FR-06 VSCode Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** J-03 fully walkable from VSCode: a Sync tree view with pull / open-PR / track / ignore actions, and a QuickPick config-editor flow — closing FR-06 entirely (TUI half shipped in PR #44).

**Architecture:** One small server addition (`GET /api/spec-runner-configs`). Everything else is extension-side: `api.ts` grows `ApiError` (body `detail`), full sync types, token-cached POSTs; `model.ts` grows the finite-contextValue parity predicate; a new `SyncProvider` in `tree.ts`; a new pure `configFlow.ts` state machine with a thin command driver in `extension.ts`.

**Tech Stack:** Python/FastAPI (server bit), TypeScript + vitest (ext — `vi.stubGlobal("fetch", ...)` + JSON fixtures convention). No new dependencies on either side.

**Spec:** `docs/superpowers/specs/2026-07-17-vscode-parity-design.md` (DESIGN-601..607) — read it first.

## Global Constraints

- Python side: line length 88 (ruff), type hints, `uv run pyrefly check` + `uv run ruff format --check .` clean; full pytest suite green (baseline 255 passed + 1 skipped).
- Ext side: `cd vscode-ext && npm run typecheck && npm test && npm run build` — all three must pass after every ext task (this is exactly the CI job).
- `api.ts` and `configFlow.ts` MUST stay vscode-free (no `import * as vscode`) — vitest runs them without mocks; `tree.ts`/`extension.ts` are the only vscode adapters.
- contextValues are EXACTLY three literal strings: `dispatcherSyncVerdict.pull`, `dispatcherSyncVerdict.pullPr`, `dispatcherSyncProposal` — menu `when` clauses match them literally.
- Error surfacing: NEVER hardcode a message per HTTP status — `ApiError.detail` (the FastAPI body) is what the user sees (409 busy vs conflict differ only there). `ok=false` + `detail=="no-op"` is checked BEFORE any generic failure branch.
- Token: cached; on 403 re-fetch EXACTLY once and retry; second 403 fails. `track` posts WITHOUT a token.
- Branch: create `feat/vscode-parity` off master before Task 1; push after the last task (controller opens the PR).

---

## File Structure

- Modify: `dispatcher/server/app.py` — the list endpoint (Task 1).
- Modify: `tests/test_api.py` — endpoint pytest (Task 1).
- Modify: `vscode-ext/src/api.ts` — ApiError, sync types, POSTs, token cache (Task 2).
- Modify: `vscode-ext/test/api.test.ts` + new fixtures (Task 2).
- Modify: `vscode-ext/src/model.ts` — `syncItemContext`, sync label helpers (Task 3).
- Modify: `vscode-ext/src/tree.ts` — `SyncProvider` (Task 3).
- Modify: `vscode-ext/src/extension.ts` — wiring: provider, action commands, editor command (Tasks 3-4).
- Modify: `vscode-ext/package.json` — view, commands, menus (Tasks 3-4).
- Create: `vscode-ext/src/configFlow.ts` + `vscode-ext/test/configFlow.test.ts` (Task 4).
- Modify: `vscode-ext/test/model.test.ts` — parity guard (Task 3).
- Modify (docs): `README.md`, `spec/discovery-brief-customer.md` FR-06 line, TUI spec context note (Task 5).

---

### Task 1: Server — `GET /api/spec-runner-configs` (DESIGN-601)

**Files:**
- Modify: `dispatcher/server/app.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Produces: `GET /api/spec-runner-configs` → `list[ProjectSpecRunnerConfig]` (the model already carries `project`, `project_yaml_path`, `base_mtime`, `typed` with per-field `value`/`explicit`, `extra_executor_config`, `extra_explicit` — the ext consumes exactly these).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api.py` (mirror the sibling spec-runner-config tests' client/workspace pattern — read them first):

```python
@pytest.mark.anyio
async def test_spec_runner_configs_list_reaches_non_overview_projects(
    tmp_path: Path,
) -> None:
    """DESIGN-601: enumeration across roots — incl. dirs that are NOT
    overview cards (a bare steward/project.yaml). This is the discovery
    gap the per-name GET can't close (it needs a known name)."""
    # workspace with one collector project (overview card) and one bare
    # config-only dir (no collector match)
    make_atp(tmp_path)
    steward = tmp_path / "steward"
    steward.mkdir()
    (steward / "project.yaml").write_text(
        "project: steward\nspec_runner:\n  max_retries: 5\nworkstreams: []\n"
    )
    config = DispatcherConfig(roots=(tmp_path,))
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.get("/api/spec-runner-configs")
    assert resp.status_code == 200
    data = resp.json()
    dirs = [Path(c["project_yaml_path"]).parent.name for c in data]
    assert "steward" in dirs  # not an overview card, still listed
    entry = next(c for c in data if c["project"] == "steward")
    assert entry["typed"]["max_retries"]["value"] == 5
    assert entry["typed"]["max_retries"]["explicit"] is True
    assert entry["base_mtime"] > 0
```

(Adapt imports/fixture names to the file's existing conventions — `make_atp`, `httpx`, `create_app`, `DispatcherConfig` are all already imported/used there; verify.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_api.py -v -k configs_list`
Expected: FAIL — 404 (route not registered).

- [ ] **Step 3: Implement**

In `dispatcher/server/app.py`, next to the existing per-name config route (`spec_runner_config_view`), register BEFORE any conflicting dynamic path (it's a distinct static path — no conflict, but keep them adjacent for readability):

```python
    @app.get(
        "/api/spec-runner-configs",
        response_model=list[ProjectSpecRunnerConfig],
    )
    def spec_runner_configs_list() -> list[ProjectSpecRunnerConfig]:
        """Enumerate every discovered project.yaml across all roots.

        Basename-keyed action contract: the action key is the directory
        NAME. Same-named dirs in two roots appear twice here and BOTH
        resolve to the first root at action time — fail-closed via the
        base_mtime conflict (409), but visible as duplicates. Closes the
        DISCOVERY gap (no other endpoint lists names); fetching a known
        name was already possible via the per-name GET.
        """
        configs, _ = discover_project_configs(config.roots)
        return configs
```

- [ ] **Step 4: Full Python suite, format, lint, type-check**

Run: `uv run pytest -q && uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: 256 passed + 1 skipped; clean.

- [ ] **Step 5: Commit**

```bash
git add dispatcher/server/app.py tests/test_api.py
git commit -m "feat: GET /api/spec-runner-configs — enumeration across roots (DESIGN-601)"
```

---

### Task 2: `ApiClient` — ApiError, sync types, POSTs, token cache (DESIGN-602)

**Files:**
- Modify: `vscode-ext/src/api.ts`
- Test: `vscode-ext/test/api.test.ts`, new fixtures under `vscode-ext/test/fixtures/`

**Interfaces:**
- Produces (used by Tasks 3-4): `class ApiError extends Error { status: number; detail: string }`; extended `SyncStatusResponse` (`report.hosts[]`, `report.proposals[]` per the shapes below); `ActionOutcome` interface; methods `pull(dir)`, `createPr(dir)`, `track(dir, action)`, `specRunnerConfigs()`, `updateSpecRunnerConfig(body)`; `SpecRunnerConfigEntry` types.

- [ ] **Step 1: Write the failing tests**

Add fixtures: `sync_full.json` (a SyncStatus body with one live host carrying three verdicts — pull-first/ahead=2, ok, pull-first/ahead=null — one kb host, one proposal `newrepo`), `spec_runner_configs.json` (a one-entry list matching Task 1's response shape), `action_outcome_ok.json` (`{"action":"pull","dir":"alpha","ok":true,"detail":"fast-forwarded", ...nulls}`).

Append to `vscode-ext/test/api.test.ts` (existing conventions: `vi.stubGlobal("fetch", ...)`, `okResponse`, `fixture`):

```typescript
function jsonResponse(payload: unknown, status: number): Response {
  return new Response(JSON.stringify(payload), { status });
}

describe("ApiClient sync shape", () => {
  it("parses hosts, verdicts and proposals", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(okResponse(fixture("sync_full.json"))),
    );
    const sync = await new ApiClient("http://x").sync();
    expect(sync.report.hosts).toHaveLength(2);
    expect(sync.report.hosts[0].verdicts[0].ahead).toBe(2);
    expect(sync.report.proposals).toEqual(["newrepo"]);
  });
});

describe("ApiError", () => {
  it("carries the body detail on non-ok responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          jsonResponse({ detail: "alpha: update already in flight" }, 409),
        ),
    );
    const err = await new ApiClient("http://x")
      .sync()
      .then(() => null)
      .catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(409);
    expect((err as ApiError).detail).toBe("alpha: update already in flight");
  });

  it("falls back to HTTP status text when the body is not JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response("nope", { status: 500 })),
    );
    const err = await new ApiClient("http://x")
      .sync()
      .then(() => null)
      .catch((e: unknown) => e);
    expect((err as ApiError).detail).toContain("HTTP 500");
  });
});

describe("action POSTs and the token cache", () => {
  it("fetches the token once and reuses it", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ token: "tok1" }))
      .mockResolvedValueOnce(okResponse(fixture("action_outcome_ok.json")))
      .mockResolvedValueOnce(okResponse(fixture("action_outcome_ok.json")));
    vi.stubGlobal("fetch", fetchMock);
    const api = new ApiClient("http://x");
    await api.pull("alpha");
    await api.createPr("alpha");
    const calls = fetchMock.mock.calls;
    expect(calls[0][0]).toBe("http://x/api/actions/session");
    expect(calls[1][0]).toBe("http://x/api/actions/pull");
    expect((calls[1][1] as RequestInit).headers).toMatchObject({
      "X-Action-Token": "tok1",
    });
    expect(calls[2][0]).toBe("http://x/api/actions/create-pr");
    expect(calls).toHaveLength(3); // token fetched once, reused
  });

  it("on 403 refetches the token exactly once and retries", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ token: "stale" }))
      .mockResolvedValueOnce(jsonResponse({ detail: "bad token" }, 403))
      .mockResolvedValueOnce(okResponse({ token: "fresh" }))
      .mockResolvedValueOnce(okResponse(fixture("action_outcome_ok.json")));
    vi.stubGlobal("fetch", fetchMock);
    const outcome = await new ApiClient("http://x").pull("alpha");
    expect(outcome.ok).toBe(true);
    expect(fetchMock.mock.calls).toHaveLength(4);
  });

  it("fails after the second 403", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(okResponse({ token: "stale" }))
      .mockResolvedValueOnce(jsonResponse({ detail: "bad token" }, 403))
      .mockResolvedValueOnce(okResponse({ token: "still-stale" }))
      .mockResolvedValueOnce(jsonResponse({ detail: "bad token" }, 403));
    vi.stubGlobal("fetch", fetchMock);
    const err = await new ApiClient("http://x")
      .pull("alpha")
      .then(() => null)
      .catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(403);
    expect(fetchMock.mock.calls).toHaveLength(4); // no third refetch
  });

  it("track posts without a token", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(okResponse({ tracked: ["newrepo"], ignored: [] }));
    vi.stubGlobal("fetch", fetchMock);
    await new ApiClient("http://x").track("newrepo", "track");
    expect(fetchMock.mock.calls[0][0]).toBe("http://x/api/sync/track");
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(
      (init.headers as Record<string, string>)["X-Action-Token"],
    ).toBeUndefined();
  });
});

describe("specRunnerConfigs degradation", () => {
  it("surfaces 404 as ApiError(status=404) for the old-server branch", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "Not Found" }, 404)),
    );
    const err = await new ApiClient("http://x")
      .specRunnerConfigs()
      .then(() => null)
      .catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(404);
  });
});
```

- [ ] **Step 2: Run to verify they fail**

Run: `cd vscode-ext && npm test`
Expected: FAIL — `ApiError` not exported, methods missing.

- [ ] **Step 3: Implement in `vscode-ext/src/api.ts`**

Extend the sync types (replace `SyncReportSummary` usage additively — keep existing fields):

```typescript
export interface RepoVerdict {
  repo: string;
  verdict: string;
  reason: string | null;
  branch: string | null;
  ahead: number | null;
  behind: number | null;
  dirty: boolean;
  is_kb: boolean;
}

export interface HostPanel {
  host: string;
  source: string; // "live" | "kb"
  generated_at: string | null;
  age_seconds: number | null;
  stale: boolean;
  gh_error: string | null;
  error: string | null;
  verdicts: RepoVerdict[];
}

export interface SyncReportSummary {
  current_host: string;
  top_line: string;
  top_reason: string | null;
  hosts: HostPanel[];
  proposals: string[];
  warnings: string[];
}

export interface ActionOutcome {
  action: string;
  dir: string;
  ok: boolean;
  detail: string | null;
  error: string | null;
  pr_url: string | null;
}

export interface TypedField {
  value: unknown;
  explicit: boolean;
}

export interface SpecRunnerConfigEntry {
  project: string;
  project_yaml_path: string;
  base_mtime: number;
  typed: Record<string, TypedField>;
  extra_executor_config: Record<string, unknown>;
  extra_explicit: boolean;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(detail);
  }
}

const ACTION_TIMEOUT_MS = 130_000; // server subprocess cap is 120s
```

Rework the private helpers and add POSTs (the token is the client's only state):

```typescript
  private token: string | null = null;

  private async raise(resp: Response, path: string): Promise<never> {
    let detail = `${path}: HTTP ${resp.status}`;
    try {
      const body = (await resp.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      // non-JSON body: keep the HTTP fallback
    }
    throw new ApiError(resp.status, detail);
  }

  private async get<T>(path: string): Promise<T> {
    const resp = await fetch(`${this.baseUrl}${path}`, {
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!resp.ok) {
      await this.raise(resp, `GET ${path}`);
    }
    return (await resp.json()) as T;
  }

  private async postJson<T>(
    path: string,
    body: unknown,
    headers: Record<string, string>,
  ): Promise<Response> {
    return fetch(`${this.baseUrl}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(ACTION_TIMEOUT_MS),
    });
  }

  private async fetchToken(): Promise<string> {
    const session = await this.get<{ token: string }>("/api/actions/session");
    this.token = session.token;
    return session.token;
  }

  private async postWithToken<T>(path: string, body: unknown): Promise<T> {
    const token = this.token ?? (await this.fetchToken());
    let resp = await this.postJson(path, body, { "X-Action-Token": token });
    if (resp.status === 403) {
      // process token rotated (server restart): refetch EXACTLY once
      const fresh = await this.fetchToken();
      resp = await this.postJson(path, body, { "X-Action-Token": fresh });
    }
    if (!resp.ok) {
      await this.raise(resp, `POST ${path}`);
    }
    return (await resp.json()) as T;
  }

  pull(dir: string): Promise<ActionOutcome> {
    return this.postWithToken("/api/actions/pull", { dir });
  }

  createPr(dir: string): Promise<ActionOutcome> {
    return this.postWithToken("/api/actions/create-pr", { dir });
  }

  async track(
    dir: string,
    action: "track" | "ignore",
  ): Promise<{ tracked: string[]; ignored: string[] }> {
    const resp = await this.postJson("/api/sync/track", { dir, action }, {});
    if (!resp.ok) {
      await this.raise(resp, "POST /api/sync/track");
    }
    return (await resp.json()) as { tracked: string[]; ignored: string[] };
  }

  specRunnerConfigs(): Promise<SpecRunnerConfigEntry[]> {
    return this.get("/api/spec-runner-configs");
  }

  updateSpecRunnerConfig(body: {
    dir: string;
    typed: Record<string, unknown>;
    extra_executor_config: null;
    base_mtime: number;
  }): Promise<ActionOutcome> {
    return this.postWithToken("/api/actions/update-spec-runner-config", body);
  }
```

(Keep the file vscode-free. The existing `get` callers and the "throws on non-200" test change shape: that old test asserted `HTTP 500` in the message — the fallback detail keeps that substring, verify it still passes or adjust its assertion to `ApiError`.)

- [ ] **Step 4: Ext gate**

Run: `cd vscode-ext && npm run typecheck && npm test && npm run build`
Expected: all pass (old tests + new).

- [ ] **Step 5: Commit**

```bash
git add vscode-ext/src/api.ts vscode-ext/test/api.test.ts vscode-ext/test/fixtures/
git commit -m "feat(ext): ApiError with body detail, full sync types, token-cached action POSTs (DESIGN-602)"
```

---

### Task 3: Sync view + action commands (DESIGN-603/605)

**Files:**
- Modify: `vscode-ext/src/model.ts`, `vscode-ext/src/tree.ts`, `vscode-ext/src/extension.ts`, `vscode-ext/package.json`
- Test: `vscode-ext/test/model.test.ts`

**Interfaces:**
- Consumes: Task 2's types.
- Produces: `syncItemContext(v: RepoVerdict, live: boolean): string | null` in `model.ts`; `SyncProvider` in `tree.ts` (constructor pattern of the existing providers, `setData(sync | null)`); commands `dispatcher.pull|openPr|track|ignore` wired in `extension.ts`.

- [ ] **Step 1: Write the failing parity-guard tests** (`model.test.ts`):

```typescript
describe("syncItemContext (web/TUI parity)", () => {
  const v = (o: Partial<RepoVerdict>): RepoVerdict => ({
    repo: "a",
    verdict: "ok",
    reason: null,
    branch: null,
    ahead: null,
    behind: null,
    dirty: false,
    is_kb: false,
    ...o,
  });

  it("pull-first + live + ahead -> pullPr (both actions)", () => {
    expect(syncItemContext(v({ verdict: "pull-first", ahead: 2 }), true)).toBe(
      "dispatcherSyncVerdict.pullPr",
    );
  });
  it("pull-first + live without ahead -> pull only (None and 0 both)", () => {
    expect(syncItemContext(v({ verdict: "pull-first" }), true)).toBe(
      "dispatcherSyncVerdict.pull",
    );
    expect(syncItemContext(v({ verdict: "pull-first", ahead: 0 }), true)).toBe(
      "dispatcherSyncVerdict.pull",
    );
  });
  it("non-live or non-pull-first -> null", () => {
    expect(syncItemContext(v({ verdict: "pull-first", ahead: 2 }), false)).toBe(
      null,
    );
    expect(syncItemContext(v({ verdict: "ok" }), true)).toBe(null);
  });
});
```

- [ ] **Step 2: Implement `syncItemContext` in `model.ts`** (pure; mirrors `_can_pull`/`_can_open_pr`, tui/app.py:113-119):

```typescript
/** Finite contextValue for a sync verdict row — web/TUI visibility parity:
 * pull ⇔ live && pull-first; open PR additionally needs truthy ahead. */
export function syncItemContext(
  v: RepoVerdict,
  live: boolean,
): string | null {
  if (!live || v.verdict !== "pull-first") {
    return null;
  }
  return v.ahead ? "dispatcherSyncVerdict.pullPr" : "dispatcherSyncVerdict.pull";
}
```

- [ ] **Step 3: `SyncProvider` in `tree.ts`** (follow the existing provider pattern exactly — EventEmitter, `setData`, offline item):

Node type: `{kind:"host"; panel: HostPanel} | {kind:"verdict"; v: RepoVerdict; live: boolean} | {kind:"proposal"; dir: string} | {kind:"offline"}`. Host items: label `host (source)`, description age (reuse/port the TUI's age formatting into a small `model.ts` helper) + `stale`; error hosts render the error text as description with a failed icon. Verdict items: label repo (📌 prefix when `is_kb`), description `↑{ahead ?? "—"}/↓{behind ?? "—"}` + ` ✎` when dirty, tooltip reason, icon by verdict (passed/warn/dim per the projects-provider icon pattern), `contextValue = syncItemContext(v, live) ?? undefined`. Proposal items: label dir, description "proposal", `contextValue = "dispatcherSyncProposal"`, icon `question`.

- [ ] **Step 4: Wire commands + package.json**

`extension.ts`: register the provider (`dispatcherSync`) fed from the existing poll (`lastSync` already holds the full response now); commands:

```typescript
    vscode.commands.registerCommand(
      "dispatcher.pull",
      (node: SyncNode) => void runAction("pull", node),
    ),
    vscode.commands.registerCommand(
      "dispatcher.openPr",
      (node: SyncNode) => void runAction("create-pr", node),
    ),
    vscode.commands.registerCommand(
      "dispatcher.track",
      (node: SyncNode) => void decideProposal("track", node),
    ),
    vscode.commands.registerCommand(
      "dispatcher.ignore",
      (node: SyncNode) => void decideProposal("ignore", node),
    ),
```

with `runAction` using `vscode.window.withProgress` around `client().pull/createPr(repo)`, then: `outcome.ok` → `showInformationMessage` with `pr_url ?? detail` (and an "Open PR" button → `vscode.env.openExternal(vscode.Uri.parse(pr_url))` when present); `ok=false` → `showErrorMessage(outcome.error ?? ...)`; `catch (e)` → `e instanceof ApiError ? e.detail : String(e)`. Then `void poll()`. `decideProposal` posts track/ignore then polls.

`package.json`: add the `dispatcherSync` view (after Roadmap), the four commands (palette-hidden via `commandPalette: when false` like `showError`), and `view/item/context` menus:

```json
      "view/item/context": [
        { "command": "dispatcher.pull", "when": "view == dispatcherSync && viewItem == dispatcherSyncVerdict.pull", "group": "inline" },
        { "command": "dispatcher.pull", "when": "view == dispatcherSync && viewItem == dispatcherSyncVerdict.pullPr", "group": "inline" },
        { "command": "dispatcher.openPr", "when": "view == dispatcherSync && viewItem == dispatcherSyncVerdict.pullPr", "group": "inline" },
        { "command": "dispatcher.track", "when": "view == dispatcherSync && viewItem == dispatcherSyncProposal", "group": "inline" },
        { "command": "dispatcher.ignore", "when": "view == dispatcherSync && viewItem == dispatcherSyncProposal", "group": "inline" }
      ]
```

(also add `dispatcherSync` to the refresh `view/title` when-clause). Commands get `icon` fields (`$(arrow-down)` pull, `$(git-pull-request)` openPr, `$(add)` track, `$(x)` ignore) so `group: "inline"` renders them as row buttons.

- [ ] **Step 5: Ext gate + commit**

Run: `cd vscode-ext && npm run typecheck && npm test && npm run build`

```bash
git add vscode-ext/src/model.ts vscode-ext/src/tree.ts vscode-ext/src/extension.ts vscode-ext/package.json vscode-ext/test/model.test.ts
git commit -m "feat(ext): Sync tree view with pull/openPr/track/ignore actions (DESIGN-603/605)"
```

---

### Task 4: Config editor QuickPick flow (DESIGN-604)

**Files:**
- Create: `vscode-ext/src/configFlow.ts`, `vscode-ext/test/configFlow.test.ts`
- Modify: `vscode-ext/src/extension.ts`, `vscode-ext/package.json`

**Interfaces:**
- Consumes: Task 2's `SpecRunnerConfigEntry`, `ApiClient.updateSpecRunnerConfig`, `ApiError`.
- Produces: pure module `configFlow.ts`:
  - `type FlowState = { entry: SpecRunnerConfigEntry; edits: Record<string, unknown> }`
  - `newFlow(entry): FlowState`
  - `validateField(entry, field, raw): string | null` — null = valid; message otherwise (bool: only true/false case-insensitive; int: integer parse; str: always valid — the TUI's `coerce_typed` rules verbatim, bool-before-int)
  - `applyEdit(state, field, raw): FlowState` (coerces; caller validates first)
  - `fieldItems(state): {field: string; value: unknown; marker: "explicit"|"default"|"edited"}[]`
  - `diffLines(state): string[]` (`- f: old` / `+ f: new` for EDITED fields; empty → `["(no changes)"]`; first line is the honesty caption "PR diff may include already-explicit keys unchanged")
  - `requestBody(state): {dir; typed; extra_executor_config: null; base_mtime}` — `typed` = all 12 current-or-edited coerced values; `dir` = basename of `project_yaml_path`.

- [ ] **Step 1: Write the failing tests** (`configFlow.test.ts`, pure vitest — no vscode):

Cover: markers (explicit/default from entry, edited after applyEdit); validateField bool "yes"→message, "TRUE"→null, int "3.5"→message, " 7 "→null, str anything→null; coercion types in requestBody (edited "9" → 9 number, bool "true" → true); diffLines only edited fields + caption + "(no changes)"; edit-then-reenter state survival (applyEdit returns a NEW state whose fieldItems reflect the edit — the diff-doc-reopen scenario); requestBody carries base_mtime and extra null; dir is the basename.

- [ ] **Step 2: Implement `configFlow.ts`** per the interface above (pure TS; coercion mirrors `dispatcher/tui/config_edit.py::coerce_typed` — bool checked before int by checking the ENTRY's current value type: `typeof current === "boolean"` → bool rules; `typeof current === "number"` → int rules; else string. NOTE: the entry's typed values come from JSON, so booleans/numbers/strings are native JSON types — no TYPED_DEFAULTS table needed ext-side; the field's CURRENT value type is the coercion authority).

- [ ] **Step 3: Command driver in `extension.ts`** (`dispatcher.editSpecRunnerConfig`, palette-visible):

```typescript
async function editConfigCommand(): Promise<void> {
  let entries: SpecRunnerConfigEntry[];
  try {
    entries = await client().specRunnerConfigs();
  } catch (e) {
    if (e instanceof ApiError && e.status === 404) {
      void vscode.window.showWarningMessage(
        "server does not support the config editor (upgrade dispatcher)",
      );
      return;
    }
    throw e;
  }
  const picked = await vscode.window.showQuickPick(
    entries.map((entry) => ({
      label: path.basename(path.dirname(entry.project_yaml_path)),
      description: entry.project,
      entry,
    })),
    { title: "spec-runner config: choose a project" },
  );
  if (!picked) return;
  let state = newFlow(picked.entry);
  // field loop: lives until confirm/cancel; diff preview re-enters with
  // the SAME state (the flow's bug magnet — state is in configFlow, not
  // in this closure's locals beyond `state` itself)
  for (;;) {
    const choice = await vscode.window.showQuickPick(
      [
        ...fieldItems(state).map((f) => ({
          label: f.field,
          description: `${String(f.value)} (${f.marker})`,
        })),
        { label: "$(diff) Preview diff", description: "" },
        { label: "$(git-pull-request) Confirm → PR", description: "" },
      ],
      { title: "spec-runner config: edit fields" },
    );
    if (!choice) return; // cancelled
    if (choice.label.endsWith("Preview diff")) {
      const doc = await vscode.workspace.openTextDocument({
        content: diffLines(state).join("\n"),
        language: "diff",
      });
      await vscode.window.showTextDocument(doc, { preview: true });
      continue; // re-enter the loop with the same state
    }
    if (choice.label.endsWith("Confirm → PR")) {
      await confirmConfig(state);
      return;
    }
    const field = choice.label;
    const current = fieldItems(state).find((f) => f.field === field);
    const raw = await vscode.window.showInputBox({
      title: field,
      value: String(current?.value ?? ""),
      validateInput: (input) => validateField(state.entry, field, input),
    });
    if (raw !== undefined) {
      state = applyEdit(state, field, raw);
    }
  }
}
```

`confirmConfig`: `withProgress` → `client().updateSpecRunnerConfig(requestBody(state))` → outcome order per the spec: `detail === "no-op"` FIRST (info "config already in this state — no PR needed"), then `ok` (info + Open PR button), then error toast with `outcome.error`; `catch` → `ApiError.detail`. package.json: command entry (palette-visible) + optionally the Sync view title menu.

- [ ] **Step 4: Ext gate + commit**

Run: `cd vscode-ext && npm run typecheck && npm test && npm run build`

```bash
git add vscode-ext/src/configFlow.ts vscode-ext/test/configFlow.test.ts vscode-ext/src/extension.ts vscode-ext/package.json
git commit -m "feat(ext): QuickPick spec-runner config editor (DESIGN-604)"
```

---

### Task 5: Docs + FR-06 closure (DESIGN-607)

**Files:**
- Modify: `README.md`, `spec/discovery-brief-customer.md`, `docs/superpowers/specs/2026-07-17-tui-parity-design.md`

- [ ] **Step 1: README** — the VSCode section gains the Sync view + actions and the config-editor command (mirror the terse style of the existing section; mention the new `GET /api/spec-runner-configs` in the API list).

- [ ] **Step 2: `spec/discovery-brief-customer.md`** — the FR-06 entry gains a resolution line: closed 2026-07-17 (TUI slice PR #44 + VSCode slice, specs `2026-07-17-tui-parity-design.md` / `2026-07-17-vscode-parity-design.md`).

- [ ] **Step 3: TUI spec context note** — update "VSCode half remains → next iteration" to "VSCode half shipped (see `2026-07-17-vscode-parity-design.md`); FR-06 closed".

- [ ] **Step 4: Verify both gates, commit, push**

```bash
uv run pytest -q && uv run ruff format --check .
cd vscode-ext && npm run typecheck && npm test && npm run build && cd ..
git add README.md spec/discovery-brief-customer.md docs/superpowers/specs/2026-07-17-tui-parity-design.md
git commit -m "docs: record the VSCode parity slice — FR-06 closed (DESIGN-607)"
git push -u origin feat/vscode-parity
```

---

## Self-Review Notes

- **Spec coverage:** DESIGN-601 → Task 1 (endpoint + docstring contract + non-overview pytest). DESIGN-602 → Task 2 (ApiError/detail, full sync types, POSTs, token single-retry both branches, track tokenless, 404 degradation, timeouts). DESIGN-603 → Task 3 (provider, finite contextValues, withProgress, poll-after). DESIGN-604 → Task 4 (pure flow state, diff-reopen preservation, validateInput strictness, no-op-first ordering, Open PR button). DESIGN-605 → Task 3's parity test (incl. `ahead: 0` — pinned here since the TUI review flagged it as untested there). DESIGN-606 → distributed. DESIGN-607 → Task 5. §3 error table: every row lands in Task 2 (ApiError/403/404) or Tasks 3-4 (toasts, no-op order, timeout).
- **Type consistency:** `RepoVerdict`/`HostPanel` field names match the server's pydantic models verbatim (checked against `dispatcher/core/sync.py:50-74`); `requestBody` matches `UpdateSpecRunnerConfigRequest` (dir/typed/extra_executor_config/base_mtime); `SpecRunnerConfigEntry` matches `ProjectSpecRunnerConfig`'s JSON.
- **Placeholder scan:** clean; the two read-first notes (test_api conventions in Task 1, old non-200 test assertion in Task 2) are explicit instructions.
- **Known judgment calls:** coercion authority ext-side is the CURRENT value's JSON type (no TYPED_DEFAULTS mirror in TS — one less drift surface; recorded in Task 4 Step 2); command icons + `group: "inline"` chosen so actions render as row buttons (hover UX) while still appearing in the context menu.
