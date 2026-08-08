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
- Поля пункта — инлайн-теги `@owner:<principal>` / `@blocked_by:<reference>` /
  `@trigger:"…"`. Канонические владельцы: `github:<login>`,
  `github-team:<org>/<team>`, `repo:<manifest-key>` или литерал `TBD`.
  Отсутствующий `@owner` означает `missing`, а `@owner:TBD` — явно отложенное
  назначение; это разные измеримые состояния. Каноническая ссылка блокера —
  `todo://<repo>/<id>`.
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

- [ ] Governance view: рендерить `declared vs observed` enforcement-зрелость правил (ADR-ECO-004 D3, Batch 2 §6) @owner:github:andrei-shtanakov @blocked_by:prograph-vault#governance-observed-derived @trigger:"в prograph-vault появился derived/governance/ с observed-зрелостью" @id:governance-declared-vs-observed
      Declared-сторона готова: `../prograph-vault/authored/registry/governance.yaml`
      (v1, 2026-07-18) и сам файл называет ожидаемый путь observed —
      `derived/governance/`, который на 2026-07-26 не существует. Сравнивать не с чем,
      поэтому пункт заблокирован, а не «в работе»: dispatcher здесь рендерер, не
      источник, и читать «на будущее» нечего.
- [x] WS-005 WS-B: вендор `gate-verdicts/v1` + governance-collector (6 состояний бандла) — PR #107 @owner:github:andrei-shtanakov @id:ws005-governance-collector
      Принятие inbox-issue #106 от steward (ADR-ECO-006). Канон:
      `steward/contracts/gate-verdicts/v1` @ `4836345`; копия —
      `contracts/steward-gate-verdicts/v1/` с раздельными copy-integrity
      (PR-гейт) и upstream-drift (scheduled). Collector читает
      `<repo>/.steward/gate_verdicts.jsonl` + git-факты и только
      классифицирует (ARCH-C1/C3): pass | blocked | no-data | unreadable |
      stale | unresolvable. Панель — WS-C, отдельная inbox-issue после.
- [x] WS-005 WS-C: governance-панель — read-only UI поверх collect_governance — PR #109 @owner:github:andrei-shtanakov @id:ws005-governance-panel
      Принятие inbox-issue #108 (ADR-ECO-006, from: steward). Панель потребляет
      только read model `dispatcher/core/governance.py` (ARCH-C4 — файл
      `.steward/gate_verdicts.jsonl` напрямую не читает), маршруты — только GET
      (ARCH-C2/BEH-09), UI-половины BEH-01/07, сквозной smoke с настоящим
      steward-бинарём на пине контракта. Критерии: M-01 (ни один класс
      повреждено/устарело/нерезолвимо не рендерится как pass), M-02 (блокер
      бандла виден с одного экрана).

## Кросс-репные контракты

- [x] Вендор steward gate-catalog v1: пиненая копия `profiles/gate-catalog.yaml` + канонический словарь obligation в governance-коллекторе — PR #126 @owner:github:andrei-shtanakov @id:vendor-gate-catalog
      Принятие inbox-issue #125 от steward (ADR-ECO-006, D7 дизайна
      gate-id-catalog). Канон: `steward/profiles/gate-catalog.yaml` @
      `c26ca38` (SSOT стабильных gate_id, v1: 19 active/quality +
      GC-APPROVAL-MISSING declared/approval); копия —
      `contracts/steward-gate-catalog/v1/` с раздельными copy-integrity
      (PR-гейт, `tests/test_gate_catalog_vendor.py`) и upstream-drift
      (scheduled `drift-steward-gate-catalog`). Потребитель:
      `core/gate_catalog.py` + валидация `obligation` finding-записей в
      `core/governance.py` по словарю каталога — значение вне словаря =
      `unreadable`, fail-closed (NFR-02), отсутствующее значение валидно
      (старый producer). Ре-вендор: `docs/revendor-steward-gate-catalog.md`.
- [ ] Ре-вендор `gate-verdicts/v1` бандлом steward: README-активация obligation + fixtures с obligation + stale-фраза в SCHEMA @owner:github:andrei-shtanakov @trigger:"steward обновил fixtures+SCHEMA gate-verdicts одним бандлом (inbox #125 п.3) — либо красный drift-steward-gate-verdicts" @id:revendor-gate-verdicts-obligation-bundle
      Из inbox #125 п.2–3: README контракта уже изменён точечно на пине
      `c26ca38` (активация obligation), SCHEMA.json не тронут побайтово —
      advisory `drift-steward-gate-verdicts` до ре-вендора может гореть, и
      это осознанно. Steward обновит fixtures (сейчас несут findings без
      obligation — схемно валидно, но уже не образец producer-вывода) и
      stale-фразу в SCHEMA одним бандлом; ре-вендорим один раз после него
      (`docs/revendor-steward-gate-verdicts.md`), чтобы не расширять
      advisory-blast дважды.
