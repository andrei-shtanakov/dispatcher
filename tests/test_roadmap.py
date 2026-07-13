"""Tests for the roadmap read-model (typed evidence rules + API)."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from conftest import make_arbiter, make_atp, make_maestro, make_maestro_home

from dispatcher.core.contracts import check_contracts
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.models import ContractStatus, ProjectSnapshot, TaskInfo
from dispatcher.core.roadmap import (
    build_blockers,
    build_drift,
    build_phases,
    build_roadmap,
    contract_sync_by_name,
    default_roadmap_dirs,
)
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio

_ROADMAP = """
version: 1
roadmap: test-v1
title: Test roadmap
items:
  - id: RD-A
    title: Implemented and verified item
    phase: "1"
    owner_project: arbiter
    evidence_rules:
      - rule: project_detected
        kind: implementation
        project: arbiter
      - rule: work_item_chain
        kind: verification
        work_item_id: T-9
        min_links: 2

  - id: RD-B
    title: Planned item (evidence missing)
    phase: "2"
    owner_project: arbiter
    evidence_rules:
      - rule: file_exists
        kind: implementation
        project: arbiter
        path: contracts/nope/schema.json

  - id: RD-C
    title: Blocked item (planned + unfinished dependency)
    phase: "3"
    depends_on: [RD-B]
    evidence_rules:
      - rule: file_exists
        kind: implementation
        project: arbiter
        path: contracts/also-nope.json

  - id: RD-D
    title: No machine rules yet
    phase: "9"
    expected_evidence:
      - prose only
    evidence_rules: []

  - id: RD-E
    title: Unknown rule name stays failed, not crashed
    phase: "9"
    evidence_rules:
      - rule: teleport_check
        kind: implementation

  - id: RD-F
    title: Contract-tracked item
    phase: "4"
    owner_project: arbiter
    target_contract: agents-catalog
    evidence_rules:
      - rule: project_detected
        kind: implementation
        project: arbiter
