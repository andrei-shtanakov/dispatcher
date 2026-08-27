"""The launchpad snapshot assembler (Task 2): global reads once, one
classification pass per repo, one internally-consistent `LaunchpadSnapshot`.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from conftest import make_maestro_run

import dispatcher.core.launchpad as launchpad_module
import dispatcher.core.run_identity as run_identity_module
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.launchpad import REPO_UNRESOLVED, assemble_snapshot
from dispatcher.core.run_controller import RunController
from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_store import RunStore
from tests.test_inventory_capture import make_repo

_OWNER = "andrei-shtanakov"


def _config(tmp_path: Path) -> DispatcherConfig:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return DispatcherConfig(
        roots=(ws,),
        maestro_home=tmp_path / "mhome",
        run_state_dir=tmp_path / "state",
        # Never executed by these tests — assemble_snapshot only needs it
        # declared (`_require_on`'s ControlPlaneOff gate), never validated.
        maestro_cli=tmp_path / "fake-maestro",
    )


def _store(config: DispatcherConfig) -> RunStore:
    assert config.run_state_dir is not None  # always set by `_config` above
    return RunStore(config.run_state_dir)


def _key(name: str) -> RepoKey:
    return RepoKey(host="github.com", owner=_OWNER, repo=name)


def _remote(name: str) -> str:
    return f"git@github.com:{_OWNER}/{name}.git"


def _ready_repo(tmp_path: Path, name: str, work_id: str) -> Path:
    """A tmp git checkout under `<tmp_path>/ws/<name>` with one ready item
    (`make_repo`'s pattern, `tests/test_inventory_capture.py`) whose DAG
    names this SAME repo via `repo_url:` — the seam `classify_inventory`
    needs to call it ready (`tests/test_inventory_end_to_end.py`)."""
    (tmp_path / "ws").mkdir(exist_ok=True)
    remote = _remote(name)
    return make_repo(
        tmp_path / "ws",
        f"- [ ] Ready item @id:{work_id} @dag:dags/{work_id}.yaml\n",
        {f"dags/{work_id}.yaml": f"repo_url: {remote}\ntasks: []\n"},
        remote=remote,
        name=name,
    )


def test_two_ready_repos_and_snapshot_id_changes_per_assembly(tmp_path: Path) -> None:
    _ready_repo(tmp_path, "repo-a", "a1")
    _ready_repo(tmp_path, "repo-b", "b1")
    controller = RunController(_config(tmp_path))

    snap1 = assemble_snapshot(controller)
    snap2 = assemble_snapshot(controller)

    assert {r.repository for r in snap1.repositories} == {"repo-a", "repo-b"}
    assert all(r.admission == "ready" for r in snap1.repositories)
    assert {r.work_id for r in snap1.ready} == {"a1", "b1"}
    assert snap1.snapshot_id != snap2.snapshot_id


def test_live_record_blocks_its_own_repo_only(tmp_path: Path) -> None:
    _ready_repo(tmp_path, "repo-a", "a1")
    _ready_repo(tmp_path, "repo-b", "b1")
    config = _config(tmp_path)
    store = _store(config)
    key_a = _key("repo-a")

    store.reserve(
        "req-a",
        key_a,
        known_runs=[],
        window_start="t",
        work_id="a1",
        revision="a" * 40,
        repository="repo-a",
    )
    store.mark_launching("req-a")
    store.mark_materialized("req-a", "01AAA")
    assert config.maestro_home is not None
    make_maestro_run(
        config.maestro_home,
        key_a.as_path_parts(),
        "01AAA",
        started_at="2026-01-01T00:00:00Z",
    )

    snap = assemble_snapshot(RunController(config))

    row_a = next(r for r in snap.repositories if r.repository == "repo-a")
    row_b = next(r for r in snap.repositories if r.repository == "repo-b")
    assert row_a.admission == "blocked"
    assert any(b.code == "run_in_flight" for b in row_a.blockers)
    assert row_b.admission == "ready"


def test_missing_checkout_is_unreadable_others_unaffected(tmp_path: Path) -> None:
    _ready_repo(tmp_path, "repo-a", "a1")
    _ready_repo(tmp_path, "repo-c", "c1")
    (tmp_path / "ws" / "repo-b").mkdir()  # a manifest entry with no `.git`

    snap = assemble_snapshot(RunController(_config(tmp_path)))

    row_b = next(r for r in snap.repositories if r.repository == "repo-b")
    assert row_b.admission == "unreadable"
    assert len(row_b.blockers) == 1
    assert row_b.blockers[0].code == REPO_UNRESOLVED

    row_a = next(r for r in snap.repositories if r.repository == "repo-a")
    row_c = next(r for r in snap.repositories if r.repository == "repo-c")
    assert row_a.admission == "ready"
    assert row_c.admission == "ready"


def test_unreadable_repo_surfaces_as_unreadable_not_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I1: `classify_inventory` sets `decision.unreadable` for a repo
    whose TODO.md could not even be read (spec §10) — an injected reader
    error, never `chmod` (platform/root-dependent). The assembler must
    not fall through to `decision.repo.admission` in that case: that
    field only reflects lock/run state and knows nothing about a broken
    inventory capture, so leaving it unchecked rendered a broken repo as
    a clean, empty, "ready" one (fail-open) — indistinguishable from a
    healthy empty repo, and diverging from submit_v2, which refuses the
    same state as 409 `repo_unresolved` (`run_controller.py`'s own
    `decision.unreadable is not None` check)."""
    root = _ready_repo(tmp_path, "repo-a", "a1")
    todo_path = root / "TODO.md"
    real_read_text = Path.read_text

    def flaky_read_text(self: Path, *args: object, **kwargs: object) -> str:
        if self == todo_path:
            raise OSError("permission denied (injected)")
        return real_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    snap = assemble_snapshot(RunController(_config(tmp_path)))

    row = next(r for r in snap.repositories if r.repository == "repo-a")
    assert row.admission == "unreadable"
    assert len(row.blockers) == 1
    assert row.blockers[0].code == REPO_UNRESOLVED
    assert row.blockers[0].detail is not None
    assert "TODO.md" in row.blockers[0].detail
    assert snap.ready == []


def test_no_configured_root_is_a_directory_surfaces_a_note(tmp_path: Path) -> None:
    """I2: `_manifest_repos` must not silently return `([], [])` when NO
    configured root exists as a directory — same channel fix round 1
    used for a workspace scan failure: a named note in `store_unreadable`,
    not a silent empty fleet with no explanation."""
    missing_root = tmp_path / "does-not-exist"
    config = DispatcherConfig(
        roots=(missing_root,),
        maestro_home=tmp_path / "mhome",
        run_state_dir=tmp_path / "state",
        maestro_cli=tmp_path / "fake-maestro",
    )

    snap = assemble_snapshot(RunController(config))

    assert snap.repositories == []
    assert any(str(missing_root) in note for note in snap.store_unreadable)


def _matches_workspace(path: object, ws: Path) -> bool:
    """`capture_inventory` also calls `os.scandir` with an int dir-fd
    (`dags/` opened via `O_DIRECTORY`, `inventory.py`) — a `Path(...)`
    conversion must not blow up on that, it must just not match."""
    return isinstance(path, (str, Path)) and Path(path) == ws


def test_workspace_scan_failure_surfaces_not_a_silent_empty_fleet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix round 1: a workspace root that cannot even be scanned (a
    permission error, a bad mount) must not read as "no repositories" —
    the snapshot must carry a named failure the UI can show instead."""
    _ready_repo(tmp_path, "repo-a", "a1")
    config = _config(tmp_path)
    ws = tmp_path / "ws"
    real_scandir = os.scandir

    def flaky_scandir(path: object) -> object:
        if _matches_workspace(path, ws):
            raise OSError("permission denied")
        return real_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(run_identity_module.os, "scandir", flaky_scandir)

    snap = assemble_snapshot(RunController(config))

    assert snap.repositories == []
    assert any("permission denied" in note for note in snap.store_unreadable)


class _FakeEntry:
    """A minimal `os.DirEntry` stand-in — the real type is immutable
    (a non-heap C type), so its `is_dir` cannot be monkeypatched to raise;
    this lets the test target the per-entry degradation directly instead
    of depending on an OS-specific permission trick to provoke a real
    stat failure."""

    def __init__(self, name: str, *, boom: bool = False) -> None:
        self.name = name
        self._boom = boom

    def is_dir(self, *, follow_symlinks=True) -> bool:
        if self._boom:
            raise OSError("stat boom")
        return True


class _FakeScan:
    def __init__(self, entries: list[_FakeEntry]) -> None:
        self._entries = entries

    def __enter__(self) -> list[_FakeEntry]:
        return self._entries

    def __exit__(self, *exc: object) -> bool:
        return False


def test_one_bad_manifest_entry_does_not_empty_the_whole_listing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ready_repo(tmp_path, "repo-a", "a1")
    _ready_repo(tmp_path, "repo-b", "b1")
    config = _config(tmp_path)
    ws = tmp_path / "ws"
    real_scandir = os.scandir

    def flaky_scandir(path: object) -> object:
        if _matches_workspace(path, ws):
            return _FakeScan(
                [
                    _FakeEntry("repo-a"),
                    _FakeEntry("broken-entry", boom=True),
                    _FakeEntry("repo-b"),
                ]
            )
        return real_scandir(path)  # type: ignore[arg-type]

    monkeypatch.setattr(run_identity_module.os, "scandir", flaky_scandir)

    snap = assemble_snapshot(RunController(config))

    names = {r.repository for r in snap.repositories}
    assert names == {"repo-a", "repo-b"}


def _set_mtime(store: RunStore, request_id: str, when: float) -> None:
    path = store._record_path(request_id)  # noqa: SLF001 — test-only introspection
    os.utime(path, (when, when))


def test_pagination_attention_first_and_bad_cursor_raises(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    key = _key("widget")

    for i, req in enumerate(["t1", "t2", "t3"]):
        store.reserve(
            req,
            key,
            known_runs=[],
            window_start="t",
            work_id=f"w{i}",
            revision="a" * 40,
            repository="widget",
        )
        store.mark_terminal(req, outcome="completed")
        _set_mtime(store, req, 1_700_000_000 + i)

    store.reserve(
        "act1",
        key,
        known_runs=[],
        window_start="t",
        work_id="w-act1",
        revision="a" * 40,
        repository="widget",
    )
    store.mark_launching("act1")
    store.mark_materialized("act1", "01PLAIN")
    assert config.maestro_home is not None
    make_maestro_run(
        config.maestro_home,
        key.as_path_parts(),
        "01PLAIN",
        started_at="2026-01-01T00:00:00Z",
    )
    _set_mtime(store, "act1", 1_700_002_000)

    store.reserve(
        "act2",
        key,
        known_runs=[],
        window_start="t",
        work_id="w-act2",
        revision="a" * 40,
        repository="widget",
    )
    store.mark_launching("act2")
    store.mark_materialized("act2", "01REVIEW")
    make_maestro_run(
        config.maestro_home,
        key.as_path_parts(),
        "01REVIEW",
        started_at="2026-01-01T00:00:00Z",
        outcome="NEEDS_REVIEW",
    )
    _set_mtime(store, "act2", 1_700_001_000)

    controller = RunController(config)
    snap = assemble_snapshot(controller, recent_limit=2)

    assert snap.completed_total == 3
    assert [r.request_id for r in snap.recent_completed] == ["t3", "t2"]
    assert snap.next_cursor is not None

    assert snap.active[0].request_id == "act2"
    assert snap.active[0].run_status == "NEEDS_REVIEW"
    assert snap.active[0].attention is True

    page2 = assemble_snapshot(controller, recent_limit=2, cursor=snap.next_cursor)
    assert [r.request_id for r in page2.recent_completed] == ["t1"]
    assert page2.next_cursor is None

    with pytest.raises(ValueError):
        assemble_snapshot(controller, cursor="not*a*valid*cursor")


def test_unlinked_active_run_blocks_its_repo(tmp_path: Path) -> None:
    _ready_repo(tmp_path, "repo-a", "a1")
    config = _config(tmp_path)
    key = _key("repo-a")
    assert config.maestro_home is not None
    make_maestro_run(
        config.maestro_home,
        key.as_path_parts(),
        "01ZZZ",
        started_at="2026-01-01T00:00:00Z",
    )

    snap = assemble_snapshot(RunController(config))

    unlinked = [r for r in snap.active if r.request_id is None]
    assert len(unlinked) == 1
    assert unlinked[0].state == "unlinked-run"
    assert unlinked[0].run_id == "01ZZZ"
    assert unlinked[0].repo_key == key.as_text()

    row_a = next(r for r in snap.repositories if r.repository == "repo-a")
    assert row_a.admission == "blocked"
    assert any(b.code == "run_in_flight" for b in row_a.blockers)


def test_admission_rejected_record_in_recent_completed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    key = _key("widget")

    store.record_admission_rejection(
        "rej1",
        key,
        work_id="w1",
        revision="a" * 40,
        repository="widget",
        code="dag_invalid",
        detail="bad dag",
        current={},
    )

    snap = assemble_snapshot(RunController(config))

    row = next(r for r in snap.recent_completed if r.request_id == "rej1")
    assert row.run_id is None
    assert row.outcome == "admission-rejected"
    assert snap.completed_total == 1


def test_logs_available_reflects_the_log_directory(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    key = _key("widget")
    assert config.maestro_home is not None
    runs_root = config.maestro_home.joinpath("projects", *key.as_path_parts(), "runs")

    store.reserve(
        "no-logs",
        key,
        known_runs=[],
        window_start="t",
        work_id="w1",
        revision="a" * 40,
        repository="widget",
    )
    store.mark_launching("no-logs")
    store.mark_materialized("no-logs", "01NOLOGS")
    store.mark_terminal("no-logs", outcome="completed")
    (runs_root / "01NOLOGS").mkdir(parents=True)  # run dir exists, no logs/

    store.reserve(
        "with-logs",
        key,
        known_runs=[],
        window_start="t",
        work_id="w2",
        revision="a" * 40,
        repository="widget",
    )
    store.mark_launching("with-logs")
    store.mark_materialized("with-logs", "01WITHLOGS")
    store.mark_terminal("with-logs", outcome="completed")
    (runs_root / "01WITHLOGS" / "logs").mkdir(parents=True)

    snap = assemble_snapshot(RunController(config))

    rows = {r.request_id: r for r in snap.recent_completed}
    assert rows["no-logs"].logs_available is False
    assert rows["with-logs"].logs_available is True


def test_corrupt_record_blocks_every_repo_and_lists_in_store_unreadable(
    tmp_path: Path,
) -> None:
    _ready_repo(tmp_path, "repo-a", "a1")
    config = _config(tmp_path)
    store = _store(config)
    store._ensure()  # noqa: SLF001 — create requests/ before planting corruption
    assert config.run_state_dir is not None
    corrupt = config.run_state_dir / "requests" / "corrupt-thing.json"
    corrupt.write_bytes(b"{ not json at all")

    snap = assemble_snapshot(RunController(config))

    assert "corrupt-thing.json" in snap.store_unreadable
    row_a = next(r for r in snap.repositories if r.repository == "repo-a")
    assert row_a.admission == "blocked"
    assert any(b.code == "run_state_unreadable" for b in row_a.blockers)


def test_global_reads_happen_once_and_a_stat_failure_degrades_to_empty_mtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, work_id in (("repo-a", "a1"), ("repo-b", "b1"), ("repo-c", "c1")):
        _ready_repo(tmp_path, name, work_id)
    config = _config(tmp_path)
    store = _store(config)
    key = _key("repo-a")

    store.reserve(
        "ok1",
        key,
        known_runs=[],
        window_start="t",
        work_id="w1",
        revision="a" * 40,
        repository="repo-a",
    )
    store.mark_terminal("ok1", outcome="completed")
    store.reserve(
        "bad1",
        key,
        known_runs=[],
        window_start="t",
        work_id="w2",
        revision="b" * 40,
        repository="repo-a",
    )
    store.mark_terminal("bad1", outcome="completed")
    bad_path = store._record_path("bad1")  # noqa: SLF001

    real_stat = Path.stat

    def flaky_stat(self: Path, *args: object, **kwargs: object) -> object:
        if self == bad_path:
            raise OSError("stat boom")
        return real_stat(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", flaky_stat)

    list_calls = 0
    real_list_with_mtime = RunStore.list_with_mtime

    def counted_list_with_mtime(self: RunStore) -> object:
        nonlocal list_calls
        list_calls += 1
        return real_list_with_mtime(self)

    monkeypatch.setattr(RunStore, "list_with_mtime", counted_list_with_mtime)

    classified_calls = 0
    real_classified_runs = launchpad_module.classified_runs

    def counted_classified_runs(home: object, snap: object) -> object:
        nonlocal classified_calls
        classified_calls += 1
        return real_classified_runs(home, snap)  # type: ignore[arg-type]

    monkeypatch.setattr(launchpad_module, "classified_runs", counted_classified_runs)

    capture_calls = 0
    real_capture_inventory = launchpad_module.capture_inventory

    def counted_capture_inventory(checkout: object) -> object:
        nonlocal capture_calls
        capture_calls += 1
        return real_capture_inventory(checkout)  # type: ignore[arg-type]

    monkeypatch.setattr(
        launchpad_module, "capture_inventory", counted_capture_inventory
    )

    snap = assemble_snapshot(RunController(config))

    assert list_calls == 1
    assert classified_calls == 1
    assert capture_calls == 3

    rows = {r.request_id: r for r in snap.recent_completed}
    assert rows["bad1"].updated_at == ""
    assert snap.recent_completed[-1].request_id == "bad1"


def test_active_is_capped_at_200_and_truncated_flag_set(tmp_path: Path) -> None:
    config = _config(tmp_path)
    store = _store(config)
    key = _key("widget")

    for i in range(201):
        req = f"req{i:04d}"
        store.reserve(
            req,
            key,
            known_runs=[],
            window_start="t",
            work_id=f"w{i}",
            revision="a" * 40,
            repository="widget",
        )
        store.mark_launching(req)
        store.mark_materialized(req, f"01R{i:04d}")

    snap = assemble_snapshot(RunController(config))

    assert len(snap.active) == 200
    assert snap.active_truncated is True


def test_duplicate_checkouts_of_one_repo_key_are_conflict_not_ready(
    tmp_path,
):
    """Gate pass-2 finding: two checkouts of one RepoKey must not be Ready.

    A ReadyRow carries only repo_key — with two checkouts behind it, submit
    could resolve the OTHER copy (different HEAD) and persist a wrong
    decision. Fail-closed: both rows admission="unreadable" with a named
    duplicate-checkout blocker, no ready rows for that key.
    """
    remote = "git@github.com:andrei-shtanakov/dupd.git"
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    for name in ("a-copy", "b-copy"):
        make_repo(
            ws,
            "- [ ] A @id:w1 @dag:dags/w1.yaml\n",
            {"dags/w1.yaml": f"repo_url: {remote}\ntasks: []\n"},
            remote=remote,
            name=name,
        )
    snap = assemble_snapshot(RunController(_config(tmp_path)))
    key = "github.com/andrei-shtanakov/dupd"
    rows = [r for r in snap.repositories if r.repo_key == key]
    assert len(rows) == 2
    for row in rows:
        assert row.admission == "unreadable"
        (blocker,) = row.blockers
        assert blocker.code == "repo_unresolved"
        assert "a-copy" in (blocker.detail or "") and "b-copy" in (blocker.detail or "")
    assert [r for r in snap.ready if r.repo_key == key] == []


def test_default_branch_is_the_remote_default_not_the_checked_out_one(
    tmp_path,
):
    """Gate pass-4 minor: the field is NAMED default_branch (spec §4.1).

    A checkout sitting on a feature branch must still report the
    repository's default (origin/HEAD), falling back to the current
    branch only when origin/HEAD is not recorded locally.
    """
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    remote = "git@github.com:andrei-shtanakov/defbr.git"
    root = make_repo(
        ws,
        "- [ ] A @id:w1 @dag:dags/w1.yaml\n",
        {"dags/w1.yaml": f"repo_url: {remote}\ntasks: []\n"},
        remote=remote,
        name="defbr",
    )
    default = subprocess.run(
        ["git", "-C", str(root), "symbolic-ref", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            f"refs/remotes/origin/{default}",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(root), "checkout", "-q", "-b", "feat/work"],
        check=True,
    )
    snap = assemble_snapshot(RunController(_config(tmp_path)))
    row = next(r for r in snap.repositories if r.repository == "defbr")
    assert row.default_branch == default
