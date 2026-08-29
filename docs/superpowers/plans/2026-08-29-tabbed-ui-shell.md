# Tabbed UI PR-1: среда, tab shell, главный экран Launchpad — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** страница перестаёт быть вертикальным отчётом: сверху — строка вкладок,
`Launchpad` открыт по умолчанию и содержит весь жизненный цикл запуска вместе с
run view, опрашивается только активный экран.

**Architecture:** три слоя, снизу вверх. (1) Тестовая среда: `tests/web/dom.js`
получает `location`/`history`/`hashchange` — это модель браузера, а не правило
приложения; без неё роутер уронит девять harness'ов на boot. (2) Tab shell в
`index.html`: существующие `<section>` заворачиваются в панели
`role="tabpanel"`, единственный писатель маршрута — `location.hash`, всё
переключение экранов происходит в обработчике `hashchange`. (3) Развязка
`refresh()`: реестр `screen id → loader`, опрос только активного экрана с одним
названным исключением для Launchpad с незакрытым pending.

**Tech Stack:** vanilla ES2020 в одном файле `dispatcher/server/static/index.html`
(без сборки и фреймворка); Node-harness'ы `tests/web/*_harness.js`, исполняющие
настоящий `<script>` страницы в VM поверх `tests/web/dom.js`; pytest, вызывающий
`node`; Python 3.13 + FastAPI на серверной стороне (в этом PR почти не трогается).

**Spec:** `docs/superpowers/specs/2026-08-29-tabbed-interface-design.md` — §3
(продуктовые решения), §4 (контракт shell), §5 (Launchpad), §8 (неизменяемые
экраны), §9 (загрузка и опрос), §10 (тестовая среда), §11 (тесты), §12 (PR-1 —
эта работа), §13 (критерии 1–7, 11–14).

## Global Constraints

- **Ни один DOM-id не переименовывается.** `#launchpad`, `#lp-*`,
  `#run-console`, `#rc-*`, `#sync-section`, `#errors`, `#epics`, `#waits-*`,
  `#roadmap*`, `#ta-*`, `#merge-gate`, `#detail-section` переезжают внутрь
  панелей как есть. Переименование = сломанный harness соседа и потерянная
  трассируемость.
- **Ни один wire-контракт не меняется.** Ни один запрос не приобретает и не
  теряет заголовок, тело или метод. Мутирующие запросы по-прежнему шлют
  `X-Action-Token` из `ensureActionToken()`.
- **Экранировать всё, что приходит с сервера,** перед попаданием в
  `innerHTML` — существующим хелпером `esc`, второго не заводить.
- **Значение вложенного сегмента hash никогда не идёт в `innerHTML`** и не
  строит путь к серверу иначе, чем через существующие endpoint'ы.
- **Harness исполняет ВЕСЬ настоящий `<script>` страницы**, не срез и не копию.
  Harness, снимающий `hidden` в обход UI, доказывает не то поведение, которое
  поставляется: экран открывается кликом по настоящей кнопке вкладки.
- **Python-тест JS падает, когда `node` отсутствует, и никогда не пропускается**
  (`tests/test_governance_js.py:25-29` — «a skip is how a suite goes green while
  covering nothing»).
- Длина строки Python — 88. После каждой задачи:
  `uv run ruff format . && uv run ruff check . --fix` и
  `uv run pyrefly check dispatcher tests scripts` — **явными путями**; голый
  `pyrefly check` не находит файлов и рапортует успех, ничего не проверив.
- Никаких npm-зависимостей: harness'ы бегут под голым `node`.
- **Базовый прогон.** На этой машине на `master` уже падают три live-smoke
  теста (нужны отсутствующие бинарники) плюс два предупреждения из
  `test_benchmarks_stub_integration.py`; известны флейки
  `test_run_end_through_the_resolution_path_also_binds_to_the_checkout` и
  `test_revendor_script.py::…[SIGINT]`. Ожидать ровно их, ничего больше.

## Реестр экранов (единый источник порядка)

Порядок и идентификаторы — контракт между задачами; таблица повторена в коде
один раз, в `SCREENS`.

| # | id | Вкладка | Панель | Секции внутри |
|---|---|---|---|---|
| 1 | `launchpad` | Launchpad | `#screen-launchpad` | `#launchpad`, `#run-console` |
| 2 | `sync` | Sync | `#screen-sync` | `#sync-section` |
| 3 | `projects` | Projects | `#screen-projects` | секция Projects, `#detail-section`, `#merge-gate`, `#task-authoring` |
| 4 | `errors` | Errors | `#screen-errors` | секция с `#errors-box` |
| 5 | `models` | Models | `#screen-models` | секция с `#models` |
| 6 | `contracts` | Contracts | `#screen-contracts` | секция с `#contracts` |
| 7 | `epics` | Epics | `#screen-epics` | секция с `#epics` |
| 8 | `waits` | Waits (partial) | `#screen-waits` | секция с `#waits-graph` |
| 9 | `roadmap` | Roadmap | `#screen-roadmap` | секция с `#roadmap` |
| 10 | `benchmarks` | Benchmarks | `#screen-benchmarks` | `#benchmarks-section` — вкладка условная (Задача 7) |

`#ta-outcomes` (`Unresolved task requests`) **вне** панелей: глобальная полоса
над активным экраном (спека §3.3).

---

## Задача 1: `location`, `history` и `hashchange` в модели браузера

**Files:**
- Modify: `tests/web/dom.js`
- Create: `tests/web/dom_selftest.js`
- Create: `tests/test_dom_js.py`

**Interfaces:**
- Produces: `makeBrowser(initialHash?) -> {window, location, history, setHash}`,
  экспортируемый из `tests/web/dom.js`. Все последующие задачи и все девять
  существующих harness'ов кладут `window`, `location`, `history` из него в
  свой VM-контекст.
- Produces: `dispatch(el, type, {force?, init?})` — `init` подмешивается в
  объект события, чтобы тест мог послать `{key: "ArrowRight"}`. Без этого
  клавиатурное требование спеки §4 непроверяемо (Задача 2).

**Design notes for the implementer:**

Прочитай шапку `tests/web/dom.js` (строки 1–14): файл реализует **браузер**, а
не приложение. `location` и `history` — браузер, поэтому им место здесь, а не в
harness'ах, где они разъехались бы девятью копиями.

Три поведения настоящего браузера, которые обязана воспроизвести модель, потому
что на них опирается роутер страницы:

1. присваивание `location.hash` меняет hash **и** доставляет `hashchange`;
2. присваивание того же значения не доставляет ничего;
3. `history.pushState`/`replaceState` меняют hash и **не** доставляют
   `hashchange` (а `history.back()`/`forward()` — доставляют, когда отличается
   только hash).

Третье — не педантизм: страница пишет маршрут единственным способом (через
`location.hash`), и модель, доставляющая событие ещё и на `pushState`, скрыла бы
двойной вызов роутера.

- [ ] **Шаг 1: написать падающий selftest**

`tests/web/dom_selftest.js`:

```js
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

console.log(failures ? `\nFAILED: ${failures}` : '\nOK: browser model');
process.exit(failures ? 1 : 0);
```

`tests/test_dom_js.py`:

