# Design — extra_executor_config editing UI (web config editor)

> **Context (2026-07-18):** последний UI-хвост конфиг-редактора: overlay
> `extra_executor_config` сегодня preserve-only (web пинит `null`, TUI
> показывает read-only). Бэкенд ПОЛНОСТЬЮ готов с PR #40: tri-state в
> `ConfigCandidate` — `null` (preserve) | `{}` (intentional clear) |
> непустой dict (replace), `validate_extra_executor_config`
> против pinned-схемы `contracts/executor-config/v0-provisional/schema.json`,
> preserve/clear/replace-тесты, propose-pr. Фича — чисто клиентская
> (index.html); серверных правок НОЛЬ. Numbering: DESIGN-1001+. Одно
> ревью folded (три поправки отмечены ниже).

## 1. Decisions

1. **Формат — JSON-textarea.** Клиентский `JSON.parse` даёт мгновенную
   синтакс-обратную связь без зависимостей; родной YAML потребовал бы
   серверный parse-endpoint или вендоренный JS-парсер. Структурная
   форма по схеме (вложенные personas/hooks) — против YAGNI для редко
   редактируемого блока.
2. **Валидация двухступенчатая, и это названо честно (ревью-поправка 1):
   web Preview diff — ЛОКАЛЬНЫЙ** (рендерит изменённые typed-ключи, на
   сервер не ходит). Поэтому: синтаксис (JSON.parse + plain-object-guard)
   блокирует Preview/submit локально; **schema-ошибки
   (`validate_extra_executor_config`, 422 с перечнем) приходят только на
   «Confirm & open PR»** и рендерятся в существующем result-div.
   Серверный preview/validate-endpoint отвергнут ради «серверных правок
   ноль»; полный YAML-дифф человек видит в самом PR (его строит
   существующий серверный путь).
3. **Plain-object-guard (ревью-поправка 2):** валидный overlay — только
   `parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)`;
   `[]`, `"x"`, `42`, `null` — формально валидный JSON, но не overlay.
4. **Экспозиция секретов названа точно (ревью-поправка 3):** новая
   API/backend-экспозиция — НЕТ (per-name GET отдаёт overlay сырым с
   PR #40); новая ВИЗУАЛЬНАЯ экспозиция в web — ДА, если рендерить
   pretty-JSON автоматически (overlay может нести `telegram_bot_token`).
   Поэтому preserve-состояние НЕ показывает содержимое: только
   «overlay present (N keys), preserved as-is» + явная кнопка
   Edit (reveal). Содержимое появляется на экране только по явному
   клику человека.

## 2. DESIGN-1001: секция overlay — три явных состояния

Проблема tri-state UX — «пустая textarea» неоднозначна. Решение:
переходы только явными действиями, дефолт безопасный.

| Состояние | UI | Отправляется |
|---|---|---|
| **preserve** (дефолт) | collapsed: «no overlay» либо «overlay present (N keys), preserved as-is» — БЕЗ содержимого (§1.4); кнопки «Edit overlay» и «Clear overlay» | `null` |
| **edit** | textarea с pretty-JSON текущего overlay (`{}` если нет); живой `JSON.parse` + plain-object-guard на каждый input: невалидно → inline-ошибка (textContent), Preview/submit заблокированы; кнопка «Cancel» → preserve | распарсенный dict |
| **clear** | textarea скрыта; видимое предупреждение «блок extra_executor_config будет УДАЛЁН из project.yaml»; «Cancel» → preserve | `{}` |

- Пустая строка в edit = невалидный JSON = заблокировано — стереть
  overlay можно только явным clear (или введя `{}` руками); неоднозначность
  устранена конструктивно.
- **Каждый переход состояния и каждый input в textarea сбрасывает
  взведённый Preview** — вызовом `resetSpecRunnerConfigPreview()` (тот же
  механизм, что у typed-полей и AI-подсказок): инвариант «Confirm никогда
  не отправляет то, чего человек не видел взведённым».
- Локальный Preview дополняется одной строкой намерения по overlay:
  `overlay: preserved` / `overlay: will be cleared` /
  `overlay: replaced (N top-level keys)` — Confirm не слеп к overlay-части
  изменения, при этом схему локально не валидируем (§1.2).
- AI-подсказки (DESIGN-307) overlay не трогают (bundle его исключает);
  состояния независимы.

## 3. DESIGN-1002: сборка запроса

Новый хелпер `readSpecRunnerConfigOverlay()` возвращает по состоянию
секции: `null` (preserve) | `{}` (clear) | непустой dict (replace).
Ввод `{}` руками в edit-режиме эквивалентен clear — это консистентно с
бэкендом и НЕ считается отдельным состоянием; результат подставляется в body POST
`update-spec-runner-config` вместо сегодняшнего хардкода
`extra_executor_config: null`. Серверные 422 (schema-перечень от
`validate_extra_executor_config`) рендерятся существующим error-путём
result-div — без изменений.

## 4. DESIGN-1003: Testing

| Scope | Пины |
|---|---|
| static (tests/test_api.py index-тест) | id-маркеры секции: edit/clear/cancel-кнопки, textarea, warning-контейнер, intent-строка |
| server | НЕ трогаем — tri-state уже пинован (`test_spec_runner_config_actions`: preserve/clear/replace, Copilot data-loss regression) |
| финальное ревью | живой click-through: edit→invalid JSON блокирует Preview; array/скаляр блокируются guard-ом; clear показывает warning; переходы сбрасывают взведённый preview; preserve не рендерит содержимое overlay до клика Edit |

## 5. Error handling

| failure | behaviour |
|---|---|
| невалидный JSON / не-plain-object в textarea | inline-ошибка, Preview/submit заблокированы (локально) |
| schema-нарушение overlay | 422 с перечнем на Confirm (существующий серверный путь), result-div |
| конфликт base_mtime | существующий 409-путь, без изменений |

## 6. Out of scope

- TUI/VSCode-редактирование overlay (read-only остаётся; отметка в их
  комментариях не требуется — поведение не меняется).
- Серверный preview/validate-endpoint (§1.2 — отвергнут).
- Схема-подсказки/автокомплит в textarea.

## 7. Traceability

| Item | Design |
|---|---|
| Ревью-поправка 1: локальный preview, 422 только на Confirm | §1.2, §2 intent-строка |
| Ревью-поправка 2: plain-object-guard | §1.3, §4 финальное ревью |
| Ревью-поправка 3: визуальная экспозиция → collapsed preserve + reveal-by-click | §1.4, §2 preserve |
| Tri-state однозначность | §2 (явные переходы, пустая строка = заблокировано) |
| Preview-disarm инвариант (DESIGN-306/307 прецедент) | §2 |

## 8. Milestone

Один PR, ветка `feat/extra-config-editing`; план — 2 таска (UI + доки),
едет в фиче-ветке без отдельного plan-PR (прецедент DESIGN-307).