- [ ] `contracts/executor-config/v0-provisional`: довести до реального потребителя либо явно пометить контракт отложенным @owner:github:andrei-shtanakov @blocked_by:todo://maestro/specrunnerconfig-passthrough @trigger:"Maestro начал читать contracts/executor-config" @id:executor-config-consumer
      Статус-обзор экосистемы 07-24 назвал это watch-item: схема запинена (DESIGN-301),
      но единственная ссылка в экосистеме — план-док Maestro
      `2026-07-17-specrunnerconfig-passthrough.md`, потребителя нет. Риск — зомби-пин,
      который «застыл» без интеграции. Наша часть — решение: ждать потребителя или
      написать в README контракта, что он отложен.
- [ ] Заморозить схемы MCP-тулзов (вендоринг пиненой копией по дисциплине ADR-ECO-003) @owner:github:andrei-shtanakov @trigger:"robin или Maestro начали вызывать dispatcher mcp" @id:freeze-mcp-tool-schemas
      Схемы 15 тулзов сознательно UNSTABLE: фиксировать контракт до первого потребителя
      значит заморозить угаданную форму.
- [x] Вендорить `contracts/actions/v1`, когда github-checker его опубликует — PR #97 @owner:github:andrei-shtanakov @blocked_by:github-checker#contracts-actions-v1 @trigger:"github-checker опубликовал контракт действий" @id:vendor-contracts-actions-v1
      Пиненая копия из `github-checker@ef03fef` (36 файлов поверхности: схема, README,
      34 фикстуры; per-file sha256 + `tree_sha256` в `manifest.json`), извлечена из
      git object database. Единственный вход — `ingest(raw, *, returncode)`; сошлись
      все три parse-path'а. Контракт действий больше не «поведение бинаря»:
      absent/`null`/`false` держатся через `model_fields_set`, а не через дефолты.
- [x] Ре-вендоринг actions/v1 — воспроизводимая процедура: runbook + скрипт с одним входом — PR #102 @owner:github:andrei-shtanakov @id:revendor-actions-runbook
      Процедура жила только в историческом плане `2026-07-31-vendor-actions-v1.md`,
      а пин правился руками в трёх местах: согласованная правка всех трёх
      оставляла сьют зелёным, заверяя новые байты старым коммитом.

## Хвосты качества

- [x] Прогнать live-смоук write-path с реальным `github-checker` на PATH — PR #98 @owner:github:andrei-shtanakov @id:live-smoke-write-path
      Выполнено 2026-08-01 локально: `test_write_path_live_smoke_real_binary` прошёл на
      `dispatcher@b571cba` против бинаря, установленного из `github-checker@ef03fef`
      (тот же коммит, на который запинен вендоренный контракт). Сюита — 650 passed,
      0 skipped. Отдельно от этого пункта CI-сторона закрыта в
      `@id:design-405-level-3-never-runs`.
- [ ] Route-level тест сериализации `ok=True` для четырёх additive-полей `ActionOutcome` @owner:github:andrei-shtanakov @id:action-outcome-serialization-test
      Принятый follow-up PR #40.
