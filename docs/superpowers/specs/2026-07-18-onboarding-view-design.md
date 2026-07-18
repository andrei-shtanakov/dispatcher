# Design — FR-04: onboarding view (project → description, roadmap position, next tasks)

> **Context (2026-07-18):** FR-04 («выбор проекта → описание, позиция в
> roadmap и предстоящие задачи одним экраном», Should, traces G-06/J-04,
> persona P-02 — новичок на онбординге). Последний открытый Should-item
> брифа. Numbering: DESIGN-801+. Folded ниже: два пре-спековых ревью и
> внешний имплементационный разбор (dev-workspace-заметка
> `dispatcher-onboarding-feature-handoff-2026-07-18.md`, вне этого
> репо); все утверждения разбора проверены против кода.

## 1. Principles

1. **Один канонический join.** «Что брать следующим» считается ровно в
   одном месте — чистом билдере `build_onboarding` (стиль
   `build_summary`/`build_drift`). Поверхности (web/TUI/MCP) НЕ
   пересчитывают ни readiness, ни блокировки — тот же аргумент, что
   закрепил `roadmap_drift` в FR-05.
2. **Семантика зависимостей прибита к существующей таксономии, не
   изобретена.** Словарь `computed_status` закрыт:
   `unknown | planned | implemented | verified | blocked | drift`
   (`_status_from_evidence` + `_apply_blocked` + `_apply_drift`,
   roadmap.py). Статуса `done` не существует. «Зависимость закрыта» ⇔
   `computed_status ∈ _DONE` — реиспользуем константу
   `roadmap._DONE = ("implemented", "verified")`, никаких перепечатанных
   литералов (review 2 §1).
3. **Пессимизм при неопределённости.** Dep, которого нет среди roadmap-
   items, — это блокер + warning «unknown dependency id …», а не
   оптимистичное ready: «неизвестное = не подтверждено», и битая ссылка
   в данных всплывает (review 2 §1). Оба знака пинуются тестами.
4. **Коллекторы не трогаем.** `description` — post-collect шаг в
   `SnapshotService`, одна реализация на все пять коллекторов.

## 2. Semantics — the load-bearing decisions (review 2 §1, §4)

Все четыре — осознанные решения, каждое пинуется тестом:

| # | Решение | Обоснование |
|---|---|---|
| S-1 | Dep закрыт ⇔ статус ∈ `_DONE` (implemented/verified) | единственная done-семантика в кодовой базе (`build_summary`, `_apply_blocked` считают так же) |
| S-2 | Dep в `drift` → в `blocked_by` | `drift ∉ _DONE`; контракт разъехался — строиться на нём нельзя; консистентно с `_apply_blocked` |
| S-3 | Отсутствующий/неизвестный dep → в `blocked_by` + warning | пессимизм при неопределённости; консистентно с `_apply_blocked` (`dep not in views` → blocker) |
| S-4 | `actionable` поверхностный: только прямые `depends_on` | транзитивность не нужна — незакрытый транзитивный dep удерживает прямой dep вне `_DONE`, эффект каскадится сам; фиксируем как решение, не как недосмотр |

Порядок `next_items` — каноническое поведение того же ранга:
`actionable` первыми, затем `(phase or "", id)`. Пинуется фикстурой с
обоими сортировочными ключами. Продуктовое решение (review 2 §4):
actionable phase-2 item показывается выше blocked phase-1 — для вопроса
«что взять прямо сейчас» это правильно (заблокированное всё равно не
взять); UI сознательно подталкивает не ждать разблокировки фаз.

**Naming (review 2 §2):** булев флаг называется **`actionable`**, НЕ
`ready` — в одном JSON с `readiness`/`median_readiness` однокоренные
`ready`/`readiness` конфаундятся и человеком, и агентом.

## 3. Components

### DESIGN-801: project description (`dispatcher/core/descriptions.py`)

`ProjectSnapshot` получает два поля:

```python
description: str | None = None
description_source: Literal["readme", "pyproject", "package.json"] | None = None
```

Хелпер `extract_project_description(path: Path) -> tuple[str | None, str | None]`
(модуль `core/descriptions.py`, коллекторы не трогаем):

- **Файлы-кандидаты** (первый существующий, регистронезависимый матч
  имени): `README.md`, `README.rst`, `README` (review 2 §3 — только-.md
  отрезал бы .rst-мир). Markdown: пропуск заголовков (`#`), badge/image-
  строк (`[![`, `![`), HTML-блоков и HTML-комментариев (`<p …>`, `<img`,
  `<!--`), склейка первого содержательного абзаца до пустой строки.
  reStructuredText/plain: то же + пропуск title-underline строк
  (`===`/`---`). Это эвристика; экзотика (logo+TOC до текста)
  деградирует в фолбэк — задокументированное ограничение, не баг.
