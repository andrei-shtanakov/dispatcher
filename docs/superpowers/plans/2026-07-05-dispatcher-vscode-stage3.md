# Dispatcher Stage 3: VSCode Extension Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** A native VSCode extension (`vscode-ext/`) with a Dispatcher
sidebar (Projects + Errors trees), a status-bar health indicator, and
auto-start of the dispatcher server, fed by the Stage 1 HTTP API.

**Architecture:** Thin native extension with a central 10 s poller.
VSCode-free modules (`api.ts`, `model.ts`, `server.ts`) hold all logic and
are vitest-tested; VSCode-dependent modules (`tree.ts`, `status.ts`,
`extension.ts`) are thin adapters verified by typecheck + manual smoke.

**Tech Stack:** TypeScript (strict), esbuild bundle, vitest, @vscode/vsce.
Node ≥20 (native `fetch`). No UI frameworks, no runtime dependencies.

**Spec:** `docs/superpowers/specs/2026-07-05-dispatcher-vscode-design.md` —
binding. Note one structure refinement vs spec §1: pure decision logic
lives in a dedicated `src/model.ts` (imported by tree/status/server)
rather than inside `tree.ts`/`status.ts`, so vitest never imports the
`vscode` module. This fulfils spec §1's "all decision logic … lives in
exported pure functions" with cleaner test isolation.

## Global Constraints

- All extension work happens under `vscode-ext/`; the Python package,
  tests, and API must not change (`uv run pytest` stays green untouched).
- npm only (no pip/uv for this part); run npm commands with
  `cd vscode-ext` or `npm --prefix vscode-ext`.
- TypeScript strict mode; `npm run typecheck` (tsc --noEmit), `npm test`
  (vitest run), `npm run build` (esbuild) must all pass after every task.
- `src/api.ts`, `src/model.ts`, `src/server.ts` must not import `vscode`.
- Settings and defaults exactly as spec §5: `dispatcher.url` =
  `http://127.0.0.1:8787`, `dispatcher.projectDir` = `""`,
  `dispatcher.autoStart` = `true`, `dispatcher.pollSeconds` = `10` (min 5).
- Offline is explicit: on fetch failure the trees show a single
  `server unreachable` node and the status bar shows offline — never
  silently stale data (spec §3/§4).
- One spawn attempt per offline episode; an episode ends when a poll
  succeeds (spec §4).
- Commits end with:
  `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: Scaffold vscode-ext

**Files:**
- Create: `vscode-ext/package.json`, `vscode-ext/tsconfig.json`,
  `vscode-ext/esbuild.mjs`, `vscode-ext/.gitignore`,
  `vscode-ext/src/extension.ts` (placeholder), `vscode-ext/.vscodeignore`
- Test: `vscode-ext/test/scaffold.test.ts`

**Interfaces:**
- Produces: the npm scripts every later task runs (`typecheck`, `test`,
  `build`, `package`); the manifest ids later tasks rely on — views
  `dispatcherProjects`, `dispatcherErrors`; commands `dispatcher.refresh`,
  `dispatcher.startServer`, `dispatcher.showError`; settings per spec §5.

- [ ] **Step 1: Create the manifest and configs**

`vscode-ext/package.json` (devDependency versions are added by `npm
install` in Step 2, not hand-written):

```json
{
  "name": "dispatcher-monitor",
  "displayName": "Dispatcher Monitor",
  "description": "Ecosystem monitoring sidebar over the dispatcher HTTP API",
  "version": "0.1.0",
  "publisher": "andrei-shtanakov",
  "license": "MIT",
  "private": true,
  "engines": { "vscode": "^1.90.0" },
  "categories": ["Other"],
  "main": "./dist/extension.js",
  "contributes": {
    "viewsContainers": {
      "activitybar": [
        { "id": "dispatcher", "title": "Dispatcher", "icon": "$(pulse)" }
      ]
    },
    "views": {
      "dispatcher": [
        { "id": "dispatcherProjects", "name": "Projects" },
        { "id": "dispatcherErrors", "name": "Errors" }
      ]
    },
    "commands": [
      { "command": "dispatcher.refresh", "title": "Dispatcher: Refresh" },
      { "command": "dispatcher.startServer", "title": "Dispatcher: Start Server" },
      { "command": "dispatcher.showError", "title": "Dispatcher: Show Error Body" }
    ],
    "menus": {
      "view/title": [
        {
          "command": "dispatcher.refresh",
          "when": "view == dispatcherProjects || view == dispatcherErrors",
          "group": "navigation"
        }
      ],
      "commandPalette": [
        { "command": "dispatcher.showError", "when": "false" }
      ]
    },
    "configuration": {
      "title": "Dispatcher",
      "properties": {
        "dispatcher.url": {
          "type": "string",
          "default": "http://127.0.0.1:8787",
          "description": "Base URL of the dispatcher HTTP API."
        },
        "dispatcher.projectDir": {
          "type": "string",
          "default": "",
          "description": "Path to the dispatcher repo, used to spawn `uv run dispatcher serve`. Empty disables auto-start."
        },
        "dispatcher.autoStart": {
          "type": "boolean",
          "default": true,
          "description": "Spawn the server when the URL is unreachable (requires projectDir)."
        },
        "dispatcher.pollSeconds": {
          "type": "number",
          "default": 10,
          "minimum": 5,
          "description": "Refresh interval in seconds."
        }
      }
    }
  },
  "scripts": {
    "typecheck": "tsc --noEmit",
    "test": "vitest run",
    "build": "node esbuild.mjs",
    "package": "npm run typecheck && npm run test && npm run build && vsce package --no-dependencies"
  }
}
```

`vscode-ext/tsconfig.json`:

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "lib": ["ES2022"],
    "strict": true,
    "noEmit": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "types": ["node"]
  },
  "include": ["src", "test"]
}
```

