"""Sync verdict engine: is it safe to start working on this machine?

Inputs (DESIGN-202): a live git-only snapshot of this host's workspace
(`github-checker snapshot --local-only`, requires github-checker on PATH) plus
per-host snapshots published to the KB (`prograph-vault/derived/snapshots/`).
Output: a verdict per (repo, host) — ``ok | pull-first | no-data | unknown`` —
and a worst-case top line for the current host. Degradation is honest by
construction: absence, staleness, schema drift and local git errors all render
as explicit non-``ok`` verdicts, never as an optimistic default; ``gh_error``
alone degrades only PR data and does not poison the git-state verdict.
"""

from __future__ import annotations

import socket
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.snapshot_contract import (
    RepoSnapshotV1,
    SnapshotContractError,
    WorkspaceSnapshotV1,
    parse_snapshot,
)
from dispatcher.core.tracking import TrackingState, load_tracking, seed_tracking

KB_REPO = "prograph-vault"
STALE_AFTER_SECONDS = 3600.0  # brief AP-02: publication freshness ≤ 1 h
_SNAPSHOT_TIMEOUT = 120

VERDICT_OK = "ok"
VERDICT_PULL_FIRST = "pull-first"
VERDICT_NO_DATA = "no-data"
VERDICT_UNKNOWN = "unknown"

# Top line = worst verdict on the current host: any pull-first beats any
# unknown ("надо синхронизироваться" is more actionable than "не знаю").
_SEVERITY = {
    VERDICT_OK: 0,
    VERDICT_NO_DATA: 1,
    VERDICT_UNKNOWN: 2,
    VERDICT_PULL_FIRST: 3,
}


class RepoVerdict(BaseModel):
    """One (repo, host) verdict row."""

    repo: str
    verdict: str
    reason: str | None = None
    branch: str | None = None
    ahead: int | None = None
    behind: int | None = None
    dirty: bool = False
    is_kb: bool = False


class HostPanel(BaseModel):
    """All verdicts for one host, with the snapshot's provenance and age."""

    host: str
    source: str  # "live" | "kb"
    generated_at: datetime | None = None
    age_seconds: float | None = None
    stale: bool = False
    gh_error: str | None = None
    error: str | None = None  # snapshot-level failure (contract/schema/read)
    verdicts: list[RepoVerdict] = Field(default_factory=list)


class SyncReport(BaseModel):
    """The sync screen's read model."""

    current_host: str
    top_line: str
    top_reason: str | None = None
    hosts: list[HostPanel] = Field(default_factory=list)
    proposals: list[str] = Field(default_factory=list)  # FR-02: «отслеживать?»
    warnings: list[str] = Field(default_factory=list)


class SyncSourceError(Exception):
    """The live snapshot could not be produced (github-checker unavailable)."""


def _repo_verdict(repo: RepoSnapshotV1, *, stale: bool) -> RepoVerdict:
    local = repo.local
    row = RepoVerdict(
        repo=repo.dir,
        verdict=VERDICT_UNKNOWN,
        branch=local.branch,
        ahead=local.ahead,
        behind=local.behind,
        dirty=local.dirty,
        is_kb=repo.dir == KB_REPO,
    )
    if stale:
        row.reason = "stale snapshot (older than 1 h)"
        return row
    if local.error:
        row.reason = f"local git error: {local.error}"
        return row
    pull_reasons = []
    if local.behind:
        pull_reasons.append(f"behind {local.behind}")
    if local.ahead:
        pull_reasons.append(f"ahead {local.ahead} (unpushed)")
    if local.dirty:
        pull_reasons.append("dirty worktree")
    if pull_reasons:
        row.verdict = VERDICT_PULL_FIRST
        row.reason = ", ".join(pull_reasons)
        return row
    if local.ahead is None or local.behind is None:
        row.reason = "ahead/behind unknown (no upstream or never fetched)"
        return row
    row.verdict = VERDICT_OK
    row.reason = None
    return row


def _panel(snapshot: WorkspaceSnapshotV1, *, source: str, now: datetime) -> HostPanel:
    age = snapshot.age_seconds(now)
    # A live run is by definition fresh; only published snapshots go stale.
    stale = source == "kb" and age > STALE_AFTER_SECONDS
    verdicts = [_repo_verdict(repo, stale=stale) for repo in snapshot.repos]
    verdicts.sort(key=lambda v: (not v.is_kb, v.repo))  # KB pinned first (G-03)
    return HostPanel(
        host=snapshot.host,
        source=source,
        generated_at=snapshot.generated_at,
        age_seconds=age,
        stale=stale,
        gh_error=snapshot.gh_error,
        verdicts=verdicts,
    )


def _fill_no_data(panels: list[HostPanel]) -> None:
    """CON-03: a repo missing from a host's snapshot is `no-data`, never ok."""
    universe = {v.repo for panel in panels for v in panel.verdicts}
    for panel in panels:
        if panel.error is not None:
            continue
        present = {v.repo for v in panel.verdicts}
        for repo in sorted(universe - present):
            panel.verdicts.append(
                RepoVerdict(
                    repo=repo,
                    verdict=VERDICT_NO_DATA,
                    reason="repo not present in this host's snapshot",
                    is_kb=repo == KB_REPO,
                )
            )
        panel.verdicts.sort(key=lambda v: (not v.is_kb, v.repo))


