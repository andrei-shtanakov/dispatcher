# Design — VSCode onboarding surface (FR-04, third renderer)

> **Context (2026-07-18):** опциональный хвост FR-04: onboarding-экран в
> VSCode-расширении. Numbering: DESIGN-1101+. Два ревью folded; их
> главный вопрос (§1) закрыт кодовым доказательством.

## 1. Named invariant — VSCode is a THIRD THIN RENDERER (мир A)

Композиция OnboardingView ЦЕЛИКОМ серверная: `build_onboarding`
(`dispatcher/core/onboarding.py`) считает `actionable`/`blocked_by`
(S-1..S-4), `roadmap_position` (реюз `build_summary`:
readiness/median/lagging/contract_drift), phase-разрез и порядок
`(not actionable, phase, id)`. Web (`renderOnboarding`, index.html) и
TUI (`_onboarding_sections`, detail.py) — чистые форматтеры; это
центральный инвариант FR-04 («канонический next — в одном месте»),
пинованный HTTP↔MCP-паритетом. Отсюда требование к этому дизайну:
**`renderOnboardingMarkdown` — форматирование и ТОЛЬКО; ни одного
вычисления вердикта/сортировки/агрегата на клиенте.** Порядок
`next_items` — серверный, клиент не пересортировывает. «Python не
трогается» — следствие мира A, а не экономия.

## 2. Components

### DESIGN-1101: mapper (`vscode-ext/src/onboarding.ts`)

Узкие TS-интерфейсы (паттерн model.ts: TS уже Python, неиспользуемое не
тащим) + `renderOnboardingMarkdown(view: OnboardingView): string`.
Секции (зеркалят web/TUI): заголовок `name · path · freshness`,
описание `[source]`, roadmap position (`readiness N% (done/total) ·
median M% · LAGGING · CONTRACT DRIFT` + phase-строки), next items
(`▶`/`⛔` `id · title · status`, `blocked by: …`), live tasks
(`task_id · status · title`), warnings.

**Runtime-тотальность (ревью 2 §4).** TS-типы — compile-time; JSON с
рантайма гарантий не даёт. Контракт полей:

| Поле | Класс | Поведение при отсутствии |
|---|---|---|
| `project.name` | **hard-required** | команда показывает честную ошибку («malformed onboarding response»), документ не открывается |
| всё остальное, включая вложенные (`project.description/…`, `roadmap_position` и его внутренности, `next_items[i].title/phase/computed_status/actionable/blocked_by`, `live_tasks[i].*`, `warnings`) | optional | секция/строка/фрагмент опускается или получает `—`-фолбэк; маппер тотален — `undefined` в выводе появиться не может |

**Эскейпинг — двухслойный, точный алгоритм (ревью 1 §4 + ревью 2 §2).**
Цель — оба выхлопа: markdown-СТРУКТУРА (markdown-it превью) и
инлайн-HTML (превью рендерит его). `mdEscape(s)`, порядок значим:

1. `\` → `\\` (до добавления новых backslash-ей);
2. HTML-энтити: `&`→`&amp;`, `<`→`&lt;`, `>`→`&gt;`;
3. backslash-эскейп структурных markdown: `` ` `` `*` `_` `[` `]` `(` `)` `#` `|`;
4. `\r`/`\n` → пробел (все интерполируемые поля — инлайновые).

Применяется к КАЖДОМУ строковому полю модели. CSP превью — второй
слой, не единственный.

### DESIGN-1102: доставка

- `ApiClient.getOnboarding(name)` → GET
  `/api/projects/{encodeURIComponent(name)}/onboarding`; 404/ошибки —
  существующий `ApiError`-путь с `detail`.
- Команда `dispatcher.projectOnboarding` («Dispatcher: Project
  Onboarding»): из палитры — QuickPick по СВЕЖЕМУ `client().overview()`
  с фильтром `detected` (ревью 1 §2 — не лезть во внутреннее состояние
  провайдера, не зависеть от poll-timing); из контекст-меню узла
  проекта — без пикера.
