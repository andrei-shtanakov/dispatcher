# Dark Factory control plane, slice 0 — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A durable, idempotent request that starts one maestro Mode-1 run against one repository at one revision from the dispatcher UI, with run state read back from maestro's own store.

**Architecture:** dispatcher persists exactly one thing nobody else owns — the `RunRequest` and its launch outcome — under a configured state directory. A new `RunController` owns the long-lived maestro child process and a durable per-`RepoKey` lock; the existing `ActionRunner` keeps the short forge actions and is not touched. Run state after the receipt is read from maestro's per-run store by the collector that already reads it.

**Tech Stack:** Python ≥3.12, pydantic v2, FastAPI, pytest, `uv`; maestro CLI invoked as a subprocess.

**Spec:** `docs/superpowers/specs/2026-08-22-dark-factory-control-plane-slice0-design.md` (merged in #165). Executors read both — every task below argues from a numbered section of that spec.

## Global Constraints

- Python `>=3.12`; run everything through `uv run` (never bare `pytest`/`python`).
- Line length **88**; `uv run ruff format .` then `uv run ruff check . --fix`; `uv run pyrefly check` must be clean. Type hints on all new code; docstrings on public functions.
- Tests live in `tests/` (flat, `test_<topic>.py`); builders go in `tests/conftest.py`. Run with `uv run pytest`.
- **Mode 1 only.** Spec §6 names `submit · status · stop · retry · approve · run-end`. This plan implements `submit` as the **launch operation** (Task 4), never a control verb, and implements four verbs: `status · retry · approve · run-end`. **`stop` is excluded on evidence:** `maestro stop [OPTIONS]` takes no `--run` and no positional — it terminates the scheduler process, not a run, so a per-request control would end every other run the scheduler manages. Verified by running `maestro stop --help`. The spec needs this correction too. Mode-2 (workstream) verbs must not appear anywhere in this plan's code.
- **No `merge_authority` field** anywhere, and no merge from the UI (spec §2.2).
- `accepted` is three-valued — `true` / `false` / `null` — and `null` is never collapsed into `false` (spec §5.3).
- **Neighbour code is cited repository-first** in comments and docstrings (`maestro/maestro/cli.py:1040`), never as a bare path — both repos have a `cli.py` (spec, Citations note).
- CON-03 ("no sibling-repo path is ever resolved") constrains the **read/classification plane** (`core/governance.py`): it forbids reaching into a neighbour repo for *contracts*, which are vendored instead. It does **not** forbid resolving a workspace checkout to act on it — `ActionRunner._target` already does exactly that (`dispatcher/core/actions.py:354`), and this plan follows that precedent.

## Out of scope for this plan

The pilot's `tasks.yaml` and the `entrypoint_in_command` fix live in **deployer**, a separate repository. Per the polyrepo rule they get their own plan, authored from inside deployer. This plan changes dispatcher only.

## File Structure

| File | Responsibility |
| --- | --- |
| `dispatcher/core/run_identity.py` (new) | Name a repository the way maestro names it: `origin` remote → `RepoKey` → `projects/<host>/<owner>/<repo>`. Pinned mirror of maestro's rule. |
| `contracts/maestro-repo-identity/v1/` (new) | `PINNED.txt` + `cases.json` — the pin and the behaviour table the mirror is tested against. |
| `dispatcher/core/run_request.py` (new) | `RunRequest` v0 model and its validation (repository, revision, tasks, refs). No I/O beyond `git cat-file`. |
| `dispatcher/core/run_store.py` (new) | Durable launch records (five states) and the durable per-`RepoKey` lock, with the spec's release rules. |
| `dispatcher/core/run_controller.py` (new) | Launch, materialization watch, receipt, `launch_unknown` resolution, control verbs. Owns the maestro child process and its environment. |
| `dispatcher/core/discovery.py` (modify) | Two config fields: `run_state_dir`, `maestro_cli`. |
| `dispatcher/server/app.py` (modify) | Four endpoints behind the existing action token. |
| `tests/test_run_identity.py`, `test_run_request.py`, `test_run_store.py`, `test_run_controller.py`, `test_run_api.py` (new) | One test module per unit above. |

---

### Task 1: Repository identity, pinned to maestro's rule

**Files:**
- Create: `dispatcher/core/run_identity.py`
- Create: `contracts/maestro-repo-identity/v1/PINNED.txt`
- Create: `contracts/maestro-repo-identity/v1/cases.json`
- Test: `tests/test_run_identity.py`

**Why this is first:** every later task needs `projects/<host>/<owner>/<repo>`. If dispatcher computes a different key than maestro, the controller watches a directory maestro never creates and reports `launch_unknown` for every healthy run (spec §5.2.1).

**Interfaces:**
- Produces: `RepoKey(host: str, owner: str, repo: str, local: bool = False)` with `as_path_parts() -> tuple[str, ...]` and `as_text() -> str`; `parse_remote_url(url: str) -> RepoKey`; `safe_path_parts(key: RepoKey) -> tuple[str, ...]`; `identity_from_checkout(repo_root: Path) -> RepoKey`; `IdentityError`. **Every path join uses `safe_path_parts`, never `as_path_parts` directly.**

- [ ] **Step 1: Write the pin file**

`contracts/maestro-repo-identity/v1/PINNED.txt`:

```
source: maestro maestro/repo_identity.py
commit: cb91759
vendored: 2026-08-22
note: pinned MIRROR of a producer RULE, not a schema copy (repo-boundaries
  vendoring, ADR-ECO-003). dispatcher must name a repository exactly as
  maestro does, or it watches the wrong run directory. Do not edit the rule
  here to fix a mismatch — re-pin against the producer.
guarantee-gap: only copy-integrity (the cases table below) is enforced today.
  The upstream-drift half of the two-guarantees rule is NOT wired; it is
  filed as `@id:maestro-identity-drift-watch` in TODO.md. Unknown drift must
  not read as green.
```

- [ ] **Step 2: Write the behaviour table**

`contracts/maestro-repo-identity/v1/cases.json` — the cases are lifted from the producer's parsing rules (`maestro/maestro/repo_identity.py:51-79`), including the case-folding hosts at `:21`:

```json
{
  "case_insensitive_hosts": ["github.com", "gitlab.com", "bitbucket.org"],
  "parse": [
    {"url": "git@github.com:Andrei-Shtanakov/Dispatcher.git",
     "key": ["github.com", "andrei-shtanakov", "dispatcher"]},
    {"url": "https://github.com/andrei-shtanakov/deployer",
     "key": ["github.com", "andrei-shtanakov", "deployer"]},
    {"url": "ssh://git@git.epam.com:7999/Team/Repo.git",
     "key": ["git.epam.com", "Team", "Repo"]},
    {"url": "git://gitlab.com/Owner/Repo.git",
     "key": ["gitlab.com", "owner", "repo"]}
  ],
  "reject": [
    {"url": "", "why": "empty remote URL"},
    {"url": "file:///tmp/repo", "why": "non-git scheme"},
    {"url": "git@github.com:repo.git", "why": "no owner/repo"},
    {"url": "git@github.com:owner/...git", "why": "repo segment is '..' — the one traversal case the producer DOES check"},
    {"url": "git@github.com:owner/re~po.git", "why": "character outside [A-Za-z0-9._-]"}
  ],
  "producer_accepts_but_dispatcher_must_refuse": [
    {"url": "git@github.com:owner/../etc.git", "key": ["github.com", "..", "etc"],
     "why": "the producer's _UNSAFE class allows dots, and only `repo` is checked against {'.','..'} — so `owner` may be '..'. Verified against maestro cb91759. dispatcher joins these segments into a filesystem path, so it guards separately (see safe_path_parts)."}
  ]
}
```

- [ ] **Step 3: Write the failing test**

`tests/test_run_identity.py`:

```python
"""dispatcher must name a repository exactly as maestro does (spec §5.2.1)."""

import json
import subprocess
from pathlib import Path

import pytest

from dispatcher.core.run_identity import (
    IdentityError,
    RepoKey,
    identity_from_checkout,
    parse_remote_url,
    safe_path_parts,
)

_CASES = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "contracts/maestro-repo-identity/v1/cases.json"
    ).read_text()
)


@pytest.mark.parametrize("case", _CASES["parse"], ids=lambda c: c["url"])
def test_parse_matches_the_pinned_table(case: dict) -> None:
    assert list(parse_remote_url(case["url"]).as_path_parts()) == case["key"]


@pytest.mark.parametrize("case", _CASES["reject"], ids=lambda c: c["why"])
def test_rejects_what_the_producer_rejects(case: dict) -> None:
    with pytest.raises(IdentityError):
        parse_remote_url(case["url"])


def test_local_key_is_two_segments() -> None:
    key = RepoKey(host="", owner="", repo="thing-abc123", local=True)
    assert key.as_path_parts() == ("_local", "thing-abc123")


def test_identity_from_checkout_reads_origin(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "git@github.com:Owner/Repo.git"],
        check=True,
    )
    assert identity_from_checkout(tmp_path).as_path_parts() == (
        "github.com", "owner", "repo",
    )


def test_traversal_segment_is_refused_before_any_join() -> None:
    """The producer accepts owner='..'; dispatcher must not join it."""
    accepted = _CASES["producer_accepts_but_dispatcher_must_refuse"][0]
    key = parse_remote_url(accepted["url"])
    assert list(key.as_path_parts()) == accepted["key"], "mirror stays faithful"
    with pytest.raises(IdentityError, match="unsafe path segment"):
        safe_path_parts(key)


def test_safe_path_parts_passes_a_normal_key() -> None:
    key = RepoKey(host="github.com", owner="owner", repo="deployer")
    assert safe_path_parts(key) == ("github.com", "owner", "deployer")


def test_identity_from_checkout_without_origin_refuses(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    with pytest.raises(IdentityError):
        identity_from_checkout(tmp_path)
```

- [ ] **Step 4: Run the test to verify it fails**

Run: `uv run pytest tests/test_run_identity.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dispatcher.core.run_identity'`

- [ ] **Step 5: Write the implementation**

`dispatcher/core/run_identity.py`:

```python
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
table beside it. Do not "fix" a mismatch by editing the rule here.
"""

from __future__ import annotations

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
    if _UNSAFE.search(owner) or _UNSAFE.search(repo) or repo in {".", ".."}:
        raise IdentityError(f"remote URL yields unsafe path segments: {url!r}")
    host, owner, repo = _fold(host, owner, repo)
    return RepoKey(host=host, owner=owner, repo=repo)


def safe_path_parts(key: RepoKey) -> tuple[str, ...]:
    """`key.as_path_parts()`, refused if any segment could escape a join.

    The mirror above is deliberately faithful, and the producer's rule has a
    hole: `_UNSAFE` permits dots and only `repo` is checked against
    `{'.', '..'}`, so `git@host:owner/../etc.git` yields `('host', '..',
    'etc')` — verified against maestro cb91759. dispatcher joins these
    segments into a filesystem path, so it refuses the traversal on its own
    side rather than diverging from the rule it mirrors. The producer-side
    gap is filed as maestro inbox issue (slug:
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
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/test_run_identity.py -v`
Expected: PASS (10 cases)

- [ ] **Step 7: Name the missing drift guarantee in TODO.md**

Append to the open section of `TODO.md` — a vendored artifact with only one of the two guarantees is a known gap, and an unnamed gap reads as green (`two-contract-guarantees`):

Both tags go on the **checkbox line**: the parser is line-based and tags on a
continuation line are invisible (CLAUDE.md, «Планы»; measured in devtools#57).

```markdown
- [ ] Upstream-drift вахта для `contracts/maestro-repo-identity/v1` @owner:repo:dispatcher @id:maestro-identity-drift-watch
      — сегодня есть только copy-integrity (таблица `cases.json`), второй гарантии нет;
      правило именования репозитория зеркалится из maestro, и расхождение делает
      контроллер слепым к собственному прогону (план среза 0, задача 1)
```

- [ ] **Step 8: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add dispatcher/core/run_identity.py contracts/maestro-repo-identity tests/test_run_identity.py TODO.md
git commit -m "feat(run): pinned mirror of maestro's repository identity rule"
```

---

### Task 2: `RunRequest` v0 and its validation

**Files:**
- Create: `dispatcher/core/run_request.py`
- Test: `tests/test_run_request.py`

**Interfaces:**
- Consumes: `RepoKey`, `identity_from_checkout`, `IdentityError` from Task 1.
- Produces: `RunRequest`, `Ref`, `RunRejectedError`, `ValidatedRequest(request: RunRequest, checkout: Path, key: RepoKey)`, `validate_request(request: RunRequest, config: DispatcherConfig) -> ValidatedRequest`.

**Design note for the implementer:** the spec asks that `tasks` resolve only inside the checkout **of `revision`**, with no `..` and no symlink escape (spec §4.2). Do not implement that with filesystem resolution — ask git instead. `git cat-file -e <revision>:<tasks>` answers "does this repo-relative path exist in that commit", and a git object path cannot traverse out of the repository or follow a symlink into one. The filesystem check would need a resolve-then-assert dance that this sidesteps entirely.

- [ ] **Step 1: Write the failing test**

`tests/test_run_request.py`:

```python
"""RunRequest v0 validation (spec §4)."""

import subprocess
from pathlib import Path

import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.run_request import (
    RunRejectedError,
    RunRequest,
    validate_request,
)

_SHA_A = "a" * 40


def _repo(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin",
         f"git@github.com:owner/{name}.git"],
        check=True,
    )
    (repo / "tasks.yaml").write_text("tasks: []\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"],
        check=True,
    )
    return repo


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _request(**over: object) -> RunRequest:
    base = {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "work_id": "todo://deployer/entrypoint-token-boundary-match",
        "repository": "deployer",
        "revision": _SHA_A,
        "tasks": "tasks.yaml",
    }
    base.update(over)
    return RunRequest(**base)  # type: ignore[arg-type]


