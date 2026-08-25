"""Ф3 — read-model оси эпиков: чем он обязан отличать «не читали» от «нет».

Тесты держат три инварианта ADR-ECO-010 D10/D11, каждый из которых легко потерять
рефакторингом и ни один из которых не проявится как падение: агрегат просто станет
уверенно неверным.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.epics import UNCLASSIFIED, build_view
from dispatcher.core.snapshot_contract import Snapshot, parse_snapshot

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

[programs.airun]
title = "airun"
kind  = "external"

[epics."eco.ops"]
title  = "Ops"
status = "standing"

[epics."airun.m3"]
title  = "airun M3"
status = "active"
opened = "2026-08-01"

[defect_classes.pipeline]
title = "Pipeline failures"
"""


# Свежесть снапшота — часть состояния плоскости, значит «сейчас» обязано быть
# входом, а не обращением к часам: иначе тест про ось версий начинает падать
# через час после того, как кто-то выбрал дату в фикстуре.
_NOW = datetime(2026, 8, 25, 10, 30, tzinfo=UTC)


def _workspace(tmp_path: Path, todos: dict[str, str]) -> DispatcherConfig:
    umbrella = tmp_path / "ai-orchestrators-workspace"
    umbrella.mkdir(parents=True)
    (umbrella / "epics.toml").write_text(_REGISTRY, encoding="utf-8")
    manifest = ['schema_version = "0.3.0"']
    for name, text in todos.items():
        repo = tmp_path / name
        (repo / ".git").mkdir(parents=True)
        (repo / "TODO.md").write_text(text, encoding="utf-8")
        manifest.append(f'[apps.{name}]\ngit_dir = "{name}"')
    (umbrella / "workspace-manifest.toml").write_text(
        "\n".join(manifest) + "\n", encoding="utf-8"
    )
    return DispatcherConfig(roots=(tmp_path,))


def _snapshot(version: int, *, issue_epic: dict | None = None) -> Snapshot:
    """Один снапшот с одним issue — В ФОРМЕ, которую объявляет пин своей версии.

    Раньше этот помощник строил «v2», не удовлетворяющий контракту (без `author`,
    без `subject_uri`/`carrier`/`observed_at` в классификации), и парсер это
    принимал: ровно тот дефект, который чинит Ф3-регрессия. Помощник исправлен, а
    не смягчена проверка — иначе тесты продолжили бы доказывать поведение, которого
    у настоящего продюсера нет.
    """
    generated = "2026-08-25T10:00:00Z"
    issue: dict[str, object] = {"number": 7, "title": "an issue"}
    github: dict[str, object]
    if version >= 2:
        issue["author"] = "dev"
        issue["epic"] = {
            "epic": None,
            "defect": None,
            "classification": "missing",
            "diagnostics": [],
            "subject_uri": "gh://demo/issues/7",
            "carrier": "issue",
            "observed_at": generated,
            **(issue_epic or {}),
        }
        github = {"name": "owner/demo", "issues": [issue], "pulls": []}
    else:
        github = {"issues": [issue], "pulls": []}
    payload = {
        "schema_version": version,
        "workspace": "/ws",
        "host": "h1",
        "generated_at": generated,
        "repos": [
            {
                "dir": "demo",
                "remote": "owner/demo",
                "local": {"branch": "master", "dirty": False},
                "github": github,
            }
        ],
    }
    return parse_snapshot(json.dumps(payload))


def test_planes_are_reported_separately_and_absence_is_not_zero(tmp_path: Path) -> None:
    """Главный инвариант: неизмеренная плоскость не притворяется нулём."""
    config = _workspace(tmp_path, {"demo": "- [ ] work @id:a @epic:eco.ops\n"})
    view = build_view(config, [])
    planes = {p.plane: p for p in view.planes}
    assert planes["todo"].state == "read"
    assert planes["issues"].state == "unavailable"
    assert planes["issues"].detail  # причина обязана быть названа
    assert planes["pull_requests"].state == "unavailable"


def test_a_v1_producer_is_a_supported_state_not_an_outage(tmp_path: Path) -> None:
    """Сосед, ещё не обновившийся до snapshot/v2, не должен ломать нашу панель.

    И при этом его плоскости обязаны быть `unavailable`, а не «эпиков нет»: одно —
    пробел наблюдения, другое — утверждение о флоте.
    """
    config = _workspace(tmp_path, {"demo": "- [ ] work @id:a @epic:eco.ops\n"})
    view = build_view(config, [_snapshot(1)])
    planes = {p.plane: p for p in view.planes}
    assert planes["issues"].state == "unavailable"
    assert "v1" in (planes["issues"].detail or "")