"""


def _snapshots() -> list[ProjectSnapshot]:
    def task(task_id: str, status: str) -> TaskInfo:
        return TaskInfo(task_id=task_id, status=status, source="test")

    return [
        ProjectSnapshot(name="arbiter", path="/tmp/x", tasks=[task("T-9", "assign")]),
        ProjectSnapshot(name="Maestro", path="", tasks=[task("T-9", "done")]),
    ]


def _write_roadmap(tmp_path: Path) -> Path:
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "test-v1.yaml").write_text(_ROADMAP)
    return d


def test_status_ladder(tmp_path: Path) -> None:
    result = build_roadmap((_write_roadmap(tmp_path),), _snapshots())
    status = {i.id: i.computed_status for i in result.items}
    assert status == {
        "RD-A": "verified",
        "RD-B": "planned",
        "RD-C": "blocked",
        "RD-D": "unknown",
        "RD-E": "planned",
        # target_contract set but canon missing → not comparable → unchanged
        "RD-F": "implemented",
    }
    blocked = next(i for i in result.items if i.id == "RD-C")
    assert blocked.blockers == ["RD-B"]
    unknown_rule = next(i for i in result.items if i.id == "RD-E")
    assert "unknown rule" in unknown_rule.evidence[0].detail


def test_missing_dir_warns(tmp_path: Path) -> None:
    result = build_roadmap((tmp_path / "absent",), _snapshots())
    assert result.items == []
    assert any("no roadmap directory" in w for w in result.warnings)


def test_duplicate_ids_warn(tmp_path: Path) -> None:
    d = tmp_path / "roadmaps"
    d.mkdir()
    dup = "items:\n  - id: RD-X\n    title: a\n  - id: RD-X\n    title: b\n"
    (d / "dup.yaml").write_text(dup)
    result = build_roadmap((d,), _snapshots())
    assert len(result.items) == 1
    assert any("duplicate" in w for w in result.warnings)


def test_path_traversal_rejected(tmp_path: Path) -> None:
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "evil.yaml").write_text(
        "items:\n"
        "  - id: RD-EVIL\n"
        "    title: traversal\n"
        "    evidence_rules:\n"
        "      - rule: file_exists\n"
        "        project: arbiter\n"
        "        path: /etc/passwd\n"
        "      - rule: file_exists\n"
        "        project: arbiter\n"
        "        path: ../../etc/passwd\n"
        "      - rule: sqlite_has_row\n"
        "        project: arbiter\n"
        "        db: ../outside.db\n"
        "        query: SELECT 1\n"
    )
    snaps = [ProjectSnapshot(name="arbiter", path=str(tmp_path / "arbiter"))]
    (tmp_path / "arbiter").mkdir()
    item = build_roadmap((d,), snaps).items[0]
    assert all(not e.passed for e in item.evidence)
    assert all("escapes project root" in e.detail for e in item.evidence)


def test_malformed_rule_degrades_not_crashes(tmp_path: Path) -> None:
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "bad.yaml").write_text(
        "items:\n"
        "  - id: RD-BAD\n"
        "    title: bad min_links\n"
        "    evidence_rules:\n"
        "      - rule: work_item_chain\n"
        "        work_item_id: T-9\n"
        "        min_links: not-a-number\n"
    )
    item = build_roadmap((d,), _snapshots()).items[0]
    assert item.computed_status == "planned"
    assert "rule error" in item.evidence[0].detail


def test_roadmap_names_dedup_and_null(tmp_path: Path) -> None:
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "a.yaml").write_text("roadmap: null\nitems: []\n")
    (d / "b.yaml").write_text("roadmap: same\nitems: []\n")
    (d / "c.yaml").write_text("roadmap: same\nitems: []\n")
    result = build_roadmap((d,), [])
    assert result.roadmaps == ["a", "same"]


def test_default_roadmap_dirs() -> None:
    dirs = default_roadmap_dirs((Path("/r1"), Path("/r2")))
    assert dirs == (
        Path("/r1/prograph-vault/authored/roadmaps"),
        Path("/r2/prograph-vault/authored/roadmaps"),
    )


def test_vault_file_rules_resolve_from_roadmap_dirs(tmp_path: Path) -> None:
    """`prograph-vault` file rules resolve via the roadmap dirs (RD-000)."""
    d = tmp_path / "prograph-vault" / "authored" / "roadmaps"
    d.mkdir(parents=True)
    (tmp_path / "prograph-vault" / "authored" / "rules").mkdir()
    (tmp_path / "prograph-vault" / "authored" / "rules" / "checklist.md").write_text(
        "# rule\n"
    )
    (d / "kb.yaml").write_text(
        "items:\n"
        "  - id: RD-KB\n"
        "    title: authored rule exists\n"
        "    evidence_rules:\n"
        "      - rule: file_exists\n"
        "        kind: implementation\n"
        "        project: prograph-vault\n"
        "        path: authored/rules/checklist.md\n"
    )
    result = build_roadmap((d,), [])
    assert result.items[0].computed_status == "implemented"


def test_dispatcher_self_evidence(tmp_path: Path) -> None:
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "self.yaml").write_text(
        "items:\n"
        "  - id: RD-SELF\n"
        "    title: dashboard attests itself\n"
        "    evidence_rules:\n"
        "      - rule: file_exists\n"
        "        kind: implementation\n"
        "        project: dispatcher\n"
        "        path: dispatcher/core/roadmap.py\n"
    )
    result = build_roadmap((d,), [])
    assert result.items[0].computed_status == "implemented"


_FRESHNESS_ROADMAP = """
items:
  - id: RD-FRESH
    title: Freshness over file and db evidence
    evidence_rules:
      - rule: file_exists
        kind: implementation
        project: arbiter
        path: artifact.txt
      - rule: sqlite_has_row
        kind: implementation
        project: arbiter
        db: state.db
        query: SELECT 1 FROM t
      - rule: sqlite_has_row
        kind: implementation
        project: arbiter
        db: state.db
        query: SELECT 1 FROM t WHERE id = 'absent'
      - rule: project_detected
        kind: implementation
        project: arbiter
      - rule: file_exists
        kind: verification
        project: arbiter
        path: missing.txt
