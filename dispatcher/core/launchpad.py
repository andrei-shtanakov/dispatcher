"""The launchpad snapshot assembler (spec §4.1, §5): one read per source,
one classification pass per repo, one internally-consistent result.

`GET /api/launchpad`'s guarantee is precise, not vague: every source —
the store's records, maestro's runs — is read EXACTLY ONCE per assembly,
and every repo's admission is derived from that ONE captured generation via
`classify_inventory`/`classify_repo` (`admission.py`), the SAME classifier
`submit` gates on. `assemble_snapshot` is the second adapter the module
docstring there promises.
"""

from __future__ import annotations

import base64
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from dispatcher.core.admission import (
    TERMINAL_RUN_STATUSES,
    Blocker,
    CapturedInputs,
    classify_inventory,
)
from dispatcher.core.collectors.maestro import classified_runs
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.inventory import capture_inventory
from dispatcher.core.inventory_types import InventorySurface
from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.run_controller import (
    RunController,
    capture_run_facts,
    logs_dir,
    read_lock_state,
)
from dispatcher.core.run_identity import list_workspace_checkouts
from dispatcher.core.run_request import RunRejectedError
from dispatcher.core.run_store import LaunchRecord

#: This assembler's own admission code — the checkout could not even be
#: resolved to a `RepoKey`, so no store/maestro lookup is meaningful for it
#: (spec's admission-codes table; shared with submit v2's 409 vocabulary).
REPO_UNRESOLVED = "repo_unresolved"

#: Task-level maestro states that make a run demand a human, surfaced onto
#: `OrchestrationRunInfo.status` verbatim when maestro records them as a
#: run's `outcome` (spec §4.1's attention triggers, alongside `launch_unknown`).
_ATTENTION_RUN_STATUSES = frozenset({"NEEDS_REVIEW", "AWAITING_APPROVAL"})

#: `active`'s hard cap (spec §4.1): the endpoint must never become unbounded.
_ACTIVE_CAP = 200

_GIT_TIMEOUT = 15


class BlockerView(BaseModel):
    code: str
    request_id: str | None = None
    run_id: str | None = None
    detail: str | None = None


class RepoRow(BaseModel):
    repo_key: str  # canonical text form
    repository: str  # display label (manifest name)
    default_branch: str
    seen_revision: str | None  # full 40-hex; None on capture failure
    admission: str  # "ready" | "blocked" | "unreadable"
    blockers: list[BlockerView]


class ReadyRow(BaseModel):
    repo_key: str
    work_id: str
    dag_path: str
    seen_revision: str


class BlockedRow(BaseModel):
    repo_key: str
    work_id: str
    dag_path: str | None
    reason_code: str
    reason: str


class UnregisteredRow(BaseModel):
    repo_key: str
    work_id: str
    reason_code: str  # "no_dag_tag"


class OrphanRow(BaseModel):
    repo_key: str
    dag_path: str


class ActiveRow(BaseModel):
    request_id: str | None  # None = unlinked maestro run
    repo_key: str
    work_id: str | None
    state: str  # record state, or "unlinked-run"
    run_id: str | None
    run_status: str | None  # from classified_runs when a run exists
    attention: bool
    updated_at: str  # ISO from the record file's mtime;
    # for unlinked runs, the run dir's mtime


class CompletedRow(BaseModel):
    request_id: str
    repo_key: str
    work_id: str
    run_id: str | None  # nullable: admission-rejected / tombstones
    revision: str
    outcome: str
    updated_at: str
    logs_available: bool


class LaunchpadSnapshot(BaseModel):
    snapshot_id: str  # uuid4 hex — opaque, unique per assembly
    generated_at: str
    repositories: list[RepoRow]
    ready: list[ReadyRow]
    blocked: list[BlockedRow]
    unregistered_items: list[UnregisteredRow]
    orphan_dags: list[OrphanRow]
    active: list[ActiveRow]
    active_truncated: bool
    recent_completed: list[CompletedRow]
    completed_total: int
    next_cursor: str | None
    store_unreadable: list[str]  # unreadable record names + workspace-listing
    # failures — global banner (fix round 1: a manifest scan failure must
    # be visible here too, never a silently empty `repositories` list)


