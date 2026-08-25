"""Ingestion of github-checker workspace snapshots (vendored contracts v1 and v2).

The JSON shape is owned by the external producing repo github-checker; the pinned
copies live in ``contracts/github-checker-snapshot/{v1,v2}/`` (DESIGN-201, ADR-ECO-010).
Ingestion is strict about the version — anything outside the supported set is an explicit
:class:`SnapshotContractError`, never a best-effort parse — and tolerant about additive
fields (``extra="allow"``: compatible additions must not break this consumer).

**Two versions on purpose, not by inertia.** v2 adds the stream axis (an epic
classification on every issue and pull request, plus a merged-PR attribution window). A
producer still publishing v1 is a SUPPORTED state, not a failure: its GitHub planes simply
carry no epic data, and consumers must render that as ``unavailable``. Refusing v1 would
turn a neighbour's unhurried upgrade into this dispatcher's outage; silently treating it
as "no epics" would turn it into a wrong number. The version travels with the parsed
snapshot so callers can tell the two apart.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

SUPPORTED_SCHEMA_VERSIONS = (1, 2)
# v2 is the first version carrying the epic axis (ADR-ECO-010 Ф2).
EPIC_AXIS_SCHEMA_VERSION = 2


class SnapshotContractError(Exception):
    """A snapshot payload that must not be consumed (bad JSON, wrong version)."""


class LocalStatusV1(BaseModel):
    """State of one local clone relative to its upstream."""

    model_config = ConfigDict(extra="allow")

    branch: str | None = None
    ahead: int | None = None
    behind: int | None = None
    dirty: bool = False
    error: str | None = None


class RepoSnapshotV1(BaseModel):
    """One workspace repository: local git state plus optional GitHub state."""

    model_config = ConfigDict(extra="allow")

    dir: str
    remote: str | None = None
    local: LocalStatusV1
    github: dict[str, Any] | None = None


class WorkspaceSnapshotV1(BaseModel):
    """Full fleet state of one host, as frozen by snapshot contract v1."""

    model_config = ConfigDict(extra="allow")

    schema_version: int
    workspace: str
    host: str
    generated_at: datetime
    gh_error: str | None = None
    repos: list[RepoSnapshotV1] = []

    def age_seconds(self, now: datetime | None = None) -> float:
        """Age of this snapshot; staleness is data, not an error."""
        moment = now if now is not None else datetime.now(UTC)
        generated = self.generated_at
        if generated.tzinfo is None:
            # naive timestamps predate contract v1's tz-aware rule; compare in
            # local time rather than guessing a zone
            moment = moment.astimezone().replace(tzinfo=None)
        return (moment - generated).total_seconds()


def parse_snapshot(payload: str) -> WorkspaceSnapshotV1:
    """Parse and validate one snapshot JSON document.

    Raises:
        SnapshotContractError: On invalid JSON/shape or an unsupported
            ``schema_version`` — the caller renders ``unknown(schema)``,
            it must not degrade silently.
    """
    try:
        snapshot = WorkspaceSnapshotV1.model_validate_json(payload)
    except ValidationError as err:
        raise SnapshotContractError(
            f"snapshot does not match the github-checker snapshot contract: {err}"
        ) from err
    if snapshot.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        supported = ", ".join(f"v{v}" for v in SUPPORTED_SCHEMA_VERSIONS)
        raise SnapshotContractError(
            f"unsupported schema_version={snapshot.schema_version!r}; "
            f"this consumer is pinned to {supported} "
            "(contracts/github-checker-snapshot/)"
        )
    return snapshot


def carries_epic_axis(snapshot: WorkspaceSnapshotV1) -> bool:
    """Whether this snapshot's producer publishes the epic classification at all.

    The distinction a consumer must not lose: "the producer does not publish epics" is
    not "the artifacts have no epics". One is a gap in observation, the other a fact
    about the fleet, and an aggregate that mixes them is wrong in a way nobody can see.
    """
    return snapshot.schema_version >= EPIC_AXIS_SCHEMA_VERSION
