"""Pure admission classification (spec §5, §7).

No IO by construction: every function consumes captured values. Both
adapters — the launchpad snapshot assembler (PR-C) and submit's gate
(this PR) — call these same functions, and the adapter-level property
test of spec §5 is what keeps a second implementation from growing.
"""

from __future__ import annotations

from dataclasses import dataclass

from plan_fields.parser import DAG_RE

from dispatcher.core.inventory_types import (
    Accepted,
    DagFileInfo,
    InventorySurface,
    PlanItem,
    Rejected,
)
from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_store import LockInfo, Malformed

# Codes — the single vocabulary shared by receipts now and 409s in PR-C.
LAUNCH_BUSY = "launch_busy"
RUN_IN_FLIGHT = "run_in_flight"
RUN_VANISHED = "run_vanished"
LOCK_MALFORMED = "lock_malformed"
LOCK_IO_UNREADABLE = "lock_io_unreadable"
RUN_STATE_UNREADABLE = "run_state_unreadable"
GUARD_BUSY = "guard_busy"

#: Fail-closed by SUBTRACTION (spec §7): terminal is the allowlist, and
#: any status outside it — today's, or one invented after this line was
#: written — blocks. An allowlist of blocking statuses would fail open
#: on the first new status maestro grows.
TERMINAL_RUN_STATUSES = frozenset({"completed", "cancelled", "superseded", "failed"})


@dataclass(frozen=True)
class RunFact:  # captured from classified_runs / launch_records
    run_id: str
    status: str
    request_id: str | None
    run_dir_exists: bool


@dataclass(frozen=True)
class Blocker:
    code: str
    request_id: str | None = None
    run_id: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class RepoAdmission:
    admission: str  # "ready" | "blocked"
    blockers: tuple[Blocker, ...]


def classify_repo(
    lock: LockInfo | Malformed | None,
    lock_error: str | None,  # an IO failure reading the lock
    runs: tuple[RunFact, ...],
    runs_unreadable: tuple[str, ...],  # unreadable state sources, by name
) -> RepoAdmission:
    """One repo's admission decision from already-captured facts.

    Every blocker that applies is collected — no hidden priority between
    a busy lock and an in-flight run, both surface (spec review).
    """
    blockers: list[Blocker] = []
    if lock_error is not None:
        blockers.append(Blocker(code=LOCK_IO_UNREADABLE, detail=lock_error))
    elif isinstance(lock, Malformed):
        blockers.append(Blocker(code=LOCK_MALFORMED, detail=lock.detail))
    elif isinstance(lock, LockInfo):
        blockers.append(Blocker(code=LAUNCH_BUSY, request_id=lock.request_id))
    for name in runs_unreadable:
        blockers.append(Blocker(code=RUN_STATE_UNREADABLE, detail=name))
    for run in runs:
        if run.status in TERMINAL_RUN_STATUSES:
            continue
        if run.request_id is not None and not run.run_dir_exists:
            blockers.append(
                Blocker(code=RUN_VANISHED, request_id=run.request_id, run_id=run.run_id)
            )
        else:
            blockers.append(
                Blocker(
                    code=RUN_IN_FLIGHT, request_id=run.request_id, run_id=run.run_id
                )
            )
    return RepoAdmission(
        admission="blocked" if blockers else "ready",
        blockers=tuple(blockers),
    )


# --- inventory classification (spec §4.1, §5.1) -------------------------

#: vocabulary shared with PR-C's submit re-validation (spec §4.2's
#: admission codes table) — not the launchpad list's own reason codes.
ITEM_CLOSED = "item_closed"
ITEM_UNREGISTERED = "item_unregistered"
DAG_INVALID = "dag_invalid"
DAG_DUPLICATE = "dag_duplicate"
DAG_DIRTY = "dag_dirty"
#: submit_v2's own code (spec §4.2, row d): the workspace's HEAD moved
#: past what the operator's `seen_revision` named — checked LAST among
#: the item decisions, never for an item that isn't there at all.
REVISION_MOVED = "revision_moved"

#: the launchpad `unregistered_items` list's own diagnostic (spec §4.1's
#: literal field value) — distinct from ITEM_UNREGISTERED above, which is
#: submit's 409 vocabulary, not this list's reason_code.
NO_DAG_TAG = "no_dag_tag"


