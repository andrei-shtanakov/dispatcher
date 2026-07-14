"""TASK-205: auto-discovery — sidecar state, proposals, confirm/reject."""

from datetime import UTC, datetime
from pathlib import Path

from dispatcher.core.snapshot_contract import WorkspaceSnapshotV1
from dispatcher.core.sync import build_report
from dispatcher.core.tracking import (
    TrackingState,
    decide,
    load_tracking,
    save_tracking,
    seed_tracking,
)

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def snap(host: str, dirs: list[str]) -> WorkspaceSnapshotV1:
    return WorkspaceSnapshotV1(
        schema_version=1,
        workspace="/ws",
        host=host,
        generated_at=NOW,
        gh_error=None,
        repos=[
            {
                "dir": d,
                "remote": None,
                "local": {
                    "branch": "master",
                    "ahead": 0,
                    "behind": 0,
                    "dirty": False,
                    "error": None,
                },
                "github": None,
            }
            for d in dirs
        ],
    )


def report(live, tracking):
    return build_report(
        current_host="mac-a",
        live=live,
        live_error=None,
        kb_snapshots=[],
        tracking=tracking,
        now=NOW,
    )


# --- sidecar state ------------------------------------------------------------


def test_tracking_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "dispatcher-sync.toml"
    save_tracking(path, TrackingState(tracked={"a", "b"}, ignored={"c"}))
    state = load_tracking(path)
    assert state is not None
    assert state.tracked == {"a", "b"}
    assert state.ignored == {"c"}


def test_load_missing_file_is_none(tmp_path: Path) -> None:
    assert load_tracking(tmp_path / "nope.toml") is None


def test_seed_tracks_everything_present(tmp_path: Path) -> None:
    path = tmp_path / "dispatcher-sync.toml"
    state = seed_tracking(path, {"a", "b"})
    assert state.tracked == {"a", "b"}
    assert load_tracking(path) == state


def test_decide_moves_between_sets(tmp_path: Path) -> None:
    path = tmp_path / "dispatcher-sync.toml"
    seed_tracking(path, {"a"})
    state = decide(path, "b", "ignore")
    assert "b" in state.ignored
    state = decide(path, "b", "track")  # передумали
    assert "b" in state.tracked
    assert "b" not in state.ignored


# --- acceptance FR-02: clone → proposal → confirm / reject --------------------


def test_new_repo_becomes_proposal_not_verdict(tmp_path: Path) -> None:
    tracking = seed_tracking(tmp_path / "s.toml", {"a"})
    rep = report(snap("mac-a", ["a", "fresh-clone"]), tracking)
    assert rep.proposals == ["fresh-clone"]
    panel = rep.hosts[0]
    assert [v.repo for v in panel.verdicts] == ["a"]
    assert rep.top_line == "ok"  # предложение не влияет на top-line


def test_confirm_moves_proposal_into_verdicts(tmp_path: Path) -> None:
    path = tmp_path / "s.toml"
    seed_tracking(path, {"a"})
    tracking = decide(path, "fresh-clone", "track")
    rep = report(snap("mac-a", ["a", "fresh-clone"]), tracking)
    assert rep.proposals == []
    assert {v.repo for v in rep.hosts[0].verdicts} == {"a", "fresh-clone"}


def test_reject_silences_repo_entirely(tmp_path: Path) -> None:
    path = tmp_path / "s.toml"
    seed_tracking(path, {"a"})
    tracking = decide(path, "fresh-clone", "ignore")
    rep = report(snap("mac-a", ["a", "fresh-clone"]), tracking)
    assert rep.proposals == []
    assert {v.repo for v in rep.hosts[0].verdicts} == {"a"}


def test_no_tracking_keeps_legacy_behaviour(tmp_path: Path) -> None:
    rep = report(snap("mac-a", ["a", "b"]), None)
    assert rep.proposals == []
    assert {v.repo for v in rep.hosts[0].verdicts} == {"a", "b"}


def test_proposal_from_other_hosts_snapshot(tmp_path: Path) -> None:
    tracking = seed_tracking(tmp_path / "s.toml", {"a"})
    rep = build_report(
        current_host="mac-a",
        live=snap("mac-a", ["a"]),
        live_error=None,
        kb_snapshots=[snap("mac-b", ["a", "cloned-on-b"])],
        tracking=tracking,
        now=NOW,
    )
    assert rep.proposals == ["cloned-on-b"]


def test_all_repos_untracked_gives_honest_unknown(tmp_path: Path) -> None:
    tracking = TrackingState()  # пусто: всё — предложения
    rep = report(snap("mac-a", ["a"]), tracking)
    assert rep.top_line == "unknown"
    assert "no tracked repos" in (rep.top_reason or "")
