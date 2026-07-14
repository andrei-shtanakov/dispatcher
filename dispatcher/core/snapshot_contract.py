"""Ingestion of github-checker workspace snapshots (vendored contract v1).

The JSON shape is owned by the external producing repo github-checker; the
pinned copy lives in ``contracts/github-checker-snapshot/v1/`` (DESIGN-201).
Ingestion is strict about the version — anything but ``schema_version: 1`` is
an explicit :class:`SnapshotContractError`, never a best-effort parse — and
tolerant about additive fields (``extra="allow"``: compatible v1 additions
must not break this consumer).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

SUPPORTED_SCHEMA_VERSION = 1


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
            f"snapshot does not match contract v1: {err}"
        ) from err
    if snapshot.schema_version != SUPPORTED_SCHEMA_VERSION:
        raise SnapshotContractError(
            f"unsupported schema_version={snapshot.schema_version!r}; "
            f"this consumer is pinned to v{SUPPORTED_SCHEMA_VERSION} "
            "(contracts/github-checker-snapshot/v1/)"
        )
    return snapshot
