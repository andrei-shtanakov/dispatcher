"""Human queue A1 adapters (spec 2026-09-29-human-queue-a1-design §3)."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from conftest import make_maestro_run

from dispatcher.core.actions import ActionOutcome
from dispatcher.core.collectors.maestro import classified_runs, waiting_tasks
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.human_queue_sources import (
    FORGE_SOURCE,
    ForgeReader,
    from_impresario_report,
    from_maestro,
    from_pr_search,
    from_run_store,
    read_store,
)
from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.product_proposals import (
    BacklogWait,
    Diagnostic,
    GateWait,
    LoopWait,
    ProductProposalsReport,
    ProposalBundle,
)
from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_store import RunStore

_ACME = ("github.com", "acme", "app")


def _add_task(db: Path, task_id: str, status: str, created_at: str) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                task_id,
                f"title {task_id}",
                status,
                "claude_code",
                created_at,
                None,
                None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_waiting_tasks_has_no_limit_and_no_recency_window(tmp_path: Path) -> None:
    """The collector keeps the newest 50; the queue must keep every wait.

    60 waiting tasks, the oldest first, among 60 newer non-waiting ones: a
    LIMIT before OR after the status filter, or a recency order, fails this.
    """
    db = make_maestro_run(tmp_path, _ACME, "01RUN", started_at="2026-09-01T00:00:00")
    for i in range(60):
        status = "needs_review" if i % 2 == 0 else "awaiting_approval"
        _add_task(db, f"W-{i:03d}", status, f"2026-08-01T00:{i:02d}:00")
    for i in range(60):
        _add_task(db, f"N-{i:03d}", "in_progress", f"2026-09-02T00:{i:02d}:00")
    rows = waiting_tasks(db)
    assert [r["id"] for r in rows] == [f"W-{i:03d}" for i in range(60)]
    assert {r["status"] for r in rows} == {"needs_review", "awaiting_approval"}


def test_missing_state_is_reported_only_when_asked(tmp_path: Path) -> None:
    make_maestro_run(tmp_path, _ACME, "01GOOD", started_at="2026-09-01T00:00:00")
    tmp_path.joinpath("projects", *_ACME, "runs", "01BARE").mkdir(parents=True)

    silent = ProjectSnapshot(name="maestro", path="")
    runs = classified_runs(tmp_path, silent)
    assert [info.run_id for info, _ in runs] == ["01GOOD"]
    assert silent.warnings == []  # every existing caller: unchanged

    loud = ProjectSnapshot(name="maestro", path="")
    runs = classified_runs(tmp_path, loud, report_missing_state=True)
    assert [info.run_id for info, _ in runs] == ["01GOOD"]
    assert len(loud.warnings) == 1
    assert loud.warnings[0].startswith("run 01BARE: no state.db")


_REQ = "11111111-1111-4111-8111-111111111111"
_OLD_REQ = "22222222-2222-4222-8222-222222222222"
_KEY = RepoKey(host="github.com", owner="acme", repo="app")


def _config(tmp_path: Path, *, control_plane: bool = True) -> DispatcherConfig:
    return DispatcherConfig(
        roots=(tmp_path / "ws",),
        maestro_home=tmp_path / "mhome",
        run_state_dir=tmp_path / "state" if control_plane else None,
    )


# -- dispatcher_runs ---------------------------------------------------------


def test_run_store_off_is_not_configured(tmp_path: Path) -> None:
    result = from_run_store(read_store(_config(tmp_path, control_plane=False)))
    assert result.status.state == "not_configured"
    assert result.waits == []


def test_launch_unknown_is_a_wait_aged_by_unknown_at(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t", work_id="w1")
    record = store.mark_unknown(_REQ, "no run appeared")
    result = from_run_store(read_store(config))
    assert result.status.state == "ok"
    [wait] = result.waits
    assert wait.key == f"dispatcher-run:{_REQ}"
    assert wait.reasons == ["launch_unknown"]
    assert wait.since == record.unknown_at
    assert wait.since_basis is not None
    assert wait.act.kind == "run_view"


def test_record_without_unknown_at_has_unknown_age(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    store.reserve(_OLD_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_unknown(_OLD_REQ, "no run appeared")
    path = store._record_path(_OLD_REQ)  # noqa: SLF001 — simulating an old record
    raw = json.loads(path.read_text())
    raw.pop("unknown_at")
    path.write_text(json.dumps(raw))
    [wait] = from_run_store(read_store(config)).waits
    assert (wait.since, wait.since_basis) == (None, None)


def test_unreadable_record_makes_source_partial(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    store = RunStore(config.run_state_dir)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_unknown(_REQ, "no run appeared")
    (config.run_state_dir / "requests" / "broken.json").write_text("{not json")
    result = from_run_store(read_store(config))
    assert result.status.state == "partial"
    assert "broken.json" in (result.status.detail or "")
    assert len(result.waits) == 1


# -- maestro -----------------------------------------------------------------


def test_waits_from_every_non_terminal_run_not_only_the_newest(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    old = make_maestro_run(home, _ACME, "01OLD", started_at="2026-09-01T00:00:00")
    _add_task(old, "T-old", "needs_review", "2026-09-01T00:00:00")
    new = make_maestro_run(home, _ACME, "01NEW", started_at="2026-09-10T00:00:00")
    _add_task(new, "T-000", "awaiting_approval", "2026-08-01T00:00:00")
    for i in range(1, 60):
        _add_task(new, f"T-{i:03d}", "in_progress", f"2026-09-10T00:{i:02d}:00")
    result = from_maestro(home, [])
    assert result.status.state == "ok"
    assert sorted(w.key for w in result.waits) == [
        "maestro:github.com/acme/app:01NEW:T-000:awaiting_approval",
        "maestro:github.com/acme/app:01OLD:T-old:needs_review",
    ]
    by_ref = {w.ref: w for w in result.waits}
    assert by_ref["01OLD/T-old"].reasons == ["run_needs_review"]
    assert by_ref["01OLD/T-old"].act.model_dump() == {
        "kind": "maestro_verb",
        "verb": "retry",
        "task_id": "T-old",
        "run_id": "01OLD",
        "repo_key": "github.com/acme/app",
        "maestro_home": str(home),
        "atp_catalog": None,
        "maestro_cli": None,
    }
    assert by_ref["01NEW/T-000"].act.model_dump()["verb"] == "approve"
    assert all(w.since is None for w in result.waits)


def test_waiting_task_in_a_finished_run_is_not_a_wait(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    db = make_maestro_run(
        home,
        _ACME,
        "01DONE",
        started_at="2026-09-01T00:00:00",
        outcome="completed",
        ended_at="2026-09-01T01:00:00",
    )
    _add_task(db, "T-1", "needs_review", "2026-09-01T00:00:00")
    result = from_maestro(home, [])
    assert result.status.state == "ok"
    assert result.waits == []


def test_unreadable_run_db_is_partial_others_still_listed(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    good = make_maestro_run(home, _ACME, "01GOOD", started_at="2026-09-01T00:00:00")
    _add_task(good, "T-1", "needs_review", "2026-09-01T00:00:00")
    bad = home.joinpath("projects", *_ACME, "runs", "01BAD", "state.db")
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a database")
    result = from_maestro(home, [])
    assert result.status.state == "partial"
    assert "01BAD" in (result.status.detail or "")
    assert [w.ref for w in result.waits] == ["01GOOD/T-1"]


def test_failing_task_query_on_a_readable_run_is_partial(tmp_path: Path) -> None:
    """The `run` row reads (run classified), the task query does not."""
    home = tmp_path / "mhome"
    good = make_maestro_run(home, _ACME, "01GOOD", started_at="2026-09-01T00:00:00")
    _add_task(good, "T-1", "needs_review", "2026-09-01T00:00:00")
    broken = make_maestro_run(
        home, _ACME, "01NOTASKS", started_at="2026-09-02T00:00:00"
    )
    conn = sqlite3.connect(broken)
    try:
        conn.execute("DROP TABLE tasks")
        conn.commit()
    finally:
        conn.close()
    result = from_maestro(home, [])
    assert result.status.state == "partial"
    assert "01NOTASKS" in (result.status.detail or "")
    assert [w.ref for w in result.waits] == ["01GOOD/T-1"]


def test_run_dir_without_state_db_is_partial(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    good = make_maestro_run(home, _ACME, "01GOOD", started_at="2026-09-01T00:00:00")
    _add_task(good, "T-1", "needs_review", "2026-09-01T00:00:00")
    home.joinpath("projects", *_ACME, "runs", "01BARE").mkdir()
    result = from_maestro(home, [])
    assert result.status.state == "partial"
    assert "01BARE" in (result.status.detail or "")
    assert [w.ref for w in result.waits] == ["01GOOD/T-1"]


def test_unlistable_runs_dir_is_partial(tmp_path: Path) -> None:
    """An enumeration warning from classified_runs is a hole in the source."""
    home = tmp_path / "mhome"
    good = make_maestro_run(home, _ACME, "01GOOD", started_at="2026-09-01T00:00:00")
    _add_task(good, "T-1", "needs_review", "2026-09-01T00:00:00")
    other = ("github.com", "acme", "locked")
    make_maestro_run(home, other, "01HIDDEN", started_at="2026-09-01T00:00:00")
    runs_dir = home.joinpath("projects", *other, "runs")
    runs_dir.chmod(0)
    try:
        if os.access(runs_dir, os.R_OK):
            pytest.skip("running as a user that ignores directory permissions")
        result = from_maestro(home, [])
    finally:
        runs_dir.chmod(0o755)
    assert result.status.state == "partial"
    assert "cannot list" in (result.status.detail or "")
    assert [w.ref for w in result.waits] == ["01GOOD/T-1"]


def test_absent_maestro_home_is_a_clean_zero(tmp_path: Path) -> None:
    result = from_maestro(tmp_path / "nowhere", [])
    assert result.status.state == "ok"
    assert result.waits == []


def test_run_launched_by_dispatcher_routes_to_run_view(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    home = tmp_path / "mhome"
    db = make_maestro_run(home, _ACME, "01RUN", started_at="2026-09-01T00:00:00")
    _add_task(db, "T-1", "needs_review", "2026-09-01T00:00:00")
    store = RunStore(config.run_state_dir)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_materialized(_REQ, "01RUN")
    listing = read_store(config)
    assert listing is not None
    [wait] = from_maestro(home, listing[0]).waits
    assert wait.act.model_dump() == {"kind": "run_view", "request_id": _REQ}


def test_run_id_under_another_repo_key_does_not_route_to_run_view(
    tmp_path: Path,
) -> None:
    """The join is (repo_key, run_id): the same run_id elsewhere is not a match."""
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    home = tmp_path / "mhome"
    db = make_maestro_run(home, _ACME, "01RUN", started_at="2026-09-01T00:00:00")
    _add_task(db, "T-1", "needs_review", "2026-09-01T00:00:00")
    other = RepoKey(host="github.com", owner="acme", repo="other")
    store = RunStore(config.run_state_dir)
    store.reserve(_REQ, other, known_runs=[], window_start="t")
    store.mark_materialized(_REQ, "01RUN")
    listing = read_store(config)
    assert listing is not None
    [wait] = from_maestro(home, listing[0]).waits
    assert wait.act.model_dump()["kind"] == "maestro_verb"


def test_record_without_run_id_does_not_route_to_run_view(tmp_path: Path) -> None:
    config = _config(tmp_path)
    assert config.run_state_dir is not None
    home = tmp_path / "mhome"
    db = make_maestro_run(home, _ACME, "01RUN", started_at="2026-09-01T00:00:00")
    _add_task(db, "T-1", "needs_review", "2026-09-01T00:00:00")
    store = RunStore(config.run_state_dir)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_unknown(_REQ, "no run appeared")
    listing = read_store(config)
    assert listing is not None
    [wait] = from_maestro(home, listing[0]).waits
    assert wait.act.model_dump()["kind"] == "maestro_verb"


# -- impresario ---------------------------------------------------------------


def _report(**kw: object) -> ProductProposalsReport:
    return ProductProposalsReport(mirror_path="/m", **kw)  # type: ignore[arg-type]


_LOOP = LoopWait(
    loop_id="L1",
    iteration=3,
    proposal_id="PP-1",
    reason="creator stuck",
    stopped_at="2026-09-20T08:00:00Z",
    bundle_path="pilot/pp-1",
)
_GATE = GateWait(
    proposal_id="PP-2",
    gate_id="qg5_business",
    gate_label="Gate A",
    authority="business_owner",
    artifact_ref="proposal://PP-2",
    bundle_path="pilot/pp-2",
    version=4,
    proposal_updated_at="2026-09-21T00:00:00Z",
)
_BACKLOG = BacklogWait(
    backlog_id="BL-1",
    artifact_ref="backlog://BL-1",
    artifact_path="backlogs/bl-1/backlog.yaml",
    version=2,
    backlog_updated_at="2026-09-22T00:00:00Z",
)


def test_impresario_waits_and_their_ages() -> None:
    result = from_impresario_report(
        _report(needs_human=[_LOOP], waits=[_GATE], backlog_waits=[_BACKLOG])
    )
    assert result.status.state == "ok"
    by_key = {w.key: w for w in result.waits}
    loop = by_key["impresario-loop:L1:3"]
    assert loop.reasons == ["loop_needs_human"]
    assert loop.since == "2026-09-20T08:00:00+00:00"
    gate = by_key["impresario-gate:PP-2:qg5_business:4"]
    assert (gate.since, gate.since_basis) == (None, None)
    assert gate.act.model_dump() == {"kind": "open_artifact", "path": "pilot/pp-2"}
    backlog = by_key["impresario-qg4:BL-1:2"]
    assert backlog.reasons == ["backlog_gate"]
    assert (backlog.since, backlog.since_basis) == (None, None)


def test_non_ok_bundle_makes_source_partial_keeps_ok_waits() -> None:
    bad = ProposalBundle(path="pilot/pp-9", state="unreadable")
    result = from_impresario_report(
        _report(needs_human=[_LOOP], bundles=[bad], attention=True)
    )
    assert result.status.state == "partial"
    assert "pilot/pp-9" in (result.status.detail or "")
    assert [w.key for w in result.waits] == ["impresario-loop:L1:3"]


@pytest.mark.parametrize(
    ("code", "state"),
    [
        # SnapshotService lists every collector; an unobserved impresario
        # arrives as this diagnostic, not as a lookup error (spec §3.3).
        ("mirror-not-detected", "not_configured"),
        # Detected, then its anchors vanished before the scan.
        ("mirror-anchors-missing", "unavailable"),
    ],
)
def test_unscanned_mirror(code: str, state: str) -> None:
    result = from_impresario_report(
        _report(diagnostics=[Diagnostic(code=code, message="m")], attention=True)
    )
    assert result.status.state == state
    assert result.waits == []


def test_null_title_or_agent_type_never_renders_as_none(tmp_path: Path) -> None:
    home = tmp_path / "mhome"
    db = make_maestro_run(home, _ACME, "01RUN", started_at="2026-09-01T00:00:00")
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO tasks VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("T-null", None, "needs_review", None, "2026-09-01T00:00:00", None, None),
        )
        conn.commit()
    finally:
        conn.close()
    result = from_maestro(home, [])
    assert [w.ref for w in result.waits] == ["01RUN/T-null"]
    assert "None" not in result.waits[0].title


# -- forge (A2) --------------------------------------------------------------


_LABEL = "human-merge-required"


def _pr(number: int, **extra: object) -> dict[str, object]:
    return {
        "repo": "acme/widget",
        "number": number,
        "title": f"pr {number}",
        "url": f"https://github.com/acme/widget/pull/{number}",
        "head_sha": "a" * 40,
        "head_ref": "feat/x",
        "labeled_at": "2026-09-21T08:30:00Z",
        **extra,
    }


def _search(prs: list[dict[str, object]] | None, *, ok: bool = True) -> ActionOutcome:
    return ActionOutcome(
        action="pr-search",
        dir="dispatcher",
        ok=ok,
        prs=prs,
        error=None if ok else "gh search prs failed",
        phase="readable_result",
    )


def test_labelled_prs_become_human_merge_waits() -> None:
    result = from_pr_search(
        _search([_pr(7), _pr(3, labeled_at=None, head_sha=None)]), _LABEL
    )
    assert result.name == FORGE_SOURCE
    assert result.status.state == "ok"
    by_key = {w.key: w for w in result.waits}
    seven = by_key["pr:acme/widget#7"]
    assert seven.reasons == ["pr_human_merge"]
    assert seven.since == "2026-09-21T08:30:00+00:00"
    assert seven.since_basis == f"label {_LABEL} added"
    assert seven.act.model_dump() == {
        "kind": "human_merge",
        "repo": "acme/widget",
        "number": 7,
        "url": "https://github.com/acme/widget/pull/7",
        "head_sha": "a" * 40,
    }
    three = by_key["pr:acme/widget#3"]
    assert (three.since, three.since_basis) == (None, None)
    assert three.act.model_dump()["head_sha"] is None  # never an invented pin


def test_an_empty_search_is_a_confirmed_empty_source() -> None:
    result = from_pr_search(_search([]), _LABEL)
    assert result.status.state == "ok"
    assert result.waits == []


def test_an_unread_search_is_unavailable_not_empty() -> None:
    result = from_pr_search(_search(None, ok=False), _LABEL)
    assert result.status.state == "unavailable"
    assert "gh search prs failed" in (result.status.detail or "")
    assert result.waits == []


def _sync(fn):
    """A spawner that runs the refresh inline — deterministic tests."""
    fn()


def test_the_forge_reader_caches_within_the_ttl() -> None:
    calls: list[str] = []
    now = [100.0]

    def search(label: str) -> ActionOutcome:
        calls.append(label)
        return _search([_pr(len(calls))])

    reader = ForgeReader(search, _LABEL, ttl=60.0, clock=lambda: now[0], spawn=_sync)
    first = reader.read()
    assert [w.key for w in first.waits] == ["pr:acme/widget#1"]
    now[0] = 159.0
    assert reader.read() is first
    assert calls == [_LABEL]
    now[0] = 160.0
    assert [w.key for w in reader.read().waits] == ["pr:acme/widget#2"]
    assert calls == [_LABEL, _LABEL]


def test_read_never_waits_and_never_doubles_a_refresh() -> None:
    """The request path returns at once; the next poll does not start a
    second search while the first is still running (review on #280)."""
    pending: list = []
    reader = ForgeReader(
        lambda label: _search([_pr(1)]),
        _LABEL,
        ttl=60.0,
        clock=lambda: 0.0,
        spawn=pending.append,  # refreshes queue up instead of running
    )
    first = reader.read()
    assert first.status.state == "unavailable"
    assert "in progress" in (first.status.detail or "")
    reader.read()
    assert len(pending) == 1  # still in flight: no second search
    pending.pop()()  # the background search finishes
    assert [w.key for w in reader.read().waits] == ["pr:acme/widget#1"]


def test_a_stale_result_is_served_while_it_refreshes() -> None:
    pending: list = []
    now = [0.0]
    reader = ForgeReader(
        lambda label: _search([_pr(int(now[0]) + 1)]),
        _LABEL,
        ttl=60.0,
        clock=lambda: now[0],
        spawn=pending.append,
    )
    reader.read()
    pending.pop()()
    now[0] = 61.0
    stale = reader.read()  # stale: refresh starts, old answer served
    assert [w.key for w in stale.waits] == ["pr:acme/widget#1"]
    assert len(pending) == 1


def test_a_raising_search_is_unavailable_and_cached() -> None:
    calls: list[str] = []

    def search(label: str) -> ActionOutcome:
        calls.append(label)
        raise RuntimeError("github down")

    reader = ForgeReader(search, _LABEL, ttl=60.0, clock=lambda: 0.0, spawn=_sync)
    result = reader.read()
    assert result.status.state == "unavailable"
    assert "github down" in (result.status.detail or "")
    reader.read()
    assert calls == [_LABEL]  # an outage is not re-searched on every poll


def test_an_ok_false_answer_with_a_list_is_not_believed() -> None:
    result = from_pr_search(_search([_pr(1)], ok=False), _LABEL)
    assert result.status.state == "unavailable"
    assert result.waits == []


def test_a_maestro_verb_carries_the_servers_maestro_environment(
    tmp_path: Path,
) -> None:
    """Review on #274: the prepared command must run against the home the
    wait was read from, with the catalog `retry` needs — the same env
    run_controller pins for its own verbs."""
    home = tmp_path / "mhome"
    db = make_maestro_run(home, _ACME, "01RUN", started_at="2026-09-01T00:00:00")
    _add_task(db, "T-1", "needs_review", "2026-09-01T00:00:00")
    catalog = tmp_path / "catalog.toml"
    cli = tmp_path / "bin" / "maestro"
    [wait] = from_maestro(home, [], atp_catalog=catalog, maestro_cli=cli).waits
    act = wait.act.model_dump()
    assert act["maestro_home"] == str(home)
    assert act["atp_catalog"] == str(catalog)
    assert act["maestro_cli"] == str(cli)
