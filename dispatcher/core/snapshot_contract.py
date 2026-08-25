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

**Each version parses through its OWN model**, and that is the whole point of the split.
Parsing v2 through the v1 model — where ``github`` is an untyped dict — turns
``schema_version: 2`` into an unverified CLAIM by the producer: the axis is typed in the
vendored schema, and none of that survives. A forged payload then reads as "the
classification field is absent", every artifact drops into the unclassified bucket, and
the coverage number that decides when Robin switches axes is quietly wrong with nothing
failing. ``test_the_typed_models_reject_what_the_vendored_schema_rejects`` holds these
models against the pin so the two cannot drift apart in silence.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

# Strict for the v2 axis: the pin declares `number` an integer, `dirty` a boolean,
# `severity` an enum. Pydantic's default lax mode would coerce `"12"` into `12` and
# accept a payload the contract forbids — a consumer that repairs its producer's
# output has stopped being able to report that the producer is broken.
_V2_CONFIG = ConfigDict(extra="allow", strict=True)

SUPPORTED_SCHEMA_VERSIONS = (1, 2)
# v2 is the first version carrying the epic axis (ADR-ECO-010 Ф2).
EPIC_AXIS_SCHEMA_VERSION = 2


class SnapshotContractError(Exception):
    """A snapshot payload that must not be consumed (bad JSON, wrong version, wrong shape)."""


class LocalStatusV1(BaseModel):
    """State of one local clone relative to its upstream.

    `branch`/`ahead`/`behind` are nullable but REQUIRED, and the difference is the
    point: `null` means the producer looked and there is no upstream to compare
    against, while an absent key means it never looked. Giving them defaults
    collapsed the two, and a consumer cannot then tell "no upstream" from "not
    measured" — `error` is the only genuinely optional field here (both pins).
    """

    model_config = ConfigDict(extra="allow")

    branch: str | None
    ahead: int | None
    behind: int | None
    dirty: bool
    error: str | None = None


# ------------------------------------------------------- shapes shared by v1/v2
# Identical in both pins; kept in one place so a re-vendoring cannot leave two
# copies of the same shape drifting apart.


class CopilotReviewV2(BaseModel):
    """Summary of GitHub Copilot's review on a pull request."""

    model_config = _V2_CONFIG

    state: str
    comment_count: int


class BranchV2(BaseModel):
    model_config = _V2_CONFIG

    name: str


class RulesetInfoV2(BaseModel):
    model_config = _V2_CONFIG

    id: int
    name: str
    enforcement: str
    target: str


# --------------------------------------------------------------------- v1 github


class IssueV1(BaseModel):
    """An open issue as contract v1 publishes it — no epic axis."""

    model_config = ConfigDict(extra="allow")

    number: int
    title: str
    author: str
    labels: list[str] = []


class PullRequestV1(BaseModel):
    model_config = ConfigDict(extra="allow")

    number: int
    title: str
    author: str
    head_branch: str
    is_dependabot: bool
    copilot_review: CopilotReviewV2 | None = None


class RepoStateV1(BaseModel):
    """The GitHub half of one repository under contract v1.

    Typed for the same reason v2 is: an untyped `dict[str, Any]` here meant the whole
    GitHub side of the version this fleet ACTUALLY publishes today went unchecked. The
    nullability of `issues` is load-bearing and survives into the read-model — `null`
    is "the list was not retrieved", `[]` is "retrieved, nothing open".
    """

    model_config = ConfigDict(extra="allow")

    name: str
    pulls: list[PullRequestV1] = []
    issues: list[IssueV1] | None = None
    branches: list[BranchV2] = []
    alerts: int | None = None
    rulesets: list[RulesetInfoV2] | None = None
    error: str | None = None
    updated_at: str | None = None
    path: str | None = None
    local: LocalStatusV1 | None = None


class RepoSnapshotV1(BaseModel):
    """One workspace repository: local git state plus optional GitHub state."""

    model_config = ConfigDict(extra="allow")

    dir: str
    remote: str | None
    local: LocalStatusV1
    github: RepoStateV1 | None = None


# --------------------------------------------------------------------- v2 axis


class EpicDiagnosticV2(BaseModel):
    """One instance of a `diagnostics.yaml` code, with its COMPUTED severity."""

    model_config = _V2_CONFIG

    code: str
    severity: Literal["warning", "error"]
    message: str
    subject_uri: str | None = None
    raw: str | None = None


class EpicClassificationV2(BaseModel):
    """The normalized per-artifact object of `classification.schema.json`.

    Every field is required on purpose: the four-state ``classification`` only carries
    information as long as its absence is impossible. An optional field with a `None`
    default would let "the producer did not say" and "the producer said nothing applies"
    arrive as the same value — which is the exact distinction this object exists for.
    """

    model_config = _V2_CONFIG

    epic: str | None
    defect: str | None
    classification: Literal["tagged", "missing", "invalid", "unavailable"]
    diagnostics: list[EpicDiagnosticV2]
    subject_uri: str | None
    carrier: Literal["pull_request", "issue"]
    observed_at: str | None


class IssueV2(BaseModel):
    """An open issue (pull requests excluded) with its classification."""

    model_config = _V2_CONFIG

    number: int
    title: str
    author: str
    labels: list[str] = []
    epic: EpicClassificationV2


class PullRequestV2(BaseModel):
    model_config = _V2_CONFIG

    number: int
    title: str
    author: str
    head_branch: str
    is_dependabot: bool
    copilot_review: CopilotReviewV2 | None = None
    epic: EpicClassificationV2


