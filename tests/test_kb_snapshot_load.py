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
from tests.test_publish import _git, make_snapshot, make_vault


def _seed_branch(root: Path, vault: Path, files: dict[str, str]) -> Path:
    """bare-origin + ветка derived-snapshots с *files*; vault её отфетчил."""
    origin = root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, text=True)
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