`vscode-ext/esbuild.mjs`:

```js
import esbuild from "esbuild";

await esbuild.build({
  entryPoints: ["src/extension.ts"],
  bundle: true,
  outfile: "dist/extension.js",
  external: ["vscode"],
  format: "cjs",
  platform: "node",
  target: "node20",
  sourcemap: true,
});
```

`vscode-ext/.gitignore`:

```
node_modules/
dist/
*.vsix
```

`vscode-ext/.vscodeignore`:

```
src/
test/
node_modules/
esbuild.mjs
tsconfig.json
.gitignore
*.map
```

`vscode-ext/src/extension.ts` (placeholder, replaced in Task 5):

```ts
import * as vscode from "vscode";

export function activate(_context: vscode.ExtensionContext): void {
  // Wired in a later task.
}

export function deactivate(): void {}
```

- [ ] **Step 2: Install dev dependencies**

Run (from `vscode-ext/`):
`npm install --save-dev typescript @types/node @types/vscode@1.90.0 esbuild vitest @vscode/vsce`
Expected: `package.json` gains pinned devDependencies; `package-lock.json`
created. (`@types/vscode` is pinned to the engines floor on purpose.)

- [ ] **Step 3: Write the scaffold test**

`vscode-ext/test/scaffold.test.ts`:

```ts
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

describe("manifest", () => {
  const manifest = JSON.parse(
    readFileSync(new URL("../package.json", import.meta.url), "utf-8"),
  );

  it("declares both views and all commands", () => {
    const views = manifest.contributes.views.dispatcher.map(
      (v: { id: string }) => v.id,
    );
    expect(views).toEqual(["dispatcherProjects", "dispatcherErrors"]);
    const commands = manifest.contributes.commands.map(
      (c: { command: string }) => c.command,
    );
    expect(commands).toContain("dispatcher.refresh");
    expect(commands).toContain("dispatcher.startServer");
  });

  it("ships spec §5 defaults", () => {
    const props = manifest.contributes.configuration.properties;
    expect(props["dispatcher.url"].default).toBe("http://127.0.0.1:8787");
    expect(props["dispatcher.projectDir"].default).toBe("");
    expect(props["dispatcher.autoStart"].default).toBe(true);
    expect(props["dispatcher.pollSeconds"].default).toBe(10);
    expect(props["dispatcher.pollSeconds"].minimum).toBe(5);
  });
});
```

- [ ] **Step 4: Verify all three scripts pass**

Run (from `vscode-ext/`): `npm run typecheck && npm test && npm run build`
Expected: tsc clean; 2 vitest tests pass; `dist/extension.js` produced.

- [ ] **Step 5: Commit**

```bash
git add vscode-ext
git commit -m "feat(vscode): extension scaffold — manifest, build, test tooling

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: ApiClient and DTOs

**Files:**
- Create: `vscode-ext/src/api.ts`
- Create: `vscode-ext/test/fixtures/overview.json`,
  `vscode-ext/test/fixtures/errors.json`,
  `vscode-ext/test/fixtures/project.json`
- Test: `vscode-ext/test/api.test.ts`

**Interfaces:**
- Produces (later tasks import these exact names from `./api`):
  - `interface Counts { tasks: number; models: number; test_results:
    number; errors: number }`
  - `interface OverviewEntry { name: string; path: string | null;
    detected: boolean; freshness: string | null; counts: Counts;
    warnings: string[] }`
  - `interface OverviewResponse { projects: OverviewEntry[]; warnings:
    string[] }`
  - `interface ErrorEvent { timestamp: string | null; service: string |
    null; severity: string; body: string }`
  - `interface SchemaVersionCheck { database: string; found: string |
    null; expected: string | null; ok: boolean | null }`
  - `interface ProjectDetail { name: string; path: string; detected:
    boolean; freshness: string | null; schema_versions:
    SchemaVersionCheck[]; models: unknown[]; tasks: unknown[];
    test_results: unknown[]; configs: unknown[]; errors: ErrorEvent[];
    warnings: string[] }`
  - `class ApiClient { constructor(baseUrl: string); overview():
    Promise<OverviewResponse>; project(name: string):
    Promise<ProjectDetail>; errors(): Promise<ErrorEvent[]> }`

- [ ] **Step 1: Create fixtures (shapes match the pydantic models)**

`vscode-ext/test/fixtures/overview.json`:

```json
{
  "projects": [
    {
      "name": "arbiter",
      "path": "/labs/arbiter",
      "detected": true,
      "freshness": "2026-07-05T09:00:00",
      "counts": { "tasks": 7, "models": 3, "test_results": 1, "errors": 2 },
      "warnings": ["schema drift: arbiter.db"]
    },
    {
      "name": "Maestro",
      "path": null,
      "detected": false,
      "freshness": null,
      "counts": {},
      "warnings": []
    }
  ],
  "warnings": ["root not found: /missing"]
}
```

`vscode-ext/test/fixtures/errors.json`:

```json
[
  {
    "timestamp": "2026-07-05T12:01:33+00:00",
    "service": "maestro",
    "severity": "ERROR",
    "body": "timeout in pipeline #42"
  },
  {
    "timestamp": null,
    "service": null,
    "severity": "ERROR",
    "body": "undated failure with [markup-looking] text"
  }
]
```

`vscode-ext/test/fixtures/project.json`:

```json
{
  "name": "arbiter",
  "path": "/labs/arbiter",
  "detected": true,
  "collected_at": "2026-07-05T12:00:00+00:00",
  "freshness": "2026-07-05T09:00:00",
  "schema_versions": [
    { "database": "arbiter.db", "found": "1", "expected": "1", "ok": true }
  ],
  "models": [{ "model_id": "gpt-5.5" }],
  "tasks": [{ "task_id": "T-9", "status": "assign" }],
  "test_results": [{ "run_id": "R-1", "name": "code-review" }],
  "configs": [{ "path": "config/agents.toml" }],
  "errors": [],
  "warnings": []
}
```

- [ ] **Step 2: Write the failing tests**

`vscode-ext/test/api.test.ts`:

```ts
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiClient } from "../src/api";

