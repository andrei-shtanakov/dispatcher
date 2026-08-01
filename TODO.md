# TODO — dispatcher (заведён 2026-07-26)

> Роль в экосистеме: **read-плоскость** — дашборд контроля/мониторинга (web + TUI +
> VSCode + MCP) поверх on-disk артефактов наблюдаемых репо. Мутации — только по явному
> клику человека и только через PR (`github-checker`); ADR-ECO-004 D1: dispatcher
> «only *shows* and *launches PR-only actions* — never a second SSOT».
> Обзор проекта: `COWORK_CONTEXT.md`; канонический бэклог реализации: `spec/tasks.md`
> (+ `spec/requirements.md`, `spec/design.md`); спеки и планы фич:
> `docs/superpowers/{specs,plans}/`.
> Повод завести файл: замер 2026-07-26 показал, что dispatcher — одно из зеркал без
> план-файла в корне, то есть его оставшаяся работа была невидима дайджесту Robin
> (`../_cowork_output/2026-07-26-plan-fields-and-todo-coverage-handoff.md` §2).

## Правила ведения

- После выполненной задачи — `[x]` и номер PR / хеш коммита.
- Задача стала неактуальной — зачеркнуть `~~...~~` с пометкой **почему**, не удалять:
  дельта-счётчики Robin читают исчезновение строки как «закрыто».
- Здесь — **только пункты уровня команды и кросс-проектные**. Микрошаги реализации
  живут в `spec/tasks.md` (TASK-NNN) и `docs/superpowers/plans/`; дайджест их намеренно
  не читает.
- Поля пункта — инлайн-теги `@owner:` / `@blocked_by:<repo>#<slug>` / `@trigger:"…"`
  (формат — §3 handoff-ноты 2026-07-26). Все три опциональны: пустое поле означает
  «неизвестно» и само измеримо — это честнее выдуманного владельца или триггера.
- `@id:<node-id>` — канонический идентификатор пункта (ADR-ECO-005 PF-2B): строчная
  грамматика `[a-z0-9][a-z0-9._-]{0,63}`, из него строится URI `todo://dispatcher/<id>`.
  Переходно `@blocked_by` принимает и legacy `<repo>#<slug>`, и канонический
  `todo://<repo>/<id>`.
- **Теги и суть пункта — на одной строке с `- [ ]`**: парсер (`plan_state._UNCHECKED`)
  разбирает пункт строго построчно, продолжения ниже он не видит. Отступленные строки
  под пунктом — контекст для человека.
- Правку в соседнем репо здесь не планируем как свою работу: кросс-репный пункт — это
  **handoff** (см. `CLAUDE.md`, scope & boundaries).

## Текущее состояние