class MergedPullRequestV2(BaseModel):
    """One merged PR of the attribution window: `commit → PR` without heuristics."""

    model_config = _V2_CONFIG

    number: int
    merge_commit_sha: str | None
    commit_shas: list[str]
    commit_shas_truncated: bool
    # kept as the raw string rather than a datetime: this value is only ever compared
    # to other values from the same producer, and a parse/reserialize round-trip would
    # let the vendored fixtures diverge from their pin over formatting alone
    merged_at: str
    epic: EpicClassificationV2


class MergedPrWindowV2(BaseModel):
    """Attribution transport for robin; dispatcher must not read it as state.

    ``truncated`` is explicit so a cut-off window is never mistaken for an empty one.
    """

    model_config = _V2_CONFIG

    window_days: int
    truncated: bool
    prs: list[MergedPullRequestV2] = []


class RepoStateV2(BaseModel):
    """The GitHub half of one repository, as snapshot v2 publishes it."""

    model_config = _V2_CONFIG

    name: str
    pulls: list[PullRequestV2] = []
    issues: list[IssueV2] | None = None
    branches: list[BranchV2] = []
    alerts: int | None = None
    rulesets: list[RulesetInfoV2] | None = None
    merged: MergedPrWindowV2 | None = None
    error: str | None = None
    updated_at: str | None = None
    path: str | None = None
    local: LocalStatusV1 | None = None


class RepoSnapshotV2(BaseModel):
    """One workspace repository under contract v2 — GitHub state fully typed."""

    model_config = _V2_CONFIG

    dir: str
    remote: str | None
    local: LocalStatusV1
    github: RepoStateV2 | None = None


# ------------------------------------------------------------------ envelopes


class _SnapshotEnvelope(BaseModel):
    """Fields every version shares — everything a caller may read without branching."""

    # deliberately NOT strict: `generated_at` arrives as an ISO string in every
    # version, and v1's envelope has always been lax. Strictness belongs where the
    # pin declares concrete scalar types — the v2 axis — not on the shared frame.
    model_config = ConfigDict(extra="allow")

    schema_version: int
    workspace: str
    host: str
    generated_at: datetime
    # required-but-nullable, same reasoning as LocalStatus: `null` is the producer
    # saying GitHub was queried fine, an absent key is it saying nothing at all
    gh_error: str | None

    def age_seconds(self, now: datetime | None = None) -> float:
        """Age of this snapshot; staleness is data, not an error."""
        moment = now if now is not None else datetime.now(UTC)
        generated = self.generated_at
        if generated.tzinfo is None:
            # naive timestamps predate contract v1's tz-aware rule; compare in
            # local time rather than guessing a zone
            moment = moment.astimezone().replace(tzinfo=None)
        return (moment - generated).total_seconds()


class WorkspaceSnapshotV1(_SnapshotEnvelope):
    """Full fleet state of one host, as frozen by snapshot contract v1."""

    repos: list[RepoSnapshotV1]


class WorkspaceSnapshotV2(_SnapshotEnvelope):
    """Full fleet state of one host under contract v2 — carries the epic axis."""

    repos: list[RepoSnapshotV2]


Snapshot = WorkspaceSnapshotV1 | WorkspaceSnapshotV2
AnyRepoSnapshot = RepoSnapshotV1 | RepoSnapshotV2

_MODEL_BY_VERSION: dict[int, type[WorkspaceSnapshotV1] | type[WorkspaceSnapshotV2]] = {
    1: WorkspaceSnapshotV1,
    2: WorkspaceSnapshotV2,
}


def parse_snapshot(payload: str) -> Snapshot:
    """Parse and validate one snapshot JSON document against ITS OWN version's model.

    The version is read first and decides the model, rather than one permissive model
    accepting both: a payload is only as trustworthy as the shape it was checked
    against, and checking v2 against v1 checks the axis against nothing.

    Raises:
        SnapshotContractError: On invalid JSON, an unsupported ``schema_version``, or a
            payload that does not match the shape it claims — the caller renders
            ``unknown(schema)``, it must not degrade silently.
    """
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError as err:
        raise SnapshotContractError(f"snapshot is not valid JSON: {err}") from err
    if not isinstance(raw, dict):
        raise SnapshotContractError(
            f"snapshot must be a JSON object, got {type(raw).__name__}"
        )
    version = raw.get("schema_version")
    # `isinstance(True, int)` is True and `True == 1`, so a membership test alone
    # routes `schema_version: true` into the v1 model, where lax validation rounds it
    # off to 1. The version is the one field that decides how everything else is
    # checked; it does not get to be approximately right.
    if not isinstance(version, int) or isinstance(version, bool):
        raise SnapshotContractError(
            f"schema_version must be an integer, got {version!r} "
            f"({type(version).__name__})"
        )
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        supported = ", ".join(f"v{v}" for v in SUPPORTED_SCHEMA_VERSIONS)
        raise SnapshotContractError(
            f"unsupported schema_version={version!r}; "
            f"this consumer is pinned to {supported} "
            "(contracts/github-checker-snapshot/)"
        )
    model = _MODEL_BY_VERSION[version]
    try:
        return model.model_validate(raw)
    except ValidationError as err:
        raise SnapshotContractError(
            f"snapshot claims schema_version={version} but does not match the "
            f"github-checker snapshot contract for it: {err}"
        ) from err


def carries_epic_axis(snapshot: Snapshot) -> bool:
    """Whether this snapshot's producer publishes the epic classification at all.

    The distinction a consumer must not lose: "the producer does not publish epics" is
    not "the artifacts have no epics". One is a gap in observation, the other a fact
    about the fleet, and an aggregate that mixes them is wrong in a way nobody can see.
    """
    return isinstance(snapshot, WorkspaceSnapshotV2)