function fixture(name: string): unknown {
  return JSON.parse(
    readFileSync(new URL(`./fixtures/${name}`, import.meta.url), "utf-8"),
  );
}

function okResponse(payload: unknown): Response {
  return new Response(JSON.stringify(payload), { status: 200 });
}

afterEach(() => vi.unstubAllGlobals());

describe("ApiClient", () => {
  it("fetches and parses the overview", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(fixture("overview.json")));
    vi.stubGlobal("fetch", fetchMock);
    const overview = await new ApiClient("http://127.0.0.1:8787").overview();
    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8787/api/overview");
    expect(overview.projects[0].name).toBe("arbiter");
    expect(overview.projects[1].detected).toBe(false);
  });

  it("requests errors with the web-parity query", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(fixture("errors.json")));
    vi.stubGlobal("fetch", fetchMock);
    const events = await new ApiClient("http://x").errors();
    expect(fetchMock.mock.calls[0][0]).toBe("http://x/api/errors?days=14&limit=50");
    expect(events).toHaveLength(2);
  });

  it("URL-encodes project names", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(fixture("project.json")));
    vi.stubGlobal("fetch", fetchMock);
    await new ApiClient("http://x").project("a b");
    expect(fetchMock.mock.calls[0][0]).toBe("http://x/api/projects/a%20b");
  });

  it("throws on non-200", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("nope", { status: 500 })));
    await expect(new ApiClient("http://x").overview()).rejects.toThrow("HTTP 500");
  });

  it("passes an abort signal (timeout)", async () => {
    const fetchMock = vi.fn().mockResolvedValue(okResponse(fixture("overview.json")));
    vi.stubGlobal("fetch", fetchMock);
    await new ApiClient("http://x").overview();
    expect(fetchMock.mock.calls[0][1]?.signal).toBeInstanceOf(AbortSignal);
  });
});
```

- [ ] **Step 3: Run tests to verify they fail**

Run (from `vscode-ext/`): `npm test`
Expected: FAIL — `Cannot find module '../src/api'`.

- [ ] **Step 4: Implement `src/api.ts`**

```ts
/** Typed client for the dispatcher HTTP API. Must stay vscode-free. */

export interface Counts {
  tasks: number;
  models: number;
  test_results: number;
  errors: number;
}

export interface OverviewEntry {
  name: string;
  path: string | null;
  detected: boolean;
  freshness: string | null;
  counts: Counts;
  warnings: string[];
}

export interface OverviewResponse {
  projects: OverviewEntry[];
  warnings: string[];
}

export interface ErrorEvent {
  timestamp: string | null;
  service: string | null;
  severity: string;
  body: string;
}

export interface SchemaVersionCheck {
  database: string;
  found: string | null;
  expected: string | null;
  ok: boolean | null;
}

export interface ProjectDetail {
  name: string;
  path: string;
  detected: boolean;
  freshness: string | null;
  schema_versions: SchemaVersionCheck[];
  models: unknown[];
  tasks: unknown[];
  test_results: unknown[];
  configs: unknown[];
  errors: ErrorEvent[];
  warnings: string[];
}

const TIMEOUT_MS = 3000;

export class ApiClient {
  constructor(private readonly baseUrl: string) {}