- **Fallback-порядок**: README → `pyproject.toml [project].description`
  → `package.json .description`. README-first — осознанный компромисс
  (review 2 §3): README богаче и человечнее для онбординга, метаданные —
  терсный одно-строчник; выбираем «богато-но-шумно», фолбэк ловит
  проекты с плохим README.
- **Отказоустойчивость**: не-UTF-8, ошибка чтения, файл > 256 KiB →
  этот источник пропускается (деградация к следующему, в конце `None`).
  Никаких исключений наружу; отсутствие README/метаданных — НЕ warning
  (шум).
- **Обрезка**: детерминированная, лимит 360 символов, по границе слова
  с `…`; пинуется тестом.

Вызов: в `SnapshotService._collect()` после цикла коллекторов и
добавления undetected-rows — обогащаются только снапшоты с
`detected and path` (undetected остаются `None`). Хелпер читает ТОЛЬКО
под `snapshot.path` — никаких скансов роутов (`_cowork_output` и т.п.
структурно недостижимы).

### DESIGN-802: `OnboardingView` + `build_onboarding` (`dispatcher/core/onboarding.py`)

Чистый билдер `build_onboarding(snapshot, roadmap, contracts) -> OnboardingView`.
Модели (отдельный модуль — граница чище, roadmap.py не растёт):

```python
class OnboardingProject(BaseModel):
    name: str
    path: str
    description: str | None
    description_source: Literal["readme", "pyproject", "package.json"] | None
    freshness: str | None

class OnboardingRoadmapPosition(BaseModel):
    summary: ProjectSummary          # реиспользуем модель build_summary
    median_readiness: float | None   # контекст для lagging
    phases: list[PhaseSummary]       # разрез ТОЛЬКО по items проекта

class OnboardingNextItem(BaseModel):
    id: str
    title: str
    phase: str | None
    computed_status: str
    actionable: bool                 # прямые deps ∈ _DONE (S-1..S-4)
    blocked_by: list[str]            # незакрытые/неизвестные прямые deps

class OnboardingView(BaseModel):
    project: OnboardingProject
    roadmap_position: OnboardingRoadmapPosition | None  # None: нет items
    next_items: list[OnboardingNextItem]
    live_tasks: list[TaskInfo]       # snapshot.tasks: pending | in_progress
    warnings: list[str]
```

- `roadmap_position`: считается через существующий `build_summary`
  (строка своего проекта + median) — readiness/lagging НЕ
  пересчитываются вторым способом (риск-таблица разбора); `phases` —
  `build_phases` над отфильтрованными items проекта. `None`, если у
  проекта нет ни одного roadmap-item — view живёт (описание + живые
  задачи).
- `next_items`: items с `owner_project == snapshot.name` и
  `computed_status ∉ _DONE`; `actionable`/`blocked_by` считаются
  независимо от `computed_status` — ключевой нюанс разбора:
  `_apply_blocked` красит только `planned`, поэтому `in_progress`/`drift`
  item с незакрытыми deps не несёт `blockers`, а onboarding обязан их
  показать.
- `live_tasks`: фильтр по статусам `pending`/`in_progress` (ровно эти
  два литерала; прочие словари статусов коллекторов — вне скоупа).
- `warnings`: `snapshot.warnings + roadmap.warnings + свои` (unknown
  deps), порядок стабильный, дедуп с сохранением первого вхождения.

### DESIGN-803: facade + HTTP

`read_api.onboarding(cache, roadmap_dirs, name) -> OnboardingView`:
lookup проекта той же семантикой, что `read_api.project()` — unknown →
`ReadLookupError(f"unknown project: {name}")` (HTTP 404 / ToolError,
текст идентичен). Контракты считаются ОДИН раз и передаются и в
`build_roadmap`, и в `build_onboarding` — паттерн `roadmap_summary`
(ADR-R5, один прогон checker-а на refresh).

Маршрут: `GET /api/projects/{name}/onboarding`,
`response_model=OnboardingView`, однострочная делегация в фасад.

### DESIGN-804: web

В SPA `detail(name)` (index.html) raw-JSON снапшота заменяется
секциями onboarding-ответа: описание (+source), позиция в roadmap
(readiness/median/lagging/drift + phases), next items
(actionable/blocked_by визуально различимы), live tasks, warnings.
Существующая spec-runner-config-панель остаётся ниже в том же
`detail-section`. Жест «клик по проекту» уже существует — «одним
экраном» выполняется без новой навигации.

### DESIGN-805: TUI

`ProjectDetailScreen(snap, onboarding)` — второй параметр
(`OnboardingView | None`; None — roadmap недоступен, экран деградирует
до сегодняшнего вида). Onboarding-секции (описание, позиция, next,
live) добавляются СВЕРХУ, существующие snapshot-секции остаются —
текущие assertions (`collected:`, `detected:`, `T-9`) не ломаются
(вариант 2 разбора). Данные — тот же `read_api.onboarding()` из
refresh-цикла `DispatcherApp`, не дублированный расчёт.

