"""Регрессии Ф3 — четыре P0, воспроизведённые до исправлений.

Разбор: `docs/findings/2026-08-25-epics-read-model-p0.md`. Общая форма всех
четырёх одна: **агрегат становится уверенно неверным, и ничего не падает**. Поэтому
каждый тест здесь проверяет не «не упало», а конкретное число или конкретное
состояние, которое обязано отличать «не читали» от «нет».
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.epics import UNCLASSIFIED, build_view
from dispatcher.core.snapshot_contract import (
    Snapshot,
    SnapshotContractError,
    parse_snapshot,
)

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

[epics."eco.dark-factory"]
title  = "Dark Factory"
status = "active"
opened = "2026-08-01"

[defect_classes.pipeline]
title = "Pipeline failures"
"""

_NOW = datetime(2026, 8, 25, 10, 30, tzinfo=UTC)
_FRESH = "2026-08-25T10:00:00Z"


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


def epic_block(epic: str | None, classification: str = "tagged") -> dict:
    """Полный `EpicClassification` завендоренной схемы v2 — все required поля."""
    return {
        "epic": epic,
        "defect": None,
        "classification": classification,
        "diagnostics": [],
        "subject_uri": "github://demo/issues/7#epic",
        "carrier": "issue",
        "observed_at": _FRESH,
    }


def v2_payload(
    host: str = "h1",
    *,
    epic: dict | None = None,
    generated_at: str = _FRESH,
    number: int = 7,
) -> dict:
    """Соответствующий контракту v2 снапшот с одним issue."""
    return {
        "schema_version": 2,
        "workspace": "/ws",
        "host": host,
        "generated_at": generated_at,
        "gh_error": None,
        "repos": [
            {
                "dir": "demo",
                "remote": "owner/demo",
                "local": {
                    "branch": "master",
                    "ahead": 0,
                    "behind": 0,
                    "dirty": False,
                },
                "github": {
                    "name": "demo",
                    "pulls": [],
                    "issues": [
                        {
                            "number": number,
                            "title": "an issue",
                            "author": "andrei-shtanakov",
                            "epic": epic
                            if epic is not None
                            else epic_block("eco.dark-factory"),
                        }
                    ],
                },
            }
        ],
    }


def v1_payload(host: str = "h-old") -> dict:
    return {
        "schema_version": 1,
        "workspace": "/ws",
        "host": host,
        "generated_at": _FRESH,
        "gh_error": None,
        "repos": [
            {
                "dir": "legacy",
                "remote": "owner/legacy",
                "local": {
                    "branch": "master",
                    "ahead": 0,
                    "behind": 0,
                    "dirty": False,
                },
                "github": {
                    "name": "owner/legacy",
                    "issues": [{"number": 1, "title": "old", "author": "dev"}],
                    "pulls": [],
                },
            }
        ],
    }


def _parse(payload: dict) -> Snapshot:
    return parse_snapshot(json.dumps(payload))


# ---------------------------------------------------------------- P0-1


def test_a_forged_v2_payload_is_refused_by_the_typed_model() -> None:
    """P0-1: `schema_version: 2` — заявление продюсера, а не доказательство формы.

    Ось эпиков в v2 типизирована в завендоренной схеме. Потребитель, который парсит
    обе версии одной моделью с `github: dict[str, Any]`, не проверяет из неё ничего:
    мусор в месте классификации проходит, читается как «поля classification нет»,
    и артефакт бесшумно оседает в бакете «без эпика» — ровно в том счётчике, по
    которому решают, пора ли переключать ось дайджеста.
    """
    forged = v2_payload(epic={"ЧУШЬ": 123})
    with pytest.raises(SnapshotContractError):
        _parse(forged)


def test_a_conforming_v2_payload_still_parses() -> None:
    """Строгость не должна отвергать честного продюсера — вторая половина P0-1."""
    snapshot = _parse(v2_payload())
    assert snapshot.schema_version == 2


def test_the_vendored_v2_fixtures_parse() -> None:
    """Фикстуры пина — единственное внешнее свидетельство формы, что у нас есть."""
    fixtures = Path("contracts/github-checker-snapshot/v2/fixtures")
    for path in sorted(fixtures.glob("*.json")):
        assert _parse(json.loads(path.read_text(encoding="utf-8"))) is not None


# ---------------------------------------------------------------- P0-2


def test_a_mixed_v1_v2_fleet_does_not_get_the_read_state(tmp_path: Path) -> None:
    """P0-2: `state` — машинное поле, `detail` — человеческое.

    Причина неполноты, дописанная только в `detail`, не видна ни вебу, ни MCP, ни
    Robin: все они смотрят на `state`. Флот, часть которого не опрошена вовсе,
    обязан отличаться от прочитанного целиком — в поле, по которому принимают
    решение, а не в строке рядом.
    """
    config = _workspace(tmp_path, {"demo": "- [ ] work @id:a @epic:eco.dark-factory\n"})
    view = build_view(config, [_parse(v1_payload()), _parse(v2_payload())], now=_NOW)
    planes = {p.plane: p for p in view.planes}
    assert planes["issues"].state != "read"
    assert "h-old" in (planes["issues"].detail or "")


# ---------------------------------------------------------------- P0-3