def test_accepts_a_well_formed_request(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "deployer")
    config = DispatcherConfig(roots=(tmp_path,))
    validated = validate_request(_request(revision=_head(repo)), config)
    assert validated.checkout == repo
    assert validated.key.as_path_parts() == ("github.com", "owner", "deployer")


def test_revision_must_be_a_full_sha(tmp_path: Path) -> None:
    _repo(tmp_path, "deployer")
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="40-hex"):
        validate_request(_request(revision="HEAD"), config)


def test_unknown_repository_is_refused(tmp_path: Path) -> None:
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="not a git repo"):
        validate_request(_request(repository="nope"), config)


def test_unsafe_repository_name_is_refused(tmp_path: Path) -> None:
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="unsafe"):
        validate_request(_request(repository="../etc"), config)


@pytest.mark.parametrize("bad", ["/etc/passwd", "../outside.yaml"])
def test_tasks_path_must_be_repo_relative(tmp_path: Path, bad: str) -> None:
    repo = _repo(tmp_path, "deployer")
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="repo-relative"):
        validate_request(_request(revision=_head(repo), tasks=bad), config)


def test_tasks_must_exist_in_that_revision(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "deployer")
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="not present in"):
        validate_request(
            _request(revision=_head(repo), tasks="missing.yaml"), config
        )


def test_checkout_must_already_be_at_the_revision(tmp_path: Path) -> None:
    """Slice 0 refuses rather than moving a neighbour's checkout."""
    repo = _repo(tmp_path, "deployer")
    (repo / "later.txt").write_text("x")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "second"],
        check=True,
    )
    first = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD~1"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    config = DispatcherConfig(roots=(tmp_path,))
    with pytest.raises(RunRejectedError, match="checkout is at"):
        validate_request(_request(revision=first), config)


def test_ref_commit_defaults_to_revision(tmp_path: Path) -> None:
    repo = _repo(tmp_path, "deployer")
    head = _head(repo)
    request = _request(revision=head, spec_ref={"path": "docs/s.md"})
    assert request.spec_ref is not None
    assert request.spec_ref.commit is None
    validated = validate_request(request, DispatcherConfig(roots=(tmp_path,)))
    assert validated.spec_commit == head
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_run_request.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dispatcher.core.run_request'`

- [ ] **Step 3: Write the implementation**

`dispatcher/core/run_request.py`:

```python
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

from pydantic import BaseModel

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.run_identity import (
    IdentityError,
    RepoKey,
    identity_from_checkout,
)

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SAFE_DIR_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")
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

    request_id: str
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
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )


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


def validate_request(
    request: RunRequest, config: DispatcherConfig
) -> ValidatedRequest:
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

    return ValidatedRequest(
        request=request,
        checkout=checkout,
        key=key,
        spec_commit=_ref_commit(request.spec_ref, request.revision),
        plan_commit=_ref_commit(request.plan_ref, request.revision),
    )


def _ref_commit(ref: Ref | None, revision: str) -> str | None:
    """A ref's commit defaults to `revision`, and a difference is kept as given.

    Normalising the two together would turn "the plan is older than the code"
    into three identical fields and one quietly different one (spec §4).
    """
    if ref is None:
        return None
    return ref.commit if ref.commit is not None else revision
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_run_request.py -v`
Expected: PASS (9 cases)

- [ ] **Step 5: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add dispatcher/core/run_request.py tests/test_run_request.py
git commit -m "feat(run): RunRequest v0 with fail-closed validation"
```

---

### Task 3: Durable launch records and the per-`RepoKey` lock

**Files:**
- Modify: `dispatcher/core/discovery.py` (add `run_state_dir`, `maestro_cli`)
- Create: `dispatcher/core/run_store.py`
- Test: `tests/test_run_store.py`

**Interfaces:**
- Consumes: `RepoKey` (Task 1), `RunRequest` (Task 2).
- Produces: `LaunchState` (literal), `LaunchRecord`, `RunStore(state_dir: Path)` with `reserve(...) -> LaunchRecord`, `mark_launching(request_id)`, `mark_materialized(request_id, run_id)`, `mark_unknown(request_id, reason)`, `mark_terminal(request_id, outcome)`, `get(request_id) -> LaunchRecord | None`, `release_lock(key, request_id)`, `LockBusyError`.

**Design notes for the implementer:**
- The lock must survive a restart, so it is a **file**, not a `threading.Lock` — the restart is exactly the event that releases a process-local lock and creates the ambiguity (spec §5.4).
- Records are written temp-then-`os.replace`. This mirrors maestro's own publication discipline (`maestro/maestro/run_publish.py:73`) and for the same reason: a half-written record must never be readable.
- `reserve()` stores the pre-launch listing of `runs/` and the launch-window start. Without them there is nothing to correlate an orphan against (spec §5.2.1).

- [ ] **Step 1: Add the two config fields**

In `dispatcher/core/discovery.py`, inside `DispatcherConfig`, after `benchmarks_token_file`:

```python
    # Durable store for RunRequest records and the per-RepoKey launch lock
    # (slice-0 spec §5.2). None → the control plane is off: no submit
    # endpoint, no controller. Mirrors the "None → feature off" shape of
    # `benchmarks_url` and `tracking_file`.
    run_state_dir: Path | None = None
    # ABSOLUTE path to the maestro binary the controller launches. None →
    # the control plane is off. Distinct from `maestro_home`/`maestro_db`,
    # which say where maestro's STATE is; this says what to execute.
    maestro_cli: Path | None = None
```

And in `load_config`, beside the `suggest_claude_cli` block:

```python
    raw_state_dir = data.get("run_state_dir")
    run_state_dir = Path(raw_state_dir).expanduser() if raw_state_dir else None
    raw_maestro_cli = data.get("maestro_cli")
    maestro_cli = Path(raw_maestro_cli).expanduser() if raw_maestro_cli else None
```

Pass both into the `DispatcherConfig(...)` construction in that function.

- [ ] **Step 2: Write the failing test**

`tests/test_run_store.py`:

```python
"""Durable launch records and the per-RepoKey lock (spec §5.2, §5.4)."""

from pathlib import Path

import pytest

from dispatcher.core.run_identity import RepoKey
from dispatcher.core.run_store import LockBusyError, RunStore

_KEY = RepoKey(host="github.com", owner="owner", repo="deployer")
_REQ = "11111111-1111-4111-8111-111111111111"
_OTHER = "22222222-2222-4222-8222-222222222222"


def _store(tmp_path: Path) -> RunStore:
    return RunStore(tmp_path / "state")


def test_reserve_writes_a_durable_record_before_anything_starts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    record = store.reserve(
        _REQ, _KEY, known_runs=["01AAA"], window_start="2026-08-22T00:00:00Z"
    )
    assert record.state == "reserved"
    stored = store.get(_REQ)
    assert stored is not None
    assert stored.known_runs == ["01AAA"]


