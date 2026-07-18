# VSCode Onboarding Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Onboarding-экран FR-04 в VSCode-расширении: команда → markdown-preview одного проекта (третий тонкий рендерер серверной OnboardingView).

**Architecture:** Чистый маппер `renderOnboardingMarkdown` + `ApiClient.getOnboarding` (Task 1), затем команда/QuickPick/контекст-меню/`TextDocumentContentProvider` со схемой `dispatcher-onboarding:` (Task 2). Python-сторона не меняется. Спека: `docs/superpowers/specs/2026-07-18-vscode-onboarding-design.md` (DESIGN-1101..1103).

**Tech Stack:** TypeScript (vscode-ext), vitest, esbuild.

## Global Constraints

- Гейты после КАЖДОГО таска: `cd vscode-ext && npm run typecheck && npm test && npm run build`; в конце ветки один прогон `uv run pytest -q` из корня (317 passed + 1 skipped — Python не тронут).
- §1 спеки — закон: маппер ФОРМАТИРУЕТ и только; ни одного вычисления вердикта/сортировки/агрегата; порядок `next_items` воспроизводится как пришёл (пин тестом).
- `mdEscape` — точный 4-шаговый алгоритм спеки, порядок значим: (1) `\`→`\\`; (2) `&`→`&amp;`, `<`→`&lt;`, `>`→`&gt;`; (3) backslash перед `` ` `` `*` `_` `[` `]` `(` `)` `#` `|`; (4) `\r`/`\n` → пробел. Применяется к КАЖДОМУ строковому полю модели.
- Runtime-контракт: `project.name` — hard-required (нет/не строка → toast «malformed onboarding response», документ не открывается); ВСЁ остальное optional с фолбэком `—`/пропуском секции — маппер тотален, `undefined` в выводе невозможен.
- Рендер через `TextDocumentContentProvider` схемы `dispatcher-onboarding:` (НЕ untitled); повторный вызов обновляет тот же документ через `onDidChange`.
- QuickPick — от свежего `overview()` с фильтром `detected`, не от состояния провайдера.
- Ветка: `feat/vscode-onboarding` (план — первый коммит, без plan-PR). Прямые коммиты в master запрещены.

---

### Task 1: mapper + API (DESIGN-1101 + api-часть DESIGN-1102)

**Files:**
- Create: `vscode-ext/src/onboarding.ts`
- Modify: `vscode-ext/src/api.ts` (метод в `ApiClient`; импорт типа)
- Test: `vscode-ext/test/onboarding.test.ts` (create), `vscode-ext/test/api.test.ts` (добавить кейс)

**Interfaces:**
- Consumes: `ApiClient`-паттерн `private get<T>(path)` (api.ts) — `getOnboarding` строится на нём; `ApiError {status, detail}`.
- Produces: типы `OnboardingView` (+вложенные), `mdEscape(value: string): string`, `renderOnboardingMarkdown(view: OnboardingView): string` (из `onboarding.ts`); `ApiClient.getOnboarding(name: string): Promise<OnboardingView>`. Task 2 зависит от всех трёх.

- [ ] **Step 1: Write the failing tests**

`vscode-ext/test/onboarding.test.ts`:

```typescript
import { describe, expect, it } from "vitest";
import {
  mdEscape,
  renderOnboardingMarkdown,
  type OnboardingView,
} from "../src/onboarding";

function base(): OnboardingView {
  return {
    project: {
      name: "arbiter",
      path: "/w/arbiter",
      description: "Routes agents.",
      description_source: "readme",
      freshness: "2026-07-18T10:00:00",
    },
    roadmap_position: {
      summary: {
        readiness: 0.5,
        done: 1,
        total: 2,
        lagging: true,
        contract_drift: false,
      },
      median_readiness: 0.75,
      phases: [{ phase: "1", counts: { planned: 1, verified: 1 } }],
    },
    next_items: [
      {
        id: "RD-2",
        title: "Blocked one",
        phase: "2",
        computed_status: "blocked",
        actionable: false,
        blocked_by: ["RD-9"],
      },
      {
        id: "RD-1",
        title: "Do it",
        phase: "1",
        computed_status: "planned",
        actionable: true,
        blocked_by: [],
      },
    ],
    live_tasks: [{ task_id: "T-1", status: "pending", title: "Live" }],
    warnings: ["unknown dependency id: RD-9 (item RD-2)"],
  };
}

describe("renderOnboardingMarkdown", () => {
  it("renders every section with both verdicts", () => {
    const md = renderOnboardingMarkdown(base());
    expect(md).toContain("# arbiter");
    expect(md).toContain("Routes agents.");
    expect(md).toContain("readiness 50% (1/2) · median 75% · **LAGGING**");
    expect(md).toContain("phase 1: planned=1, verified=1");
    expect(md).toContain("⛔ RD-2 · Blocked one · blocked — blocked by: RD-9");
    expect(md).toContain("▶ RD-1 · Do it · planned");
    expect(md).toContain("T-1 · pending · Live");
    expect(md).toContain("unknown dependency id");
  });

  it("REPRODUCES server order — never re-sorts (spec §1)", () => {
    const md = renderOnboardingMarkdown(base());
    // fixture deliberately puts the blocked item FIRST
    expect(md.indexOf("RD-2")).toBeLessThan(md.indexOf("RD-1"));
  });

  it("degrades: position null, empty lists, no description", () => {
    const md = renderOnboardingMarkdown({
      project: { name: "bare" },
      roadmap_position: null,
      next_items: [],
      live_tasks: [],
      warnings: [],
    });
    expect(md).toContain("# bare");
    expect(md).toContain("no roadmap items");
    expect(md).toContain("(none)");
    expect(md).not.toContain("undefined");
  });

  describe("missing-field cross matrix (mapper is total)", () => {
    const cases: Array<[string, OnboardingView]> = [
      ["item without computed_status but with blocked_by",
        { project: { name: "p" },
          next_items: [{ id: "X", blocked_by: ["Y"] }] }],
      ["item without title or id", { project: { name: "p" }, next_items: [{}] }],
      ["position without median or phases",
        { project: { name: "p" },
          roadmap_position: { summary: { readiness: 0.1 } } }],
      ["position summary missing entirely",
        { project: { name: "p" }, roadmap_position: {} }],
      ["live task without title",
        { project: { name: "p" },
          live_tasks: [{ task_id: "T", status: "pending" }] }],
      ["all optionals absent", { project: { name: "p" } }],
    ];
    for (const [label, view] of cases) {
      it(label, () => {
        const md = renderOnboardingMarkdown(view);
        expect(md).toContain("# p");
        expect(md).not.toContain("undefined");
        expect(md).not.toContain("null");
      });
    }
  });

  describe("escape matrix (markdown structure AND inline HTML)", () => {
    it("neutralizes an HTML injection vector", () => {
      const md = renderOnboardingMarkdown({
        project: { name: "p", description: '<img src=x onerror="alert(1)">' },
      });
      expect(md).not.toContain("<img");
      expect(md).toContain("&lt;img");
    });
    it("escapes markdown controls", () => {
      expect(mdEscape("a*b_c`d#e")).toBe("a\\*b\\_c\\`d\\#e");
    });
    it("escapes pipes (table safety)", () => {
      expect(mdEscape("a|b")).toBe("a\\|b");
    });
    it("neutralizes a link-breaker", () => {
      expect(mdEscape("x](http://evil)")).toBe("x\\]\\(http://evil\\)");
    });
    it("collapses newlines (list-item safety)", () => {
      expect(mdEscape("line1\nline2\r\nline3")).toBe("line1 line2 line3");
    });
    it("doubles backslashes BEFORE adding escapes", () => {
      expect(mdEscape("a\\*")).toBe("a\\\\\\*");
    });
    it("html-encodes ampersand and angle brackets", () => {
      expect(mdEscape("a&b<c>d")).toBe("a&amp;b&lt;c&gt;d");
    });
  });
});
```

В `vscode-ext/test/api.test.ts` добавить (стиль файла — `okResponse`/`jsonResponse` уже есть):

```typescript
  it("URL-encodes the onboarding project name and parses the view", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(okResponse({ project: { name: "a b" } }));
    vi.stubGlobal("fetch", fetchMock);
    const view = await new ApiClient("http://x").getOnboarding("a b");
    expect(fetchMock.mock.calls[0][0]).toBe(
      "http://x/api/projects/a%20b/onboarding",
    );
    expect(view.project.name).toBe("a b");
  });

  it("propagates the 404 detail for an unknown onboarding project", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(
        jsonResponse({ detail: "unknown project: nope" }, 404),
      );
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      new ApiClient("http://x").getOnboarding("nope"),
    ).rejects.toMatchObject({ status: 404, detail: "unknown project: nope" });
  });
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vscode-ext && npm test`
Expected: FAIL — модуль `src/onboarding` не существует; `getOnboarding` не метод.

- [ ] **Step 3: Implement `vscode-ext/src/onboarding.ts`**

```typescript
/** FR-04 third thin renderer (DESIGN-1101).
 *
 * FORMATTING ONLY: every verdict, aggregate and the next_items order come
 * from the server's build_onboarding (spec §1) — this module never
 * computes or re-sorts. All model strings pass mdEscape (two layers:
 * markdown structure + inline HTML; the preview renders both).
 */

