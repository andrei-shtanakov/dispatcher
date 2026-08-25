"""Обязательность полей snapshot v1/v2 — сгенерированная сверка с пином.

Блокер повторного ревью Ф3: модели объявляли необязательными поля, которые пин
требует. Отсутствие поля разбиралось как `None`, а `None` дальше читался как «пусто»,
и панель показывала `read 0` там, где верный ответ — «данные не получены».

Проверка НЕ списком: список руками — это тот же ручной пересказ схемы, который уже
один раз разошёлся с ней. Сайты обязательных полей обходятся по самой схеме, поэтому
новое обязательное поле после ре-вендоринга попадает под проверку само.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from dispatcher.core.snapshot_contract import SnapshotContractError, parse_snapshot

_CONTRACTS = Path(__file__).parent.parent / "contracts" / "github-checker-snapshot"


def _resolve(node: dict[str, Any], defs: dict[str, Any], value: Any) -> dict[str, Any]:
    """Развернуть `$ref`/`anyOf` до ветки, которой реально соответствует значение."""
    if "$ref" in node:
        return _resolve(defs[node["$ref"].rsplit("/", 1)[-1]], defs, value)
    if "anyOf" in node:
        for branch in node["anyOf"]:
            resolved = _resolve(branch, defs, value)
            if resolved.get("type") == "null":
                continue
            if isinstance(value, dict) and "properties" in resolved:
                return resolved
            if isinstance(value, list) and resolved.get("type") == "array":
                return resolved
        return {}
    return node


def _sites(node: dict[str, Any], defs, value: Any, path: tuple):
    """Все места в фикстуре, где схема объявляет поле обязательным."""
    resolved = _resolve(node, defs, value)
    if isinstance(value, dict) and "properties" in resolved:
        for key in resolved.get("required", []):
            if key in value:
                yield path, key
        for key, sub in value.items():
            prop = resolved["properties"].get(key)
            if prop is not None:
                yield from _sites(prop, defs, sub, (*path, key))
    elif isinstance(value, list) and resolved.get("type") == "array":
        item = resolved.get("items")
        if item is not None:
            for i, element in enumerate(value):
                yield from _sites(item, defs, element, (*path, i))


def _at(payload: Any, path: tuple) -> Any:
    for step in path:
        payload = payload[step]
    return payload


def _cases(version: str) -> list[tuple[str, dict, dict, tuple, str]]:
    schema = json.loads((_CONTRACTS / version / "snapshot.schema.json").read_text())
    fixture = json.loads(
        (_CONTRACTS / version / "fixtures" / "snapshot_full.json").read_text()
    )
    defs = schema.get("$defs", {})
    out = []
    for path, key in _sites(schema, defs, fixture, ()):
        label = "/".join(str(p) for p in (*path, key)) or key
        out.append((f"{version}:{label}", schema, fixture, path, key))
    return out


CASES = _cases("v1") + _cases("v2")


def test_the_walk_actually_found_the_required_fields() -> None:
    """Обход, нашедший ноль сайтов, дал бы зелёный набор, ничего не проверяющий."""
    assert len(CASES) > 40, f"обход нашёл всего {len(CASES)} обязательных полей"
    labels = {c[0] for c in CASES}
    # контрольные точки: корень, вложенный объект, элемент массива, ось эпиков
    assert "v2:repos" in labels
    assert "v2:repos/0/remote" in labels
    assert "v2:repos/0/local/ahead" in labels
    assert "v2:repos/0/github/issues/0/epic/classification" in labels


@pytest.mark.parametrize(
    "label,schema,fixture,path,key", CASES, ids=[c[0] for c in CASES]
)
def test_removing_a_required_field_is_refused(
    label: str, schema: dict, fixture: dict, path: tuple, key: str
) -> None:
    """Удаление любого обязательного поля обязано отвергаться — и пином, и моделью.

    Расхождение здесь всегда однонаправленно опасно: модель мягче пина принимает то,
    что контракт запрещает, и делает это молча.
    """
    broken = copy.deepcopy(fixture)
    del _at(broken, path)[key]

    assert not Draft202012Validator(schema).is_valid(broken), (
        f"пин не отвергает удаление {label} — случай негоден"
    )
    with pytest.raises(SnapshotContractError):
        parse_snapshot(json.dumps(broken))