def _manifest_repos(
    config: DispatcherConfig,
) -> tuple[list[tuple[str, Path]], list[str]]:
    """`(name, checkout)` pairs — every visible directory of the workspace
    (first existing root), enumerated instead of looked up by name; a
    directory need not carry `.git` to be listed here, that is exactly
    what makes `repo_unresolved` observable below rather than the entry
    simply not existing. `list_workspace_checkouts` (`run_identity.py`) is
    the shared enumeration submit v2's checkout resolver also scans — the
    review fix wave C, C1 fix that keeps this list and submit's own
    identity-based lookup from ever walking the workspace differently.

    Returns `(entries, notes)`. When NO configured root is even a
    directory, that is reported as a note too (review fix wave C, I2)
    rather than a silent `([], [])` that would render as an empty, healthy
    fleet with no explanation.
    """
    workspace = next((r for r in config.roots if r.is_dir()), None)
    if workspace is None:
        roots = ", ".join(str(r) for r in config.roots) or "none configured"
        return [], [f"no configured workspace root is a directory: {roots}"]
    return list_workspace_checkouts(workspace)


def _default_branch(checkout: Path) -> str:
    """Best-effort, display-only — never used for identity or admission.
    The REMOTE default (`origin/HEAD`) when the clone recorded one — the
    field is NAMED default_branch (spec §4.1), and a checkout parked on a
    feature branch must not report that branch as the default (gate
    pass-4). Falls back to the current branch (a fresh clone sits on the
    default), and any git failure degrades to `""` rather than raising,
    same as every other cosmetic field in this module.
    """
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "symbolic-ref",
                "--short",
                "refs/remotes/origin/HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            # "origin/master" → "master"
            return proc.stdout.strip().split("/", 1)[-1]
    except (OSError, subprocess.TimeoutExpired):
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(checkout), "symbolic-ref", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _unresolved_row(
    repository: str, checkout: Path, inv: InventorySurface | None
) -> RepoRow:
    if inv is None:
        detail = f"{checkout} is not a git checkout (.git missing)"
        seen_revision = None
    else:
        detail = inv.capture_error or "repository identity could not be resolved"
        seen_revision = inv.head_revision
    return RepoRow(
        repo_key=repository,
        repository=repository,
        default_branch="",
        seen_revision=seen_revision,
        admission="unreadable",
        blockers=[BlockerView(code=REPO_UNRESOLVED, detail=detail)],
    )


def _blocker_view(blocker: Blocker) -> BlockerView:
    return BlockerView(
        code=blocker.code,
        request_id=blocker.request_id,
        run_id=blocker.run_id,
        detail=blocker.detail or None,
    )


def _mtime_iso(path: Path) -> str:
    """ISO UTC mtime, or `""` on any stat failure — never raises (mirrors
    `RunStore.list_with_mtime`'s own degradation)."""
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()
    except OSError:
        return ""


def _logs_available(home: Path, record: LaunchRecord) -> bool:
    if record.run_id is None:
        return False
    try:
        path = logs_dir(home, record)
    except RunRejectedError:
        return False
    return path.is_dir()


def _encode_cursor(updated_at: str, request_id: str) -> str:
    raw = f"{updated_at}\x00{request_id}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[str, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as err:
        raise ValueError(f"invalid cursor: {cursor!r}") from err
    updated_at, sep, request_id = raw.partition("\x00")
    if not sep or not updated_at or not request_id:
        # Server-issued cursors always encode a NON-EMPTY pair; an empty
        # half (e.g. base64 of a lone NUL) was never issued by this server
        # and silently filtering everything out would fake an empty page.
        raise ValueError(f"invalid cursor: {cursor!r}")
    return updated_at, request_id


