"""Pure inventory classifier — Ready/blocked/unregistered/orphan (Task 4).

One test per §5.1 condition, in the spec's numbering. Every `CapturedInputs`
here is a pure-data literal — no filesystem, no git: capture (Task 3) is
what turns disk facts into these values, and this module never re-derives
them.
"""

from __future__ import annotations

from dispatcher.core.admission import (
    DAG_DIRTY,
    DAG_DUPLICATE,
    DAG_INVALID,
    NO_DAG_TAG,
    RUN_IN_FLIGHT,
    CapturedInputs,
    RunFact,
    classify_inventory,
)
from dispatcher.core.dag_subset import Accepted, Rejected
from dispatcher.core.inventory import DagFileInfo, InventorySurface, PlanItem
from dispatcher.core.run_identity import RepoKey

REPO_KEY = RepoKey(host="github.com", owner="andrei-shtanakov", repo="demo")
FOREIGN_KEY = RepoKey(host="github.com", owner="someone-else", repo="other")
HEAD = "a" * 40


def _item(
    item_id: str | None,
    line: int,
    *,
    open: bool = True,
    shipped: bool = False,
    dag_raw: str | None = None,
    dag_tag: str | None = None,
    dag_diag: str | None = None,
) -> PlanItem:
    return PlanItem(
        item_id=item_id,
        line=line,
        open=open,
        shipped=shipped,
        dag_raw=dag_raw,
        dag_tag=dag_tag,
        dag_diag=dag_diag,
    )


def _dag(
    rel_path: str,
    *,
    is_regular: bool = True,
    text: str | None = "repo: /x\ntasks: []\n",
    blob_sha: str | None = "sha-clean",
    head_blob_sha: str | None = "sha-clean",
    subset: Accepted | Rejected | None = Accepted(repo_path="/x", repo_url=None),
    named_repo: RepoKey | None = REPO_KEY,
    named_repo_error: str | None = None,
    error: str | None = None,
) -> DagFileInfo:
    return DagFileInfo(
        rel_path=rel_path,
        is_regular=is_regular,
        text=text,
        blob_sha=blob_sha,
        head_blob_sha=head_blob_sha,
        subset=subset,
        named_repo=named_repo,
        named_repo_error=named_repo_error,
        error=error,
    )


def _inventory(
    plan_items: tuple[PlanItem, ...] = (),
    dag_files: tuple[DagFileInfo, ...] = (),
    *,
    head_revision: str | None = HEAD,
    repo_key: RepoKey | None = REPO_KEY,
    plan_error: str | None = None,
    dag_dir_error: str | None = None,
    capture_error: str | None = None,
) -> InventorySurface:
    return InventorySurface(
        plan_items=plan_items,
        dag_files=dag_files,
        head_revision=head_revision,
        repo_key=repo_key,
        plan_error=plan_error,
        dag_dir_error=dag_dir_error,
        capture_error=capture_error,
    )


def _captured(
    inv: InventorySurface,
    *,
    lock=None,
    lock_error: str | None = None,
    runs: tuple[RunFact, ...] = (),
    runs_unreadable: tuple[str, ...] = (),
) -> CapturedInputs:
    return CapturedInputs(
        inventory=inv,
        lock=lock,
        lock_error=lock_error,
        runs=runs,
        runs_unreadable=runs_unreadable,
    )


# --- 1. all eight conditions hold → ready -------------------------------


def test_rule01_all_conditions_hold_is_ready():
    item = _item("demo", 3, dag_raw="dags/demo.yaml", dag_tag="dags/demo.yaml")
    dag = _dag("dags/demo.yaml")
    decision = classify_inventory(_captured(_inventory((item,), (dag,))))

    assert decision.unreadable is None
    assert decision.repo.admission == "ready"
    assert [d.work_id for d in decision.ready] == ["demo"]
    assert decision.ready[0].dag_path == "dags/demo.yaml"
    assert decision.ready[0].reason_code is None
    assert decision.blocked == ()
    assert decision.unregistered_items == ()


# --- 2. closed item / id-less open item appear in no list --------------


def test_rule02_closed_and_idless_items_appear_in_no_list():
    closed = _item(
        "closed1",
        1,
        open=False,
        dag_raw="dags/closed1.yaml",
        dag_tag="dags/closed1.yaml",
    )
    idless = _item(None, 2, open=True)
    decision = classify_inventory(_captured(_inventory((closed, idless), ())))

    assert decision.ready == ()
    assert decision.blocked == ()
    assert decision.unregistered_items == ()


