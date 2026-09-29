"""Human queue — every wait for a human dispatcher can see (slice A1).

Spec: docs/superpowers/specs/2026-09-29-human-queue-a1-design.md. This module
holds the model and `assemble`, which owns every invariant of spec §4.1:
merge by key, ordering, completeness. It reads nothing — the adapters in
`human_queue_sources` do.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator

SourceState = Literal["ok", "partial", "unavailable", "not_configured", "not_connected"]
Reason = Literal[
    "launch_unknown",
    "run_needs_review",
    "run_awaiting_approval",
    "loop_needs_human",
    "proposal_gate",
    "backlog_gate",
]

# `not_configured` = the feature is off, so nothing can wait there; it is not
# incompleteness. Every other non-ok state is.
_COMPLETE_STATES = frozenset({"ok", "not_configured"})


class SourceStatus(BaseModel):
    """Health of one queue source: `ok`, or why its waits may be missing."""

    state: SourceState
    detail: str | None = None


class RunViewAct(BaseModel):
    """Open dispatcher's own run view (where `/resolve` and verbs live)."""

    kind: Literal["run_view"] = "run_view"
    request_id: str


class MaestroVerbAct(BaseModel):
    """A maestro verb for a run dispatcher did not launch; B renders it."""

    kind: Literal["maestro_verb"] = "maestro_verb"
    verb: Literal["retry", "approve"]
    task_id: str
    run_id: str
    repo_key: str


class OpenArtifactAct(BaseModel):
    """The decision is recorded at the source; dispatcher only points to it."""

    kind: Literal["open_artifact"] = "open_artifact"
    path: str


Act = Annotated[
    RunViewAct | MaestroVerbAct | OpenArtifactAct, Field(discriminator="kind")
]


class HumanWait(BaseModel):
    """One item waiting on a human, in the wire shape shared with later slices.

    Invariants: `since` and `since_basis` are both set or both null; a set
    `since` is a timezone-aware ISO-8601 time from a proven wait start only;
    the `key` formats are a public contract (spec §4.1).
    """

    key: str
    reasons: list[Reason]
    source: str
    repo: str | None
    ref: str
    title: str
    since: str | None
    since_basis: str | None
    act: Act

    @model_validator(mode="after")
    def _since_is_proven_and_sortable(self) -> HumanWait:
        if (self.since is None) != (self.since_basis is None):
            raise ValueError("since and since_basis are both set or both null")
        if self.since is not None:
            if datetime.fromisoformat(self.since).tzinfo is None:
                raise ValueError("since must carry a timezone")
        return self


class HumanQueueView(BaseModel):
    """The merged human queue: waits, per-source status, completeness."""

    waits: list[HumanWait]
    sources: dict[str, SourceStatus]
    complete: bool
    generated_at: str


class SourceResult(BaseModel):
    """One adapter's output: its name, status and the waits it found."""

    name: str
    status: SourceStatus
    waits: list[HumanWait] = Field(default_factory=list)


def proven_since(value: str | None, basis: str) -> tuple[str | None, str | None]:
    """`(since, since_basis)` from a source time, or `(None, None)`.

    A value that is empty, unparseable, or has no timezone cannot be ordered
    against the others; it is dropped to "age unknown" rather than guessed.
    """
    if not value:
        return None, None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None, None
    if parsed.tzinfo is None:
        return None, None
    return parsed.isoformat(), basis


def assemble(results: list[SourceResult], *, now: str) -> HumanQueueView:
    """One view over all sources (spec §4.1)."""
    merged: dict[str, HumanWait] = {}
    for result in results:
        for wait in result.waits:
            prior = merged.get(wait.key)
            if prior is None:
                merged[wait.key] = wait
                continue
            union = sorted(set(prior.reasons) | set(wait.reasons))
            merged[wait.key] = prior.model_copy(update={"reasons": union})
    known = sorted(
        (w for w in merged.values() if w.since is not None),
        key=lambda w: (datetime.fromisoformat(w.since or ""), w.key),
    )
    unknown = sorted(
        (w for w in merged.values() if w.since is None), key=lambda w: w.key
    )
    sources = {r.name: r.status for r in results}
    return HumanQueueView(
        waits=[*known, *unknown],
        sources=sources,
        complete=all(s.state in _COMPLETE_STATES for s in sources.values()),
        generated_at=now,
    )