def test_an_unknown_github_epic_is_reported_and_stays_in_the_aggregate(
    tmp_path: Path,
) -> None:
    """P0-3: опечатка в теге не вправе уносить артефакт из агрегата.

    TODO-плоскость резолвится по реестру и получает EP-UNKNOWN; GitHub-плоскость не
    резолвится вовсе. Артефакт с неизвестным эпиком выпадает и из строк, и из
    бакета, оставаясь в итоге плоскости: сумма по строкам перестаёт сходиться с
    итогом, и ровно это read-model обязана не допускать.
    """
    config = _workspace(tmp_path, {"demo": "- [ ] work @id:a @epic:eco.dark-factory\n"})
    view = build_view(
        config, [_parse(v2_payload(epic=epic_block("eco.dark-factroy")))], now=_NOW
    )

    # Проверка намеренно не привязана к полю: инвариант — находка ОБЯЗАНА быть
    # названа хоть где-то. Где именно, решает исправление, и оно решило — в
    # `classification_diagnostics`, отдельно от реестра: опечатка в одном issue
    # ничего не говорит о самом epics.toml, а попав в `registry_diagnostics`, она
    # уронила бы `registry_ok` и отправила оператора чинить не тот файл.
    codes = {
        d["code"]
        for d in (*view.registry_diagnostics, *view.classification_diagnostics)
    }
    assert "EP-UNKNOWN" in codes, "неизвестный эпик обязан быть НАЗВАН"
    assert view.registry_ok is True, "реестр здесь ни при чём"

    assert "eco.dark-factroy" not in {r.id for r in view.rows}
    bucket = next(r for r in view.rows if r.type == "classification_bucket")
    assert {p.plane: p.count for p in bucket.planes}["issues"] == 1

    issues_total = next(p for p in view.planes if p.plane == "issues").count
    by_rows = sum({p.plane: p.count for p in r.planes}["issues"] for r in view.rows)
    assert by_rows == issues_total, "сумма по строкам обязана сходиться с итогом"


# ---------------------------------------------------------------- P0-4


def test_one_artifact_seen_by_two_producers_is_counted_once(tmp_path: Path) -> None:
    """P0-4a: две машины одного владельца видят один и тот же issue.

    Это нормальная конфигурация, а не сбой, и она удваивает каждый счётчик
    пересечения. Число растёт от того, что владелец завёл вторую машину.
    """
    config = _workspace(tmp_path, {"demo": "- [ ] work @id:a @epic:eco.dark-factory\n"})
    view = build_view(
        config,
        [_parse(v2_payload(host="h1")), _parse(v2_payload(host="h2"))],
        now=_NOW,
    )
    assert next(p for p in view.planes if p.plane == "issues").count == 1
    row = next(r for r in view.rows if r.id == "eco.dark-factory")
    assert {p.plane: p.count for p in row.planes}["issues"] == 1


def test_a_stale_producer_visibly_degrades_completeness(tmp_path: Path) -> None:
    """P0-4b: возраст снапшота не участвует в read-model вообще.

    `STALE_AFTER_SECONDS` живёт в sync.py; ось эпиков про него не знает, поэтому
    сутки назад снятые данные идут в счёт как свежие, и полнота выглядит выше
    реальной. Устаревший продюсер обязан ухудшать состояние ЯВНО.
    """
    config = _workspace(tmp_path, {"demo": "- [ ] work @id:a @epic:eco.dark-factory\n"})
    old = (_NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    view = build_view(
        config, [_parse(v2_payload(host="h-stale", generated_at=old))], now=_NOW
    )
    issues = next(p for p in view.planes if p.plane == "issues")
    assert issues.state != "read"
    assert "h-stale" in (issues.detail or "")


def test_the_unclassified_bucket_survives_all_of_it(tmp_path: Path) -> None:
    """Сквозной инвариант: бакет остаётся на месте при любом из сценариев выше."""
    config = _workspace(tmp_path, {"demo": "- [ ] bare @id:a\n"})
    view = build_view(config, [_parse(v2_payload())], now=_NOW)
    assert any(r.id == UNCLASSIFIED for r in view.rows)


# ------------------------------------------- находки ревью к самому исправлению


@pytest.mark.parametrize("version", [True, False, 1.0, "1", None])
def test_a_non_integer_schema_version_is_refused(version: object) -> None:
    """`True == 1` в Python, и на этом membership-тест версии ломается молча.

    `schema_version: true` проходило проверку «версия в поддержанном наборе» и
    уезжало в модель v1, где lax-режим достраивал `True` до `1`. Продюсер,
    отдающий чушь на месте версии, получал полноценно разобранный снапшот — это
    та же болезнь, что и P0-1, этажом выше: проверка есть, а проверяет она не то.
    """
    payload = v2_payload()
    payload["schema_version"] = version
    with pytest.raises(SnapshotContractError, match="schema_version"):
        _parse(payload)


def test_unreadable_snapshots_degrade_a_plane_that_others_could_read(
    tmp_path: Path,
) -> None:
    """Хост, чей снапшот не прочитался, — это НЕнаблюдённый хост.

    Ошибки загрузки учитывались только когда не прочиталось вообще ничего. Стоило
    одному снапшоту разобраться — и плоскость объявляла себя `read`, хотя вклад
    остальных неизвестен ровно так же, как у продюсера на v1.
    """
    config = _workspace(tmp_path, {"demo": "- [ ] work @id:a @epic:eco.dark-factory\n"})
    view = build_view(
        config,
        [_parse(v2_payload(host="h-ok"))],
        snapshot_errors=[("h-broken", "unsupported schema_version=99")],
        now=_NOW,
    )
    issues = next(p for p in view.planes if p.plane == "issues")
    assert issues.state == "partial"
    assert "h-broken" in (issues.detail or "")
    assert issues.count == 1, "прочитанное не выбрасывается, а помечается неполным"
