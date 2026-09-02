---
spec_stage: requirements
status: draft
owner_role: product
traces_to: [charter]
upstream_hashes:
  charter: "6dbfc030a29fe0162c4ea164643c2b168836aafd"
---

# Requirements — PF-OWNER-REPO-SELF: диагностика владельца `repo:<свой>` в `plan_fields`

## Назначение

Документ определяет наблюдаемое и проверяемое поведение fleet-потребителей
`plan_fields`, когда typed owner типа `repository` указывает на репозиторий
provenance того же пункта плана. Требования не меняют грамматику single-repo
parser и не назначают владельца автоматически.

## Термины

- **Source repository** — репозиторий, к которому относится provenance
  проверяемого plan node.
- **Owner repository** — репозиторий, на который ссылается
  `owner_ref` типа `repository`.
- **Canonical identity** — manifest key репозитория после разрешения допустимого
  manifest key или `git_dir`-написания через frozen workspace manifest.
- **Self-owner** — owner repository и source repository имеют одну canonical
  identity.
- **External repo-owner** — owner repository известен manifest и его canonical
  identity отличается от source repository.
- **Unknown repo-owner** — owner repository невозможно разрешить через frozen
  manifest.

## Функциональные требования

#### FR-01: Определение self-owner по канонической identity
**Priority**: Must

Fleet-анализ должен классифицировать owner как self-owner, если `owner_ref.type`
равен `repository`, а canonical identity owner repository совпадает с canonical
identity source repository из provenance того же node.

Критерии приёмки:

- `@owner:repo:dispatcher` в node из `dispatcher/TODO.md` распознаётся как
  self-owner, если `dispatcher` — canonical manifest key source repository.
- Сравнение выполняется после manifest-нормализации, а не по строковому
  равенству исходного owner tag и provenance.
- Результат не зависит от состояния checkbox node.
- Owner другого типа, включая typed person/team и `TBD`, не попадает под эту
  классификацию.

#### FR-02: Распознавание git_dir-написания собственного репозитория
**Priority**: Must

Fleet-анализ должен разрешать объявленное в frozen workspace manifest
`git_dir`-написание owner repository в ту же canonical identity, которая
используется для source repository. `git_dir`-локатор — единственная
альтернативная manifest-форма записи репозитория; отдельного понятия alias
identity-модель не содержит.

Критерии приёмки:

- Допустимое `git_dir`-написание source repository приводит к тому же
  self-owner verdict, что и canonical manifest key.
- Необъявленное написание не считается совпадением только из-за сходства строк.

#### FR-03: Диагностика PF-OWNER-REPO-SELF
**Priority**: Must

Для каждого self-owner node fleet-анализ должен выдавать ровно одну
диагностику с кодом `PF-OWNER-REPO-SELF` и severity `warning`.

Критерии приёмки:

- Диагностика привязана к URI проверяемого node.
- Диагностика содержит provenance, достаточный для определения исходного
  репозитория, файла и местоположения пункта.
- Сообщение объясняет, что repository owner совпадает с репозиторием самого
  пункта и потому не передаёт ответственность внешнему principal.
- Диагностика предлагает исправление: назначить реального typed principal либо
  явно использовать `TBD`.
- Одна owner-ссылка не порождает дубликаты `PF-OWNER-REPO-SELF`, даже если
  repository identity доступна через несколько manifest-имён.

#### FR-04: Взаимоисключающие verdicts repository owner
**Priority**: Must

Fleet-анализ должен выдавать для корректного `owner_ref` типа `repository`
ровно один из трёх owner verdicts: self-owner, external repo-owner или unknown
repo-owner.

Критерии приёмки:

- Self-owner получает `PF-OWNER-REPO-SELF` и не получает
  `PF-OWNER-REPO-UNKNOWN`.
- Неизвестный manifest repository получает только существующую диагностику
  `PF-OWNER-REPO-UNKNOWN` и не получает `PF-OWNER-REPO-SELF`.
- Известный внешний repository остаётся валидным repo-owner и не получает ни
  `PF-OWNER-REPO-SELF`, ни `PF-OWNER-REPO-UNKNOWN`.
- Ошибка грамматики owner обрабатывается существующим grammar verdict и не
  переопределяется identity-диагностикой.

#### FR-05: Исключение self-owner из валидного repo-owned состояния
**Priority**: Must

Fleet read-model и reporter-facing представления не должны учитывать
self-owner node как валидно назначенный external `repo-owned` node.

Критерии приёмки:

- Агрегаты и owner views, различающие назначенное repo-владение, не включают
  self-owner в класс валидного external repo-owner.
- Машинный результат позволяет потребителю однозначно отличить self-owner от
  external и unknown состояний без повторной строковой нормализации.
- Все fleet reporters применяют одну и ту же классификационную семантику.

#### FR-06: Сохранение исходных данных и объяснимости
**Priority**: Must

Добавление self-owner verdict не должно изменять или терять исходное значение
`owner_ref.raw`, node URI и provenance.

Критерии приёмки:

- В результате доступно исходное написание owner tag, включая
  `git_dir`-форму.
- Пользователь может по диагностике перейти к конкретному пункту и увидеть
  исходную запись, вызвавшую finding.
- Анализ не переписывает `TODO.md`, manifest или иные входные артефакты.

