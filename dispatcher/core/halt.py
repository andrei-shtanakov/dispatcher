"""The DarkFactory halt ("stop-crane"), slice D1.

Spec: docs/superpowers/specs/2026-09-30-halt-d1-design.md (parent: the human
control plane design §6). The halt of a repository is a GitHub ruleset that
github-checker reads and writes (`halt-read` / `halt-set`); dispatcher owns
only the **halt request** — who, when, why, which repos — and never asserts
the effective state: it is read back from GitHub, per repo, every time.

A fleet halt is N independent operations. Confirmed results are kept on a
partial failure and nothing is rolled back; running the same request again
retries only what did not land (a repo already in the wanted state answers
`changed: false`). Repos added to `halt_fleet` after the last fleet request
are shown as deviations, not as covered.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ValidationError

from dispatcher.core.actions import ActionOutcome, ActionRejectedError
from dispatcher.core.background_reader import BackgroundReader, spawn_daemon
from dispatcher.core.human_queue import SourceStatus

HaltState = Literal["on", "off", "missing", "misconfigured", "unknown"]
Target = Literal["on", "off"]
#: The fleet read is ~2 GitHub calls per repo; at 60 s a 23-repo fleet spent
#: ~2800 calls an hour and, with the other readers, exhausted the account's
#: 5000/h core limit on 2026-10-01 (one repo then read `unknown`). The halt
#: changes almost only through our own toggle, which refreshes the read at
#: once (`invalidate`); a ruleset changed by hand in GitHub shows within this
#: window — as a deviation, never as a false `off`.
HALT_TTL_SECONDS = 600.0
REQUESTS_FILE = "halt-requests.jsonl"
REASON_MAX_LEN = 500


class RepoHalt(BaseModel):
    """One fleet repo's halt as last READ from GitHub."""

    repo: str  # directory name in the workspace
    state: HaltState
    ruleset_id: int | None = None
    detail: str | None = None


class RepoResult(BaseModel):
    """What one repo's write came back with (the read-back, not the write)."""

    repo: str
    ok: bool
    changed: bool | None
    state: HaltState
    error: str | None = None


class HaltRequest(BaseModel):
    """One human request to halt or lift — the only halt record dispatcher
    owns. `principal` is the forge profile of the dispatcher process: v1 is
    a single-operator, local dispatcher (spec §6.3), and says so."""

    request_id: str
    at: str
    target: Target
    scope: Literal["fleet", "repos"]
    repos: list[str]
    fleet_at_request: list[str]
    reason: str
    principal: str = "dispatcher process (its gh profile)"
    results: list[RepoResult] = []


class HaltView(BaseModel):
    """The fleet's halt: per-repo read-back, the last request, deviations."""

    fleet: list[RepoHalt]
    applying: str | None = None  # request id still being written
    halted: int  # repos read `on`
    unhealthy: list[str]  # repos read other than on/off
    deviations: list[str]
    last_request: HaltRequest | None
    sources: dict[str, SourceStatus]
    complete: bool
    generated_at: str


class HaltRejectedError(Exception):
    """A request refused before any repo was touched (HTTP 422)."""


class HaltBusyError(Exception):
    """Another halt request is still applying (HTTP 409)."""


class HaltStore:
    """Append-only JSONL of halt requests under `run_state_dir`."""

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / REQUESTS_FILE

    def append(self, request: HaltRequest) -> None:
        """Record one request; a failure to record raises — an unrecorded
        halt request must not be applied. Same modes as RunStore's tree
        (0700 / 0600): the journal carries reasons and the fleet."""
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(request.model_dump_json() + "\n")

    def last(self) -> tuple[HaltRequest | None, str | None]:
        """(newest readable request, problem). A torn or foreign line is
        named as a problem, never silently skipped past."""
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return None, None
        except OSError as err:
            return None, f"cannot read {self._path}: {err}"
        problem = None
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                return HaltRequest.model_validate(json.loads(line)), problem
            except (json.JSONDecodeError, ValidationError):
                problem = f"unreadable line in {self._path.name}"
        return None, problem