def test_a_v2_producer_contributes_real_counts(tmp_path: Path) -> None:
    config = _workspace(tmp_path, {"demo": "- [ ] work @id:a @epic:eco.ops\n"})
    view = build_view(
        config,
        [
            _snapshot(
                2,
                issue_epic={
                    "epic": "eco.ops",
                    "defect": None,
                    "classification": "tagged",
                    "diagnostics": [],
                },
            )
        ],
        now=_NOW,
    )
    planes = {p.plane: p for p in view.planes}
    assert planes["issues"].state == "read" and planes["issues"].count == 1
    ops = next(r for r in view.rows if r.id == "eco.ops")
    assert {p.plane: p.count for p in ops.planes} == {
        "todo": 1,
        "issues": 1,
        "pull_requests": 0,
    }


def test_an_unretrieved_body_is_not_counted_as_unmarked(tmp_path: Path) -> None:
    """`unavailable` на артефакте — это «не прочитали», а не «не размечен».

    Посчитать его как неразмеченный значит выдумать факт об артефакте, которого
    никто не читал, и раздуть ровно тот счётчик, по которому решают, пора ли
    переключать ось дайджеста.
    """
    config = _workspace(tmp_path, {"demo": "- [ ] work @id:a @epic:eco.ops\n"})
    view = build_view(
        config,
        [
            _snapshot(
                2,
                issue_epic={
                    "epic": None,
                    "defect": None,
                    "classification": "unavailable",
                    "diagnostics": [],
                },
            )
        ],
        now=_NOW,
    )
    planes = {p.plane: p for p in view.planes}
    assert planes["issues"].count == 0
    bucket = next(r for r in view.rows if r.type == "classification_bucket")
    assert {p.plane: p.count for p in bucket.planes}["issues"] == 0


def test_unclassified_bucket_is_present_even_at_zero(tmp_path: Path) -> None:
    """Строка, исчезающая на нуле, неотличима от строки, которую не посчитали."""
    config = _workspace(tmp_path, {"demo": "- [ ] work @id:a @epic:eco.ops\n"})
    view = build_view(config, [])
    bucket = [r for r in view.rows if r.type == "classification_bucket"]
    assert len(bucket) == 1
    assert bucket[0].id == UNCLASSIFIED
    assert sum(p.count for p in bucket[0].planes) == 0


def test_untagged_items_land_in_the_bucket(tmp_path: Path) -> None:
    config = _workspace(tmp_path, {"demo": "- [ ] no stream @id:a\n"})
    view = build_view(config, [])
    bucket = next(r for r in view.rows if r.type == "classification_bucket")
    assert {p.plane: p.count for p in bucket.planes}["todo"] == 1


def test_kind_filter_never_hides_the_bucket(tmp_path: Path) -> None:
    """Неразмеченный артефакт не принадлежит программе — значит фильтр по программе
    не вправе его скрыть, иначе частичный агрегат выглядит полным."""
    config = _workspace(tmp_path, {"demo": "- [ ] no stream @id:a\n"})
    view = build_view(config, [], kind="external")
    ids = [r.id for r in view.rows]
    assert UNCLASSIFIED in ids
    assert "eco.ops" not in ids  # ecosystem-эпик отфильтрован
    assert "airun.m3" in ids


def test_closed_items_are_not_counted(tmp_path: Path) -> None:
    config = _workspace(tmp_path, {"demo": "- [x] shipped @id:a @epic:eco.ops\n"})
    view = build_view(config, [])
    ops = next(r for r in view.rows if r.id == "eco.ops")
    assert {p.plane: p.count for p in ops.planes}["todo"] == 0


def test_defect_cut_is_independent_of_the_epic(tmp_path: Path) -> None:
    config = _workspace(
        tmp_path, {"demo": "- [ ] fix @id:a @epic:eco.ops @defect:pipeline\n"}
    )
    view = build_view(config, [])
    assert [(d.defect, d.count) for d in view.defects] == [("pipeline", 1)]
    assert view.defects[0].by_epic == {"eco.ops": 1}