#### FR-07: Обратная совместимость single-repo parsing
**Priority**: Must

Single-repo parser должен продолжать проверять синтаксис `repo:<key>`, не
выдавая identity-verdict `PF-OWNER-REPO-SELF`, для которого необходим workspace
manifest и provenance fleet node.

Критерии приёмки:

- Синтаксически корректный `@owner:repo:<key>` остаётся допустимым результатом
  single-repo parsing.
- Новая диагностика появляется только в контексте fleet-анализа с frozen
  manifest.
- Существующие single-repo parser contracts и валидные fixtures сохраняют
  совместимость.

#### FR-08: Контракт и документация диагностики
**Priority**: Must

Канонический контракт `plan_fields` и пользовательская документация должны
описывать код `PF-OWNER-REPO-SELF`, его severity, область применения и способ
исправления.

Критерии приёмки:

- Контракт различает self-owner, unknown repo-owner и валидного external
  repo-owner.
- Документация содержит примеры canonical key и `git_dir` self-owner.
- Документация рекомендует заменить self-owner на реального typed principal
  либо `TBD` и не обещает автоматического назначения.
- Канонический документ проходит применимую schema validation.

#### FR-09: Автоматическая проверка поведения
**Priority**: Must

Conformance suite и профильные тесты должны закреплять identity-нормализацию,
диагностику и классификацию self-owner.

Критерии приёмки:

- Тест проверяет canonical-key self-owner и ровно одну warning-диагностику с
  кодом `PF-OWNER-REPO-SELF`, node URI и provenance.
- Тесты проверяют `git_dir`-вариант собственного репозитория.
- Негативные тесты проверяют известный внешний и неизвестный repository.
- Тест подтверждает отсутствие одновременных `PF-OWNER-REPO-SELF` и
  `PF-OWNER-REPO-UNKNOWN` для одной owner-ссылки.
- Reporter-facing тест подтверждает, что self-owner не считается валидным
  external `repo-owned` состоянием.
- Single-repo parser regression test подтверждает требование FR-07.

#### FR-10: Управление включением в governance gate
**Priority**: Should

До отдельного продуктового решения `PF-OWNER-REPO-SELF` должна оставаться
наблюдаемой warning-диагностикой и не должна неявно становиться новым
обязательным governance gate.

Критерии приёмки:

- Базовая severity равна `warning`.
- Включение диагностики в обязательный gate выполняется отдельным явным
  решением и сопровождается оценкой числа существующих self-owner записей.
- Отсутствие gate enforcement не скрывает finding из fleet output и reporters.

## Нефункциональные требования

#### NFR-01: Детерминизм

При одинаковых plan inputs, provenance и frozen workspace manifest анализ
должен формировать одинаковые owner verdicts, diagnostics и классификацию.

#### NFR-02: Единая identity-модель

Реализация должна переиспользовать manifest identity-модель fleet-слоя для
разрешения canonical key и `git_dir`, не вводя отдельный несовместимый
алгоритм сопоставления owner.

#### NFR-03: Read-only безопасность

Проверка должна быть read-only: она не изменяет планы, manifest, contract или
рабочее дерево и не выполняет внешних мутаций.

#### NFR-04: Стабильность машинного контракта

Код `PF-OWNER-REPO-SELF`, severity, node URI и provenance должны быть доступны
в стабильной структурированной форме, пригодной для conformance tests и
машинных потребителей.

#### NFR-05: Совместимость и локальность изменения

Новая семантика не должна менять правила `@blocked_by`, `@trigger`, `@dag`,
`@epic`, legacy role или семантику владельца, указывающего на другой известный
репозиторий.

## Ограничения и вне объёма

- Автоматический выбор, подстановка или создание нового owner.
- Запрет формы `repo:<key>` на single-repo parser уровне.
- Проверка наличия конкретного человека или команды внутри owner repository.
- Массовое исправление существующих `TODO.md`.
- Изменение severity выше `warning` или включение обязательного gate без
  отдельного решения по FR-10.

## Матрица трассируемости

| Источник charter | Требования |
|---|---|
| Canonical identity и self-определение | FR-01, FR-02, NFR-02 |
| Warning `PF-OWNER-REPO-SELF` | FR-03, NFR-04 |
| Взаимоисключающие self/unknown/external verdicts | FR-04, FR-09 |
| Self не является валидным `repo-owned` | FR-05, FR-09 |
| Сохранение raw owner и provenance | FR-06, NFR-04 |
| Single-repo parser остаётся grammar-only | FR-07, NFR-05 |
| Контракт, документация и conformance | FR-08, FR-09 |
| Read-only и frozen-input детерминизм | NFR-01, NFR-03 |
| Миграционный governance gate | FR-10 |

## Открытые решения

- **Q-01 (product, blocking):** должна ли warning-диагностика
  `PF-OWNER-REPO-SELF` после наблюдаемого миграционного периода участвовать в
  обязательном governance gate? До решения действует FR-10, а статус документа
  остаётся `draft`.
- **Q-02 (architect, non-blocking):** хранится ли self как явное поле/состояние
  owner-классификации или однозначно выводится из diagnostics? Любой вариант
  обязан удовлетворять FR-05 и NFR-04 без дублирования identity-логики между
  reporters.
