# Доставка снапшотов через ветку `derived-snapshots` (snapshot-publish-branch)

- Дата: 2026-08-28
- Статус: одобрен владельцем (брейншторм в сессии, четыре правки внесены)
- Входящие: inbox #199 (`snapshot-publish-branch`), inbox #213
  (`publish-snapshot-master-drift`), резолюция ecosystem-kb#98 (2026-08-26)
- TODO: `@id:snapshot-publish-branch`; хост-деплой — `@id:publish-snapshot-master-drift`
- Связано: DESIGN-203 (publisher — единственный write-path sync-фичи)

## Контекст и проблема

`dispatcher publish-snapshot` сегодня пишет `derived/snapshots/<host>.json` в
рабочее дерево живого чекаута prograph-vault и коммитит в **ту ветку, что там
checked out**. `master` vault закрыт ruleset-ом (PR-only, у машин нет bypass),
поэтому плановые прогоны молча копили локальные коммиты: на EPGETBIW050F за
26–28.08 — ~89 коммитов, расхождение с origin на 93, сломанный
`git pull --ff-only`; отдельный прецедент — снапшот, уехавший в постороннюю
feature-ветку.

Резолюция владельца vault: `derived/snapshots` — регенерируемая проекция, не
authority; её машинный writer не получает bypass `master`. Снапшоты
публикуются в незащищённую ветку `derived-snapshots` (создана и засеяна,
`b01f390`); локальный `master` vault намеренно больше не несёт актуальный
машинный read-model.

## Цели

1. Publisher пушит только в `derived-snapshots`, меняет только собственный
   `<host>.json`, никогда не коммитит в checked-out ветку основного
   vault-чекаута (ни в какую — рабочее дерево, index и HEAD чекаута не
   трогаются вообще).
2. Читатели снапшотов читают явно из remote-tracking ref
   `origin/derived-snapshots`, не переключая основной чекаут; сеть на рендере
   не появляется (NFR-02).
3. Деградация честная: отсутствие/недоступность ветки → существующие
   `unknown`/`no-data`, панель не ломается и не рисует несуществующих хостов.

## Не-цели

- Перевод хоста EPGETBIW050F (деплой обновлённого dispatcher + launchd-джоба
  на машине) — отдельный пункт `publish-snapshot-master-drift`,
  `@blocked_by` этого.
