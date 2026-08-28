# Snapshot Publish Branch — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `dispatcher publish-snapshot` доставляет `derived/snapshots/<host>.json` в ветку `derived-snapshots` через эфемерный worktree, а все читатели снапшотов читают remote-tracking ref `origin/derived-snapshots`, не трогая основной vault-чекаут.

**Architecture:** Publisher — полный цикл на попытку (fetch по явному refspec → worktree add в несуществующий подпуть mkdtemp → адресный add/commit → `push --porcelain` с классификацией отказа), retry только на non-fast-forward, ≤ 3 попыток, строго адресный cleanup. Reader — `ls-tree -rz` + `cat-file blob` по ref, типизированный `KbSnapshotLoad` (snapshots / per-file errors / source_warning), warning доезжает до Sync и до Epics-плоскостей web+MCP. Фоновый fetch vault получает явный refspec ветки.

**Tech Stack:** Python 3.12, pydantic, subprocess+git, pytest (реальные git-фикстуры), uv, ruff, pyrefly.

**Spec:** `docs/superpowers/specs/2026-08-28-snapshot-publish-branch-design.md`

## Global Constraints

- Ветка: `derived-snapshots`; refspec fetch: `+refs/heads/derived-snapshots:refs/remotes/origin/derived-snapshots`; ref чтения: `origin/derived-snapshots`.
- Основной vault-чекаут не трогается вообще: HEAD, ветка, рабочее дерево, index.
- Retry ТОЛЬКО на non-fast-forward из porcelain-вывода push; hook/auth/сеть — немедленный `PublishError`. ≤ 3 попыток, jitter-sleep 0.5–2 с (инжектируемый `sleeper`).
- Cleanup строго адресный: `git worktree remove --force <worktree_path>` + удаление только собственного `tmp_root`; НИКАКОГО `git worktree prune`.
- Отсутствие vault/ref — `source_warning`, не фиктивный хост в `(host, error)`.
- Сеть на рендере не появляется (NFR-02): читатели читают локальный ref.
- CLAUDE.md: только uv; type hints везде; `uv run pytest`, `uv run ruff format . && uv run ruff check .`, `pyrefly check` после каждого изменения; ≤ 88 колонок; docstrings на публичные API.
- Коммиты — на ветке `feat/snapshot-publish-branch` (уже создана, спека в ней).

---

### Task 1: Reader — `KbSnapshotLoad` и чтение `origin/derived-snapshots` (`core/sync.py` + call-sites)

**Files:**
- Modify: `dispatcher/core/sync.py` (заменить `kb_snapshot_dirs`/`load_kb_snapshots`, `build_report`, `collect_sync`; добавить `SAFE_HOST_RE`, `SNAPSHOT_BRANCH`)
- Modify: `dispatcher/core/publish.py` (импортировать `SAFE_HOST_RE` из sync вместо локального `_SAFE_HOST_RE`)
- Modify: `dispatcher/server/app.py:432-441,464-470` (`_epic_snapshots` → `KbSnapshotLoad`)
- Modify: `dispatcher/mcp_server.py:157-184` (тулзы `epics`/`epic`)
- Test: `tests/test_kb_snapshot_load.py` (новый)

**Interfaces:**
- Produces: `SNAPSHOT_BRANCH = "derived-snapshots"`, `SNAPSHOT_REF = "origin/derived-snapshots"`, `SAFE_HOST_RE` (в `core/sync.py`); `class KbSnapshotLoad(BaseModel)` с полями `snapshots: list[Snapshot]`, `errors: list[tuple[str, str]]`, `source_warning: str | None`; `load_kb_snapshots(roots: tuple[Path, ...]) -> KbSnapshotLoad`; `build_report(..., kb_source_warning: str | None = None)`.
- Consumes: существующие `parse_snapshot`, `KB_REPO`, `SyncReport.warnings`.

- [ ] **Step 1: Написать падающие тесты**

Создать `tests/test_kb_snapshot_load.py`:

```python
"""Reader снапшотов: явное чтение origin/derived-snapshots, не рабочего дерева.

Спека 2026-08-28-snapshot-publish-branch: основной vault-чекаут может стоять
на любой ветке с любыми локальными изменениями — Sync обязан видеть снапшоты
из remote-tracking ref; отсутствие ref — source_warning, не фиктивный хост.
"""

import subprocess
from pathlib import Path

from dispatcher.core.sync import (
    SNAPSHOT_BRANCH,
    KbSnapshotLoad,
    build_report,
    load_kb_snapshots,
)
from tests.test_publish import make_snapshot, make_vault, _git


def _seed_branch(root: Path, vault: Path, files: dict[str, str]) -> Path:
    """bare-origin + ветка derived-snapshots с *files*; vault её отфетчил."""
    origin = root / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)], check=True, text=True
    )
    _git(vault, "remote", "add", "origin", str(origin))
    _git(vault, "push", "-q", "origin", "master")
    writer = root / "writer"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(writer)], check=True, text=True
    )
    _git(writer, "config", "user.email", "t@example.com")
    _git(writer, "config", "user.name", "t")
    _git(writer, "switch", "-q", "-c", SNAPSHOT_BRANCH)
    for rel, text in files.items():
        target = writer / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        _git(writer, "add", "--", rel)
    _git(writer, "commit", "-q", "-m", "seed snapshots")
    _git(writer, "push", "-q", "origin", SNAPSHOT_BRANCH)
    _git(
        vault,
        "fetch",
        "-q",
        "origin",
        f"+refs/heads/{SNAPSHOT_BRANCH}:refs/remotes/origin/{SNAPSHOT_BRANCH}",
    )
    return origin


def _snapshot_json(host: str) -> str:
    return make_snapshot(host).model_dump_json(indent=2) + "\n"


def test_reads_ref_while_checkout_dirty_on_feature_branch(tmp_path: Path) -> None:
    """Пин владельца: чекаут на своей ветке с правками — Sync видит ветку."""
    vault = make_vault(tmp_path)
    _seed_branch(
        tmp_path, vault, {"derived/snapshots/mac-a.json": _snapshot_json("mac-a")}
    )
    _git(vault, "switch", "-q", "-c", "feature/wip")
    (vault / "wip.txt").write_text("dirty\n", encoding="utf-8")

    load = load_kb_snapshots((tmp_path,))

    assert isinstance(load, KbSnapshotLoad)
    assert [s.host for s in load.snapshots] == ["mac-a"]
    assert load.errors == []
    assert load.source_warning is None
    # рабочее дерево не содержит снапшота и не тронуто
    assert not (vault / "derived").exists()
    assert (vault / "wip.txt").read_text(encoding="utf-8") == "dirty\n"


def test_missing_ref_is_source_warning_not_fake_host(tmp_path: Path) -> None:
    make_vault(tmp_path)  # vault есть, ref origin/derived-snapshots — нет
    load = load_kb_snapshots((tmp_path,))
    assert load.snapshots == []
    assert load.errors == []  # НЕ (host, error) — иначе Sync нарисует машину
    assert load.source_warning is not None
    assert SNAPSHOT_BRANCH in load.source_warning


def test_missing_vault_is_source_warning(tmp_path: Path) -> None:
    load = load_kb_snapshots((tmp_path,))
    assert load.snapshots == [] and load.errors == []
    assert load.source_warning is not None


def test_per_file_errors_and_nested_junk(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    _seed_branch(
        tmp_path,
        vault,
        {
            "derived/snapshots/mac-a.json": _snapshot_json("mac-a"),
            "derived/snapshots/mac-b.json": "{not json",
            "derived/snapshots/mac-c.json": _snapshot_json("OTHER-HOST"),
            "derived/snapshots/sub/nested.json": _snapshot_json("nested"),
            "derived/snapshots/notes.txt": "junk\n",
        },
    )
    load = load_kb_snapshots((tmp_path,))
    assert [s.host for s in load.snapshots] == ["mac-a"]
    assert sorted(host for host, _ in load.errors) == ["mac-b", "mac-c"]
    assert load.source_warning is None


def test_build_report_carries_source_warning(tmp_path: Path) -> None:
    report = build_report(
        current_host="mac-a",
        live=None,
        live_error=None,
        kb_snapshots=[],
        kb_source_warning="ref unavailable: no fetch yet",
    )
    assert any("ref unavailable" in w for w in report.warnings)
    assert all(p.host != SNAPSHOT_BRANCH for p in report.hosts)
```

- [ ] **Step 2: Прогнать — убедиться, что падают**

Run: `uv run pytest tests/test_kb_snapshot_load.py -x -q`
Expected: FAIL — `ImportError: cannot import name 'SNAPSHOT_BRANCH'`.

- [ ] **Step 3: Реализация в `core/sync.py`**

Добавить рядом с `KB_REPO` (и обновить docstring модуля: источник — ветка, не рабочее дерево):

```python
SNAPSHOT_BRANCH = "derived-snapshots"
SNAPSHOT_REF = f"origin/{SNAPSHOT_BRANCH}"
_SNAPSHOTS_PREFIX = "derived/snapshots/"

# hostnames: буквы/цифры/точка/дефис/подчёркивание — иное могло бы выйти за
# пределы derived/snapshots как компонент имени файла (без ведущего дефиса)
SAFE_HOST_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")


class KbSnapshotLoad(BaseModel):
    """Результат чтения опубликованных снапшотов из ветки KB.

    Три раздельных канала: снапшоты, per-file ошибки (host, причина) и
    warning уровня ИСТОЧНИКА — недоступный vault/ref не превращается в
    фиктивную машину в списке хостов.
    """

    snapshots: list[Snapshot] = Field(default_factory=list)
    errors: list[tuple[str, str]] = Field(default_factory=list)
    source_warning: str | None = None
```

Заменить `kb_snapshot_dirs` + старый `load_kb_snapshots` (строки 270–303) на:

```python
def _git_read(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Read-only git у vault; сбои процесса — предмет source_warning."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=_SNAPSHOT_TIMEOUT,
    )


def load_kb_snapshots(roots: tuple[Path, ...]) -> KbSnapshotLoad:
    """Опубликованные `<host>.json` из `origin/derived-snapshots` (AP-02).

    Читает remote-tracking ref, никогда — рабочее дерево vault: локальный
    master намеренно больше не несёт машинный read-model (ecosystem-kb#98).
    Сеть не используется; свежесть ref даёт фоновый fetch.
    """
    snapshots: list[Snapshot] = []
    errors: list[tuple[str, str]] = []
    warnings: list[str] = []
    vaults = [r / KB_REPO for r in roots if (r / KB_REPO / ".git").exists()]
    if not vaults:
        return KbSnapshotLoad(
            source_warning=f"KB repo {KB_REPO!r} not found in any workspace root"
        )
    for vault in vaults:
        try:
            listing = _git_read(
                vault, "ls-tree", "-rz", "--name-only", SNAPSHOT_REF,
                "--", _SNAPSHOTS_PREFIX,
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            warnings.append(f"{vault}: git ls-tree failed: {err}")
            continue
        if listing.returncode != 0:
            warnings.append(
                f"{vault}: ref {SNAPSHOT_REF} unavailable "
                f"(fetch pending?): {listing.stderr.strip() or 'ls-tree failed'}"
            )
            continue
        for path in listing.stdout.split("\0"):
            if not path.startswith(_SNAPSHOTS_PREFIX):
                continue
            name = path[len(_SNAPSHOTS_PREFIX):]
            if "/" in name or not name.endswith(".json"):
                continue  # только непосредственные *.json
            host = name[: -len(".json")]
            if not SAFE_HOST_RE.fullmatch(host) or host in (".", ".."):
                continue
            try:
                blob = _git_read(vault, "cat-file", "blob", f"{SNAPSHOT_REF}:{path}")
            except (OSError, subprocess.TimeoutExpired) as err:
                errors.append((host, f"git cat-file failed: {err}"))
                continue
            if blob.returncode != 0:
                errors.append((host, blob.stderr.strip() or "cat-file failed"))
                continue
            try:
                snapshot = parse_snapshot(blob.stdout)
            except SnapshotContractError as err:
                errors.append((host, str(err)))
                continue
            if snapshot.host != host:
                # `<host>.json` convention (prograph-vault#24)
                errors.append(
                    (
                        host,
                        f"payload host {snapshot.host!r} does not match "
                        f"filename {name!r}",
                    )
                )
                continue
            snapshots.append(snapshot)
    return KbSnapshotLoad(
        snapshots=snapshots,
        errors=errors,
        source_warning="; ".join(warnings) or None,
    )
```

В `build_report` (строка 168): добавить keyword-параметр `kb_source_warning: str | None = None`; после блока `for name, err in kb_errors:` добавить:

```python
    if kb_source_warning is not None:
        warnings.append(kb_source_warning)
```

В `collect_sync` (строка ~317): заменить `kb_snapshots, kb_errors = load_kb_snapshots(kb_snapshot_dirs(config.roots))` на `kb_load = load_kb_snapshots(config.roots)` и передать в `build_report(...)`: `kb_snapshots=kb_load.snapshots, kb_errors=kb_load.errors, kb_source_warning=kb_load.source_warning`.

В `core/publish.py`: удалить локальные `re`-регекс `_SAFE_HOST_RE` и импортировать `from dispatcher.core.sync import KB_REPO, SAFE_HOST_RE`; заменить употребление в `write_snapshot`.

Механически обновить call-sites (warning до Epics доводит Task 5, здесь — только совместимость):

- `dispatcher/server/app.py`: `_epic_snapshots()` → `def _epic_snapshots() -> KbSnapshotLoad: return load_kb_snapshots(config.roots)` (импорт `KbSnapshotLoad, load_kb_snapshots` вместо `kb_snapshot_dirs`); в `/api/epics`: `load = _epic_snapshots()` и `build_view(config, load.snapshots, kind=kind, snapshot_errors=load.errors)`; в `/api/epics/{epic_id}` (строка ~469): `load = _epic_snapshots()`, `build_detail(config, epic_id, load.snapshots)`.
- `dispatcher/mcp_server.py` (тулзы `epics`, `epic`, строки 157–184): та же замена — `load = load_kb_snapshots(config.roots)`, использовать `load.snapshots` / `load.errors`.

- [ ] **Step 4: Прогнать новые тесты и смежные сьюты**

Run: `uv run pytest tests/test_kb_snapshot_load.py tests/test_sync.py tests/test_api.py tests/test_mcp_server.py tests/test_publish.py -q`
Expected: `test_kb_snapshot_load.py` PASS. В `test_sync.py`/`test_api.py`/`test_mcp_server.py` упадут тесты, которые раскладывали `*.json` в рабочее дерево `prograph-vault/derived/snapshots/` — переписать их фикстуры на `_seed_branch`-паттерн (вынести `_seed_branch` в `tests/conftest.py` как фикстуру-хелпер `seed_snapshot_branch`, если нужен более чем одному файлу). Смысл ассертов не менять; тест, проверявший чтение из рабочего дерева, становится тестом чтения из ref.

- [ ] **Step 5: Форматирование, типы, коммит**

```bash
uv run ruff format . && uv run ruff check . --fix
uv run pyrefly check
git add -A && git commit -m "feat(sync): reader снапшотов читает origin/derived-snapshots — KbSnapshotLoad с source_warning"
```

---

### Task 2: Publisher — эфемерный worktree, публикация в ветку (happy path)

**Files:**
- Modify: `dispatcher/core/publish.py` (заменить `commit_and_push` на `publish_to_branch`; обновить `publish` и module docstring)
- Test: `tests/test_publish.py` (переписать git-тесты; helpers `make_vault`, `make_snapshot`, `_git` остаются)

**Interfaces:**
- Consumes: `KB_REPO`, `SAFE_HOST_RE`, `SNAPSHOT_BRANCH` из `core/sync.py` (Task 1); `write_snapshot` без изменений.
- Produces: `publish_to_branch(vault_repo: Path, snapshot: Snapshot, *, push: bool = True, attempts: int = 3, sleeper: Callable[[float], None] = time.sleep, before_push: Callable[[int], None] | None = None) -> str` — исходы `"committed and pushed" | "no changes" | "validated; push skipped"`; `publish(...)` — прежняя сигнатура, ответ `"{branch}:derived/snapshots/{host}.json: {outcome}"`. `before_push(attempt)` — тестовый шов, вызывается перед каждым push.

