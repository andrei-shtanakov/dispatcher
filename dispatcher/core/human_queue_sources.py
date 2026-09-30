"""Human queue A1 adapters — one per source (spec 2026-09-29-human-queue-a1 §3).

Each adapter reads one source and returns a `SourceResult`; none of them
orders, merges, or decides completeness — `human_queue.assemble` does.
Degradation rule for every adapter: what could not be read is named in the
source's status, never absorbed into a shorter list.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

from dispatcher.core import read_api
from dispatcher.core.actions import ActionOutcome
from dispatcher.core.background_reader import BackgroundReader, spawn_daemon
from dispatcher.core.collectors.base import SourceReadError, coerce_str
from dispatcher.core.collectors.maestro import classified_runs, waiting_tasks
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.human_queue import (
    HumanMergeAct,
    HumanQueueView,
    HumanWait,
    MaestroVerbAct,
    OpenArtifactAct,
    Reason,
    RunViewAct,
    SourceResult,
    SourceStatus,
    assemble,
    proven_since,
)
from dispatcher.core.models import OrchestrationRunInfo, ProjectSnapshot
from dispatcher.core.product_proposals import (
    BacklogWait,
    GateWait,
    LoopWait,
    ProductProposalsReport,
)
from dispatcher.core.run_store import LaunchRecord, RunStore
from dispatcher.core.service import SnapshotService

_UNKNOWN_BASIS = "dispatcher launch record: entered launch_unknown"

# maestro run statuses that can still hold an actionable wait (spec §3.2);
# a run with an `outcome` is over.
_NON_TERMINAL = frozenset({"running", "suspended", "interrupted"})

# task status → (reason, the slice-0 verb that clears it — spec §6 there)
_MAESTRO_WAITS: dict[str, tuple[Reason, str]] = {
    "needs_review": ("run_needs_review", "retry"),
    "awaiting_approval": ("run_awaiting_approval", "approve"),
}


def _status(problems: list[str]) -> SourceStatus:
    if problems:
        return SourceStatus(state="partial", detail="; ".join(problems))
    return SourceStatus(state="ok")


#: `RunStore.list()`'s answer: readable records, unreadable filenames.
StoreListing = tuple[list[LaunchRecord], list[str]]


def read_store(config: DispatcherConfig) -> StoreListing | None:
    """One read of dispatcher's RunStore; None when the control plane is off.

    Read once per assembly: the same listing feeds `from_run_store` and the
    maestro join, so the two cannot see different store states.
    """
    if config.run_state_dir is None:
        return None
    return RunStore(config.run_state_dir).list()


def from_run_store(listing: StoreListing | None) -> SourceResult:
    """`launch_unknown` records (spec §3.1)."""
    if listing is None:
        return SourceResult(
            name="dispatcher_runs",
            status=SourceStatus(
                state="not_configured", detail="run_state_dir is not set"
            ),
        )
    records, unreadable = listing
    problems = [f"unreadable: {', '.join(unreadable)}"] if unreadable else []
    waits = [_launch_unknown_wait(r) for r in records if r.state == "launch_unknown"]
    return SourceResult(name="dispatcher_runs", status=_status(problems), waits=waits)


def _launch_unknown_wait(record: LaunchRecord) -> HumanWait:
    since, basis = proven_since(record.unknown_at, _UNKNOWN_BASIS)
    return HumanWait(
        key=f"dispatcher-run:{record.request_id}",
        reasons=["launch_unknown"],
        source="dispatcher_runs",
        repo=record.repo_key or None,
        ref=record.request_id,
        title=f"launch outcome unknown: {record.work_id or record.request_id}",
        since=since,
        since_basis=basis,
        act=RunViewAct(request_id=record.request_id),
    )


def from_maestro(
    home: Path,
    records: list[LaunchRecord],
    *,
    atp_catalog: Path | None = None,
    maestro_cli: Path | None = None,
) -> SourceResult:
    """Waiting tasks of every non-terminal run (spec §3.2).

    *atp_catalog* / *maestro_cli* ride along in each `maestro_verb` act so
    the prepared command runs in the environment the wait was read from.
    """
    env = {
        "maestro_home": str(home),
        "atp_catalog": None if atp_catalog is None else str(atp_catalog),
        "maestro_cli": None if maestro_cli is None else str(maestro_cli),
    }
    snap = ProjectSnapshot(name="maestro", path=str(home))
    runs = classified_runs(home, snap, report_missing_state=True)
    # classified_runs reports enumeration failures, unreadable runs and (with
    # the flag) run directories without state.db as warnings on the
    # snapshot; each one is a hole in this source.
    problems = list(snap.warnings)
    launched: dict[tuple[str, str], str] = {
        (r.repo_key, r.run_id): r.request_id for r in records if r.run_id
    }
    waits: list[HumanWait] = []
    for info, db in runs:
        if info.status not in _NON_TERMINAL:
            continue
        try:
            rows = waiting_tasks(db)
        except SourceReadError as err:
            problems.append(f"run {info.run_id}: {err}")
            continue
        waits.extend(_maestro_wait(info, row, launched, env) for row in rows)
    return SourceResult(name="maestro", status=_status(problems), waits=waits)


def _maestro_wait(
    info: OrchestrationRunInfo,
    row: dict[str, object],
    launched: dict[tuple[str, str], str],
    env: dict[str, str | None],
) -> HumanWait:
    status = coerce_str(row["status"])
    reason, verb = _MAESTRO_WAITS[status]
    run_id = info.run_id or ""
    task_id = coerce_str(row["id"])
    request_id = launched.get((info.repo_key, run_id))
    act: RunViewAct | MaestroVerbAct = (
        RunViewAct(request_id=request_id)
        if request_id
        else MaestroVerbAct(
            verb="retry" if verb == "retry" else "approve",
            task_id=task_id,
            run_id=run_id,
            repo_key=info.repo_key,
            maestro_home=env["maestro_home"] or "",
            atp_catalog=env["atp_catalog"],
            maestro_cli=env["maestro_cli"],
        )
    )
    return HumanWait(
        key=f"maestro:{info.repo_key}:{run_id}:{task_id}:{status}",
        reasons=[reason],
        source="maestro",
        repo=info.repo_key,
        ref=f"{run_id}/{task_id}",
        title=f"{coerce_str(row['title'])} [{coerce_str(row['agent_type'])}]",
        since=None,
        since_basis=None,
        act=act,
    )


_LOOP_BASIS = "impresario loop stop.at"
_NOT_OBSERVED = SourceResult(
    name="impresario",
    status=SourceStatus(state="not_configured", detail="no impresario mirror observed"),
)


def from_impresario(cache: SnapshotService) -> SourceResult:
    """The existing impresario read model, re-shaped (spec §3.3)."""
    try:
        report = read_api.product_proposals(cache, "impresario")
    except read_api.ReadLookupError:
        # Unreachable in practice: SnapshotService lists every collector.
        return _NOT_OBSERVED
    return from_impresario_report(report)


def from_impresario_report(report: ProductProposalsReport) -> SourceResult:
    """Map an impresario product-proposals report to a `SourceResult`.

    Mirror diagnostics decide the source state; waits are only emitted from a
    trustworthy mirror.
    """
    codes = sorted({d.code for d in report.diagnostics})
    if "mirror-not-detected" in codes:
        return _NOT_OBSERVED
    if "mirror-anchors-missing" in codes:
        return SourceResult(
            name="impresario",
            status=SourceStatus(state="unavailable", detail=", ".join(codes)),
        )
    waits = [
        *(_loop_wait(w) for w in report.needs_human),
        *(_gate_wait(w) for w in report.waits),
        *(_backlog_wait(w) for w in report.backlog_waits),
    ]
    problems: list[str] = []
    if report.attention:
        bad = [b.path for b in report.bundles if b.state != "ok"]
        bad += [b.path for b in report.backlog_bundles if b.state != "ok"]
        if bad:
            problems.append(f"non-ok bundles: {', '.join(bad)}")
        if codes:
            problems.append(f"diagnostics: {', '.join(codes)}")
        if not problems:
            problems.append("report flagged attention")
    return SourceResult(name="impresario", status=_status(problems), waits=waits)


def _loop_wait(wait: LoopWait) -> HumanWait:
    since, basis = proven_since(wait.stopped_at, _LOOP_BASIS)
    return HumanWait(
        key=f"impresario-loop:{wait.loop_id}:{wait.iteration}",
        reasons=["loop_needs_human"],
        source="impresario",
        repo="impresario",
        ref=wait.loop_id,
        title=f"{wait.proposal_id}: {wait.reason}",
        since=since,
        since_basis=basis,
        act=OpenArtifactAct(path=wait.bundle_path),
    )


def _gate_wait(wait: GateWait) -> HumanWait:
    # proposal_updated_at is NOT a wait start (core/product_proposals.py:129).
    return HumanWait(
        key=f"impresario-gate:{wait.proposal_id}:{wait.gate_id}:{wait.version}",
        reasons=["proposal_gate"],
        source="impresario",
        repo="impresario",
        ref=wait.proposal_id,
        title=f"{wait.proposal_id}: {wait.gate_label} ({wait.authority})",
        since=None,
        since_basis=None,
        act=OpenArtifactAct(path=wait.bundle_path),
    )


def _backlog_wait(wait: BacklogWait) -> HumanWait:
    # backlog_updated_at is left out until spec §8 question 1 is answered.
    return HumanWait(
        key=f"impresario-qg4:{wait.backlog_id}:{wait.version}",
        reasons=["backlog_gate"],
        source="impresario",
        repo="impresario",
        ref=wait.backlog_id,
        title=f"{wait.backlog_id}: {wait.gate_label} ({wait.authority})",
        since=None,
        since_basis=None,
        act=OpenArtifactAct(path=wait.artifact_path),
    )


FORGE_SOURCE = "forge_labelled_prs"
#: pr-search answers are reused this long: the queue is polled every few
#: seconds, and one search plus two reads per PR must not run that often.
FORGE_TTL_SECONDS = 60.0

_FORGE_OFF = SourceResult(
    name=FORGE_SOURCE,
    status=SourceStatus(state="not_configured", detail="forge_merge_label is not set"),
)


def from_pr_search(outcome: ActionOutcome, label: str) -> SourceResult:
    """PRs the policy reserves for a human (spec A2 §2).

    Only a list the producer stated is believed: `prs` null — a failed,
    non-exhaustive or unreadable search, or github-checker not runnable —
    is `unavailable`, never an empty queue.
    """
    if outcome.prs is None or not outcome.ok:
        # A list on an ok:false answer is not believed either: the producer
        # said the search failed, whatever else it sent.
        detail = outcome.error or f"pr-search did not answer ({outcome.phase})"
        return SourceResult(
            name=FORGE_SOURCE, status=SourceStatus(state="unavailable", detail=detail)
        )
    basis = f"label {label} added"
    waits = [_pr_wait(pr, basis) for pr in outcome.prs]
    return SourceResult(name=FORGE_SOURCE, status=SourceStatus(state="ok"), waits=waits)


def _pr_wait(pr: dict[str, object], basis: str) -> HumanWait:
    repo = coerce_str(pr["repo"])
    number = int(str(pr["number"]))
    labeled_at = pr.get("labeled_at")
    since, since_basis = proven_since(
        labeled_at if isinstance(labeled_at, str) else None, basis
    )
    head_sha = pr.get("head_sha")
    return HumanWait(
        key=f"pr:{repo}#{number}",
        reasons=["pr_human_merge"],
        source=FORGE_SOURCE,
        repo=repo,
        ref=f"{repo}#{number}",
        title=coerce_str(pr.get("title"), default=f"{repo}#{number}"),
        since=since,
        since_basis=since_basis,
        act=HumanMergeAct(
            repo=repo,
            number=number,
            url=coerce_str(pr["url"]),
            head_sha=head_sha if isinstance(head_sha, str) else None,
        ),
    )


_FORGE_PENDING = SourceResult(
    name=FORGE_SOURCE,
    status=SourceStatus(state="unavailable", detail="first pr-search in progress"),
)


def _spawn_daemon(fn: Callable[[], None]) -> None:
    spawn_daemon(fn, "forge-pr-search")


class ForgeReader(BackgroundReader[SourceResult]):
    """The forge source, refreshed in the background (spec A2 §1).

    `read()` never waits on GitHub: a search plus two reads per PR can
    outlast a client's request timeout. Before the first search completes
    the source says so (`unavailable`, "in progress"), never an empty list.
    A raising search is this source's `unavailable`, cached like a success.
    """

    def __init__(
        self,
        search: Callable[[str], ActionOutcome],
        label: str,
        *,
        ttl: float = FORGE_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None]], None] = _spawn_daemon,
    ) -> None:
        super().__init__(
            lambda: _guarded(
                FORGE_SOURCE, lambda: from_pr_search(search(label), label)
            ),
            _FORGE_PENDING,
            ttl=ttl,
            clock=clock,
            spawn=spawn,
        )


def _unavailable(name: str, exc: Exception) -> SourceResult:
    return SourceResult(
        name=name,
        status=SourceStatus(state="unavailable", detail=f"{type(exc).__name__}: {exc}"),
    )


def _guarded(name: str, adapter: Callable[[], SourceResult]) -> SourceResult:
    """One source failing must not take the others down (spec §4.1)."""
    try:
        return adapter()
    except Exception as exc:  # noqa: BLE001 — isolation IS the contract
        return _unavailable(name, exc)


def build_human_queue(
    config: DispatcherConfig,
    cache: SnapshotService,
    *,
    now: str,
    forge: ForgeReader | None = None,
) -> HumanQueueView:
    """Every source, each isolated, assembled into one view.

    *forge* None means the forge source is off (`not_configured`) — the
    app passes a reader only when `forge_merge_label` is set.
    """
    # LaunchRecords are only an enrichment for maestro (run_view instead of
    # maestro_verb, spec §3.2). A failing store read makes dispatcher_runs
    # unavailable and leaves maestro to be read with no records — never the
    # other way round (spec §4.1, acceptance 7).
    records: list[LaunchRecord] = []
    try:
        listing = read_store(config)
    except Exception as exc:  # noqa: BLE001 — isolation IS the contract
        store = _unavailable("dispatcher_runs", exc)
    else:
        store = _guarded("dispatcher_runs", lambda: from_run_store(listing))
        records = listing[0] if listing is not None else []
    results = [
        store,
        _guarded(
            "maestro",
            lambda: from_maestro(
                config.effective_maestro_home,
                records,
                atp_catalog=config.atp_catalog,
                maestro_cli=config.maestro_cli,
            ),
        ),
        # Looked up through the module at call time so tests can patch it.
        _guarded("impresario", lambda: from_impresario(cache)),
        # Guarded like every other source: a failure inside the reader (a
        # thread that cannot start) is this source's `unavailable`, not a 500.
        _guarded(FORGE_SOURCE, forge.read) if forge is not None else _FORGE_OFF,
    ]
    return assemble(results, now=now)
