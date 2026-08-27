"""Repository identity as maestro computes it — a pinned mirror.

maestro names a repository by its `origin` remote, never by a filesystem
path, and builds every run path from that name
(`maestro/maestro/repo_identity.py`, `maestro/maestro/state_paths.py:36-45`).
dispatcher has to reach the same name from the same checkout: the pre-launch
snapshot and the materialization watch (spec §5.2, §5.3) both address
`projects/<host>/<owner>/<repo>/runs/`, so a divergent key would make every
healthy launch look like `launch_unknown`.

This is a mirror of a producer RULE, pinned in
`contracts/maestro-repo-identity/v1/PINNED.txt` and held to the behaviour
table beside it. Re-pinned to maestro `95e5b3f` after maestro#211 — the
traversal the previous pin documented as producer-accepted is now refused
by the producer itself. Do not "fix" a mismatch by editing the rule here.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_CASE_INSENSITIVE_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org"})
_SCP_LIKE = re.compile(r"^(?:(?P<user>[^@]+)@)?(?P<host>[^:/]+):(?P<path>.+)$")
_URL_LIKE = re.compile(
    r"^(?P<scheme>https?|ssh|git)://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+)$"
)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_GIT_TIMEOUT = 15
_HIDDEN_PREFIXES = (".",)


def _segment_is_safe(segment: str) -> bool:
    """One path segment of a `RepoKey`, as the producer now judges it.

    Dots INSIDE a segment stay legal (`x..y` is a valid repo name); a
    segment that *is* `.` or `..` is not, because every one of the three
    becomes a directory name under `projects/`. The producer applied this
    to `repo` alone until maestro#211 widened it to all three — the mirror
    follows, it does not lead.
    """
    return bool(segment) and not _UNSAFE.search(segment) and segment not in {".", ".."}


class IdentityError(Exception):
    """Identity could not be established; the request must be refused."""


@dataclass(frozen=True)
class RepoKey:
    host: str
    owner: str
    repo: str
    local: bool = False

    def as_path_parts(self) -> tuple[str, ...]:
        """Path segments under `projects/`. Local keys are two segments."""
        if self.local:
            return ("_local", self.repo)
        return (self.host, self.owner, self.repo)

    def as_text(self) -> str:
        """The `<host>/<owner>/<repo>` form the collector keys runs by."""
        return "/".join(self.as_path_parts())


def _fold(host: str, owner: str, repo: str) -> tuple[str, str, str]:
    host = host.lower()
    if host in _CASE_INSENSITIVE_HOSTS:
        return host, owner.lower(), repo.lower()
    return host, owner, repo


def parse_remote_url(url: str) -> RepoKey:
    """Parse a git remote into a `RepoKey`, or raise `IdentityError`."""
    text = (url or "").strip()
    if not text:
        raise IdentityError("empty remote URL")
    match = _URL_LIKE.match(text) or _SCP_LIKE.match(text)
    if match is None:
        raise IdentityError(f"cannot parse remote URL: {url!r}")
    host = match.group("host")
    if host == "file":
        raise IdentityError(f"cannot parse remote URL: {url!r}")
    path = match.group("path").strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise IdentityError(f"remote URL has no owner/repo: {url!r}")
    owner, repo = parts[-2], parts[-1]
    if not all(_segment_is_safe(s) for s in (host, owner, repo)):
        raise IdentityError(f"remote URL yields unsafe path segments: {url!r}")
    host, owner, repo = _fold(host, owner, repo)
    return RepoKey(host=host, owner=owner, repo=repo)


def safe_path_parts(key: RepoKey) -> tuple[str, ...]:
    """`key.as_path_parts()`, refused if any segment could escape a join.

    The mirror above is deliberately faithful, and the producer's rule had a
    hole before maestro#211: `_UNSAFE` permits dots and only `repo` was
    checked against `{'.', '..'}`, so `git@host:owner/../etc.git` yielded
    `('host', '..', 'etc')` — verified against maestro cb91759.
    `_segment_is_safe` has checked all three segments since. dispatcher joins
    these segments into a filesystem path, so it refuses the traversal on
    its own side rather than diverging from the rule it mirrors. The
    producer-side gap is filed as maestro inbox issue (slug:
    `repo-identity-owner-traversal`).
    """
    parts = key.as_path_parts()
    for part in parts:
        if part in {"", ".", ".."} or "/" in part or "\\" in part:
            raise IdentityError(
                f"repository identity has an unsafe path segment {part!r}: "
                f"{'/'.join(parts)} would not stay under projects/"
            )
    return parts


def identity_from_checkout(repo_root: Path) -> RepoKey:
    """The `RepoKey` of a checkout, from its `origin` remote."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as err:
        raise IdentityError(f"cannot read origin of {repo_root}: {err}") from err
    if proc.returncode != 0:
        raise IdentityError(
            f"{repo_root} has no usable origin remote: "
            f"{proc.stderr.strip() or 'git exited ' + str(proc.returncode)}"
        )
    return parse_remote_url(proc.stdout)