- [ ] **Step 1: Написать падающие тесты**

В `tests/test_publish.py` удалить импорт и тесты `commit_and_push` (`test_commit_records_snapshot_and_skips_noop`, `test_publish_pushes_to_origin`, `test_commit_outside_vault_is_publish_error`) и добавить:

```python
from dispatcher.core.publish import publish_to_branch
from dispatcher.core.sync import SNAPSHOT_BRANCH


def _out(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def make_origin(root: Path, vault: Path) -> Path:
    """bare origin с master и засеянной derived-snapshots (как у владельца)."""
    origin = root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(vault, "remote", "add", "origin", str(origin))
    _git(vault, "push", "-q", "origin", "master")
    _git(vault, "push", "-q", "origin", f"master:{SNAPSHOT_BRANCH}")
    return origin


def test_publish_pushes_to_snapshot_branch(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    origin = make_origin(tmp_path, vault)
    out = publish(tmp_path, push=True, snapshot=make_snapshot("mac-a"))
    assert "committed and pushed" in out and SNAPSHOT_BRANCH in out
    payload = _out(
        origin, "show", f"{SNAPSHOT_BRANCH}:derived/snapshots/mac-a.json"
    )
    assert parse_snapshot(payload).host == "mac-a"
    # master на origin не двигался
    assert _out(origin, "rev-parse", "master") == _out(
        vault, "rev-parse", "master"
    )


def test_main_checkout_untouched_even_dirty_feature_branch(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    make_origin(tmp_path, vault)
    _git(vault, "switch", "-q", "-c", "feature/wip")
    (vault / "wip.txt").write_text("dirty\n", encoding="utf-8")
    head_before = _out(vault, "rev-parse", "HEAD")

    publish(tmp_path, snapshot=make_snapshot("mac-a"))

    assert _out(vault, "rev-parse", "HEAD") == head_before
    assert _out(vault, "branch", "--show-current").strip() == "feature/wip"
    assert (vault / "wip.txt").read_text(encoding="utf-8") == "dirty\n"
    assert not (vault / "derived").exists()
    # эфемерный worktree не пережил прогон
    assert _out(vault, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_second_run_without_change_is_no_changes(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    make_origin(tmp_path, vault)
    snap = make_snapshot("mac-a")
    publish(tmp_path, snapshot=snap)
    assert "no changes" in publish(tmp_path, snapshot=snap)


def test_missing_branch_on_origin_is_publish_error(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(vault, "remote", "add", "origin", str(origin))
    _git(vault, "push", "-q", "origin", "master")  # ветки снапшотов НЕТ
    with pytest.raises(PublishError):
        publish(tmp_path, snapshot=make_snapshot("mac-a"))


def test_no_push_validates_without_creating_commit(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    origin = make_origin(tmp_path, vault)
    before = _out(vault, "rev-list", "--all", "--count").strip()
    out = publish(tmp_path, push=False, snapshot=make_snapshot("mac-a"))
    assert "validated; push skipped" in out
    assert _out(vault, "rev-list", "--all", "--count").strip() == before
    assert "derived/snapshots" not in _out(origin, "ls-tree", "-r", SNAPSHOT_BRANCH)


def test_cleanup_removes_only_own_tmp_even_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dispatcher.core.publish as publish_module

    vault = make_vault(tmp_path)
    origin = make_origin(tmp_path, vault)
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    created: list[str] = []
    real_mkdtemp = publish_module.tempfile.mkdtemp

    def tracking_mkdtemp(**kwargs: object) -> str:
        path = real_mkdtemp(**kwargs)  # type: ignore[arg-type]
        created.append(path)
        return path

    monkeypatch.setattr(publish_module.tempfile, "mkdtemp", tracking_mkdtemp)
    with pytest.raises(PublishError):
        publish(tmp_path, snapshot=make_snapshot("mac-a"))
    assert created and all(not Path(p).exists() for p in created)
    assert _out(vault, "worktree", "list", "--porcelain").count("worktree ") == 1
```

- [ ] **Step 2: Прогнать — убедиться, что падают**

Run: `uv run pytest tests/test_publish.py -x -q`
Expected: FAIL — `ImportError: cannot import name 'publish_to_branch'`.

- [ ] **Step 3: Реализация в `core/publish.py`**

Обновить docstring модуля (write-path теперь ветка, не master-чекаут; ссылка на спеку). Импорты: `import random`, `import shutil`, `import sys`, `import time`, `from typing import Callable`, `from dispatcher.core.sync import KB_REPO, SAFE_HOST_RE, SNAPSHOT_BRANCH`. Удалить `commit_and_push`. Добавить:

```python
_SNAPSHOT_REFSPEC = (
    f"+refs/heads/{SNAPSHOT_BRANCH}:refs/remotes/origin/{SNAPSHOT_BRANCH}"
)
_SNAPSHOT_REF = f"origin/{SNAPSHOT_BRANCH}"
_PUSH_ATTEMPTS = 3
_RETRY = "__retry__"  # внутренний маркер: non-fast-forward, цикл повторяется


def _classify_push(proc: subprocess.CompletedProcess[str]) -> str:
    """'ok' | 'non_fast_forward' | 'fatal' — по porcelain, не по stderr.

    Локализованный stderr нестабилен; `--porcelain` даёт машинный формат
    `!\t<src>:<dst>\t[rejected] (<reason>)`. Retryable — только настоящий
    non-fast-forward; hook/auth/protected-branch не лечатся повтором.
    """
    if proc.returncode == 0:
        return "ok"
    for line in proc.stdout.splitlines():
        if line.startswith("!") and "[rejected]" in line and (
            "non-fast-forward" in line or "fetch first" in line
        ):
            return "non_fast_forward"
    return "fatal"


def _attempt_publish(
    vault_repo: Path,
    snapshot: Snapshot,
    *,
    push: bool,
    attempt: int,
    before_push: Callable[[int], None] | None,
) -> str:
    """Один полный цикл: fetch → worktree → write → commit → push."""
    _run(
        ["git", "-C", str(vault_repo), "fetch", "--quiet", "origin",
         _SNAPSHOT_REFSPEC],
        timeout=_GIT_TIMEOUT,
    )
    # git worktree add требует несуществующий целевой путь: сам
    # mkdtemp-каталог не годится, worktree живёт в его подпути
    tmp_root = Path(tempfile.mkdtemp(prefix="dispatcher-snapshot-publish-"))
    worktree = tmp_root / "worktree"
    registered = False
    try:
        _run(
            ["git", "-C", str(vault_repo), "worktree", "add", "--detach",
             str(worktree), _SNAPSHOT_REF],
            timeout=_GIT_TIMEOUT,
        )
        registered = True
        target = write_snapshot(snapshot, worktree / "derived" / "snapshots")
        rel = str(target.relative_to(worktree))
        _run(["git", "-C", str(worktree), "add", "--", rel], timeout=_GIT_TIMEOUT)
        status = _run(
            ["git", "-C", str(worktree), "status", "--porcelain", "--", rel],
            timeout=_GIT_TIMEOUT,
        )
        if not status.strip():
            return "no changes"
        if not push:
            # коммит не создаётся: после удаления worktree он был бы
            # недостижим, а "committed" вводил бы в заблуждение
            return "validated; push skipped"
        _run(
            ["git", "-C", str(worktree), "commit", "-q", "-m",
             f"chore(snapshots): {snapshot.host} sync snapshot", "--", rel],
            timeout=_GIT_TIMEOUT,
        )
        if before_push is not None:
            before_push(attempt)
        try:
            proc = subprocess.run(
                ["git", "-C", str(worktree), "push", "--porcelain", "origin",
                 f"HEAD:refs/heads/{SNAPSHOT_BRANCH}"],
                capture_output=True, text=True, timeout=_GIT_TIMEOUT,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as err:
            raise PublishError(f"git push: {err}") from err
        kind = _classify_push(proc)
        if kind == "ok":
            return "committed and pushed"
        if kind == "non_fast_forward":
            return _RETRY
        raise PublishError(
            f"push to {SNAPSHOT_BRANCH} rejected: "
            f"{(proc.stderr or proc.stdout).strip()}"
        )
    finally:
        _cleanup_worktree(vault_repo, worktree if registered else None, tmp_root)


def _cleanup_worktree(
    vault_repo: Path, worktree: Path | None, tmp_root: Path
) -> None:
    """Строго адресный cleanup: свой worktree и свой tmp_root, ничего чужого.

    `git worktree prune` не используется: глобальная операция могла бы
    подчистить чужое состояние. Сбой remove не маскирует основной результат —
    логируется, затем удаляется только собственный temp-каталог.
    """
    if worktree is not None:
        try:
            proc = subprocess.run(
                ["git", "-C", str(vault_repo), "worktree", "remove", "--force",
                 str(worktree)],
                capture_output=True, text=True, timeout=_GIT_TIMEOUT,
            )
            if proc.returncode != 0:
                print(
                    "warning: snapshot worktree cleanup failed: "
                    f"{proc.stderr.strip()}",
                    file=sys.stderr,
                )
        except (FileNotFoundError, subprocess.TimeoutExpired) as err:
            print(
                f"warning: snapshot worktree cleanup failed: {err}",
                file=sys.stderr,
            )
    shutil.rmtree(tmp_root, ignore_errors=True)


def publish_to_branch(
    vault_repo: Path,
    snapshot: Snapshot,
    *,
    push: bool = True,
    attempts: int = _PUSH_ATTEMPTS,
    sleeper: Callable[[float], None] = time.sleep,
    before_push: Callable[[int], None] | None = None,
) -> str:
    """Публикация `<host>.json` в ветку derived-snapshots (спека 2026-08-28).

    Retry — только на non-fast-forward, полным новым циклом от свежего
    fetch; любой другой отказ — немедленный PublishError. *before_push* —
    тестовый шов (вызывается с номером попытки перед push).
    """
    for attempt in range(1, attempts + 1):
        outcome = _attempt_publish(
            vault_repo, snapshot, push=push, attempt=attempt,
            before_push=before_push,
        )
        if outcome != _RETRY:
            return outcome
        if attempt < attempts:
            sleeper(random.uniform(0.5, 2.0))
    raise PublishError(
        f"push to {SNAPSHOT_BRANCH} was not fast-forward after "
        f"{attempts} attempts"
    )
```

`publish()` — заменить последние две строки тела:

```python
    outcome = publish_to_branch(vault_repo, snap, push=push)
    return f"{SNAPSHOT_BRANCH}:derived/snapshots/{snap.host}.json: {outcome}"
```

- [ ] **Step 4: Прогнать тесты**

