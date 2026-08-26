"""Inventory capture — every IO of one capture generation, frozen facts.

Launchpad classifies plan items and `dags/` files against each other
(Task 4), and that classifier must never touch a filesystem or git itself —
otherwise a symlink swapped in, or a commit landed, between two reads could
make the classifier's verdict describe a DAG that was never actually seen.
This module is the ONE place all of that IO happens, exactly once per file,
and it returns only frozen facts: `head_revision` is read once and every
later blob lookup is pinned to that captured hex, never the moving `HEAD`
ref; each candidate file's bytes are read once and BOTH its text and its
`git hash-object` blob sha derive from that single read, so there is no
second path-based access for a symlink to race.

`dags/` itself is opened once with `O_DIRECTORY | O_NOFOLLOW` — a symlinked
or swapped `dags/` is refused at the root, not just at the leaves — and every
candidate file is opened once through that directory fd with
`O_NOFOLLOW | O_NONBLOCK`: `O_NOFOLLOW` refuses a symlinked leaf (`ELOOP`),
`O_NONBLOCK` keeps a FIFO from hanging the open. `os.fstat` on the opened fd
is the sole authority on whether the entry is a regular file — a
directory-fd `scandir()` entry's `is_file()` may follow symlinks and cannot
be trusted (B1 lesson also applies to `Path.glob`, which silently swallows
scan errors; `os.scandir` is used here instead).

`repo_key` and per-DAG `named_repo` are both resolved through
`run_identity.py`'s resolvers, with the SAME `repo_url`-then-`repo`
precedence `run_request.py::_reconcile_repo` uses at submit — so inventory
and submit can never disagree about which repository a DAG names.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from plan_fields.parser import DAG_RE, parse_dag
from plan_fields.scrape import scrape_items

from dispatcher.core.dag_subset import (
    MAX_DAG_BYTES,
    Accepted,
    DagSubsetVerdict,
    classify_dag_text,
)
from dispatcher.core.run_identity import (
    IdentityError,
    RepoKey,
    identity_from_checkout,
    parse_remote_url,
)

_GIT_TIMEOUT = 15
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_READ_CHUNK = 65536


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


def capture_inventory(checkout: Path) -> InventorySurface:
    """Capture one generation of TODO.md + `dags/` facts for `checkout`."""
    checkout = Path(checkout)
    head_revision, head_error = _resolve_head(checkout)
    repo_key, identity_error = _resolve_repo_key(checkout)
    plan_items, plan_error = _capture_plan_items(checkout)
    dag_files, dag_dir_error = _capture_dag_files(checkout, head_revision)

    return InventorySurface(
        plan_items=plan_items,
        dag_files=dag_files,
        head_revision=head_revision,
        repo_key=repo_key,
        plan_error=plan_error,
        dag_dir_error=dag_dir_error,
        capture_error=_join(head_error, identity_error),
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as err:
        return subprocess.CompletedProcess(
            args=["git", "-C", str(repo), *args],
            returncode=1,
            stdout="",
            stderr=str(err),
        )


def _join(*parts: str | None) -> str | None:
    text = "; ".join(p for p in parts if p)
    return text or None


def _resolve_head(checkout: Path) -> tuple[str | None, str | None]:
    result = _git(checkout, "rev-parse", "HEAD")
    if result.returncode != 0:
        return None, f"cannot read HEAD of {checkout}: {result.stderr.strip()}"
    head = result.stdout.strip()
    if not _SHA_RE.fullmatch(head):
        return None, f"unexpected HEAD output from {checkout}: {head!r}"
    return head, None


def _resolve_repo_key(checkout: Path) -> tuple[RepoKey | None, str | None]:
    try:
        return identity_from_checkout(checkout), None
    except IdentityError as err:
        return None, str(err)


# --- plan scrape -------------------------------------------------------


def _shipped_lines(text: str) -> frozenset[int]:
    """Line numbers under a `##`-level section whose title contains "Shipped".

    The capture tracks `##`-level sections itself over the same text: a
    deeper `###`+ sub-heading does NOT end the region, only another `#` or
    `##` heading does. `ScrapedItem.section` alone cannot answer this — it
    holds the nearest heading of ANY level, not the nearest `##`.
    """
    shipped: set[int] = set()
    in_shipped = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            if level <= 2:
                in_shipped = level == 2 and "Shipped" in heading.group(2)
            continue
        if in_shipped:
            shipped.add(lineno)
    return frozenset(shipped)


def _capture_plan_items(checkout: Path) -> tuple[tuple[PlanItem, ...], str | None]:
    try:
        text = (checkout / "TODO.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as err:
        return (), f"cannot read TODO.md: {err}"

    shipped = _shipped_lines(text)
    items = []
    for item in scrape_items(text):
        dag_tag, dag_diags = parse_dag(item, item.item_id, checkout.name)
        items.append(
            PlanItem(
                item_id=item.item_id,
                line=item.line,
                open=not item.checked,
                shipped=item.line in shipped,
                dag_raw=item.tags.get("dag"),
                dag_tag=dag_tag,
                dag_diag=dag_diags[0][0] if dag_diags else None,
            )
        )
    return tuple(items), None


# --- dags/ capture -------------------------------------------------------


def _capture_dag_files(
    checkout: Path, head_revision: str | None
) -> tuple[tuple[DagFileInfo, ...], str | None]:
    dags_path = checkout / "dags"
    try:
        dir_fd = os.open(dags_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return (), None  # a repo with no DAGs is normal, not broken
    except OSError as err:
        return (), f"cannot open dags/: {err}"

    try:
        try:
            with os.scandir(dir_fd) as entries:
                names = sorted(entry.name for entry in entries)
        except OSError as err:
            return (), f"cannot list dags/: {err}"

        files = []
        for name in names:
            rel_path = f"dags/{name}"
            if not DAG_RE.fullmatch(rel_path):
                continue  # not a candidate: invisible to launchpad
            files.append(
                _capture_one_dag_file(checkout, dir_fd, name, rel_path, head_revision)
            )
        return tuple(files), None
    finally:
        os.close(dir_fd)


def _capture_one_dag_file(
    checkout: Path,
    dir_fd: int,
    name: str,
    rel_path: str,
    head_revision: str | None,
) -> DagFileInfo:
    try:
        fd = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=dir_fd,
        )
    except OSError as err:
        # ELOOP (symlink) and friends: refused before any content is read.
        return DagFileInfo(
            rel_path=rel_path,
            is_regular=False,
            text=None,
            blob_sha=None,
            head_blob_sha=None,
            subset=None,
            named_repo=None,
            named_repo_error=None,
            error=f"cannot open {rel_path}: {err}",
        )

    try:
        st = os.fstat(fd)
        is_regular = stat.S_ISREG(st.st_mode)
        if is_regular and st.st_size > MAX_DAG_BYTES:
            # Refused from st_size, BEFORE any read: the classifier's cap
            # fires only after text exists, and capture must not materialize
            # gigabytes to hand it something to refuse.
            return DagFileInfo(
                rel_path=rel_path,
                is_regular=True,
                text=None,
                blob_sha=None,
                head_blob_sha=None,
                subset=None,
                named_repo=None,
                named_repo_error=None,
                error=(
                    f"{rel_path} exceeds {MAX_DAG_BYTES} bytes ({st.st_size}): not read"
                ),
            )
        data = _read_all(fd) if is_regular else b""
    except OSError as err:
        # EIO/ESTALE and friends mid-read: captured as a named fact, same
        # discipline as the open() failure above — never propagate.
        return DagFileInfo(
            rel_path=rel_path,
            is_regular=False,
            text=None,
            blob_sha=None,
            head_blob_sha=None,
            subset=None,
            named_repo=None,
            named_repo_error=None,
            error=f"cannot read {rel_path}: {err}",
        )
    finally:
        os.close(fd)

    if not is_regular:
        # A FIFO or device: the open did not hang (O_NONBLOCK) and nothing
        # was read (bytes are read ONLY after S_ISREG).
        return DagFileInfo(
            rel_path=rel_path,
            is_regular=False,
            text=None,
            blob_sha=None,
            head_blob_sha=None,
            subset=None,
            named_repo=None,
            named_repo_error=None,
            error=f"{rel_path} refused: not a regular file",
        )

    blob_sha, hash_error = _hash_object(checkout, data)
    head_blob_sha, head_error = _head_blob_sha(checkout, head_revision, rel_path)

    decode_error: str | None
    try:
        text: str | None = data.decode("utf-8")
        decode_error = None
    except UnicodeDecodeError as err:
        text = None
        decode_error = f"{rel_path} is not valid UTF-8: {err}"

    subset = classify_dag_text(text) if text is not None else None
    named_repo, named_repo_error = _resolve_named_repo(subset)

    return DagFileInfo(
        rel_path=rel_path,
        is_regular=True,
        text=text,
        blob_sha=blob_sha,
        head_blob_sha=head_blob_sha,
        subset=subset,
        named_repo=named_repo,
        named_repo_error=named_repo_error,
        error=_join(hash_error, head_error, decode_error),
    )


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, _READ_CHUNK)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _hash_object(checkout: Path, data: bytes) -> tuple[str | None, str | None]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(checkout), "hash-object", "--stdin"],
            input=data,
            capture_output=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as err:
        return None, f"cannot hash-object in {checkout}: {err}"
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        return None, f"git hash-object failed: {stderr}"
    return proc.stdout.decode("utf-8").strip(), None


def _head_blob_sha(
    checkout: Path, head_revision: str | None, rel_path: str
) -> tuple[str | None, str | None]:
    if head_revision is None:
        return None, None
    result = _git(checkout, "ls-tree", head_revision, "--", rel_path)
    if result.returncode != 0:
        return None, (
            f"cannot read {rel_path} at {head_revision[:12]}: {result.stderr.strip()}"
        )
    line = result.stdout.strip()
    if not line:
        return None, None  # absent from that tree — a fact, not a failure
    parts = line.split()
    if len(parts) < 3:
        return None, f"unparseable ls-tree output for {rel_path}: {line!r}"
    return parts[2], None


def _resolve_named_repo(
    subset: DagSubsetVerdict | None,
) -> tuple[RepoKey | None, str | None]:
    """The repo a DAG names, resolved EXACTLY as `_reconcile_repo` does.

    `repo_url` wins when non-empty; else `repo` is a checkout path. This is
    deliberately the same precedence and the same two resolvers submit uses,
    so inventory and submit cannot disagree about which repository a DAG
    names."""
    if not isinstance(subset, Accepted):
        return None, None
    if subset.repo_url:
        try:
            return parse_remote_url(subset.repo_url), None
        except IdentityError as err:
            return None, str(err)
    if subset.repo_path:
        try:
            return identity_from_checkout(Path(subset.repo_path).expanduser()), None
        except IdentityError as err:
            return None, str(err)
    return None, None