```python
"""The browser model under tests/web/dom.js is itself under test.

A router built on location/history/hashchange is only as trustworthy as the
stand-in it is exercised against, so the stand-in gets its own red/green.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

WEB = Path(__file__).parent / "web"
SELFTEST = WEB / "dom_selftest.js"


def test_browser_model_behaves_like_a_browser() -> None:
    node = shutil.which("node")
    # A skip is how a suite goes green while covering nothing.
    assert node is not None, "node is required for the web harnesses"
    result = subprocess.run(
        [node, str(SELFTEST)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
```

- [ ] **Шаг 2: прогнать до RED**

Run: `uv run pytest tests/test_dom_js.py -v`
Expected: FAIL — `TypeError: makeBrowser is not a function` (dom.js его не
экспортирует).

- [ ] **Шаг 3: реализовать модель браузера**

В `tests/web/dom.js`, перед `module.exports`:

```js
// ---- browser: location, history, hashchange -------------------------------
//
// The page routes screens through the hash and writes it exactly one way
// (`location.hash = …`), so the model has to get three browser behaviours
// right: assignment fires `hashchange`, a same-value assignment does not, and
// pushState/replaceState move the hash silently while back()/forward() fire.

/** A minimal same-document history + location pair. */
function makeBrowser(initialHash = '') {
  const listeners = Object.create(null);
  const normalise = v => {
    const s = String(v == null ? '' : v);
    if (s === '') return '';
    return s.startsWith('#') ? s : `#${s}`;
  };
  // History entries the page created, plus the one it started on.
  const entries = [normalise(initialHash)];
  let index = 0;
  const fire = () => {
    for (const fn of (listeners.hashchange || []).slice()) {
      fn({type: 'hashchange'});
    }
  };
  const window = {
    open() {},
    addEventListener(type, fn) {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    removeEventListener(type, fn) {
      listeners[type] = (listeners[type] || []).filter(f => f !== fn);
    },
  };
  const location = {
    get hash() { return entries[index]; },
    set hash(value) {
      const next = normalise(value);
      if (next === entries[index]) return;   // browsers stay silent here
      entries.splice(index + 1);             // a new entry truncates forward
      entries.push(next);
      index = entries.length - 1;
      fire();
    },
  };
  const move = delta => {
    const next = index + delta;
    if (next < 0 || next >= entries.length) return;
    index = next;
    fire();
  };
  const history = {
    // Same-document pushState/replaceState do NOT fire hashchange.
    pushState(_state, _title, url) {
      entries.splice(index + 1);
      entries.push(normalise(hashOfUrl(url, entries[index])));
      index = entries.length - 1;
    },
    replaceState(_state, _title, url) {
      entries[index] = normalise(hashOfUrl(url, entries[index]));
    },
    back() { move(-1); },
    forward() { move(1); },
  };
  window.location = location;
  window.history = history;
  return {window, location, history, setHash: v => { location.hash = v; }};
}

/** `url` is whatever the page passed to pushState: '#x', '/p#x' or null. */
function hashOfUrl(url, fallback) {
  if (url == null) return fallback;
  const s = String(url);
  const at = s.indexOf('#');
  return at === -1 ? '' : s.slice(at);
}
```

В `dispatch()` — необязательный `init`, подмешиваемый в событие (нужен, чтобы
тест мог послать `key`; существующие вызовы не меняются):

```js
function dispatch(el, type, {force = false, init = null} = {}) {
  if (!force && (el.disabled || !el.visible)) return [];
  const event = {
    type, target: el, currentTarget: null,
    preventDefault() {}, stopPropagation() {},
    ...(init || {}),
  };
  // …unchanged body…
}
```

и в экспорт:

```js
module.exports = {Document, El, TextNode, dispatch, parseFragment, makeBrowser};
```

- [ ] **Шаг 4: прогнать до GREEN**

Run: `uv run pytest tests/test_dom_js.py -v`
Expected: PASS.

- [ ] **Шаг 5: гигиена и коммит**

```bash
uv run ruff format . && uv run ruff check . --fix
uv run pyrefly check dispatcher tests scripts
git add tests/web/dom.js tests/web/dom_selftest.js tests/test_dom_js.py
git commit -m "test(web): модель браузера — location, history, hashchange в dom.js"
```

---

## Задача 2: tab shell — разметка, роутер, перенос секций, починка harness'ов

**Files:**
- Modify: `dispatcher/server/static/index.html`
- Create: `tests/web/screens.js`
- Create: `tests/web/tabs_harness.js`
- Create: `tests/test_tabs_js.py`
- Modify: все девять существующих harness'ов — `tests/web/benchmarks_harness.js`,
  `epics_harness.js`, `governance_harness.js`, `launchpad_harness.js`,
  `product_proposals_harness.js`, `run_console_harness.js`,
  `run_status_harness.js`, `runs_harness.js`, `task_authoring_harness.js`

**Interfaces:**
- Consumes: `makeBrowser` (Задача 1).
- Produces: в странице — `SCREENS`, `parseHash(raw) -> {screen, sub}`,
  `navigate(screen, sub)`, `applyRoute()`, `activeScreen()`; в тестах —
  `tests/web/screens.js` с `browserGlobals(initialHash?)` и
  `openScreen(page, id)`.

**Design notes for the implementer:**

Эта задача — единственная в плане, которая одновременно ломает и чинит: как
только панели получают `hidden`, `dispatch()` из `dom.js` отказывается кликать
по элементам вне активного экрана (`el.visible` обходит предков, `dom.js:231`).
Поэтому перенос секций и перевод девяти harness'ов на явное открытие экрана —
один деливерабл: «страница со вкладками и зелёный набор». Разделить их значит
оставить ревьюеру задачу, которую нельзя принять.

Единственный писатель маршрута — `location.hash`. Клик по вкладке не переключает
панели напрямую: он пишет hash, браузер доставляет `hashchange`, и уже
обработчик применяет маршрут. Back/Forward после этого работают сами, без второй
кодовой ветки.

Секции переносятся **целиком, без правки внутреннего HTML**. Единственное
допустимое изменение внутри секции — ничего. Если задача потребовала тронуть
разметку внутри секции, значит она вышла за границы; остановись и скажи.

- [ ] **Шаг 1: написать падающий harness вкладок**

`tests/web/screens.js`:

```js
// Shared, app-level helpers for harnesses that must open a screen before they
// can touch it. Deliberately thin: it knows the page has tabs, nothing else.
'use strict';

const path = require('path');
const {makeBrowser, dispatch} = require(path.join(__dirname, 'dom.js'));

/** Globals a harness must put in its VM context for the router to work. */
function browserGlobals(initialHash = '') {
  const {window, location, history} = makeBrowser(initialHash);
  return {window, location, history};
}

/**
 * Opens a screen the way a person does: by clicking the real tab button.
 * A harness that flips `hidden` by hand proves nothing about the shipped UI.
 */
async function openScreen(page, id) {
  const tab = page.document.getElementById(`tab-${id}`);
  if (!tab) throw new Error(`no tab button for screen "${id}"`);
  await Promise.all(dispatch(tab, 'click'));
  for (let i = 0; i < 5; i++) await new Promise(r => setTimeout(r, 0));
  const panel = page.document.getElementById(`screen-${id}`);
  if (!panel || panel.hidden) throw new Error(`screen "${id}" did not open`);
}