Run: `uv run pytest tests/test_publish.py tests/test_cli.py -q`
Expected: PASS (в `test_cli.py` поправить ассерты на новый формат ответа, если они пиновали старый `"{path}: {outcome}"`).

- [ ] **Step 5: Форматирование, типы, коммит**

```bash
uv run ruff format . && uv run ruff check . --fix
uv run pyrefly check
git add -A && git commit -m "feat(publish): доставка снапшота в ветку derived-snapshots через эфемерный worktree"
```

---

### Task 3: Publisher — классификация push и bounded retry

**Files:**
- Modify: `dispatcher/core/publish.py` (только если Step 2 выявит пробелы — логика уже в Task 2)
- Test: `tests/test_publish.py` (классификация + конкурентные сценарии)

**Interfaces:**
- Consumes: `publish_to_branch(..., before_push=, sleeper=)`, `_classify_push` из Task 2.

- [ ] **Step 1: Написать падающие/доказательные тесты**

Добавить в `tests/test_publish.py`:

```python
from collections.abc import Callable

from dispatcher.core.publish import _classify_push


def _completed(returncode: int, stdout: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git", "push"], returncode=returncode, stdout=stdout, stderr=""
    )


def test_classify_push_ok() -> None:
    assert _classify_push(_completed(0, "")) == "ok"


def test_classify_push_non_fast_forward() -> None:
    line = "!\tHEAD:refs/heads/derived-snapshots\t[rejected] (non-fast-forward)\n"
    assert _classify_push(_completed(1, line)) == "non_fast_forward"


def test_classify_push_fetch_first_is_non_fast_forward() -> None:
    line = "!\tHEAD:refs/heads/derived-snapshots\t[rejected] (fetch first)\n"
    assert _classify_push(_completed(1, line)) == "non_fast_forward"


def test_classify_push_hook_rejection_is_fatal() -> None:
    line = (
        "!\tHEAD:refs/heads/derived-snapshots\t[remote rejected] "
        "(pre-receive hook declined)\n"
    )
    assert _classify_push(_completed(1, line)) == "fatal"


def _competing_pusher(tmp_path: Path, origin: Path) -> Callable[[], None]:
    """Пишет конкурентный коммит в derived-snapshots на origin."""
    clone = tmp_path / "competing"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True)
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    _git(clone, "switch", "-q", "-c", SNAPSHOT_BRANCH, f"origin/{SNAPSHOT_BRANCH}")
    counter = {"n": 0}

    def push_competing() -> None:
        counter["n"] += 1
        _git(clone, "pull", "-q", "--rebase", "origin", SNAPSHOT_BRANCH)
        (clone / f"competing-{counter['n']}.txt").write_text("x\n")
        _git(clone, "add", ".")
        _git(clone, "commit", "-q", "-m", f"competing {counter['n']}")
        _git(clone, "push", "-q", "origin", SNAPSHOT_BRANCH)

    return push_competing


def test_non_fast_forward_retries_with_fresh_cycle(tmp_path: Path) -> None:
    """Настоящий NFF: конкурентный коммит между созданием worktree и push."""
    vault = make_vault(tmp_path)
    origin = make_origin(tmp_path, vault)
    compete = _competing_pusher(tmp_path, origin)
    attempts: list[int] = []

    def before_push(attempt: int) -> None:
        attempts.append(attempt)
        if attempt == 1:
            compete()

    out = publish_to_branch(
        vault, make_snapshot("mac-a"), before_push=before_push,
        sleeper=lambda _s: None,
    )
    assert out == "committed and pushed"
    assert attempts == [1, 2]
    log = _out(origin, "log", "--oneline", SNAPSHOT_BRANCH)
    assert "mac-a sync snapshot" in log and "competing 1" in log


def test_retry_exhaustion_raises_publish_error(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    origin = make_origin(tmp_path, vault)
    compete = _competing_pusher(tmp_path, origin)
    attempts: list[int] = []

    def always_compete(attempt: int) -> None:
        attempts.append(attempt)
        compete()

    with pytest.raises(PublishError, match="not fast-forward after 3"):
        publish_to_branch(
            vault, make_snapshot("mac-a"), before_push=always_compete,
            sleeper=lambda _s: None,
        )
    assert attempts == [1, 2, 3]


def test_hook_rejection_is_not_retried(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    origin = make_origin(tmp_path, vault)
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    attempts: list[int] = []
    with pytest.raises(PublishError, match="rejected"):
        publish_to_branch(
            vault, make_snapshot("mac-a"), before_push=attempts.append,
            sleeper=lambda _s: None,
        )
    assert attempts == [1]  # ровно одна попытка — hook не лечится повтором
```

- [ ] **Step 2: Прогнать**

Run: `uv run pytest tests/test_publish.py -q`
Expected: PASS сразу, если Task 2 реализован по плану; любое падение здесь — дефект классификации/retry, чинить в `core/publish.py` до зелёного.

