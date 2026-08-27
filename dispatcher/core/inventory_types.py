"""Frozen captured-fact shapes shared by the IO capture, the pure classifier
and the subset discriminator — import-light by design, B2 review M5.
"""

from __future__ import annotations

from dataclasses import dataclass

from dispatcher.core.run_identity import RepoKey


@dataclass(frozen=True)
class Accepted:
    repo_path: str | None  # `repo:` — a checkout path (submit semantics)
    repo_url: str | None  # `repo_url:` — wins when both are present


@dataclass(frozen=True)
class Rejected:
    reason: str


DagSubsetVerdict = Accepted | Rejected


@dataclass(frozen=True)
class PlanItem:
    item_id: str | None
    line: int
    open: bool  # `- [ ]` vs anything else
    shipped: bool  # under a `##`-level section whose title contains "Shipped"
    dag_raw: str | None  # the @dag tag as the tags map holds it (last-wins)
    dag_tag: str | None  # validated value (grammar + equality), else None
    dag_diag: str | None  # "PF-DAG-GRAMMAR" | "PF-DAG-MISMATCH" | None


@dataclass(frozen=True)
class DagFileInfo:
    rel_path: str  # "dags/<name>.yaml"
    is_regular: bool  # fstat on the opened fd: regular file
    text: str | None  # decoded from the SAME captured bytes; None if undecodable
    blob_sha: str | None  # `git hash-object --stdin` over the SAME bytes
    head_blob_sha: str | None  # blob at <head_revision>:<rel>; None = ABSENT
    subset: DagSubsetVerdict | None  # classify_dag_text(text); None when text is None
    named_repo: RepoKey | None  # the repo the DAG names, resolved at capture
    named_repo_error: str | None  # why resolution failed, when it did
    error: str | None  # named IO/git FAILURE for THIS file


@dataclass(frozen=True)
class InventorySurface:
    plan_items: tuple[PlanItem, ...]  # FULL ledger, Shipped included
    dag_files: tuple[DagFileInfo, ...]
    head_revision: str | None  # full 40-hex; None on capture failure
    repo_key: RepoKey | None  # None on identity failure
    plan_error: str | None  # TODO.md unreadable
    dag_dir_error: str | None  # dags/ dir unreadable (absent dir is not an error)
    capture_error: str | None  # HEAD/identity failure, named
