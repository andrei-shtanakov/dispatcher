---
spec_stage: discovery
status: approved
version: 1
generated_by: discovery-agent@claude-fable-5
generated_at: 2026-07-14
source_prompt_version: sha256:5bf8fa2588dfeb262aa42350acb5b65075d57ed75395271eba3c7b5d14b2cdbb
validation: pass
approved_by: discovery-agent@claude-fable-5
approved_at: 2026-07-14
owner_role: architect
approver: andrei-shtanakov
schema: discovery-brief
schema_version: 1
feeds: [system-assessment, tech-selection]
interview:
  frame: engineer
  sessions:
    - participant_role: ecosystem-engineer
      date: 2026-07-14
      medium: sync
coverage:
  systems: covered
  interfaces: covered
  constraints: covered
  arch_preferences: covered
  risks: covered
  feasibility_review: covered
  gate_passed: true
open_questions: 1
blocking_open_questions: 0
conflicts: 0
traces_to:
  - discovery-brief-customer.md
---

# Discovery Brief — dispatcher, итерация «sync & roadmap» (engineer-фрейм)

Цели и требования не пересобирались — источник: approved customer-brief
(`discovery-brief-customer.md`, status: approved, PR #14/#15). Здесь — реальность систем
(→ Gate 0b System Assessment) и вход в Tech Selection (0a).

## System Assessment

- **S-01** Git-репо polyrepo-workspace (~15 шт.) — сырьё синк-статуса; читаются штатным
  git. Состояние: стабильно.
- **S-02** `.prograph/` в корне workspace (graph.db SQLite, contracts, projects) — сырьё
  контрактной связности для сводного roadmap. Состояние: **хрупко** — схема `graph.db`
  не заморожена, менялась без нотиса.
- **S-03** `prograph-vault` (KB): `authored/roadmaps/*.yaml` — сырьё roadmap (стабильно);
  `derived/` — целевое место публикации машинных snapshot'ов (пишут только тулзы).
- **S-04** `~/.maestro/maestro.db` — состояние задач Maestro. Состояние: стабильно.
- **S-05** `github-checker` (отдельный репо workspace) — живой (свежие PR #5–6): TUI по
  набору GitHub-реп + headless `snapshot --workspace` → JSON (ветка, ahead/behind, dirty,
  открытые PR/issues/alerts, поле `host`, деградация до git-only с `gh_error`); действия
  `s` (fetch) / `S` (pull --ff-only). Потребитель сегодня — скилл fleet-check.

## Interfaces

- **IF-01** `traces: [S-01]` git CLI по локальным клонам — стабильный, версионируемый контракт.
- **IF-02** `traces: [S-02]` Прямое чтение SQLite `graph.db` — схема не документирована и
  не заморожена; ломается молча.
- **IF-03** `traces: [S-05]` `github-checker snapshot` JSON — **схема заморожена как v1**
  (github-checker PR #7, 2026-07-14): `contracts/snapshot/v1/` + `schema_version: 1` в
  выходе; dispatcher вендорит пиненую копию при постройке FR-01 (см. Q-01, resolved).
- **IF-04** `traces: [S-05]` `gh` CLI (авторизация GitHub) — при отсутствии snapshot
  деградирует до git-only и пишет причину в `gh_error`.
- **IF-05** `traces: [S-03]` Публикация snapshot'ов машин в KB `derived/` — новый
  интерфейс этой итерации: писать может только тулза, по конституции KB.

## Constraints

- **CON-01** Запись в KB — только в `derived/` и только инструментом (конституция
  prograph-vault); `authored/` недоступен.
- **CON-02** Мутации наблюдаемых репо — только белый список {pull, создание PR} по
  явному действию человека (NFR-01 customer-брифа); исполнитель действий —
  github-checker, dispatcher остаётся view.
- **CON-03** Машина может не содержать все репо проекта: отсутствие репо в snapshot'е
  машины ≠ «синхронизирован»; агрегация обязана различать «нет данных» и «ок».
- **CON-04** `gh` может быть не авторизован / сети нет: вердикт «можно работать»
  деградирует честно — «неизвестно» + последний snapshot из KB с явным возрастом данных.

## Architecture Preferences

- **AP-01** `traces: [S-05, CON-02]` Dispatcher **потребляет** `github-checker snapshot`
  как готовый синк-коллектор: github-checker = источник данных + исполнитель белого
  списка действий; dispatcher = view поверх JSON. Не реализовывать синк-коллектор
  заново. (Решение engineer-сессии 2026-07-14.)
- **AP-02** `traces: [S-03, CON-03]` Кросс-машинная видимость (Q-01 customer-брифа,
  вариант «б»): каждая машина публикует свой snapshot в KB (cron/по событию), свежесть
  ≤ 1 час; dispatcher агрегирует по полю `host`.

## Risks

- **RK-01** Схема `graph.db` не заморожена — чтение ломается молча (подтверждён в обоих
  фреймах; главный риск сводного roadmap).
- **RK-02** Snapshot-JSON github-checker не версионировался — строить Must поверх
  незамороженной схемы нельзя. **Закрыт 2026-07-14** пином v1 (Q-01, github-checker PR #7).
- **RK-03** *(tacit, недооценено)* Надёжность cron-публикации с машин: выключенная
  машина или молча умерший cron дают протухший snapshot, который выглядит как «всё ок»,
  если возраст данных не выведен в UI явно (связка с CON-04, Q-02).

## Feasibility Review (по approved customer-brief)

- FR-01 (синк-статус машин с действиями, Must) — **реализуемо**: ahead/behind/dirty
  из snapshot, действия `s`/`S`, PR-статусы через `gh`. Условия: пин snapshot-схемы v1
  (Q-01); деградация без `gh`/сети — «честное неизвестно» + возраст последнего
  KB-snapshot (CON-04). Вердикт: ок.
- FR-02 (авто-обнаружение новых репо, Must) — **реализуемо дёшево**: diff обхода
  `snapshot --workspace` (конфиг не нужен) против списка отслеживаемого + подтверждение
  в UI dispatcher. Вердикт: ок.
- FR-03 (сводный roadmap, Must) — **реализуемо** на существующем сырье
  (`/api/roadmap`, `/api/contracts`, roadmap-YAML); контрактная связность наследует
  хрупкость `graph.db` (RK-01). Вердикт: ок, с учтённым риском.
- NFR-01 (белый список мутаций) — совместимо с AP-01: исполнитель github-checker,
  `S` уже ограничен fast-forward. NFR-02/NFR-03 (perf) — противопоказаний нет.

Blocking-конфликтов с customer-брифом feasibility-проход не породил.

## Open Questions

- **Q-01** `owner_role: architect` · `blocking: false` · `resolved: true` — заморозить и
  версионировать схему `snapshot`-JSON (v1) в репо github-checker до постройки FR-01.
  **Резолюция (2026-07-14):** исполнено github-checker PR #7 — `contracts/snapshot/v1/`
  (schema + golden-фикстуры full/degraded), поле `schema_version: 1` в выходе,
  контракт-тест «breaking → только v2 рядом»; `generated_at` переведён на tz-aware
  RFC3339. Блокер FR-01 снят (закрывает и RK-02).
- **Q-02** `owner_role: architect` · `blocking: false` — механика надёжности
  cron-публикации (RK-03): heartbeat/возраст snapshot'а в UI обязателен; нужен ли alert
  при протухании > 1 ч и где он живёт.

## Stakeholder Conflicts

Конфликтов не выявлено: позиции engineer-сессии не противоречат approved customer-брифу.