def build_report(
    *,
    current_host: str,
    live: WorkspaceSnapshotV1 | None,
    live_error: str | None,
    kb_snapshots: list[WorkspaceSnapshotV1],
    kb_errors: list[tuple[str, str]] | None = None,
    tracking: TrackingState | None = None,
    now: datetime | None = None,
) -> SyncReport:
    """Pure assembly of the sync report from already-ingested snapshots.

    With *tracking* set (DESIGN-205), verdicts cover only tracked repos:
    ignored repos disappear, unknown ones become proposals («отслеживать?»)
    and never affect the top line until confirmed.
    """
    kb_errors = kb_errors or []
    moment = now if now is not None else datetime.now(UTC)
    panels: list[HostPanel] = []
    warnings: list[str] = []

    if live is not None:
        panels.append(_panel(live, source="live", now=moment))
    elif live_error is not None:
        warnings.append(f"live snapshot unavailable: {live_error}")

    for snapshot in kb_snapshots:
        if live is not None and snapshot.host == live.host:
            continue  # the live run supersedes this host's published snapshot
        panels.append(_panel(snapshot, source="kb", now=moment))
    for name, err in kb_errors:
        panels.append(HostPanel(host=name, source="kb", error=err))
        warnings.append(
            f"KB snapshot {name!r} rejected: {err} "
            "(contract pin: contracts/github-checker-snapshot/v1/)"
        )

    proposals: list[str] = []
    if tracking is not None:
        seen = {v.repo for panel in panels for v in panel.verdicts}
        proposals = sorted(seen - tracking.known())
        for panel in panels:
            panel.verdicts = [v for v in panel.verdicts if v.repo in tracking.tracked]

    _fill_no_data(panels)

    current = next((p for p in panels if p.host == current_host), None)
    if current is None:
        top_line, top_reason = (
            VERDICT_UNKNOWN,
            "no snapshot for this host — работаем локально, синк не подтверждён",
        )
    elif not current.verdicts:
        top_line, top_reason = (
            VERDICT_UNKNOWN,
            "no tracked repos on this host",
        )
    else:
        worst = max(current.verdicts, key=lambda v: _SEVERITY[v.verdict])
        top_line = worst.verdict
        top_reason = (
            None
            if worst.verdict == VERDICT_OK
            else f"{worst.repo}: {worst.reason or worst.verdict}"
        )
    return SyncReport(
        current_host=current_host,
        top_line=top_line,
        top_reason=top_reason,
        hosts=panels,
        proposals=proposals,
        warnings=warnings,
    )


def run_live_snapshot(
    workspace: Path, *, command: tuple[str, ...] = ("github-checker",)
) -> WorkspaceSnapshotV1:
    """Run a git-only snapshot of *workspace*; requires github-checker on PATH."""
    argv = [
        *command,
        "snapshot",
        "--workspace",
        str(workspace),
        "--local-only",
        "--indent",
        "0",
    ]
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=_SNAPSHOT_TIMEOUT
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as err:
        raise SyncSourceError(str(err)) from err
    if proc.returncode != 0:
        raise SyncSourceError(proc.stderr.strip() or "github-checker snapshot failed")
    try:
        return parse_snapshot(proc.stdout)
    except SnapshotContractError as err:
        raise SyncSourceError(str(err)) from err


def kb_snapshot_dirs(roots: tuple[Path, ...]) -> tuple[Path, ...]:
    """Published-snapshot locations derived from workspace roots (AP-02)."""
    return tuple(root / KB_REPO / "derived" / "snapshots" for root in roots)


def load_kb_snapshots(
    dirs: tuple[Path, ...],
) -> tuple[list[WorkspaceSnapshotV1], list[tuple[str, str]]]:
    """Read every published `<host>.json`; contract failures become errors, not crashes."""
    snapshots: list[WorkspaceSnapshotV1] = []
    errors: list[tuple[str, str]] = []
    for directory in dirs:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                snapshot = parse_snapshot(path.read_text(encoding="utf-8"))
            except (OSError, SnapshotContractError) as err:
                errors.append((path.stem, str(err)))
                continue
            if snapshot.host != path.stem:
                # `<host>.json` convention (prograph-vault#24): a mismatched
                # payload would misattribute the panel — contract error
                errors.append(
                    (
                        path.stem,
                        f"payload host {snapshot.host!r} does not match "
                        f"filename {path.name!r}",
                    )
                )
                continue
            snapshots.append(snapshot)
    return snapshots, errors


def collect_sync(config: DispatcherConfig, now: datetime | None = None) -> SyncReport:
    """IO shell: live snapshot of the first existing root + KB panels."""
    live: WorkspaceSnapshotV1 | None = None
    live_error: str | None = None
    workspace = next((root for root in config.roots if root.is_dir()), None)
    if workspace is None:
        live_error = "no existing workspace root configured"
    else:
        try:
            live = run_live_snapshot(workspace)
        except SyncSourceError as err:
            live_error = str(err)
    kb_snapshots, kb_errors = load_kb_snapshots(kb_snapshot_dirs(config.roots))

    tracking: TrackingState | None = None
    seeded = False
    if config.tracking_file is not None:
        tracking = load_tracking(config.tracking_file)
        if tracking is None and live is not None:
            # zero-docs bootstrap (FR-02): всё уже присутствующее — tracked,
            # предложения дальше получают только действительно новые клоны
            tracking = seed_tracking(
                config.tracking_file, {repo.dir for repo in live.repos}
            )
            seeded = True

    report = build_report(
        current_host=socket.gethostname(),
        live=live,
        live_error=live_error,
        kb_snapshots=kb_snapshots,
        kb_errors=kb_errors,
        tracking=tracking,
        now=now,
    )
    if seeded and tracking is not None:
        report.warnings.append(
            f"sync tracking initialized: {len(tracking.tracked)} repos tracked "
            f"({config.tracking_file})"
        )
    return report
