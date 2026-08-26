"""Supported DAG subset (spec §6.1) — a structural predicate, not validation.

dispatcher does not vendor maestro's schema. Accepted means "shaped like a
Mode-1 ProjectConfig as far as launchpad needs"; authoritative validation
stays with maestro at launch. `workstreams:` is the sole Mode-2 marker
(OrchestratorConfig requires it, ProjectConfig lacks it); `repo_url:` is
legal Mode-1 remote-URL naming, not a Mode-2 marker — it mirrors the
`repo`/`repo_url` precedence submit already uses (PR#202 review ruling).
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
    # Precedence mirrors _reconcile_repo: try repo_url first (if valid string),
    # then fall back to repo. A present-but-invalid field (non-string, empty,
    # whitespace-only) is treated as absent, not rejected.
    repo_url = doc.get("repo_url")
    repo = doc.get("repo")

    # Phase 1: check if repo_url is usable (string with non-whitespace content)
    if isinstance(repo_url, str) and repo_url.strip():
        result_url = repo_url
        result_path = None
    else:
        # repo_url is absent or invalid; fall back to repo
        result_url = None
        if isinstance(repo, str) and repo.strip():
            result_path = repo
        else:
            # Neither field yields a usable value
            return Rejected(
                "names no repository ('repo:'/'repo_url:') — "
                "maestro would refuse this DAG for the same reason"
            )

    if not isinstance(doc.get("tasks"), list):
        return Rejected("top-level 'tasks:' list is required")
    return Accepted(repo_path=result_path, repo_url=result_url)