- [x] ruamel: standalone-комментарий сразу после блока `spec_runner` теряется при ре-рендере `project.yaml` — PR #113 @owner:github:andrei-shtanakov @id:ruamel-standalone-comment-loss
      Закрыто 2026-08-03. Формулировка пункта занижала масштаб: одна строка
      (`doc["spec_runner"] = new_block`, свежий plain dict поверх загруженной
      `CommentedMap`) теряла **три** вещи — комментарии в блоке и после него,
      порядок ключей файла и **любой ключ, для которого у редактора нет поля**.
      На реальном `research-bench/project.yaml` no-op-правка удаляла
      `spec_gen_budget_usd: null`, а его же комментарий говорит, что без явного
      null preflight отвергает конфиг. То есть «лишний PR вместо no-op» — это
      ещё и сломанный конфиг у соседа, а не только потеря текста.
      Четвёртый источник шума — не эта строка, а дефолты эмиттера: ruamel
      переиндентировал все блочные последовательности файла (весь `domain:`,
      которого редактор не касается) и писал `k: null` как `k:`. Стиль теперь
      **измеряется** по файлу, а не угадывается.
      Хвост (пустая строка + col-0 комментарий после блока) висит у ruamel на
      последнем ключе блока — при вложенном последнем значении на листе на
      несколько уровней ниже; переносится только когда последняя строка блока
      действительно двигается, поэтому обычная правка воспроизводит вход
      побайтово. Это и есть новая планка: `test_a_noop_candidate_reproduces_the_file_byte_for_byte`.
      Остаток осознанный: файл с внутренне непоследовательным стилем (смешанные
      отступы последовательностей, `null` и `~` вместе) откатывается на дефолты
      ruamel и всё ещё получит косметический шум. Жёсткий вариант — fail-closed
      гейт, отказывающийся рендерить, если меняется хоть строка вне блока
      `spec_runner:`; это превращает тихий шум в отказ и требует продуктового
      решения — см. `@id:render-outside-block-fail-closed`.