- Контекст-меню (ревью 1 §1): `ProjectsProvider.getTreeItem()` ставит
  `contextValue: "dispatcherProject"` detected-узлам; menu-rule
  `view == dispatcherProjects && viewItem == dispatcherProject`.
- **Рендер — `TextDocumentContentProvider` со схемой
  `dispatcher-onboarding:`** (ревью 2 §3), НЕ untitled-документ:
  untitled приходит «грязным» (Save?-промпт на закрытии, новый буфер на
  каждый вызов — известный недостаток untitled-подхода) —
  editable-паттерн config-editor-а не подходит read-only экрану. Провайдер отдаёт последний отрендеренный markdown
  из кэша по URI `dispatcher-onboarding:/<name>.md`; команда обновляет
  кэш, дёргает `onDidChange`, затем
  `vscode.workspace.openTextDocument(uri)` →
  `vscode.commands.executeCommand("markdown.showPreview", doc.uri)`
  (точная инкантация — ревью 1 §3: `markdown.showPreview` в расширении
  ещё не использовался). Повторный вызов обновляет тот же документ.
- Ошибки (сервер лежит, 404, malformed) → toast с detail (паттерн
  соседних команд).

### DESIGN-1103: Testing (гейты: `npm run typecheck && npm test && npm run build`)

| Scope | Пины |
|---|---|
| mapper | обе actionable-вердикта; `roadmap_position: null`; пустые live/warnings; **матрица эскейпа** (HTML-тег с `onerror`, markdown-контролы `` `*_[]()#| ``, pipe, `](http://…)`-ломатель, перевод строки — каждый как отдельный кейс класса); **missing-field кросс-матрица** (не по одному: комбинации, включая вложенные — `next_items[i]` без `computed_status` но с `blocked_by`, position без `median_readiness`, item без `title`); порядок next_items ВОСПРОИЗВОДИТСЯ как пришёл (анти-пересортировка §1) |
| api | `getOnboarding("a b")` — URL-encoding в пути + проброс `ApiError.detail` (ревью 1 §6) |
| scaffold.test.ts | команда в manifest; context-menu item с правильным when-rule; команда доступна из палитры (НЕ скрыта) (ревью 1 §5) |
| Python | не трогается — ноль изменений (следствие §1, не цель) |

## 3. Error handling

| failure | behaviour |
|---|---|
| сервер недоступен / HTTP-ошибка | toast с `ApiError.detail` |
| unknown project (404) | toast `unknown project: {name}` |
| `project.name` отсутствует в ответе | toast «malformed onboarding response», документ не открывается |
| прочие отсутствующие поля | тотальный маппер: секция опускается / `—` |

## 4. Out of scope

- Авто-refresh превью по poll-циклу (снимок по команде; схема
  `dispatcher-onboarding:` оставляет дверь открытой).
- Webview-панель; VSCode-suggest-поверхность (DESIGN-307 остаётся
  dashboard-only).
- Изменения Python-стороны.

## 5. Traceability

| Item | Design |
|---|---|
| Ревью 2 §1: мир A подтверждён кодом, анти-третий-источник-истины | §1 (named invariant + анти-пересортировка пин) |
| Ревью 2 §2 + ревью 1 §4: двухслойный эскейп, точный алгоритм, матрица | DESIGN-1101, DESIGN-1103 |
| Ревью 2 §3: недостаток untitled-подхода назван, ContentProvider выбран | DESIGN-1102 |
| Ревью 2 §4: required/optional-таблица, тотальность, кросс-матрица | DESIGN-1101, DESIGN-1103 |
| Ревью 1 §1: contextValue + menu-rule | DESIGN-1102 |
| Ревью 1 §2: QuickPick от свежего overview | DESIGN-1102 |
| Ревью 1 §3: showPreview-инкантация | DESIGN-1102 |
| Ревью 1 §5: scaffold-пины | DESIGN-1103 |
| Ревью 1 §6: URL-encoding юнит | DESIGN-1103 |

## 6. Milestone

Один PR, ветка `feat/vscode-onboarding`; план — 2 таска (mapper+api;
команда/меню/провайдер+доки), едет в фиче-ветке без plan-PR.