### DESIGN-806: MCP — 15-й тул

`onboarding(project)` — осознанное расширение whitelist-а (equality-пин
обновляется 14 → 15). Overlap с `project`/`roadmap_summary` гасится
описанием тула, явная формула (review 2 §5): «Для вопроса „что делать
следующим в конкретном проекте" предпочитай `onboarding(project)`
вместо связки `project` + `roadmap_summary`; `project` — сырой
снапшот; `roadmap_summary` — readiness всей экосистемы». Параметр
`project` — с описанием и примером `'Maestro'` (регистр имени
коллектора!). Ошибки: `ReadLookupError` → `ToolError(str(err))`.
Возврат `model_dump(mode="json")`. Альтернатива «параметр verbosity у
существующего тула» рассмотрена и отклонена: дискаверабилити по имени
для агента ценнее компактности whitelist-а.

### DESIGN-807: Testing

| Scope | Пины |
|---|---|
| descriptions.py | содержательный абзац; README из одних заголовков/бейджей; пустой/отсутствующий → fallback pyproject → package.json; **какой источник победил** (`description_source`, оба фолбэка); .rst; не-UTF-8 → пропуск источника; обрезка 360 по слову с `…` |
| SnapshotService | detected получает description; undetected (`path=""`) — `None`; коллекторы не тронуты |
| build_onboarding | S-1..S-4 **оба знака**: dep verified → actionable=true; dep planned → blocked_by; dep drift → blocked_by; dep unknown → blocked_by + warning; сортировка (actionable-first, затем phase/id) фикстурой с обоими ключами; проект без items → `roadmap_position=None`; live_tasks-фильтр; warnings merge + dedup |
| HTTP | 200-shape; 404 detail text |
| MCP | whitelist 15 (equality); описания тула и параметра непусты; ToolError text; HTTP↔MCP JSON-паритет на shared-инстансах (существующая machinery, фикстура получает roadmap с deps во всех состояниях S-1..S-3) |
| Serializer | populated `OnboardingView` в существующий `jsonable_encoder == model_dump(mode="json")` guard |
| TUI | detail рендерит 4 onboarding-секции на фикстуре; существующие snapshot-assertions живы |
| Web | лёгкий static-пин: index.html содержит endpoint-строку и detail-контейнер |

### DESIGN-808: Documentation

README: секция API — `/api/projects/{name}/onboarding`; секция MCP —
«15 read-only tools» + строка тула. COWORK_CONTEXT: interfaces line.
`spec/discovery-brief-customer.md`: resolution-pointer FR-04 после
мержа.

## 4. Error handling

| failure | behaviour |
|---|---|
| unknown project | `ReadLookupError` → HTTP 404 / `ToolError`, текст `unknown project: {name}` |
| README/метаданные нечитаемы | деградация источника → следующий → `None`; без warnings |
| dep-id вне roadmap | `blocked_by` + warning (S-3) |
| проект без roadmap-items | `roadmap_position=None`, next_items=[], view живёт |
| roadmap недоступен в TUI | `onboarding=None` → detail деградирует до snapshot-секций |

## 5. Out of scope

- VSCode-поверхность (отдельный заход при желании).
- Редактирование описаний; AI-подсказки (DESIGN-307).
- Транзитивный анализ deps (S-4 — поверхностность осознанна).
- Статусные словари live-задач за пределами `pending`/`in_progress`.

## 6. Traceability

| Item | Design |
|---|---|
| FR-04 acceptance (описание + позиция + предстоящее, один экран) | §2, DESIGN-801..805 |
| Разбор: blocked красит только planned → считать независимо | DESIGN-802 |
| Разбор: контракты один раз (ADR-R5) | DESIGN-803 |
| Разбор: TUI вариант 2 (snap + onboarding) | DESIGN-805 |
| Review 2 §1: таксономия закрыта, `_DONE`-константа, missing dep пессимистично | §2 S-1, S-3 |
| Review 1: dep в drift блокирует | §2 S-2 |
| Review 2 §2: `actionable`, не `ready` | §2 naming |
| Review 2 §3: .rst/plain README, README-first как названный компромисс, деградация, word-boundary trim, `Literal` source | DESIGN-801 |
| Review 2 §4: порядок пинуется, actionable-first — продуктовое решение | §2 |
| Review 2 §5: отдельный тул + анти-overlap формула в описании | DESIGN-806 |
| Review 2 minors: warnings dedup, source-winner тест, S-4 одной строкой | DESIGN-802, DESIGN-807, §2 S-4 |

## 7. Milestone

Два PR (размер под Copilot-review/human-merge flow, по прецеденту FR-05):

1. **Data-plane PR**: DESIGN-801..803 + DESIGN-806 + их тесты (описание,
   билдер, фасад, HTTP, MCP — весь канонический слой и parity).
2. **UI PR**: DESIGN-804..805 + DESIGN-808 (web-секции, TUI-детализация,
   документация).