- [x] Fail-closed гейт: отказываться рендерить `project.yaml`, если правка меняет хоть строку вне блока `spec_runner:` — PR #114 @owner:github:andrei-shtanakov @id:render-outside-block-fail-closed
      Поднято при закрытии `@id:ruamel-standalone-comment-loss` (PR #113). Сейчас
      совпадение стиля с файлом — измеренное, но всё же best-effort: файл, чей
      стиль ruamel не воспроизводит, тихо получает шум в чужом PR. Гейт делает
      неизвестность видимой (`fail-closed-covers-the-instrument`), но ценой того,
      что редактор откажется работать с таким файлом вместо того, чтобы открыть
      неаккуратный PR. Что из двух правильнее — решение владельца, не рефакторинг.
      Закрыто: две независимые проверки, ни одна не заменяет другую. Check A
      (fidelity) — чистый `load` → `dump` без кандидата, требует побайтового
      равенства с источником; ловит строки 1–6 таблицы инвентаря и всё, что
      никто не перечислил. Check B (containment) — с применённым кандидатом,
      требует побайтового равенства ВНЕ владеемого span'а `spec_runner:`; ловит
      строку 7 (alias, раскрывшийся из-за уничтоженного anchor'а внутри блока)
      и любой будущий побег правки за границы блока. Строки 1–5 (BOM, CRLF,
      `---`/`...`, отступ маппинга, sequence-offset) сохраняются рендерингом, а
      не прощаются компаратором — 19 стилевых тестов зелёные, включая новый
      изолированный случай `...` без `---`. Строка 6 (aligned values) и
      merge-key `project.yaml` (spec_runner только через `<<: *anchor`) теперь
      честные отказы — снятие возможности, не только новая защита: файл,
      который редактировался сегодня с шумом, после этого PR откажется
      редактироваться вовсе (задокументировано в design doc, "Known
      residuals", рядом со строкой 6). Диагностика по всему пути рендера
      content-free — единственный тип `UnsafeEditError`, без ссылки на
      оригинал ни на атрибуте, ни на `__cause__`/`__context__` — потому что
      `DuplicateKeyError` ИЗМЕРЕННО печатает оба значения (новое и оригинал)
      вербатим прямо из `yaml.load`, а не гипотетически. Принятый остаток —
      traceback: кадры держат `base_bytes`/`base_text` живьём, и отдельно
      измерен второй канал в кадре `contextlib.__exit__` (локальная `value` =
      сам оригинальный объект исключения) — см. `@id:exception-traceback-frame-locals`.
      Сохранение неизвестных данных ВНУТРИ блока (соседский ключ, стиль
      кавычек, комментарии, порядок, значения вне кандидата) не покрыто НИ
      одной из двух проверок — Check A идёт до применения кандидата, Check B
      исключает блок из сравнения по построению — и держится только на
      целевых тестах (5 тестов, все прошли на первом прогоне без правок кода —
      характеризационные, не регрессионные).
      Task 7 (после финального ревью всей ветки, владелец постановил
      «отказывать»): Check B побайтовый и слеп к правке, меняющей СМЫСЛ
      данных вне span'а без единого сдвинувшегося байта — anchor внутри
      `spec_runner:`, заалиасенный или влитый через merge key снаружи,
      отслеживает мутацию блока, а сам alias-сайт (`*sr`, `<<: *sr`) в
      тексте не меняется. Добавлен Check C (`check-c`) — отказ, если ЛЮБОЙ
      узел внутри владеемого блока, включая сам блок, достижим снаружи;
      identity-based (не по имени anchor'а — алиасы резолвятся при
      загрузке, у alias-сайта имени нет), с двумя load-bearing тонкостями
      (искать по КЛЮЧУ, не по identity — иначе алиас на весь блок
      пропускается; identity значим только для контейнеров и anchored
      scalars — plain scalars интернированы). Третье снятие возможности
      (после строки 6 и merge-key case): `project.yaml`, чей `spec_runner:`
      где-то ещё заанкорен/заалиасен, теперь совсем нередактируем, даже
      если конкретный кандидат тот узел не трогает — владелец явно
      разрешил такую упрощённую границу вместо доказательства, что
      кандидат касается именно того узла. Побочный эффект: строка 7
      теперь полностью поглощается Check C (он срабатывает раньше на том
      же исходном документе), `stage` у соответствующего теста сменился
      с `check-b` на `check-c`. Check C намеренно НЕ расширяет merge-key
      residual выше: пропускается, когда у блока нет собственной
      топ-левел позиции (только через `<<: *anchor`), потому что
      безспановый fallback Check B там уже не слабее для реальной
      правки, а Check C иначе отказал бы и no-op'у, ничего не защищая.
- [ ] Read-путь `project.yaml` эхом печатает PyYAML-ошибку с исходной строкой файла @owner:github:andrei-shtanakov @id:read-path-yaml-error-leak
      Найдено во время работы над `@id:render-outside-block-fail-closed`
      (write-путь). `dispatcher/core/collectors/base.py:203`
      (`read_yaml`/`SourceReadError`) кладёт `str(err)` от PyYAML в сообщение,
      а `discover_project_configs` (`core/spec_runner_config.py:148`) кладёт
      его в `warnings` как есть — та же природа утечки,
      что была измерена на write-пути (ruamel `DuplicateKeyError` печатает
      значение вербатим): секрет из `project.yaml` соседнего репо оказывается
      в тексте предупреждения. Сейчас все четыре места вызова разбирают
      результат как `configs, _ = discover_project_configs(...)` и warnings
      отбрасывают, поэтому наружу (HTTP, аудит-лог) сегодня ничего не
      достигает — не блокер, а долг: любое будущее место, которое начнёт
      читать вторую компоненту кортежа, унаследует утечку молча. Код здесь не
      трогаем — вне диффа этой задачи.
- [ ] `UnsafeEditError`'а traceback всё ещё несёт кадры с секретом — принятый остаток, не баг @owner:github:andrei-shtanakov @id:exception-traceback-frame-locals @trigger:"в dispatcher появляется traceback-форматтер/репортер с locals capture (Sentry include_local_variables, better-exceptions, rich) или иной потребитель полного traceback"
      Измерено при закрытии `@id:render-outside-block-fail-closed`. Гарантия
      «ни одна ссылка на оригинал не переживает» (Finding из ревью Task 2,
      round 1+2) закрывает сам объект исключения — сообщение, `args`,
      атрибуты, `__cause__`, `__context__` — но НЕ его traceback: кадры
      `_build_new_yaml_bytes` держат `base_bytes`/`base_text` (весь файл
      соседа целиком, секрет включён) локальными переменными, пока жив
      объект traceback. Второй, отдельный канал измерен при закрытии Task 6:
      тот же traceback доходит и до кадра `contextlib.__exit__` (машинерия
      `_stage`-контекстника) — его локальная переменная `value` держит сам
      ОРИГИНАЛЬНЫЙ объект исключения, который вся эта граница существует,
      чтобы зачистить, с секретом в сообщении вербатим. Это не то же самое,
      что канал с `base_bytes`/`base_text`: там утекает сырой файл, здесь —
      незачищенное исключение. Любой репортер, настроенный сохранять локали
      кадров (Sentry `include_local_variables`, `better-exceptions`), достанет
      оба канала — независимо от того, что зачищено на самом объекте
      исключения. Это свойство трейсбеков Python, а не дефект этого кода:
      починки на уровне этой функции не существует. Пункт — не «сделать», а
      «не потерять»:
      если в проекте когда-нибудь появится Sentry/аналогичный репортер,
      его конфиг для этого пути должен явно выключать захват локалей кадров
      (или маскировать `project.yaml`-содержимое до того, как оно попадёт в
      кадр), иначе гарантия окажется тише, чем заявлено в
      `docs/superpowers/specs/2026-08-03-render-outside-block-fail-closed-design.md`.
      Заготовка на срабатывание триггера. Первичная защита — прежняя (см. выше):
      конфиг репортера обязан выключать захват локалей кадров на этом пути.
      Кандидат-механизм вдобавок (гипотеза, проверить при срабатывании):
      зачистка traceback на внешней границе — в финальном catch, после
      завершения распространения (`traceback.clear_frames` /
      `__traceback__ = None`). Зачистка в choke point `_build_new_yaml_bytes`
      канал `contextlib.__exit__` НЕ закрывает — этот кадр добавляется в
      traceback позже, при распространении вверх; `raise from None` тоже не
      решение — implicit chaining происходит в момент raise (измерено при
      закрытии `@id:render-outside-block-fail-closed`). Истина — acceptance:
      canary-секрет в фикстурном `project.yaml`, тест обходит tb-кадры и
      `f_locals` и не находит канарейку — обход ловит оба измеренных канала.
- [ ] README + иконка для `vscode-ext/` @owner:github:andrei-shtanakov @id:vscode-ext-readme
      Страница расширения показывает «No README available».
- [x] `detail()` (`dispatcher/server/static/index.html`) брал `repoDir` из display-имени коллектора и кормил им четыре directory-keyed места — PR #111 @owner:github:andrei-shtanakov @id:spec-runner-config-dir-name-mismatch
      Найдено при зачистке merge-gate-console (2026-07-30), закрыто 2026-08-02.
      `detail(name, dirName)` уже получал оба значения — merge-gate брал `dirName`
      с тех самых пор, а блок конфига пятнадцатью строками ниже продолжал брать
      `name`. Четыре directory-keyed места: сам GET, `repoDir`, и через него `dir`
      у `POST /api/actions/update-spec-runner-config` (резолвит
      `root / repo_dir / "project.yaml"`) и два suggest-эндпоинта.
      Два разных отказа, и это важно: **read-путь от ФС не зависит** — сервер
      сравнивает строки (`parent.name == dir_name`), то есть ломается везде, а
      404 попадает в `if (resp.ok)` и читается как «у проекта нет project.yaml»;
      **write-путь** ломается только на регистрозависимой ФС. Поля не
      объединяли — `/api/projects/{name}/onboarding` действительно по имени, а
      `.../{dir_name}/spec-runner-config` действительно по каталогу; теперь это
      видно в сигнатурах. Отсутствующий `dirName` не откатывается к имени:
      запрос не уходит, панель скрыта. Коллектор по-прежнему отдаёт `Maestro`
      при каноне каталога `maestro` — см. `maestro-double-name` ниже.
- [ ] Merge-gate: список открытых PR по репо (номер + заголовок), чтобы `#merge-gate` открывался кликом, а не ручным вводом номера @owner:github:andrei-shtanakov @id:merge-gate-pr-listing
      Task 4 (2026-07-30) исходно планировала клик по существующему PR-рендерингу
      в карточке проекта — такого рендеринга нет: read-модель несёт GitHub-состояние
      только как непрозрачный `github: dict[str, Any]` (`core/snapshot_contract.py`),
      номер PR нигде не всплывает, и ни один таск плана его не заводит. S1 вместо
      этого — ручной ввод номера PR рядом с панелью project-detail (аддитивно, без
      нового backend). Реальный список требует поля read-модели + endpoint + UI;
      см. `docs/superpowers/plans/2026-07-30-merge-gate-console.md` Task 4 Step 3.
- [x] `ActionRunner._invoke` ловит вокруг `subprocess.run` только `FileNotFoundError`/`TimeoutExpired`/`JSONDecodeError`/`ValidationError` — гарантия «аудит-строка на каждую попытку» пробивается ещё двумя исключениями — PR #96 (фикс) + #100 (гарантия как свойство) @owner:github:andrei-shtanakov @id:actions-envelope-catch-too-narrow
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
      **Чекбокс не был перевёрнут** до 2026-08-01: тело говорило «закрыто», строка
      осталась `- [ ]`, а дельта-счётчики читают чекбокс, а не прозу — пункт месяц
      числился открытой работой при собственном тексте «сделано». Заодно PR #100
      закрепил саму гарантию свойством, а не набором частных случаев: свип по шести
      выходам `_invoke` (включая таймаут — единственный, у кого своего теста не было)
      требует аудит-строку с фазой на каждом. Мутации проверены поимённо.

- [ ] Вёрстка экранов (`<style>` в `<head>` у `index.html`) вне досягаемости Node-харнесса: удаление всего блока оставляет сьют зелёным @owner:github:andrei-shtanakov @id:web-harness-does-not-see-css
      Найдено финальным ревью ветки task-authoring (2026-07-31, coverage-gap,
      не блокер). Харнесс исполняет `<script>` из `<body>` над распарсенной
      разметкой и проверяет поведение, а не внешний вид; CSS он не применяет и
      применять не должен — `tests/web/dom.js` не layout-движок. Значит про
      «экран не разъехался» сьют не говорит ничего, и говорить не начнёт без
      настоящего браузера (Playwright + скриншот-диффы). Записано, чтобы зелёный
      прогон не читался как «вёрстка проверена».
- [ ] `tests/web/dom.js` — рукописная DOM-заглушка для task-authoring Node-харнесса; `jsdom` предпочтительнее, но отсутствует осознанно @owner:github:andrei-shtanakov @id:web-tests-hand-rolled-dom
      Python-сьют не ставит npm-пакеты (`uv`-only дисциплина этого репо), поэтому
      `jsdom` как test-only devDependency потребовал бы отдельного `npm install`
      шага в CI и локально — цена, которую задача не оправдала. Компромисс держит
      харнесс зависимым только от `node` на PATH; если объём/сложность DOM-стабов
      вырастет, `jsdom` стоит пересмотреть.

- [x] `DESIGN-405` уровень 3 (`test_write_path_live_smoke_real_binary`) теперь @owner:github:andrei-shtanakov @id:design-405-level-3-never-runs
      выполняется в CI: `skipif` снят, бинарь ставится на пине — PR #98
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

- [x] PF-6: развести две гарантии, сидевшие в одном `in_sync` — PR #99 @owner:github:andrei-shtanakov @id:pf6-split-integrity-and-drift
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

- [ ] Пин `fastmcp<3` и три отклонённых GHSA (OpenAPI SSRF, OAuth proxy, Gemini-CLI injection) — переоценить @owner:github:andrei-shtanakov @trigger:"бамп fastmcp до 3.x" @id:fastmcp-pin-reeval
      Отклонены обоснованно: путь только stdio, без OpenAPI/OAuth/HTTP. Патчи есть лишь
      в 3.2.0, а пин `<3` держит мажор общим с Maestro.
- [ ] Двойное имя Maestro: коллектор отдаёт `"Maestro"`, канон репо — `maestro` @owner:github:andrei-shtanakov @trigger:"каталог Maestro/ переименован в maestro/ на диске" @id:maestro-double-name
      Детект коллектора content-based (`maestro/` + `pyproject.toml`), поэтому
      переименование каталога discovery не сломает — но в Sync-вкладке repo придёт под
      именем каталога, и два пространства имён разойдутся. Тот же класс, что discovery-имя
      vs service-id у proctor: слепое переименование запрещено, нужна осознанная
      нормализация.
- [ ] Handoff в arbiter по ошибке `agent_id` в `report_benchmark` @owner:github:andrei-shtanakov @id:arbiter-agent-id-handoff
      Дефект соседа, который dispatcher только показывает; наша часть — написать
      handoff/issue, править чужой репо нельзя.
- [x] Для `contracts/github-checker-actions/v1` нет drift-сигнала: о том, что продюсер уехал, узнаём только вручную — PR #110 @owner:github:andrei-shtanakov @id:actions-v1-no-drift-signal
      У plan-fields две гарантии — offline integrity в PR-гейте и scheduled
      `upstream-drift.yml`; у actions/v1 была только первая, и она по построению
      останется зелёной сколько угодно долго после того, как канон уехал.
      Закрыто симметричным advisory: `.github/workflows/actions-upstream-drift.yml`
      + `scripts/actions_drift_report.py`. Сравнение не манифест-к-манифесту, как
      у plan-fields, а пересчёт `tree_sha256` алгоритмом `build_manifest` — у
      продюсера своего манифеста нет. Следствие закрыто явно: файл, добавленный
      апстримом под именем из `EXCLUDED_NAMES`, ловится до пересчёта и даёт дрейф,
      а не молчание. Коды `0/1/2`, причём `unavailable` ≠ «нет дрейфа» и требует
      чинить наблюдение, а не вендорить с коммита, который никто не прочитал.
      Не required и не на PR. Процедура ре-вендоринга:
      `docs/revendor-github-checker-actions.md`.