- ✅ Stage 1–3: JSON API + web-дашборд, TUI (textual), VSCode-расширение (`vscode-ext/`)
- ✅ Roadmap-модуль M1–M3: read-model поверх vault-YAML, drift-проекция, `last_seen`,
  агрегации phases/blockers, `owner_role` pass-through (PR #6, #7, #13)
- ✅ Итерация «sync & roadmap» M1+M2: вендоренный snapshot-контракт v1, verdict-движок,
  фоновый fetch, `dispatcher publish-snapshot`, auto-discovery предложений, живые
  whitelist-действия, VSCode status-bar вердикт (TASK-201..211)
- ✅ Spec-runner config editor: чтение + запись через
  `github-checker propose-pr --edit --if-match`, ноль записей в живое дерево
  (PR #37, #40); tri-state `extra_executor_config` + web-overlay редактор (PR #63)
- ✅ FR-04 onboarding-view (web/TUI/MCP + VSCode, PR #58, #59, #65), FR-05 MCP-сервер
  (`dispatcher mcp`, read-only whitelist, PR #52, #53), FR-06 паритет TUI/VSCode
  (PR #44, #47), DESIGN-307 AI-подсказки через локальный claude-CLI (PR #61)
- ✅ Governance-гейт экосистемы принят (ADR-ECO-004 D5): `governance / gate` как
  обязательный чек + least-privilege permissions в caller (PR #66, #67)
- ✅ Merge gate (2026-07-30): чтение PR (`github-checker pr-detail`, девять
  предикатов гейта) + gated squash-merge (`merge --if-head`) + `post-merge-sync`
  одним локом на репо; `merged` трёхзначный (`true`/`false`/`null` — транспортный
  сбой остаётся неизвестностью, не заявленным «не смержено»); вход — ручной ввод
  номера PR, не список (PR #93)
- ✅ Task-authoring console (2026-07-31, ADR-ECO-004a): «Create task request» на
  панели проекта заводит **только** `inbox`-issue в целевом репо через
  `github-checker issue-lookup` (lock-free read) / `issue-create` (locked write);
  не правит `TODO.md`, не принимает запрос от имени владельца, не открывает PR,
  не запускает executor. `from:` пишет сервер, не форма. `created` трёхзначный
  как `merged` выше (`null` — попытка мутации сломалась, не «не создано»;
  единственный follow-up на unknown — Re-check). Create запрещён **пока
  состояние неизвестно и пока Re-check его не разрешит**, а не навсегда:
  определённый ответ lookup снимает предупреждение и решает судьбу кнопки
  (явный `[]` → Create снова доступен, найденный issue → остаётся выключен),
  неопределённый (транспорт, non-ok, нечитаемый конверт, битый элемент)
  ничего не меняет — unknown сохраняется. Формулировка сверена с README
  (2026-07-31); «никогда повторный Create» было неверно. `matches` из
  lookup аналогично различает `null` (inbox прочитан не полностью) и `[]`
  (подтверждённо пусто). Клиентские правила экрана гоняются под Node
  (`tests/web/`) из Python-сьюта — Node обязателен, при его отсутствии тест
  падает, не скипается (PR #96)
- 🔜 Открытого продуктового scope из discovery-брифа (FR-01..06) не осталось; ниже —
  кросс-проектные точки и хвосты качества.

---

## Governance-плоскость (ADR-ECO-004)

- [ ] Governance view: рендерить `declared vs observed` enforcement-зрелость правил (ADR-ECO-004 D3, Batch 2 §6) @owner:andrei @blocked_by:prograph-vault#governance-observed-derived @trigger:"в prograph-vault появился derived/governance/ с observed-зрелостью" @id:governance-declared-vs-observed
      Declared-сторона готова: `../prograph-vault/authored/registry/governance.yaml`
      (v1, 2026-07-18) и сам файл называет ожидаемый путь observed —
      `derived/governance/`, который на 2026-07-26 не существует. Сравнивать не с чем,
      поэтому пункт заблокирован, а не «в работе»: dispatcher здесь рендерер, не
      источник, и читать «на будущее» нечего.

## Кросс-репные контракты

- [ ] `contracts/executor-config/v0-provisional`: довести до реального потребителя либо явно пометить контракт отложенным @owner:andrei @blocked_by:todo://maestro/specrunnerconfig-passthrough @trigger:"Maestro начал читать contracts/executor-config" @id:executor-config-consumer
      Статус-обзор экосистемы 07-24 назвал это watch-item: схема запинена (DESIGN-301),
      но единственная ссылка в экосистеме — план-док Maestro
      `2026-07-17-specrunnerconfig-passthrough.md`, потребителя нет. Риск — зомби-пин,
      который «застыл» без интеграции. Наша часть — решение: ждать потребителя или
      написать в README контракта, что он отложен.
- [ ] Заморозить схемы MCP-тулзов (вендоринг пиненой копией по дисциплине ADR-ECO-003) @owner:andrei @trigger:"robin или Maestro начали вызывать dispatcher mcp" @id:freeze-mcp-tool-schemas
      Схемы 15 тулзов сознательно UNSTABLE: фиксировать контракт до первого потребителя
      значит заморозить угаданную форму.
- [x] Вендорить `contracts/actions/v1`, когда github-checker его опубликует — PR #97 @owner:andrei @blocked_by:github-checker#contracts-actions-v1 @trigger:"github-checker опубликовал контракт действий" @id:vendor-contracts-actions-v1
      Пиненая копия из `github-checker@ef03fef` (36 файлов поверхности: схема, README,
      34 фикстуры; per-file sha256 + `tree_sha256` в `manifest.json`), извлечена из
      git object database. Единственный вход — `ingest(raw, *, returncode)`; сошлись
      все три parse-path'а. Контракт действий больше не «поведение бинаря»:
      absent/`null`/`false` держатся через `model_fields_set`, а не через дефолты.

## Хвосты качества

- [x] Прогнать live-смоук write-path с реальным `github-checker` на PATH — PR #98 @owner:andrei @id:live-smoke-write-path
      Выполнено 2026-08-01 локально: `test_write_path_live_smoke_real_binary` прошёл на
      `dispatcher@b571cba` против бинаря, установленного из `github-checker@ef03fef`
      (тот же коммит, на который запинен вендоренный контракт). Сюита — 650 passed,
      0 skipped. Отдельно от этого пункта CI-сторона закрыта в
      `@id:design-405-level-3-never-runs`.
- [ ] Route-level тест сериализации `ok=True` для четырёх additive-полей `ActionOutcome` @owner:andrei @id:action-outcome-serialization-test
      Принятый follow-up PR #40.
- [ ] ruamel: standalone-комментарий сразу после блока `spec_runner` теряется при ре-рендере `project.yaml` @owner:andrei @id:ruamel-standalone-comment-loss
      Деградация fail-visible (лишний PR вместо no-op), но это потеря пользовательского
      текста в чужом репо.
- [ ] README + иконка для `vscode-ext/` @owner:andrei @id:vscode-ext-readme
      Страница расширения показывает «No README available».
- [ ] `loadSpecRunnerConfig` (`dispatcher/server/static/index.html`) берёт `repoDir` из display-имени коллектора и кормит им три directory-keyed эндпоинта — работает только на case-insensitive FS @owner:andrei @id:spec-runner-config-dir-name-mismatch
      Найдено при зачистке merge-gate-console (2026-07-30): та же природа, что и
      только что закрытый хэзард на входе в merge gate, но здесь не тронуто —
      pre-existing и вне скоупа этой ветки. Три эндпоинта хотят каталог
      (`GET /api/projects/{name}/spec-runner-config`, `POST .../suggest`, `dir`
      у `POST /api/actions/update-spec-runner-config`, который резолвит
      `root / repo_dir / "project.yaml"`), а остальные чтения панели —
      имя; коллектор отдаёт `Maestro`, канон каталога — `maestro`
      (см. `maestro-double-name` ниже). Фикс — не переименование поля: этим
      трём нужен каталог, остальным — имя, они расходятся сознательно.
- [ ] Merge-gate: список открытых PR по репо (номер + заголовок), чтобы `#merge-gate` открывался кликом, а не ручным вводом номера @owner:andrei @id:merge-gate-pr-listing
      Task 4 (2026-07-30) исходно планировала клик по существующему PR-рендерингу
      в карточке проекта — такого рендеринга нет: read-модель несёт GitHub-состояние
      только как непрозрачный `github: dict[str, Any]` (`core/snapshot_contract.py`),
      номер PR нигде не всплывает, и ни один таск плана его не заводит. S1 вместо
      этого — ручной ввод номера PR рядом с панелью project-detail (аддитивно, без
      нового backend). Реальный список требует поля read-модели + endpoint + UI;
      см. `docs/superpowers/plans/2026-07-30-merge-gate-console.md` Task 4 Step 3.
- [ ] `ActionRunner._invoke` ловит вокруг `subprocess.run` только `FileNotFoundError`/`TimeoutExpired`/`JSONDecodeError`/`ValidationError` — гарантия «аудит-строка на каждую попытку» пробивается ещё двумя исключениями @owner:andrei @id:actions-envelope-catch-too-narrow
      Найдено same-class-свипом финального ревью merge-gate-console (2026-07-30,
      S-1, pre-existing, Minor). `subprocess.run(..., text=True)` декодирует строго,
      поэтому продюсер, который **реально отработал** и написал в stdout не-UTF-8
      байт, роняет `UnicodeDecodeError` внутри `_invoke` — мимо `_audit_outcome`,
      то есть ровно тот сценарий, ради которого заведён guard на `ValidationError`,
      но через другое исключение. Тем же путём уходит не-`FileNotFoundError`
      `OSError` на exec (безобидно: ничего не запускалось). Наружу поведение
      остаётся безопасным — экран показывает «unknown, check the PR», — поэтому
      Minor, а не блокер.
      Рядом уже лежит более сильный образец: `core/spec_runner_config_actions.py`
      (~212-244) заворачивает свой `_invoke` в сплошной `except Exception` →
      failed outcome → аудит-строка. Два класса действий охраняют конверт на разную
      глубину, и это расхождение — из тех, что тихо становятся постоянными.
      Фикс — расширить catch вокруг `subprocess.run` до той же глубины.
      **ЗАКРЫТО 2026-07-31** (ревью task-authoring, PR #96),
      все три пути:
      • NUL в argv (JSON пропускает его как escape `\u0000`, т.е. приходит прямо
        с провода) → `_argv_refusal()` до `subprocess.run` → pre-mutation refusal
        (`created=False`) с аудит-строкой;
      • не-`FileNotFoundError` `OSError` на exec → catch расширен до
        `(OSError, TimeoutExpired)`, симметрично продюсеру (реальная репродукция:
        E2BIG на переразмеренном `--if-head`);
      • `UnicodeDecodeError` на stdout продюсера, который **реально отработал** →
        `text=True` убран, декодирование вынесено отдельным шагом ПОСЛЕ
        `subprocess.run`. Это было не «ещё одно исключение»: `UnicodeDecodeError`
        **является** `ValueError`, поэтому первый фикс ловил его в pre-fork ветку и
        помечал завершившийся прогон как «refused before launch» с `created=False` —
        экран после этого включал Create, т.е. ровно тот дубликат, против которого
        сделана фича. Классификация теперь структурная (по стороне fork), а не по
        типу исключения; post-run decode → `created=None` (unknown).

- [ ] Вёрстка экранов (`<style>` в `<head>` у `index.html`) вне досягаемости
      Node-харнесса: удаление всего блока оставляет сьют зелёным
      @owner:andrei @id:web-harness-does-not-see-css
      Найдено финальным ревью ветки task-authoring (2026-07-31, coverage-gap,
      не блокер). Харнесс исполняет `<script>` из `<body>` над распарсенной
      разметкой и проверяет поведение, а не внешний вид; CSS он не применяет и
      применять не должен — `tests/web/dom.js` не layout-движок. Значит про
      «экран не разъехался» сьют не говорит ничего, и говорить не начнёт без
      настоящего браузера (Playwright + скриншот-диффы). Записано, чтобы зелёный
      прогон не читался как «вёрстка проверена».
- [ ] `tests/web/dom.js` — рукописная, без зависимостей DOM-заглушка для
      task-authoring Node-харнесса; `jsdom` был бы предпочтительнее, но его нет
      осознанно, а не по недосмотру @owner:andrei @id:web-tests-hand-rolled-dom
      Python-сьют не ставит npm-пакеты (`uv`-only дисциплина этого репо), поэтому
      `jsdom` как test-only devDependency потребовал бы отдельного `npm install`
      шага в CI и локально — цена, которую задача не оправдала. Компромисс держит
      харнесс зависимым только от `node` на PATH; если объём/сложность DOM-стабов
      вырастет, `jsdom` стоит пересмотреть.

- [x] `DESIGN-405` уровень 3 (`test_write_path_live_smoke_real_binary`) теперь
      выполняется в CI: `skipif` снят, бинарь ставится на пине — PR #98
      @owner:andrei @id:design-405-level-3-never-runs
      Закрыто 2026-08-01 по факту CI, а не по локальному прогону: в обоих джобах
      `test (3.12)` и `test (3.13)` в логе есть
      `installing github-checker @ ef03fefcded37676b19ef1c6f88b956a09a26d3f`, затем
      `test_write_path_live_smoke_real_binary PASSED`. Скрипт
      `scripts/install_pinned_checker.sh` берёт коммит из вендоренного манифеста,
      так что ре-вендоринг двигает и бинарь; личность бинаря доказывается PEP 610
      (`direct_url.json`), потому что у продюсера нет ни `--version`, ни коммита
      в выводе. Отсутствие бинаря и чужой коммит краснят джоб — проверено
      настоящей установкой `4532a8a`. Теперь совпадает с
      `tests/test_task_authoring_js.py`, который тоже падает, а не скипается.
      Осталась несвязанная ступень того же класса:
      `test_pf6_drift.py::test_real_vendored_copy_is_in_sync_with_canon` скипается
      в CI, потому что соседний canon-репо там не чекаутится — закрыто ниже.

- [x] PF-6: развести две гарантии, сидевшие в одном `in_sync` — PR #99 @owner:andrei @id:pf6-split-integrity-and-drift
      Тест скипался в CI не по недосмотру: он сравнивал завендоренную копию с
      **живым рабочим деревом** соседа, то есть отвечал «апстрим не уехал?», а
      назывался и использовался как «пин цел». Чекаут соседа стоял на `03affef`,
      `PINNED.txt` называл `db6c7a6`, и тест был зелёным — совпало содержимое
      plan-fields между этими коммитами. Зелёный означал буквально «копия
      совпадает с тем, что оказалось на этой машине».
      Теперь `vendored_integrity` — offline, consumer-owned, в обычном PR-гейте,
      читает только свою копию (per-file хэши, покрытие множества в обе стороны,
      `tree_sha256`, формат `PINNED.txt`), никогда не скипается; утверждает ровно
      внутреннюю согласованность копии с её манифестом, а связь с vault-коммитом
      — reviewable provenance, не крипто-аттестация. `upstream_drift` —
      наблюдение, отвечает только при явно переданном canon-чекауте, иначе `null`
      = неизвестно (не «в синхроне»), и живёт в отдельном scheduled-workflow
      `upstream-drift.yml`: движущийся default branch, запись resolved SHA,
      remote и пересчитанного tree-хэша, `unavailable` ≠ `no drift`, не required
      и не на PR. Governance-evidence читает integrity, drift-вью — наблюдение.
      Красный advisory = нужен осознанный re-vendor PR, а не правка хэша руками.
      Попутно: `PINNED.txt` исключён из манифеста и до сих пор не сверялся ничем.

## Наблюдения (работу не начинаем, пока не сработает триггер)

- [ ] Пин `fastmcp<3` и три отклонённых GHSA (OpenAPI SSRF, OAuth proxy, Gemini-CLI injection) — переоценить @owner:andrei @trigger:"бамп fastmcp до 3.x" @id:fastmcp-pin-reeval
      Отклонены обоснованно: путь только stdio, без OpenAPI/OAuth/HTTP. Патчи есть лишь
      в 3.2.0, а пин `<3` держит мажор общим с Maestro.
- [ ] Двойное имя Maestro: коллектор отдаёт `"Maestro"`, канон репо — `maestro` @owner:andrei @trigger:"каталог Maestro/ переименован в maestro/ на диске" @id:maestro-double-name
      Детект коллектора content-based (`maestro/` + `pyproject.toml`), поэтому
      переименование каталога discovery не сломает — но в Sync-вкладке repo придёт под
      именем каталога, и два пространства имён разойдутся. Тот же класс, что discovery-имя
      vs service-id у proctor: слепое переименование запрещено, нужна осознанная
      нормализация.
- [ ] Handoff в arbiter по ошибке `agent_id` в `report_benchmark` @owner:andrei @id:arbiter-agent-id-handoff
      Дефект соседа, который dispatcher только показывает; наша часть — написать
      handoff/issue, править чужой репо нельзя.
