# Design — DESIGN-307: AI value suggestions in the spec-runner config editor

> **Context (2026-07-18):** доводка отложенного DESIGN-307 из
> `2026-07-17-spec-runner-config-editor-design.md` («recommendation only:
> агент префиллит значения формы, человек редактирует и одобряет; агент
> никогда не трогает PR-путь»). Numbering: DESIGN-901+. Дизайн прошёл
> критический разбор в две итерации (архитектурный выбор канала; bundle,
> cancel, peers-bias) — решения ниже зафиксированы с обоснованиями,
> включая два места, где первоначальная версия была отвергнута.

## 1. Architectural decision — LLM-канал

**Выбор: делегация локальному CLI (`claude -p`), захардённая; sidecar —
зафиксированный апгрейд-путь.** Рассмотрены и отклонены: прямой
anthropic-SDK-вызов из dispatcher; внешний агент через MCP; отдельный
suggestion-sidecar.

Обоснование через инварианты ПРОЕКТА, не общие места:

1. **Dispatcher держит ноль секретов — это граница, не случайность.**
   Ни БД-кредов, ни TLS; коллекторы активно РЕДАЧАТ чужие секреты
   (`_KEY_RE`/`_TOKEN_VALUE_RE`, `collectors/base.py`); единственный
   сетевой выход (git fetch / gh pr) уже отдан делегацией
   github-checker-у. Первый API-ключ в конфиге dispatcher — слом
   инварианта на категорию («не касается кредов» → «касается»), а не
   «ещё один секрет».
2. **Однопользовательский localhost-инструмент.** Concurrency-штормы,
   зомби-пулы и токен-дашборды — издержки hosted-мира, которого у
   dispatcher нет; зато claude CLI уже установлен и аутентифицирован на
   каждой машине пользователя — «ноль управления ключами» здесь
   буквальная правда, не маркетинг.
3. **Честность формулировки (по разбору): секрет РЕЛОЦИРОВАН, не
   устранён** — креды живут в конфиге CLI на том же хосте; blast radius
   для threat-model почти тот же, управляется CLI, а не нами. Спека
   называет это прямо.
4. **Sidecar (изолированный suggestion-сервис) доминирует CLI-делегацию
   только в мультипользовательском мире** (телеметрия, ретраи,
   стоимость-лимиты). Для v1 он добавляет деплоймент без выгоды. Контракт
   DESIGN-902 оформлен так, что замена spawn→HTTP — замена транспорта,
   не редизайн.

## 2. Hardening decisions (все — из критического разбора)

| # | Решение | Обоснование |
|---|---|---|
| H-1 | Контекст — ТОЛЬКО stdin; команда — из allowlist (`claude` / абсолютный путь до него в конфиге), флаги фиксированы кодом; `shell=False` | интерполяция контекста в argv = классический путь к инъекции/RCE |
| H-2 | Пин `--output-format json`: stdout — конверт агента (`type`, `result`, `cost_usd`, …), полезная нагрузка — СТРОКА в `.result`, которая парсится вторым проходом | «первый JSON в stdout» поймал бы конверт, и валидация честно отвалилась бы на его структуре; без пина формат stdout не гарантирован между версиями CLI |
| H-3 | Вся CLI-специфика (конверт, извлечение `.result`, версия CLI) изолирована в адаптере DESIGN-902 | замена spawn→HTTP (sidecar) не должна тянуть предположения о конверте |
| H-4 | Bundle строится из УЖЕ ЗАМАСКИРОВАННОГО вывода (те же регэспы коллекторов) — чужой токен не уезжает в модель | иначе фича сама ломает свидетельство №1 из §1 |
| H-5 | Непрямая prompt-injection через чужой конфиг-контент — ВОЗМОЖНА и названа: содержимое чужих конфигов втекает в промпт; blast radius локализован typed-валидацией выхода + human accept | закрыть полностью нельзя — честно ограничить и задокументировать |
| H-6 | Cancel = `proc.terminate()` над собственным дочерним процессом через явный endpoint; лок освобождается немедленно, трата обрезается | «kill-по-disconnect ненадёжен» относится к детекции ушедшего HTTP-клиента, НЕ к убийству процесса, чей handle у нас в руках; cancel-«отпустить-UI» держал бы лок и деньги до конца таймаута |
| H-7 | Остаточный риск cancel: race «terminate после уже-улетевшего API-вызова» — одна генерация может быть оплачена | узкое окно, не 60-секундная дыра |

## 3. Content decisions

### Bundle (что видит модель) — и два ВЫРЕЗАННЫХ включения

Включено, каждое с причинной цепочкой:
- `instruction` — фиксированный версионируемый текст промпта (константа в
  коде), требует строгий JSON-ответ и rationale, ОПИРАЮЩИЙСЯ на
  распределения peers («3 из 8 так»), а не на конформизм.
