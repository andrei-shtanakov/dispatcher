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

- [ ] Governance view: рендерить `declared vs observed` enforcement-зрелость правил (ADR-ECO-004 D3, Batch 2 §6) @owner:github:andrei-shtanakov @blocked_by:todo://prograph-vault/governance-observed-derived @trigger:"в prograph-vault появился derived/governance/ с observed-зрелостью" @id:governance-declared-vs-observed @epic:eco.governance-plane
      Declared-сторона готова: `../prograph-vault/authored/registry/governance.yaml`
      (v1, 2026-07-18) и сам файл называет ожидаемый путь observed —
      `derived/governance/`, который на 2026-07-26 не существует. Сравнивать не с чем,
      поэтому пункт заблокирован, а не «в работе»: dispatcher здесь рендерер, не
      источник, и читать «на будущее» нечего.
- [ ] Actor-aware evidence мержа: `agent_merge` / `human_merge` в наблюдении (ADR-ECO-004 I4, ADR-ECO-008 D6) @owner:github:andrei-shtanakov @trigger:"инцидент агентского мержа: спорный merged_by, ревокация, расхождение аудита gh pr list --json mergedBy" @id:agent-merge-observability @epic:eco.governance-plane
      Принятие inbox-issue #159 (ADR-ECO-006, from: prograph-vault#adr-eco-008).
      **Предпосылка выполнена 2026-08-21, ожидание снято 2026-08-22.** steward#72
      закрыт: опубликован `contracts/approval-facts/v2/` (`SCHEMA.json` с `actor_class`
      закрытой классификацией `human|agent|unknown`, `fixtures/`, `README.md`), факты
      материализуются в `.steward/approval_facts.jsonl` (`approvalfacts/publish.py`),
      evidence — `steward/docs/evidence/2026-08-21-approval-facts-v2-migration/`.
      Сопутствующий steward#69 тоже закрыт: `agent_merge_allowed` стал значением политики
      с fail-closed дефолтом `false`, в `agent_identities` внесена App-личность
      `github:merge-broker`.
      **Версия в прежнем триггере была неверна и стоила суток простоя:** он требовал
      `contracts/approval-facts/**v1**`, тогда как владелец выпустил сразу **v2**, и
      каталога `v1` не существовало никогда. Буквальное чтение давало «не сработал» при
      выполненной предпосылке; `@blocked_by:steward#72` при этом резолвился по состоянию
      issue, а issue оставался открытым сутки после отгрузки. Отсюда правило на будущее:
      ожидание указывать на **пункт продюсера** (`todo://<repo>/<id>`), а не на issue —
      пункт продюсер закрывает как часть самой работы, issue же отдельная бухгалтерия,
      которую забывают.
      **Условие срабатывания записано 2026-09-01.** Пункт перестал быть
      предусловием чего-либо, но остался без блокера и без условия, то есть
      машинно читался как готовый к работе — а решение по нему принято:
      ADR-ECO-011 OQ-1 постановил, что для DarkFactory достаточно
      `merged_by`-аудита, а отдельный дашборд предусловием не является,
      «пересмотр при первом инциденте». Этим же решением закрыт
      `todo://steward/agent-merge-d1-enablement`, и с тех пор **ни один пункт
      флота нас по этой оси не ждёт** (проверено: `@blocked_by` на
      `agent-merge-observability` — ноль вхождений).
      «Пересмотр при первом инциденте» — не триггер, который кто-то заметит,
      поэтому он записан буквально в теге. Пока инцидента нет, пункт честно
      лежит в waiting, а не изображает готовность.
      Живой контекст на дату записи: первые агентские мержи флота состоялись
      2026-09-01, `merged_by: ai-prosto` различает актора без нового рантайма —
      ровно как обещал ADR-ECO-011 D3. Инцидентом считается расхождение этого
      аудита, спорный `merged_by` или ревокация.
      **Ход теперь наш, стадия 2:** завендорить пиненую копию `contracts/approval-facts/v2`
      внутрь себя (`PIN` + copy-integrity, как остальные контракты) и написать
      `core/merge_actor.py` — валидация по вендоренной схеме и рендер типизированного
      наблюдения, без собственной семантики.
      Замер `../prograph-vault/authored/notes/2026-08-19-i4-observability-measurement.md`:
      merge-команда и исход `merged` есть (`dispatcher/core/actions.py:574`), актора и
      evidence — нет, агентский мерж и человеческий в dispatcher неразличимы.
      **Источник решён (ревью дизайна 2026-08-19).** Локальный git актора не доказывает
      в принципе — steward документирует это прямо: author/committer merge-коммита
      ставятся при создании и не являются каноническим мержером, канон — только
      `PullRequest.mergedBy` с форджа. Значит источник ровно один: типизированное
      наблюдение steward. Запрошено как steward#72 (внешний контракт
      `contracts/approval-facts/v1` + `actor_class` + provenance политики +
      материализация в `.steward/` рядом с `gate_verdicts.jsonl` + атомарность).
      Смежный, но отдельный предмет — steward#69 (разрешение `agent_merge` политикой
      вместо константы, App-личность): без него различать нечего, без #72 нечего читать.
      **Два хода отвергнуты на ревью — не повторять:**
      — читать `profiles/approval-policy.yaml` из чекаута steward: рантайм-зависимость
      от файла соседа, нарушение `repo-boundaries.md` п.3 (контракт потребляется
      пиненой копией внутрь, не импортом из пути соседа); соседний коллектор уже живёт
      под этим явно — `core/governance.py` CON-03, «no sibling-repo path is ever resolved»;
      — воспроизводить `classify_actor` у себя: steward и dispatcher стали бы **двумя
      policy engines**, способными разойтись. Владелец классификации — steward,
      dispatcher валидирует по пиненой копии и рендерит, своей семантики не выводит.
      **Разделение осей (обязательное).** `unknown` — валидное наблюдение steward при
      успешно применённой закрытой политике; `no-source` / `unreadable` — поломка
      инструмента. Сводить их в одно состояние запрещено: иначе outage классификатора
      читается как характеристика актора — тот самый класс дефекта, ради которого
      контур и строится. Ни один путь ошибки не даёт «чисто» (дисциплина NFR-02).
      **Ограничения, зафиксированные до проектирования:**
      — мержи внутри прогона на GitHub не видны вовсе (в арке `discovery` maestro слил
      четыре воркстрима в интеграционную ветку локально, без PR и события). Поверхность
      на предположении «каждый мерж = событие GitHub» будет систематически
      недосчитывать; наблюдаемая единица — только финальный PR;
      — dispatcher **не держит** installation token merge-личности: наблюдатель, сам
      владеющий ключом от наблюдаемого действия, перестаёт быть независимым. Владелец
      выпуска — открытый вопрос (runtime оркестратора либо узкий merge broker), но не
      dispatcher UI;
      — синтезировать `human_merge` из того, что мерж запущен кликом в dispatcher,
      запрещено: клик доказывает авторизацию в UI, а не личность мержера на GitHub.
      **Стадии:** (1) steward#72 — контракт и продюсер; (2) вендор пиненой копии +
      read-модель `core/merge_actor.py` по образцу `core/governance.py` + тесты;
      (3) панель web + паритет TUI/VSCode/MCP; (4) отдельной стадией — аномалия
      declared-vs-observed, но только после появления `merge_authority`: сегодня поле
      не существует нигде (ADR-ECO-008 D5 предписал его конфигу оркестратора, он его не
      реализовал), поэтому сравнивать не с чем — тот же случай, что
      `@id:governance-declared-vs-observed`.
      Пока пункт закрыт, ADR-ECO-008 D6 предписывает поведение сам: нет наблюдаемости —
      прогон обязан вести себя как `merge_authority: human`. Плюс D1 не является
      действующей политикой до ратификации (ADR-ECO-008 `status: proposed`).
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