"""


def _iso_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


def test_last_seen_freshness(tmp_path: Path) -> None:
    """`last_seen` = mtime of the matched artifact; None where n/a (REQ-011)."""
    proj = tmp_path / "arbiter"
    proj.mkdir()
    artifact = proj / "artifact.txt"
    artifact.write_text("x")
    db = proj / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE t (id TEXT)")
        conn.execute("INSERT INTO t VALUES ('a')")
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "fresh.yaml").write_text(_FRESHNESS_ROADMAP)
    snaps = [ProjectSnapshot(name="arbiter", path=str(proj))]
    item = build_roadmap((d,), snaps).items[0]
    file_ev, db_ev, no_row_ev, detected_ev, missing_ev = item.evidence
    assert file_ev.passed and file_ev.last_seen == _iso_mtime(artifact)
    assert db_ev.passed and db_ev.last_seen == _iso_mtime(db)
    # stamps are tz-aware UTC, never naive/local (guards string ordering)
    assert file_ev.last_seen is not None and file_ev.last_seen.endswith("+00:00")
    # no matched artifact → no observation timestamp
    assert not no_row_ev.passed and no_row_ev.last_seen is None
    assert detected_ev.passed and detected_ev.last_seen is None
    assert not missing_ev.passed and missing_ev.last_seen is None
    # item-level freshness is the newest evidence stamp
    assert item.last_seen == max(_iso_mtime(artifact), _iso_mtime(db))


def test_last_seen_none_without_file_evidence(tmp_path: Path) -> None:
    result = build_roadmap((_write_roadmap(tmp_path),), _snapshots())
    by_id = {i.id: i for i in result.items}
    assert by_id["RD-A"].last_seen is None  # project_detected + chain rules
    assert by_id["RD-B"].last_seen is None  # file_exists failed
    assert by_id["RD-D"].last_seen is None  # no rules


_CONTRACT_RULE_ROADMAP = """
items:
  - id: RD-CR
    title: Contract-in-sync evidence rule
    evidence_rules:
      - rule: contract_in_sync
        kind: verification
        name: agents-catalog
"""


def test_contract_in_sync_rule_carries_no_last_seen(tmp_path: Path) -> None:
    """The `contract_in_sync` rule attests a comparison, not an artifact,
    so it never populates `last_seen` (REQ-011)."""
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "cr.yaml").write_text(_CONTRACT_RULE_ROADMAP)
    in_sync = [ContractStatus(name="agents-catalog", canonical_path="c", in_sync=True)]
    item = build_roadmap((d,), [], in_sync).items[0]
    ev = item.evidence[0]
    assert ev.rule == "contract_in_sync"
    assert ev.passed and ev.detail == "contract agents-catalog in sync"
    assert ev.last_seen is None
    assert item.last_seen is None


_DRIFT_ROADMAP = """
items:
  - id: RD-TC
    title: Tracks the agents catalog contract
    target_contract: agents-catalog
    evidence_rules:
      - rule: project_detected
        kind: implementation
        project: arbiter

  - id: RD-NC
    title: No contract, same rules
    evidence_rules:
      - rule: project_detected
        kind: implementation
        project: arbiter

  - id: RD-UC
    title: Unknown contract, no rules
    target_contract: no-such-contract
    evidence_rules: []

  - id: RD-BK
    title: Blocked dependency and drifted contract
    target_contract: agents-catalog
    depends_on: [RD-UC]
    evidence_rules:
      - rule: project_detected
        kind: implementation
        project: no-such-project