  private async get<T>(path: string): Promise<T> {
    const resp = await fetch(`${this.baseUrl}${path}`, {
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    if (!resp.ok) {
      throw new Error(`GET ${path}: HTTP ${resp.status}`);
    }
    return (await resp.json()) as T;
  }

  overview(): Promise<OverviewResponse> {
    return this.get("/api/overview");
  }

  project(name: string): Promise<ProjectDetail> {
    return this.get(`/api/projects/${encodeURIComponent(name)}`);
  }

  errors(): Promise<ErrorEvent[]> {
    return this.get("/api/errors?days=14&limit=50");
  }
}
```

Note: `OverviewEntry.counts` for undetected projects arrives as `{}` from
the API; treat count reads as possibly missing (`counts.errors ?? 0`) in
consumers — Task 3 does this.

- [ ] **Step 5: Run tests to verify they pass**

Run: `npm test` — Expected: ALL PASS. Then `npm run typecheck` — clean.

- [ ] **Step 6: Commit**

```bash
git add vscode-ext/src/api.ts vscode-ext/test
git commit -m "feat(vscode): typed ApiClient with timeout and fixtures

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Pure view-model and decision logic

**Files:**
- Create: `vscode-ext/src/model.ts`
- Test: `vscode-ext/test/model.test.ts`

**Interfaces:**
- Consumes: DTO types from `./api` (types only).
- Produces (exact names later tasks import from `./model`):
  - `MSG_LIMIT = 160`
  - `type Health = "ok" | "err" | "off"`
  - `interface ProjectView { name: string; description: string; health:
    Health; detected: boolean }`
  - `humanizeAgo(iso: string | null, now: Date): string`
  - `projectView(entry: OverviewEntry, now: Date): ProjectView`
  - `detailLines(detail: ProjectDetail): string[]`
  - `truncate(body: string, limit?: number): string`
  - `errorLabel(event: ErrorEvent): string`
  - `statusText(overview: OverviewResponse | null): string`
  - `portFromUrl(url: string): number`
  - `shouldSpawn(opts: { reachable: boolean; autoStart: boolean;
    projectDir: string; alreadyTried: boolean }): boolean`

- [ ] **Step 1: Write the failing tests**

`vscode-ext/test/model.test.ts`:

```ts
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { ErrorEvent, OverviewResponse, ProjectDetail } from "../src/api";
import {
  detailLines,
  errorLabel,
  humanizeAgo,
  portFromUrl,
  projectView,
  shouldSpawn,
  statusText,
  truncate,
} from "../src/model";

function fixture<T>(name: string): T {
  return JSON.parse(
    readFileSync(new URL(`./fixtures/${name}`, import.meta.url), "utf-8"),
  ) as T;
}

const overview = fixture<OverviewResponse>("overview.json");
const NOW = new Date("2026-07-05T12:00:00Z");

describe("humanizeAgo", () => {
  it("formats minutes, hours, days", () => {
    expect(humanizeAgo("2026-07-05T11:57:00Z", NOW)).toBe("3m ago");
    expect(humanizeAgo("2026-07-05T09:00:00Z", NOW)).toBe("3h ago");
    expect(humanizeAgo("2026-07-01T12:00:00Z", NOW)).toBe("4d ago");
    expect(humanizeAgo(null, NOW)).toBe("fresh?");
  });
});

describe("projectView", () => {
  it("maps a detected project with errors to health=err", () => {
    const view = projectView(overview.projects[0], NOW);
    expect(view.health).toBe("err");
    expect(view.description).toContain("7t");
    expect(view.description).toContain("2e");
    expect(view.detected).toBe(true);
  });

  it("maps an undetected project to health=off", () => {
    const view = projectView(overview.projects[1], NOW);
    expect(view).toEqual({
      name: "Maestro",
      description: "not detected",
      health: "off",
      detected: false,
    });
  });
});

describe("detailLines", () => {
  it("summarizes counts, schema checks, warnings", () => {
    const detail = fixture<ProjectDetail>("project.json");
    const lines = detailLines(detail);
    expect(lines[0]).toBe("tasks: 1 · tests: 1 · models: 1 · configs: 1");
    expect(lines).toContain("schema arbiter.db: ok");
  });

  it("marks drift and unknown schema states", () => {
    const detail = fixture<ProjectDetail>("project.json");
    detail.schema_versions = [
      { database: "a.db", found: "2", expected: "1", ok: false },
      { database: "b.db", found: null, expected: "1", ok: null },
    ];
    detail.warnings = ["boom"];
    const lines = detailLines(detail);
    expect(lines).toContain("schema a.db: DRIFT");
    expect(lines).toContain("schema b.db: unknown");
    expect(lines).toContain("⚠ boom");
  });
});

describe("errors", () => {
  it("truncates at the web-parity limit", () => {
    expect(truncate("x".repeat(160))).toBe("x".repeat(160));
    expect(truncate("x".repeat(161))).toBe("x".repeat(160) + "…");
  });

  it("labels dated and undated events", () => {
    const [dated, undated] = fixture<ErrorEvent[]>("errors.json");
    expect(errorLabel(dated)).toBe("12:01 maestro — timeout in pipeline #42");
    expect(errorLabel(undated)).toMatch(/^— — /);
  });
});

describe("statusText", () => {
  it("counts detected projects and projects with errors", () => {
    expect(statusText(overview)).toBe("$(pulse) disp: 1✓ 1✗");
  });

  it("shows offline when there is no data", () => {
    expect(statusText(null)).toBe("$(debug-disconnected) disp: offline");
  });
});

describe("server decisions", () => {
  it("extracts the port", () => {
    expect(portFromUrl("http://127.0.0.1:8787")).toBe(8787);
    expect(portFromUrl("http://localhost")).toBe(80);
  });

  it("spawns only when unreachable+autoStart+projectDir+first try", () => {
    const base = {
      reachable: false,
      autoStart: true,
      projectDir: "/x",
      alreadyTried: false,
    };
    expect(shouldSpawn(base)).toBe(true);
    expect(shouldSpawn({ ...base, reachable: true })).toBe(false);
    expect(shouldSpawn({ ...base, autoStart: false })).toBe(false);
    expect(shouldSpawn({ ...base, projectDir: "  " })).toBe(false);
    expect(shouldSpawn({ ...base, alreadyTried: true })).toBe(false);
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npm test` — Expected: FAIL, `Cannot find module '../src/model'`.

- [ ] **Step 3: Implement `src/model.ts`**

```ts
/** Pure view-model mappers and decisions. Must stay vscode-free. */

import type {
  ErrorEvent,
  OverviewEntry,
  OverviewResponse,
  ProjectDetail,
} from "./api";

export const MSG_LIMIT = 160; // same truncation as web and TUI

export type Health = "ok" | "err" | "off";

export interface ProjectView {
  name: string;
  description: string;
  health: Health;
  detected: boolean;
}

export function humanizeAgo(iso: string | null, now: Date): string {
  if (iso === null) {
    return "fresh?";
  }
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) {
    return "fresh?";
  }
  const minutes = Math.max(0, Math.floor((now.getTime() - then) / 60_000));
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  if (minutes < 60 * 24) {
    return `${Math.floor(minutes / 60)}h ago`;
  }
  return `${Math.floor(minutes / (60 * 24))}d ago`;
}

export function projectView(entry: OverviewEntry, now: Date): ProjectView {
  if (!entry.detected) {
    return {
      name: entry.name,
      description: "not detected",
      health: "off",
      detected: false,
    };
  }
  const tasks = entry.counts.tasks ?? 0;
  const errors = entry.counts.errors ?? 0;
  return {
    name: entry.name,
    description: `${tasks}t · ${errors}e · ${humanizeAgo(entry.freshness, now)}`,
    health: errors > 0 ? "err" : "ok",
    detected: true,
  };
}

export function detailLines(detail: ProjectDetail): string[] {
  const lines = [
    `tasks: ${detail.tasks.length} · tests: ${detail.test_results.length}` +
      ` · models: ${detail.models.length} · configs: ${detail.configs.length}`,
  ];
  for (const check of detail.schema_versions) {
    const state =
      check.ok === true ? "ok" : check.ok === false ? "DRIFT" : "unknown";
    lines.push(`schema ${check.database}: ${state}`);
  }
  for (const warning of detail.warnings) {
    lines.push(`⚠ ${warning}`);
  }
  return lines;
}

export function truncate(body: string, limit: number = MSG_LIMIT): string {
  return body.length <= limit ? body : body.slice(0, limit) + "…";
}

export function errorLabel(event: ErrorEvent): string {
  const time = event.timestamp === null ? "—" : event.timestamp.slice(11, 16);
  return `${time} ${event.service ?? "—"} — ${truncate(event.body, 80)}`;
}

export function statusText(overview: OverviewResponse | null): string {
  if (overview === null) {
    return "$(debug-disconnected) disp: offline";
  }
  const detected = overview.projects.filter((p) => p.detected);
  const withErrors = detected.filter((p) => (p.counts.errors ?? 0) > 0);
  return `$(pulse) disp: ${detected.length}✓ ${withErrors.length}✗`;
}

export function portFromUrl(url: string): number {
  const parsed = new URL(url);
  return parsed.port === "" ? 80 : Number(parsed.port);
}

export function shouldSpawn(opts: {
  reachable: boolean;
  autoStart: boolean;
  projectDir: string;
  alreadyTried: boolean;
}): boolean {
  return (
    !opts.reachable &&
    opts.autoStart &&
    opts.projectDir.trim() !== "" &&
    !opts.alreadyTried
  );
}
```

Note on `Counts` reads: the DTO declares numbers, but undetected entries
arrive with `{}` — hence `?? 0`. Make the DTO honest in `api.ts` by
changing `counts: Counts` to `counts: Partial<Counts>` in this task, and
adjust nothing else (the tests above already cover both shapes).

- [ ] **Step 4: Run tests to verify they pass**

Run: `npm test` — ALL PASS; `npm run typecheck` — clean (this catches the
`Partial<Counts>` adjustment).

- [ ] **Step 5: Commit**

```bash
git add vscode-ext/src/model.ts vscode-ext/src/api.ts vscode-ext/test/model.test.ts
git commit -m "feat(vscode): pure view-model mappers and spawn decisions

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: ServerManager

**Files:**
- Create: `vscode-ext/src/server.ts`
- Test: `vscode-ext/test/server.test.ts`

**Interfaces:**
- Consumes: `portFromUrl`, `shouldSpawn` from `./model`.
- Produces (imported by Task 5):
  - `interface ServerManagerOptions { url: string; projectDir: string;
    autoStart: boolean; probe: () => Promise<boolean>; notify: (message:
    string) => void; sleep?: (ms: number) => Promise<void>; spawnFn?:
    typeof import("node:child_process").spawn }`
  - `class ServerManager { constructor(opts: ServerManagerOptions);
    ensureRunning(): Promise<void>; start(): void; markOnline(): void;
    dispose(): void }`

- [ ] **Step 1: Write the failing tests**

`vscode-ext/test/server.test.ts`:

```ts
import { EventEmitter } from "node:events";
import { describe, expect, it, vi } from "vitest";
import { ServerManager, type ServerManagerOptions } from "../src/server";

class FakeChild extends EventEmitter {
  stderr = new EventEmitter();
  killed = false;
  kill(): boolean {
    this.killed = true;
    return true;
  }
}

function manager(overrides: Partial<ServerManagerOptions> = {}) {
  const child = new FakeChild();
  const spawnFn = vi.fn().mockReturnValue(child);
  const notify = vi.fn();
  const opts: ServerManagerOptions = {
    url: "http://127.0.0.1:8787",
    projectDir: "/repo",
    autoStart: true,
    probe: vi.fn().mockResolvedValue(false),
    notify,
    sleep: () => Promise.resolve(),
    spawnFn: spawnFn as unknown as ServerManagerOptions["spawnFn"],
    ...overrides,
  };
  return { mgr: new ServerManager(opts), spawnFn, notify, child, opts };
}

describe("ServerManager", () => {
  it("does not spawn when the server is reachable", async () => {
    const { mgr, spawnFn } = manager({ probe: vi.fn().mockResolvedValue(true) });
    await mgr.ensureRunning();
    expect(spawnFn).not.toHaveBeenCalled();
  });

  it("spawns uv run dispatcher serve with the URL port", async () => {
    const { mgr, spawnFn } = manager();
    await mgr.ensureRunning();
    expect(spawnFn).toHaveBeenCalledWith(
      "uv",
      ["run", "dispatcher", "serve", "--port", "8787"],
      expect.objectContaining({ cwd: "/repo" }),
    );
  });

  it("spawns at most once per offline episode", async () => {
    const { mgr, spawnFn } = manager();
    await mgr.ensureRunning();
    await mgr.ensureRunning();
    expect(spawnFn).toHaveBeenCalledTimes(1);
    mgr.markOnline(); // a successful poll ends the episode
    await mgr.ensureRunning();
    expect(spawnFn).toHaveBeenCalledTimes(2);
  });

  it("does not spawn when autoStart is off or projectDir empty", async () => {
    const a = manager({ autoStart: false });
    await a.mgr.ensureRunning();
    expect(a.spawnFn).not.toHaveBeenCalled();
    const b = manager({ projectDir: "" });
    await b.mgr.ensureRunning();
    expect(b.spawnFn).not.toHaveBeenCalled();
  });

  it("notifies with stderr tail on nonzero exit", async () => {
    const { mgr, notify, child } = manager();
    await mgr.ensureRunning();
    child.stderr.emit("data", Buffer.from("uvicorn exploded"));
    child.emit("exit", 1);
    expect(notify).toHaveBeenCalledWith(
      expect.stringContaining("uvicorn exploded"),
    );
  });

  it("notifies when spawn itself fails", async () => {
    const { mgr, notify, child } = manager();
    await mgr.ensureRunning();
    child.emit("error", new Error("uv not found"));
    expect(notify).toHaveBeenCalledWith(expect.stringContaining("uv not found"));
  });

  it("dispose kills only its own child", async () => {
    const { mgr, child } = manager();
    await mgr.ensureRunning();
    mgr.dispose();
    expect(child.killed).toBe(true);
    const fresh = manager();
    fresh.mgr.dispose(); // never spawned — nothing to kill, no throw
  });
});
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `npm test` — Expected: FAIL, `Cannot find module '../src/server'`.

- [ ] **Step 3: Implement `src/server.ts`**

```ts
/** Dispatcher server lifecycle: probe, spawn, kill. Must stay vscode-free. */

import { spawn, type ChildProcess } from "node:child_process";
import { portFromUrl, shouldSpawn } from "./model";

export interface ServerManagerOptions {
  url: string;
  projectDir: string;
  autoStart: boolean;
  probe: () => Promise<boolean>;
  notify: (message: string) => void;
  sleep?: (ms: number) => Promise<void>;
  spawnFn?: typeof spawn;
}

const READY_TRIES = 20;
const READY_DELAY_MS = 500;
const STDERR_TAIL = 500;

export class ServerManager {
  private child: ChildProcess | null = null;
  private triedThisEpisode = false;

  constructor(private readonly opts: ServerManagerOptions) {}

  /** A successful poll ends the offline episode (spec §4). */
  markOnline(): void {
    this.triedThisEpisode = false;
  }

  async ensureRunning(): Promise<void> {
    const reachable = await this.opts.probe();
    if (reachable) {
      this.triedThisEpisode = false;
      return;
    }
    const spawnIt = shouldSpawn({
      reachable,
      autoStart: this.opts.autoStart,
      projectDir: this.opts.projectDir,
      alreadyTried: this.triedThisEpisode,
    });
    if (!spawnIt) {
      return;
    }
    this.triedThisEpisode = true;
    this.start();
    await this.waitUntilReady();
  }

  /** Also invoked directly by the "Start Server" command. */
  start(): void {
    if (this.child !== null) {
      return;
    }
    const spawnFn = this.opts.spawnFn ?? spawn;
    const port = portFromUrl(this.opts.url);
    const stderr: string[] = [];
    this.child = spawnFn(
      "uv",
      ["run", "dispatcher", "serve", "--port", String(port)],
      { cwd: this.opts.projectDir, stdio: ["ignore", "ignore", "pipe"] },
    );
    this.child.stderr?.on("data", (chunk: Buffer) => {
      stderr.push(chunk.toString());
      if (stderr.length > 20) {
        stderr.shift();
      }
    });
    this.child.on("exit", (code) => {
      if (code !== 0 && code !== null) {
        this.opts.notify(
          `dispatcher serve exited (${code}): ` +
            stderr.join("").slice(-STDERR_TAIL),
        );
      }
      this.child = null;
    });
    this.child.on("error", (err: Error) => {
      this.opts.notify(`failed to spawn dispatcher: ${err.message}`);
      this.child = null;
    });
  }

  private async waitUntilReady(): Promise<void> {
    const sleep =
      this.opts.sleep ??
      ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
    for (let i = 0; i < READY_TRIES; i++) {
      await sleep(READY_DELAY_MS);
      if (await this.opts.probe()) {
        return;
      }
    }
  }

  dispose(): void {
    this.child?.kill();
    this.child = null;
  }
}
```

Note: `FakeChild` in the tests is structurally compatible; if tsc
complains about the `spawnFn` mock type, keep the
`as unknown as ServerManagerOptions["spawnFn"]` cast in the test, not in
production code.

- [ ] **Step 4: Run tests to verify they pass**

Run: `npm test` — ALL PASS; `npm run typecheck` — clean.

- [ ] **Step 5: Commit**

```bash
git add vscode-ext/src/server.ts vscode-ext/test/server.test.ts
git commit -m "feat(vscode): ServerManager — probe, one spawn per offline episode, kill own child

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Trees, status bar, extension wiring

**Files:**
- Create: `vscode-ext/src/tree.ts`, `vscode-ext/src/status.ts`
- Modify: `vscode-ext/src/extension.ts` (replace placeholder)

**Interfaces:**
- Consumes: everything from Tasks 2–4 (exact names listed there).
- Produces: the running extension. No later task imports from these files.

- [ ] **Step 1: Implement `src/tree.ts`**

```ts
/** TreeDataProviders: thin adapters over the pure model mappers. */

import * as vscode from "vscode";
import type { ApiClient, ErrorEvent, OverviewEntry } from "./api";
import { detailLines, errorLabel, projectView } from "./model";

export type ProjectNode =
  | { kind: "project"; entry: OverviewEntry }
  | { kind: "line"; text: string }
  | { kind: "offline" };

function offlineItem(): vscode.TreeItem {
  const item = new vscode.TreeItem("server unreachable");
  item.iconPath = new vscode.ThemeIcon("debug-disconnected");
  item.command = {
    command: "dispatcher.startServer",
    title: "Start server",
  };
  return item;
}

export class ProjectsProvider
  implements vscode.TreeDataProvider<ProjectNode>
{
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private entries: OverviewEntry[] | null = null; // null = offline

  constructor(private readonly api: () => ApiClient) {}

  setData(entries: OverviewEntry[] | null): void {
    this.entries = entries;
    this.changed.fire();
  }

  getTreeItem(node: ProjectNode): vscode.TreeItem {
    if (node.kind === "offline") {
      return offlineItem();
    }
    if (node.kind === "line") {
      return new vscode.TreeItem(node.text);
    }
    const view = projectView(node.entry, new Date());
    const item = new vscode.TreeItem(
      view.name,
      view.detected
        ? vscode.TreeItemCollapsibleState.Collapsed
        : vscode.TreeItemCollapsibleState.None,
    );
    item.description = view.description;
    item.iconPath =
      view.health === "ok"
        ? new vscode.ThemeIcon(
            "circle-filled",
            new vscode.ThemeColor("testing.iconPassed"),
          )
        : view.health === "err"
          ? new vscode.ThemeIcon(
              "circle-filled",
              new vscode.ThemeColor("testing.iconFailed"),
            )
          : new vscode.ThemeIcon("circle-outline");
    return item;
  }

  async getChildren(node?: ProjectNode): Promise<ProjectNode[]> {
    if (node === undefined) {
      if (this.entries === null) {
        return [{ kind: "offline" }];
      }
      return this.entries.map((entry) => ({ kind: "project", entry }));
    }
    if (node.kind !== "project" || !node.entry.detected) {
      return [];
    }
    const detail = await this.api().project(node.entry.name);
    return detailLines(detail).map((text) => ({ kind: "line", text }));
  }
}

export type ErrorNode =
  | { kind: "error"; event: ErrorEvent }
  | { kind: "empty" }
  | { kind: "offline" };

export class ErrorsProvider implements vscode.TreeDataProvider<ErrorNode> {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private events: ErrorEvent[] | null = null; // null = offline

  setData(events: ErrorEvent[] | null): void {
    this.events = events;
    this.changed.fire();
  }

  getTreeItem(node: ErrorNode): vscode.TreeItem {
    if (node.kind === "offline") {
      return offlineItem();
    }
    if (node.kind === "empty") {
      const item = new vscode.TreeItem("no errors 🎉");
      item.iconPath = new vscode.ThemeIcon(
        "check",
        new vscode.ThemeColor("testing.iconPassed"),
      );
      return item;
    }
    const item = new vscode.TreeItem(errorLabel(node.event));
    item.tooltip = node.event.body;
    item.iconPath = new vscode.ThemeIcon(
      "error",
      new vscode.ThemeColor("testing.iconFailed"),
    );
    item.command = {
      command: "dispatcher.showError",
      title: "Show error body",
      arguments: [node.event.body],
    };
    return item;
  }

  getChildren(node?: ErrorNode): ErrorNode[] {
    if (node !== undefined) {
      return [];
    }
    if (this.events === null) {
      return [{ kind: "offline" }];
    }
    if (this.events.length === 0) {
      return [{ kind: "empty" }];
    }
    return this.events.map((event) => ({ kind: "error", event }));
  }
}
```

- [ ] **Step 2: Implement `src/status.ts`**

```ts
/** Status-bar item: thin adapter over model.statusText. */

import * as vscode from "vscode";
import type { OverviewResponse } from "./api";
import { statusText } from "./model";

export function createStatusBar(): {
  item: vscode.StatusBarItem;
  update: (overview: OverviewResponse | null) => void;
} {
  const item = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left,
    100,
  );
  item.name = "Dispatcher";
  item.command = "dispatcherProjects.focus"; // auto-generated view command
  item.show();
  return {
    item,
    update: (overview) => {
      item.text = statusText(overview);
      item.tooltip =
        overview === null
          ? "dispatcher: server unreachable"
          : "dispatcher: detected✓ with-errors✗";
    },
  };
}
```

- [ ] **Step 3: Replace `src/extension.ts`**

```ts
/** Extension entry point: config, poller, commands, wiring. */

import * as vscode from "vscode";
import { ApiClient } from "./api";
import { ServerManager } from "./server";
import { createStatusBar } from "./status";
import { ErrorsProvider, ProjectsProvider } from "./tree";

interface Config {
  url: string;
  projectDir: string;
  autoStart: boolean;
  pollSeconds: number;
}

function readConfig(): Config {
  const cfg = vscode.workspace.getConfiguration("dispatcher");
  return {
    url: cfg.get<string>("url", "http://127.0.0.1:8787"),
    projectDir: cfg.get<string>("projectDir", ""),
    autoStart: cfg.get<boolean>("autoStart", true),
    pollSeconds: Math.max(5, cfg.get<number>("pollSeconds", 10)),
  };
}

export function activate(context: vscode.ExtensionContext): void {
  const client = (): ApiClient => new ApiClient(readConfig().url);

  const server = new ServerManager({
    get url() {
      return readConfig().url;
    },
    get projectDir() {
      return readConfig().projectDir;
    },
    get autoStart() {
      return readConfig().autoStart;
    },
    probe: async () => {
      try {
        await client().overview();
        return true;
      } catch {
        return false;
      }
    },
    notify: (message) => {
      void vscode.window.showErrorMessage(message);
    },
  });

  const projects = new ProjectsProvider(client);
  const errors = new ErrorsProvider();
  const status = createStatusBar();

  async function poll(): Promise<void> {
    try {
      const api = client();
      const [overview, events] = await Promise.all([
        api.overview(),
        api.errors(),
      ]);
      projects.setData(overview.projects);
      errors.setData(events);
      status.update(overview);
      server.markOnline();
    } catch {
      projects.setData(null);
      errors.setData(null);
      status.update(null);
      await server.ensureRunning();
    }
  }

  const timer = setInterval(() => void poll(), readConfig().pollSeconds * 1000);

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("dispatcherProjects", projects),
    vscode.window.registerTreeDataProvider("dispatcherErrors", errors),
    status.item,
    vscode.commands.registerCommand("dispatcher.refresh", () => void poll()),
    vscode.commands.registerCommand("dispatcher.startServer", () => {
      server.start();
      void poll();
    }),
    vscode.commands.registerCommand(
      "dispatcher.showError",
      async (body: string) => {
        const doc = await vscode.workspace.openTextDocument({
          content: body,
          language: "log",
        });
        await vscode.window.showTextDocument(doc, { preview: true });
      },
    ),
    { dispose: () => clearInterval(timer) },
    { dispose: () => server.dispose() },
  );

