---
spec_stage: behaviour-spec
status: draft
owner_role: product
traces_to: [requirements]
upstream_hashes:
  requirements: "fdf89dc702a4d51b5e06a3100894850b6cbfdd8d"
---

# Behaviour spec — PF-OWNER-REPO-SELF: диагностика владельца `repo:<свой>` в `plan_fields`

## Область поведения

Спецификация описывает наблюдаемое поведение fleet-анализа `plan_fields` при
сопоставлении typed repository owner с repository provenance того же plan node.
Разрешение identity выполняется по frozen workspace manifest и не изменяет
grammar-only поведение single-repo parser.

## Сценарии

#### BEH-01: Канонический ключ собственного репозитория определяется как self-owner
`traces: [FR-01, FR-03]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/plan_fields/fleet/test_owner_repo_self.py`
- **Given** frozen manifest содержит canonical key `dispatcher`, а plan node с
  provenance `dispatcher/TODO.md` имеет `owner_ref.type = repository` и
  `owner_ref.raw = repo:dispatcher`.
- **When** fleet-анализ классифицирует owner этого node.
- **Then** owner verdict равен self-owner независимо от состояния checkbox.
- **And** node получает ровно одну диагностику `PF-OWNER-REPO-SELF` с severity
  `warning`.
- **And** диагностика привязана к URI node, содержит repository/file/location
  provenance, объясняет отсутствие передачи ответственности внешнему principal
  и предлагает typed principal либо `TBD`.

#### BEH-03: git_dir-написание собственного репозитория нормализуется в self-owner
`traces: [FR-02, FR-03, FR-09]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/plan_fields/fleet/test_owner_repo_self_git_dir.py`
- **Given** frozen manifest связывает допустимое `git_dir`-написание с source
  repository, а repository owner использует это написание.
- **When** fleet-анализ разрешает owner identity.
- **Then** verdict равен self-owner и совпадает с verdict для canonical key.
- **And** node получает ровно одну `PF-OWNER-REPO-SELF` warning с URI и
  provenance.

#### BEH-04: Похожее, но необъявленное имя не считается self-owner
`traces: [FR-02, FR-04]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/plan_fields/fleet/test_owner_repo_verdicts.py`
- **Given** repository owner строково похож на canonical key или
  `git_dir` source repository, но не объявлен frozen manifest.
- **When** fleet-анализ разрешает repository owner.
- **Then** owner получает unknown repo-owner verdict и существующую диагностику
  `PF-OWNER-REPO-UNKNOWN`.
- **And** `PF-OWNER-REPO-SELF` не выдаётся.

#### BEH-05: Известный внешний репозиторий остаётся валидным repo-owner
`traces: [FR-04, FR-05, FR-09]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/plan_fields/fleet/test_owner_repo_verdicts.py`
- **Given** owner repository разрешается через frozen manifest, а его canonical
  identity отличается от canonical identity source repository.
- **When** fleet-анализ классифицирует owner.
- **Then** verdict равен external repo-owner.
- **And** node не получает ни `PF-OWNER-REPO-SELF`, ни
  `PF-OWNER-REPO-UNKNOWN` и учитывается как валидный external `repo-owned`.

#### BEH-06: Repository verdicts взаимоисключающие
`traces: [FR-04, FR-09]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/plan_fields/contracts/test_repository_owner_verdict.py`
- **Given** корректный `owner_ref` типа `repository` и frozen manifest.
- **When** fleet-анализ завершает identity-классификацию.
- **Then** машинный результат содержит ровно один verdict: self-owner,
  external repo-owner или unknown repo-owner.
- **And** одна owner-ссылка не может одновременно иметь диагностики
  `PF-OWNER-REPO-SELF` и `PF-OWNER-REPO-UNKNOWN`.
- **And** синтаксически некорректный owner остаётся grammar verdict и не
  переопределяется repository identity-диагностикой.

#### BEH-07: Owner другого типа не участвует в repository self-классификации
`traces: [FR-01, FR-04]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/plan_fields/fleet/test_owner_repo_type_boundaries.py`
- **Given** owner является typed person, typed team, `TBD` или иным
  нерепозиторным вариантом.
- **When** fleet-анализ обрабатывает node из любого source repository.
- **Then** repository self/unknown/external verdict для owner не формируется.
- **And** node не получает `PF-OWNER-REPO-SELF` из-за совпадения любого сырого
  фрагмента с repository identity.

#### BEH-08: Self-owner исключается из валидного external repo-owned состояния
`traces: [FR-05, FR-09]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/plan_fields/reporters/test_self_owner_views.py`
- **Given** fleet snapshot содержит canonical-key или `git_dir`
  self-owner node, а также валидный external repo-owner node.
- **When** read-model строит агрегаты и все reporter-facing owner views.
- **Then** self-owner доступен как отдельное машинно различимое состояние и не
  входит в класс валидного external `repo-owned`.
- **And** external node продолжает входить в этот класс.
- **And** потребителям не требуется повторно нормализовать исходную строку owner.