def read_fleet(
    fleet: tuple[str, ...], read: Callable[[str], ActionOutcome]
) -> list[RepoHalt]:
    """Read every fleet repo's halt; a failed read is `unknown`, never `off`."""
    out: list[RepoHalt] = []
    for repo in fleet:
        try:
            outcome = read(repo)
        except Exception as err:  # noqa: BLE001 — one repo's read, never the reader
            # BackgroundReader requires a fetch that never raises; a repo
            # whose read raised is `unknown`, and the rest are still read.
            detail = str(err) if isinstance(err, ActionRejectedError) else repr(err)
            out.append(RepoHalt(repo=repo, state="unknown", detail=detail))
            continue
        out.append(_repo_halt(repo, outcome))
    return out


def _repo_halt(repo: str, outcome: ActionOutcome) -> RepoHalt:
    halt = outcome.halt or {}
    state = halt.get("state")
    if state not in ("on", "off", "missing", "misconfigured", "unknown"):
        detail = outcome.error or f"halt-read did not answer ({outcome.phase})"
        return RepoHalt(repo=repo, state="unknown", detail=detail)
    return RepoHalt(
        repo=repo,
        state=state,
        ruleset_id=halt.get("ruleset_id"),
        detail=halt.get("detail") or (outcome.error if state == "unknown" else None),
    )


def _spawn(fn: Callable[[], None]) -> None:
    spawn_daemon(fn, "halt-read")


class HaltReader(BackgroundReader[list[RepoHalt] | None]):
    """The fleet's halt, read in the background (one `halt-read` per repo);
    `None` until the first read lands."""

    def __init__(
        self,
        fleet: tuple[str, ...],
        read: Callable[[str], ActionOutcome],
        *,
        ttl: float = HALT_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        spawn: Callable[[Callable[[], None]], None] = _spawn,
    ) -> None:
        super().__init__(
            lambda: read_fleet(fleet, read), None, ttl=ttl, clock=clock, spawn=spawn
        )


def build_halt_view(
    fleet: tuple[str, ...],
    reader: HaltReader | None,
    store: HaltStore | None,
    *,
    now: str,
    applying: str | None = None,
) -> HaltView:
    """The halt view; every hole is a named source state, HTTP 200 always."""
    sources: dict[str, SourceStatus] = {}
    repos: list[RepoHalt] = []
    if not fleet or reader is None:
        sources["forge_halt"] = SourceStatus(
            state="not_configured", detail="halt_fleet is empty"
        )
    else:
        try:
            read = reader.read()
        except Exception as exc:  # noqa: BLE001 — isolation IS the contract
            read = None
            sources["forge_halt"] = SourceStatus(
                state="unavailable", detail=f"{type(exc).__name__}: {exc}"
            )
        if read is None:
            sources.setdefault(
                "forge_halt",
                SourceStatus(state="unavailable", detail="first halt read in progress"),
            )
        else:
            repos = read
            unknown = [r.repo for r in repos if r.state == "unknown"]
            sources["forge_halt"] = (
                SourceStatus(state="partial", detail=f"unread: {', '.join(unknown)}")
                if unknown
                else SourceStatus(state="ok")
            )
    last: HaltRequest | None = None
    if store is None:
        sources["halt_requests"] = SourceStatus(
            state="not_configured", detail="run_state_dir is not set"
        )
    else:
        last, problem = store.last()
        sources["halt_requests"] = (
            SourceStatus(state="partial", detail=problem)
            if problem
            else SourceStatus(state="ok")
        )
    return HaltView(
        fleet=repos,
        applying=applying,
        halted=sum(1 for r in repos if r.state == "on"),
        unhealthy=[r.repo for r in repos if r.state not in ("on", "off")],
        deviations=_deviations(fleet, repos, last),
        last_request=last,
        sources=sources,
        complete=all(s.state in ("ok", "not_configured") for s in sources.values()),
        generated_at=now,
    )


