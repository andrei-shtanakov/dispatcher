"""Reader снапшотов: явное чтение origin/derived-snapshots, не рабочего дерева.

Спека 2026-08-28-snapshot-publish-branch: основной vault-чекаут может стоять
на любой ветке с любыми локальными изменениями — Sync обязан видеть снапшоты
из remote-tracking ref; отсутствие ref — source_warning, не фиктивный хост.
"""

from pathlib import Path

from conftest import seed_snapshot_branch

from dispatcher.core.sync import (
    SNAPSHOT_BRANCH,
    KbSnapshotLoad,
    build_report,
    load_kb_snapshots,
)
from tests.test_publish import _git, make_snapshot, make_vault


def _snapshot_json(host: str) -> str:
    return make_snapshot(host).model_dump_json(indent=2) + "\n"


def test_reads_ref_while_checkout_dirty_on_feature_branch(tmp_path: Path) -> None:
    """Пин владельца: чекаут на своей ветке с правками — Sync видит ветку."""
    vault = make_vault(tmp_path)
    seed_snapshot_branch(
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
    seed_snapshot_branch(
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


def test_mixed_roots_snapshot_and_warning_both_survive(tmp_path: Path) -> None:
    """Два workspace root: root A читаем, root B — vault без ref. Reader
    обязан вернуть И снапшот из A, И warning про B — ни один факт не должен
    молча вытеснить другой."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    vault_a = make_vault(root_a)
    seed_snapshot_branch(
        root_a, vault_a, {"derived/snapshots/mac-a.json": _snapshot_json("mac-a")}
    )
    make_vault(root_b)  # vault есть, ref origin/derived-snapshots — нет

    load = load_kb_snapshots((root_a, root_b))

    assert [s.host for s in load.snapshots] == ["mac-a"]
    assert load.errors == []  # НЕ (host, error) — иначе Sync нарисует машину
    assert load.source_warning is not None
    assert SNAPSHOT_BRANCH in load.source_warning


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