  void poll();
}

export function deactivate(): void {}
```

Note on `dispatcher.showError`: the spec says "read-only document"; an
untitled preview document is the v1 interpretation (a true readonly
provider needs a `TextDocumentContentProvider` — YAGNI for a body you
might want to copy from anyway). This is a conscious simplification.

Note the `get url()` accessors in the ServerManager options: settings are
re-read on every poll/spawn, so config changes apply without a reload.
The poll interval itself is read once at activation — changing
`pollSeconds` needs a window reload; that is acceptable for v1 and matches
the spec's silence on live interval changes.

- [ ] **Step 4: Verify**

Run (from `vscode-ext/`): `npm run typecheck && npm test && npm run build`
Expected: tsc clean (this is the real gate for these three files); all
prior vitest suites still pass; bundle builds.

- [ ] **Step 5: Commit**

```bash
git add vscode-ext/src
git commit -m "feat(vscode): trees, status bar, poller and command wiring

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: CI node job and docs

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `README.md`, `COWORK_CONTEXT.md`

- [ ] **Step 1: Add the node job**

Append to `jobs:` in `.github/workflows/ci.yml` (same style as existing
jobs):

```yaml
  vscode-ext:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: vscode-ext
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-node@v4
        with:
          node-version: "22"
          cache: npm
          cache-dependency-path: vscode-ext/package-lock.json
      - run: npm ci
      - run: npm run typecheck
      - run: npm test
      - run: npm run build
```