export interface OnboardingProject {
  name: string;
  path?: string | null;
  description?: string | null;
  description_source?: string | null;
  freshness?: string | null;
}

export interface OnboardingSummary {
  readiness?: number | null;
  done?: number | null;
  total?: number | null;
  lagging?: boolean | null;
  contract_drift?: boolean | null;
}

export interface OnboardingPhase {
  phase?: string | null;
  counts?: Record<string, number> | null;
}

export interface OnboardingPosition {
  summary?: OnboardingSummary | null;
  median_readiness?: number | null;
  phases?: OnboardingPhase[] | null;
}

export interface OnboardingNextItem {
  id?: string | null;
  title?: string | null;
  phase?: string | null;
  computed_status?: string | null;
  actionable?: boolean | null;
  blocked_by?: string[] | null;
}

export interface OnboardingTask {
  task_id?: string | null;
  status?: string | null;
  title?: string | null;
}

export interface OnboardingView {
  project: OnboardingProject;
  roadmap_position?: OnboardingPosition | null;
  next_items?: OnboardingNextItem[] | null;
  live_tasks?: OnboardingTask[] | null;
  warnings?: string[] | null;
}

const DASH = "—";

/** Spec's exact 4-step algorithm; order is significant. */
export function mdEscape(value: string): string {
  return value
    .replace(/\\/g, "\\\\")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/[`*_[\]()#|]/g, (ch) => `\\${ch}`)
    .replace(/\r\n|\r|\n/g, " ");
}

function pct(x: number | null | undefined): string {
  return typeof x === "number" ? `${Math.round(x * 100)}%` : DASH;
}

function text(x: string | null | undefined): string {
  return typeof x === "string" && x !== "" ? mdEscape(x) : DASH;
}

export function renderOnboardingMarkdown(view: OnboardingView): string {
  const p = view.project;
  const lines: string[] = [`# ${mdEscape(p.name)}`];
  const meta = [
    p.path ? mdEscape(p.path) : null,
    p.freshness ? `freshness: ${mdEscape(p.freshness)}` : null,
  ].filter((x): x is string => x !== null);
  if (meta.length) {
    lines.push("", meta.join(" · "));
  }

  lines.push("", "## Description", "");
  lines.push(
    p.description
      ? mdEscape(p.description) +
          (p.description_source ? ` (${mdEscape(p.description_source)})` : "")
      : `${DASH} (no description)`,
  );

  lines.push("", "## Roadmap position", "");
  const pos = view.roadmap_position;
  const s = pos?.summary;
  if (s) {
    const flags = [
      s.lagging === true ? "**LAGGING**" : null,
      s.contract_drift === true ? "**CONTRACT DRIFT**" : null,
    ].filter((x): x is string => x !== null);
    lines.push(
      `readiness ${pct(s.readiness)} (${s.done ?? DASH}/${s.total ?? DASH})` +
        ` · median ${pct(pos?.median_readiness)}` +
        (flags.length ? ` · ${flags.join(" · ")}` : ""),
    );
    for (const ph of pos?.phases ?? []) {
      // alphabetical join is presentation, not aggregation (spec §1)
      const counts = Object.entries(ph.counts ?? {})
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([k, v]) => `${mdEscape(k)}=${v}`)
        .join(", ");
      lines.push(`- phase ${text(ph.phase)}: ${counts || DASH}`);
    }
  } else {
    lines.push(`${DASH} (no roadmap items for this project)`);
  }

  lines.push("", "## Next items", "");
  const items = view.next_items ?? [];
  if (items.length === 0) {
    lines.push(`${DASH} (none)`);
  }
  for (const n of items) {
    // missing actionable => pessimistic ⛔ (consistent with S-3 spirit)
    const head = n.actionable === true ? "▶" : "⛔";
    const body = [text(n.id), text(n.title), text(n.computed_status)].join(
      " · ",
    );
    const blocked = n.blocked_by?.length
      ? ` — blocked by: ${n.blocked_by.map(mdEscape).join(", ")}`
      : "";
    lines.push(`- ${head} ${body}${blocked}`);
  }

  const tasks = view.live_tasks ?? [];
  if (tasks.length) {
    lines.push("", "## Live tasks", "");
    for (const t of tasks) {
      lines.push(`- ${text(t.task_id)} · ${text(t.status)} · ${text(t.title)}`);
    }
  }

  const warnings = view.warnings ?? [];
  if (warnings.length) {
    lines.push("", "## Warnings", "");
    for (const w of warnings) {
      lines.push(`- ⚠ ${mdEscape(w)}`);
    }
  }

  return lines.join("\n") + "\n";
}
```

`vscode-ext/src/api.ts` — импорт и метод (рядом с соседними GET-методами):

```typescript
import type { OnboardingView } from "./onboarding";
```

```typescript
  async getOnboarding(name: string): Promise<OnboardingView> {
    return this.get<OnboardingView>(
      `/api/projects/${encodeURIComponent(name)}/onboarding`,
    );
  }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd vscode-ext && npm test`
Expected: PASS.

- [ ] **Step 5: Gates, then commit**

```bash
cd vscode-ext && npm run typecheck && npm test && npm run build
git add src/onboarding.ts src/api.ts test/onboarding.test.ts test/api.test.ts
git commit -m "feat: onboarding markdown mapper + getOnboarding (DESIGN-1101)"
```

---

### Task 2: команда, меню, ContentProvider, доки (DESIGN-1102 + scaffold-пины DESIGN-1103)

**Files:**
- Modify: `vscode-ext/src/extension.ts` (провайдер, команда, регистрации), `vscode-ext/src/tree.ts` (`contextValue` на detected project-узлах, ~строка 66), `vscode-ext/package.json` (command + menu), `README.md` (строка в VSCode-секции)
- Test: `vscode-ext/test/scaffold.test.ts` (пины)

**Interfaces:**
- Consumes: `renderOnboardingMarkdown`, `OnboardingView` (Task 1); `ApiClient.getOnboarding`; существующий способ получения `ApiClient` в командах extension.ts (найти и использовать РОВНО его — не изобретать второй); `ProjectNode` тип из tree.ts.
- Produces: команда `dispatcher.projectOnboarding`; схема `dispatcher-onboarding:`.

- [ ] **Step 1: Write the failing scaffold pins**

В `vscode-ext/test/scaffold.test.ts` (стиль файла — он читает package.json; следовать его хелперам):

```typescript
  it("contributes the project onboarding command, palette-visible", () => {
    const commands = manifest.contributes.commands as Array<{
      command: string;
      title: string;
    }>;
    expect(
      commands.some(
        (c) =>
          c.command === "dispatcher.projectOnboarding" &&
          c.title === "Dispatcher: Project Onboarding",
      ),
    ).toBe(true);
    // must NOT be hidden from the palette
    const palette = (manifest.contributes.menus?.commandPalette ?? []) as Array<{
      command: string;
      when?: string;
    }>;
    expect(
      palette.some(
        (m) => m.command === "dispatcher.projectOnboarding" && m.when === "false",
      ),
    ).toBe(false);
  });

  it("contributes the project context-menu entry with the exact when-rule", () => {
    const ctx = manifest.contributes.menus["view/item/context"] as Array<{
      command: string;
      when: string;
    }>;
    expect(
      ctx.some(
        (m) =>
          m.command === "dispatcher.projectOnboarding" &&
          m.when === "view == dispatcherProjects && viewItem == dispatcherProject",
      ),
    ).toBe(true);
  });