# --- 3. open item, @id, no @dag → unregistered --------------------------


def test_rule03_no_dag_tag_is_unregistered():
    item = _item("a", 1)
    decision = classify_inventory(_captured(_inventory((item,), ())))

    assert decision.ready == () and decision.blocked == ()
    assert len(decision.unregistered_items) == 1
    got = decision.unregistered_items[0]
    assert got.work_id == "a"
    assert got.reason_code == NO_DAG_TAG == "no_dag_tag"


# --- 4. @dag written but broken (dag_diag set) → dag_invalid -----------


def test_rule04_broken_dag_tag_is_dag_invalid_never_unregistered():
    item = _item(
        "a", 1, dag_raw="dags/wrong.yaml", dag_tag=None, dag_diag="PF-DAG-MISMATCH"
    )
    decision = classify_inventory(_captured(_inventory((item,), ())))

    assert decision.unregistered_items == ()
    assert len(decision.blocked) == 1
    got = decision.blocked[0]
    assert got.reason_code == DAG_INVALID
    assert "PF-DAG-MISMATCH" in got.reason


# --- 5. duplication, ledger-wide -----------------------------------------


def test_rule05_two_open_claimants_both_blocked_naming_each_other():
    item_a = _item("a", 1, dag_raw="dags/shared.yaml", dag_tag="dags/shared.yaml")
    item_b = _item("b", 5, dag_raw="dags/shared.yaml", dag_tag="dags/shared.yaml")
    dag = _dag("dags/shared.yaml")
    decision = classify_inventory(_captured(_inventory((item_a, item_b), (dag,))))

    assert decision.ready == ()
    assert len(decision.blocked) == 2
    by_id = {d.work_id: d for d in decision.blocked}
    assert by_id["a"].reason_code == DAG_DUPLICATE
    assert "5" in by_id["a"].reason
    assert by_id["b"].reason_code == DAG_DUPLICATE
    assert "1" in by_id["b"].reason


def test_rule05_open_and_shipped_claimant_only_open_blocks():
    open_item = _item("a", 1, dag_raw="dags/shared.yaml", dag_tag="dags/shared.yaml")
    shipped_item = _item(
        "s",
        20,
        open=False,
        shipped=True,
        dag_raw="dags/shared.yaml",
        dag_tag="dags/shared.yaml",
    )
    dag = _dag("dags/shared.yaml")
    decision = classify_inventory(
        _captured(_inventory((open_item, shipped_item), (dag,)))
    )

    assert decision.ready == ()
    assert len(decision.blocked) == 1
    got = decision.blocked[0]
    assert got.work_id == "a"
    assert got.reason_code == DAG_DUPLICATE
    assert "20" in got.reason
    assert decision.unregistered_items == ()  # the shipped item is in no list


# --- 6. dag_tag names a path absent from dag_files -----------------------


def test_rule06_registered_but_absent_is_dag_invalid():
    item = _item("a", 1, dag_raw="dags/gone.yaml", dag_tag="dags/gone.yaml")
    decision = classify_inventory(_captured(_inventory((item,), ())))

    assert len(decision.blocked) == 1
    assert decision.blocked[0].reason_code == DAG_INVALID


# --- 7. is_regular=False → dag_invalid -----------------------------------


def test_rule07_irregular_file_is_dag_invalid():
    item = _item("a", 1, dag_raw="dags/a.yaml", dag_tag="dags/a.yaml")
    dag = _dag("dags/a.yaml", is_regular=False, text=None, error=None)
    decision = classify_inventory(_captured(_inventory((item,), (dag,))))

    assert len(decision.blocked) == 1
    assert decision.blocked[0].reason_code == DAG_INVALID


# --- 8. error set, or text is None → dag_invalid (fail-closed) ----------


def test_rule08_error_set_is_dag_invalid():
    item = _item("a", 1, dag_raw="dags/a.yaml", dag_tag="dags/a.yaml")
    dag = _dag("dags/a.yaml", error="git hash-object failed")
    decision = classify_inventory(_captured(_inventory((item,), (dag,))))

    assert len(decision.blocked) == 1
    assert decision.blocked[0].reason_code == DAG_INVALID


