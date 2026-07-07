# dispatcher v0.1.0 — Ecosystem Monitoring Dashboard

## Назначение

Read-only дашборд контроля и мониторинга проектов экосистемы (atp-platform,
Maestro, arbiter, spec-runner, proctor; список расширяемый). Читает
on-disk артефакты напрямую — наблюдаемым проектам не нужно быть запущенными
или вообще установленными: отсутствующий проект просто не показывается.

Показывает: результаты тестов/бенчмарков, используемые модели (SSOT-каталог
+ локальные конфиги), текущие задачи, конфигурации (с маскированием
секретов), статус контрактов (drift-check каталога), сбои и ошибки
(OTel JSONL + state-базы), с фильтрами по периоду / проекту / сервису.

## Стек

- **Язык**: Python ≥3.12, uv
- **Ядро**: pydantic v2 модели + Collector-протокол (по коллектору на проект)
- **HTTP**: FastAPI + uvicorn, JSON API + одностраничный vanilla-JS дашборд
  (без сборки), поллинг 10s
- **TUI**: textual (вкладки Projects/Errors/Models/Contracts), читает
  dispatcher.core напрямую через SnapshotService
- **Источники**: SQLite (только `mode=ro` + busy_timeout + 1 retry),
  TOML/YAML конфиги, OTel-JSONL логи (`SeverityNumber ≥ 17`)
- **Тесты**: pytest + anyio + httpx (ASGITransport), фикстуры-миникопии
  реальных деревьев проектов; ruff, pyrefly
- **VSCode**: расширение vscode-ext/ (TypeScript, сайдбар + статус-бар),
  потребляет HTTP API

## Запуск

    uv run dispatcher serve            # http://127.0.0.1:8787
    uv run dispatcher serve --port N --config dispatcher.toml
    uv run dispatcher tui               # терминальный дашборд, те же данные

Конфиг (опционален): `roots` (список корней; без конфига — родительская
директория, monorepo-fallback), `maestro_db` (~/.maestro/maestro.db),
`port`.

## API (публичный контракт, его же будет потреблять VSCode-плагин)

`GET /api/overview` · `/api/projects/{name}` ·
`/api/errors?limit&days&project&service` · `/api/models` · `/api/contracts`

## Жёсткие инварианты

1. **Read-only**: никогда не пишет в наблюдаемые проекты; SQLite строго
   `mode=ro` (`immutable=1` запрещён — источники живые WAL-базы).
2. **Никогда не читает `_cowork_output/`** (dev-only зона); discovery
   пропускает каталоги на `_`/`.`.
3. **Коллекторы не бросают исключений** — деградация до
   `snapshot.warnings` (плюс last-resort guard на сервере).
4. **Version-gate** каждого чтения БД (arbiter schema_version=1,
   atp alembic=f1a2b3c4d5e6, maestro schema_migrations=2, остальные —
   expected-tables) — дрейф схем виден в UI, а не молчалив.
5. **Секреты маскируются в коллекторе** (по имени ключа и по паттерну
   значения, включая тела ошибок) до выхода данных в API.
6. **Drift-check контрактов** — канон vs явный whitelist вендоренных копий
   (никогда не по имени файла).

## Роль в экосистеме

Потребитель (только чтение) артефактов всех core-проектов; ни один проект
от dispatcher не зависит. Связанность с приватными схемами БД осознанная и
version-gated; долгосрочный план — стабильный read-model/`status.json` у
каждого владельца как пиннованный контракт (ADR-ECO-003-практика).

## Roadmap

- **Stage 1 (done, 2026-07-03)**: HTML-дашборд + JSON API + CLI.
- **Stage 2 (done, 2026-07-05)**: TUI (textual) поверх dispatcher.core
  (SnapshotService).
- **Stage 3 (done, 2026-07-05)**: VSCode-плагин (vscode-ext/) поверх HTTP API.
- Возможное редактирование (пока строго view-only).

## Документы

- Спека: `docs/superpowers/specs/2026-07-03-dispatcher-design.md`
- План Stage 1: `docs/superpowers/plans/2026-07-03-dispatcher-stage1.md`
- Спека Stage 2 (TUI): `docs/superpowers/specs/2026-07-05-dispatcher-tui-design.md`
- План Stage 2: `docs/superpowers/plans/2026-07-05-dispatcher-tui-stage2.md`
- Спека Stage 3 (VSCode): `docs/superpowers/specs/2026-07-05-dispatcher-vscode-design.md`
- План Stage 3: `docs/superpowers/plans/2026-07-05-dispatcher-vscode-stage3.md`
- Remote: `github.com/andrei-shtanakov/dispatcher` (ветка `master`)