@dataclass(frozen=True)
class CapturedInputs:
    """Spec §5's one input set: both surfaces, ONE capture generation.

    The adapter (PR-C's assembler; B2's seam test) fills every field from
    facts captured in one pass — mixing generations between the repo
    surface and the inventory surface is exactly what this type exists to
    make visible.
    """

    inventory: InventorySurface
    lock: LockInfo | Malformed | None
    lock_error: str | None
    runs: tuple[RunFact, ...]
    runs_unreadable: tuple[str, ...]


@dataclass(frozen=True)
class ItemDecision:
    work_id: str
    dag_path: str | None
    category: str  # "ready" | "blocked" | "unregistered"
    reason_code: str | None
    reason: str


@dataclass(frozen=True)
class InventoryDecision:
    repo: RepoAdmission
    ready: tuple[ItemDecision, ...]
    blocked: tuple[ItemDecision, ...]
    unregistered_items: tuple[ItemDecision, ...]
    orphan_dags: tuple[str, ...]  # rel paths, diagnostics only
    unreadable: str | None  # set ⇒ every list above is EMPTY


def _capture_broken(inv: InventorySurface) -> str | None:
    """The first reason captured facts cannot be trusted, if any (fail-closed)."""
    if inv.plan_error is not None:
        return inv.plan_error
    if inv.dag_dir_error is not None:
        return inv.dag_dir_error
    if inv.capture_error is not None:
        return inv.capture_error
    if inv.repo_key is None:
        return "repo identity unresolved"
    if inv.head_revision is None:
        return "HEAD unresolved"
    return None


def _file_unreadable_reason(dag_file: DagFileInfo | None, dag_path: str) -> str | None:
    """`None` when the file is a readable regular file; else why not."""
    if dag_file is None:
        return f"{dag_path} is registered but absent from dags/"
    if dag_file.error is not None:
        return dag_file.error
    if not dag_file.is_regular:
        return f"{dag_path} is not a regular file"
    if dag_file.text is None:
        return f"{dag_path} could not be decoded"
    return None


def _identity_mismatch_reason(dag_file: DagFileInfo, repo_key: RepoKey) -> str:
    if dag_file.named_repo is None:
        named = dag_file.named_repo_error or "unresolved"
    else:
        named = dag_file.named_repo.as_text()
    return f"@dag names {named}, not this repository ({repo_key.as_text()})"


def _classify_open_item(
    work_id: str,
    item: PlanItem,
    dag_by_path: dict[str, DagFileInfo],
    claims: dict[str, list[PlanItem]],
    repo_key: RepoKey,
    repo: RepoAdmission,
) -> ItemDecision:
    """One open, identified item's launchpad category (spec §5.1, first match wins)."""
    if item.dag_raw is None:
        return ItemDecision(work_id, None, "unregistered", NO_DAG_TAG, "no @dag tag")
    if item.dag_diag is not None:
        return ItemDecision(
            work_id, item.dag_raw, "blocked", DAG_INVALID, item.dag_diag
        )

    dag_path = item.dag_tag
    if dag_path is None:
        # dag_raw set + no dag_diag ⇒ parse_dag's contract guarantees a
        # validated dag_tag; treated as invalid rather than assumed, so a
        # captured state that violates the contract still fails closed.
        return ItemDecision(
            work_id, item.dag_raw, "blocked", DAG_INVALID, "@dag did not resolve"
        )

    dag_file = dag_by_path.get(dag_path)
    unreadable = _file_unreadable_reason(dag_file, dag_path)
    if unreadable is not None:
        return ItemDecision(work_id, dag_path, "blocked", DAG_INVALID, unreadable)
    assert dag_file is not None  # unreadable is None only once a file was found

    subset = dag_file.subset
    if isinstance(subset, Rejected):
        return ItemDecision(work_id, dag_path, "blocked", DAG_INVALID, subset.reason)

    if dag_file.named_repo is None or dag_file.named_repo != repo_key:
        return ItemDecision(
            work_id,
            dag_path,
            "blocked",
            DAG_INVALID,
            _identity_mismatch_reason(dag_file, repo_key),
        )

    others = sorted(
        (c for c in claims.get(dag_path, ()) if c is not item), key=lambda c: c.line
    )
    if others:
        lines = ", ".join(str(c.line) for c in others)
        reason = f"{dag_path} is also claimed by line {lines}"
        return ItemDecision(work_id, dag_path, "blocked", DAG_DUPLICATE, reason)

    if dag_file.blob_sha != dag_file.head_blob_sha:
        reason = f"{dag_path} on disk differs from the captured HEAD blob"
        return ItemDecision(work_id, dag_path, "blocked", DAG_DIRTY, reason)

    if repo.blockers:
        first = repo.blockers[0]
        return ItemDecision(
            work_id, dag_path, "blocked", first.code, first.detail or first.code
        )

    return ItemDecision(work_id, dag_path, "ready", None, "")