- [ ] **Step 3: Коммит**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "test(publish): porcelain-классификация push, NFF-retry новым циклом, hook rejection без повтора"
```

---

### Task 4: Фоновый fetch — явный refspec ветки снапшотов

**Files:**
- Modify: `dispatcher/core/sync_service.py:47-70` (`fetch_workspace`)
- Test: `tests/test_sync_service.py`

**Interfaces:**
- Consumes: `KB_REPO`, `SNAPSHOT_BRANCH` из `core/sync.py`.
- Produces: `fetch_workspace` дополнительно обновляет `refs/remotes/origin/derived-snapshots` у vault; ошибки — в тот же `list[str]`.

- [ ] **Step 1: Написать падающий тест**

Добавить в `tests/test_sync_service.py` (использовать локальные git-хелперы файла; если их нет — импортировать `_git` из `tests.test_publish`):

```python
def test_fetch_workspace_updates_snapshot_ref_on_single_branch_clone(
    tmp_path: Path,
) -> None:
    """Дефолтному refspec доверять нельзя: single-branch clone видит только
    master — ветку снапшотов обязан приносить явный refspec."""
    from dispatcher.core.sync import KB_REPO, SNAPSHOT_BRANCH
    from dispatcher.core.sync_service import fetch_workspace
    from tests.test_publish import _git, make_snapshot, make_vault

    seed = make_vault(tmp_path / "seed-root")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "master")
    _git(seed, "push", "-q", "origin", f"master:{SNAPSHOT_BRANCH}")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    vault = workspace / KB_REPO
    subprocess.run(
        ["git", "clone", "-q", "--single-branch", "--branch", "master",
         str(origin), str(vault)],
        check=True,
    )
    probe = ["git", "-C", str(vault), "rev-parse", "--verify",
             f"origin/{SNAPSHOT_BRANCH}"]
    assert subprocess.run(probe, capture_output=True).returncode != 0

    errors = fetch_workspace(workspace)

    assert errors == []
    assert subprocess.run(probe, capture_output=True).returncode == 0