- [ ] **Step 2: Update README.md**

Next to the Terminal UI section, add:

```markdown
### VSCode extension

    cd vscode-ext && npm install && npm run package   # builds .vsix

Install via "Extensions: Install from VSIX…". Adds a Dispatcher sidebar
(projects + recent errors) and a status-bar health indicator; the server
is auto-started when unreachable (`dispatcher.projectDir` setting must
point at this repo). Settings: `dispatcher.url`, `dispatcher.projectDir`,
`dispatcher.autoStart`, `dispatcher.pollSeconds`.
```

- [ ] **Step 3: Update COWORK_CONTEXT.md (in Russian, matching style)**

- `## Стек`: add
  `- **VSCode**: расширение vscode-ext/ (TypeScript, сайдбар + статус-бар),
  потребляет HTTP API`
- `## Roadmap`: change Stage 3 line to
  `**Stage 3 (done, 2026-07-05)**: VSCode-плагин (vscode-ext/) поверх HTTP API.`
- `## Документы`: add the Stage 3 spec and plan paths.

- [ ] **Step 4: Verify**

Run: `uv run pytest -q` (python suite untouched, stays green) and
`cd vscode-ext && npm run typecheck && npm test && npm run build`.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/ci.yml README.md COWORK_CONTEXT.md
git commit -m "ci+docs: vscode-ext node job, stage 3 usage and roadmap

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Package VSIX and smoke checklist

