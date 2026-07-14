"""TASK-202: sync verdict engine — one test per degradation-matrix row."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.snapshot_contract import WorkspaceSnapshotV1
from dispatcher.core.sync import (
    KB_REPO,
    build_report,
    collect_sync,
    kb_snapshot_dirs,
    load_kb_snapshots,
)

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=UTC)


def snap(
    host: str,
    repos: list[dict],
    *,
    generated_at: datetime = NOW,
    gh_error: str | None = None,
) -> WorkspaceSnapshotV1:
    return WorkspaceSnapshotV1(
        schema_version=1,
        workspace="/ws",
        host=host,
        generated_at=generated_at,
        gh_error=gh_error,
        repos=[
            {
                "dir": r["dir"],
                "remote": r.get("remote"),
                "local": {
                    "branch": r.get("branch", "master"),
                    "ahead": r.get("ahead", 0),
                    "behind": r.get("behind", 0),
                    "dirty": r.get("dirty", False),
                    "error": r.get("error"),
                },
                "github": None,
            }
            for r in repos
        ],
    )


def report(live, **kwargs):
    return build_report(
        current_host="mac-a",
        live=live,
        live_error=kwargs.pop("live_error", None),
        kb_snapshots=kwargs.pop("kb_snapshots", []),
        kb_errors=kwargs.pop("kb_errors", None),
        now=NOW,
    )


def verdict_of(rep, host: str, repo: str):
    panel = next(p for p in rep.hosts if p.host == host)
    return next(v for v in panel.verdicts if v.repo == repo)


# --- verdict table rows ------------------------------------------------------


def test_clean_repo_is_ok_and_topline_ok() -> None:
    rep = report(snap("mac-a", [{"dir": "alpha"}]))
    assert verdict_of(rep, "mac-a", "alpha").verdict == "ok"
    assert rep.top_line == "ok"
    assert rep.top_reason is None


def test_behind_ahead_dirty_are_pull_first_with_reasons() -> None:
    rep = report(
        snap(
            "mac-a",
            [
                {"dir": "a", "behind": 5},
                {"dir": "b", "ahead": 2},
                {"dir": "c", "dirty": True},
            ],
        )
    )
    assert verdict_of(rep, "mac-a", "a").reason == "behind 5"
    assert verdict_of(rep, "mac-a", "b").reason == "ahead 2 (unpushed)"
    assert verdict_of(rep, "mac-a", "c").reason == "dirty worktree"
    assert all(
        verdict_of(rep, "mac-a", r).verdict == "pull-first" for r in ("a", "b", "c")
    )


def test_local_error_is_unknown() -> None:
    rep = report(
        snap("mac-a", [{"dir": "a", "error": "boom", "ahead": None, "behind": None}])
    )
    row = verdict_of(rep, "mac-a", "a")
    assert row.verdict == "unknown"
    assert "local git error" in (row.reason or "")


def test_no_upstream_is_unknown_not_ok() -> None:
    rep = report(snap("mac-a", [{"dir": "a", "ahead": None, "behind": None}]))
    row = verdict_of(rep, "mac-a", "a")
    assert row.verdict == "unknown"
    assert "ahead/behind unknown" in (row.reason or "")


def test_gh_error_does_not_poison_verdict() -> None:
    rep = report(snap("mac-a", [{"dir": "a"}], gh_error="gh: not authenticated"))
    assert verdict_of(rep, "mac-a", "a").verdict == "ok"
    panel = next(p for p in rep.hosts if p.host == "mac-a")
    assert panel.gh_error == "gh: not authenticated"


def test_stale_kb_panel_poisons_verdicts_to_unknown() -> None:
    old = NOW - timedelta(hours=2)
    rep = report(
        snap("mac-a", [{"dir": "a"}]),
        kb_snapshots=[snap("mac-b", [{"dir": "a"}], generated_at=old)],
    )
    panel = next(p for p in rep.hosts if p.host == "mac-b")
    assert panel.stale
    assert all(v.verdict == "unknown" for v in panel.verdicts if v.verdict != "no-data")


def test_fresh_kb_panel_keeps_verdicts() -> None:
    rep = report(
        snap("mac-a", [{"dir": "a"}]),
        kb_snapshots=[
            snap(
                "mac-b",
                [{"dir": "a", "behind": 1}],
                generated_at=NOW - timedelta(minutes=10),
            )
        ],
    )
    assert verdict_of(rep, "mac-b", "a").verdict == "pull-first"


def test_missing_repo_on_host_is_no_data_never_ok() -> None:
    rep = report(
        snap("mac-a", [{"dir": "a"}, {"dir": "b"}]),
        kb_snapshots=[snap("mac-b", [{"dir": "a"}])],
    )
    row = verdict_of(rep, "mac-b", "b")
    assert row.verdict == "no-data"


def test_kb_contract_error_panel_and_warning() -> None:
    rep = report(
        snap("mac-a", [{"dir": "a"}]),
        kb_errors=[("mac-c", "unsupported schema_version=2")],
    )
    panel = next(p for p in rep.hosts if p.host == "mac-c")
    assert panel.error is not None
    assert any("contracts/github-checker-snapshot/v1/" in w for w in rep.warnings)


# --- top line and KB special-casing -----------------------------------------


def test_topline_pull_first_beats_unknown() -> None:
    rep = report(
        snap(
            "mac-a",
            [
                {"dir": "a", "ahead": None, "behind": None},
                {"dir": "b", "behind": 3},
            ],
        )
    )
    assert rep.top_line == "pull-first"
    assert "b:" in (rep.top_reason or "")


def test_kb_repo_is_pinned_first_row() -> None:
    rep = report(snap("mac-a", [{"dir": "zzz"}, {"dir": KB_REPO}, {"dir": "aaa"}]))
    panel = next(p for p in rep.hosts if p.host == "mac-a")
    assert panel.verdicts[0].repo == KB_REPO
    assert panel.verdicts[0].is_kb


def test_live_supersedes_kb_snapshot_of_same_host() -> None:
    rep = report(
        snap("mac-a", [{"dir": "a"}]),
        kb_snapshots=[
            snap(
                "mac-a",
                [{"dir": "a", "behind": 9}],
                generated_at=NOW - timedelta(minutes=30),
            )
        ],
    )
    panels = [p for p in rep.hosts if p.host == "mac-a"]
    assert len(panels) == 1
    assert panels[0].source == "live"


def test_live_unavailable_falls_back_to_kb_panel_with_age() -> None:
    rep = report(
        None,
        live_error="github-checker: command not found",
        kb_snapshots=[
            snap("mac-a", [{"dir": "a"}], generated_at=NOW - timedelta(minutes=20))
        ],
    )
    assert any("live snapshot unavailable" in w for w in rep.warnings)
    assert rep.top_line == "ok"  # свежий KB-снапшот этого хоста даёт вердикт
    panel = next(p for p in rep.hosts if p.host == "mac-a")
    assert panel.source == "kb"
    assert panel.age_seconds == 1200.0


def test_no_snapshot_for_current_host_is_honest_unknown() -> None:
    rep = report(None, live_error="down", kb_snapshots=[snap("mac-b", [{"dir": "a"}])])
    assert rep.top_line == "unknown"
    assert "no snapshot for this host" in (rep.top_reason or "")


# --- IO shell ----------------------------------------------------------------


def test_load_kb_snapshots_reads_and_rejects(tmp_path: Path) -> None:
    d = tmp_path / "prograph-vault" / "derived" / "snapshots"
    d.mkdir(parents=True)
    good = snap("mac-b", [{"dir": "a"}])
    (d / "mac-b.json").write_text(good.model_dump_json())
    bad = json.loads(good.model_dump_json())
    bad["schema_version"] = 2
    (d / "mac-c.json").write_text(json.dumps(bad))

    snapshots, errors = load_kb_snapshots(kb_snapshot_dirs((tmp_path,)))
    assert [s.host for s in snapshots] == ["mac-b"]
    assert errors and errors[0][0] == "mac-c"


def test_collect_sync_survives_missing_github_checker(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))  # no github-checker anywhere
    config = DispatcherConfig(roots=(tmp_path,))
    rep = collect_sync(config, now=NOW)
    assert rep.top_line == "unknown"
    assert any("live snapshot unavailable" in w for w in rep.warnings)
