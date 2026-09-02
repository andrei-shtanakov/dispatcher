---
traces_to:
- behaviour-spec
upstream_hashes:
  behaviour-spec: 5774992d22534cb6f8cd6c71b6f792542390bbe7
spec_stage: tasks
status: approved
version: 3
generated_by: fleet-agent
generated_at: '2026-09-02T07:48:56'
source_prompt_version: ''
validation: warn
approved_by: andrei-shtanakov
approved_at: '2026-09-02T06:07:39Z'
---

## Milestone 1: PF-OWNER-REPO-SELF: диагностика владельца repo:<свой> в plan_fields (dispatcher#229)

Сгенерировано task_bridge из behaviour-spec бандла WS-dispatcher-229 (шаг 3 плана развития конвейера; группировка задач — по Feature-секциям). Человеческий approve дан владельцем 2026-09-02 (`spec approve tasks`, лейн devtools#110); charter, requirements и behaviour-spec бандла — approved (ревью-контур пройден, Q-01 решён decision-record-ом в charter: миграционный период). Исполнение — режим FR-10: severity `warning`, без нового обязательного gate; включение в gate — отдельное будущее решение.

### TASK-001: Канонический ключ собственного репозитория определяется как self-owner
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-01.
Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-01

**Checklist:**
- [x] реализовать BEH-01: Канонический ключ собственного репозитория определяется как self-owner
- [x] проверка группы: tests/plan_fields/fleet/test_owner_repo_self.py (kind: integration) зелёные на BEH-01

**Traces to:** [FR-01, FR-03]

### TASK-002: git_dir-написание собственного репозитория нормализуется в self-owner
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-03.
Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-03
**Depends on:** [TASK-001]

**Checklist:**
- [x] реализовать BEH-03: git_dir-написание собственного репозитория нормализуется в self-owner
- [x] проверка группы: tests/plan_fields/fleet/test_owner_repo_self_git_dir.py (kind: integration) зелёные на BEH-03

**Traces to:** [FR-02, FR-03, FR-09]

### TASK-003: Похожее, но необъявленное имя не считается self-owner
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-04.
Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-04
**Depends on:** [TASK-002]

**Checklist:**
- [x] реализовать BEH-04: Похожее, но необъявленное имя не считается self-owner
- [x] проверка группы: tests/plan_fields/fleet/test_owner_repo_verdicts.py (kind: integration) зелёные на BEH-04

**Traces to:** [FR-02, FR-04]

### TASK-004: Известный внешний репозиторий остаётся валидным repo-owner
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-05.
Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-05
**Depends on:** [TASK-003]

**Checklist:**
- [x] реализовать BEH-05: Известный внешний репозиторий остаётся валидным repo-owner
- [x] проверка группы: tests/plan_fields/fleet/test_owner_repo_verdicts.py (kind: integration) зелёные на BEH-05

**Traces to:** [FR-04, FR-05, FR-09]

### TASK-005: Repository verdicts взаимоисключающие
P2 | ✅ DONE   Est: 0.5d

Реализовать сценарии BEH-06.
Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-06
**Depends on:** [TASK-004]

**Checklist:**
- [x] реализовать BEH-06: Repository verdicts взаимоисключающие
- [x] проверка группы: tests/plan_fields/contracts/test_repository_owner_verdict.py (kind: contract) зелёные на BEH-06

**Traces to:** [FR-04, FR-09]

### TASK-006: Owner другого типа не участвует в repository self-классификации
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-07.
Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-07
**Depends on:** [TASK-005]

**Checklist:**
- [ ] реализовать BEH-07: Owner другого типа не участвует в repository self-классификации
- [ ] проверка группы: tests/plan_fields/fleet/test_owner_repo_type_boundaries.py (kind: integration) зелёные на BEH-07

**Traces to:** [FR-01, FR-04]

### TASK-007: Self-owner исключается из валидного external repo-owned состояния
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-08.
Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-08
**Depends on:** [TASK-006]

**Checklist:**
- [ ] реализовать BEH-08: Self-owner исключается из валидного external repo-owned состояния
- [ ] проверка группы: tests/plan_fields/reporters/test_self_owner_views.py (kind: e2e) зелёные на BEH-08

**Traces to:** [FR-05, FR-09]

### TASK-008: Исходный owner и provenance сохраняются без изменений
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-09.
Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-09
**Depends on:** [TASK-007]

**Checklist:**
- [ ] реализовать BEH-09: Исходный owner и provenance сохраняются без изменений
- [ ] проверка группы: tests/plan_fields/fleet/test_owner_repo_self_explainability.py (kind: integration) зелёные на BEH-09

**Traces to:** [FR-06]

### TASK-009: Single-repo parser остаётся grammar-only
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-10.
Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-10
**Depends on:** [TASK-008]

**Checklist:**
- [ ] реализовать BEH-10: Single-repo parser остаётся grammar-only
- [ ] проверка группы: tests/plan_fields/parser/test_repository_owner_regression.py (kind: contract) зелёные на BEH-10

**Traces to:** [FR-07, FR-09]

### TASK-010: Публичный контракт различает три repository owner состояния
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-11.
Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-11
**Depends on:** [TASK-009]

**Checklist:**
- [ ] реализовать BEH-11: Публичный контракт различает три repository owner состояния
- [ ] проверка группы: tests/plan_fields/contracts/test_pf_owner_repo_self_schema.py (kind: contract) зелёные на BEH-11

**Traces to:** [FR-03, FR-05, FR-08]

### TASK-011: Документация объясняет диагностику и исправление
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-12.
Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-12
**Depends on:** [TASK-010]

**Checklist:**
- [ ] реализовать BEH-12: Документация объясняет диагностику и исправление
- [ ] проверка группы: docs/plan_fields/owner-repository.md (kind: manual) зелёные на BEH-12

**Traces to:** [FR-08]

### TASK-012: Self-owner warning наблюдаема без неявного governance gate
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-13.
Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-13
**Depends on:** [TASK-011]

**Checklist:**
- [ ] реализовать BEH-13: Self-owner warning наблюдаема без неявного governance gate
- [ ] проверка группы: tests/plan_fields/governance/test_owner_repo_self_gate.py (kind: e2e) зелёные на BEH-13

**Traces to:** [FR-03, FR-10]

### TASK-013: Одинаковые frozen inputs дают одинаковый результат
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-14.
Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-14
**Depends on:** [TASK-012]

**Checklist:**
- [ ] реализовать BEH-14: Одинаковые frozen inputs дают одинаковый результат
- [ ] проверка группы: tests/plan_fields/fleet/test_owner_repo_self_determinism.py (kind: integration) зелёные на BEH-14

**Traces to:** [FR-01, FR-03, FR-04, FR-05]