def test_rule08_undecodable_text_is_dag_invalid():
    item = _item("a", 1, dag_raw="dags/a.yaml", dag_tag="dags/a.yaml")
    dag = _dag("dags/a.yaml", text=None, subset=None, error=None)
    decision = classify_inventory(_captured(_inventory((item,), (dag,))))

    assert len(decision.blocked) == 1
    assert decision.blocked[0].reason_code == DAG_INVALID


# --- 9. subset Rejected → dag_invalid with Rejected.reason ---------------


def test_rule09_rejected_subset_carries_its_reason():
    item = _item("a", 1, dag_raw="dags/a.yaml", dag_tag="dags/a.yaml")
    dag = _dag("dags/a.yaml", subset=Rejected("top level is not a mapping"))
    decision = classify_inventory(_captured(_inventory((item,), (dag,))))

    assert len(decision.blocked) == 1
    got = decision.blocked[0]
    assert got.reason_code == DAG_INVALID
    assert got.reason == "top level is not a mapping"


# --- 10. named_repo mismatch / unresolved → dag_invalid, both keys ------


def test_rule10_foreign_named_repo_names_both_keys():
    item = _item("a", 1, dag_raw="dags/a.yaml", dag_tag="dags/a.yaml")
    dag = _dag("dags/a.yaml", named_repo=FOREIGN_KEY)
    decision = classify_inventory(_captured(_inventory((item,), (dag,))))

    assert len(decision.blocked) == 1
    got = decision.blocked[0]
    assert got.reason_code == DAG_INVALID
    assert FOREIGN_KEY.as_text() in got.reason
    assert REPO_KEY.as_text() in got.reason


def test_rule10_unresolved_named_repo_is_dag_invalid():
    item = _item("a", 1, dag_raw="dags/a.yaml", dag_tag="dags/a.yaml")
    dag = _dag(
        "dags/a.yaml",
        named_repo=None,
        named_repo_error="cannot parse remote URL",
    )
    decision = classify_inventory(_captured(_inventory((item,), (dag,))))

    assert len(decision.blocked) == 1
    got = decision.blocked[0]
    assert got.reason_code == DAG_INVALID
    assert "cannot parse remote URL" in got.reason


# --- 11. blob_sha != head_blob_sha (incl. clean absence) → dag_dirty ----


def test_rule11_blob_mismatch_is_dirty():
    item = _item("a", 1, dag_raw="dags/a.yaml", dag_tag="dags/a.yaml")
    dag = _dag("dags/a.yaml", blob_sha="sha-now", head_blob_sha="sha-head")
    decision = classify_inventory(_captured(_inventory((item,), (dag,))))

    assert len(decision.blocked) == 1
    assert decision.blocked[0].reason_code == DAG_DIRTY


def test_rule11_clean_absence_from_head_is_also_dirty():
    item = _item("a", 1, dag_raw="dags/a.yaml", dag_tag="dags/a.yaml")
    dag = _dag("dags/a.yaml", blob_sha="sha-now", head_blob_sha=None, error=None)
    decision = classify_inventory(_captured(_inventory((item,), (dag,))))

    assert len(decision.blocked) == 1
    assert decision.blocked[0].reason_code == DAG_DIRTY


def test_rule11_git_failure_is_dag_invalid_not_dirty():
    """A failure to gather facts is never reported as a content verdict."""
    item = _item("a", 1, dag_raw="dags/a.yaml", dag_tag="dags/a.yaml")
    dag = _dag(
        "dags/a.yaml",
        blob_sha="sha-now",
        head_blob_sha=None,
        error="cannot read dags/a.yaml at abc123: fatal",
    )
    decision = classify_inventory(_captured(_inventory((item,), (dag,))))

    assert len(decision.blocked) == 1
    assert decision.blocked[0].reason_code == DAG_INVALID


# --- 12. degraded capture → InventoryDecision(unreadable=…), all empty --


def test_rule12_plan_error_yields_unreadable_decision():
    item = _item("a", 1, dag_raw="dags/a.yaml", dag_tag="dags/a.yaml")
    dag = _dag("dags/a.yaml")
    inv = _inventory((item,), (dag,), plan_error="cannot read TODO.md")
    decision = classify_inventory(_captured(inv))

    assert decision.unreadable == "cannot read TODO.md"
    assert decision.ready == () and decision.blocked == ()
    assert decision.unregistered_items == () and decision.orphan_dags == ()
    assert decision.repo is not None  # repo is still computed


