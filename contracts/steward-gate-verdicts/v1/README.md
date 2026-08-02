# Contract `gate-verdicts/v1` — verdicts file of a gate-check run

> Канон живёт здесь (steward — producer и владелец схемы). Потребители (dispatcher
> collector; позже Maestro gates-in-DAG) **вендорят пиненую копию** каталога и держат
> две раздельные гарантии: copy-integrity (offline PR-гейт) и upstream-drift
> (scheduled-наблюдение) — не одно `in_sync`.
> Governance-бандл capability: `workstreams/WS-005-gate-verdicts/spec/` (WS-A).

## Файл

`<bundle-repo>/.steward/gate_verdicts.jsonl` — gitignored локальный артефакт
(ARCH-D1). Перезаписывается **целиком** каждым прогоном `gate-check --emit-verdicts`
(ARCH-D4); атомарная запись через temp+rename. Прогон с конфигурационной ошибкой
(exit 2) файла **не пишет**: без валидного прогона нет и вердиктов, а оставшийся
старый файл читатель сам классифицирует как stale по `source_commit`.

## Записи (JSONL, по одной на строку)

Строка 1 — обязательно `header`; дальше `artifact`- и `finding`-записи.
`kind` обязателен в каждой записи. Нормативная схема — `SCHEMA.json` (draft 2020-12,
`additionalProperties: false`: producer строг, дрейф ловится на фикстурах).

- **header** — provenance прогона: `schema_version` (const `"1"`), `source_commit`
  (40-hex HEAD), `dirty` (грязное дерево ⇒ читатель обязан классифицировать как
  stale-grade — fail-closed покрывает сам прибор), `generated_at`, `profile`, `bundle`.
- **artifact** — инвентарь бандла: `path`, `node_id|null`, `status`
  (зеркало frontmatter), `owner_roles` — **DEC-007 слаги без `@`** (список до
  миграции на singular).
- **finding** — факт нарушения: `gate_id` (`GC-*`), `verdict` (`fail`|`warn` —
  маппинг severity error→fail), `artifact`, `message`. Поля `obligation`, `tier`,
  `phase`, `risk_model_version`, `waiver_ref` объявлены и **зарезервированы**:
  их словари принадлежат gate-id каталогу и риск-интеграции (TODO §3), producer
  их пока не эмитит.

**Steward пишет только факты** — findings и header. Классификацию
(pass / blocked / no-data / unreadable / stale / unresolvable) вычисляет читатель
из файла + git-фактов (ARCH-D2); файл о своей свежести не свидетельствует.

## Фикстуры (`fixtures/`)

Канон и для producer-тестов, и для вендоринга потребителем:

| Фикстура | Класс | Схема-валидна? |
|---|---|---|
| `clean.jsonl` | чистый прогон: header + artifacts, findings нет | ✅ |
| `findings.jsonl` | exit 1: findings по двум артефактам (BEH-07) | ✅ |
| `malformed_line.jsonl` | битая JSON-строка (BEH-03) | ❌ строка 3 не парсится |
| `future_schema.jsonl` | header со `schema_version: "99"` (BEH-04) | ❌ header не проходит схему |
| `dangling_artifact.jsonl` | finding ссылается на артефакт вне инвентаря (BEH-06) | ✅ **намеренно**: дефект семантический, ловит читатель-резолвер, не схема |

## Версионирование

Ломающее изменение = новый каталог `v2/` рядом; `v1/` замораживается. Потребитель
с копией `v1` обязан классифицировать header `schema_version != "1"` как
unreadable/unsupported — никогда как pass.