**Files:**
- No new tracked files (the `.vsix` artifact is gitignored).

- [ ] **Step 1: Build the VSIX**

Run (from `vscode-ext/`): `npm run package`
Expected: typecheck + tests + build all green, then
`dispatcher-monitor-0.1.0.vsix` appears. If `vsce` complains about a
missing repository field, add
`"repository": { "type": "git", "url": "https://github.com/andrei-shtanakov/dispatcher" }`
to `vscode-ext/package.json` and rerun.

- [ ] **Step 2: Headless sanity of the bundle**

Run: `node -e "const m=require('./dist/extension.js'); if (typeof m.activate!=='function'||typeof m.deactivate!=='function') process.exit(1)"`
Expected: exits 0 — the bundle exports activate/deactivate and its only
unresolved require is `vscode`.

- [ ] **Step 3: Manual smoke checklist (user-run, in VSCode)**

Record in the task report which rows the user should verify — the
implementer cannot drive the VSCode UI:

1. Install from VSIX → Dispatcher icon appears in the activity bar.
2. With the server running: Projects tree lists projects with icons and
   `Nt · Ne · Xh ago` descriptions; undetected projects dim, not
   expandable; expanding a project shows counts/schema/warnings lines.
3. Errors tree lists recent errors; click opens the full body in an
   editor tab; empty state shows `no errors 🎉`.
4. Status bar shows `$(pulse) disp: N✓ M✗`; click focuses the sidebar.
5. Stop the server: within one poll the trees show `server unreachable`
   and the status bar shows offline; with `projectDir` set the server
   auto-starts (one attempt) and data returns.
6. Reload/quit VSCode: a server spawned by the extension is killed.

- [ ] **Step 4: Commit (only if Step 1 required a manifest fix)**

```bash
git add vscode-ext/package.json
git commit -m "fix(vscode): add repository field for vsce packaging

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