module.exports = {browserGlobals, openScreen};
```

`tests/web/tabs_harness.js` — новый harness, тот же скелет, что у соседей
(шапка, счётчики, `boot`, `withPage`, `testCase`, печать сводки). Скопируй
скелет из `tests/web/launchpad_harness.js` (строки 32–230): загрузка `<body>`,
единственный `<script>`, `defaultRoutes`, `makeIntervalRecorder`, `drain`,
`check`. В `ctx` добавь `...browserGlobals(opts.hash)`.

**Форма `withPage`.** Ровно один объект опций на все задачи плана:
`withPage(fn, opts)`, где `opts = {hash?: string, routes?: Array<[test, make]>}`.
Маршруты из `opts.routes` **добавляются в начало** списка `defaultRoutes(...)`,
чтобы переопределять его. Там же — хелпер переопределения маршрута уже
загруженной страницы (список маршрутов во всех девяти harness'ах — массив пар,
а не Map, и таким остаётся):

```js
/** Overrides a route on an already-booted page. */
function overrideRoute(page, url, make) {
  page.routes.unshift([u => u === url, make]);
}
```

Случаи:

```js
testCase('boots on Launchpad when no hash is given', async () => {
  await withPage(page => {
    check(!el(page, '#screen-launchpad').hidden, 'launchpad panel is visible');
    check(el(page, '#screen-sync').hidden, 'sync panel is hidden');
    check(el(page, '#tab-launchpad').attributes['aria-selected'] === 'true',
      'launchpad tab is aria-selected');
  });
});

testCase('exactly one panel is visible at a time', async () => {
  await withPage(async page => {
    await openScreen(page, 'epics');
    const visible = SCREEN_IDS.filter(
      id => !page.document.getElementById(`screen-${id}`).hidden);
    check(visible.length === 1 && visible[0] === 'epics',
      `one visible panel, got ${visible.join(',') || 'none'}`);
  });
});

testCase('a direct hash opens that screen', async () => {
  await withPage(page => {
    check(!el(page, '#screen-waits').hidden, 'waits opened from the hash');
  }, {hash: '#waits'});
});

testCase('an unknown hash falls back to Launchpad', async () => {
  await withPage(page => {
    check(!el(page, '#screen-launchpad').hidden, 'fell back to launchpad');
  }, {hash: '#no-such-screen'});
});

testCase('a malformed nested segment falls back to Launchpad', async () => {
  await withPage(page => {
    check(!el(page, '#screen-launchpad').hidden, 'fell back to launchpad');
  }, {hash: '#projects/../etc/passwd'});
});

testCase('back and forward walk the screens', async () => {
  await withPage(async page => {
    await openScreen(page, 'models');
    await openScreen(page, 'contracts');
    page.ctx.history.back();
    await drain();
    check(!el(page, '#screen-models').hidden, 'back landed on models');
    page.ctx.history.forward();
    await drain();
    check(!el(page, '#screen-contracts').hidden, 'forward returned to contracts');
  });
});

testCase('arrow keys move between screens, Enter opens one', async () => {
  await withPage(async page => {
    dispatch(el(page, '#tab-launchpad'), 'keydown', {init: {key: 'ArrowRight'}});
    await drain();
    check(!el(page, '#screen-sync').hidden, 'ArrowRight moved to sync');
    dispatch(el(page, '#tab-sync'), 'keydown', {init: {key: 'ArrowLeft'}});
    await drain();
    check(!el(page, '#screen-launchpad').hidden, 'ArrowLeft moved back');
    dispatch(el(page, '#tab-epics'), 'keydown', {init: {key: 'Enter'}});
    await drain();
    check(!el(page, '#screen-epics').hidden, 'Enter opened the focused tab');
  });
});

testCase('every tab carries its ARIA wiring', async () => {
  await withPage(page => {
    for (const id of SCREEN_IDS) {
      const tab = el(page, `#tab-${id}`);
      const panel = el(page, `#screen-${id}`);
      check(tab.attributes.role === 'tab', `${id}: role=tab`);
      check(tab.attributes['aria-controls'] === `screen-${id}`,
        `${id}: aria-controls points at the panel`);
      check(panel.attributes.role === 'tabpanel', `${id}: role=tabpanel`);
      check(panel.attributes['aria-labelledby'] === `tab-${id}`,
        `${id}: panel is labelled by its tab`);
    }
  });
});

testCase('the unresolved-requests band lives outside every panel', async () => {
  await withPage(page => {
    const band = el(page, '#ta-outcomes');
    for (const id of SCREEN_IDS) {
      check(!band.closest(`#screen-${id}`),
        `#ta-outcomes must not sit inside screen-${id}`);
    }
  });
});
```

`SCREEN_IDS` в harness'е — литеральный список из девяти id в порядке реестра
(`benchmarks` добавит Задача 7); он существует именно для того, чтобы порядок
вкладок был зафиксирован тестом, а не только кодом.

`tests/test_tabs_js.py` — копия структуры `tests/test_launchpad_js.py`:
находит `node`, ассертит его наличие (не skip), запускает
`tests/web/tabs_harness.js` с путём до `index.html`, требует returncode 0.

- [ ] **Шаг 2: прогнать до RED**

Run: `uv run pytest tests/test_tabs_js.py -v`
Expected: FAIL — `#tab-launchpad` в разметке нет.

- [ ] **Шаг 3: разметка shell и перенос секций**

Сразу после `<h1>`-шапки страницы и **перед** `#launchpad`:

```html
<nav id="tablist" role="tablist" aria-label="Разделы">
  <button id="tab-launchpad" role="tab" type="button" aria-controls="screen-launchpad" aria-selected="true">Launchpad</button>
  <button id="tab-sync" role="tab" type="button" aria-controls="screen-sync" aria-selected="false">Sync</button>
  <button id="tab-projects" role="tab" type="button" aria-controls="screen-projects" aria-selected="false">Projects</button>
  <button id="tab-errors" role="tab" type="button" aria-controls="screen-errors" aria-selected="false">Errors</button>
  <button id="tab-models" role="tab" type="button" aria-controls="screen-models" aria-selected="false">Models</button>
  <button id="tab-contracts" role="tab" type="button" aria-controls="screen-contracts" aria-selected="false">Contracts</button>
  <button id="tab-epics" role="tab" type="button" aria-controls="screen-epics" aria-selected="false">Epics</button>
  <button id="tab-waits" role="tab" type="button" aria-controls="screen-waits" aria-selected="false">Waits (partial)</button>
  <button id="tab-roadmap" role="tab" type="button" aria-controls="screen-roadmap" aria-selected="false">Roadmap</button>
</nav>
```

`#ta-outcomes` вынести **выше** панелей, сразу под `<nav>`. Затем каждую группу
секций из реестра обернуть:

```html
<div id="screen-launchpad" class="screen" role="tabpanel" aria-labelledby="tab-launchpad">
  <!-- существующие <section id="launchpad"> и <section id="run-console"> без правок -->
</div>
<div id="screen-sync" class="screen" role="tabpanel" aria-labelledby="tab-sync" hidden>
  <!-- существующая <section id="sync-section"> -->
</div>
…
```