"""


def _write_drift_roadmap(tmp_path: Path) -> Path:
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "drift.yaml").write_text(_DRIFT_ROADMAP)
    return d


def _contract_snapshots(tmp_path: Path) -> list[ProjectSnapshot]:
    """Real canon + vendored trees; fixture copies differ (drifted)."""
    atp = make_atp(tmp_path)
    arb = make_arbiter(tmp_path)
    return [
        ProjectSnapshot(name="atp-platform", path=str(atp)),
        ProjectSnapshot(name="arbiter", path=str(arb)),
    ]


def test_drift_when_contract_out_of_sync(tmp_path: Path) -> None:
    snaps = _contract_snapshots(tmp_path)
    result = build_roadmap((_write_drift_roadmap(tmp_path),), snaps)
    status = {i.id: i.computed_status for i in result.items}
    assert status["RD-TC"] == "drift"
    # regression: no target_contract → MVP statuses unaffected
    assert status["RD-NC"] == "implemented"
    # contract unknown to the checker → status stays unknown
    assert status["RD-UC"] == "unknown"
    # drift is projected after blocked and wins; blockers stay listed
    assert status["RD-BK"] == "drift"
    blocked = next(i for i in result.items if i.id == "RD-BK")
    assert blocked.blockers == ["RD-UC"]


def test_no_drift_when_contract_in_sync(tmp_path: Path) -> None:
    snaps = _contract_snapshots(tmp_path)
    canon = (tmp_path / "atp-platform" / "method" / "agents-catalog.toml").read_text()
    (tmp_path / "arbiter" / "config" / "agents-catalog.toml").write_text(canon)
    result = build_roadmap((_write_drift_roadmap(tmp_path),), snaps)
    status = {i.id: i.computed_status for i in result.items}
    assert status["RD-TC"] == "implemented"
    assert status["RD-BK"] == "blocked"  # no drift → blocked survives


def test_no_drift_when_contract_not_comparable(tmp_path: Path) -> None:
    """Canon missing → in_sync is None → status unchanged (stays honest)."""
    arb = make_arbiter(tmp_path)
    snaps = [ProjectSnapshot(name="arbiter", path=str(arb))]
    result = build_roadmap((_write_drift_roadmap(tmp_path),), snaps)
    status = {i.id: i.computed_status for i in result.items}
    assert status["RD-TC"] == "implemented"


def test_build_drift_join(tmp_path: Path) -> None:
    snaps = _contract_snapshots(tmp_path)
    roadmap_dir = _write_drift_roadmap(tmp_path)
    (roadmap_dir / "zz-bad.yaml").write_text("- not a mapping\n")
    roadmap = build_roadmap((roadmap_dir,), snaps)
    contracts = check_contracts(
        {s.name: Path(s.path) for s in snaps if s.detected and s.path}
    )
    drift = build_drift(roadmap, contracts)
    entries = {e.id: e for e in drift.items}
    # only items with a target_contract are part of the view
    assert set(entries) == {"RD-TC", "RD-UC", "RD-BK"}
    assert entries["RD-TC"].contract_in_sync is False
    assert entries["RD-TC"].computed_status == "drift"
    assert entries["RD-TC"].contract_detail is None  # hash mismatch: no detail
    assert entries["RD-UC"].contract_in_sync is None
    assert entries["RD-UC"].contract_detail == "contract not checked"
    # roadmap warnings propagate through the drift view
    assert any("zz-bad.yaml" in w for w in drift.warnings)


def test_build_roadmap_uses_provided_contracts(tmp_path: Path) -> None:
    """Injected checker results are reused — no second checker run."""
    snaps = _contract_snapshots(tmp_path)  # real vendored copy IS drifted
    in_sync = [ContractStatus(name="agents-catalog", canonical_path="c", in_sync=True)]
    roadmap = build_roadmap((_write_drift_roadmap(tmp_path),), snaps, in_sync)
    status = {i.id: i.computed_status for i in roadmap.items}
    assert status["RD-TC"] == "implemented"


def test_contract_sync_fold_any_drifted_copy_wins(tmp_path: Path) -> None:
    """The checker emits one row per vendored copy, all same-named: one
    drifted copy drifts the contract regardless of row order, and a
    not-comparable copy blocks an in-sync verdict."""

    def row(in_sync: bool | None) -> ContractStatus:
        return ContractStatus(
            name="agents-catalog", canonical_path="c", in_sync=in_sync
        )

    assert contract_sync_by_name([row(False), row(True)]) == {"agents-catalog": False}
    assert contract_sync_by_name([row(True), row(None)]) == {"agents-catalog": None}
    snaps = _contract_snapshots(tmp_path)
    contracts = [row(True), row(False)]
    roadmap = build_roadmap((_write_drift_roadmap(tmp_path),), snaps, contracts)
    assert {i.id: i.computed_status for i in roadmap.items}["RD-TC"] == "drift"
    drift = build_drift(roadmap, contracts)
    entry = next(e for e in drift.items if e.id == "RD-TC")
    assert entry.contract_in_sync is False


async def test_roadmap_endpoint(tmp_path: Path) -> None:
    make_atp(tmp_path)
    make_arbiter(tmp_path)
    make_maestro(tmp_path)
    db = make_maestro_home(tmp_path)
    with sqlite3.connect(db) as conn:
        # second T-9 link so the RD-A verification chain rule passes
        conn.execute(
            "INSERT INTO tasks VALUES ('T-9', 'Route me', 'done', 'auto', "
            "'2026-07-02T09:58:00', '2026-07-02T09:59:00', "
            "'2026-07-02T10:06:00')"
        )
    vault = tmp_path / "prograph-vault" / "authored" / "roadmaps"
    vault.mkdir(parents=True)
    (vault / "test-v1.yaml").write_text(_ROADMAP)
    config = DispatcherConfig(roots=(tmp_path,), maestro_db=db)
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        listing = await c.get("/api/roadmap")
        one = await c.get("/api/roadmap/RD-A")
        missing = await c.get("/api/roadmap/RD-ZZZ")
        drift = await c.get("/api/roadmap/drift")
    assert listing.status_code == 200
    data = listing.json()
    assert data["roadmaps"] == ["test-v1"]
    assert {i["id"] for i in data["items"]} == {
        "RD-A",
        "RD-B",
        "RD-C",
        "RD-D",
        "RD-E",
        "RD-F",
    }
    # freshness is part of the payload for every item and rule (REQ-011)
    assert all("last_seen" in i for i in data["items"])
    assert all("last_seen" in e for i in data["items"] for e in i["evidence"])
    assert one.status_code == 200
    assert one.json()["computed_status"] == "verified"
    assert missing.status_code == 404
    # drift route is not shadowed by /{item_id} and joins contract state
    assert drift.status_code == 200
    entries = drift.json()["items"]
    assert [e["id"] for e in entries] == ["RD-F"]
    assert entries[0]["target_contract"] == "agents-catalog"
    assert entries[0]["contract_in_sync"] is False  # fixture copies differ
    assert entries[0]["computed_status"] == "drift"


# --- TASK-104: phase / blocker aggregations ---------------------------------

_GRAPH_ROADMAP = """
items:
  - id: RD-1
    title: Done root
    phase: P1
    owner_project: arbiter
    evidence_rules:
      - rule: project_detected
        kind: implementation
        project: arbiter

  - id: RD-2
    title: Depends on done root and a ghost id
    phase: P1
    depends_on: [RD-1, RD-GHOST]
    evidence_rules:
      - rule: file_exists
        kind: implementation
        project: arbiter
        path: nope.json

  - id: RD-5
    title: Also depends on the done root
    phase: P1
    depends_on: [RD-1]

  - id: RD-3
    title: Cycle a
    phase: P2
    depends_on: [RD-4]

  - id: RD-4
    title: Cycle b
    phase: P2
    depends_on: [RD-3]