def test_rule12_dag_dir_error_yields_unreadable_decision():
    inv = _inventory((), (), dag_dir_error="cannot list dags/")
    decision = classify_inventory(_captured(inv))
    assert decision.unreadable == "cannot list dags/"
    assert decision.ready == decision.blocked == decision.unregistered_items == ()


def test_rule12_capture_error_yields_unreadable_decision():
    inv = _inventory((), (), capture_error="cannot read HEAD")
    decision = classify_inventory(_captured(inv))
    assert decision.unreadable == "cannot read HEAD"


def test_rule12_missing_repo_key_yields_unreadable_decision():
    inv = _inventory((), (), repo_key=None)
    decision = classify_inventory(_captured(inv))
    assert decision.unreadable is not None
    assert decision.ready == decision.blocked == decision.unregistered_items == ()


def test_rule12_missing_head_revision_yields_unreadable_decision():
    inv = _inventory((), (), head_revision=None)
    decision = classify_inventory(_captured(inv))
    assert decision.unreadable is not None


# --- 13. orphan dags: unclaimed, or claimed only by Shipped -------------


def test_rule13_unclaimed_and_shipped_only_claims_are_orphans():
    shipped = _item(
        "s",
        10,
        open=False,
        shipped=True,
        dag_raw="dags/shipped.yaml",
        dag_tag="dags/shipped.yaml",
    )
    dag_shipped_claim = _dag("dags/shipped.yaml")
    dag_unclaimed = _dag("dags/unclaimed.yaml")
    decision = classify_inventory(
        _captured(_inventory((shipped,), (dag_shipped_claim, dag_unclaimed)))
    )

    assert decision.orphan_dags == ("dags/shipped.yaml", "dags/unclaimed.yaml")


def test_rule13_dag_claimed_by_open_item_is_not_orphan():
    item = _item("a", 1, dag_raw="dags/a.yaml", dag_tag="dags/a.yaml")
    dag = _dag("dags/a.yaml")
    decision = classify_inventory(_captured(_inventory((item,), (dag,))))
    assert decision.orphan_dags == ()


# --- 14. repo surface blocked → items land in blocked with the repo's --
# --- first blocker code, never in ready ----------------------------------


def test_rule14_repo_blocked_blocks_otherwise_ready_items():
    item = _item("a", 1, dag_raw="dags/a.yaml", dag_tag="dags/a.yaml")
    dag = _dag("dags/a.yaml")
    runs = (
        RunFact(run_id="01A", status="running", request_id="rc-1", run_dir_exists=True),
    )
    decision = classify_inventory(_captured(_inventory((item,), (dag,)), runs=runs))

    assert decision.repo.admission == "blocked"
    first_code = decision.repo.blockers[0].code
    assert first_code == RUN_IN_FLIGHT
    assert decision.ready == ()
    assert len(decision.blocked) == 1
    assert decision.blocked[0].reason_code == first_code


def test_shipped_mismatch_claim_still_blocks_the_open_owner():
    """A Shipped line naming dags/a.yaml under ITS OWN wrong id still claims it.

    parse_dag yields dag_tag=None + PF-DAG-MISMATCH for that line, but spec
    §5.1(3) counts ANY ledger line naming the same DAG — the open owner of
    dags/a.yaml must be dag_duplicate, not ready.
    """
    key = RepoKey(host="github.com", owner="o", repo="r")
    dag = DagFileInfo(
        rel_path="dags/a.yaml",
        is_regular=True,
        text="repo: /x\ntasks: []\n",
        blob_sha="s1",
        head_blob_sha="s1",
        subset=Accepted(repo_path="/x", repo_url=None),
        named_repo=key,
        named_repo_error=None,
        error=None,
    )
    inv = InventorySurface(
        plan_items=(
            PlanItem(
                item_id="a",
                line=3,
                open=True,
                shipped=False,
                dag_raw="dags/a.yaml",
                dag_tag="dags/a.yaml",
                dag_diag=None,
            ),
            PlanItem(
                item_id="old",
                line=9,
                open=False,
                shipped=True,
                dag_raw="dags/a.yaml",
                dag_tag=None,
                dag_diag="PF-DAG-MISMATCH",
            ),
        ),
        dag_files=(dag,),
        head_revision="c" * 40,
        repo_key=key,
        plan_error=None,
        dag_dir_error=None,
        capture_error=None,
    )
    decision = classify_inventory(_captured(inv))
    assert decision.ready == ()
    (item,) = decision.blocked
    assert item.reason_code == DAG_DUPLICATE
    assert "line 9" in item.reason