def test_second_request_for_a_busy_repo_is_refused_not_queued(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    with pytest.raises(LockBusyError, match="deployer"):
        store.reserve(_OTHER, _KEY, known_runs=[], window_start="t")


def test_lock_survives_a_new_store_instance(tmp_path: Path) -> None:
    """A process-local lock is released by exactly the restart that creates
    the problem the lock exists to prevent (spec §5.4)."""
    _store(tmp_path).reserve(_REQ, _KEY, known_runs=[], window_start="t")
    with pytest.raises(LockBusyError):
        _store(tmp_path).reserve(_OTHER, _KEY, known_runs=[], window_start="t")


def test_materializing_releases_the_lock(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_materialized(_REQ, "01BBB")
    stored = store.get(_REQ)
    assert stored is not None and stored.run_id == "01BBB"
    store.reserve(_OTHER, _KEY, known_runs=[], window_start="t")  # no raise


def test_launch_unknown_keeps_the_lock(tmp_path: Path) -> None:
    """Dropping the lock on uncertainty would let the next request launch a
    second run into the same tree (spec §5.2.1)."""
    store = _store(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    store.mark_unknown(_REQ, "no run appeared within the timeout")
    stored = store.get(_REQ)
    assert stored is not None and stored.state == "launch_unknown"
    with pytest.raises(LockBusyError):
        store.reserve(_OTHER, _KEY, known_runs=[], window_start="t")


def test_release_lock_requires_the_owning_request(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    with pytest.raises(LockBusyError, match="held by"):
        store.release_lock(_KEY, _OTHER)
    store.release_lock(_KEY, _REQ)
    store.reserve(_OTHER, _KEY, known_runs=[], window_start="t")


def test_a_repeated_request_id_returns_the_existing_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    again = store.reserve(_REQ, _KEY, known_runs=[], window_start="t")
    assert again.state == first.state
    assert again.request_id == first.request_id
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_run_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dispatcher.core.run_store'`

- [ ] **Step 4: Write the implementation**

`dispatcher/core/run_store.py`:

```python
"""Durable launch records and the per-RepoKey lock (spec §5.2, §5.4).

Two facts live here and nowhere else: what dispatcher asked for, and how far
that ask got. Everything about the run itself is maestro's and is read from
maestro's store (spec §3.2).

The lock is a file on purpose. maestro does not stop two concurrent CLI runs
of one repository — its `RunIsLive` guard fires only for a run classified
`running`, which needs a holder file that only the service tick writes
(`maestro/maestro/service/tick.py:133`) — so this lock is the only thing
between slice 0 and two agent-driven runs mutating one checkout.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from dispatcher.core.run_identity import RepoKey, safe_path_parts

LaunchState = Literal[
    "reserved", "launching", "materialized", "terminal", "launch_unknown"
]

#: States in which the launch is still in flight and the lock must be held.
_LOCK_HELD_STATES = frozenset({"reserved", "launching", "launch_unknown"})

_DIR_MODE = 0o700
_FILE_MODE = 0o600


class RunStoreError(Exception):
    """The store cannot honour the call (→ 422)."""


class LockBusyError(RunStoreError):
    """The repository already has a launch in flight (→ 409)."""


class LaunchRecord(BaseModel):
    request_id: str
    repo_key: str
    state: LaunchState
    run_id: str | None = None
    reason: str | None = None
    #: `runs/` as it looked immediately before the launch — the only thing an
    #: orphan can be correlated against (spec §5.2.1).
    known_runs: list[str] = Field(default_factory=list)
    window_start: str = ""
    outcome: str | None = None


class RunStore:
    """Crash-safe records under `<state_dir>/requests`, locks under `locks`."""

    def __init__(self, state_dir: Path) -> None:
        self._root = state_dir
        self._requests = state_dir / "requests"
        self._locks = state_dir / "locks"

    def _ensure(self) -> None:
        for path in (self._root, self._requests, self._locks):
            path.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)

    def _record_path(self, request_id: str) -> Path:
        # request_id reaches this off the wire; it must never shape a path.
        safe = "".join(c for c in request_id if c.isalnum() or c in "-_")
        if not safe or safe != request_id:
            # Not LockBusyError: nothing is in flight, the input is bad.
            raise RunStoreError(f"unsafe request_id: {request_id!r}")
        return self._requests / f"{safe}.json"

    def _lock_path(self, key: RepoKey) -> Path:
        slug = "-".join(safe_path_parts(key)).replace("/", "-")
        return self._locks / f"{slug}.lock"

    def get(self, request_id: str) -> LaunchRecord | None:
        try:
            raw = self._record_path(request_id).read_text()
        except OSError:
            return None
        try:
            return LaunchRecord.model_validate_json(raw)
        except ValueError:
            return None

    def _write(self, record: LaunchRecord) -> None:
        """Temp-then-rename: a half-written record must never be readable."""
        self._ensure()
        target = self._record_path(record.request_id)
        fd, tmp = tempfile.mkstemp(dir=str(self._requests), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(record.model_dump_json(indent=2))
            os.chmod(tmp, _FILE_MODE)
            os.replace(tmp, target)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def reserve(
        self,
        request_id: str,
        key: RepoKey,
        *,
        known_runs: list[str],
        window_start: str,
    ) -> LaunchRecord:
        """Take the lock and write the record BEFORE any process starts.

        A repeated `request_id` returns its existing record and never starts a
        second launch (spec §5.2).
        """
        existing = self.get(request_id)
        if existing is not None:
            return existing
        self._ensure()
        lock = self._lock_path(key)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _FILE_MODE)
        except FileExistsError:
            holder = self._lock_holder(lock)
            raise LockBusyError(
                f"{key.as_text()}: a launch is already in flight "
                f"(held by {holder or 'an unreadable lock file'}); "
                "refused rather than queued — a queue lengthens the ambiguity "
                "window instead of closing it"
            ) from None
        with os.fdopen(fd, "w") as handle:
            json.dump({"request_id": request_id, "pid": os.getpid()}, handle)
        record = LaunchRecord(
            request_id=request_id,
            repo_key=key.as_text(),
            state="reserved",
            known_runs=known_runs,
            window_start=window_start,
        )
        self._write(record)
        return record

    def _lock_holder(self, lock: Path) -> str | None:
        try:
            return str(json.loads(lock.read_text())["request_id"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def release_lock(self, key: RepoKey, request_id: str) -> None:
        """Release only the lock this request owns (spec §5.2.1)."""
        lock = self._lock_path(key)
        holder = self._lock_holder(lock)
        if holder is not None and holder != request_id:
            raise LockBusyError(
                f"{key.as_text()}: lock is held by {holder}, not {request_id}"
            )
        lock.unlink(missing_ok=True)

    def _transition(self, request_id: str, **fields: object) -> LaunchRecord:
        record = self.get(request_id)
        if record is None:
            raise RunStoreError(f"no launch record for {request_id}")
        updated = record.model_copy(update=fields)
        self._write(updated)
        return updated

    def mark_launching(self, request_id: str) -> LaunchRecord:
        return self._transition(request_id, state="launching")

    def mark_materialized(self, request_id: str, run_id: str) -> LaunchRecord:
        """The launch is no longer in flight, so the lock is released here."""
        record = self._transition(request_id, state="materialized", run_id=run_id)
        self._release_for(record)
        return record

    def mark_unknown(self, request_id: str, reason: str) -> LaunchRecord:
        """The lock is deliberately NOT released (spec §5.2.1)."""
        return self._transition(request_id, state="launch_unknown", reason=reason)

    def mark_terminal(self, request_id: str, outcome: str) -> LaunchRecord:
        record = self._transition(request_id, state="terminal", outcome=outcome)
        self._release_for(record)
        return record

    def _release_for(self, record: LaunchRecord) -> None:
        parts = record.repo_key.split("/")
        key = (
            RepoKey(host="", owner="", repo=parts[1], local=True)
            if parts[0] == "_local"
            else RepoKey(host=parts[0], owner=parts[1], repo=parts[2])
        )
        self.release_lock(key, record.request_id)

    def holds_lock(self, key: RepoKey) -> str | None:
        """The request_id currently holding this repo's launch lock, if any."""
        return self._lock_holder(self._lock_path(key))
```

Note for the implementer: `_LOCK_HELD_STATES` documents the invariant the transitions implement; assert against it in Task 5 rather than deleting it.

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_run_store.py -v`
Expected: PASS (7 cases)

- [ ] **Step 6: Run the whole suite — the config change touches every loader test**

Run: `uv run pytest tests/test_discovery.py tests/test_cli.py -v`
Expected: PASS (the two new fields default to `None`)

- [ ] **Step 7: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add dispatcher/core/run_store.py dispatcher/core/discovery.py tests/test_run_store.py
git commit -m "feat(run): durable launch records and the per-RepoKey lock"
```

---

### Task 4: `RunController` — launch and receipt

**Files:**
- Create: `dispatcher/core/run_controller.py`
- Test: `tests/test_run_controller.py`

**Interfaces:**
- Consumes: `validate_request`/`RunRejectedError` (Task 2), `RunStore`/`LockBusyError` (Task 3), `RepoKey` (Task 1).
- Produces: `LaunchReceipt(request_id: str, run_id: str | None, accepted: bool | None, reason: str | None)`; `RunController(config: DispatcherConfig, *, poll_interval: float = 0.5, materialize_timeout: float = 120.0)` with `submit(request: RunRequest) -> LaunchReceipt`.

**Design notes for the implementer:**
- `accepted` is three-valued. `False` may be returned **only** when the controller knows no run was created (validation, `busy`, a non-zero exit before publication). Unknown is `None`. The precedent is `merged` in `dispatcher/core/actions.py:580-584`, and collapsing `None` into `False` would invite the caller to retry — the one action spec §5.2.1 forbids.
- The child's environment decides where the run lands. Set `MAESTRO_HOME` **explicitly** from `config.effective_maestro_home` — the resolver, never the raw `maestro_home` field, which is `None` for a deployment that configures only `maestro_db` — and resolve the watch from that same value; inheriting whatever the web process holds is how a healthy launch turns into `launch_unknown` (spec §3.2, `maestro/maestro/state_paths.py:26-29`).
- Watch for the **rename into `runs/`**, not for a directory anywhere: maestro builds under `<project>/.staging/<run_id>` and renames only after the database is closed (`maestro/maestro/run_publish.py:45-73`). Watching `runs/` therefore observes a producer-defined boundary, not a guess.

- [ ] **Step 1: Write the failing test**

`tests/test_run_controller.py`:

```python
"""RunController launch path (spec §5.3, §5.4)."""

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.run_controller import RunController
from dispatcher.core.run_request import RunRequest

_REQ = "11111111-1111-4111-8111-111111111111"


def _repo(root: Path) -> str:
    repo = root / "deployer"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin",
         "git@github.com:owner/deployer.git"],
        check=True,
    )
    (repo / "tasks.yaml").write_text("tasks: []\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def _fake_maestro(path: Path, *, creates_run: str | None, exit_code: int = 0) -> Path:
    """A stand-in that publishes a run directory the way maestro does.

    It reads MAESTRO_HOME from the environment on purpose: a controller that
    launches under one root and watches another must fail this test.
    """
    body = textwrap.dedent(
        f"""
        #!/usr/bin/env python3
        import os, pathlib, sys
        run_id = {creates_run!r}
        if run_id:
            home = pathlib.Path(os.environ["MAESTRO_HOME"])
            runs = home / "projects/github.com/owner/deployer/runs" / run_id
            runs.mkdir(parents=True)
            (runs / "state.db").write_text("")
        sys.exit({exit_code})
        """
    ).strip()
    path.write_text(body + "\n")
    path.chmod(0o755)
    return path


def _config(tmp_path: Path, cli: Path) -> DispatcherConfig:
    return DispatcherConfig(
        roots=(tmp_path / "ws",),
        maestro_home=tmp_path / "mhome",
        run_state_dir=tmp_path / "state",
        maestro_cli=cli,
    )


def _request(revision: str) -> RunRequest:
    return RunRequest(
        request_id=_REQ,
        work_id="todo://deployer/entrypoint-token-boundary-match",
        repository="deployer",
        revision=revision,
        tasks="tasks.yaml",
    )


def test_accepted_true_only_after_the_run_appears(tmp_path: Path) -> None:
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01AAA")
    controller = RunController(_config(tmp_path, cli), materialize_timeout=10.0)
    receipt = controller.submit(_request(head))
    assert receipt.accepted is True
    assert receipt.run_id == "01AAA"


def test_launch_that_never_materializes_is_null_not_false(tmp_path: Path) -> None:
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(
        _config(tmp_path, cli), poll_interval=0.05, materialize_timeout=0.5
    )
    receipt = controller.submit(_request(head))
    assert receipt.accepted is None, "unknown must never be reported as a refusal"
    assert receipt.run_id is None
    assert "unknown" in (receipt.reason or "").lower()


def test_validation_failure_is_accepted_false(tmp_path: Path) -> None:
    _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01AAA")
    controller = RunController(_config(tmp_path, cli))
    receipt = controller.submit(_request("b" * 40))
    assert receipt.accepted is False
    assert receipt.run_id is None


def test_busy_repository_is_accepted_false(tmp_path: Path) -> None:
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(
        _config(tmp_path, cli), poll_interval=0.05, materialize_timeout=0.3
    )
    controller.submit(_request(head))  # leaves launch_unknown, keeps the lock
    second = _request(head).model_copy(
        update={"request_id": "22222222-2222-4222-8222-222222222222"}
    )
    receipt = controller.submit(second)
    assert receipt.accepted is False
    assert "in flight" in (receipt.reason or "")


def test_child_is_launched_with_an_explicit_maestro_home(tmp_path: Path) -> None:
    """The fake binary writes into $MAESTRO_HOME; finding the run proves the
    controller passed the configured home rather than inheriting one."""
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run="01CCC")
    config = _config(tmp_path, cli)
    receipt = RunController(config, materialize_timeout=10.0).submit(_request(head))
    assert receipt.accepted is True
    expected = (
        config.effective_maestro_home / "projects/github.com/owner/deployer/runs/01CCC"
    )
    assert expected.is_dir()


def test_repeated_request_id_does_not_launch_twice(tmp_path: Path) -> None:
    head = _repo(tmp_path / "ws")
    marker = tmp_path / "calls.txt"
    cli = tmp_path / "counting-maestro"
    cli.write_text(
        textwrap.dedent(
            f"""
            #!/usr/bin/env python3
            import os, pathlib
            p = pathlib.Path({str(marker)!r})
            p.write_text(str(int(p.read_text() or 0) + 1 if p.exists() else 1))
            home = pathlib.Path(os.environ["MAESTRO_HOME"])
            d = home / "projects/github.com/owner/deployer/runs/01DDD"
            d.mkdir(parents=True, exist_ok=True)
            (d / "state.db").write_text("")
            """
        ).strip()
        + "\n"
    )
    cli.chmod(0o755)
    controller = RunController(_config(tmp_path, cli), materialize_timeout=10.0)
    first = controller.submit(_request(head))
    second = controller.submit(_request(head))
    assert first.run_id == second.run_id
    assert int(marker.read_text()) == 1, "the second submit must not re-launch"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_run_controller.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'dispatcher.core.run_controller'`

- [ ] **Step 3: Write the implementation**

`dispatcher/core/run_controller.py`:

```python
"""The long-work executor: owns the maestro child process (spec §7.1).

`ActionRunner` is not stretched to cover this and must not be. It is
synchronous by construction — `subprocess.run(..., timeout=120)`
(`dispatcher/core/actions.py:498`, `:51`) behind a process-local
`threading.Lock` (`:351`) — so hosting a run there would produce a web
request holding a process open until it times out, not a control plane.

Short forge actions stay with `ActionRunner`; the steward verdict belongs to
neither, because ARCH-C3 forbids dispatcher computing verdicts at all
(`dispatcher/core/governance.py:10-12`).
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.run_identity import RepoKey, safe_path_parts
from dispatcher.core.run_request import (
    RunRejectedError,
    RunRequest,
    validate_request,
)
from dispatcher.core.run_store import LockBusyError, RunStore

_audit = logging.getLogger("dispatcher.runs")

_POLL_INTERVAL = 0.5
_MATERIALIZE_TIMEOUT = 120.0


class ControlPlaneOff(Exception):
    """`run_state_dir` or `maestro_cli` is unset; there is no control plane."""


class LaunchReceipt(BaseModel):
    """The synchronous answer to a submit (spec §5.3).

    `accepted` is three-valued and the values are not interchangeable:
    `True` — the rename into `runs/` was observed; `False` — refused before
    any run could exist; `None` — `launch_unknown`, a run may or may not
    exist. `False` is a CLAIM and may be made only when the controller knows
    no run was created.
    """

    request_id: str
    run_id: str | None = None
    accepted: bool | None = None
    reason: str | None = None


class RunController:
    """Starts Mode-1 runs and reports what is actually known about them."""

    def __init__(
        self,
        config: DispatcherConfig,
        *,
        poll_interval: float = _POLL_INTERVAL,
        materialize_timeout: float = _MATERIALIZE_TIMEOUT,
    ) -> None:
        self._config = config
        self._poll = poll_interval
        self._timeout = materialize_timeout

    # -- wiring -------------------------------------------------------------

    def _require_on(self) -> tuple[Path, Path, Path]:
        """The three paths the control plane needs, or `ControlPlaneOff`.

        Only `run_state_dir` and `maestro_cli` are switches. `maestro_home`
        is NOT one: the config documents `None` there as "derive from
        `maestro_db.parent`" and exposes that through the
        `effective_maestro_home` property, which the rest of the codebase
        already consumes (`core/service.py:72`). Gating on it would report
        the control plane off for a working, supported config shape.
        """
        state_dir = self._config.run_state_dir
        cli = self._config.maestro_cli
        if state_dir is None or cli is None:
            raise ControlPlaneOff(
                "control plane is off: run_state_dir and maestro_cli must "
                "both be configured"
            )
        # One value, two uses — the child's $MAESTRO_HOME and the watched
        # `runs/` must never be resolved separately.
        return state_dir, cli, self._config.effective_maestro_home

    def _store(self) -> RunStore:
        state_dir, _, _ = self._require_on()
        return RunStore(state_dir)

    def runs_dir(self, key: RepoKey) -> Path:
        """`<maestro-home>/projects/<...>/runs` — the watched directory.

        Built from the CONFIGURED home, which is also what the child is given
        as `$MAESTRO_HOME`. One value, two uses: launching under one root and
        watching another is how a healthy run reads as `launch_unknown`.
        """
        _, _, home = self._require_on()
        return home.joinpath("projects", *safe_path_parts(key), "runs")

    @staticmethod
    def _listing(runs: Path) -> list[str]:
        try:
            return sorted(p.name for p in runs.iterdir() if p.is_dir())
        except OSError:
            # An absent `runs/` is normal before the first run of a repo.
            return []

    # -- submit -------------------------------------------------------------

    def submit(self, request: RunRequest) -> LaunchReceipt:
        """Start one run; return what is known, never more (spec §5.3)."""
        try:
            self._require_on()
        except ControlPlaneOff as err:
            return self._refuse(request.request_id, str(err))

        store = self._store()
        existing = store.get(request.request_id)
        if existing is not None and existing.state != "reserved":
            # Idempotency: a repeated request_id continues or returns the
            # existing record and never starts a second process (spec §5.2).
            return LaunchReceipt(
                request_id=request.request_id,
                run_id=existing.run_id,
                accepted=_accepted_for(existing),
                reason=existing.reason,
            )

        try:
            validated = validate_request(request, self._config)
        except RunRejectedError as err:
            return self._refuse(request.request_id, str(err))

        runs = self.runs_dir(validated.key)
        try:
            store.reserve(
                request.request_id,
                validated.key,
                known_runs=self._listing(runs),
                window_start=datetime.now(UTC).isoformat(),
            )
        except LockBusyError as err:
            return self._refuse(request.request_id, str(err))

        return self._launch(store, request, validated.checkout, validated.key, runs)

    def _launch(
        self,
        store: RunStore,
        request: RunRequest,
        checkout: Path,
        key: RepoKey,
        runs: Path,
    ) -> LaunchReceipt:
        _, cli, home = self._require_on()
        reserved = store.get(request.request_id)
        assert reserved is not None  # just written by `reserve`
        before = set(reserved.known_runs)
        argv = [str(cli), "run", request.tasks]
        env = {**os.environ, "MAESTRO_HOME": str(home)}
        store.mark_launching(request.request_id)
        try:
            child = subprocess.Popen(  # noqa: S603 — argv is a fixed shape
                argv, cwd=str(checkout), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as err:
            # Nothing was executed, so "no run exists" is a fact, not a guess.
            store.mark_terminal(request.request_id, f"launch failed: {err}")
            return self._refuse(request.request_id, f"cannot start maestro: {err}")

        run_id = self._await_materialization(runs, before, child)
        if run_id is not None:
            store.mark_materialized(request.request_id, run_id)
            _audit.info(
                "submit request=%s repo=%s run=%s accepted=True",
                request.request_id, key.as_text(), run_id,
            )
            return LaunchReceipt(
                request_id=request.request_id, run_id=run_id, accepted=True
            )

        reason = (
            "launch_unknown: no run appeared under "
            f"{runs} within {self._timeout:g}s. A run may or may not exist; "
            "resolve it before retrying (spec §5.2.1). The repository lock is "
            "deliberately still held."
        )
        store.mark_unknown(request.request_id, reason)
        _audit.info(
            "submit request=%s repo=%s accepted=None launch_unknown",
            request.request_id, key.as_text(),
        )
        return LaunchReceipt(
            request_id=request.request_id, accepted=None, reason=reason
        )

    def _await_materialization(
        self, runs: Path, before: set[str], child: subprocess.Popen[bytes]
    ) -> str | None:
        """Watch for the rename INTO `runs/` — maestro's own publication point.

        maestro builds the run under `<project>/.staging/<run_id>`, outside
        `runs/`, and renames it in only after the database is closed
        (`maestro/maestro/run_publish.py:45-73`). A new entry here is
        therefore a materialisation defined by the producer.
        """
        deadline = time.monotonic() + self._timeout
        while time.monotonic() < deadline:
            fresh = [name for name in self._listing(runs) if name not in before]
            if len(fresh) == 1:
                return fresh[0]
            if len(fresh) > 1:
                # Two new runs cannot both be ours; the lock is supposed to
                # make this impossible, so it is unknown, not a pick.
                return None
            if child.poll() is not None and not fresh:
                # The child is gone and published nothing. It may still have
                # published between these two observations, so this is
                # unknown rather than a claimed non-launch.
                time.sleep(self._poll)
                late = [n for n in self._listing(runs) if n not in before]
                return late[0] if len(late) == 1 else None
            time.sleep(self._poll)
        return None

    def _refuse(self, request_id: str, reason: str) -> LaunchReceipt:
        _audit.info("submit request=%s accepted=False rejected=%s", request_id, reason)
        return LaunchReceipt(request_id=request_id, accepted=False, reason=reason)


def _accepted_for(record: LaunchRecord) -> bool | None:
    """Map a stored record back to the three-valued receipt.

    Takes the RECORD, not the bare state: `terminal` covers both "the run
    ended" and "the launch failed before any run existed", and only
    `run_id` tells those apart. Deciding from `state` alone would report a
    refusal as `accepted: true` — the exact collapse spec §5.3 forbids.
    """
    if record.state == "materialized":
        return True
    if record.state == "terminal":
        return record.run_id is not None
    if record.state == "launch_unknown":
        return None
    return None
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_run_controller.py -v`
Expected: PASS (6 cases)

- [ ] **Step 5: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add dispatcher/core/run_controller.py tests/test_run_controller.py
git commit -m "feat(run): RunController launch path with a three-valued receipt"
```

---

### Task 5: Leaving `launch_unknown` — adoption and operator resolution

**Files:**
- Modify: `dispatcher/core/run_controller.py`
- Test: `tests/test_run_controller.py` (append)

**Interfaces:**
- Produces: `UnknownResolution(request_id: str, adopted_run_id: str | None, candidates: list[str], reason: str)`; `RunController.resolve_unknown(request_id: str) -> UnknownResolution`; `RunController.end_orphan(request_id: str, run_id: str, outcome: str) -> UnknownResolution`.

**Design notes for the implementer:** the orphan does not clear itself — with no outcome row and no lock holder, maestro classifies it `interrupted` forever (`maestro/maestro/run_state.py:60-70`), and accumulated orphans make every command that resolves without `--run` refuse with `AmbiguousRun` (`maestro/maestro/run_registry.py:190`). Adoption is allowed **only** on exactly one candidate; zero and two-or-more both stay unknown. Never end a run because its timestamp fits.

- [ ] **Step 1: Write the failing tests (append to `tests/test_run_controller.py`)**

```python
def _state(controller: RunController, request_id: str) -> str:
    """The stored state, with the Optional narrowed once instead of per call."""
    record = controller.record(request_id)
    assert record is not None
    return record.state


def _unknown(tmp_path: Path) -> tuple[RunController, Path]:
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    config = _config(tmp_path, cli)
    controller = RunController(config, poll_interval=0.05, materialize_timeout=0.3)
    receipt = controller.submit(_request(head))
    assert receipt.accepted is None
    runs = config.effective_maestro_home / "projects/github.com/owner/deployer/runs"
    runs.mkdir(parents=True, exist_ok=True)
    return controller, runs


def test_exactly_one_candidate_is_adopted(tmp_path: Path) -> None:
    controller, runs = _unknown(tmp_path)
    (runs / "01LATE").mkdir()
    resolution = controller.resolve_unknown(_REQ)
    assert resolution.adopted_run_id == "01LATE"
    assert _state(controller, _REQ) == "materialized"


def test_zero_candidates_stays_unknown(tmp_path: Path) -> None:
    controller, _ = _unknown(tmp_path)
    resolution = controller.resolve_unknown(_REQ)
    assert resolution.adopted_run_id is None
    assert _state(controller, _REQ) == "launch_unknown"


def test_two_candidates_are_never_guessed_between(tmp_path: Path) -> None:
    controller, runs = _unknown(tmp_path)
    (runs / "01AAA").mkdir()
    (runs / "01BBB").mkdir()
    resolution = controller.resolve_unknown(_REQ)
    assert resolution.adopted_run_id is None
    assert sorted(resolution.candidates) == ["01AAA", "01BBB"]
    assert _state(controller, _REQ) == "launch_unknown"


def test_adoption_releases_the_lock(tmp_path: Path) -> None:
    controller, runs = _unknown(tmp_path)
    (runs / "01LATE").mkdir()
    controller.resolve_unknown(_REQ)
    head = _repo_head(tmp_path / "ws")
    second = _request(head).model_copy(
        update={"request_id": "33333333-3333-4333-8333-333333333333"}
    )
    assert controller.submit(second).accepted is not False


def test_end_orphan_refuses_a_run_outside_the_candidate_set(tmp_path: Path) -> None:
    controller, runs = _unknown(tmp_path)
    (runs / "01AAA").mkdir()
    (runs / "01BBB").mkdir()
    with pytest.raises(RunRejectedError, match="not a candidate"):
        controller.end_orphan(_REQ, "01ZZZ", "cancelled")


def test_end_orphan_rejects_an_outcome_outside_the_operator_endings(
    tmp_path: Path,
) -> None:
    controller, runs = _unknown(tmp_path)
    (runs / "01AAA").mkdir()
    with pytest.raises(RunRejectedError, match="cancelled|superseded"):
        controller.end_orphan(_REQ, "01AAA", "completed")
```

Add this helper beside `_repo` at the top of the module (the adoption test needs the head of an already-created repo):

```python
def _repo_head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root / "deployer"), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_run_controller.py -k "unknown or adopt or orphan or candidate" -v`
Expected: FAIL — `AttributeError: 'RunController' object has no attribute 'resolve_unknown'`

- [ ] **Step 3: Write the implementation (append to `run_controller.py`)**

Add the operator endings constant beside the other module constants:

```python
#: The two endings a run cannot observe about itself
#: (`maestro/maestro/cli.py:1973`).
_OPERATOR_ENDINGS = frozenset({"cancelled", "superseded"})
```

Add the resolution model beside `LaunchReceipt`:

```python
class UnknownResolution(BaseModel):
    """What a `launch_unknown` resolution attempt could and could not decide."""

    request_id: str
    adopted_run_id: str | None = None
    candidates: list[str] = Field(default_factory=list)
    reason: str = ""
```

Add these methods to `RunController`:

```python
    def record(self, request_id: str):  # -> LaunchRecord | None
        """The stored launch record, for the API and for tests."""
        return self._store().get(request_id)

    def _candidates(self, request_id: str) -> tuple[list[str], RepoKey]:
        record = self._store().get(request_id)
        if record is None:
            raise RunRejectedError(f"no launch record for {request_id}")
        parts = record.repo_key.split("/")
        key = (
            RepoKey(host="", owner="", repo=parts[1], local=True)
            if parts[0] == "_local"
            else RepoKey(host=parts[0], owner=parts[1], repo=parts[2])
        )
        before = set(record.known_runs)
        fresh = [n for n in self._listing(self.runs_dir(key)) if n not in before]
        return fresh, key

    def resolve_unknown(self, request_id: str) -> UnknownResolution:
        """Adopt an orphan only under unambiguous correlation (spec §5.2.1).

        Exactly one new run relative to the pre-launch snapshot is adopted.
        Zero and two-or-more both remain `launch_unknown`: a heuristic
        adoption attributes work to a request that may not have produced it,
        and every later control verb would then act on a stranger's run.
        """
        candidates, _ = self._candidates(request_id)
        if len(candidates) == 1:
            adopted = candidates[0]
            self._store().mark_materialized(request_id, adopted)
            _audit.info("resolve request=%s adopted=%s", request_id, adopted)
            return UnknownResolution(
                request_id=request_id,
                adopted_run_id=adopted,
                candidates=candidates,
                reason="exactly one new run correlated; adopted",
            )
        reason = (
            "no new run to correlate; the launch may never have started"
            if not candidates
            else (
                f"{len(candidates)} new runs correlate equally well; "
                "attribution is ambiguous in principle. Name the exact run_id "
                "and end it — the best-fitting timestamp is not evidence."
            )
        )
        _audit.info(
            "resolve request=%s adopted=None candidates=%d", request_id, len(candidates)
        )
        return UnknownResolution(
            request_id=request_id, candidates=candidates, reason=reason
        )

    def end_orphan(
        self, request_id: str, run_id: str, outcome: str
    ) -> UnknownResolution:
        """Operator resolution: end a NAMED orphan, then release the lock.

        The run must be one the correlation actually offered — ending a run
        that merely fits the launch window is forbidden for the same reason
        automatic adoption is (spec §5.2.1).
        """
        if outcome not in _OPERATOR_ENDINGS:
            raise RunRejectedError(
                f"outcome must be one of cancelled|superseded, got {outcome!r}: "
                "'completed' and 'failed' are recorded by the run itself"
            )
        candidates, key = self._candidates(request_id)
        if run_id not in candidates:
            raise RunRejectedError(
                f"{run_id} is not a candidate for {request_id}; "
                f"candidates are: {', '.join(candidates) or 'none'}"
            )
        _, cli, home = self._require_on()
        proc = subprocess.run(
            [str(cli), "run-end", run_id, "--outcome", outcome],
            capture_output=True,
            text=True,
            env={**os.environ, "MAESTRO_HOME": str(home)},
            timeout=_VERB_TIMEOUT,
        )
        if proc.returncode != 0:
            raise RunRejectedError(
                f"maestro run-end refused: {proc.stderr.strip() or proc.stdout.strip()}"
            )
        store = self._store()
        store.mark_terminal(request_id, outcome)
        _audit.info("end-orphan request=%s run=%s outcome=%s", request_id, run_id, outcome)
        return UnknownResolution(
            request_id=request_id,
            adopted_run_id=run_id,
            candidates=candidates,
            reason=f"operator ended {run_id} as {outcome}; lock released",
        )
```

`_VERB_TIMEOUT` is defined in Task 6; if Task 6 is not yet done, add `_VERB_TIMEOUT = 120` beside the other constants now and leave it there.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_run_controller.py -v`
Expected: PASS (12 cases)

- [ ] **Step 5: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add dispatcher/core/run_controller.py tests/test_run_controller.py
git commit -m "feat(run): resolve launch_unknown by adoption or named run-end"
```

---

### Task 6: The Mode-1 control verbs

**Files:**
- Modify: `dispatcher/core/run_controller.py`
- Test: `tests/test_run_controller.py` (append)

**Interfaces:**
- Produces: `VerbOutcome(verb: str, run_id: str, ok: bool, stdout: str, stderr: str)`; `RunController.control(request_id: str, verb: str, *, task_id: str | None = None, outcome: str | None = None) -> VerbOutcome`.

**Design notes:** `approve` (`maestro/maestro/cli.py:1207`) releases a task sitting in `AWAITING_APPROVAL`; a task in `TaskStatus.NEEDS_REVIEW` is cleared by `retry`, whose retryable set is `{FAILED, NEEDS_REVIEW}` (`maestro/maestro/cli.py:940`). Do not wire a control labelled "approve" to a review outcome. Mode-2 workstream verbs must not be added here.

**Both `approve` and `retry` take a required positional task id** — `maestro approve [OPTIONS] TASK_ID` and `maestro retry [OPTIONS] TASK_ID` — and both accept `--run`. Refuse either without one. `stop` is not implemented at all (see Global Constraints). Every argv shape in this task was checked by running `maestro <verb> --help`; do not re-derive them by reading `cli.py`.

- [ ] **Step 1: Write the failing tests (append)**

```python
def _materialized(tmp_path: Path, script: str) -> RunController:
    head = _repo(tmp_path / "ws")
    cli = tmp_path / "verb-maestro"
    cli.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(script).strip() + "\n")
    cli.chmod(0o755)
    controller = RunController(_config(tmp_path, cli), materialize_timeout=10.0)
    controller.submit(_request(head))
    return controller


_PUBLISH_THEN_ECHO = """
import os, pathlib, sys
home = pathlib.Path(os.environ["MAESTRO_HOME"])
d = home / "projects/github.com/owner/deployer/runs/01AAA"
if not d.exists():
    d.mkdir(parents=True)
    (d / "state.db").write_text("")
    sys.exit(0)
print(" ".join(sys.argv[1:]))
"""


def test_status_is_addressed_to_the_adopted_run(tmp_path: Path) -> None:
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    outcome = controller.control(_REQ, "status")
    assert outcome.ok
    assert "--run 01AAA" in outcome.stdout


def test_verb_outside_the_allowlist_is_refused(tmp_path: Path) -> None:
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    with pytest.raises(RunRejectedError, match="not allowlisted"):
        controller.control(_REQ, "workstream-continue")


def test_approve_requires_a_task_id(tmp_path: Path) -> None:
    controller = _materialized(tmp_path, _PUBLISH_THEN_ECHO)
    with pytest.raises(RunRejectedError, match="task_id"):
        controller.control(_REQ, "approve")


def test_verbs_refuse_a_request_with_no_run_yet(tmp_path: Path) -> None:
    head = _repo(tmp_path / "ws")
    cli = _fake_maestro(tmp_path / "fake-maestro", creates_run=None)
    controller = RunController(
        _config(tmp_path, cli), poll_interval=0.05, materialize_timeout=0.3
    )
    controller.submit(_request(head))
    with pytest.raises(RunRejectedError, match="no run"):
        controller.control(_REQ, "status")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_run_controller.py -k "verb or status or approve" -v`
Expected: FAIL — `AttributeError: 'RunController' object has no attribute 'control'`

- [ ] **Step 3: Write the implementation (append)**

Beside the other constants:

```python
#: Mode-1 only, minus `stop`. `submit` is not here either — it is the
#: long-lived launch operation, not a short verb. Mode-2 verbs serve
#: `orchestrate` and stay outside the slice: one request type must not
#: control two state machines.
#:
#: `stop` is absent although spec §6 names it. Verified by running the
#: producer: `maestro stop [OPTIONS]` takes no `--run` and no positional —
#: it stops the SCHEDULER PROCESS. Offering it as an action on one request's
#: run would silently end every other run that scheduler is managing.
_VERBS = frozenset({"status", "retry", "approve", "run-end"})
_VERB_TIMEOUT = 120
```

Beside the other models:

```python
class VerbOutcome(BaseModel):
    verb: str
    run_id: str
    ok: bool
    stdout: str = ""
    stderr: str = ""
```

On `RunController`:

```python
    def control(
        self,
        request_id: str,
        verb: str,
        *,
        task_id: str | None = None,
        outcome: str | None = None,
    ) -> VerbOutcome:
        """Run one allowlisted Mode-1 verb against this request's run."""
        if verb not in _VERBS:
            raise RunRejectedError(f"verb not allowlisted: {verb!r}")
        record = self._store().get(request_id)
        if record is None or record.run_id is None:
            raise RunRejectedError(
                f"{request_id} has no run to act on "
                f"(state: {record.state if record else 'absent'})"
            )
        run_id = record.run_id
        _, cli, home = self._require_on()

        # argv shapes verified by running `maestro <verb> --help`, never by
        # reading the source: this table was wrong for two of five verbs when
        # derived by reading. `approve` and `retry` each take a REQUIRED
        # positional task id plus `--run`; `run-end` takes a positional run id
        # plus `--outcome` and no `--run`; `status` takes `--run` alone.
        if verb in {"approve", "retry"}:
            if not task_id:
                raise RunRejectedError(
                    f"{verb} needs a task_id. `approve` releases a task sitting "
                    "in AWAITING_APPROVAL; a task in NEEDS_REVIEW is cleared by "
                    "`retry` instead — both address one task, not the whole run"
                )
            argv = [str(cli), verb, task_id, "--run", run_id]
        elif verb == "run-end":
            if outcome not in _OPERATOR_ENDINGS:
                raise RunRejectedError(
                    f"run-end outcome must be cancelled|superseded, got {outcome!r}"
                )
            argv = [str(cli), verb, run_id, "--outcome", outcome]
        else:
            argv = [str(cli), verb, "--run", run_id]

        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env={**os.environ, "MAESTRO_HOME": str(home)},
            timeout=_VERB_TIMEOUT,
        )
        _audit.info(
            "verb=%s request=%s run=%s ok=%s", verb, request_id, run_id,
            proc.returncode == 0,
        )
        if verb == "run-end" and proc.returncode == 0:
            self._store().mark_terminal(request_id, outcome or "ended")
        return VerbOutcome(
            verb=verb,
            run_id=run_id,
            ok=proc.returncode == 0,
            stdout=proc.stdout.strip(),
            stderr=proc.stderr.strip(),
        )
```

If Task 5 already added a placeholder `_VERB_TIMEOUT`, remove the duplicate.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_run_controller.py -v`
Expected: PASS (16 cases)

- [ ] **Step 5: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add dispatcher/core/run_controller.py tests/test_run_controller.py
git commit -m "feat(run): Mode-1 control verbs behind an allowlist"
```

---

### Task 7: HTTP surface

**Files:**
- Modify: `dispatcher/server/app.py`
- Test: `tests/test_run_api.py`

**Interfaces:**
- Consumes: `RunController`, `LaunchReceipt`, `UnknownResolution`, `VerbOutcome`, `ControlPlaneOff`, `RunRejectedError`.
- Produces: `POST /api/runs/submit`, `GET /api/runs/{request_id}`, `POST /api/runs/{request_id}/resolve`, `POST /api/runs/{request_id}/verb`.

**Design note:** every mutating endpoint takes the existing `X-Action-Token` header and compares it to `action_token`, exactly as `_run_action` does (`dispatcher/server/app.py:353-366`). The read endpoint takes no token, like `pr_detail`.

- [ ] **Step 1: Write the failing test**

`tests/test_run_api.py`:

```python
"""HTTP surface of the control plane (spec §5.3, §6)."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.server.app import create_app


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    config = DispatcherConfig(roots=(tmp_path / "ws",))
    (tmp_path / "ws").mkdir()
    return TestClient(create_app(config))


def _token(client: TestClient) -> str:
    return client.get("/api/actions/session").json()["token"]


def _body() -> dict:
    return {
        "request_id": "11111111-1111-4111-8111-111111111111",
        "work_id": "todo://deployer/entrypoint-token-boundary-match",
        "repository": "deployer",
        "revision": "a" * 40,
        "tasks": "tasks.yaml",
    }


def test_submit_requires_the_action_token(client: TestClient) -> None:
    assert client.post("/api/runs/submit", json=_body()).status_code == 403


def test_submit_with_the_control_plane_off_is_a_refusal_not_a_crash(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/runs/submit",
        json=_body(),
        headers={"X-Action-Token": _token(client)},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["accepted"] is False
    assert "control plane is off" in payload["reason"]


def test_unknown_request_reads_404(client: TestClient) -> None:
    assert client.get("/api/runs/nope").status_code == 404


def test_verb_outside_the_allowlist_is_422(client: TestClient) -> None:
    response = client.post(
        "/api/runs/11111111-1111-4111-8111-111111111111/verb",
        json={"verb": "workstream-continue"},
        headers={"X-Action-Token": _token(client)},
    )
    assert response.status_code == 422
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_run_api.py -v`
Expected: FAIL — 404 on `/api/runs/submit` (route not registered)

- [ ] **Step 3: Add the endpoints**

In `dispatcher/server/app.py`, add to the imports:

```python
from dispatcher.core.run_controller import (
    ControlPlaneOff,
    LaunchReceipt,
    RunController,
    UnknownResolution,
    VerbOutcome,
)
from dispatcher.core.run_request import RunRejectedError, RunRequest
from dispatcher.core.run_store import LaunchRecord
```

Add the request bodies beside `ActionRequest`:

```python
class ResolveRequest(BaseModel):
    """POST /api/runs/{id}/resolve — optional named orphan to end."""

    run_id: str | None = None
    outcome: str | None = None


class VerbRequest(BaseModel):
    """POST /api/runs/{id}/verb — one Mode-1 control verb."""

    verb: str
    task_id: str | None = None
    outcome: str | None = None
```

Beside `actions = ActionRunner(config)`:

```python
    runs = RunController(config)
```

And the routes, after the existing action endpoints:

```python
    def _require_token(token: str | None) -> None:
        if token != action_token:
            raise HTTPException(status_code=403, detail="bad or missing action token")

    @app.post("/api/runs/submit", response_model=LaunchReceipt)
    def submit_run(
        request: RunRequest,
        x_action_token: str | None = Header(default=None),
    ) -> LaunchReceipt:
        """Explicit human click: start one Mode-1 run (spec §5.3).

        Every outcome is a receipt, including a refusal: `accepted` is
        three-valued and `null` (launch_unknown) is not an error.
        """
        _require_token(x_action_token)
        return runs.submit(request)

    @app.get("/api/runs/{request_id}", response_model=LaunchRecord)
    def read_run(request_id: str) -> LaunchRecord:
        try:
            record = runs.record(request_id)
        except ControlPlaneOff as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        if record is None:
            raise HTTPException(status_code=404, detail=f"no record: {request_id}")
        return record

    @app.post("/api/runs/{request_id}/resolve", response_model=UnknownResolution)
    def resolve_run(
        request_id: str,
        request: ResolveRequest,
        x_action_token: str | None = Header(default=None),
    ) -> UnknownResolution:
        """Adopt an unambiguous orphan, or end the one the operator names."""
        _require_token(x_action_token)
        try:
            if request.run_id is not None:
                return runs.end_orphan(
                    request_id, request.run_id, request.outcome or ""
                )
            return runs.resolve_unknown(request_id)
        except RunRejectedError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        except ControlPlaneOff as err:
            raise HTTPException(status_code=409, detail=str(err)) from err

    @app.post("/api/runs/{request_id}/verb", response_model=VerbOutcome)
    def run_verb(
        request_id: str,
        request: VerbRequest,
        x_action_token: str | None = Header(default=None),
    ) -> VerbOutcome:
        _require_token(x_action_token)
        try:
            return runs.control(
                request_id,
                request.verb,
                task_id=request.task_id,
                outcome=request.outcome,
            )
        except RunRejectedError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        except ControlPlaneOff as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
```

`RunController.record` must not raise when the control plane is off for the read path — wrap its `_store()` call so `ControlPlaneOff` propagates as written above, and confirm `test_unknown_request_reads_404` passes.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_run_api.py -v`
Expected: PASS (4 cases)

- [ ] **Step 5: Run the whole suite**

Run: `uv run pytest`
Expected: PASS — no regressions in `tests/test_api.py`

- [ ] **Step 6: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add dispatcher/server/app.py tests/test_run_api.py
git commit -m "feat(run): HTTP surface for submit, read, resolve and control verbs"
```

---

### Task 8: Read the run back from maestro's own store

**Files:**
- Modify: `dispatcher/core/run_controller.py`
- Test: `tests/test_run_api.py` (append)

**Interfaces:**
- Produces: `RunView(record: LaunchRecord, run: OrchestrationRunInfo | None)`; `RunController.view(request_id: str) -> RunView`; `GET /api/runs/{request_id}` returns `RunView`.

**Design note:** dispatcher must not restate maestro's FSM (spec §3.2). `MaestroCollector` already walks `<maestro_home>/projects/<...>/runs/<run-id>/state.db` and classifies fail-closed (`dispatcher/core/collectors/maestro.py:80-114`); this task joins `request_id → run_id` to what that collector already produces, and adds no second classification.

- [ ] **Step 1: Write the failing test (append to `tests/test_run_api.py`)**

```python
def test_view_joins_the_request_to_maestros_own_run_row(tmp_path: Path) -> None:
    """dispatcher renders maestro's FSM; it does not restate it (spec §3.2)."""
    from conftest import make_maestro_run

    from dispatcher.core.run_controller import RunController
    from dispatcher.core.run_identity import RepoKey
    from dispatcher.core.run_store import RunStore

    home = tmp_path / "mhome"
    key = RepoKey(host="github.com", owner="owner", repo="deployer")
    make_maestro_run(
        home,
        key.as_path_parts(),
        "01AAA",
        started_at="2026-08-22T00:00:00Z",
        outcome="completed",
    )
    config = DispatcherConfig(
        roots=(tmp_path / "ws",),
        maestro_home=home,
        run_state_dir=tmp_path / "state",
        maestro_cli=tmp_path / "unused-maestro",
    )
    (tmp_path / "ws").mkdir()
    store = RunStore(tmp_path / "state")
    store.reserve("req-1", key, known_runs=[], window_start="t")
    store.mark_materialized("req-1", "01AAA")

    view = RunController(config).view("req-1")
    assert view.record.run_id == "01AAA"
    assert view.run is not None
    assert view.run.status == "completed"
```

- [ ] **Step 2: Use the builder that already exists — do not add one**

`tests/conftest.py:50` already has `make_maestro_run(home, repo_parts, run_id, *,
started_at, outcome=None, ...)`, writing the same layout the collector walks. Adding a
second builder would give the suite two fixtures that must agree about maestro's schema.
Reuse it, and import it the way this suite does — `from conftest import ...`, not
`from tests.conftest import ...` (`tests/` is not a package; see `tests/test_api.py:11`).

No conftest change is needed for this task.

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_run_api.py -k view -v`
Expected: FAIL — `AttributeError: 'RunController' object has no attribute 'view'`

- [ ] **Step 4: Give the collector one public classifier, then reuse it**

`MaestroCollector.collect` takes `(path, ctx)` and returns a snapshot
(`dispatcher/core/collectors/maestro.py:67`), which is the dashboard's shape, not a
lookup. Rather than calling it or re-deriving status, extract the classification loop
into one public helper and have both callers use it — one classifier, two callers.

In `dispatcher/core/collectors/maestro.py`, add:

```python
def classified_runs(
    home: Path | None, snap: ProjectSnapshot
) -> list[tuple[OrchestrationRunInfo, Path]]:
    """Every run under `home`, classified once, with its `state.db`.

    The single place run status is decided. `_collect_runs` layers its extra
    work (tasks, logs, freshness sources) on top; the control plane
    (`core/run_controller.py`) uses this alone, so a request's status and the
    dashboard's can never disagree.
    """
    if home is None:
        return []
    out: list[tuple[OrchestrationRunInfo, Path]] = []
    for repo_key, project_dir in _project_dirs(home / "projects", snap):
        holder = _holder_run_id(project_dir / "locks")
        for run_dir in _subdirs(project_dir / "runs", snap):
            db = run_dir / "state.db"
            if not db.is_file():
                continue
            out.append((_classify_run(db, repo_key, run_dir.name, holder, snap), db))
    return out
```

Then rewrite `_collect_runs` to consume it, grouping by `info.repo_key` so its
per-project sort and its "newest run gets tasks and logs" behaviour are unchanged:

```python
    def _collect_runs(self, home: Path | None, snap: ProjectSnapshot) -> list[Path]:
        """Enumerate per-project run DBs; returns freshness sources."""
        sources: list[Path] = []
        by_project: dict[str, list[tuple[OrchestrationRunInfo, Path]]] = {}
        for info, db in classified_runs(home, snap):
            sources.append(db)
            by_project.setdefault(info.repo_key, []).append((info, db))
        for _, runs in sorted(by_project.items()):
            runs.sort(
                key=lambda r: (r[0].started_at or "", r[0].run_id or ""),
                reverse=True,
            )
            snap.runs.extend(info for info, _ in runs)
            newest_db = runs[0][1]
            self._collect_run_tasks(newest_db, snap)
            logs_dir = newest_db.parent / "logs"
            snap.errors.extend(read_otel_errors(logs_dir))
            # Everything read must feed freshness: telemetry can land under
            # the run's logs/ after the last state.db write.
            sources.append(logs_dir)
        return sources
```

`tests/test_maestro.py` is the regression guard for this refactor — it pins the
warning prefixes and the per-project ordering. Run it before moving on.

- [ ] **Step 5: Write the view (append to `run_controller.py`)**

```python
class RunView(BaseModel):
    """dispatcher's request, joined to maestro's own row for that run."""

    record: LaunchRecord
    run: OrchestrationRunInfo | None = None
```

```python
    def view(self, request_id: str) -> RunView:
        """The request plus maestro's classification of its run (spec §3.2).

        Status is read through the collector's one classifier; nothing here
        re-derives liveness. A `run_id` whose directory is gone yields
        `run=None` — absent, not invented.

        The home comes from `_require_on()`, never from `config.maestro_home`
        directly: that field is `None` for a deployment configuring only
        `maestro_db`, and `classified_runs(None, ...)` returns `[]`, which
        would make every lookup report `run=None` while the dashboard finds
        the run perfectly well.
        """
        _, _, home = self._require_on()
        record = self._store().get(request_id)
        if record is None:
            raise RunRejectedError(f"no launch record for {request_id}")
        if record.run_id is None:
            return RunView(record=record)
        # A throwaway snapshot: `classified_runs` reports unreadable sources
        # into it, and those warnings belong to the dashboard's snapshot, not
        # to this lookup.
        scratch = ProjectSnapshot(name="maestro")
        match = next(
            (
                info
                for info, _ in classified_runs(home, scratch)
                if info.run_id == record.run_id
                and info.repo_key == record.repo_key
            ),
            None,
        )
        return RunView(record=record, run=match)
```

with the imports:

```python
from dispatcher.core.collectors.maestro import classified_runs
from dispatcher.core.models import OrchestrationRunInfo, ProjectSnapshot
from dispatcher.core.run_store import LaunchRecord, LockBusyError, RunStore
```

- [ ] **Step 6: Point the read endpoint at the view**

In `dispatcher/server/app.py`, change `read_run`'s `response_model` to `RunView` and its body to call `runs.view(request_id)`, mapping **both** `RunRejectedError` and `ControlPlaneOff` to 404 — with the control plane off there is no record to show, and a 500 would read as a server fault rather than a feature that is switched off:

```python
    @app.get("/api/runs/{request_id}", response_model=RunView)
    def read_run(request_id: str) -> RunView:
        try:
            return runs.view(request_id)
        except (RunRejectedError, ControlPlaneOff) as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_run_api.py tests/test_maestro.py -v`
Expected: PASS

- [ ] **Step 8: Full suite, format, lint, typecheck, commit**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add dispatcher/core/run_controller.py dispatcher/core/collectors/maestro.py \
  dispatcher/server/app.py tests/test_run_api.py tests/conftest.py
git commit -m "feat(run): join a request to maestro's own run classification"
```

---

## After the plan

**The UI is not in this plan, and slice 0 is not complete without it.** Spec §2.1 and §9
both say the request is issued "from the dispatcher UI", and pass 1's acceptance criterion
is "`RunRequest` from the UI". This plan's File Structure has no entry under
`dispatcher/server/static/` and no task creates one. After all eight tasks an operator must
fetch the CSRF token from `/api/actions/session` and `curl` the endpoint. Narrowing slice 0
to the server half is a defensible call — it is the hard half — but it was never stated,
and the sentence below originally claimed the dispatcher half was finished. It is not:
**pass 1 cannot be accepted on this plan alone.** The UI is owed its own plan.

Slice 0's dispatcher *server* half is done when Task 8 lands. Three things are then true and should be stated rather than assumed:

1. **The one-launch invariant is still a working agreement, not a mechanism** (spec §5.4.1). The durable lock binds `RunController` only; a terminal, a second controller instance, or a service tick can still launch against the same `RepoKey`.
2. **Logs are read outside dispatcher** (spec §10). Cheap to close later — maestro writes them to `<run_dir>/logs` (`maestro/maestro/run_publish.py:59`) — but it is not in this plan.
3. **Pass 1 cannot run on dispatcher alone.** It needs deployer's `tasks.yaml` and the pilot fix, which are a separate plan in a separate repository, and the red/green evidence comes from the run because deployer's CI runs no tests (spec §9.2).

## What the whole-branch review found that the per-task reviews could not

Eight task-scoped reviews each saw one task's diff. The whole-branch review saw the branch
against the producer's code, and found one Critical and six Important that were invisible
task by task. Recording them because they say what this plan's structure could not check:

- **The run's repository is chosen by the `tasks.yaml`, not by the request.** maestro's
  Mode-1 identity comes from the DAG's own required `repo:`/`repo_url:` field
  (`maestro/maestro/models.py:871`, `maestro/maestro/repo_identity.py:103-135`), never from
  the child's cwd. This plan validated `repository` and `revision` against a checkout
  maestro might never touch, took the per-`RepoKey` lock on it, and watched its `runs/`.
  §4.2 treated `tasks` as an opaque path to check for reachability. **This is a spec defect
  as much as a plan defect** — spec §3.2 reasons carefully about `MAESTRO_HOME` deciding
  *where* a run lands and never asks what decides *under which key*.
- A launch whose child died immediately held the repository lock with no in-band release
  and no diagnostics; spec §5.2.1 names three release conditions and the plan assigned the
  third no task.
- The `Popen` lacked `start_new_session=True`, so a `Ctrl-C` in the server's terminal killed
  the run — the opposite of what spec §7.1 requires.
- `LaunchRecord` persisted no request body, so spec §3.1's five-way join — the stated reason
  dispatcher owns a store at all — lived nowhere. Neither this plan's own self-review nor any
  per-task review caught it.

## Self-review

**Spec coverage.** §2.2 non-goals — no compiler, no Mode 2, no work-item registry, no merge, no `merge_authority`: nothing in tasks 1–8 introduces any of them. §3.1 dispatcher owns only the request — Task 3. §3.2 read-back without restating — Task 8. §4 `RunRequest` and validation — Task 2. §5.1 maestro allocates `run_id` — Task 4 never passes `--run` on `submit`. §5.2/§5.2.1 states and exits — Tasks 3 and 5. §5.3 three-valued `accepted` bound to the rename — Task 4. §5.4/§5.4.1 durable lock and its limits — Task 3, restated in "After the plan". §6 Mode-1 allowlist — Task 6. §7.1 `RunController` separate from `ActionRunner` — Task 4. §7.2/§7.3 forge actions and the verdict producer path — untouched by design; no task adds a steward call. §7.4 credential boundary — the controller sets the child environment (Task 4); no credentials move into the web process. §8 closing the loop and §9 acceptance are deployer-side or manual in pass 1, named in "After the plan".

**Gap found and filled:** the two-guarantees rule would have been silently half-met by Task 1's vendored mirror; Step 7 of that task files the missing drift watch as a named TODO item instead.

**Placeholders:** none — every code step carries the actual code. Task 8 originally guessed `MaestroCollector.collect(ctx, snap)`; the real signature is `collect(path, ctx) -> ProjectSnapshot` (`dispatcher/core/collectors/maestro.py:67`), so that task now extracts a `classified_runs` helper instead of calling the collector wrongly.

**Second pass — Copilot review plus a pre-execution conflict scan.** Nine defects were
found in the first draft of this plan and fixed here:

1. `submit` was listed in the Global Constraints allowlist and omitted from `_VERBS` —
   a contradiction that would have exposed the launch through the control-verb path.
2. `_accepted_for(state)` returned `True` for `terminal`, which also covers a launch that
   failed before any run existed — a refusal reported as an acceptance, the exact
   collapse §5.3 forbids. It now takes the record and reads `run_id`.
3. The pinned cases table claimed the producer rejects `owner='..'`. It does not:
   `parse_remote_url("git@github.com:owner/../etc.git")` returns `('github.com', '..',
   'etc')`, verified against maestro cb91759. The mirror stays faithful and dispatcher
   guards its own joins with `safe_path_parts`; the producer-side hole is filed as a
   maestro inbox issue.
4. Pydantic list defaults used bare `[]` against the house `Field(default_factory=list)`
   (`dispatcher/core/models.py:150-157`).
5. Task 8 added a `make_maestro_run` builder that **already exists** at
   `tests/conftest.py:50` — two fixtures would have had to agree about maestro's schema.
6. That task also imported it as `from tests.conftest import`; `tests/` is not a package
   and this suite uses `from conftest import` (`tests/test_api.py:11`).
7. Tests dereferenced `store.get(...)` and `controller.record(...)` without narrowing the
   `| None` — a pyrefly failure in a plan whose own constraints demand a clean run.
8. `RunStore` raised `LockBusyError` for an unsafe `request_id`, where nothing is in
   flight and the input is simply bad. Added `RunStoreError` as the base.
9. The idempotency test's counter did `len(read_text())` instead of `int(...)`, so it
   would have passed without measuring anything.

**Sixth pass — caught during execution of Task 8, and it is a repeat.** `view()` read
`config.maestro_home` directly instead of `effective_maestro_home`, so for any deployment
configuring only `maestro_db` the lookup returns `[]` and every read-back reports
`run=None`. This is the SAME confusion corrected in Task 4 — and the correction was
patched only into the task that surfaced it, never swept across the plan. Four call sites
carried it forward. Process lesson worth more than the fix: **a mid-execution plan
correction must be swept over the whole plan, not applied where it was found.**

**Fifth pass — caught during execution of Task 6, and the worst of them.** The verb argv
table was wrong for two of five verbs. `maestro retry` takes a REQUIRED positional task
id, which this plan routed only into `approve`, so `retry` could never have succeeded.
And `maestro stop` takes no `--run` and no positional at all — it stops the scheduler
process, so wiring it as a per-request verb would have shipped a UI control that ends
every other run. `stop` is removed from the allowlist and flagged for the spec. Both were
found by running `maestro <verb> --help`; both would have survived another reading.

**Fourth pass — caught during execution of Task 4.** `_require_on` treated
`config.maestro_home is None` as `ControlPlaneOff`. In the real `DispatcherConfig` only
`run_state_dir` and `maestro_cli` carry "None → off"; `maestro_home = None` means "derive
from `maestro_db.parent`" via the `effective_maestro_home` property, already consumed at
`core/service.py:72`. The plan's version would have switched the control plane off for a
working config. Corrected above, with the one-wire invariant made explicit in the code
rather than only in prose.

**Third pass — caught during execution of Task 1.** The replacement reject case
`git@github.com:owner/x..y.git` was wrong too: `_UNSAFE` permits dots and only an exact
`repo == ".."` is checked, so the producer *accepts* `x..y` (verified against cb91759).
Replaced with two cases the producer genuinely rejects — `owner/..` (the one traversal
form it does catch) and a character outside `[A-Za-z0-9._-]`. Both verified by running
the producer, not by reading it. Twice now this table asserted a rejection the rule does
not make: **verify a pinned behaviour table by executing the producer, never by reading
its source.**

**Type consistency:** `RepoKey.as_path_parts()`/`as_text()` (T1) are used unchanged in T3, T4, T5. `RunRejectedError` is raised by T2 and caught in T4, T5, T6, T7. `LaunchRecord.state` values match `_LOCK_HELD_STATES` and `_accepted_for`. `_OPERATOR_ENDINGS` is defined once (T5) and reused by T6. `_VERB_TIMEOUT` is flagged in T5 and owned by T6 to avoid a duplicate definition.
