"""`RunRequest` v0 — the one record dispatcher owns (spec §3.1, §4).

Validation is fail-closed and happens before anything is launched or locked:
a request that cannot be resolved to a checkout, a commit and a task file
inside that commit is refused, and no state is written for it.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.run_identity import (
    IdentityError,
    RepoKey,
    identity_from_checkout,
    parse_remote_url,
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_DIR_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")
# The spec calls request_id a uuid4 (client-generated idempotency key), but
# the charset — not the version — is what dispatcher depends on: RunStore
# keys a JSON record and a lock file off this string
# (`dispatcher/core/run_store.py:78-83`), so anything outside it must never
# reach that layer. A uuid4 always matches; a path separator or whitespace
# never does.
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_GIT_TIMEOUT = 15


class RunRejectedError(Exception):
    """The request cannot be honoured; nothing was launched (→ 422)."""


class Ref(BaseModel):
    """A provenance pointer. `commit` defaults to the request's `revision`."""

    path: str
    commit: str | None = None


class RunRequest(BaseModel):
    """One request to start one Mode-1 run (spec §4).

    `run_id` is deliberately absent: maestro allocates it
    (`maestro/maestro/run_bootstrap.py:103`), so a request cannot carry one.
    `pr_ref`/`verdict_ref` are absent for the same class of reason — they
    come into existence after the run and belong to the outcome record.
    """

    request_id: str = Field(pattern=_REQUEST_ID_RE.pattern)
    work_id: str
    repository: str
    revision: str
    tasks: str
    spec_ref: Ref | None = None
    plan_ref: Ref | None = None


@dataclass(frozen=True)
class ValidatedRequest:
    request: RunRequest
    checkout: Path
    key: RepoKey
    spec_commit: str | None
    plan_commit: str | None


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as err:
        raise RunRejectedError(f"cannot run git: {err}") from err


def _checkout(repository: str, config: DispatcherConfig) -> Path:
    """Resolve a workspace repo the way `ActionRunner._target` does."""
    if not _SAFE_DIR_RE.fullmatch(repository) or repository in (".", ".."):
        raise RunRejectedError(f"unsafe repository name: {repository!r}")
    workspace = next((r for r in config.roots if r.is_dir()), None)
    if workspace is None:
        raise RunRejectedError("no existing workspace root configured")
    target = workspace / repository
    if not (target / ".git").exists():
        raise RunRejectedError(f"not a git repo in workspace: {repository}")
    return target


def validate_request(request: RunRequest, config: DispatcherConfig) -> ValidatedRequest:
    """Refuse anything that cannot be launched reproducibly (spec §4.1–4.2)."""
    if not _SHA_RE.fullmatch(request.revision):
        raise RunRejectedError(
            f"revision must be a full 40-hex commit sha, got {request.revision!r}: "
            "a ref would make the request non-reproducible on retry"
        )
    checkout = _checkout(request.repository, config)

    tasks = request.tasks
    if tasks.startswith("/") or ".." in Path(tasks).parts or not tasks.strip():
        raise RunRejectedError(
            f"tasks must be a repo-relative path without '..', got {tasks!r}"
        )

    try:
        key = identity_from_checkout(checkout)
    except IdentityError as err:
        raise RunRejectedError(str(err)) from err

    head = _git(checkout, "rev-parse", "HEAD")
    if head.returncode != 0:
        raise RunRejectedError(f"cannot read HEAD of {request.repository}")
    if head.stdout.strip() != request.revision:
        # Slice 0 does not move a neighbour's checkout: the run must happen at
        # the revision the requester named, and silently checking out someone
        # else's working tree is a mutation nobody asked for.
        raise RunRejectedError(
            f"{request.repository} checkout is at {head.stdout.strip()[:12]}, "
            f"not the requested {request.revision[:12]}"
        )

    probe = _git(checkout, "cat-file", "-e", f"{request.revision}:{tasks}")
    if probe.returncode != 0:
        raise RunRejectedError(
            f"tasks {tasks!r} is not present in {request.revision[:12]}"
        )

    _reconcile_repo(checkout, request.revision, tasks, key)

    return ValidatedRequest(
        request=request,
        checkout=checkout,
        key=key,
        spec_commit=_ref_commit(request.spec_ref, request.revision),
        plan_commit=_ref_commit(request.plan_ref, request.revision),
    )


def _reconcile_repo(
    checkout: Path, revision: str, tasks: str, expected: RepoKey
) -> None:
    """Refuse a `tasks.yaml` that would launch under a different repository.

    maestro does not key a Mode-1 run by the caller's cwd: `bootstrap_run`
    calls `identity_from_config(config)`, which for a Mode-1 DAG reads the
    required `repo:` field of the YAML and takes THAT checkout's `origin`
    (`maestro/maestro/run_bootstrap.py:68`, `maestro/maestro/models.py:871`,
    `maestro/maestro/repo_identity.py:103-135`). Nothing before this call
    reads that field, so dispatcher's `RepoKey` (from `request.repository`)
    and maestro's own could name two different repositories with nothing
    reconciling them — misplacing the lock, the `runs_dir()` watch, and the
    revision guard all at once (review finding C1).

    Refusing an absent/unparseable/unresolvable `repo:`/`repo_url:` is not a
    widening: maestro would exit non-zero on it too
    (`maestro/maestro/cli.py:590-592`), and refusing here costs nothing
    while a failed launch costs the repository lock (see I1).
    """
    show = _git(checkout, "show", f"{revision}:{tasks}")
    if show.returncode != 0:
        raise RunRejectedError(
            f"cannot read {tasks!r} at {revision[:12]} to check its repo: "
            f"{show.stderr.strip()}"
        )
    try:
        doc = yaml.safe_load(show.stdout)
    except yaml.YAMLError as err:
        raise RunRejectedError(f"{tasks!r} is not valid YAML: {err}") from err
    if not isinstance(doc, dict):
        raise RunRejectedError(f"{tasks!r} does not parse to a mapping")

    repo_url = doc.get("repo_url")
    if isinstance(repo_url, str) and repo_url.strip():
        try:
            named = parse_remote_url(repo_url)
        except IdentityError as err:
            raise RunRejectedError(
                f"{tasks!r} repo_url is unresolvable: {err}"
            ) from err
    else:
        repo_field = doc.get("repo")
        if not isinstance(repo_field, str) or not repo_field.strip():
            raise RunRejectedError(
                f"{tasks!r} names no repository (`repo:`/`repo_url:`); "
                "maestro would refuse this DAG for the same reason"
            )
        try:
            named = identity_from_checkout(Path(repo_field).expanduser())
        except IdentityError as err:
            raise RunRejectedError(
                f"{tasks!r} repo: {repo_field!r} is unresolvable: {err}"
            ) from err

    if named != expected:
        raise RunRejectedError(
            f"{tasks!r} names repository {named.as_text()!r}, but the "
            f"request names {expected.as_text()!r}: refusing to lock, "
            "watch and validate a repository that will not be the one "
            "maestro actually runs against"
        )


def _ref_commit(ref: Ref | None, revision: str) -> str | None:
    """A ref's commit defaults to `revision`, and a difference is kept as given.

    Normalising the two together would turn "the plan is older than the code"
    into three identical fields and one quietly different one (spec §4).
    """
    if ref is None:
        return None
    return ref.commit if ref.commit is not None else revision