- Per-host refs / отдельный snapshot-репозиторий — следующий шаг, только если
  коллизии машин на одной ветке станут регулярными (фиксация из #199).
- Изменение схемы снапшота или частоты публикации.

## Дизайн

### Константы

- `SNAPSHOT_BRANCH = "derived-snapshots"`; ref чтения —
  `refs/remotes/origin/derived-snapshots`.
- Явный fetch-refspec (везде, где ветка фетчится):
  `+refs/heads/derived-snapshots:refs/remotes/origin/derived-snapshots`.
  Полагаться на дефолтный refspec нельзя: single-branch clone получает только
  `master`.

### Publisher (`core/publish.py`)

`commit_and_push` заменяется публикацией через эфемерный worktree; полный
цикл одной попытки:

1. `git -C <vault> fetch origin <явный refspec>`; ветки нет на origin →
   `PublishError` сразу (fail loud: ветка засеяна владельцем, её отсутствие —
   признак не той конфигурации, не повод автосоздать).
2. `tempfile.mkdtemp` → `git -C <vault> worktree add --detach <tmp>
   origin/derived-snapshots`.
3. В worktree записывается только `derived/snapshots/<host>.json` —
   существующие `write_snapshot` (атомарная запись) и safe-host-валидация
   без изменений.
4. Файл не изменился → результат `"no changes"`, без коммита и push.
5. `git add -- <только этот путь>` + commit
   (`chore(snapshots): <host> sync snapshot`, как сейчас).
6. `git push --porcelain origin HEAD:refs/heads/derived-snapshots`.

Классификация исхода push — по porcelain-выводу, не по локализованному
stderr:

- строка `!` с причиной non-fast-forward (`non-fast-forward` /
  `fetch first`) → **retryable**: удалить этот worktree целиком и повторить
  весь цикл от свежего fetch (не reset/rebase внутри использованного);
- любой другой отказ (`remote rejected` — hook, protected branch; auth;
  сеть; таймаут) → `PublishError` немедленно, без повторов;
- всего ≤ 3 попыток, между ними sleep с jitter (~0.5–2 с); исчерпание →
  `PublishError` (exit 1 — мёртвый прогон виден, RK-03).

Cleanup в `finally` каждой попытки: `git worktree remove --force <tmp>` +
удаление **только явно созданного** tmp-пути (best-effort `worktree prune`).
Между часовыми запусками не остаётся никакого состояния: ни rebase-state, ни
lock-файлов, ни протухшего worktree.

`--no-push`: локальный пайплайн валидируется без публикации и **без
коммита** — снапшот снят, файл сформирован в worktree, diff проверен;
результат `"validated; push skipped"` (или `"no changes"`). Прежний ответ
`"committed (push skipped)"` уходит: после удаления worktree такой коммит
недостижим, сообщение вводило в заблуждение.

### Reader (`core/sync.py`)

Filesystem-чтение рабочего дерева (`kb_snapshot_dirs` +
`load_kb_snapshots(dirs)`) заменяется чтением ref:

- листинг: `git ls-tree -rz --name-only origin/derived-snapshots --
  derived/snapshots/` — NUL-разделители; принимаются только
  **непосредственные** дети `derived/snapshots/` вида `<host>.json` с
  валидным safe-host (тот же `_SAFE_HOST_RE`); без `-r` git может вернуть
  только запись каталога;
- содержимое: `git cat-file blob
  origin/derived-snapshots:derived/snapshots/<name>` → существующий
  `parse_snapshot` + проверка соответствия `payload.host` ↔ имя файла.

Результат загрузки — типизированный, три раздельных канала:

```python
class KbSnapshotLoad(BaseModel):
    snapshots: list[Snapshot]
    errors: list[tuple[str, str]]      # per-file: (host, причина)
    source_warning: str | None = None  # уровень источника, не хоста
```

Отсутствие vault-репо или самого ref (ещё не отфетчен, ветка удалена) —
`source_warning`, **не** фиктивная запись `(host, error)`: иначе Sync
нарисует несуществующую машину. Warning поднимается в
`SyncReport.warnings`; вердикты штатно уезжают в `no-data`/`unknown`.

Потребители (`collect_sync`, `server/app.py`, `mcp_server.py`) переводятся
на новый вход в этом же PR; `kb_snapshot_dirs` удаляется.

Сеть на рендере не появляется: читается локальный remote-tracking ref.

### Фоновый fetch (`core/sync_service.py`)

`fetch_workspace` дополняется: для KB-репо (prograph-vault) после обычного
`fetch --prune` выполняется fetch с явным refspec ветки снапшотов (см.
Константы). Нельзя предполагать, что дефолтный fetch обновит эту ветку —
single-branch clone её не получит. Это остаётся фоновым fetch; ошибки — в
существующий канал `last_fetch_error`.

## Тесты

Реальные git-фикстуры (bare origin + чекауты), как в `tests/test_publish.py`:

1. Публикация уходит в `derived-snapshots` на origin; checked-out ветка
   основного чекаута не получает коммитов — включая сценарий «чекаут стоит
   на грязной feature-ветке».
2. Пин чтения (закреплён владельцем): основной чекаут на своей ветке с
   любыми локальными изменениями — Sync видит снапшот из
   `origin/derived-snapshots`.
3. Настоящий non-fast-forward: конкурентный коммит в bare-origin **между**
   созданием worktree и push → повтор цикла новым worktree → успех со второй
   попытки; оба снапшот-коммита в истории ветки.
4. Hook rejection (pre-receive declined) **не** ретраится — немедленный
   `PublishError`, одна попытка.
5. Исчерпание 3 попыток на непрекращающемся NFF → `PublishError`.
6. Ветки нет на origin → `PublishError` на шаге fetch.
7. Cleanup: `git worktree list` чист и tmp-путь удалён — и на успехе, и на
   исключении посреди цикла.
8. `--no-push`: результат `"validated; push skipped"`, на origin и в object
   db не появляется новых коммитов ветки.
9. Reader: отсутствующий ref → `source_warning`, пустые snapshots, ноль
   фиктивных хостов; mismatch host↔filename и битый JSON — per-file errors,
   как раньше.
10. Reader-листинг: вложенный мусор под `derived/snapshots/подкаталог/…` и
    не-JSON игнорируются; имена с NUL-безопасным парсингом.
11. Фоновый fetch: vault-клон с ограниченным (single-branch) refspec — после
    `fetch_workspace` ref `origin/derived-snapshots` обновлён.

## Деплой и трассировка

- После мержа и релиза — перевод EPGETBIW050F
  (`publish-snapshot-master-drift`): обновить dispatcher на хосте,
  launchd-джоб менять не нужно (CLI-интерфейс не меняется). Done-признак —
  плановый прогон кладёт снапшот в ветку, локальный master vault-чекаута
  новых снапшот-коммитов не получает.
- Закрытие `snapshot-publish-branch` в `TODO.md` — `[x]` + номер PR; issue
  #199/#213 закрываются по факту работы (принятие уже выведено из слагов).