def list_workspace_checkouts(
    workspace: Path,
) -> tuple[list[tuple[str, Path]], list[str]]:
    """`(name, checkout)` pairs for every visible directory directly under
    `workspace` — the ONE enumeration shared by the launchpad assembler's
    manifest scan (`dispatcher/core/launchpad.py::_manifest_repos`) and
    submit v2's identity-based checkout resolver
    (`RunController._resolve_v2_checkout`), so the two adapters can never
    walk a workspace differently (review fix wave C, C1).

    Only DOT-prefixed (genuinely hidden) entries are skipped: the
    repository contract permits `_` in directory names, so a valid
    `_service` checkout must stay visible (gate pass-4 finding) —
    non-repo scratch/coordination directories are excluded by carrying
    no .git, not by their name.

    `os.scandir`, not `iterdir`/`glob`: a single bad entry (a broken
    symlink, a permission-denied child) must degrade that ONE entry, not
    abort a whole comprehension via a blanket `except OSError` around it
    — an earlier version of the launchpad scan did exactly that and
    silently emptied the entire manifest (fail-open, review fix round 1).
    `os.scandir` makes each entry's stat a separate call this loop can
    catch and skip.

    Returns `(entries, notes)`; a failure to scan `workspace` itself is a
    different class of problem (nothing was listed at all) and is
    reported as a note too, rather than a silent `[]`.
    """
    try:
        with os.scandir(workspace) as scanned:
            raw_entries = list(scanned)
    except OSError as err:
        return [], [f"cannot list workspace {workspace}: {err}"]
    entries: list[tuple[str, Path]] = []
    notes: list[str] = []
    # The root ITSELF is a candidate when it carries .git (a plain dir or
    # a worktree's .git FILE): `roots=(repo,)` is a supported shape —
    # discovery checks `[root, *children]` and the B1 escape resolver
    # pinned it (review-pr finding on #209). Children are still scanned:
    # a checkout can contain sibling tooling dirs.
    root_is_checkout = (workspace / ".git").exists()
    if root_is_checkout:
        entries.append((workspace.name, workspace))
    for entry in raw_entries:
        if entry.name.startswith(_HIDDEN_PREFIXES):
            continue
        try:
            # follow_symlinks=False: a symlink in the workspace root would
            # smuggle an EXTERNAL checkout in as a candidate and a launch
            # would act outside the configured workspace (review on #209).
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError as err:
            notes.append(f"cannot stat {workspace / entry.name}: {err}")
            continue
        if not is_dir:
            continue
        if root_is_checkout and not (workspace / entry.name / ".git").exists():
            # Inside a root-as-checkout, a plain internal dir (docs/, src/)
            # is repo CONTENT, not a repo candidate — only nested checkouts
            # qualify (dry-run finding on #209).
            continue
        entries.append((entry.name, workspace / entry.name))
    entries.sort(key=lambda pair: pair[0])
    return entries, notes


def find_checkouts_by_identity(
    workspace: Path, target: RepoKey
) -> tuple[list[Path], list[str]]:
    """EVERY checkout directly under `workspace` whose `origin` remote
    resolves to `target`, in sorted-name order.

    A list, not first-match: two checkouts of one identity are a real
    workspace state (`run_store.py`'s locator contract names it), and a
    resolver that silently picks the first could act on the copy the
    operator was NOT looking at (gate pass-2 finding) — the caller decides
    whether >1 is an error.

    submit v2's fallback when the fast path `workspace / target.repo` is
    absent or names a different repository: a checkout's workspace
    directory name need not match its remote's `repo` segment (real fleet
    case: a directory named `open-prose/` cloned from `.../libretto.git`)
    — resolving by directory name alone made such a repo's Ready row
    unlaunchable (review fix wave C, C1). Uses `list_workspace_checkouts`,
    the SAME enumeration the launchpad assembler uses to derive each row's
    `repo_key` in the first place, so the two can never resolve a
    `repo_key` to two different checkouts.
    """
    entries, notes = list_workspace_checkouts(workspace)
    matches: list[Path] = []
    for _name, checkout in entries:
        if not (checkout / ".git").exists():
            continue
        try:
            found = identity_from_checkout(checkout)
        except IdentityError:
            continue
        if found.as_text() == target.as_text():
            matches.append(checkout.resolve())
    return matches, notes


def find_checkout_by_identity(workspace: Path, target: RepoKey) -> Path | None:
    """First identity match or None — kept for callers that tolerate
    ambiguity AND scan gaps; submit v2 uses `find_checkouts_by_identity`
    and refuses both."""
    matches, _notes = find_checkouts_by_identity(workspace, target)
    return matches[0] if matches else None