"""


def _write_graph_roadmap(tmp_path: Path) -> Path:
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "graph.yaml").write_text(_GRAPH_ROADMAP)
    return d


def test_build_phases_counts_and_blocked(tmp_path: Path) -> None:
    roadmap = build_roadmap((_write_roadmap(tmp_path),), _snapshots())
    phases = {p.phase: p for p in build_phases(roadmap).phases}
    # phases follow the (phase, id) ordering of build_roadmap
    assert [p.phase for p in build_phases(roadmap).phases] == ["1", "2", "3", "4", "9"]
    assert phases["1"].counts == {"verified": 1}
    assert phases["3"].counts == {"blocked": 1}
    assert phases["3"].blocked == ["RD-C"]  # only blocked ids listed
    assert phases["1"].blocked == []
    # phase "9" holds two items of different status
    assert phases["9"].total == 2
    assert phases["9"].counts == {"unknown": 1, "planned": 1}


def test_build_phases_empty_roadmap(tmp_path: Path) -> None:
    roadmap = build_roadmap((tmp_path / "absent",), _snapshots())
    result = build_phases(roadmap)
    assert result.phases == []
    assert any("no roadmap directory" in w for w in result.warnings)


def test_build_phases_lists_multiple_blocked_ids_in_order(tmp_path: Path) -> None:
    """A phase's `blocked` aggregates every blocked id in (phase, id) order."""
    d = tmp_path / "roadmaps"
    d.mkdir()
    # Two planned items (failing impl rule) with an unfinished dep => blocked;
    # written out of id order to prove the output follows build_roadmap's sort.
    item = (
        "  - id: {id}\n"
        "    title: {id}\n"
        "    phase: PB\n"
        "    depends_on: [RD-MISSING]\n"
        "    evidence_rules:\n"
        "      - rule: file_exists\n"
        "        kind: implementation\n"
        "        project: arbiter\n"
        "        path: nope.json\n"
    )
    (d / "blk.yaml").write_text(
        "items:\n" + item.format(id="RD-Z") + item.format(id="RD-Y")
    )
    phase = build_phases(build_roadmap((d,), _snapshots())).phases[0]
    assert phase.phase == "PB"
    assert phase.counts == {"blocked": 2}
    assert phase.blocked == ["RD-Y", "RD-Z"]  # sorted by id, not file order