`#benchmarks-section` пока оставить на месте, вне панелей (Задача 7 заведёт ей
вкладку). Стиль вкладок — в существующий `<style>`:

```css
#tablist { display: flex; gap: 4px; overflow-x: auto; white-space: nowrap;
           border-bottom: 1px solid #d4d4d8; margin: 0 0 12px; }
#tablist button { border: 0; background: none; padding: 8px 12px;
                  font: inherit; cursor: pointer; border-bottom: 3px solid transparent; }
#tablist button[aria-selected="true"] { border-bottom-color: #2563eb;
                                        background: #eff6ff; font-weight: 600; }
```

- [ ] **Шаг 4: роутер**

В начало `<script>`, рядом с `esc`:

```js
// ---- screens and routing --------------------------------------------------
//
// The hash is the ONLY writer of the current screen: a tab click assigns
// `location.hash`, the browser delivers `hashchange`, and applyRoute() is the
// single place that shows a panel. Back/Forward then work with no second code
// path. Where there is no `location` (the Node harness sandbox before it
// installs one), routing degrades to a static Launchpad rather than throwing —
// the same feature-detect idiom the launchpad timer already uses.

const SCREENS = [
  {id: "launchpad", label: "Launchpad"},
  {id: "sync", label: "Sync"},
  {id: "projects", label: "Projects"},
  {id: "errors", label: "Errors"},
  {id: "models", label: "Models"},
  {id: "contracts", label: "Contracts"},
  {id: "epics", label: "Epics"},
  {id: "waits", label: "Waits (partial)"},
  {id: "roadmap", label: "Roadmap"},
];
const DEFAULT_SCREEN = "launchpad";
const HASH_RE = /^#([a-z][a-z-]*)(?:\/([A-Za-z0-9._-]{1,200}))?$/;
const hasLocation = typeof location !== "undefined" && location !== null;
const hasWindow = typeof window !== "undefined" && window !== null
  && typeof window.addEventListener === "function";

let route = {screen: DEFAULT_SCREEN, sub: null};

/** Screens currently offered — Task 7 makes this conditional. */
function screenIds() { return SCREENS.map(s => s.id); }

function parseHash(raw) {
  const m = HASH_RE.exec(String(raw || ""));
  if (!m || !screenIds().includes(m[1])) {
    return {screen: DEFAULT_SCREEN, sub: null};
  }
  return {screen: m[1], sub: m[2] || null};
}

function hashFor(screen, sub) {
  return sub ? `#${screen}/${sub}` : `#${screen}`;
}

/** The one way the page changes screen. */
function navigate(screen, sub) {
  const next = hashFor(screen, sub || null);
  if (hasLocation && location.hash !== next) { location.hash = next; return; }
  route = parseHash(next);
  applyRoute();
}

function applyRoute() {
  if (hasLocation) route = parseHash(location.hash);
  for (const {id} of SCREENS) {
    const panel = document.getElementById(`screen-${id}`);
    const tab = document.getElementById(`tab-${id}`);
    if (!panel || !tab) continue;
    const active = id === route.screen;
    panel.hidden = !active;
    tab.setAttribute("aria-selected", active ? "true" : "false");
  }
  onScreenShown(route);
}

/** Extended by Task 5 (loaders) and Task 3 (drill-down). */
function onScreenShown(_route) {}

function activeScreen() { return route.screen; }

document.getElementById("tablist").addEventListener("click", e => {
  const button = e.target.closest("button[role='tab']");
  if (!button) return;
  navigate(button.id.replace(/^tab-/, ""), null);
});

document.getElementById("tablist").addEventListener("keydown", e => {
  const key = e.key;
  if (key !== "ArrowLeft" && key !== "ArrowRight"
      && key !== "Enter" && key !== " ") return;
  const ids = screenIds();
  const at = ids.indexOf(route.screen);
  if (key === "Enter" || key === " ") {
    const button = e.target.closest("button[role='tab']");
    if (button) { e.preventDefault(); navigate(button.id.replace(/^tab-/, ""), null); }
    return;
  }
  e.preventDefault();
  const delta = key === "ArrowLeft" ? -1 : 1;
  const next = ids[(at + delta + ids.length) % ids.length];
  navigate(next, null);
  const button = document.getElementById(`tab-${next}`);
  if (button) button.focus();
});

if (hasWindow) window.addEventListener("hashchange", applyRoute);
applyRoute();
```

- [ ] **Шаг 5: перевести девять harness'ов на явное открытие экрана**

В каждом из девяти harness'ов:

1. подключить хелперы —
   `const {browserGlobals, openScreen} = require(path.join(__dirname, 'screens.js'));`
2. в `ctx` заменить `window: {open: () => {}}` на `...browserGlobals()`;
3. перед первым обращением к элементам вне Launchpad вставить
   `await openScreen(page, '<id>')`.

Карта harness → экран: `epics_harness` → `epics`; `governance_harness`,
`product_proposals_harness`, `task_authoring_harness` → `projects`;
`benchmarks_harness`, `run_status_harness` → пока без открытия (секция
`#benchmarks-section` вне панелей до Задачи 7); `runs_harness`,
`run_console_harness`, `launchpad_harness` → `launchpad` (открыт по умолчанию,
явный вызов не нужен — но `browserGlobals()` в `ctx` нужен всем девяти, иначе
`hasWindow` окажется false и `hashchange` не подпишется).

- [ ] **Шаг 6: прогнать до GREEN**

Run: `uv run pytest tests/test_tabs_js.py tests/test_epics_js.py tests/test_governance_js.py tests/test_launchpad_js.py tests/test_product_proposals_js.py tests/test_run_console_js.py tests/test_runs_js.py tests/test_task_authoring_js.py tests/test_benchmarks_js.py tests/test_run_status_js.py -v`
Expected: все PASS.

- [ ] **Шаг 7: гигиена и коммит**

```bash
uv run ruff format . && uv run ruff check . --fix
uv run pyrefly check dispatcher tests scripts
uv run pytest -x -q
git add dispatcher/server/static/index.html tests/web tests/test_tabs_js.py
git commit -m "feat(ui): верхние вкладки и hash-роутинг — один экран за раз"
```

---

## Задача 3: главный экран Launchpad — run view внутри панели и drill-down

**Files:**
- Modify: `dispatcher/server/static/index.html`
- Modify: `tests/web/tabs_harness.js`

**Interfaces:**
- Consumes: `navigate`, `route`, `onScreenShown` (Задача 2); существующие
  `rcOpenView(requestId)` / `#rc-run-view` (в коде Run console).
- Produces: `lpOpenRun(requestId)` — открывает run view и проставляет
  `#launchpad/<request_id>`.

**Design notes for the implementer:**

`#run-console` уже внутри `#screen-launchpad` после Задачи 2. Здесь он получает
своё место в порядке экрана и сворачивается: форма `repo_key`/`work_id` плюс
поле «Open run» — это Manual (advanced) спеки §3.2, служебный вход, а не первое,
что видит оператор.