- `requested_fields` — только default-provenance поля (explicit —
  человеческие решения, их не трогаем даже подсказкой).
- `field_schema` — тип/constraints (min/max/enum)/default каждого из 12
  typed-полей → модель не гадает форматы.
- `current_config` — все 12 полей с provenance → подсказки не
  противоречат уже принятому стилю проекта.
- `peers` — **распределения, не сырые значения** (см. ниже).
- `project.description` (+source) из FR-04-обогащения → языковой
  стек/тема проекта причинно влияют на claude_model/review_model и
  test/lint_command.

**Вырезано: roadmap (полностью — и summary, и next_items).** Исходный
текст DESIGN-307 упоминал roadmap-контекст; разбор потребовал причинную
цепочку «roadmap-сигнал → значение typed-поля» — её не существует
(«lagging → подними max_retries» — гадание). Критерий возврата:
появление поля, на значение которого roadmap влияет причинно. Выгода
вырезания: меньше токенов, меньше injection-поверхности, ноль
потерянного lift-а.

**Вырезано: errors/models/contracts/sync, сырой README,
`extra_executor_config`** — не влияют на значения 12 полей / вне скоупа.

### Peers — распределения с анти-эхо-камерой

Для каждого из 12 полей: `{value → {count, explicit_count}}`, топ-5
значений по частоте + `"other": N`. Закрывает разом:
- **cap**: размер ограничен distinct-значениями, не числом проектов;
  отбор проектов — все при ≤15, иначе топ-15 по freshness (правило
  простое и наблюдаемое);
- **monoculture-bias** (из разбора): «большинство ≠ правильно» — если
  три соседа растиражировали плохой дефолт, `explicit_count` показывает,
  сколько значений — человеческие решения, а сколько — эхо; модель
  обязана (instruction) показывать распределение в rationale, а не
  выдавать конформизм за консенсус.

### Ответ и частичное принятие

`.result` парсится как
`{"suggestions": {field: {"value": ..., "rationale": "<1 предложение>"}}}`.
Каждое value → `validate_typed_fields`. **По-полевое частичное
принятие**: невалидные, непрошенные и совпадающие-с-дефолтом поля
отбрасываются с видимой пометкой «N dropped: …» — CLI-дрейф локализуется
по полю; полностью нераспарсенный ответ (конверт или `.result`) → явная
ошибка «suggestion invalid», НЕ тихий мусор в форме.

## 4. Components

### DESIGN-901: bundle-билдер (`dispatcher/core/suggest_bundle.py`)

Чистая функция `build_suggest_bundle(cfg, peers_cfgs, snapshot) -> dict`:
состав по §3, детерминированный (стабильная сортировка ключей и
распределений), редакция H-4 применяется ко ВСЕМ строковым значениям
перед сериализацией. Юнит-тестируется без CLI.

### DESIGN-902: CLI-адаптер (`dispatcher/core/suggest_cli.py`)

`SuggestRunner`: собирает argv из allowlist-бинаря + фиксированных
флагов (`-p --output-format json`), пишет bundle в stdin, таймаут 60 s,
`shell=False`. Парсит конверт → `.result` → suggestions; наружу отдаёт
типизированный результат
`SuggestOutcome {suggestions, dropped, cli_version, duration_s, cost_usd|None}`
— НИКАКИХ деталей конверта выше адаптера (H-3). Держит handle процесса;
`terminate()` — публичный метод для cancel (H-6). Один in-flight на
процесс (паттерн ActionRunner): занято → `SuggestBusyError`.

### DESIGN-903: endpoints + audit

- `POST /api/projects/{name}/spec-runner-config/suggest` — требует
  `X-Action-Token`: не потому что мутация (её нет), а потому что вызов
  ТРАТИТ ДЕНЬГИ — drive-by-cost закрывается той же CSRF-машинерией.
  Ошибки: 404 unknown project, 409 busy, 409 cancelled, 422 «suggestion
  invalid», 503 CLI не настроен/не найден (паттерн gated-фичи).
- `POST /api/projects/{name}/spec-runner-config/suggest/cancel` — тот же
  токен; `terminate()` текущего процесса; идемпотентен (нет in-flight →
  200 «nothing to cancel»).
- Audit: логгер `dispatcher.actions.spec_runner_config` (существующий):
  строка на каждый запуск — project, cli_version, duration, поля
  suggested/dropped, `cost_usd` — **optional** (subscription/OAuth-конверт
  может его не отдать; строка переживает пропуск), исход
  (ok/cancelled/timeout/invalid).

### DESIGN-904: web UX