def assemble_snapshot(
    controller: RunController,
    *,
    recent_limit: int = 20,
    cursor: str | None = None,
) -> LaunchpadSnapshot:
    """One internally-consistent read of everything the launchpad shows.

    `ControlPlaneOff` propagates from `controller._require_on()`, same as
    `submit` (Task 3 maps it at the route). Every other source — the
    store's records and maestro's runs — is read exactly once here and
    threaded into every repo's classification, never re-read per repo
    (spec §4.1's "every source read once" made structural, mirrored from
    `capture_run_facts`'s own hoisted-parameter contract).
    """
    _, _, home = controller._require_on()  # noqa: SLF001 — mirrors submit's own gate
    store = controller._store()  # noqa: SLF001 — same private wiring submit uses

    records_with_mtime, store_unreadable = store.list_with_mtime()
    records = [record for record, _ in records_with_mtime]
    mtime_by_request_id = {
        record.request_id: mtime for record, mtime in records_with_mtime
    }
    scratch = ProjectSnapshot(name="maestro", path="")
    classified = classified_runs(home, scratch)
    scratch_warnings = scratch.warnings
    status_by_run_id = {
        info.run_id: info.status for info, _ in classified if info.run_id is not None
    }

    repositories: list[RepoRow] = []
    ready: list[ReadyRow] = []
    blocked: list[BlockedRow] = []
    unregistered_items: list[UnregisteredRow] = []
    orphan_dags: list[OrphanRow] = []

    manifest_repos, manifest_notes = _manifest_repos(controller._config)  # noqa: SLF001

    # Capture ONCE per repo (spec §4.1's once-per-assembly, pinned by the
    # instrumented-counter test), then dedupe identities BEFORE classifying:
    # two checkouts of one RepoKey make every ReadyRow behind that key
    # ambiguous — the row carries only repo_key, so submit could resolve
    # the OTHER copy (a different HEAD) and persist a wrong decision.
    # Fail closed (gate pass-2 finding); submit_v2's resolver refuses the
    # same state with the same code.
    surfaces: list[tuple[str, Path, InventorySurface | None]] = []
    identity_dirs: dict[str, list[str]] = {}
    for repository, checkout in manifest_repos:
        if not (checkout / ".git").exists():
            surfaces.append((repository, checkout, None))
            continue
        inv = capture_inventory(checkout)
        surfaces.append((repository, checkout, inv))
        if inv.repo_key is not None:
            identity_dirs.setdefault(inv.repo_key.as_text(), []).append(repository)
    duplicated = {k for k, dirs in identity_dirs.items() if len(dirs) > 1}

    for repository, checkout, inv in surfaces:
        if inv is None:
            repositories.append(_unresolved_row(repository, checkout, None))
            continue
        if inv.repo_key is None:
            repositories.append(_unresolved_row(repository, checkout, inv))
            continue
        key = inv.repo_key
        if key.as_text() in duplicated:
            copies = ", ".join(identity_dirs[key.as_text()])
            repositories.append(
                RepoRow(
                    repo_key=key.as_text(),
                    repository=repository,
                    default_branch=_default_branch(checkout),
                    seen_revision=inv.head_revision,
                    admission="unreadable",
                    blockers=[
                        BlockerView(
                            code=REPO_UNRESOLVED,
                            detail=(
                                f"{len(identity_dirs[key.as_text()])} checkouts "
                                f"of {key.as_text()!r} in the workspace "
                                f"({copies}) — ambiguous; remove or move the "
                                "duplicates"
                            ),
                        )
                    ],
                )
            )
            continue
        lock, lock_error = read_lock_state(store, key)
        runs_root = controller.runs_dir(key)
        run_facts, runs_unreadable = capture_run_facts(
            store,
            key,
            runs_root,
            home,
            records=records,
            list_unreadable=store_unreadable,
            classified=classified,
            scratch_warnings=scratch_warnings,
        )
        captured = CapturedInputs(
            inventory=inv,
            lock=lock,
            lock_error=lock_error,
            runs=run_facts,
            runs_unreadable=runs_unreadable,
        )
        decision = classify_inventory(captured)
        if decision.unreadable is not None:
            # Parity with submit_v2 (`run_controller.py`'s own
            # `decision.unreadable is not None` check maps this to 409
            # `repo_unresolved`): degraded captured facts must read as
            # unreadable here too, never fall through to `decision.repo`'s
            # admission — that field only reflects lock/run state and
            # knows nothing about a broken inventory capture, so leaving
            # it unchecked rendered a broken repo as a clean, empty,
            # "ready" one (fail-open, review fix wave C, I1).
            repositories.append(
                RepoRow(
                    repo_key=key.as_text(),
                    repository=repository,
                    default_branch=_default_branch(checkout),
                    seen_revision=inv.head_revision,
                    admission="unreadable",
                    blockers=[
                        BlockerView(code=REPO_UNRESOLVED, detail=decision.unreadable)
                    ],
                )
            )
            continue
        repositories.append(
            RepoRow(
                repo_key=key.as_text(),
                repository=repository,
                default_branch=_default_branch(checkout),
                seen_revision=inv.head_revision,
                admission=decision.repo.admission,
                blockers=[_blocker_view(b) for b in decision.repo.blockers],
            )
        )
        for item in decision.ready:
            assert item.dag_path is not None  # ready items always resolve one
            assert inv.head_revision is not None  # repo/broken gate above proves it
            ready.append(
                ReadyRow(
                    repo_key=key.as_text(),
                    work_id=item.work_id,
                    dag_path=item.dag_path,
                    seen_revision=inv.head_revision,
                )
            )
        for item in decision.blocked:
            blocked.append(
                BlockedRow(
                    repo_key=key.as_text(),
                    work_id=item.work_id,
                    dag_path=item.dag_path,
                    reason_code=item.reason_code or "",
                    reason=item.reason,
                )
            )
        for item in decision.unregistered_items:
            assert item.reason_code is not None  # only NO_DAG_TAG reaches here
            unregistered_items.append(
                UnregisteredRow(
                    repo_key=key.as_text(),
                    work_id=item.work_id,
                    reason_code=item.reason_code,
                )
            )
        for dag_path in decision.orphan_dags:
            orphan_dags.append(OrphanRow(repo_key=key.as_text(), dag_path=dag_path))

    active_rows: list[ActiveRow] = []
    for record in records:
        if record.state == "terminal":
            continue
        run_status = status_by_run_id.get(record.run_id) if record.run_id else None
        attention = (
            record.state == "launch_unknown" or run_status in _ATTENTION_RUN_STATUSES
        )
        active_rows.append(
            ActiveRow(
                request_id=record.request_id,
                repo_key=record.repo_key,
                work_id=record.work_id or None,
                state=record.state,
                run_id=record.run_id,
                run_status=run_status,
                attention=attention,
                updated_at=mtime_by_request_id.get(record.request_id, ""),
            )
        )

    linked_run_ids = {r.run_id for r in records if r.run_id is not None}
    for info, db in classified:
        if info.run_id is None or info.run_id in linked_run_ids:
            continue
        if info.status in TERMINAL_RUN_STATUSES or info.status == "unreadable":
            continue
        active_rows.append(
            ActiveRow(
                request_id=None,
                repo_key=info.repo_key,
                work_id=None,
                state="unlinked-run",
                run_id=info.run_id,
                run_status=info.status,
                attention=info.status in _ATTENTION_RUN_STATUSES,
                updated_at=_mtime_iso(db.parent),
            )
        )

    active_sorted = sorted(
        active_rows, key=lambda r: (r.attention, r.updated_at), reverse=True
    )
    active_truncated = len(active_sorted) > _ACTIVE_CAP
    active = active_sorted[:_ACTIVE_CAP]

    completed_rows = [
        CompletedRow(
            request_id=record.request_id,
            repo_key=record.repo_key,
            work_id=record.work_id,
            run_id=record.run_id,
            revision=record.revision,
            outcome=record.outcome or "",
            updated_at=mtime_by_request_id.get(record.request_id, ""),
            logs_available=_logs_available(home, record),
        )
        for record in records
        if record.state == "terminal"
    ]
    completed_sorted = sorted(
        completed_rows, key=lambda r: (r.updated_at, r.request_id), reverse=True
    )
    completed_total = len(completed_sorted)
    remaining = completed_sorted
    if cursor is not None:
        after_updated_at, after_request_id = _decode_cursor(cursor)
        after_key = (after_updated_at, after_request_id)
        remaining = [
            row
            for row in completed_sorted
            if (row.updated_at, row.request_id) < after_key
        ]
    page = remaining[:recent_limit]
    next_cursor = (
        _encode_cursor(page[-1].updated_at, page[-1].request_id)
        if len(remaining) > recent_limit
        else None
    )

    return LaunchpadSnapshot(
        snapshot_id=uuid.uuid4().hex,
        generated_at=datetime.now(UTC).isoformat(),
        repositories=repositories,
        ready=ready,
        blocked=blocked,
        unregistered_items=unregistered_items,
        orphan_dags=orphan_dags,
        active=active,
        active_truncated=active_truncated,
        recent_completed=page,
        completed_total=completed_total,
        next_cursor=next_cursor,
        store_unreadable=list(store_unreadable) + manifest_notes,
    )