def _deviations(
    fleet: tuple[str, ...], repos: list[RepoHalt], last: HaltRequest | None
) -> list[str]:
    """What the last request does not cover or no longer matches."""
    if last is None:
        return []
    out: list[str] = []
    if last.scope == "fleet":
        out += [
            f"{repo}: joined the fleet after the last fleet request"
            for repo in fleet
            if repo not in last.fleet_at_request
        ]
    out += [
        f"{repo}: left halt_fleet after the last request — its halt is no longer read"
        for repo in last.repos
        if repo not in fleet
    ]
    by_repo = {r.repo: r for r in repos}
    for repo in last.repos:
        seen = by_repo.get(repo)
        if seen is not None and seen.state not in (last.target, "unknown"):
            out.append(f"{repo}: requested {last.target}, reads {seen.state}")
    return out


class HaltApplier:
    """Applies one request at a time, off the request path.

    `start` validates and records the request synchronously (so a refusal is
    immediate and an accepted request is on disk before any write), then
    writes repo by repo in the background — a fleet is ~3 GitHub calls per
    repo, far longer than a client should hold a POST open. The finished
    request, with its per-repo read-back, is appended as a second line.
    """

    def __init__(
        self,
        fleet: tuple[str, ...],
        write: Callable[[str, str], ActionOutcome],
        store: HaltStore,
        *,
        on_applied: Callable[[], None] = lambda: None,
        spawn: Callable[[Callable[[], None]], None] = _spawn,
    ) -> None:
        self._fleet, self._write, self._store = fleet, write, store
        self._on_applied, self._spawn = on_applied, spawn
        self._lock = threading.Lock()
        self._applying: str | None = None

    @property
    def applying(self) -> str | None:
        """The request id being applied, if any."""
        with self._lock:
            return self._applying

    def start(
        self, target: str, repos: list[str] | None, reason: str, *, now: datetime
    ) -> HaltRequest:
        """Validate, record, and start writing; returns the recorded request."""
        request = self._validate(target, repos, reason, now)
        with self._lock:
            if self._applying is not None:
                raise HaltBusyError(f"halt request {self._applying} is still applying")
            self._applying = request.request_id
        try:
            # Recorded BEFORE the first write: a crash mid-fleet must leave
            # evidence of what was asked, even with no results.
            self._store.append(request)
            self._spawn(lambda: self._run(request))
        except Exception:
            with self._lock:
                self._applying = None
            raise
        return request

    def _run(self, request: HaltRequest) -> None:
        try:
            results = [self._one(repo, request.target) for repo in request.repos]
            self._store.append(request.model_copy(update={"results": results}))
        finally:
            with self._lock:
                self._applying = None
            self._on_applied()

    def _validate(
        self, target: str, repos: list[str] | None, reason: str, now: datetime
    ) -> HaltRequest:
        if target not in ("on", "off"):
            raise HaltRejectedError(f"state must be on|off, got {target!r}")
        why = reason.strip()
        if not why or len(why) > REASON_MAX_LEN or not why.isprintable():
            raise HaltRejectedError(
                f"a reason is required: one printable line, ≤{REASON_MAX_LEN} chars"
            )
        if not self._fleet:
            raise HaltRejectedError("halt_fleet is empty — nothing can be halted")
        if repos is None:
            scope: Literal["fleet", "repos"] = "fleet"
            chosen = list(self._fleet)
        else:
            outside = sorted(set(repos) - set(self._fleet))
            if not repos or outside:
                raise HaltRejectedError(
                    f"repos must be a non-empty subset of halt_fleet; not in it: "
                    f"{outside}"
                )
            scope, chosen = "repos", list(dict.fromkeys(repos))
        return HaltRequest(
            request_id=str(uuid.uuid4()),
            at=now.isoformat(),
            target=target,  # type: ignore[arg-type]
            scope=scope,
            repos=chosen,
            fleet_at_request=list(self._fleet),
            reason=why,
        )

    def _one(self, repo: str, target: str) -> RepoResult:
        try:
            outcome = self._write(repo, target)
        except Exception as exc:  # noqa: BLE001 — one repo must not stop the rest
            return RepoResult(
                repo=repo,
                ok=False,
                changed=False,
                state="unknown",
                error=f"{type(exc).__name__}: {exc}",
            )
        seen = _repo_halt(repo, outcome)
        return RepoResult(
            repo=repo,
            ok=outcome.ok and seen.state == target,
            changed=outcome.changed,
            state=seen.state,
            error=outcome.error,
        )