Drill-down не заводит второй способ открыть прогон: он вызывает уже
существующую функцию открытия run view и дополнительно пишет hash. Найди в
коде функцию, которую сегодня дёргает кнопка `#rc-open`, и переиспользуй её —
не пиши вторую.

Незалинкованные прогоны (`run_id` без `request_id`) drill-down не получают: у
них нет ключа, по которому `GET /api/runs/{request_id}` вообще отвечает.

- [ ] **Шаг 1: написать падающие случаи**

В `tests/web/tabs_harness.js`:

```js
testCase('the manual form sits collapsed inside the Launchpad screen', async () => {
  await withPage(page => {
    const rc = el(page, '#run-console');
    check(!!rc.closest('#screen-launchpad'),
      'run console must live inside the launchpad panel');
    const details = rc.querySelector('details');
    check(!!details, 'the manual form must be collapsed (a <details>)');
    check(!!details && !!details.querySelector('#rc-repo-key'),
      'the repo_key input must sit inside the collapsed block');
  });
});

testCase('clicking an active run opens the run view and sets the hash', async () => {
  await withPage(async page => {
    const row = el(page, '#lp-active [data-lp-request-id="rc-active-1"]');
    await Promise.all(dispatch(row, 'click'));
    await drain();
    check(page.ctx.location.hash === '#launchpad/rc-active-1',
      `hash is the drill-down, got ${page.ctx.location.hash}`);
    check(page.calls.some(c => c.url === '/api/runs/rc-active-1'),
      'the run view fetched the run');
  }, snapshotRoute({active: [ACTIVE_LINKED]}));
});

testCase('an unlinked active run offers no drill-down', async () => {
  await withPage(page => {
    check(!maybeEl(page, '#lp-active [data-lp-request-id]'),
      'an unlinked row must not be clickable into a run view');
  }, snapshotRoute({active: [ACTIVE_UNLINKED]}));
});

testCase('a direct drill-down hash opens the run view on load', async () => {
  await withPage(page => {
    check(page.calls.some(c => c.url === '/api/runs/rc-deep'),
      'the drill-down hash fetched the run on boot');
  }, {hash: '#launchpad/rc-deep'});
});
```

`ACTIVE_LINKED` / `ACTIVE_UNLINKED` — фикстуры строк `active` в форме
`LaunchpadSnapshot` (см. `snapshot()` в `launchpad_harness.js`): у первой есть
`request_id: 'rc-active-1'`, у второй `request_id: null` и непустой `run_id`.

- [ ] **Шаг 2: прогнать до RED**

Run: `uv run pytest tests/test_tabs_js.py -v`
Expected: FAIL — `#run-console` не свёрнут, `data-lp-request-id` в строках нет.

- [ ] **Шаг 3: реализовать**

1. Обернуть содержимое `#run-console` в `<details>` с
   `<summary><h3>Manual (advanced)</h3></summary>` и переставить секцию **после**
   `#lp-recent` и **перед** `#lp-diagnostics` внутри `#screen-launchpad`.
2. В рендерах `#lp-active`, `#lp-recent` и `#lp-pending` навесить на строку с
   известным `request_id` атрибут `data-lp-request-id="${esc(requestId)}"` и
   класс-указатель; строки без `request_id` атрибут не получают.
3. Делегированный обработчик на `#launchpad`:

```js
document.getElementById("launchpad").addEventListener("click", e => {
  const row = e.target.closest("[data-lp-request-id]");
  if (!row) return;
  lpOpenRun(row.dataset.lpRequestId);
});

/** Opens the run view in place and records it in the hash. */
function lpOpenRun(requestId) {
  if (!requestId) return;
  navigate("launchpad", requestId);
  rcOpenView(requestId);      // the SAME entry point #rc-open already uses
}
```

4. В `onScreenShown` открыть прогон, когда маршрут пришёл с вложенным
   сегментом. **`onScreenShown` накапливает ветки, а не заменяется:** Задачи 4
   и 5 дописывают в неё своё и обязаны сохранить эту:

```js
function onScreenShown(r) {
  if (r.screen === "launchpad" && r.sub) rcOpenView(r.sub);
}
```

- [ ] **Шаг 4: прогнать до GREEN**

Run: `uv run pytest tests/test_tabs_js.py tests/test_launchpad_js.py tests/test_run_console_js.py -v`
Expected: PASS.

- [ ] **Шаг 5: гигиена и коммит**

```bash
uv run ruff format . && uv run ruff check . --fix
uv run pyrefly check dispatcher tests scripts
git add dispatcher/server/static/index.html tests/web/tabs_harness.js
git commit -m "feat(ui): Launchpad — главный экран, run view как drill-down"
```

---

## Задача 4: `lpState` переживает переключение вкладки, разрешение — нет

**Files:**
- Modify: `dispatcher/server/static/index.html`
- Modify: `tests/web/launchpad_harness.js`

**Interfaces:**
- Consumes: `lpState` (`pending`, `rowMessages`, `escapes`, `seq`),
  `onScreenShown`, `lpRefetchAfterAction()`.
- Produces: `lpRevalidateOpenConfirm()` — вызывается на возврате и после
  каждого применённого снапшота.

**Design notes for the implementer:**

Спека §5.5 разводит две вещи, которые легко слить в одну: **состояние**
переживает уход и возврат, **разрешение действовать** — нет. `lpState` живёт вне
DOM и уже переживает: переключение вкладки только ставит `hidden`. Работа здесь
в другом — гарантировать, что возврат не консервирует раскрытое подтверждение,
выданное по устаревшему снапшоту.

Механизм уже написан: тот же предикат, что применяется после refetch
(§5.4.3 — строка вышла из Ready, появился блокер, сменился `seen_revision`).
Задача — **вызвать** его на возврате, а не написать второй.

Не пересоздавай DOM Launchpad при возврате: `panel.hidden = false` и один
refetch. Полная перерисовка стёрла бы открытые формы escape вместе с введённой
причиной.

**`onScreenShown` дописывается, а не переписывается:** ветка
`if (r.sub) rcOpenView(r.sub)` из Задачи 3 обязана остаться.

**Хелперы, которых в `launchpad_harness.js` ещё нет** — их пишет эта задача,
рядом с существующими `click`/`htmlOf`/`callsTo`: `READY_ROW` (фикстура строки
`ready` для `REPO_READY`), `openReadyRow(page, repo)` (клик по строке,
раскрывающий подтверждение), `openReadyRowAndConfirm(page, repo)` (то же плюс
Confirm), `failTheSubmitTransport(page)` (переопределяет `/api/runs/submit` на
`Promise.reject`, чтобы получить состояние «launch outcome unknown»), и
`overrideRoute(page, url, make)` из Задачи 2 — перенеси его в
`tests/web/screens.js`, если он нужен обоим harness'ам.

- [ ] **Шаг 1: написать падающие случаи**

В `tests/web/launchpad_harness.js`:

```js
testCase('leaving and returning keeps an unresolved attempt', async () => {
  await withPage(async page => {
    await openReadyRowAndConfirm(page, 'deployer');    // helper: two-step confirm
    await failTheSubmitTransport(page);                // -> "launch outcome unknown"
    await openScreen(page, 'sync');
    await openScreen(page, 'launchpad');
    const pending = htmlOf(page, '#lp-pending');
    check(/launch outcome unknown/.test(pending),
      'the unknown-outcome row survived the round trip');
    await click(page, '#lp-pending [data-lp-retry]');
    const retries = page.calls.filter(c => c.url === '/api/runs/submit');
    const ids = new Set(retries.map(c => JSON.parse(c.opts.body).request_id));
    check(ids.size === 1, `retry reuses one request_id, saw ${ids.size}`);
  });
});

testCase('an open confirmation survives an unchanged snapshot', async () => {
  await withPage(async page => {
    await openReadyRow(page, 'deployer');
    await openScreen(page, 'sync');
    await openScreen(page, 'launchpad');
    const confirm = el(page, '#lp-ready [data-lp-confirm]');
    check(!confirm.disabled, 'Confirm stays enabled over an unchanged row');
  });
});

testCase('an open confirmation is disabled when the row changed', async () => {
  await withPage(async page => {
    await openReadyRow(page, 'deployer');
    await openScreen(page, 'sync');
    overrideRoute(page, '/api/launchpad', () => ok(snapshot({
      repositories: [{...REPO_READY, seen_revision: 'c'.repeat(40)}],
      ready: [READY_ROW],
    })));
    await openScreen(page, 'launchpad');
    const confirm = el(page, '#lp-ready [data-lp-confirm]');
    check(confirm.disabled, 'Confirm is disabled after seen_revision moved');
    check(/revision/i.test(htmlOf(page, '#lp-ready')),
      'the cause is shown, not just the disabled state');
  });
});
```

- [ ] **Шаг 2: прогнать до RED**

Run: `uv run pytest tests/test_launchpad_js.py -v`
Expected: FAIL — на возврате refetch не делается, подтверждение не
перепроверяется.

- [ ] **Шаг 3: реализовать**

```js
function onScreenShown(r) {
  if (r.screen === "launchpad") {
    // A return must not preserve a stale permission to act: refetch, then let
    // the SAME post-refetch predicate re-validate any open confirmation.
    lpRefetchAfterAction();
    if (r.sub) rcOpenView(r.sub);
  }
}
```

Если ре-валидация подтверждения сегодня живёт внутри функции применения
снапшота — ничего дописывать не нужно, `lpRefetchAfterAction()` её протянет.
Если она вызывается только на пути действия, вынеси её в
`lpRevalidateOpenConfirm()` и вызови из обоих мест.

- [ ] **Шаг 4: прогнать до GREEN**

Run: `uv run pytest tests/test_launchpad_js.py tests/test_tabs_js.py -v`
Expected: PASS.

- [ ] **Шаг 5: коммит**

```bash
git add dispatcher/server/static/index.html tests/web/launchpad_harness.js
git commit -m "fix(ui): возврат на Launchpad перепроверяет раскрытое подтверждение"
```

---

## Задача 5: развязка `refresh()` — загрузчики экранов и опрос активного

**Files:**
- Modify: `dispatcher/server/static/index.html:1921-2052` (тело `refresh()`),
  `index.html:3774` (`setInterval(refresh, 10000)`)
- Modify: `tests/web/tabs_harness.js`

**Interfaces:**
- Consumes: `activeScreen()`, `onScreenShown`.
- Produces: `LOADERS` — объект `screen id -> async () => void`;
  `loadActiveScreen()`; `SCREEN_REFRESH_MS = 10000`.

**Design notes for the implementer:**

Сегодня `refresh()` — один `Promise.all` на восемь endpoint'ов плюс отдельно
вызванные `refreshWaits()` и `refreshEpics()`. Разбирается он по границам,
которые уже есть в коде: каждый блок рендера читает ровно свой ответ.

**Названная ловушка.** Колонка `Contract` в Roadmap строится не из
`/api/roadmap`, а из `/api/contracts` (`syncByName`, `index.html:2006`).
Загрузчик Roadmap обязан тянуть **оба** и ещё `/api/roadmap/summary`. Разрежешь
по endpoint'ам вместо экранов — колонка молча опустеет, и ни один существующий
тест этого не заметит.

`refreshWaits()` и `refreshEpics()` уже автономны, со своими catch и guard —
они становятся загрузчиками почти без изменений.

`#updated` — один глобальный элемент; после развязки его пишет загрузчик
активного экрана, а не тот, кто ответил последним.

- [ ] **Шаг 1: написать падающие случаи**

```js
testCase('opening a screen fetches only its own endpoints', async () => {
  await withPage(async page => {
    const before = page.calls.length;
    await openScreen(page, 'models');
    const after = page.calls.slice(before).map(c => c.url);
    check(after.some(u => u.startsWith('/api/models')), 'models was fetched');
    check(!after.some(u => u.startsWith('/api/epics')),
      `epics must not be fetched for the models screen, saw ${after.join(',')}`);
  });
});

testCase('the periodic timer only refreshes the active screen', async () => {
  await withPage(async page => {
    await openScreen(page, 'contracts');
    const before = page.calls.length;
    page.timers.byPeriod(10000).cb();
    await drain();
    const urls = page.calls.slice(before).map(c => c.url);
    check(urls.every(u => u.startsWith('/api/contracts')),
      `only contracts refreshed, saw ${urls.join(',')}`);
  });
});

testCase('a broken endpoint on one screen leaves its neighbour alone', async () => {
  await withPage(async page => {
    overrideRoute(page, '/api/models', () => resp(500, {}));
    await openScreen(page, 'models');
    await openScreen(page, 'contracts');
    check(/in sync|drift|n\/a/.test(htmlOf(page, '#contracts')),
      'contracts rendered despite the models failure');
  });
});

testCase('the roadmap screen still fills its Contract column', async () => {
  await withPage(async page => {
    await openScreen(page, 'roadmap');
    const urls = page.calls.map(c => c.url);
    check(urls.some(u => u.startsWith('/api/contracts')),
      'roadmap loader must fetch /api/contracts for the Contract column');
    check(/in sync|drift/.test(htmlOf(page, '#roadmap')),
      'the Contract column is not empty');
  }, roadmapWithContractRoute);
});
```

- [ ] **Шаг 2: прогнать до RED**

Run: `uv run pytest tests/test_tabs_js.py -v`
Expected: FAIL — открытие любого экрана по-прежнему тянет все endpoint'ы.

- [ ] **Шаг 3: разрезать `refresh()`**

Тело `refresh()` разбирается на функции, каждая — со своим `try/catch`,
пишущая только свои узлы. Каркас:

```js
const SCREEN_REFRESH_MS = 10000;

const LOADERS = {
  launchpad: null,               // owns its own 30 s timer (LP_REFRESH_MS)
  sync:      loadSync,
  projects:  loadProjects,
  errors:    loadErrors,
  models:    loadModels,
  contracts: loadContracts,
  epics:     refreshEpics,       // already self-contained
  waits:     refreshWaits,       // already self-contained
  roadmap:   loadRoadmap,
};

async function loadActiveScreen() {
  const load = LOADERS[activeScreen()];
  if (!load) return;
  markUpdating(true);
  try { await load(); } finally { markUpdating(false); }
  document.getElementById("updated").textContent =
    "updated " + new Date().toLocaleTimeString();
}

// The Contract column of Roadmap is folded from /api/contracts, NOT from
// /api/roadmap (syncByName below). Splitting by endpoint instead of by screen
// would empty that column silently.
async function loadRoadmap() {
  try {
    const [roadmap, summary, contracts] = await Promise.all([
      get("/api/roadmap"), get("/api/roadmap/summary"), get("/api/contracts"),
    ]);
    renderSummary(summary);
    renderRoadmap(roadmap, contracts);
  } catch (err) {
    document.getElementById("roadmap-names").textContent = "roadmap failed: " + err;
  }
}
```

