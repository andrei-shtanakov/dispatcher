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
- 🔜 Открытого продуктового scope из discovery-брифа (FR-01..06) не осталось; ниже —
  кросс-проектные точки и хвосты качества.

---

## Governance-плоскость (ADR-ECO-004)

- [ ] Governance view: рендерить `declared vs observed` enforcement-зрелость правил (ADR-ECO-004 D3, Batch 2 §6) @owner:andrei @blocked_by:prograph-vault#governance-observed-derived @trigger:"в prograph-vault появился derived/governance/ с observed-зрелостью"
      Declared-сторона готова: `../prograph-vault/authored/registry/governance.yaml`
      (v1, 2026-07-18) и сам файл называет ожидаемый путь observed —
      `derived/governance/`, который на 2026-07-26 не существует. Сравнивать не с чем,
      поэтому пункт заблокирован, а не «в работе»: dispatcher здесь рендерер, не
      источник, и читать «на будущее» нечего.

## Кросс-репные контракты

- [ ] `contracts/executor-config/v0-provisional`: довести до реального потребителя либо явно пометить контракт отложенным @owner:andrei @blocked_by:maestro#specrunnerconfig-passthrough @trigger:"Maestro начал читать contracts/executor-config"
      Статус-обзор экосистемы 07-24 назвал это watch-item: схема запинена (DESIGN-301),
      но единственная ссылка в экосистеме — план-док Maestro
      `2026-07-17-specrunnerconfig-passthrough.md`, потребителя нет. Риск — зомби-пин,
      который «застыл» без интеграции. Наша часть — решение: ждать потребителя или
      написать в README контракта, что он отложен.
- [ ] Заморозить схемы MCP-тулзов (вендоринг пиненой копией по дисциплине ADR-ECO-003) @owner:andrei @trigger:"robin или Maestro начали вызывать dispatcher mcp"
      Схемы 15 тулзов сознательно UNSTABLE: фиксировать контракт до первого потребителя
      значит заморозить угаданную форму.
- [ ] Вендорить `contracts/actions/v1`, когда github-checker его опубликует @owner:andrei @blocked_by:github-checker#contracts-actions-v1 @trigger:"github-checker опубликовал контракт действий"
      Принятый (не блокирующий) follow-up PR #40: сейчас контракт действий существует
      только как поведение бинаря и проверяется live-смоуком.

## Хвосты качества

- [ ] Прогнать live-смоук write-path с реальным `github-checker` на PATH @owner:andrei
      Замер 2026-07-26: `test_write_path_live_smoke_real_binary` — **skipped**, бинаря на
      PATH нет, то есть уровень 3 из DESIGN-405 на этой машине не проверяется. Именно
      stub-маскировка однажды дала ложное «ok» и стоила гейта на write-path.
- [ ] Route-level тест сериализации `ok=True` для четырёх additive-полей `ActionOutcome` @owner:andrei
      Принятый follow-up PR #40.
- [ ] ruamel: standalone-комментарий сразу после блока `spec_runner` теряется при ре-рендере `project.yaml` @owner:andrei
      Деградация fail-visible (лишний PR вместо no-op), но это потеря пользовательского
      текста в чужом репо.
- [ ] README + иконка для `vscode-ext/` @owner:andrei
      Страница расширения показывает «No README available».

## Наблюдения (работу не начинаем, пока не сработает триггер)

- [ ] Пин `fastmcp<3` и три отклонённых GHSA (OpenAPI SSRF, OAuth proxy, Gemini-CLI injection) — переоценить @owner:andrei @trigger:"бамп fastmcp до 3.x"
      Отклонены обоснованно: путь только stdio, без OpenAPI/OAuth/HTTP. Патчи есть лишь
      в 3.2.0, а пин `<3` держит мажор общим с Maestro.
- [ ] Двойное имя Maestro: коллектор отдаёт `"Maestro"`, канон репо — `maestro` @owner:andrei @trigger:"каталог Maestro/ переименован в maestro/ на диске"
      Детект коллектора content-based (`maestro/` + `pyproject.toml`), поэтому
      переименование каталога discovery не сломает — но в Sync-вкладке repo придёт под
      именем каталога, и два пространства имён разойдутся. Тот же класс, что discovery-имя
      vs service-id у proctor: слепое переименование запрещено, нужна осознанная
      нормализация.
- [ ] Handoff в arbiter по ошибке `agent_id` в `report_benchmark` @owner:andrei
      Дефект соседа, который dispatcher только показывает; наша часть — написать
      handoff/issue, править чужой репо нельзя.