#### BEH-09: Исходный owner и provenance сохраняются без изменений
`traces: [FR-06]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/plan_fields/fleet/test_owner_repo_self_explainability.py`
- **Given** self-owner записан canonical key или `git_dir`-формой.
- **When** fleet-анализ формирует verdict и диагностику.
- **Then** результат сохраняет точное исходное значение `owner_ref.raw`, node
  URI и repository/file/location provenance.
- **And** пользователь может сопоставить finding с конкретным исходным пунктом.
- **And** анализ не переписывает `TODO.md`, manifest, contract или другие
  входные артефакты.

#### BEH-10: Single-repo parser остаётся grammar-only
`traces: [FR-07, FR-09]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/plan_fields/parser/test_repository_owner_regression.py`
- **Given** single-repo parser получает синтаксически корректный
  `@owner:repo:<key>` без fleet provenance и frozen manifest.
- **When** parser проверяет owner tag.
- **Then** он возвращает допустимый typed repository owner по существующему
  контракту.
- **And** parser не выдаёт identity-verdict или диагностику
  `PF-OWNER-REPO-SELF`.
- **And** существующие валидные fixtures и parser contracts не меняют результат.

#### BEH-11: Публичный контракт различает три repository owner состояния
`traces: [FR-03, FR-05, FR-08]`

- **checked_by**: `status: planned` `kind: contract` `owner: qa` `target: tests/plan_fields/contracts/test_pf_owner_repo_self_schema.py`
- **Given** обновлённый канонический контракт `plan_fields`.
- **When** выполняется применимая schema validation и читается структурированный
  результат fleet-анализа.
- **Then** контракт описывает self-owner отдельно от unknown и external
  repo-owner.
- **And** `PF-OWNER-REPO-SELF`, severity `warning`, node URI и provenance
  представлены стабильными машинно читаемыми полями.
- **And** все fleet reporters используют эту общую классификационную семантику.

#### BEH-12: Документация объясняет диагностику и исправление
`traces: [FR-08]`

- **checked_by**: `status: planned` `kind: manual` `owner: qa` `target: docs/plan_fields/owner-repository.md`
- **Given** пользовательская документация `plan_fields`.
- **When** пользователь ищет описание `PF-OWNER-REPO-SELF`.
- **Then** он видит severity, fleet-only область применения и отличие self от
  unknown и валидного external repo-owner.
- **And** примеры покрывают canonical key и `git_dir` self-owner.
- **And** исправление рекомендует реального typed principal либо явный `TBD` и
  не обещает автоматического назначения.

#### BEH-13: Self-owner warning наблюдаема без неявного governance gate
`traces: [FR-03, FR-10]`

- **checked_by**: `status: planned` `kind: e2e` `owner: qa` `target: tests/plan_fields/governance/test_owner_repo_self_gate.py`
- **Given** отдельное продуктовое решение о включении
  `PF-OWNER-REPO-SELF` в обязательный gate отсутствует.
- **When** fleet pipeline анализирует self-owner node.
- **Then** finding с базовой severity `warning` присутствует в fleet output и
  reporters.
- **And** finding само по себе не включает новый обязательный governance gate.
- **And** будущее включение gate требует отдельного явного решения и оценки
  существующих self-owner записей.

#### BEH-14: Одинаковые frozen inputs дают одинаковый результат
`traces: [FR-01, FR-03, FR-04, FR-05]`

- **checked_by**: `status: planned` `kind: integration` `owner: qa` `target: tests/plan_fields/fleet/test_owner_repo_self_determinism.py`
- **Given** неизменные plan inputs, provenance и frozen workspace manifest.
- **When** fleet-анализ выполняется повторно.
- **Then** owner verdict, набор и количество diagnostics, URI/provenance и
  reporter-facing классификация совпадают между запусками.
- **And** нормализация использует общую manifest identity-модель fleet-слоя.

## Матрица трассируемости

| Behaviour | Functional requirements |
|---|---|
| BEH-01 | FR-01, FR-03 |
| BEH-03 | FR-02, FR-03, FR-09 |
| BEH-04 | FR-02, FR-04 |
| BEH-05 | FR-04, FR-05, FR-09 |
| BEH-06 | FR-04, FR-09 |
| BEH-07 | FR-01, FR-04 |
| BEH-08 | FR-05, FR-09 |
| BEH-09 | FR-06 |
| BEH-10 | FR-07, FR-09 |
| BEH-11 | FR-03, FR-05, FR-08 |
| BEH-12 | FR-08 |
| BEH-13 | FR-03, FR-10 |
| BEH-14 | FR-01, FR-03, FR-04, FR-05 |

Все FR-01–FR-10 покрыты хотя бы одним сценарием. Нефункциональные требования
о детерминизме, единой identity-модели, read-only безопасности и стабильности
машинного контракта закреплены наблюдаемыми результатами BEH-06, BEH-08,
BEH-09, BEH-11 и BEH-14 без введения дополнительных идентификаторов трассировки.