`renderRoadmap(roadmap, contracts)` — существующий код из `refresh()`
(построение `syncByName` и таблицы), вынесенный без изменения логики.
Аналогично `loadSync` → `renderSync`, `loadErrors` → таблица `#errors` и
`renderServiceOptions()`, `loadModels`, `loadContracts`, `loadProjects` → грид
`#projects`.

Замена цикла опроса:

```js
onScreenShown(route);            // applyRoute already called it; loaders now run
let screenTimer = null;
function restartScreenTimer() {
  if (typeof setInterval !== "function") return;
  if (screenTimer !== null) clearInterval(screenTimer);
  screenTimer = setInterval(loadActiveScreen, SCREEN_REFRESH_MS);
}
```

`onScreenShown` **дополняется** вызовом `loadActiveScreen()` и
`restartScreenTimer()` — ветки Задач 3 и 4 (`rcOpenView(r.sub)`,
`lpRefetchAfterAction()`) остаются на месте. Старая строка `refresh(); setInterval(refresh, 10000);`
удаляется целиком; сама `refresh()` удаляется после того, как её последний
вызов исчез — оставленная «на всякий случай», она снова начнёт тянуть всё.

- [ ] **Шаг 4: прогнать до GREEN**

Run: `uv run pytest tests/test_tabs_js.py -v && uv run pytest -k "_js" -q`
Expected: PASS.

- [ ] **Шаг 5: гигиена и коммит**

```bash
uv run ruff format . && uv run ruff check . --fix
uv run pyrefly check dispatcher tests scripts
git add dispatcher/server/static/index.html tests/web/tabs_harness.js
git commit -m "perf(ui): опрашивается только активный экран; refresh разобран на загрузчики"
```

---

## Задача 6: Launchpad опрашивается в фоне, пока есть незакрытая попытка

**Files:**
- Modify: `dispatcher/server/static/index.html`
- Modify: `tests/web/launchpad_harness.js`

**Interfaces:**
- Consumes: `lpState.pending`, `activeScreen()`, `lpRefreshTimer`, `LP_REFRESH_MS`.
- Produces: `lpShouldPoll() -> boolean`.

**Design notes for the implementer:**

Спека §9.1: скрытый Launchpad продолжает опрашивать `/api/launchpad`, **пока в
`lpState.pending` есть незакрытая попытка**, и умолкает сразу после
терминального результата или снятия uncertainty. Без этого «launch outcome
unknown» замирает, пока оператор смотрит другой экран.

Правило проверяется в самом тике, а не при переключении вкладки: запись в
`pending` может закрыться между тиками, и остановка по событию переключения её
не поймает.

Второго таймера не заводить — у Launchpad уже свой на 30 с; меняется только
условие, при котором его тик делает запрос.

`SCREEN_REFRESH_MS` в harness'е — литерал `10000` рядом с существующим
`LP_REFRESH_MS`; таймеры различаются периодом, а не порядком регистрации.

- [ ] **Шаг 1: написать падающие случаи**

```js
testCase('a hidden Launchpad keeps polling while an attempt is unresolved', async () => {
  await withPage(async page => {
    await openReadyRowAndConfirm(page, 'deployer');
    await failTheSubmitTransport(page);
    await openScreen(page, 'sync');
    const before = callsTo(page, '/api/launchpad');
    page.timers.byPeriod(LP_REFRESH_MS).cb();
    await drain();
    check(callsTo(page, '/api/launchpad') === before + 1,
      'the hidden panel refetched while pending was open');
  });
});

testCase('a hidden Launchpad stops polling once nothing is pending', async () => {
  await withPage(async page => {
    await openScreen(page, 'sync');
    const before = callsTo(page, '/api/launchpad');
    page.timers.byPeriod(LP_REFRESH_MS).cb();
    await drain();
    check(callsTo(page, '/api/launchpad') === before,
      'a hidden panel with nothing pending must not poll');
  });
});

testCase('the visible Launchpad polls as before', async () => {
  await withPage(async page => {
    const before = callsTo(page, '/api/launchpad');
    page.timers.byPeriod(LP_REFRESH_MS).cb();
    await drain();
    check(callsTo(page, '/api/launchpad') === before + 1, 'visible panel polls');
  });
});

testCase('/api/launchpad has exactly one poller', async () => {
  await withPage(async page => {
    const before = callsTo(page, '/api/launchpad');
    page.timers.byPeriod(LP_REFRESH_MS).cb();
    page.timers.byPeriod(SCREEN_REFRESH_MS).cb();
    await drain();
    check(callsTo(page, '/api/launchpad') === before + 1,
      'the screen timer must not fetch the launchpad snapshot');
  });
});
```

- [ ] **Шаг 2: прогнать до RED**

Run: `uv run pytest tests/test_launchpad_js.py -v`
Expected: FAIL — скрытая панель либо опрашивает всегда, либо не опрашивает
никогда (в зависимости от того, что вышло из Задачи 5).

- [ ] **Шаг 3: реализовать**

```js
// Spec §9.1: the ONE exception to active-only polling. An unresolved attempt
// is the state the operator must not silently lose; anything else, hidden
// means quiet.
function lpShouldPoll() {
  if (activeScreen() === "launchpad") return true;
  return Object.keys(lpState.pending).length > 0;
}
```

и в теле тика таймера Launchpad — ранний выход `if (!lpShouldPoll()) return;`
перед запросом.

- [ ] **Шаг 4: прогнать до GREEN**

Run: `uv run pytest tests/test_launchpad_js.py tests/test_tabs_js.py -v`
Expected: PASS.

- [ ] **Шаг 5: коммит**

```bash
git add dispatcher/server/static/index.html tests/web/launchpad_harness.js
git commit -m "feat(ui): скрытый Launchpad опрашивается, пока есть незакрытая попытка"
```

---

## Задача 7: условная вкладка Benchmarks, документация, полный прогон

**Files:**
- Modify: `dispatcher/server/static/index.html`
- Modify: `tests/web/tabs_harness.js`, `tests/web/benchmarks_harness.js`,
  `tests/web/run_status_harness.js`
- Modify: `README.md` (раздел про навигацию панели)

**Interfaces:**
- Consumes: `SCREENS`, `screenIds()`, `renderBenchmarks(status)`.
- Produces: `setBenchmarksAvailable(bool)`.

**Design notes for the implementer:**

Сегодня `renderBenchmarks` снимает `hidden` с секции ровно тогда, когда
`report.status !== "unconfigured"` (`index.html:539-541`). Спека §3.1 поднимает
ровно эту видимость на уровень навигации: вкладка присутствует тогда и только
тогда, когда профиль сконфигурирован. На несконфигурированном стенде владелец
видит девять вкладок — как и обещано.