Кнопка «Suggest values» в конфиг-панели: elapsed-счётчик
(«Suggesting… 12s») + кнопка Cancel рядом — редактор не выглядит
зависшим. Принятые подсказки: значение в инпуте + ТРЕТЬЕ состояние
provenance-маркера `suggested` (визуально отличимо от explicit/default)
+ dim-rationale рядом; dropped-пометка над формой. В PR-путь подсказка
попадает ТОЛЬКО через существующий preview-diff → submit — DESIGN-304
не отличает «человек напечатал» от «человек принял»; агент к PR-пути не
прикасается (инвариант DESIGN-307 сохранён). CLI не настроен → кнопка
disabled с подсказкой «suggestions unavailable: claude CLI not found»
(паттерн честной деградации github-checker-absent).

### DESIGN-905: Testing

| Scope | Пины |
|---|---|
| bundle | состав §3 (roadmap ОТСУТСТВУЕТ — негативный пин); распределения peers с explicit_count и топ-5+other; отбор топ-15 по freshness; **редакция: фикстура с токеном в чужом explicit-значении → в bundle он замаскирован** |
| адаптер | fake-CLI бинарь (прецедент fake github-checker): валидный конверт; конверт без cost_usd; `.result` — не-JSON (→ invalid); лишние/невалидные поля (→ dropped, остальные приняты); совпадение с дефолтом (→ dropped); таймаут; отсутствие бинаря |
| cancel | terminate освобождает лок НЕМЕДЛЕННО (следующий suggest не ловит 409-busy); cancel без in-flight идемпотентен |
| endpoints | токен-403; busy-409; 503 при ненастроенном CLI; audit-строка на каждый исход, включая пропущенный cost_usd |
| web | static-пины: suggest-кнопка, cancel, suggested-маркер, dropped-контейнер в index.html |
| injection | фикстурный «злой» конфиг соседа с инструкцией в значении поля → пин, что значение уезжает в bundle ЗАМАСКИРОВАННЫМ (если матчит секрет-регэспы) и что невалидный выход всё равно дропается типизацией (H-5 blast radius) |

### DESIGN-906: Documentation

README: секция «AI suggestions» (требования: claude CLI на PATH или путь
в dispatcher.toml; секрет живёт в CLI-конфиге — релоцирован, не
устранён; стоимость — на аккаунте пользователя). COWORK_CONTEXT:
interfaces line. Конфиг: `suggest_cli` (optional path) в
`DispatcherConfig`/`dispatcher.toml`.

## 5. Error handling

| failure | behaviour |
|---|---|
| CLI не настроен / не найден | 503 + disabled-кнопка с подсказкой; редактор полностью работоспособен |
| таймаут 60 s | terminate + 409 «timed out», лок освобождён, audit |
| конверт/`.result` нераспарсен | 422 «suggestion invalid», audit с исходом invalid |
| часть полей невалидна | принятые — в форму, dropped — пометкой; audit перечисляет |
| busy | 409, существующий паттерн |
| cancel | лок освобождён немедленно; H-7 race задокументирован |

## 6. Out of scope

- TUI/VSCode-поверхности подсказок (после приживания фичи).
- Sidecar-сервис (апгрейд-путь; контракт DESIGN-902 к нему готов).
- Кэш по хэшу bundle (опция при раздражающем недетерминизме — YAGNI).
- Автономное применение, объяснение полей, валидация человеческого
  ввода моделью (исходный скоуп DESIGN-307: recommendation only).
- `extra_executor_config` в bundle и в подсказках.

## 7. Traceability

| Item | Design |
|---|---|
| DESIGN-307 исходный скоуп (prefill, human accept, агент вне PR-пути) | §3 requested_fields, DESIGN-904 |
| Разбор 1: секрет релоцирован, не устранён | §1.3, DESIGN-906 |
| Разбор 1: stdin-only + allowlist + shell=False | H-1 |
| Разбор 1: конверт `--output-format json` / `.result` | H-2, H-3 |
| Разбор 1: непрямая injection + редакция bundle | H-4, H-5, DESIGN-905 injection |
| Разбор 2: cancel = terminate, лок и деньги | H-6, H-7, DESIGN-902/903/905 |
| Разбор 2: roadmap без причинной цепочки → вырезан | §3 (критерий возврата зафиксирован) |
| Разбор 2: peers cap + отбор + monoculture-bias | §3 peers (распределения, explicit_count, топ-15 по freshness) |
| Разбор 2: cost_usd optional; прогресс-UX | DESIGN-903 audit, DESIGN-904 |
| Ступенька к sidecar | §1.4, H-3, DESIGN-902 |

## 8. Milestone

Один PR (фича компактна: 2 новых core-модуля + 2 endpoint-а + web-панель
+ тесты); ветка `feat/config-suggestions`.