## Product-governance (impresario)

- [x] Read-only `gate_waiting`: какие product-решения ждут человека — фаза 1, contract-backed состояния — PR #132, #133 @owner:github:andrei-shtanakov @id:product-proposal-gate-waiting
      Принятие inbox-issue #129 от impresario (ADR-ECO-006). Отдельный вид
      сущности `product_proposal` — не маскировать под PR-review/merge-gate:
      другая authority-модель (роли гейтов) и lifecycle (supersedes-цепочки,
      recycle). Состояния фазы 1: `status: ready_for_business` без активного
      approve `qg5_business` → ждёт Gate A (`business_owner`);
      `status: business_approved` без активного approve `qg5_committee` → ждёт
      Gate B (`committee_chair`); «активное» = не перекрытое supersedes-цепочкой
      (семантика evidence-проверки steward#64). Поля записи: `gate_id`,
      `authority`, `artifact_ref` (`proposal://PP-<id>` + путь workspace),
      `since`/freshness от `updated_at`, dedup identity =
      `proposal_id`+`gate_id`+`version`. Строго read-only (ARCH-C3/D1: producer
      решает — dispatcher рендерит). Схемы `product-proposal/v1` +
      `gate-decision/v1` вендорятся пиненой копией (пин на момент реализации;
      copy-integrity PR-гейт + upstream-drift, прецедент steward#65).
      Fail-closed: нечитаемый/невалидный proposal или decision = unknown, не
      «ничего не ждёт». Acceptance: копия бандла PP-101 со
      `status: ready_for_business` и снятым GD-001 → ровно одна запись
      (Gate A, business_owner, proposal://PP-101); настоящий approved бандл →
      ноль; нечитаемый decision-файл → unknown. Семантика контрактов (SSOT):
      `impresario/docs/semantics.md`; живой пример входа:
      `impresario/pilot/forconcept/pp-101/`. Фаза 2 (`needs_human` из
      loop.state) заблокирована на стороне impresario и в scope не входит.
      Прогресс: PR-1 (#132, вендор @ 28727ff, `core/product_proposals.py`,
      `collectors/impresario.py`, `GET /api/projects/{name}/product-proposals`,
      acceptance на пине PP-101, live smoke) + PR-2 (#133, web-панель + Node
      harness) — фаза-1 acceptance (web + Node harness) закрыта; спека
      `docs/superpowers/specs/2026-08-12-product-proposal-gate-waiting-design.md`.

- [x] Parity `product_proposal` в TUI/VSCode/MCP: read-only gate_waiting на остальных поверхностях — PR #138 @owner:github:andrei-shtanakov @id:product-proposal-parity
      Follow-up фазы 1 (#129: PR #132 collector/API + PR #133 web-панель),
      включает `needs_human`/loop-статусы фазы 2. Все поверхности — тонкие
      рендереры над `read_api.product_proposals`: MCP-тул `product_proposals`
      (пин whitelist 15→16, стабильные error-codes), секции в TUI
      `ProjectDetailScreen`, formatting-only секция в onboarding-доке VSCode
      (независимые запросы + generation guard). Правило нулевых состояний
      закреплено кросс-поверхностно (web приведён к нему же): уверенный
      «0 gates/loops waiting» — только при все-ok бандлах без report-level
      diagnostics.

- [x] Read-only `qg4_backlog` wait — фаза 3 gate_waiting — PR #155 @owner:github:andrei-shtanakov @id:product-proposal-qg4-backlog-wait
      Принятие inbox-issue #154 от impresario (ADR-ECO-006), продолжение
      #129/#136. QG-4 — один gate над версией RankedBacklog: ожидание
      существует, пока в текущей версии `backlog.yaml` есть selectable-позиции
      (`new` | `under_review`) и нет активного (не superseded) GateDecision
      `qg4_backlog` той же версии с любым исходом QG-4
      (select | defer | park | reject); identity `(backlog_id, version)`,
      freshness `updated_at`. Re-pin всех impresario-контрактов @ `a9d11fa`
      (5 директорий, один пин): добавлены `ranked-backlog/v1` и
      `loop-resume-decision/v1` — LRD-записи в `decisions/` распознаются и
      игнорируются (живой lrd-001.yaml в pp-101 больше не роняет бандл в
      unknown). Поля `backlog_bundles`/`backlog_waits` в
      `ProductProposalsReport`, web-панель, acceptance #154 на пинованной
      копии `pilot/backlog.yaml` (BL-ecosystem v4). Спека:
      `docs/superpowers/specs/2026-08-17-qg4-backlog-wait-design.md`.
      Follow-up (вне scope): явный рендер `backlog_waits` в TUI/VSCode
      (прецедент parity — PR #138); MCP отдаёт поля уже сейчас.

- [x] Read-only `needs_human` из loop-state/v1 — фаза 2 gate_waiting — PR #137 @owner:github:andrei-shtanakov @id:product-proposal-needs-human
      Принятие inbox-issue #136 от impresario (ADR-ECO-006), продолжение
      #129 (фаза 1 — PR #132..#135). Единый re-pin всех трёх контрактов @
      `51e3103` (anti-mix по трём манифестам, checkout — трёхсторонний
      agreement). `loop.state` классифицируется fail-closed
      (absent | running | needs_human | ready_for_business | failed |
      unknown), строгий JSON, локальный membership-чек `proposal_id`;
      identity ожидания `(loop_id, stop.iteration)`, freshness `stop.at`.
      Панель: чипы loop-статусов (включая absent) + таблица needs_human +
      «0 loops waiting». Acceptance #136 на пинованной копии PP-101.
      Producer-side `LOOPSTATE_*` кросс-чеки остаются за impresario.

## ATP benchmark view (eco-профиль atp-platform)

- [x] ATP benchmark view: рендерить benchmark runs + leaderboard eco-сервера atp-platform через Benchmark API — PR #143 (спека+план), #144 (core), #145 (web-панель) @owner:github:andrei-shtanakov @id:atp-benchmark-view
      Принятие inbox-issue #139 от atp-platform (ADR-ECO-006); слаг запроса
      `atp-eco-benchmark-view` переименован при принятии («eco» — внутреннее
      имя серверного профиля продюсера, вид в планах dispatcher называем по
      сущности), `slug:` в теле issue поправлен. ATP поставляет API-only
      профиль (`ATP_SERVER_PROFILE=eco`, atp-platform#287/#288) без HTML UI;
      решение Andrei — поверхность отображения выступает dispatcher. Контракт
      стабильный: `GET /api/v1/benchmarks`, `GET /api/v1/runs/{id}/...`
      (статус пока отдаёт только `total_score`; экспорт `score_components` —
      отдельный пункт atp-platform/TODO.md), leaderboard-эндпоинты; SDK
      `atp-platform-sdk>=2.0.0` (`leaderboard_sync()`, `status_sync()`);
      аутентификация токенами `atp_u_*` / `atp_a_*`; маркер профиля —
      `GET /` → `{"profile": "eco"}`, `/openapi.json` доступен. Спека профиля:
      atp-platform `docs/superpowers/specs/2026-08-14-eco-server-profile-design.md`.
      Внимание при дизайне: это новый для dispatcher класс источника — живой
      HTTP API с токенной авторизацией, а не on-disk артефакты; строго
      read-only (D1), контракт вендорить пиненой копией (`/openapi.json`),
      спека в `docs/superpowers/specs/` до реализации.
      Завершено: PR #143 (спека + план), PR #144 (вендор atp-benchmark-api/v1,
      core/benchmarks.py, BenchmarkService, GET /api/benchmarks, интеграция),
      PR #145 (web-панель + list rendering + leaderboard navigation +
      live-smoke runbook). Явно не входит в scope: токен-авторизация (фаза 2
      run-status), TUI/VSCode/MCP parity — отдельные пункты ниже.

- [x] Benchmark run-status — фаза 2: статус ранов через токен-гейтед `GET /api/v1/runs/{id}/status` — PR #150 (спека), #151 (re-vendor+core), #152 (web) @owner:github:andrei-shtanakov @id:atp-benchmark-runs-phase2
      Продолжение `atp-benchmark-view` (спека 2026-08-15, §2 non-goals).
      Эндпоинт токен-гейтед И owner-scoped: виден только владелец токена,
      значит нужны (а) **первый хранимый секрет dispatcher** — паттерна нет,
      ближайший прецедент 0600 git-ignored sidecar (`dispatcher-sync.toml`),
      решение о месте хранения токена `atp_u_*` — часть дизайна; (б) история
      обнаружения run-id (список ранов у публичной поверхности отсутствует).
      ~~`score_components` в статусе — отдельный пункт atp-platform/TODO.md~~
      (у продюсера уже приземлилось: `RunStatusResponse` @ `da3a264` несёт
      `score_semantics`+`score_components` — спека фазы 2 потребляет сразу).
      Начинать со спеки; ре-вендор контракта расширит пруненный openapi.
      Закрыто цепочкой: PR #150 — спека
      `docs/superpowers/specs/2026-08-16-atp-benchmark-run-status-design.md`
      (token_file 0600-гейт + lstat/анти-симлинк + канарейка секретности;
      ручной ввод run-id по прецеденту merge-gate #93; клик-driven fetch
      без фонового поллинга секрета; честный `not_found` = «нет или не
      твой»); PR #151 — re-vendor (третий путь + securitySchemes в prune,
      authored-фикстуры) + core (конфиг/ридер/модели/сервис/маршрут
      `GET /api/benchmarks/runs/{id}`, канарейка по всем состояниям,
      отравленный fetcher пинует «секрет не ездит в фоне»); PR #152 —
      Run-status row в web-панели Benchmarks (formatting-only, серверные
      формулировки вербатим, in-flight lock, Node-харнес) + расширение
      live-smoke ранбука фазой 2. MCP-тула нет и не будет без отдельного
      дизайна (X-02: тул-вызов — действие агента, не клик человека).

- [x] Parity панели Benchmarks: TUI/VSCode/MCP поверх `read_api.benchmarks` — PR #153 @owner:github:andrei-shtanakov @id:atp-benchmark-view-parity
      Прецедент — product-proposal parity (#138): все поверхности — тонкие
      рендереры над той же read_api-функцией с `start_fetch`-passthrough.
      TUI: вкладка Benchmarks (скрыта на unconfigured — hide_tab, как web
      скрывает секцию), таблицы список+leaderboard, статус в лейбле вкладки.
      VSCode: view `dispatcherBenchmarks` (скрыт через when-контекст
      `dispatcher.benchmarksConfigured`), узлы — чистые функции в model.ts
      под vitest. MCP: whitelist 16→17, тул `benchmarks`; осознанное
      расхождение с no-fetch паттерном sync_status — кэш BenchmarkService
      in-memory per-process, standalone stdio-процесс с start_fetch=False
      вечно отдавал бы «not fetched yet», поэтому тул делает kick-and-wait
      ОДНОГО троттленного read-only GET ПУБЛИЧНОЙ поверхности; сервис MCP
      строится без token_file (token-gated путь недоступен по построению,
      пин отравленным fetcher'ом). Правило нулевых состояний перенесено
      как есть: уверенный «0 benchmarks»/«0 entries» — только при ok;
      not-fetched-yet (fetched_at+error оба null) — не failure.

## Maestro state (per-project run DBs)

- [x] Maestro per-project run DBs: коллектор перечисляет `~/.maestro/projects/**/runs/<id>/state.db` вместо одного пути, fail-closed статусы ранов, legacy-файл помечен — PR #148 @owner:github:andrei-shtanakov @id:maestro-per-project-run-dbs
      Принятие inbox-issue #147 от maestro (ADR-ECO-006). Продюсерская сторона
      уже приземлилась (maestro `a4caef0`, спека
      `2026-08-15-maestro-state-layout-design.md` rev 3) — текст issue
      (`runs/<run-id>.db`) устарел относительно неё: ран — **директория**
      (`runs/<id>/state.db`), `_local`-ключи двухсегментные. Load-bearing
      пункт: ран без терминальной записи — `interrupted`, никогда не
      «running»; `running` только по позитивному свидетельству
      (`orchestrate.holder` + живой pid — осознанное ужесточение
      относительно holder-only `resolve_runs` самого maestro: holder
      переживает SIGKILL; пробовать сам flock нельзя — это взятие лока
      read-плоскостью). `unreadable` ≠ `legacy` (§D: видимый ран обязан
      нести run row). Новое аддитивное поле `ProjectSnapshot.runs`;
      конфиг-ключ `maestro_home` (дефолт — родитель `maestro_db`,
      герметичность тестов). Спека:
      `docs/superpowers/specs/2026-08-16-maestro-per-project-run-dbs-design.md`.
- [x] Runs-панель: рендер `ProjectSnapshot.runs` на web/TUI/VSCode + MCP-паритет — PR #149 @owner:github:andrei-shtanakov @id:maestro-runs-panel-parity
      Follow-up #147/PR #148 по цепочечному прецеденту atp-benchmark-view
      (#144 core → #145 web → parity). Все поверхности — formatting-only
      поверх fail-closed классификации коллектора: web `#runs`-панель
      (detailGen guard, fail-loud, скрыта на 404 и чистом нуле), TUI-секция
      `orchestration runs`, VSCode — независимый третий fetch + markdown
      (`src/runs.ts`). MCP — осознанно БЕЗ 17-го тула: снапшот с `runs` уже
      отдаёт тул `project` (в отличие от product-proposals, где отдельный
      тул оправдан вычисляемым report). Кросс-поверхностные пины: слова
      бейджей идентичны везде; незнакомый статус продюсера — `✖ <status>`
      вербатим, не молча зелёный; правило нулевых состояний — чистый ноль
      скрывает секцию, любой warning с префиксом `run `/`runs ` (пин
      `test_run_warning_prefixes_are_pinned`) открывает её как
      «unknown, not zero». Дыра закрыта в core: нечитаемая директория при
      енумерировании теперь warning, а не молчаливый ноль. Спека §7a.

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
- [x] Вендор steward roles-catalog v1: пиненая копия `profiles/roles.yaml` @ `b79c858` + подготовка к следующему перепину gate-check — PR #131 @owner:github:andrei-shtanakov @id:vendor-roles-catalog
      Принятие inbox-issue #128 от steward (ADR-ECO-006, DEC-007 §1). Канон:
      `steward/profiles/roles.yaml` (v1, 6 slug'ов: product, architects, qa,
      tech-lead, stream-owner, owner; `slug_pattern`; состав запинован на
      `version`) @ `b79c858dc5f5dc7651f15a1cdf3bcd51a1de2d16` (master после
      steward#56). Копия — рядом с gate-verdicts/gate-catalog по паттерну
      #125/#127: copy-integrity в PR-гейте + upstream-drift отдельным
      scheduled-workflow. К следующему перепину бинаря gate-check (иначе
      live-смоук уйдёт в exit 2 — спроектированный отказ, не баг):
      `roles.yaml` — обязательный сосед профиля на каждом прогоне (вендоренная
      копия и есть правильный сосед; минимальный каталог из одного `owner` не
      резолвит роли тестового бандла); тестовый профиль смоука — в
      канонической форме (legacy `"@product"` = ProfileError, exit 2);
      `role-assignments.yaml` для solo-смоука не нужен; идентичности — точные
      строки без case-folding, канон `github:andrei-shtanakov`.
      Закрыто PR #131: копия + обе гарантии (copy-integrity в PR-гейте с
      пином состава v1, scheduled drift-job) + ре-вендор скрипт/runbook.
      Drift-репортёр не скопирован, а параметризован (`ContractSpec` в
      `gate_catalog_drift_report.py`, roles — тонкая обёртка). Чек-лист к
      перепину gate-check записан в
      `docs/revendor-steward-roles-catalog.md` — сам перепин произойдёт в
      `@id:revendor-gate-verdicts-obligation-bundle`.
- [x] Ре-вендор `gate-verdicts/v1` бандлом steward: README-активация obligation + fixtures с obligation + stale-фраза в SCHEMA — PR #177 @owner:github:andrei-shtanakov @trigger:"steward обновил fixtures+SCHEMA gate-verdicts одним бандлом (inbox #125 п.3) — либо красный drift-steward-gate-verdicts" @id:revendor-gate-verdicts-obligation-bundle
      Из inbox #125 п.2–3: README контракта уже изменён точечно на пине
      `c26ca38` (активация obligation), SCHEMA.json не тронут побайтово —
      advisory `drift-steward-gate-verdicts` до ре-вендора может гореть, и
      это осознанно. Steward обновит fixtures (сейчас несут findings без
      obligation — схемно валидно, но уже не образец producer-вывода) и
      stale-фразу в SCHEMA одним бандлом; ре-вендорим один раз после него
      (`docs/revendor-steward-gate-verdicts.md`), чтобы не расширять
      advisory-blast дважды.
      Закрыто тем же ре-вендором, что и `gate-verdicts-v1-prev-hash-revendor`
      (пин `9916787`): апстрим привёз obligation-фикстуры и hash-chain одним
      срезом master, отдельного бандл-коммита ждать было не нужно.
- [x] Ре-вендор `gate-verdicts/v1`: аддитивное поле `prev_hash` (hash-chain, steward#105/PR #109) — PR #177 @owner:github:andrei-shtanakov @id:gate-verdicts-v1-prev-hash-revendor
      Принятие inbox-issue #173 от steward (ADR-ECO-006). Пин `9b79700` →
      `9916787ff53946612922d65bd7c4ccfc4b0868bd` (steward master после PR #109
      + фиксы верификатора b7036c5/ac738bb); поверхность 7 → 9 файлов
      (`chained.jsonl`, `broken_chain.jsonl`). Схемного ре-вендора было
      недостаточно: pydantic-модели коллектора держат `extra="forbid"`, и без
      аддитивного `prev_hash: str | None` на artifact/finding каждый новый
      сцепленный леджер оставался unreadable — направление отказа верное
      (fail-closed), но панель горела бы и после обновления копии. Header
      поля не несёт по контракту (якорь цепочки) — его модель не тронута,
      сцепленный header остаётся невалидным. Acceptance issue: копия
      byte-equal master, copy-integrity зелёная, chained-фикстура читается
      (`test_chained_ledger_is_read_not_unreadable`).
- [ ] Верификатор hash-chain перед чтением леджера: broken-файл читать как unreadable @owner:github:andrei-shtanakov @id:gate-verdicts-chain-verification @epic:eco.governance-plane
      Опциональная половина inbox #173, вынесенная продюсером в «отдельное
      решение» — потому и отдельный пункт, а не часть ре-вендора. Сегодня
      цепочка НЕ проверяется: канонная фикстура `broken_chain.jsonl`
      (подменённый message при сохранённом prev_hash) классифицируется как
      обычный blocked — задокументировано характеризационным тестом
      `test_broken_chain_reads_the_same_because_the_chain_is_not_verified`,
      который при реализации обязан перевернуться на unreadable. Варианты по
      issue: гонять `steward verdicts-verify` (рантайм-зависимость от бинаря
      соседа — против дисциплины вендоринга) либо своя реализация по README
      §«Целостность» (механика SHA-256 предыдущей строки — контрактная
      арифметика, не вторая policy engine). Решение владельца.
- [ ] `contracts/executor-config/v0-provisional`: довести до реального потребителя либо явно пометить контракт отложенным @owner:github:andrei-shtanakov @blocked_by:todo://maestro/specrunnerconfig-passthrough @trigger:"Maestro начал читать contracts/executor-config" @id:executor-config-consumer @epic:eco.spec-toolchain
      Статус-обзор экосистемы 07-24 назвал это watch-item: схема запинена (DESIGN-301),
      но единственная ссылка в экосистеме — план-док Maestro
      `2026-07-17-specrunnerconfig-passthrough.md`, потребителя нет. Риск — зомби-пин,
      который «застыл» без интеграции. Наша часть — решение: ждать потребителя или
      написать в README контракта, что он отложен.
- [ ] Заморозить схемы MCP-тулзов (вендоринг пиненой копией по дисциплине ADR-ECO-003) @owner:github:andrei-shtanakov @trigger:"robin или Maestro начали вызывать dispatcher mcp" @id:freeze-mcp-tool-schemas @epic:eco.tooling
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
- [ ] Upstream-drift вахта для `contracts/maestro-repo-identity/v1` @owner:repo:dispatcher @id:maestro-identity-drift-watch @epic:eco.distributed-execution
      — сегодня есть только copy-integrity (таблица `cases.json`), второй гарантии нет;
      правило именования репозитория зеркалится из maestro, и расхождение делает
      контроллер слепым к собственному прогону (план среза 0, задача 1).
      **Пробел уже сработал 2026-08-22:** maestro#211 закрыл traversal, наша таблица
      сутки утверждала обратное про продюсера, и перепин случился потому, что человек
      заметил мерж, а не потому, что прибор доложил. Один раз — совпадение.

## KB snapshots: доставка через ветку derived-snapshots (резолюция ecosystem-kb#98)

- [x] `publish-snapshot` → ветка `derived-snapshots`: publisher и читатели уходят с master — PR #217 @owner:github:andrei-shtanakov @id:snapshot-publish-branch @epic:eco.ops
      Принятие inbox-issue #199 от prograph-vault (ADR-ECO-006, резолюция
      ecosystem-kb#98 от 2026-08-26). `derived/snapshots` — регенерируемая
      проекция, не authority: её машинный writer не получает bypass
      защищённого master. Ветка `derived-snapshots` уже создана владельцем и
      засеяна (`b01f390`), не защищена — прямой пуш проходит. Scope:
      (1) publisher (`dispatcher publish-snapshot` / launchd-джоб) пушит
      только в `derived-snapshots`, меняет только собственный `<host>.json`,
      при non-fast-forward — fetch → rebase → повтор (ветку обновляют
      несколько машин); (2) публикация — через выделенный служебный
      checkout/worktree, никогда не коммит в текущую checked-out ветку
      основного KB-чекаута (прецедент: снапшот уехал в постороннюю
      feature-ветку, бывший `84a1132`); (3) читатели (`kb_snapshot_dirs` в
      `dispatcher/core/sync.py` и далее) читают снапшоты явно из
      `origin/derived-snapshots` (fetch + `git show` либо служебный worktree),
      не переключая основной KB-чекаут — локальный master намеренно больше не
      несёт актуальный машинный read-model; (4) деградация: ветка
      отсутствует/недоступна → существующие `unknown`/`stale`, не поломка
      панели. Если коллизии машин на одной ветке станут регулярными —
      следующий шаг per-host refs или отдельный snapshot-репо, но не bypass
      master.
- [x] Перевести паблишер на EPGETBIW050F на новую доставку — остановить дрейф локального master vault-чекаута — PR #217 (код) + деплой хоста 2026-08-28 @owner:github:andrei-shtanakov @blocked_by:todo://dispatcher/snapshot-publish-branch @id:publish-snapshot-master-drift @epic:eco.ops
      Деплой = `git pull` этого чекаута (launchd-джоб `dev.atp.dispatcher.snapshot`
      гоняет `uv run dispatcher publish-snapshot` из него, plist не менялся).
      Накопленный с утреннего сброса дрейф (4 снапшот-коммита старого паблишера,
      только `derived/snapshots/EPGETBIW050F.json`) снят повторным
      `reset --hard origin/master`. Done-признак подтверждён: плановый прогон
      17:19 и kickstart 17:29 (2026-08-28) легли в `origin/derived-snapshots`
      (`87f9acc`, `42d3809`), локальный master vault = origin, лишних worktree нет.
      Принятие inbox-issue #213 от prograph-vault (эскалация #199). Пока
      `snapshot-publish-branch` не доехал до хоста, каждый плановый прогон
      снова коммитит `chore(snapshots): EPGETBIW050F sync snapshot` в
      локальный master рабочего checkout prograph-vault: 26–28.08 — ~89
      коммитов, расхождение с origin на 93, сломанный `git pull --ff-only`
      для людей и инструментов. Vault уже подчистил у себя (спасение журнала
      PR prograph-vault#108, ручная доставка `8ffaff6` в `derived-snapshots`,
      reset локального master), но дрейф возобновляется на каждом прогоне по
      расписанию. Done-признак: плановый прогон на EPGETBIW050F кладёт снапшот
      в `derived-snapshots` через служебный worktree, локальный master
      vault-чекаута новых снапшот-коммитов не получает.

## Launchpad (спека docs/superpowers/specs/2026-08-26-launchpad-design.md)

- [x] PR-B1: store + admission + single-live-run гейт + оба escape — ветка `feat/launchpad-b1` смержена (8 задач SDD, финальное ревью + fix-волна чисты) (PR #200) @owner:github:andrei-shtanakov @id:launchpad-b1
- [x] PR-B2: ре-вендор plan-fields с `@dag` + inventory reader — план `docs/superpowers/plans/2026-08-26-launchpad-b2-inventory.md` (PR #204) @owner:github:andrei-shtanakov @id:launchpad-b2
- [ ] PR-C: `/api/launchpad` + структурные 409 + UI (в работе — ветка `feat/launchpad-c`, план `docs/superpowers/plans/2026-08-27-launchpad-c-api-ui.md`) @owner:github:andrei-shtanakov @id:launchpad-c
      Наследует записанные хвосты B1: ~~персистентность отказов lock-семьи
      (launch_busy/lock_malformed/lock_io_unreadable сейчас эфемерны by
      design)~~ (неверно — уже персистятся: PR #200 добавил
      `mark_admission_rejected`/`record_admission_rejection` для
      lock-семьи, строка устарела на момент написания);
      perf — `_capture_run_facts` сканирует весь request store на каждый submit;
      escape/reconciliation для crash-окна terminal-перехода (здоровый лок,
      которым владеет терминальная запись, — см. TODO в докстринге
      `mark_admission_rejected`); ~~message-split capture-phase отказов
      submit~~ (вытеснено v2 структурной таксономией 409 — задача 4 этой
      ветки, feat/launchpad-c).
      ~~Хвосты B2: спека §5.1 «on both items» → «on both open items» (ruling
      задачи 4); §6.1 `repo_url` — НЕ Mode-2 маркер, shipped/test-pinned
      семантика submit принимает его как remote-URL именование репо,
      дискриминатор — только `workstreams` (ревью PR #202, code-over-spec);
      §7 нужен проход под НОВОЙ семантикой — linked-unreadable теперь
      `run_state_unreadable` (owner override на ревью B1), а не
      предполагавшееся in-flight.~~ (все три поправлены в спеке этой веткой,
      задача 1, feat/launchpad-c).
- [ ] `snapshot_id` в submit v2 нигде не эхоится @owner:github:andrei-shtanakov @id:launchpad-snapshot-id-echo
      Спека зовёт его audit echo — либо поле в `LaunchRecord`, либо правка спеки.
- [ ] Capture укладывается в ≤3 git-подпроцесса на файл `dags/` за submit и на репо за assembly (B2 M8) @owner:github:andrei-shtanakov @id:launchpad-perf-capture
      Оптимизация — только когда замер покажет вред, риды ассемблера уже
      once-per-assembly.
- [ ] §10 живая приёмка среза launchpad @owner:github:andrei-shtanakov @id:launchpad-live-acceptance
      Один боевой прогон реального пункта бэклога через панель — записанные
      `work_id`, полная `seen_revision`, ровно один `request_id`, `run_branch`
      созданный рантаймом, дефолтная ветка не сдвинута, терминальный исход в
      Recent completed. Ведёт владелец, после мержа.
- [ ] Флейк `test_run_end_through_the_resolution_path_also_binds_to_the_checkout` (fake-maestro subprocess timing; выстреливал в плане B1 и падает и standalone ~25-50% — замер 2026-08-26 на fbaed1c и HEAD, симптом: поздняя строка `run` от исходного launch-процесса перекрывает `run-end` в cwd-логе) @owner:github:andrei-shtanakov @id:flake-run-end-checkout
- [ ] Флейк `test_revendor_script.py::test_a_signal_mid_run_leaves_the_working_copy_alone[SIGINT]` (периодически в полном прогоне, standalone зелёный) @owner:github:andrei-shtanakov @id:flake-revendor-sigint

## Waits graph (спека docs/superpowers/specs/2026-08-26-waits-graph-design.md)

- [x] Графовый вид ожиданий флота в панели: рёбра @blocked_by + сторожевые @trigger — #205 (пара) + #207 (имплементация) @owner:github:andrei-shtanakov @id:waits-graph-view @epic:eco.plan-fields
      Принятие inbox #201, влито 2026-08-27. Slice 0: `core/waits.py` поверх
      канонического `plan_fields` (parse_fleet + check_fleet +
      check_legacy_fleet — ожидания без-@id пунктов видимы находками, включая
      доставленные), `GET /api/waits` (всегда 200), SVG-секция в панели
      (stale — состояние ребра с бейджем «доставлено»; loose/findings двумя
      несшиваемыми таблицами; сериализованный опрос). Живая приёмка: 26
      канонических рёбер, 6 переходных в loose_refs, 64 триггера. Published
      snapshot как источник — отдельное расширение по мультимашинному
      триггеру (см. §6 спеки). Ложная находка терминального ревьюера
      («файлов нет в дереве») опровергнута в #207 — наводка обвязке
      review-pr.sh: отдавать ревьюеру дерево головы, не чекаут.

## Переработка интерфейса: вкладки и главный экран Launchpad (спека docs/superpowers/specs/2026-08-29-tabbed-interface-design.md)

Приёмка FR-02 (Must) из `spec/discovery-brief-customer-ui.md`: «ни один экран не
смешивает темы». Черновик воркспейса
`_cowork_output/plans/2026-08-27-dispatcher-tabbed-interface-rework.md` — dev-scratch,
канон — спека в репо. Три PR-а, PR-1 первый по зависимости.

- [x] PR-1: тестовая среда + tab shell + главный экран Launchpad + развязка refresh — PR #220 влит 2026-08-29 (35 коммитов, 7 задач TDD + 8 прогонов терминального ревью) @owner:github:andrei-shtanakov @id:tabbed-ui-shell @epic:eco.ops
      Девять вкладок, `Launchpad` первый и по умолчанию; run view — drill-down
      `#launchpad/<request_id>`, отдельной вкладки `Run console` нет; опрос
      только активного экрана; условная десятая вкладка `Benchmarks`.
      Шесть находок терминального ревью закрыты. Последняя вскрыла класс:
      вызывающий загрузчик выбрасывал исход, и это чинилось точечно четыре
      раза подряд. Закрыто структурно — `runLoader(screen)` единственный вход
      (`loadActiveScreen` удалён), Launchpad отчитывается через тот же
      `applyOutcome`, а харнесс-кейс читает поставляемую страницу и падает,
      если вызов загрузчика появился мимо точки входа (на доисправленной
      странице находит 20 мест).
      **Визуальная приёмка владельцем (1440/390 px, тёмная и светлая схема,
      различимость активной вкладки не только цветом) на момент мержа не
      проведена.**
- [ ] Фокус остаётся на скрываемой кнопке ещё в двух местах @owner:github:andrei-shtanakov @id:hidden-focus-strays @epic:eco.ops
      Тот же класс, что починен в PR #220 для вкладки `Benchmarks`:
      `suggestSetBusy(false)` прячет `#spec-runner-config-suggest-cancel` по
      завершении запроса, и `#mg-retry-sync` прячется по `outcome.ok` в merge
      gate. Оба скрываются по состоянию, а не по действию оператора, поэтому
      клавиатурный фокус остаётся на скрытом элементе. Проверены и признаны
      безобидными: `overlaySetState` (оператор сам жмёт эти кнопки, на месте
      появляется соседняя) и `applyRoute` (смену экрана ведёт оператор, фокус
      уже на вкладке).
      Девять вкладок, `Launchpad` первый и по умолчанию; `Run console` исчезает
      из верхней навигации и становится вложенным инструментом (run view —
      drill-down `#launchpad/<request_id>`, manual-форма — свёрнутый блок).
      Названные в спеке решения владельца: скрытый Launchpad опрашивается,
      пока в `lpState.pending` есть незакрытая попытка, и умолкает после её
      разрешения; глобальной полосы-уведомления не вводим; раскрытое
      подтверждение переживает возврат только после ре-валидации против
      актуального снапшота. Найдено при написании плана и решено в спеке:
      `Benchmarks` — условная десятая вкладка (§3.1), колонка `Contract` в
      Roadmap кормится из `/api/contracts` и переживает развязку загрузчиков
      (§9). Цена, которую нельзя пропустить: `tests/web/dom.js` не знает
      `location`/`history`, а `dispatch()` не кликает по невидимому — девять
      harness'ов чинятся в той же задаче, что вводит панели (§10).
- [ ] PR-2: `OverviewEntry.directory` + read-only README endpoint + экран Projects (список каталогов слева, полный README справа) @owner:github:andrei-shtanakov @id:tabbed-ui-projects @epic:eco.ops
      Спека §6. Снимает UI-входы старого Project detail (onboarding, governance,
      product proposals, orchestration runs, merge gate, task authoring,
      spec-runner config) — backend не удаляется, изменение доступности
      называется в PR первой строкой. План пишется после мержа PR-1.
      Найдено в PR-1 (терминальное ревью 7 + разбор потребителей `sub`):
      **`#projects/<directory>` объявлен спекой §4.1, но не реализован.**
      Роутер его принимает (`sub: true` в реестре — иначе код противоречил бы
      спеке), однако экран Projects выбор пишет в `errorsProject` по клику
      карточки и `route.sub` не читает никогда. Единственные потребители
      `sub` во всей странице — Launchpad'овы. PR-2 обязан либо реализовать
      адресацию, либо снять `sub: true` вместе с правкой спеки.
- [ ] PR-3: перекомпоновка экрана Sync без смены источника истины @owner:github:andrei-shtanakov @id:tabbed-ui-sync @epic:eco.ops
      Спека §7. Repo-centric строки, явный статус источника каждой машины
      (`live`/`published`/`stale`/`unavailable`), обе машины отдельно. Сырой JSON
      github-checker в браузер не транслируется. План пишется после мержа PR-1.

## Хвосты качества

- [x] Проход 1 слайса 0 ПРИНЯТ — deployer#40 @owner:github:andrei-shtanakov @id:df-slice0-pass1-acceptance
      Принят вторым прогоном 2026-08-24 (`01M0T5HA1PW0J0GWTCGMZVFWW0`, запрос выдан
      из консоли). Пункт `todo://deployer/envrc-context-ignore` пронесён контуром от
      кнопки до записи в `Shipped`: красно-зелёная пара воспроизведена вручную против
      красного коммита, 476 тестов зелёные.
      **Изоляцию обеспечила система, а не человек.** `git.run_branch` (maestro#216
      фаза A) создал `pilot/envrc-context-ignore` от `master` до публикации прогона;
      до нажатия кнопки не выполнялось ни одной git-команды, `master` не сдвинут.
      Именно этого не хватало ПЕРВОМУ прогону (deployer#36): задача и верификация там
      прошли, а изоляция провалилась — Mode 1 коммитил прямо в `master`, и условие
      «без прямых коммитов в protected-ветку» обеспечило ручное восстановление
      постфактум. Два результата не сливались в один, и это различие двигало всю
      работу.
      Три системных дефекта, которые нашёл только боевой прогон, а не тесты и не
      ревью: глаголы адресовали не тот репозиторий (#174), `ATP_CATALOG` был
      окружением вместо конфигурации (#176), `branch_prefix` в Mode 1 мёртв
      (maestro#216). Первые два закрыты здесь, третий у соседа.
      Улики изоляции взяты ДВУМЯ следами из трёх: на момент приёмки событий
      `run_branch_gate.*` не существовало, хотя §8 спеки обещала три. Расхождение
      закрыто соседом сразу после — maestro#223 добавил `.created`/`.verified`/
      `.refused`, — но приёмка состоялась без них, и переписывать это задним
      числом нельзя: два следа были наблюдаемым состоянием репозитория, а не
      самоотчётом системы, и их хватило.
- [x] Постоянный launchd-агент для dispatcher — PR #189 @owner:github:andrei-shtanakov @id:atp-catalog-in-service-config @epic:eco.ops
      Прежняя формулировка («закрепить `ATP_CATALOG` в конфигурации запуска
      сервиса») описывала уже устранённый дефект: после #176 каталог объявлен в
      `dispatcher.toml`, проверяется до запуска процесса и передаётся ребёнку
      поверх окружения. От того, как стартовал сервер, он не зависит.
      Оставалось другое — сама служба. `scripts/dispatcher_launchd.sh`
      генерирует plist, а не коммитит машинно-зависимый: каждый абсолютный путь
      выводится на глазах читателя. `ATP_CATALOG` в plist НЕ дублируется —
      источник истины один, и два бы разошлись. Рестарт доказан исполнением:
      убитый процесс заменён новым, UI отвечает, конфиг прочитан тот же
      (`/api/runs/<неизвестный>` → 404, а не 409).
      Ротация добавлена PR #190: copy-truncate вторым агентом, ставится вместе
      со службой. `newsyslog` не годится — он ротирует переименованием, а
      launchd держит дескриптор открытым: измерено, служба пишет в
      ротированный файл, а новый остаётся пустым навсегда.
- [x] Прогнать live-смоук write-path с реальным `github-checker` на PATH — PR #98 @owner:github:andrei-shtanakov @id:live-smoke-write-path
      Выполнено 2026-08-01 локально: `test_write_path_live_smoke_real_binary` прошёл на
      `dispatcher@b571cba` против бинаря, установленного из `github-checker@ef03fef`
      (тот же коммит, на который запинен вендоренный контракт). Сюита — 650 passed,
      0 skipped. Отдельно от этого пункта CI-сторона закрыта в
      `@id:design-405-level-3-never-runs`.
- [x] Route-level тест сериализации `ok=True` для четырёх additive-полей `ActionOutcome` — PR #141 @owner:github:andrei-shtanakov @id:action-outcome-serialization-test
      Принятый follow-up PR #40. Пин значений `branch`/`base_branch`/
      `commit_sha`/`changed_paths` на успешном теле
      `update-spec-runner-config`: golden-тест провода видел их только как
      `null` на незаполненном outcome, потерю установленного значения он бы
      пропустил.
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
- [ ] Read-путь `project.yaml` эхом печатает PyYAML-ошибку с исходной строкой файла @owner:github:andrei-shtanakov @id:read-path-yaml-error-leak @epic:eco.ops
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
- [ ] `UnsafeEditError`'а traceback всё ещё несёт кадры с секретом — принятый остаток, не баг @owner:github:andrei-shtanakov @id:exception-traceback-frame-locals @trigger:"в dispatcher появляется traceback-форматтер/репортер с locals capture (Sentry include_local_variables, better-exceptions, rich) или иной потребитель полного traceback" @epic:eco.ops
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
- [x] README + иконка для `vscode-ext/` — PR #142 @owner:github:andrei-shtanakov @id:vscode-ext-readme
      Страница расширения показывала «No README available». README сверен
      с `src/`; иконка — сгенерированный 256×256 PNG (pulse-мотив), поле
      `icon` в манифесте; `npm run package` подтверждает включение обоих
      файлов в VSIX.
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
- [ ] Merge-gate: список открытых PR по репо (номер + заголовок), чтобы `#merge-gate` открывался кликом, а не ручным вводом номера @owner:github:andrei-shtanakov @id:merge-gate-pr-listing @epic:eco.ops
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

- [ ] Вёрстка экранов (`<style>` в `<head>` у `index.html`) вне досягаемости Node-харнесса: удаление всего блока оставляет сьют зелёным @owner:github:andrei-shtanakov @id:web-harness-does-not-see-css @epic:eco.ops
      Найдено финальным ревью ветки task-authoring (2026-07-31, coverage-gap,
      не блокер). Харнесс исполняет `<script>` из `<body>` над распарсенной
      разметкой и проверяет поведение, а не внешний вид; CSS он не применяет и
      применять не должен — `tests/web/dom.js` не layout-движок. Значит про
      «экран не разъехался» сьют не говорит ничего, и говорить не начнёт без
      настоящего браузера (Playwright + скриншот-диффы). Записано, чтобы зелёный
      прогон не читался как «вёрстка проверена».
- [ ] Визуальное различие B2 (`accepted: null` не выглядит отказом) держится только на CSS, которую харнесс не видит @owner:github:andrei-shtanakov @id:b2-visual-distinction-untested @epic:eco.ops
      Найдено при финальной сверке ветки консоли Dark Factory (PR по
      `feat/dark-factory-console`). Сегодня цвет верный: `.unknown` →
      `--warn: #b26a00` (янтарный), отличимый от `--bad: #c0392b` у `.err`.
      Но обеспечено это ничем: по `@id:web-harness-does-not-see-css` удаление
      всего блока `<style>` оставляет сьют зелёным, поэтому замена `.unknown`
      на `var(--bad)` нарушит владельческое решение B2 визуально при полностью
      зелёных тестах. Не дефект — непокрытая граница; закрывается тем же
      настоящим браузером, что и родительский пункт.
- [ ] Сбой `ensureActionToken()` рисуется как `accepted: null`, хотя доказуемо «не отправлено» @owner:github:andrei-shtanakov @id:console-token-failure-is-not-unknown @epic:eco.ops
      Токен запрашивается внутри `try` вокруг `fetch`, поэтому его падение
      попадает в транспортную ветку и даёт третье состояние. Неточно, но в
      безопасную сторону: консоль никогда не заявит «прогона нет», когда он мог
      начаться. Цена ошибки — оператор встречает поток разрешения из-за рядовой
      осечки авторизации. Принято сознательно на ревью задачи 1.
- [ ] Путь 404/409 в `rcPollView` не покрыт тестом @owner:github:andrei-shtanakov @id:console-poll-error-path-untested @epic:eco.ops
      Проверка `resp.ok` добавлена вместе со стражем устаревания и разбирает
      случаи «запись исчезла» и «контур выключен», но ни один кейс харнесса её
      не исполняет. Состояние `#rc-submit` эти пути намеренно не трогают.
- [ ] Зависший maestro-ребёнок: у оператора нет способа сдаться из консоли @owner:github:andrei-shtanakov @id:console-hung-child-no-give-up @epic:eco.ops
      Найдено ревью ветки консоли, признано остатком по решению спеки, а не
      пробелом UI. §5.2.1 в редакции 2026-08-22 убрала «явное разрешение
      оператора» из условий освобождения лока именно потому, что механизма у
      него не было, и заменила решаемым случаем (ребёнок вышел с ненулевым
      кодом + повторный взгляд в `runs/` пуст → `accepted: false`, терминал).
      `null` остаётся только для честного таймаута, когда ребёнок ещё жив —
      то есть когда прогон может появиться и продолжать блокировать правильно;
      `resolve_unknown` пересчитывает кандидатов при каждом клике, так что
      «подождать и нажать снова» — рабочий выход. Настоящий остаток — ребёнок,
      который не опубликует уже никогда; спека прямо называет восстановление
      действием в файловой системе. Кнопку «отменить» не добавлять: она дала бы
      отмахнуться от подлинной неоднозначности, что §5.2.1 запрещает.
- [ ] `tests/web/dom.js` — рукописная DOM-заглушка для task-authoring Node-харнесса; `jsdom` предпочтительнее, но отсутствует осознанно @owner:github:andrei-shtanakov @id:web-tests-hand-rolled-dom @epic:eco.ops
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

- [ ] Пин `fastmcp<3` и три отклонённых GHSA (OpenAPI SSRF, OAuth proxy, Gemini-CLI injection) — переоценить @owner:github:andrei-shtanakov @trigger:"бамп fastmcp до 3.x" @id:fastmcp-pin-reeval @epic:eco.ops
      Отклонены обоснованно: путь только stdio, без OpenAPI/OAuth/HTTP. Патчи есть лишь
      в 3.2.0, а пин `<3` держит мажор общим с Maestro.
- [ ] Двойное имя Maestro: коллектор отдаёт `"Maestro"`, канон репо — `maestro` @owner:github:andrei-shtanakov @trigger:"каталог Maestro/ переименован в maestro/ на диске" @id:maestro-double-name @epic:eco.ops
      Детект коллектора content-based (`maestro/` + `pyproject.toml`), поэтому
      переименование каталога discovery не сломает — но в Sync-вкладке repo придёт под
      именем каталога, и два пространства имён разойдутся. Тот же класс, что discovery-имя
      vs service-id у proctor: слепое переименование запрещено, нужна осознанная
      нормализация.
- [ ] Handoff в arbiter по ошибке `agent_id` в `report_benchmark` @owner:github:andrei-shtanakov @id:arbiter-agent-id-handoff @epic:eco.ops
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

## codex-review: потребитель кита steward (принят 2026-08-25)

- [x] PR-B: caller-workflow гейта codex-review — влит #188 (`fe8da31`, 2026-08-25, пустой вердикт с первого прогона) @owner:github:andrei-shtanakov @id:codex-review-caller
      По образцу пилота spec-runner: механика из base, потолки,
      generated-декларация, экономный триггер по драфту/лейблу; плюс лейбл
      `codex-review` и секрет `CODEX_REVIEW_API_KEY` (кладёт владелец в
      настройки репо) — после мержа PR-A.

  PR-A (этот): кит завендорен — `scripts/review/` (5 POSIX-скриптов) +
  `.github/codex/review-schema.json`, PIN @ steward `e4c43cc`;
  copy-integrity — джоба `review-kit-integrity` в ci.yml, чекер из base
  (на первом PR — бутстрап-notice); upstream-drift — вахта
  `review-kit-drift.yml` (не PR-гейт); `review-prompt.md` — данные репо,
  вне integrity; generated-декларация — `.gitattributes`. Ре-вендор —
  рецепт в комментарии PIN; дисциплина раундов гейта — спека steward §13;
  умолчание итераций — экономный цикл (local.sh → драфт → один платный
  прогон, см. Git workflow в CLAUDE.md).
