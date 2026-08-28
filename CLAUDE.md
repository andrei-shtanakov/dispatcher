# CLAUDE.md

## Repo scope & boundaries

- **Этот репо:** `dispatcher` — git-корень `all_ai_orchestrators/dispatcher/`, remote `git@github.com:andrei-shtanakov/dispatcher.git`.
- **Соседи (READ-ONLY reference):** все остальные подпроекты воркспейса — их код не
  редактировать. Состав флота — `ai-orchestrators-workspace/workspace-manifest.toml`
  (SSOT); рукописные списки соседей в CLAUDE.md не ведём — они дрейфуют.
- **Канон имени репо = имя каталога после обычного `git clone`** (`maestro`, `libretto`).
- Нужна правка у соседа → **стоп**: запиши handoff в `../prograph-vault/authored/notes/`
  (кросс-проектное) или `../_cowork_output/` (черновик), не трогай его файлы.
- Кросс-репные контракты — **вендорить пиненой копией внутрь**, не ссылаться наружу.
- Это правило о **разработке** (сессии Claude Code не редактируют файлы соседей
  напрямую). Отдельно от него у dispatcher есть свой узкий whitelist рантайм-мутаций
  (`core/actions.py`, `core/spec_runner_config_actions.py`): запущенное приложение может
  открывать PR в наблюдаемые репо только по явному клику человека, никогда — от имени
  coding-сессии. См. X-02, `docs/superpowers/specs/2026-07-17-spec-runner-config-editor-design.md`.
- Полное правило (SSOT): `../prograph-vault/authored/rules/repo-boundaries.md`.

## Планы: где что лежит

- **`TODO.md` в корне** — план уровня команды и кросс-проектные точки. Это единственный
  машинно-читаемый план-файл: дайджест Robin строит прогноз работ по чекбоксам
  `- [ ]` / `- [x]` в корне зеркал и в `spec/`, `docs/` намеренно не заходит. Репо без
  `TODO.md` выглядит в обзоре экосистемы как репо без открытой работы.
- **`spec/tasks.md`** (+ `requirements.md`, `design.md`) — канонический бэклог реализации,
  TASK-NNN с трассировкой на REQ/DESIGN. **`docs/superpowers/{specs,plans}/`** — спеки и
  планы отдельных фич. Микрошаги живут здесь, а не в `TODO.md`.
- Поля пунктов `TODO.md` — инлайн-теги `@owner:<principal>` /
  `@blocked_by:<reference>` / `@trigger:"…"`. Канонические владельцы:
  `github:<login>`, `github-team:<org>/<team>`, `repo:<manifest-key>` или `TBD`.
  Отсутствующий `@owner` (`missing`) отличается от явно отложенного
  `@owner:TBD`. Канонический блокер — `todo://<repo>/<id>`; legacy
  `<repo>#<slug>` поддерживается только на переходный период. Все теги должны
  находиться на строке чекбокса: построчный парсер не читает продолжения.
- Закрытый пункт — `[x]` + номер PR; неактуальный — `~~зачеркнуть~~` с причиной.
  **Строку не удалять**: дельта-счётчики читают исчезновение как «закрыто».
- Правя план-доки, держи их сверенными с кодом: устаревшая сводка (например, «сделано
  1/2», когда обе задачи закрыты) хуже отсутствующей — по ней принимают решения.

## Git workflow (у репо есть remote)

- Ветка `<type>/<slug>` → push → `gh pr create`. **Прямые коммиты в `master`
  запрещены**, как и локальный мерж ветки в `master` в обход PR.
- **Ревью PR — терминальный прогон от ai-prosto** (дефолт с 2026-08-28):
  `sh ../devtools/review-pr.sh <repo> <pr> --dry-run`, затем без `--dry-run` — вердикт публикуется
  PR-ревью. Находки отрабатывать как обычно: валидное — фикс-коммитом,
  невалидное — ответить с обоснованием, не применять вслепую. CI-гейт
  codex-review (где есть) — advisory-фолбэк по лейблу `codex-review`, его
  красноту/зависание не перегонять. **Copilot по умолчанию не запрашивать** —
  только по явной просьбе владельца. SSOT: `../prograph-vault/authored/rules/git-workflow.md`.
- **Не мержить.** Мерж делает пользователь.
- После мержа пользователем: `git switch master && git pull --ff-only`, затем удалить
  влитую ветку в **обеих половинах**: локально `git branch -d <ветка>` (после squash-мержа
  `-d` откажется — сверить, что `git diff master <ветка>` пуст, и удалить
  `git branch -D <ветка>`) и на origin
  `git push origin --delete <ветка>`, если GitHub не удалил сам; затем `git fetch --prune`.
- Никогда не делать force-push в общие ветки; не трогать другие репо (см. scope выше).
- Полное правило (SSOT): `../prograph-vault/authored/rules/git-workflow.md`.

## Входящие запросы (inbox)

В начале работы проверь входящие: `gh issue list --label inbox --state open`.
Issue с лейблом `inbox` — запрос от соседнего репо, ещё **не** пункт плана.
Принять = завести пункт в `TODO.md` с указанным `slug:`; принял под другим
именем — поправь `slug:` в теле issue.
Отказать = `gh issue close --reason "not planned"`.
Нужна работа в соседнем репо — не редактируй его: заведи там issue
(`slug:` + `from:` + проза). Правило: ADR-ECO-006 — канон в `ecosystem-kb`
(каталог `prograph-vault/` в корне воркспейса),
`authored/decisions/2026-07-28-adr-eco-006-cross-repo-issue-inbox.md`.

Исходящее ожидание — вторая половина того же ритуала: «ждём соседа» существует
**только** как чекбокс `TODO.md` с `@blocked_by:todo://<repo>/<id>` (переходно —
`<repo>#<номер>`); память сессий, заметки и handoff-доки — лишь зеркало. Находка
PF-BLOCKER-STALE по этому репо = «ожидание доставлено — действуй или переставь тег».
Правило (SSOT): `../prograph-vault/authored/rules/cross-repo-waits.md`.

## `../_cowork_output/` — dev-only

Координационный dev-scratch воркспейса; у пользователей и клонов проекта его НЕТ.
Shipped/runtime-код никогда не читает и не резолвит пути под ним; кросс-репные
контракты вендорятся пиненой копией внутрь, не ссылкой наружу. Ссылаться на него
могут только dev-тулинг самого воркспейса и документация. Канонические факты живут
в репо-владельце (пример: SSOT agents-catalog — `atp-platform/method/agents-catalog.toml`,
ADR-ECO-003). Полное правило (SSOT): `../prograph-vault/authored/rules/cowork-output.md`.
