"""Suggestion context bundle (DESIGN-901).

Deterministic and redacted: every string value passes mask_secrets before
serialization (H-4) — foreign config content flows into the model prompt
(H-5), but foreign SECRETS must not. Roadmap is deliberately absent: no
causal chain from roadmap signals to any typed-field value (spec §3).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from dispatcher.core.collectors.base import mask_secrets
from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.spec_runner_config import (
    TYPED_DEFAULTS,
    ProjectSpecRunnerConfig,
)

PROMPT_VERSION = "v1"
PEERS_PROJECT_CAP = 15
PEERS_VALUE_TOP = 5

INSTRUCTION = (
    "You are suggesting values for a Maestro spec_runner config. Reply "
    "with EXACTLY one JSON object, no prose, no markdown fence: "
    '{"suggestions": {"<field>": {"value": <typed value>, "rationale": '
    '"<one sentence>"}}}. Suggest ONLY fields listed in requested_fields. '
    "field_schema gives each field's type and default. peers gives value "
    "distributions across neighbour projects as lists of "
    "{value, count, explicit_count}; explicit_count is how many are "
    "deliberate human choices — the rest are copied defaults, so majority "
    "alone is not correctness. Ground each rationale in the distribution "
    "(e.g. '3 of 8 peers set this explicitly') and the project "
    "description. Omit a field rather than guess."
)


def build_suggest_bundle(
    cfg: ProjectSpecRunnerConfig,
    peers: list[ProjectSpecRunnerConfig],
    snapshot: ProjectSnapshot | None,
) -> dict[str, Any]:
    """Assemble the stdin document for the suggest CLI call (spec §3)."""
    selected = _select_peers(peers)
    project: dict[str, Any] = {"name": cfg.project}
    if snapshot is not None and snapshot.description:
        project["description"] = mask_secrets(snapshot.description)
        project["description_source"] = snapshot.description_source
    return {
        "instruction": INSTRUCTION,
        "prompt_version": PROMPT_VERSION,
        "requested_fields": [
            name for name in TYPED_DEFAULTS if not cfg.typed[name].explicit
        ],
        "field_schema": {
            name: {"type": type(default).__name__, "default": default}
            for name, default in TYPED_DEFAULTS.items()
        },
        "current_config": {
            name: {
                "value": mask_secrets(field.value, key=name),
                "explicit": field.explicit,
            }
            for name, field in cfg.typed.items()
        },
        "peers": {name: _distribution(selected, name) for name in TYPED_DEFAULTS},
        "project": project,
    }


def _select_peers(
    peers: list[ProjectSpecRunnerConfig],
) -> list[ProjectSpecRunnerConfig]:
    """All when <= cap, else freshest by base_mtime (observable rule)."""
    if len(peers) <= PEERS_PROJECT_CAP:
        return peers
    return sorted(peers, key=lambda p: p.base_mtime, reverse=True)[:PEERS_PROJECT_CAP]


def _distribution(
    peers: list[ProjectSpecRunnerConfig], name: str
) -> list[dict[str, Any]]:
    """Top-N value rows + {"other": N} tail; list-encoded so bool/int
    values stay typed (JSON object keys would stringify them)."""
    counts: Counter[Any] = Counter()
    explicit: Counter[Any] = Counter()
    for peer in peers:
        field = peer.typed[name]
        value = mask_secrets(field.value, key=name)
        counts[value] += 1
        if field.explicit:
            explicit[value] += 1
    # deterministic: count desc, then stable repr order
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], repr(kv[0])))
    rows: list[dict[str, Any]] = [
        {"value": value, "count": count, "explicit_count": explicit[value]}
        for value, count in ranked[:PEERS_VALUE_TOP]
    ]
    other = sum(count for _, count in ranked[PEERS_VALUE_TOP:])
    if other:
        rows.append({"other": other})
    return rows