def test_build_phases_groups_none_phase(tmp_path: Path) -> None:
    d = tmp_path / "roadmaps"
    d.mkdir()
    (d / "np.yaml").write_text(
        "items:\n  - id: RD-NOP\n    title: no phase\n    evidence_rules: []\n"
    )
    result = build_phases(build_roadmap((d,), []))
    assert result.phases[0].phase is None
    assert result.phases[0].counts == {"unknown": 1}


def test_build_blockers_reverse_view(tmp_path: Path) -> None:
    roadmap = build_roadmap((_write_graph_roadmap(tmp_path),), _snapshots())
    entries = {e.id: e for e in build_blockers(roadmap).items}
    # every depended-on id becomes an entry, sorted by id
    assert [e.id for e in build_blockers(roadmap).items] == [
        "RD-1",
        "RD-3",
        "RD-4",
        "RD-GHOST",
    ]
    # a resolved (implemented) dependency is not holding anyone back;
    # its `blocks` aggregates every dependent in (phase, id) order
    assert entries["RD-1"].blocks == ["RD-2", "RD-5"]
    assert entries["RD-1"].computed_status == "implemented"
    assert entries["RD-1"].unresolved is False
    # an id referenced but never defined: unknown, and a real blocker
    ghost = entries["RD-GHOST"]
    assert ghost.blocks == ["RD-2"]
    assert ghost.title is None
    assert ghost.computed_status is None
    assert ghost.unresolved is True


def test_build_blockers_handles_cyclic_depends_on(tmp_path: Path) -> None:
    """A dependency cycle inverts into mutual entries, never recursion."""
    roadmap = build_roadmap((_write_graph_roadmap(tmp_path),), _snapshots())
    entries = {e.id: e for e in build_blockers(roadmap).items}
    assert entries["RD-3"].blocks == ["RD-4"]
    assert entries["RD-4"].blocks == ["RD-3"]
    assert entries["RD-3"].unresolved is True
    assert entries["RD-4"].unresolved is True


def test_build_blockers_empty_roadmap(tmp_path: Path) -> None:
    result = build_blockers(build_roadmap((tmp_path / "absent",), _snapshots()))
    assert result.items == []
    assert any("no roadmap directory" in w for w in result.warnings)


async def test_aggregation_endpoints(tmp_path: Path) -> None:
    make_arbiter(tmp_path)
    vault = tmp_path / "prograph-vault" / "authored" / "roadmaps"
    vault.mkdir(parents=True)
    (vault / "graph.yaml").write_text(_GRAPH_ROADMAP)
    # Items whose ids literally collide with the aggregation routes: the
    # routes must win over /api/roadmap/{item_id}, never resolve to these.
    collide = (
        "  - id: {id}\n    title: collide {id}\n    phase: P1\n    evidence_rules: []\n"
    )
    (vault / "collide.yaml").write_text(
        "items:\n" + collide.format(id="phases") + collide.format(id="blockers")
    )
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        phases = await c.get("/api/roadmap/phases")
        blockers = await c.get("/api/roadmap/blockers")
        item = await c.get("/api/roadmap/RD-1")
    # aggregation shape wins, not the RoadmapItemView of the id "phases"
    assert phases.status_code == 200
    assert "phases" in phases.json() and "computed_status" not in phases.json()
    assert {p["phase"] for p in phases.json()["phases"]} == {"P1", "P2"}
    assert blockers.status_code == 200
    assert "items" in blockers.json() and "computed_status" not in blockers.json()
    ids = {e["id"] for e in blockers.json()["items"]}
    assert ids == {"RD-1", "RD-3", "RD-4", "RD-GHOST"}
    # the catch-all /{item_id} still resolves a genuine roadmap item id
    assert item.status_code == 200
    assert item.json()["id"] == "RD-1"
