"""Human queue model invariants (spec 2026-09-29-human-queue-a1-design §4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dispatcher.core.human_queue import (
    HumanWait,
    OpenArtifactAct,
    Reason,
    SourceResult,
    SourceStatus,
    assemble,
    proven_since,
)

_NOW = "2026-09-29T12:00:00+00:00"


def _wait(
    key: str, since: str | None = None, reason: Reason = "proposal_gate"
) -> HumanWait:
    return HumanWait(
        key=key,
        reasons=[reason],
        source="impresario",
        repo="impresario",
        ref=key,
        title=key,
        since=since,
        since_basis=None if since is None else "test",
        act=OpenArtifactAct(path=f"{key}.yaml"),
    )


def _ok(name: str, *waits: HumanWait) -> SourceResult:
    return SourceResult(name=name, status=SourceStatus(state="ok"), waits=list(waits))


def test_known_ages_first_oldest_first_then_unknown_by_key() -> None:
    view = assemble(
        [
            _ok(
                "impresario",
                _wait("b-unknown"),
                _wait("new", "2026-09-28T00:00:00+00:00"),
                _wait("a-unknown"),
                _wait("old", "2026-09-01T00:00:00Z"),
            )
        ],
        now=_NOW,
    )
    assert [w.key for w in view.waits] == ["old", "new", "a-unknown", "b-unknown"]


def test_since_sorts_by_instant_not_text() -> None:
    """'10:00+02:00' is 08:00Z — earlier than '09:00Z', though it sorts
    after it as text. A string sort puts 'utc' first and fails."""
    view = assemble(
        [
            _ok(
                "impresario",
                _wait("utc", "2026-09-01T09:00:00Z"),
                _wait("plus2", "2026-09-01T10:00:00+02:00"),
            )
        ],
        now=_NOW,
    )
    assert [w.key for w in view.waits] == ["plus2", "utc"]


def test_same_key_merges_and_unions_reasons() -> None:
    first = _wait("pr:7", reason="proposal_gate")
    second = _wait("pr:7", reason="backlog_gate")
    view = assemble([_ok("a", first), _ok("b", second)], now=_NOW)
    assert len(view.waits) == 1
    assert view.waits[0].reasons == ["backlog_gate", "proposal_gate"]
    assert view.waits[0].source == "impresario"


@pytest.mark.parametrize(
    ("states", "complete"),
    [
        (["ok", "not_configured"], True),
        (["ok", "partial"], False),
        (["ok", "unavailable"], False),
        (["ok", "not_connected"], False),
    ],
)
def test_complete_only_when_every_source_is_ok_or_off(
    states: list[str], complete: bool
) -> None:
    results = [
        SourceResult(name=f"s{i}", status=SourceStatus.model_validate({"state": s}))
        for i, s in enumerate(states)
    ]
    assert assemble(results, now=_NOW).complete is complete


def test_since_and_basis_are_paired() -> None:
    with pytest.raises(ValidationError):
        HumanWait(
            key="k",
            reasons=["proposal_gate"],
            source="impresario",
            repo=None,
            ref="k",
            title="k",
            since="2026-09-01T00:00:00Z",
            since_basis=None,
            act=OpenArtifactAct(path="p"),
        )


def test_naive_since_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _wait("k", "2026-09-01T00:00:00")


def test_proven_since_normalizes_or_drops() -> None:
    assert proven_since("2026-09-01T00:00:00Z", "b") == (
        "2026-09-01T00:00:00+00:00",
        "b",
    )
    assert proven_since(None, "b") == (None, None)
    assert proven_since("", "b") == (None, None)
    assert proven_since("not a time", "b") == (None, None)
    assert proven_since("2026-09-01T00:00:00", "b") == (None, None)