Один край, который нельзя пропустить: профиль пропал, а активной была вкладка
`Benchmarks`. Экран падает на `Launchpad` — иначе оператор остался бы на панели,
кнопки которой в tablist больше нет.

Ответ `/api/benchmarks` нужен, чтобы решить про вкладку, но опрашивать его с
любого экрана — значит вернуть общий refresh. Решение: один запрос при загрузке
страницы решает наличие вкладки; дальше вкладку обновляет её собственный
загрузчик, когда она активна.

- [ ] **Шаг 1: написать падающие случаи**

```js
// The tab button stays in the static markup and is toggled with `hidden`:
// a hidden button is out of the a11y tree and `dispatch()` refuses to click
// it, so "present in the tablist" is measured over VISIBLE tabs.
const visibleTabs = page =>
  page.document.querySelectorAll('#tablist button').filter(b => !b.hidden);

testCase('no Benchmarks tab when the profile is unconfigured', async () => {
  await withPage(page => {
    check(el(page, '#tab-benchmarks').hidden, 'the benchmarks tab is hidden');
    check(visibleTabs(page).length === 9,
      `nine visible tabs, got ${visibleTabs(page).length}`);
  });
});

testCase('a configured profile adds the Benchmarks tab last', async () => {
  await withPage(async page => {
    const tabs = visibleTabs(page);
    check(tabs.length === 10, `ten visible tabs, got ${tabs.length}`);
    check(tabs[tabs.length - 1].attributes.id === 'tab-benchmarks',
      'benchmarks is last');
    await openScreen(page, 'benchmarks');
    check(!el(page, '#screen-benchmarks').hidden, 'the benchmarks panel opened');
  }, configuredBenchmarksRoutes);
});

testCase('losing the profile drops the active Benchmarks screen to Launchpad',
  async () => {
    await withPage(async page => {
      await openScreen(page, 'benchmarks');
      overrideRoute(page, '/api/benchmarks', () => ok({
        fetch_in_flight: false, report: {status: 'unconfigured', url: null,
          fetched_at: null, error: null, benchmarks: [], leaderboards: {}},
      }));
      page.timers.byPeriod(SCREEN_REFRESH_MS).cb();
      await drain();
      check(!el(page, '#screen-launchpad').hidden, 'fell back to launchpad');
      check(el(page, '#tab-benchmarks').hidden, 'the tab is hidden again');
    }, configuredBenchmarksRoutes);
  });
```

- [ ] **Шаг 2: прогнать до RED**

Run: `uv run pytest tests/test_tabs_js.py -v`
Expected: FAIL — вкладки `benchmarks` нет ни при каком ответе.

- [ ] **Шаг 3: реализовать**

1. Обернуть `#benchmarks-section` в `<div id="screen-benchmarks" class="screen"
   role="tabpanel" aria-labelledby="tab-benchmarks" hidden>` и добавить в
   `<nav>` кнопку `#tab-benchmarks` с `hidden`.
2. Ввести флаг и учесть его в `screenIds()`:

```js
let benchmarksAvailable = false;

function screenIds() {
  return SCREENS.map(s => s.id)
    .filter(id => id !== "benchmarks" || benchmarksAvailable);
}

/** The tab exists exactly when the section used to un-hide itself. */
function setBenchmarksAvailable(available) {
  if (available === benchmarksAvailable) return;
  benchmarksAvailable = available;
  const tab = document.getElementById("tab-benchmarks");
  if (tab) tab.hidden = !available;
  // An operator standing on a screen whose tab just vanished has nowhere to
  // click back from.
  if (!available && route.screen === "benchmarks") navigate(DEFAULT_SCREEN, null);
}
```

3. Добавить `benchmarks` в `SCREENS` последним и в `LOADERS` — загрузчик,
   который зовёт `renderBenchmarks` и `setBenchmarksAvailable`.
4. На загрузке страницы — один запрос `/api/benchmarks`, решающий наличие
   вкладки.

- [ ] **Шаг 4: прогнать до GREEN и полный набор**

```bash
uv run pytest tests/test_tabs_js.py -v
uv run ruff format . && uv run ruff check . --fix
uv run pyrefly check dispatcher tests scripts
uv run pytest -q
```

Expected: зелено, кроме известной базы (три live-smoke + два предупреждения).

- [ ] **Шаг 5: документация**

В `README.md`, в разделе про web-панель, заменить описание единой страницы на
строку вкладок: перечислить девять экранов в порядке реестра, назвать
`Launchpad` экраном по умолчанию, сказать, что run view открывается изнутри
Launchpad, и что `Benchmarks` появляется только при сконфигурированном
eco-профиле. Не описывать Projects и Sync как переработанные — это PR-2 и PR-3.

- [ ] **Шаг 6: ручная визуальная приёмка**

Поднять панель, проверить на 1440 px и 390 px: строка вкладок прокручивается и
не переносится; активная вкладка видна не только цветом; Left/Right и
Enter/Space работают; прямые ссылки на каждый hash открывают свой экран;
`#launchpad/<request_id>` открывает прогон; уход с Launchpad с открытым
pending и возврат сохраняют строку и перепроверяют подтверждение.

- [ ] **Шаг 7: коммит и PR**

```bash
git add dispatcher/server/static/index.html tests/web README.md
git commit -m "feat(ui): условная вкладка Benchmarks + документация навигации"
git push -u origin feat/tabbed-ui
gh pr create --title "feat(ui): вкладки и главный экран Launchpad (PR-1)" --body …
```

В теле PR **первой строкой** назвать изменение доступности: прямого экрана
`Run console` больше нет, run view открывается только изнутри Launchpad.

---

## Self-review: покрытие спеки

| Раздел спеки | Задача |
|---|---|
| §3.1 порядок вкладок и дефолт | 2 (девять), 7 (условная десятая) |
| §3.2 Run console — вложенный инструмент | 3 |
| §3.3 один tabpanel, `#ta-outcomes` вне панелей | 2 |
| §4 ARIA, клавиатура, responsive | 2 (ARIA, клавиатура), 7 шаг 6 (responsive) |
| §4.1 роутинг, вложенный hash, закрытая грамматика | 2 (грамматика, fallback), 3 (`#launchpad/<id>`) |
| §5.1 состав экрана | 2 (перенос), 3 (порядок, Manual свёрнут) |
| §5.2 Pending launches | 4 |
| §5.3 drill-down | 3 |
| §5.4 инварианты переноса | 2 (перенос без правки внутреннего HTML), 4 (ре-валидация) |
| §5.5 жизненный цикл при переключении | 4 |
| §8 неизменяемые экраны | 2 |
| §9 реестр загрузчиков, `updated`, зависимость Roadmap→contracts | 5 |
| §9.1 исключение опроса Launchpad | 6 |
| §10 тестовая среда | 1 (модель браузера), 2 (`screens.js`, девять harness'ов) |
| §11 web-harness кейсы | 2, 3, 4, 5, 6, 7 |
| §13 критерии 1–7, 11–14 | все задачи; 8–10 — PR-2/PR-3 |

§6 (Projects) и §7 (Sync) в этот план не входят по §12: они PR-2 и PR-3.
