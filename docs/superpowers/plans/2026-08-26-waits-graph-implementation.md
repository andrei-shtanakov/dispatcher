# Waits graph — план имплементации

Спека: `docs/superpowers/specs/2026-08-26-waits-graph-design.md`.
Все задачи — одна ветка, один implementation-PR (отдельный от PR пары).
Старт имплементации — после решения по launchpad B2 либо после проверки
непересечения файлов (решение владельца при принятии пары). Каждая задача
завершается зелёным `uv run pytest -q` + `ruff format --check` +
`ruff check` + `pyrefly check`; UI-задача — последней.

## Задача 1 — `dispatcher/core/waits.py`: сбор и раскладка (§3.1–§3.2)

- pydantic-модели: `WaitsView`, `WaitEdge`, `NodeRef`, `LooseRef`,
  `Finding`, `TriggerItem`, `AbsentRepo{repo, reason}`,
  `WaitsPlane{state, detail?, repos_read}` — формы §3.2 дословно.
- `build_waits(config, *, now) -> WaitsView`:
  - манифест: `_manifest_path`/`_workspace_root` (реиспользовать из
    `core/epics.py` импортом, не копией);
  - страж вокруг `manifest_index`+`checkout_map`: любой сбой →
    `unavailable`, detail = класс ошибки, пустые списки;
  - inputs по ВСЕМ репо манифеста: прочитан / `available=True, None`
    (нет TODO.md или `unreadable: <класс>` per-file try/except) /
    `available=False`; причины → `absent_repos`;
  - `parse_fleet` + `check_fleet`; страж вокруг обоих → `unavailable`;
  - раскладка: edges (+stale по PF-BLOCKER-STALE, матч
    subject_uri/related_uri), loose_refs (references с
    `resolved_target is None`), findings (PF-BLOCKER-*, PF-ID-DANGLING,
    PF-LEGACY-* минус PF-BLOCKER-STALE), triggers (open, не tombstone,
    trigger непуст);
  - NodeRef строится из узлов снапшота (по node_id); статус —
    `declared_status`;
  - сортировки §3.2; `generated_at` = `now` (ISO), параметр — для тестов.
- `state`, в порядке проверки: `repos_read == 0` → `unavailable`
  («ни одного TODO.md» — спека §3.1, прецедент `_todo_plane` epics.py:216);
  absent_repos непуст → `partial`; иначе `read`. Ветка unavailable
  срабатывает и когда все чекауты на месте, но TODO.md нет ни в одном.

Тесты задачи 1 — весь список §5 спеки, кроме двух последних UI/route-строк:
фикстурный воркспейс (tmp-манифест + TODO-файлы, паттерн
`tests/test_epics_view.py`), без моков пакета. Явно: канонич. ссылка →
edge waiting; закрытая цель → stale, ребро не исчезает, дубля в findings
нет; legacy unique → loose_refs без findings; legacy ambiguous/missing →
loose_refs + finding; dangling todo:// → loose_refs + PF-ID-DANGLING;
репо без чекаута → UNRESOLVABLE + absent + partial; чекаут без TODO →
NO-TODO; нечитаемый TODO (битые байты) → absent `unreadable:*` + partial;
нет манифеста / манифест не парсится → unavailable ×2; все чекауты
есть, но ни одного TODO.md → unavailable, НЕ partial (регрессионный к
границе состояний); триггеры
open-only; два @blocked_by → два ребра; детерминизм без generated_at.

## Задача 2 — роут `GET /api/waits` (§3.1)

- `dispatcher/server/app.py`: `@app.get("/api/waits",
  response_model=WaitsView)` рядом с `/api/epics`; тело — один вызов
  `build_waits(config, now=...)`. Всегда HTTP 200.
- Тест: TestClient по фикстурному воркспейсу — 200 и форма ответа на
  обоих путях (read и unavailable).

## Задача 3 — секция «Waits» в статике (§3.3)

- `dispatcher/server/static/index.html`: секция после Epics — SVG
  двухколоночной раскладки (источники слева, цели справа, группировка по
  репо, кубические рёбра; stale — акцент + бейдж «доставлено»; счётчик
  входящих у концентраторов; легенда), таблица loose_refs, таблица
  findings (код + message дословно), сворачиваемый список triggers по
  репо, строка-дисклеймер при partial/unavailable, «рёбер нет» при
  пустом графе.
- fetch: отдельный `refreshWaits()` с собственным catch вне основного
  `Promise.all` — по образцу `refreshEpics` (`index.html:711`); отказ
  красит только свою секцию.
- Никаких библиотек; вся геометрия — генерация SVG-элементов из JSON.

Проверка задачи — глазами на живом воркспейсе (§7): бейдж и легенда
читаются, loose_refs отделены, пустой граф не прячет секцию. Если в репо
есть паттерн smoke-теста статики — добавить наличие секции; нового
UI-тест-харнесса не заводить.

## Задача 4 — черта

- Полный набор: `uv run pytest -q`, `ruff format --check .`,
  `ruff check`, `pyrefly check`.
- Экономичный цикл гейта: `sh scripts/review/local.sh` до чистого →
  драфт → номер PR в TODO-пункт `@id:waits-graph-view` тем же PR →
  снятие драфта = один платный прогон.
- Приёмка §7 на живом воркспейсе: 13 канонических из замера 26.08 в
  edges (минус погашенные), 6 переходных в loose_refs, stale показан,
  если есть.

## Трассируемость

| Секция спеки | Задача |
|---|---|
| §3.1 источник, inputs всех репо, стражи | 1 |
| §3.2 форма ответа, раскладка, сортировки | 1 |
| §3.1 роут, HTTP 200 всегда | 2 |
| §3.3 рендер, fetch-изоляция | 3 |
| §4 ошибки | 1 (стражи) + 2 (тест unavailable) |
| §5 тесты | 1, 2 |
| §6 пороги | вне кода: пункты-триггеры при наступлении |
| §7 приёмка | 4 |

Вне плана (не-цели спеки): published-snapshot источник, граф триггеров,
библиотека рендера, мутации, починка чтения файлов в `_todo_plane`.
