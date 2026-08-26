"""Seam test: disk → `capture_inventory` → `classify_inventory` (Task 5).

One real tmp git repo carries every launchpad category at once — ready,
unregistered, a dirty DAG, a duplicate claim (open + Shipped), an orphan
file, and a Mode-2 file registered by an open item — and the assertion is
the FULL `InventoryDecision` against a literal in one go. The repo surface
of `CapturedInputs` (lock/runs) is quiet by construction: B1's own tests
already cover that half, so this is deliberately a pure inventory seam.
This fixture is what PR-C's assembler will be built and regression-tested
against first.
"""

from __future__ import annotations

from dispatcher.core.admission import (
    DAG_DIRTY,
    DAG_DUPLICATE,
    DAG_INVALID,
    NO_DAG_TAG,
    CapturedInputs,
    InventoryDecision,
    ItemDecision,
    RepoAdmission,
    classify_inventory,
)
from dispatcher.core.inventory import capture_inventory
from tests.test_inventory_capture import make_repo

_REMOTE_URL = "git@github.com:andrei-shtanakov/demo.git"


def test_end_to_end_inventory_seam(tmp_path):
    root = make_repo(
        tmp_path,
        (
            "- [ ] Ready thing @id:ready1 @dag:dags/ready1.yaml\n"
            "- [ ] Unregistered thing @id:unreg1\n"
            "- [ ] Dirty thing @id:dirty1 @dag:dags/dirty1.yaml\n"
            "- [ ] Duplicate open thing @id:dup @dag:dags/dup.yaml\n"
            "- [ ] Mode-2 thing @id:mode2-1 @dag:dags/mode2-1.yaml\n"
            "## Shipped\n"
            "- [x] Duplicate shipped thing @id:dup @dag:dags/dup.yaml\n"
        ),
        {
            "dags/ready1.yaml": f"repo_url: {_REMOTE_URL}\ntasks: []\n",
            "dags/dirty1.yaml": f"repo_url: {_REMOTE_URL}\ntasks: []\n",
            "dags/dup.yaml": f"repo_url: {_REMOTE_URL}\ntasks: []\n",
            "dags/mode2-1.yaml": "workstreams: []\ntasks: []\n",
            "dags/orphan.yaml": f"repo_url: {_REMOTE_URL}\ntasks: []\n",
        },
    )
    # Dirty AFTER the commit — blob_sha now diverges from the captured HEAD blob.
    (root / "dags" / "dirty1.yaml").write_text(
        f"repo_url: {_REMOTE_URL}\ntasks: []\n# edited\n"
    )

    surface = capture_inventory(root)
    captured = CapturedInputs(
        inventory=surface,
        lock=None,
        lock_error=None,
        runs=(),
        runs_unreadable=(),
    )
    decision = classify_inventory(captured)

    assert decision == InventoryDecision(
        repo=RepoAdmission(admission="ready", blockers=()),
        ready=(
            ItemDecision(
                work_id="ready1",
                dag_path="dags/ready1.yaml",
                category="ready",
                reason_code=None,
                reason="",
            ),
        ),
        blocked=(
            ItemDecision(
                work_id="dirty1",
                dag_path="dags/dirty1.yaml",
                category="blocked",
                reason_code=DAG_DIRTY,
                reason=("dags/dirty1.yaml on disk differs from the captured HEAD blob"),
            ),
            ItemDecision(
                work_id="dup",
                dag_path="dags/dup.yaml",
                category="blocked",
                reason_code=DAG_DUPLICATE,
                reason="dags/dup.yaml is also claimed by line 7",
            ),
            ItemDecision(
                work_id="mode2-1",
                dag_path="dags/mode2-1.yaml",
                category="blocked",
                reason_code=DAG_INVALID,
                reason=(
                    "'workstreams:' present — a Mode-2 marker, "
                    "not the supported Mode-1 subset"
                ),
            ),
        ),
        unregistered_items=(
            ItemDecision(
                work_id="unreg1",
                dag_path=None,
                category="unregistered",
                reason_code=NO_DAG_TAG,
                reason="no @dag tag",
            ),
        ),
        orphan_dags=("dags/orphan.yaml",),
        unreadable=None,
    )