def _named_dag(item: PlanItem) -> str | None:
    """The DAG path this ledger line NAMES, launchable or not (spec §5.1.3).

    A validated tag names its file; failing that, a grammar-valid raw value
    names one too — a PF-DAG-MISMATCH line, or a line with no usable @id.
    Only a grammar-invalid raw (PF-DAG-GRAMMAR) names nothing.
    """
    if item.dag_tag is not None:
        return item.dag_tag
    if item.dag_raw is not None and DAG_RE.fullmatch(item.dag_raw):
        return item.dag_raw
    return None


def classify_inventory(captured: CapturedInputs) -> InventoryDecision:
    """Ready / blocked / unregistered / orphan over one capture generation.

    Spec §5: consumes only captured values — the classifier never touches
    a store or a disk. `classify_repo` is called here, on the SAME capture
    generation, so there is no separately precomputed repo decision to
    drift from this one. Degraded capture facts (spec §5.1 cond. 12) never
    raise: they produce a decision with `unreadable` set and every list
    empty instead.
    """
    inv = captured.inventory
    repo = classify_repo(
        captured.lock, captured.lock_error, captured.runs, captured.runs_unreadable
    )

    broken = _capture_broken(inv)
    if broken is not None:
        return InventoryDecision(
            repo=repo,
            ready=(),
            blocked=(),
            unregistered_items=(),
            orphan_dags=(),
            unreadable=broken,
        )
    assert inv.repo_key is not None  # narrowed by _capture_broken above

    dag_by_path = {d.rel_path: d for d in inv.dag_files}
    claims: dict[str, list[PlanItem]] = {}
    for claim_item in inv.plan_items:
        # Spec §5.1(3): ANY ledger line naming the DAG claims it — see
        # _named_dag for what counts as naming.
        named = _named_dag(claim_item)
        if named is not None:
            claims.setdefault(named, []).append(claim_item)

    ready: list[ItemDecision] = []
    blocked: list[ItemDecision] = []
    unregistered: list[ItemDecision] = []
    for item in inv.plan_items:
        if not item.open or item.item_id is None:
            continue  # closed, or no identity to launch (PF-ID-MISSING's plane)
        decision = _classify_open_item(
            item.item_id, item, dag_by_path, claims, inv.repo_key, repo
        )
        if decision.category == "ready":
            ready.append(decision)
        elif decision.category == "blocked":
            blocked.append(decision)
        else:
            unregistered.append(decision)

    # Same naming rule as the claims index above (_named_dag).
    open_claims = {
        named
        for open_item in inv.plan_items
        if open_item.open and (named := _named_dag(open_item)) is not None
    }
    orphan_dags = tuple(
        sorted(
            d.rel_path
            for d in inv.dag_files
            # Spec §5.1: an orphan is a VALID artifact nobody registered —
            # readable, regular, subset-accepted. A broken unclaimed file is
            # not reported as orphan: the list would assert a validity it
            # never checked.
            if d.rel_path not in open_claims
            and d.is_regular
            and d.error is None
            and isinstance(d.subset, Accepted)
        )
    )

    return InventoryDecision(
        repo=repo,
        ready=tuple(ready),
        blocked=tuple(blocked),
        unregistered_items=tuple(unregistered),
        orphan_dags=orphan_dags,
        unreadable=None,
    )
