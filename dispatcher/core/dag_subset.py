"""Supported DAG subset (spec §6.1) — a structural predicate, not validation.

dispatcher does not vendor maestro's schema. Accepted means "shaped like a
Mode-1 ProjectConfig as far as launchpad needs"; authoritative validation
stays with maestro at launch. `workstreams:`/`repo_url:` are Mode-2 markers
(OrchestratorConfig requires them, ProjectConfig lacks them).
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

_MAX_DAG_BYTES = 1024 * 1024  # a plan DAG is text; anything bigger is refused
_MODE2_MARKER = "workstreams"  # recorded ruling: repo_url is legal Mode-1


@dataclass(frozen=True)
class Accepted:
    repo_path: str | None  # `repo:` — a checkout path (submit semantics)
    repo_url: str | None  # `repo_url:` — wins when both are present


@dataclass(frozen=True)
class Rejected:
    reason: str


DagSubsetVerdict = Accepted | Rejected


def classify_dag_text(text: str) -> DagSubsetVerdict:
    """Classify one DAG file's text against the supported subset."""
    if len(text.encode("utf-8", errors="replace")) > _MAX_DAG_BYTES:
        return Rejected(f"file exceeds {_MAX_DAG_BYTES} bytes")
    try:
        # phase 1: event scan — no construction, aliases NOT expanded here.
        # PyYAML's safe_load DOES expand aliases (billion-laughs fits inside
        # the size cap), so anchors/aliases are refused before construction.
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent):
                return Rejected("YAML aliases are not in the supported subset")
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return Rejected(f"not parseable as YAML: {exc}")
    if not isinstance(doc, dict):
        return Rejected("top level is not a mapping")
    if _MODE2_MARKER in doc:
        return Rejected(
            f"'{_MODE2_MARKER}:' present — a Mode-2 marker, "
            "not the supported Mode-1 subset"
        )
    repo_url = doc.get("repo_url")
    repo = doc.get("repo")
    url_ok = isinstance(repo_url, str) and bool(repo_url.strip())
    path_ok = isinstance(repo, str) and bool(repo.strip())
    if repo_url is not None and not url_ok:
        return Rejected("'repo_url:' must be a non-empty string")
    if not url_ok and repo is not None and not path_ok:
        return Rejected("'repo:' must be a non-empty string")
    if not url_ok and not path_ok:
        return Rejected(
            "names no repository ('repo:'/'repo_url:') — "
            "maestro would refuse this DAG for the same reason"
        )
    if not isinstance(doc.get("tasks"), list):
        return Rejected("top-level 'tasks:' list is required")
    return Accepted(
        repo_path=repo if path_ok else None,
        repo_url=repo_url if url_ok else None,
    )