```

- [ ] **Step 2: Прогнать — убедиться, что падает**

Run: `uv run pytest tests/test_sync_service.py::test_fetch_workspace_updates_snapshot_ref_on_single_branch_clone -q`
Expected: FAIL на последнем assert (single-branch refspec не принёс ветку).

- [ ] **Step 3: Реализация**

В `dispatcher/core/sync_service.py`: импорт `from dispatcher.core.sync import KB_REPO, SNAPSHOT_BRANCH, SyncReport, collect_sync`; в конец `fetch_workspace` перед `return errors`:

```python
    vault = workspace / KB_REPO
    if (vault / ".git").exists():
        # дефолтный refspec может быть single-branch (только master) —
        # ветку снапшотов приносит только явный refspec (спека 2026-08-28)
        refspec = (
            f"+refs/heads/{SNAPSHOT_BRANCH}:"
            f"refs/remotes/origin/{SNAPSHOT_BRANCH}"
        )
        try:
            proc = subprocess.run(
                ["git", "-C", str(vault), "fetch", "--quiet", "origin", refspec],
                capture_output=True, text=True,
                timeout=_FETCH_TIMEOUT_PER_REPO,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as err:
            errors.append(f"{KB_REPO} ({SNAPSHOT_BRANCH}): {err}")
        else:
            if proc.returncode != 0:
                errors.append(
                    f"{KB_REPO} ({SNAPSHOT_BRANCH}): "
                    f"{proc.stderr.strip() or 'fetch failed'}"
                )
    return errors
```

(Существующий `return errors` заменяется этим блоком; обновить docstring `fetch_workspace` — одна строка про явный refspec ветки снапшотов.)

- [ ] **Step 4: Прогнать**

Run: `uv run pytest tests/test_sync_service.py -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(sync): фоновый fetch vault явно обновляет ref derived-snapshots"
```

---

### Task 5: Epics — `source_warning` до web и MCP

**Files:**
- Modify: `dispatcher/core/epics.py:308-344` (`_github_planes`), `:552-590` (`build_view`), `:677-700` (`build_detail`)
- Modify: `dispatcher/server/app.py` (`/api/epics`, `/api/epics/{epic_id}`)
- Modify: `dispatcher/mcp_server.py` (тулзы `epics`, `epic`)
- Test: `tests/test_epics_view.py`, `tests/test_api.py`, `tests/test_mcp_server.py`

**Interfaces:**
- Produces: `build_view(..., snapshot_source_warning: str | None = None)`, `build_detail(..., snapshot_source_warning: str | None = None)`, `_github_planes(..., source_warning: str | None = None)`.
- Consumes: `KbSnapshotLoad` из Task 1.

- [ ] **Step 1: Написать падающие тесты**

В `tests/test_epics_view.py` (хелпер `_workspace` уже есть):

```python
def test_github_planes_carry_snapshot_source_warning(tmp_path: Path) -> None:
    """Недоступный ref — точная причина в detail, не общее 'no published
    snapshot': Epics обязан отличать «источник недоступен» от «пусто»."""
    config = _workspace(tmp_path, {})
    view = build_view(
        config,
        [],
        snapshot_source_warning="ref origin/derived-snapshots unavailable",
        now=_NOW,
    )
    gh = [p for p in view.planes if p.plane in ("issues", "pull_requests")]
    assert len(gh) == 2
    for plane in gh:
        assert plane.state == "unavailable"
        assert plane.detail is not None
        assert "snapshot source unavailable" in plane.detail
        assert "origin/derived-snapshots" in plane.detail
```

Там же, в `tests/test_epics_view.py`, поверхностный тест 9а по образцу
существующего web+MCP parity-теста этого файла (строка ~295: `TestClient` +
`build_server`/`call_tool`); `_workspace` не создаёт prograph-vault, поэтому
`source_warning` течёт end-to-end от reader'а:

```python
def test_epics_surfaces_source_warning_on_web_and_mcp(tmp_path: Path) -> None:
    """Тест 9а спеки: при недоступном ref обе поверхности отдают точную
    причину в detail GitHub-плоскостей, не общее 'no published snapshot'."""
    import asyncio

    from fastapi.testclient import TestClient
    from fastmcp import Client

    from dispatcher.mcp_server import build_server
    from dispatcher.server.app import create_app

    config = _workspace(tmp_path, {"demo": "- [ ] work @id:a @epic:eco.ops\n"})

    api = TestClient(create_app(config)).get("/api/epics").json()

    async def _mcp() -> dict:
        async with Client(build_server(config)) as client:
            result = await client.call_tool("epics", {})
            return json.loads(result.content[0].text)

    mcp = asyncio.run(_mcp())
    for payload in (api, mcp):
        issues = next(p for p in payload["planes"] if p["plane"] == "issues")
        assert issues["state"] == "unavailable"
        assert "snapshot source unavailable" in issues["detail"]
```

(Разбор результата `call_tool` — как в соседних MCP-тестах: если в
`tests/test_mcp_server.py` есть хелпер `_tool_json`, использовать его форму.)

- [ ] **Step 2: Прогнать — убедиться, что падают**

Run: `uv run pytest tests/test_epics_view.py -k source_warning -q`
Expected: FAIL — `build_view` не знает `snapshot_source_warning`.

- [ ] **Step 3: Реализация**

`dispatcher/core/epics.py`:

1. `_github_planes` — добавить параметр `source_warning: str | None = None` (после `now`); заменить вычисление `detail` в ветке `if not snapshots:`:

```python
        parts: list[str] = []
        if source_warning:
            parts.append(f"snapshot source unavailable: {source_warning}")
        if load_errors:
            parts.append(
                "published snapshots unreadable: "
                + "; ".join(
                    f"{host}: {reason}" for host, reason in sorted(load_errors)
                )
            )
        detail = "; ".join(parts) if parts else "no published snapshot"
```

2. `build_view` — добавить keyword `snapshot_source_warning: str | None = None`; в вызов `_github_planes(...)` передать `source_warning=snapshot_source_warning` (позиционные аргументы не менять).
3. `build_detail` — добавить keyword `snapshot_source_warning: str | None = None`; пробросить в `build_view(config, snapshots, snapshot_source_warning=snapshot_source_warning)` и в `_github_planes(snapshots or [], registry, source_warning=snapshot_source_warning)`.

`dispatcher/server/app.py`: в `/api/epics` — `build_view(config, load.snapshots, kind=kind, snapshot_errors=load.errors, snapshot_source_warning=load.source_warning)`; в `/api/epics/{epic_id}` — `build_detail(config, epic_id, load.snapshots, snapshot_source_warning=load.source_warning)`. Обновить docstring `_epic_snapshots`: третий самоименованный факт — «источник снапшотов недоступен».

`dispatcher/mcp_server.py`: тулза `epics` — `build_view(config, load.snapshots, kind=kind, snapshot_errors=load.errors, snapshot_source_warning=load.source_warning)`; тулза `epic` — `build_detail(config, epic_id, load.snapshots, snapshot_source_warning=load.source_warning)`.

- [ ] **Step 4: Прогнать**

Run: `uv run pytest tests/test_epics_view.py tests/test_epics_read_model_regressions.py tests/test_epics_unobserved.py tests/test_api.py tests/test_mcp_server.py -q`
Expected: PASS.

- [ ] **Step 5: Коммит**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A && git commit -m "feat(epics): source_warning снапшотов доезжает до плоскостей web и MCP"
```

---

### Task 6: CLI/доки, полный прогон

**Files:**
- Modify: `dispatcher/cli.py:1,26-28` (docstring модуля и help `publish-snapshot`)
- Modify: `README.md:317-331` (раздел publish-snapshot)
- Test: полный сьют

- [ ] **Step 1: CLI help**

В `dispatcher/cli.py` help парсера `publish-snapshot`: `"publish this host's sync snapshot to the KB branch derived-snapshots"`. Help у `--no-push` (если есть текст про commit) — «validate the pipeline without publishing (no commit is created)».

- [ ] **Step 2: README**

Обновить строки 317–331: снапшот публикуется в **ветку `derived-snapshots`** (резолюция ecosystem-kb#98) через эфемерный worktree; основной vault-чекаут не трогается; `--no-push` → `validated; push skipped`; читатели читают `origin/derived-snapshots`, свежесть даёт фоновый fetch. Cron-пример оставить как есть.

- [ ] **Step 3: Полный прогон и самопроверка спеки**

```bash
uv run ruff format . && uv run ruff check .
uv run pyrefly check
uv run pytest -q
```

Expected: всё зелёное. Сверить чек-лист тестов спеки (§Тесты, 1–11 и 9а) с фактическими тестами; пробел — дописать тест до коммита.

- [ ] **Step 4: Коммит**

```bash
git add -A && git commit -m "docs(cli,readme): publish-snapshot публикует в ветку derived-snapshots"
```

---

## После выполнения плана

1. `git push -u origin feat/snapshot-publish-branch`, `gh pr create` (тело: спека, inbox #199/#213, инварианты).
2. Ревью: `sh ../devtools/review-pr.sh dispatcher <pr> --dry-run`, затем без `--dry-run`; находки — фикс-коммитами.
3. В `TODO.md` пункт `@id:snapshot-publish-branch` → `[x]` + номер PR (отдельным коммитом в тот же PR).
4. Мерж — человек. После мержа: чистка ветки; хост-деплой — отдельный пункт `publish-snapshot-master-drift`.