def test_last_activity_comes_only_from_proven_semantics(tmp_path: Path) -> None:
    """Плоскость TODO не даёт даты изменения пункта, поэтому в last_activity не входит.

    Отсутствующее поле честнее поля с недоказуемой семантикой (D11).
    """
    config = _workspace(tmp_path, {"demo": "- [ ] work @id:a @epic:eco.ops\n"})
    view = build_view(config, [])
    ops = next(r for r in view.rows if r.id == "eco.ops")
    assert ops.last_activity_at is None
    assert ops.activity_sources == []


def test_a_missing_registry_is_an_error_not_an_empty_view(tmp_path: Path) -> None:
    (tmp_path / "empty").mkdir()
    view = build_view(DispatcherConfig(roots=(tmp_path,)), [])
    assert view.registry_ok is False
    assert view.registry_diagnostics
    assert all(p.state == "unavailable" for p in view.planes)


@pytest.mark.parametrize("version", [0, 3, 99])
def test_an_unsupported_snapshot_version_is_refused(version: int) -> None:
    from dispatcher.core.snapshot_contract import SnapshotContractError

    with pytest.raises(SnapshotContractError):
        _snapshot(version)


def test_api_and_mcp_expose_the_same_view(tmp_path: Path) -> None:
    """Одна read-model на все поверхности: web/API и MCP не вправе разойтись.

    Две реализации одного среза расходятся не сразу и не громко — сначала одна
    начинает считать закрытые пункты, и полгода два экрана показывают разные числа.
    """
    from fastapi.testclient import TestClient

    from dispatcher.mcp_server import build_server
    from dispatcher.server.app import create_app

    config = _workspace(
        tmp_path, {"demo": "- [ ] work @id:a @epic:eco.ops\n- [ ] bare @id:b\n"}
    )
    api = TestClient(create_app(config)).get("/api/epics").json()

    server = build_server(config)
    import asyncio

    tools = asyncio.run(server.get_tools())
    result = asyncio.run(tools["epics"].run({}))
    structured: dict = getattr(result, "structured_content", None) or {}
    payload: dict = structured.get("result", structured)

    def rows(view: dict) -> dict[str, dict[str, int]]:
        return {
            r["id"]: {p["plane"]: p["count"] for p in r["planes"]} for r in view["rows"]
        }

    assert payload, "MCP tool returned no structured content"
    assert rows(api) == rows(payload)


def test_the_manifest_root_is_scanned_not_the_first_root(tmp_path: Path) -> None:
    """Многокорневой конфиг: сканируется корень, где ЛЕЖИТ манифест.

    Иначе плоскость честно рапортует `read`, а пункты молча теряются — число,
    называющее себя полным, хуже, чем `unavailable`.
    """
    first = tmp_path / "empty-root"
    first.mkdir()
    second = tmp_path / "real-root"
    second.mkdir()
    config = _workspace(second, {"demo": "- [ ] work @id:a @epic:eco.ops\n"})
    multi = DispatcherConfig(roots=(first, *config.roots))

    view = build_view(multi, [])
    todo = next(p for p in view.planes if p.plane == "todo")
    assert todo.state == "read" and todo.count == 1
    ops = next(r for r in view.rows if r.id == "eco.ops")
    assert {p.plane: p.count for p in ops.planes}["todo"] == 1


def test_unreadable_snapshots_are_distinguished_from_none_published(
    tmp_path: Path,
) -> None:
    """«Ничего не опубликовано» и «опубликованное не читается» — разные факты."""
    config = _workspace(tmp_path, {"demo": "- [ ] work @id:a @epic:eco.ops\n"})

    silent = build_view(config, [])
    issues = next(p for p in silent.planes if p.plane == "issues")
    assert issues.detail == "no published snapshot"

    broken = build_view(
        config, [], snapshot_errors=[("host-a", "unsupported schema_version=99")]
    )
    issues = next(p for p in broken.planes if p.plane == "issues")
    assert issues.state == "unavailable"
    assert "unreadable" in (issues.detail or "")
    assert "host-a" in (issues.detail or "")


def test_mcp_rejects_a_kind_the_http_surface_would_refuse(tmp_path: Path) -> None:
    """Пустой ответ на неверный вопрос читается как «эпиков нет»."""
    import asyncio

    from dispatcher.mcp_server import build_server

    config = _workspace(tmp_path, {"demo": "- [ ] work @id:a @epic:eco.ops\n"})
    tools = asyncio.run(build_server(config).get_tools())
    with pytest.raises(Exception, match="unknown kind"):
        asyncio.run(tools["epics"].run({"kind": "nonsense"}))