```

(если `manifest` в файле называется иначе — использовать имя файла.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd vscode-ext && npm test`
Expected: FAIL на обоих новых пинах.

- [ ] **Step 3: package.json**

В `contributes.commands`:

```json
      {
        "command": "dispatcher.projectOnboarding",
        "title": "Dispatcher: Project Onboarding"
      }
```

В `contributes.menus["view/item/context"]`:

```json
        {
          "command": "dispatcher.projectOnboarding",
          "when": "view == dispatcherProjects && viewItem == dispatcherProject",
          "group": "navigation"
        }
```

- [ ] **Step 4: tree.ts — contextValue**

В `ProjectsProvider.getTreeItem`, в ветке project-узла, после создания `item` (только для detected — `view.detected`):

```typescript
    if (view.detected) {
      item.contextValue = "dispatcherProject";
    }
```

- [ ] **Step 5: extension.ts — провайдер + команда**

Импорт: `import { renderOnboardingMarkdown } from "./onboarding";` (+ `ApiError` если ещё не импортирован; `ProjectNode` из tree.ts).

Рядом с другими module/activate-level конструкциями:

```typescript
  const onboardingDocs = new Map<string, string>();
  const onboardingChanged = new vscode.EventEmitter<vscode.Uri>();
  const onboardingProvider: vscode.TextDocumentContentProvider = {
    onDidChange: onboardingChanged.event,
    provideTextDocumentContent: (uri) =>
      onboardingDocs.get(uri.path) ??
      "onboarding not loaded — run “Dispatcher: Project Onboarding”",
  };

  async function showOnboarding(name: string): Promise<void> {
    try {
      const view = await client().getOnboarding(name);
      if (typeof view?.project?.name !== "string") {
        void vscode.window.showErrorMessage("malformed onboarding response");
        return;
      }
      const uri = vscode.Uri.parse(
        `dispatcher-onboarding:/${encodeURIComponent(name)}.md`,
      );
      onboardingDocs.set(uri.path, renderOnboardingMarkdown(view));
      onboardingChanged.fire(uri); // re-run refreshes the SAME document
      const doc = await vscode.workspace.openTextDocument(uri);
      await vscode.commands.executeCommand("markdown.showPreview", doc.uri);
    } catch (err) {
      void vscode.window.showErrorMessage(
        err instanceof ApiError ? err.detail : String(err),
      );
    }
  }

  async function onboardingCommand(node?: ProjectNode): Promise<void> {
    if (node !== undefined && node.kind === "project") {
      await showOnboarding(node.entry.name);
      return;
    }
    // palette path: FRESH overview, never the provider's poll state
    let names: string[];
    try {
      const overview = await client().overview();
      names = overview.projects.filter((p) => p.detected).map((p) => p.name);
    } catch (err) {
      void vscode.window.showErrorMessage(
        err instanceof ApiError ? err.detail : String(err),
      );
      return;
    }
    const pick = await vscode.window.showQuickPick(names, {
      title: "Project onboarding",
    });
    if (pick !== undefined) {
      await showOnboarding(pick);
    }
  }
```

