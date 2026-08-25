"""«Данные не получены» ≠ ноль — на уровне репозитория и хоста.

Вторая половина блокера повторного ревью Ф3. Первая — модели принимали отсутствие
обязательных полей; эта — что даже при валидном снапшоте есть четыре законных способа
сказать «эту часть GitHub я не наблюдал», и все четыре read-model проглатывала,
объявляя плоскость прочитанной.

Асимметрия в самом контракте не случайна: `pulls` не nullable и по умолчанию пуст, а
`issues` — nullable. То есть `issues: null` — это НЕ «открытых задач нет», это «список
не получен», и разница ровно та, ради которой вся ось четырёхсостоянная.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.epics import EpicsView, PlaneState, build_view
from dispatcher.core.snapshot_contract import Snapshot, parse_snapshot

_NOW = datetime(2026, 8, 25, 10, 30, tzinfo=UTC)
_FRESH = "2026-08-25T10:00:00Z"

_REGISTRY = """\
schema_version = "1.0.0"
adopted_at = "2026-09-01"

[coverage_policy]
robin_cutover_todo   = 0.98
robin_cutover_issues = 0.90
robin_cutover_prs    = 0.90
missing_error_after  = "2026-11-01"

[programs.eco]
title = "Ecosystem"
kind  = "ecosystem"

[epics."eco.ops"]
title  = "Ops"
status = "standing"
"""


def _workspace(tmp_path: Path) -> DispatcherConfig:
    umbrella = tmp_path / "ai-orchestrators-workspace"
    umbrella.mkdir(parents=True)
    (umbrella / "epics.toml").write_text(_REGISTRY, encoding="utf-8")
    (umbrella / "workspace-manifest.toml").write_text(
        'schema_version = "0.3.0"\n[apps.demo]\ngit_dir = "demo"\n', encoding="utf-8"
    )
    repo = tmp_path / "demo"
    (repo / ".git").mkdir(parents=True)
    (repo / "TODO.md").write_text("- [ ] work @id:a @epic:eco.ops\n", encoding="utf-8")
    return DispatcherConfig(roots=(tmp_path,))


def _local() -> dict:
    return {"branch": "master", "ahead": 0, "behind": 0, "dirty": False, "error": None}


def _snapshot(*, github: dict | None, gh_error: str | None = None) -> Snapshot:
    return parse_snapshot(
        json.dumps(
            {
                "schema_version": 2,
                "workspace": "/ws",
                "host": "h1",
                "generated_at": _FRESH,
                "gh_error": gh_error,
                "repos": [
                    {
                        "dir": "demo",
                        "remote": "owner/demo",
                        "local": _local(),
                        "github": github,
                    }
                ],
            }
        )
    )


def _issues(view: EpicsView) -> PlaneState:
    return next(p for p in view.planes if p.plane == "issues")


def test_a_null_issue_list_is_not_zero_open_issues(tmp_path: Path) -> None:
    """`issues: null` — список не получен, а не «задач нет».

    `github.issues or []` схлопывало эти два случая в один, и плоскость рапортовала
    `read 0`. Ноль, полученный из ненаблюдения, — самое дорогое число в этой панели:
    именно по нему решают, что размечать больше нечего.
    """
    view = build_view(
        _workspace(tmp_path),
        [_snapshot(github={"name": "owner/demo", "pulls": [], "issues": None})],
        now=_NOW,
    )
    issues = _issues(view)
    assert issues.state != "read"
    assert "demo" in (issues.detail or "")


def test_a_repo_without_github_state_is_not_a_repo_without_work(tmp_path: Path) -> None:
    """Репозиторий с remote, но без блока github, — ненаблюдённый репозиторий."""
    view = build_view(_workspace(tmp_path), [_snapshot(github=None)], now=_NOW)
    issues = _issues(view)
    assert issues.state != "read"
    assert "demo" in (issues.detail or "")


def test_a_host_wide_gh_error_makes_the_github_planes_unobserved(
    tmp_path: Path,
) -> None:
    """`gh_error` — продюсер прямо говорит, что GitHub не опрошен.

    Поле читалось ровно никем: снапшот с `gh_error` и пустыми списками давал
    уверенный `read 0` — продюсер сообщил о провале, а панель показала факт о флоте.
    """
    view = build_view(
        _workspace(tmp_path),
        [_snapshot(github=None, gh_error="gh не авторизован")],
        now=_NOW,
    )
    issues = _issues(view)
    assert issues.state != "read"
    assert "h1" in (issues.detail or "")


def test_a_per_repo_github_error_degrades_the_plane(tmp_path: Path) -> None:
    """Ошибка по одному репо не делает остальные неверными, но и не молчит."""
    view = build_view(
        _workspace(tmp_path),
        [
            _snapshot(
                github={
                    "name": "owner/demo",
                    "pulls": [],
                    "issues": [],
                    "error": "rate limited",
                }
            )
        ],
        now=_NOW,
    )
    issues = _issues(view)
    assert issues.state != "read"
    assert "demo" in (issues.detail or "")


def test_a_fully_observed_repo_still_reads_clean(tmp_path: Path) -> None:
    """Обратная сторона: честно пустой результат обязан остаться `read 0`.

    Иначе лекарство хуже болезни — панель, у которой всё всегда `partial`,
    не отличает неполноту ни от чего и перестаёт что-либо значить.
    """
    view = build_view(
        _workspace(tmp_path),
        [_snapshot(github={"name": "owner/demo", "pulls": [], "issues": []})],
        now=_NOW,
    )
    issues = _issues(view)
    assert issues.state == "read"
    assert issues.count == 0
    assert issues.detail is None
