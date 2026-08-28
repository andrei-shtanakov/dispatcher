"""TASK-204: publisher — atomic write, KB commit, contract-valid output."""

import json
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dispatcher.core.publish import (
    PublishError,
    _classify_push,
    publish,
    publish_to_branch,
    take_snapshot,
    write_snapshot,
)
from dispatcher.core.snapshot_contract import WorkspaceSnapshotV1, parse_snapshot
from dispatcher.core.sync import SNAPSHOT_BRANCH

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def make_snapshot(host: str = "mac-a") -> WorkspaceSnapshotV1:
    return WorkspaceSnapshotV1(
        schema_version=1,
        workspace="/ws",
        host=host,
        generated_at=NOW,
        gh_error=None,
        repos=[
            {
                "dir": "alpha",
                "remote": "o/alpha",
                "local": {
                    "branch": "master",
                    "ahead": 0,
                    "behind": 0,
                    "dirty": False,
                    "error": None,
                },
                "github": None,
            }
        ],
    )


def _git(path: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )


def make_vault(root: Path) -> Path:
    vault = root / "prograph-vault"
    vault.mkdir(parents=True)
    _git(vault, "init", "-q", "-b", "master")
    _git(vault, "config", "user.email", "t@example.com")
    _git(vault, "config", "user.name", "t")
    (vault / "README.md").write_text("kb\n")
    _git(vault, "add", "README.md")
    _git(vault, "commit", "-q", "-m", "init")
    return vault


def test_write_snapshot_is_atomic_and_named_by_host(tmp_path: Path) -> None:
    target = write_snapshot(make_snapshot("mac-a"), tmp_path)
    assert target.name == "mac-a.json"
    assert not list(tmp_path.glob("*.tmp"))
    # выход валиден против вендоренного контракта v1 (TASK-201)
    reparsed = parse_snapshot(target.read_text())
    assert reparsed.host == "mac-a"
    assert reparsed.schema_version == 1


def test_write_snapshot_overwrites_in_place(tmp_path: Path) -> None:
    write_snapshot(make_snapshot(), tmp_path)
    second = make_snapshot()
    second.gh_error = "changed"
    target = write_snapshot(second, tmp_path)
    assert json.loads(target.read_text())["gh_error"] == "changed"
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_write_snapshot_rejects_traversal_host(tmp_path: Path) -> None:
    evil = make_snapshot()
    evil.host = "../escape"
    with pytest.raises(PublishError, match="unsafe host"):
        write_snapshot(evil, tmp_path / "snapshots")
    assert not (tmp_path / "escape.json").exists()


def test_write_snapshot_rejects_leading_hyphen_host(tmp_path: Path) -> None:
    evil = make_snapshot()
    evil.host = "-rf"
    with pytest.raises(PublishError, match="unsafe host"):
        write_snapshot(evil, tmp_path / "snapshots")


def _out(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def make_origin(root: Path, vault: Path) -> Path:
    """bare origin с master и засеянной derived-snapshots (как у владельца)."""
    origin = root / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    _git(vault, "remote", "add", "origin", str(origin))
    _git(vault, "push", "-q", "origin", "master")
    _git(vault, "push", "-q", "origin", f"master:{SNAPSHOT_BRANCH}")
    return origin


def test_publish_pushes_to_snapshot_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import dispatcher.core.publish as publish_module

    vault = make_vault(tmp_path)
    origin = make_origin(tmp_path, vault)
    created: list[str] = []
    real_mkdtemp = publish_module.tempfile.mkdtemp

    def tracking_mkdtemp(**kwargs: object) -> str:
        path = real_mkdtemp(**kwargs)  # type: ignore[arg-type]
        created.append(path)
        return path

    monkeypatch.setattr(publish_module.tempfile, "mkdtemp", tracking_mkdtemp)
    out = publish(tmp_path, push=True, snapshot=make_snapshot("mac-a"))
    assert "committed and pushed" in out and SNAPSHOT_BRANCH in out
    payload = _out(origin, "show", f"{SNAPSHOT_BRANCH}:derived/snapshots/mac-a.json")
    assert parse_snapshot(payload).host == "mac-a"
    # master на origin не двигался
    assert _out(origin, "rev-parse", "master") == _out(vault, "rev-parse", "master")
    # временный worktree убран, а не забыт после успешного паблиша
    assert created and all(not Path(p).exists() for p in created)
    assert _out(vault, "worktree", "list", "--porcelain").count("worktree ") == 1


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


def test_publish_without_kb_repo_fails(tmp_path: Path) -> None:
    with pytest.raises(PublishError, match="KB repo not found"):
        publish(tmp_path, push=False, snapshot=make_snapshot())


def test_take_snapshot_missing_producer_fails(tmp_path: Path) -> None:
    with pytest.raises(PublishError):
        take_snapshot(tmp_path, command=("definitely-not-a-binary",))


def test_take_snapshot_rejects_contract_violation(tmp_path: Path) -> None:
    """Публиковать снапшот, формы которого мы не проверяем, нельзя.

    Исходный смысл теста, восстановленный после Ф3 (см. regression finding в
    `docs/findings/2026-08-25-epics-read-model-p0.md`). До Ф3 здесь стояла v2 —
    тогда неподдержанная. Когда v2 стала легитимной, я поменял число на 99 вместо
    того, чтобы восстановить ИНВАРИАНТ, и предохранитель снялся ровно в тот момент,
    когда стал нужен: непротиворечивость v2 по СОДЕРЖАНИЮ никто не проверял.

    Номер версии — частный случай. Общий: продюсер заявляет контракт, а отдаёт
    что-то другое.
    """
    payload = json.loads(make_snapshot().model_dump_json())
    payload["schema_version"] = 2
    payload["repos"][0]["github"] = {
        "name": "alpha",
        "pulls": [],
        "issues": [{"number": 7, "title": "an issue", "epic": {"ЧУШЬ": 123}}],
    }
    bad = json.dumps(payload)
    script = tmp_path / "fake.py"
    script.write_text(f"import sys; sys.stdout.write({bad!r})")
    with pytest.raises(PublishError, match="contract"):
        take_snapshot(tmp_path, command=("python3", str(script), "--ignored"))


def test_take_snapshot_rejects_an_unvendored_version(tmp_path: Path) -> None:
    """Отдельно — версия вне вендоренного набора: это уже другая проверка."""
    bad = json.dumps(
        {**json.loads(make_snapshot().model_dump_json()), "schema_version": 99}
    )
    script = tmp_path / "fake.py"
    script.write_text(f"import sys; sys.stdout.write({bad!r})")
    with pytest.raises(PublishError, match="contract"):
        take_snapshot(tmp_path, command=("python3", str(script), "--ignored"))


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
        vault,
        make_snapshot("mac-a"),
        before_push=before_push,
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
            vault,
            make_snapshot("mac-a"),
            before_push=always_compete,
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
            vault,
            make_snapshot("mac-a"),
            before_push=attempts.append,
            sleeper=lambda _s: None,
        )
    assert attempts == [1]  # ровно одна попытка — hook не лечится повтором