ВАЖНО: `client()` — заменить на РОВНО тот механизм, каким соседние команды получают `ApiClient` (прочитать `editConfigCommand`/`runAction` и использовать его; в отчёте назвать, что это было).

В `context.subscriptions.push(...)`:

```typescript
    vscode.workspace.registerTextDocumentContentProvider(
      "dispatcher-onboarding",
      onboardingProvider,
    ),
    vscode.commands.registerCommand(
      "dispatcher.projectOnboarding",
      (node?: ProjectNode) => void onboardingCommand(node),
    ),
    onboardingChanged,
```

- [ ] **Step 6: README**

В VSCode-секцию README.md — одна строка: команда «Dispatcher: Project Onboarding» (палитра или контекст-меню проекта) открывает read-only markdown-превью onboarding-экрана FR-04 (описание, позиция в roadmap, next items, live tasks); повторный вызов обновляет тот же документ.

- [ ] **Step 7: Run tests, gates, commit**

```bash
cd vscode-ext && npm run typecheck && npm test && npm run build
cd .. && uv run pytest -q
git add vscode-ext/src/extension.ts vscode-ext/src/tree.ts vscode-ext/package.json vscode-ext/test/scaffold.test.ts README.md
git commit -m "feat: Project Onboarding command — markdown preview via content provider (DESIGN-1102)"
```

Expected: ext-гейты зелёные; `uv run pytest -q` → 317 passed + 1 skipped (Python не тронут).

---

## Final whole-branch review mandate

- Оба гейта самому: ext (`npm run typecheck && npm test && npm run build`) + Python (317+1, ноль изменённых .py файлов — проверить `git diff --stat master...HEAD -- '*.py'` пуст).
- §1-инвариант: grep маппера на sort/filter по вердиктам — только presentation-сортировка counts; анти-пересортировка пинована тестом.
- Эскейп-матрица и missing-field кросс-матрица — прогнать, убедиться что кейсы реально ассертят класс (не один пример).
- Живой прогон, если MCP-Chromium/Node доступны — иначе честно: собрать .vsix НЕ требуется; минимум — запустить uvicorn на фикстурном workspace и дернуть `getOnboarding`-путь через node-скрипт с реальным fetch против живого сервера (URL-encoding + 404-detail), маппер прогнать на живом JSON-ответе и глазами проверить итоговый markdown.
- Deferred Minors из таск-ревью — триаж.
