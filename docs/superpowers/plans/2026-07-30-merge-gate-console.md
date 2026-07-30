# Merge-gate console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn dispatcher's read plane into a merge-gate console: show one pull request's full state on a screen, and let a human collapse "merge → switch to default → ff-pull → prune" into a single click that keeps the per-repo lock for the whole operation.

**Architecture:** dispatcher gains no new authority over GitHub. It shells out to the `github-checker` verbs added by that repo's companion plan (`pr-detail`, `merge --if-head`, `post-merge-sync`), exactly as today's `pull` / `open-pr` do. The one genuinely new mechanism is a **composite action**: `ActionRunner` today releases its per-repo lock after a single `_invoke`, so two separate calls would let another action wedge between merge and sync. `merge-and-sync` holds the lock across both steps. Because a merged PR cannot be un-merged, the outcome is modelled as two independent facts — `merged` and `local_sync` — rather than one boolean.

**Tech Stack:** Python 3.11+, FastAPI, pydantic v2, vanilla-JS single-file SPA (`dispatcher/server/static/index.html`), pytest, uv.

**Depends on:** `github-checker` verbs from `github-checker/docs/superpowers/plans/2026-07-30-merge-gate-verbs.md`. That plan lands **first**. Every test here fakes the `github-checker` binary (the established pattern in `tests/test_actions.py:29-33`), so this plan is implementable and testable before the real verbs ship — but it must not be *released* before them.

**Design source:** `_cowork_output/2026-07-30-dispatcher-operator-console-design.md` §5.4-5.5. That file lives in the dev-only cowork workspace; shipped code never reads it. Everything needed is restated here.

## Global Constraints

- Package management: **uv only**. `uv run pytest`, `uv run ruff`. Never `pip`.
- Line length **88**. `uv run ruff format .` then `uv run ruff check . --fix` before each commit.
- Type hints on all functions; docstrings on public ones.
- `uv run pyrefly check` must report **0 errors** before each commit. CI runs it
  in two jobs (`typecheck` and `test`), and `master` is clean, so any new error
  is ours — narrow `Optional` access in new code and tests instead of shipping it.
- **dispatcher never calls the GitHub API itself.** Every GitHub read or mutation goes through the `github-checker` binary. This boundary is the reason the whole design stays inside ADR-ECO-004's D1 constraint.
- **Human-click-only.** Every mutating endpoint requires the `X-Action-Token` header, mirroring `dispatcher/server/app.py:270-283`. No auto-merge, no background triggering.
- **Every attempt leaves an audit line**, including rejected (422) and busy (409) ones — the invariant stated in `dispatcher/core/actions.py:84-85`.
- **No second SSOT.** dispatcher stores no PR state; it reads through to `github-checker` on every request.
- Existing suite must stay green: `uv run pytest`.
- Comments are sparse and explain *why*; Russian is used where a guard needs justifying. Match that, don't narrate.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `dispatcher/core/actions.py` | **modify** — `Action` whitelist grows; `ActionOutcome` gains `merged` / `local_sync` / `gate_failed` / `pr_detail`; lock acquisition extracted into a context manager; `merge_and_sync()` and `pr_detail()` added. |
| `dispatcher/server/app.py` | **modify** — three endpoints: `GET /api/pr-detail`, `POST /api/actions/merge-and-sync`, `POST /api/actions/post-merge-sync`. |
| `dispatcher/server/static/index.html` | **modify** — merge-gate view. |
| `README.md` | **modify** — the self-description at line 9 stops being true. |
| `tests/test_actions.py` | **modify** — composite action, lock span, outcome parsing. |
| `tests/test_api.py` | **modify** — endpoint contracts, token gating, 409/422. |

**Deliberately not created: a vendored `PrDetail` pydantic model.** `pr_detail` is
carried as an opaque `dict` passthrough for S1 — a **provisional adapter**, explicitly
recorded as temporary debt.

Be precise about why, because the obvious reason is the wrong one. The shape is *not*
"still being designed": it stabilised when github-checker#14 merged (`f05cf8d`). The
actual reason is that **the canonical contract does not exist yet to be vendored.**
`github-checker/contracts/` holds only `snapshot/v1`; `contracts/actions/v1` is still
an open producer-side item (`github-checker/TODO.md`, `@id:contracts-actions-v1`,
owned by that repo), and this repo's dependent item already exists and is correctly
blocked on it (`TODO.md`, `@id:vendor-contracts-actions-v1`,
`@blocked_by:github-checker#contracts-actions-v1`). Until the producer publishes,
there is nothing canonical to pin a copy of.

Blocking S1 on that publication would be wrong too: it would couple this console to a
much broader contract pass over *every* headless action, which is a separate piece of
work with its own owner.

**Constraints on the provisional mode — these are not optional, they are what makes a
passthrough acceptable instead of reckless:**

1. **Validate before rendering.** Every field the UI depends on must be checked for
   presence *and* type before it is used. A passthrough is not permission to trust the
   payload's shape.
2. **An incomplete or unexpected payload renders as "cannot read PR" with Merge
   disabled** — never as a partially-drawn screen, and never as a green gate. This is
   the same fail-closed rule the producer's own gate follows: what cannot be read is
   not clean.
3. **Add a consumer test against the real verb's actual output**, pinned to
   `pr-detail` at github-checker `f05cf8d`, as a fixture or a live smoke. The producer
   item itself records that this contract "проверяется у потребителя live-смоуком" —
   so consumer-side verification is the established mechanism here, not an extra
   invention. Without it, "passthrough" means "unverified".
4. **Do not declare a local pydantic model to be the contract.** Adding a typed model
   here and calling it authoritative would create exactly the thing this defers: a
   second, unpinned copy of someone else's schema. A validation helper is fine; a
   "contract" is not.
5. **Do not close `@id:vendor-contracts-actions-v1` in this PR.** It stays open,
   blocked on the producer, and is discharged only when the real contract lands and a
   pinned copy is vendored per ADR-ECO-003.

The canonical end state is unchanged: github-checker publishes `contracts/actions/v1`
in the shape of its frozen `contracts/snapshot/v1/` (schema beside golden fixtures),
then this repo vendors a pinned copy.

---

### Task 1: Outcome fields and lock-span refactor

**Files:**
- Modify: `dispatcher/core/actions.py`
- Test: `tests/test_actions.py` (append)

**Interfaces:**
- Consumes: existing `ActionRunner`, `ActionOutcome`, `ActionBusyError`, `ActionRejectedError`.
- Produces: `ActionOutcome` with `merged: bool | None`, `local_sync: str | None`, `gate_failed: list[str] | None`, `pr_detail: dict[str, Any] | None`; a private `ActionRunner._hold(action, repo_dir)` context manager yielding the validated target `Path`; `_invoke` accepting trailing argv.

**Behaviour that must not change:** `run("pull", ...)` and `run("open-pr", ...)` keep their exact current semantics, audit lines and error types. This task is a refactor plus new optional fields.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_actions.py
def test_outcome_carries_merge_and_sync_fields(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "post-merge-sync",
        "dir": "alpha",
        "ok": True,
        "local_sync": "ok",
        "detail": "synced master",
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.run("post-merge-sync", "alpha")
    assert outcome.ok is True
    assert outcome.local_sync == "ok"


def test_post_merge_sync_is_whitelisted_but_junk_is_not(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(DispatcherConfig(roots=(tmp_path,)))
    with pytest.raises(ActionRejectedError, match="not whitelisted"):
        runner.run("rm-rf", "alpha")  # type: ignore[arg-type]


def test_gate_failure_fields_survive_the_round_trip(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "merge",
        "dir": "alpha",
        "ok": False,
        "merged": False,
        "local_sync": "not_attempted",
        "gate_failed": ["not-draft", "threads-resolved"],
        "error": "merge gate refused: not-draft, threads-resolved",
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner._invoke("merge", tmp_path / "alpha", "7", "--if-head", "a" * 40)
    assert outcome.merged is False
    assert outcome.gate_failed == ["not-draft", "threads-resolved"]


def test_invoke_passes_extra_argv_through(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    script = tmp_path / "echo_argv.py"
    script.write_text(
        "import sys, json;"
        "json.dump({'action':'merge','dir':'alpha','ok':True,"
        "'detail':' '.join(sys.argv[1:])}, sys.stdout)"
    )
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    outcome = runner._invoke("merge", tmp_path / "alpha", "7", "--if-head", "abc")
    assert "--if-head abc" in (outcome.detail or "")
    assert outcome.detail.startswith("merge ")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_actions.py -k "merge or whitelisted or argv" -v`
Expected: FAIL — `ActionOutcome` has no `local_sync`; `_invoke` takes no extra argv

- [ ] **Step 3: Write the implementation**

In `dispatcher/core/actions.py`:

```python
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

Action = Literal["pull", "open-pr", "post-merge-sync"]
_WHITELIST = frozenset({"pull", "open-pr", "post-merge-sync"})
```

Add to `ActionOutcome` (after `changed_paths`):

```python
    merged: bool | None = None
    local_sync: str | None = None  # ok | failed | not_attempted | not_applicable
    gate_failed: list[str] | None = None
    pr_detail: dict[str, Any] | None = None
```

Replace the acquire/release logic in `run()` with a shared context manager:

```python
    @contextmanager
    def _hold(self, action: str, repo_dir: str) -> Iterator[Path]:
        """Own the repo for a whole operation — composite actions included.

        The lock must span every step: releasing between merge and sync
        would let another action wedge into the gap.
        """
        try:
            target = self._target(repo_dir)
            with self._lock:
                if repo_dir in self._busy:
                    raise ActionBusyError(f"{repo_dir}: action already in flight")
                self._busy.add(repo_dir)
        except (ActionRejectedError, ActionBusyError) as err:
            _audit.info(
                "action=%s repo=%s ok=False rejected=%s", action, repo_dir, err
            )
            raise
        try:
            yield target
        finally:
            with self._lock:
                self._busy.discard(repo_dir)

    def run(self, action: Action, repo_dir: str) -> ActionOutcome:
        """Execute one whitelist action; EVERY attempt leaves an audit line."""
        if action not in _WHITELIST:
            _audit.info(
                "action=%s repo=%s ok=False rejected=not whitelisted",
                action,
                repo_dir,
            )
            raise ActionRejectedError(f"action not whitelisted: {action!r}")
        with self._hold(action, repo_dir) as target:
            outcome = self._invoke(action, target)
        self._audit_outcome(action, repo_dir, outcome)
        return outcome

    def _audit_outcome(
        self, action: str, repo_dir: str, outcome: ActionOutcome
    ) -> None:
        _audit.info(
            "action=%s repo=%s ok=%s merged=%s local_sync=%s detail=%s error=%s",
            action,
            repo_dir,
            outcome.ok,
            outcome.merged,
            outcome.local_sync,
            outcome.detail,
            outcome.error,
        )
```

Note the whitelist check now happens **before** `_hold`, so an unwhitelisted action
can never take the lock.

Change `_invoke` to accept trailing argv and to read the new fields:

```python
    def _invoke(self, action: str, target: Path, *extra: str) -> ActionOutcome:
        argv = [*self._command, action, str(target), *extra]
```

and in the success branch, add to the returned `ActionOutcome(...)`:

```python
            merged=data.get("merged"),
            local_sync=data.get("local_sync"),
            gate_failed=data.get("gate_failed"),
            pr_detail=data.get("pr_detail"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_actions.py -v`
Expected: PASS — new tests green **and** every pre-existing test in the file still green

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && uv run ruff check . --fix
git add dispatcher/core/actions.py tests/test_actions.py
git commit -m "refactor(actions): lock-span context manager and merge outcome fields"
```

---

### Task 2: The composite `merge-and-sync` action

**Files:**
- Modify: `dispatcher/core/actions.py`
- Test: `tests/test_actions.py` (append)

**Interfaces:**
- Consumes: `_hold`, `_invoke` (Task 1).
- Produces: `ActionRunner.merge_and_sync(repo_dir: str, pr: int, if_head: str) -> ActionOutcome`
  and `ActionRunner.pr_detail(repo_dir: str, pr: int) -> ActionOutcome`.

**The outcome model — the point of the whole task:**

| `merged` | `local_sync` | `ok` | Meaning |
|----------|--------------|------|---------|
| `False` | `not_attempted` | `False` | gate refused or remote rejected; nothing mutated |
| `None` | `not_attempted` | `False` | transport failure (timeout/missing binary/unparseable output); whether it merged is genuinely unknown |
| `True` | `ok` | `True` | green path |
| `True` | `failed` | **`True`** | PR is merged; the local clone needs attention |
| `True` | `not_applicable` | `True` | no local clone; nothing to sync |

`ok == merged` for the composite. A failed local sync must **not** drag `ok` to
`False`: callers and the audit log would then read a merged PR as unfinished work,
and a merged PR cannot be retried. The `local_sync` field carries that warning
instead.

`merged=False` and `merged=None` are not interchangeable on a failed merge:
`False` means github-checker actually answered — a parsed gate refusal — while
`None` means we never got a readable answer at all, so we don't get to claim
the PR didn't land. Consumers (Tasks 3-4) must render `merged=None` as
"unknown — check the PR", never as "not merged".

`pr_detail` takes **no lock** — it is a read, and a read must not queue behind an
in-flight action or block one.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_actions.py
import json


def scripted_checker(tmp_path: Path, by_action: dict[str, dict]) -> tuple[str, ...]:
    """A fake github-checker answering differently per verb, recording argv."""
    script = tmp_path / "scripted_checker.py"
    script.write_text(
        "import sys, json, pathlib\n"
        f"table = {by_action!r}\n"
        f"log = pathlib.Path({str(tmp_path / 'calls.log')!r})\n"
        "action = sys.argv[1]\n"
        "with log.open('a') as fh:\n"
        "    fh.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "payload = table[action]\n"
        "json.dump(payload, sys.stdout)\n"
        "sys.exit(0 if payload.get('ok') else 1)\n"
    )
    return ("python3", str(script))


def read_calls(tmp_path: Path) -> list[str]:
    log = tmp_path / "calls.log"
    return log.read_text().splitlines() if log.exists() else []


HEAD = "a" * 40


def test_green_path_merges_then_syncs(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)),
        command=scripted_checker(
            tmp_path,
            {
                "merge": {"action": "merge", "dir": "alpha", "ok": True,
                          "merged": True, "detail": "squash-merged"},
                "post-merge-sync": {"action": "post-merge-sync", "dir": "alpha",
                                    "ok": True, "local_sync": "ok"},
            },
        ),
    )
    outcome = runner.merge_and_sync("alpha", 7, HEAD)
    assert outcome.ok is True
    assert outcome.merged is True
    assert outcome.local_sync == "ok"
    calls = read_calls(tmp_path)
    assert calls[0].startswith(f"merge {tmp_path / 'alpha'} 7 --if-head {HEAD}")
    assert calls[1].startswith("post-merge-sync")


def test_gate_refusal_never_reaches_the_sync_step(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)),
        command=scripted_checker(
            tmp_path,
            {
                "merge": {"action": "merge", "dir": "alpha", "ok": False,
                          "merged": False, "gate_failed": ["threads-resolved"],
                          "error": "merge gate refused: threads-resolved"},
                "post-merge-sync": {"action": "post-merge-sync", "dir": "alpha",
                                    "ok": True, "local_sync": "ok"},
            },
        ),
    )
    outcome = runner.merge_and_sync("alpha", 7, HEAD)
    assert outcome.ok is False
    assert outcome.merged is False
    assert outcome.local_sync == "not_attempted"
    assert outcome.gate_failed == ["threads-resolved"]
    assert [c.split()[0] for c in read_calls(tmp_path)] == ["merge"]


def test_merged_but_sync_failed_is_not_reported_as_failure(tmp_path: Path) -> None:
    """The PR is merged and cannot be un-merged; ok must follow `merged`."""
    make_repo(tmp_path, "alpha")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)),
        command=scripted_checker(
            tmp_path,
            {
                "merge": {"action": "merge", "dir": "alpha", "ok": True,
                          "merged": True},
                "post-merge-sync": {"action": "post-merge-sync", "dir": "alpha",
                                    "ok": False, "local_sync": "failed",
                                    "error": "working tree is dirty"},
            },
        ),
    )
    outcome = runner.merge_and_sync("alpha", 7, HEAD)
    assert outcome.merged is True
    assert outcome.local_sync == "failed"
    assert outcome.ok is True
    assert "dirty" in (outcome.error or "")


def test_lock_is_held_across_both_steps(tmp_path: Path) -> None:
    """Nothing may wedge between merge and post-merge-sync."""
    make_repo(tmp_path, "alpha")
    started = threading.Event()
    release = threading.Event()
    script = tmp_path / "blocking_checker.py"
    script.write_text(
        "import sys, json, pathlib, time\n"
        f"flag = pathlib.Path({str(tmp_path / 'in_merge')!r})\n"
        f"gate = pathlib.Path({str(tmp_path / 'go')!r})\n"
        "action = sys.argv[1]\n"
        "if action == 'merge':\n"
        "    flag.touch()\n"
        "    while not gate.exists():\n"
        "        time.sleep(0.01)\n"
        "    json.dump({'action':'merge','dir':'alpha','ok':True,'merged':True},"
        " sys.stdout)\n"
        "else:\n"
        "    json.dump({'action':'post-merge-sync','dir':'alpha','ok':True,"
        "'local_sync':'ok'}, sys.stdout)\n"
    )
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    result: list[ActionOutcome] = []
    worker = threading.Thread(
        target=lambda: result.append(runner.merge_and_sync("alpha", 7, HEAD))
    )
    worker.start()
    while not (tmp_path / "in_merge").exists():
        started.wait(0.01)
    with pytest.raises(ActionBusyError):
        runner.run("pull", "alpha")
    (tmp_path / "go").touch()
    worker.join(timeout=10)
    release.set()
    assert result[0].ok is True
    runner.run("pull", "alpha")  # lock released once the composite finished


def test_pr_detail_passes_through_without_taking_the_lock(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "pr-detail",
        "dir": "alpha",
        "ok": True,
        "pr_detail": {"number": 7, "head_sha": HEAD, "is_draft": False},
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.pr_detail("alpha", 7)
    assert outcome.ok is True
    assert outcome.pr_detail["number"] == 7
    assert runner._busy == set()


def test_pr_detail_still_validates_the_repo_dir(tmp_path: Path) -> None:
    runner = ActionRunner(DispatcherConfig(roots=(tmp_path,)))
    with pytest.raises(ActionRejectedError, match="unsafe"):
        runner.pr_detail("../etc", 7)
```

Ensure `tests/test_actions.py` imports `ActionOutcome` alongside the existing names.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_actions.py -k "merge_and_sync or lock_is_held or pr_detail or green_path or gate_refusal" -v`
Expected: FAIL — `AttributeError: 'ActionRunner' object has no attribute 'merge_and_sync'`

- [ ] **Step 3: Write the implementation**

Append to `ActionRunner` in `dispatcher/core/actions.py`:

```python
    def merge_and_sync(self, repo_dir: str, pr: int, if_head: str) -> ActionOutcome:
        """Merge one PR and re-sync the clone, holding the repo lock throughout.

        `ok` follows `merged`: a merged PR is finished work even when the local
        sync afterwards refuses, and it cannot be retried — the warning rides on
        `local_sync` instead of flipping the operation to failed.
        """
        with self._hold("merge-and-sync", repo_dir) as target:
            merge = self._invoke("merge", target, str(pr), "--if-head", if_head)
            self._audit_outcome("merge", repo_dir, merge)
            if not merge.ok:
                merge.action = "merge-and-sync"
                merge.merged = False
                merge.local_sync = "not_attempted"
                self._audit_outcome("merge-and-sync", repo_dir, merge)
                return merge
            sync = self._invoke("post-merge-sync", target)
            self._audit_outcome("post-merge-sync", repo_dir, sync)

        outcome = ActionOutcome(
            action="merge-and-sync",
            dir=merge.dir,
            ok=True,  # == merged
            merged=True,
            local_sync=sync.local_sync or ("ok" if sync.ok else "failed"),
            detail=merge.detail,
            error=sync.error,
            pr_url=merge.pr_url,
            branch=sync.branch,
            local_behind=sync.local_behind,
            local_dirty=sync.local_dirty,
        )
        self._audit_outcome("merge-and-sync", repo_dir, outcome)
        return outcome

    def pr_detail(self, repo_dir: str, pr: int) -> ActionOutcome:
        """Read one PR through github-checker. A read takes no lock."""
        target = self._target(repo_dir)
        outcome = self._invoke("pr-detail", target, str(pr))
        _audit.info(
            "action=pr-detail repo=%s pr=%s ok=%s", repo_dir, pr, outcome.ok
        )
        return outcome
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_actions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
uv run ruff format . && uv run ruff check . --fix
git add dispatcher/core/actions.py tests/test_actions.py
git commit -m "feat(actions): composite merge-and-sync holding the repo lock"
```

---

### Task 3: API endpoints

**Files:**
- Modify: `dispatcher/server/app.py`
- Test: `tests/test_api.py` (append)

**Interfaces:**
- Consumes: `ActionRunner.merge_and_sync`, `.pr_detail`, `.run` (Tasks 1-2).
- Produces:
  - `GET /api/pr-detail?dir=<repo>&pr=<n>` → `ActionOutcome` (read; no token)
  - `POST /api/actions/merge-and-sync` body `{dir, pr, if_head}` → `ActionOutcome` (token required)
  - `POST /api/actions/post-merge-sync` body `{dir}` → `ActionOutcome` (token required)

**Status-code contract, matching `_run_action` at `dispatcher/server/app.py:270-283`:**
`ActionRejectedError` → 422, `ActionBusyError` → 409, bad/missing token → 403.
On any outcome with `ok=true`, invalidate `sync_cache` — repo state changed.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_api.py
def test_merge_and_sync_requires_the_action_token(client) -> None:
    response = client.post(
        "/api/actions/merge-and-sync",
        json={"dir": "alpha", "pr": 7, "if_head": "a" * 40},
    )
    assert response.status_code == 403


def test_merge_and_sync_returns_the_composite_outcome(client, action_token) -> None:
    response = client.post(
        "/api/actions/merge-and-sync",
        json={"dir": "alpha", "pr": 7, "if_head": "a" * 40},
        headers={"X-Action-Token": action_token},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "merge-and-sync"
    assert body["merged"] is True
    assert body["local_sync"] == "ok"


def test_merge_and_sync_maps_busy_to_409(client, action_token, monkeypatch) -> None:
    from dispatcher.core.actions import ActionBusyError

    def busy(*args, **kwargs):
        raise ActionBusyError("alpha: action already in flight")

    monkeypatch.setattr("dispatcher.core.actions.ActionRunner.merge_and_sync", busy)
    response = client.post(
        "/api/actions/merge-and-sync",
        json={"dir": "alpha", "pr": 7, "if_head": "a" * 40},
        headers={"X-Action-Token": action_token},
    )
    assert response.status_code == 409


def test_merge_and_sync_maps_rejection_to_422(client, action_token) -> None:
    response = client.post(
        "/api/actions/merge-and-sync",
        json={"dir": "../etc", "pr": 7, "if_head": "a" * 40},
        headers={"X-Action-Token": action_token},
    )
    assert response.status_code == 422


def test_pr_detail_is_readable_without_a_token(client) -> None:
    response = client.get("/api/pr-detail", params={"dir": "alpha", "pr": 7})
    assert response.status_code == 200
    assert response.json()["pr_detail"]["number"] == 7


def test_post_merge_sync_endpoint_retries_the_local_half(client, action_token) -> None:
    response = client.post(
        "/api/actions/post-merge-sync",
        json={"dir": "alpha"},
        headers={"X-Action-Token": action_token},
    )
    assert response.status_code == 200
    assert response.json()["local_sync"] == "ok"
```

Reuse `tests/test_api.py`'s existing `client` fixture. If it does not already expose
an `action_token` fixture, add one that reads `GET /api/actions/session` and returns
`response.json()["token"]`, and point the app's `ActionRunner` at a scripted fake
`github-checker` the same way `tests/test_actions.py` does.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api.py -k "merge_and_sync or pr_detail or post_merge_sync" -v`
Expected: FAIL — 404, the routes do not exist

- [ ] **Step 3: Write the implementation**

Add request models next to `ActionRequest` in `dispatcher/server/app.py`:

```python
class MergeRequest(BaseModel):
    """POST /api/actions/merge-and-sync body."""

    dir: str
    pr: int
    if_head: str
```

Add the endpoints after `action_create_pr`:

```python
    @app.get("/api/pr-detail", response_model=ActionOutcome)
    def pr_detail(dir: str, pr: int) -> ActionOutcome:
        """Read-through to github-checker; no mutation, so no token."""
        try:
            return actions.pr_detail(dir.strip(), pr)
        except ActionRejectedError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    @app.post("/api/actions/merge-and-sync", response_model=ActionOutcome)
    def action_merge_and_sync(
        request: MergeRequest,
        x_action_token: str | None = Header(default=None),
    ) -> ActionOutcome:
        """Явный клик человека: squash-merge + локальная синхронизация одним локом."""
        if x_action_token != action_token:
            raise HTTPException(status_code=403, detail="bad or missing action token")
        try:
            outcome = actions.merge_and_sync(
                request.dir.strip(), request.pr, request.if_head.strip()
            )
        except ActionRejectedError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        except ActionBusyError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        if outcome.ok:
            sync_cache.invalidate()
        return outcome

    @app.post("/api/actions/post-merge-sync", response_model=ActionOutcome)
    def action_post_merge_sync(
        request: ActionRequest,
        x_action_token: str | None = Header(default=None),
    ) -> ActionOutcome:
        """Добор локальной половины, когда merge прошёл, а sync — нет."""
        return _run_action("post-merge-sync", request, x_action_token)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api.py -v`
Expected: PASS

- [ ] **Step 5: Pin a consumer fixture of the real verb's output**

`pr_detail` is an unvalidated passthrough, so nothing so far proves this code agrees
with what `github-checker` actually emits — every test above uses a hand-written fake.
That is the gap this step closes, and it is a required constraint of the provisional
adapter, not a nice-to-have. The producer's own contract item records that this
contract is "проверяется у потребителя live-смоуком", so consumer-side verification is
the established mechanism.

Capture the real output once and commit it as a fixture, pinned to the producer commit
it came from:

```bash
# Requires github-checker >= f05cf8d on PATH and an authenticated gh.
# Use any real PR in a repo you can read; the values don't matter, the SHAPE does.
github-checker pr-detail <path-to-a-real-clone> <pr-number> \
  > tests/fixtures/pr_detail_github_checker_f05cf8d.json
```

Record the producer version beside it so a future reader knows what it pins:

```bash
github-checker --version 2>/dev/null || git -C <path-to-github-checker> rev-parse HEAD
```

Then add a test asserting the fixture satisfies every field the UI requires — the same
list the browser checks, kept in one place on the Python side so a producer change
fails a test rather than a screen:

```python
# append to tests/test_api.py
import json
from pathlib import Path

# Mirrors MG_REQUIRED in index.html. If github-checker's payload changes shape,
# this fails here — loudly, in CI — instead of silently blanking the merge-gate
# screen at runtime.
PR_DETAIL_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "number": int,
    "title": str,
    "url": str,
    "state": str,
    "is_draft": bool,
    "mergeable": str,
    "head_branch": str,
    "head_sha": str,
    "base_branch": str,
    "checks": list,
    "files": list,
    "review_threads": list,
}

# Legitimately nullable, so checked for PRESENCE plus type — see the matching
# comment in index.html. `review_decision` especially: its predicate passes on
# null, so a field that vanished would read as "no review required".
PR_DETAIL_NULLABLE: dict[str, type | tuple[type, ...]] = {
    "review_decision": str,
    "allows_squash": bool,
}

FIXTURE = Path(__file__).parent / "fixtures" / "pr_detail_github_checker_f05cf8d.json"


def test_real_pr_detail_payload_has_every_field_the_console_reads() -> None:
    """Consumer check against github-checker's ACTUAL output, not a fake.

    Provisional-adapter guard: `pr_detail` is an opaque passthrough until
    `contracts/actions/v1` is published and vendored
    (TODO @id:vendor-contracts-actions-v1).
    """
    envelope = json.loads(FIXTURE.read_text())
    assert envelope["ok"] is True
    detail = envelope["pr_detail"]
    missing = [
        key
        for key, expected in PR_DETAIL_REQUIRED.items()
        if not isinstance(detail.get(key), expected)
    ]
    # Nullable fields: the KEY must exist, and its value must be null or the
    # expected type. `.get()` would conflate "absent" with "null" — which for
    # review_decision is the difference between "cannot read" and "no review
    # required".
    missing += [
        key
        for key, expected in PR_DETAIL_NULLABLE.items()
        if key not in detail
        or not (detail[key] is None or isinstance(detail[key], expected))
    ]
    assert missing == [], f"github-checker payload no longer provides: {missing}"
```

Two Python details worth not tripping over:

- `isinstance(True, int)` is `True`, so a boolean would satisfy an `int` entry. The
  dict is checked per-key against its own expected type, so it is safe as written —
  just do not "simplify" it into a scheme that tests a value against several types.
- `detail.get(key)` cannot distinguish an absent key from a `null` one, which is
  exactly the distinction that matters for `review_decision`. That is why the nullable
  fields use `key not in detail` instead.

If you cannot produce the fixture (no authenticated `gh`, no readable PR), do NOT skip
this step silently and do NOT invent a fixture by hand — a fabricated fixture would
assert that the producer emits whatever we guessed. Stop and report it; the controller
decides whether to defer with a written ruling.

- [ ] **Step 6: Commit**

```bash
uv run ruff format . && uv run ruff check . --fix
git add dispatcher/server/app.py tests/test_api.py \
  tests/fixtures/pr_detail_github_checker_f05cf8d.json
git commit -m "feat(api): pr-detail read and merge-and-sync action endpoints"
```

---

### Task 4: The merge-gate view

**Files:**
- Modify: `dispatcher/server/static/index.html`
- Test: manual, plus the smoke assertion below

**Interfaces:**
- Consumes: `GET /api/pr-detail`, `POST /api/actions/merge-and-sync`, `POST /api/actions/post-merge-sync`, and `ensureActionToken()` (already defined at `index.html:328`).
- Produces: a `#merge-gate` section.

**Screen contents (design §5.4):** PR header and branch; CI status and governance;
review threads; changed-file list; per-file diff; a Merge button.

**The three UI rules that carry the design's meaning:**
1. The Merge button is **disabled** unless every predicate is green. The screen reads `pr_detail` and applies the same predicate names github-checker uses, so the two agree — but the screen is a convenience, not the authority. A refusal from the server is normal and must render clearly.

**⚠ Known duplication, and why it is tolerable — read before editing `MG_PREDICATES`.**
This list restates in JavaScript a rule whose authority lives in
`github-checker`'s `evaluate_gate`. That is duplication, and it can drift: the
gate grew from seven predicates to nine during github-checker's own
implementation (`checks-complete` and `threads-complete` were added after
review found two ways an incomplete read could read as "all clear"), and
`approvals` changed from a blocklist to an allowlist so that an unrecognised
future GitHub enum value blocks rather than passes. This list already had to be
rewritten once for that.

It is tolerable only because the two copies are not equally load-bearing: the
server's copy decides, this copy only greys out a button. A drifted screen
shows a wrong hint; it can never merge something the gate would refuse.

If this drifts again, do not fix it by being more careful — fix it by deleting
it. The right follow-up is to have `github-checker`'s `pr-detail` return the
gate verdict itself (`gate.passed` + `gate.failed`), so the screen renders a
verdict instead of recomputing one. That is a small, additive change to
`pr-detail`, and it removes this whole class of bug. It is deliberately not in
S1 because `pr-detail` shipped before this consumer existed.
2. `merged=true, local_sync=failed` renders as a **warning, not an error**: "PR merged; local sync needs attention: <reason>", with a "Retry local sync" button hitting `/api/actions/post-merge-sync`. Never show a merged PR as a failure.
3. Truncation is visible: when `files_truncated`, `diff_truncated` or `threads_truncated` is set, say so and link out to the PR.

- [ ] **Step 1: Add the markup**

Insert after the `#detail-section` block (around `index.html:139`):

```html
<section id="merge-gate" hidden>
  <h2>Merge gate <span id="mg-title" class="sub"></span></h2>
  <div id="mg-head" class="fresh"></div>
  <div id="mg-gate" class="fresh"></div>
  <div id="mg-threads" class="fresh"></div>
  <div id="mg-files" class="fresh"></div>
  <pre id="mg-diff" hidden></pre>
  <div id="mg-truncation" class="fresh" hidden></div>
  <button id="mg-merge" type="button" disabled>Merge</button>
  <button id="mg-retry-sync" type="button" hidden>Retry local sync</button>
  <span id="mg-result"></span>
</section>
```

- [ ] **Step 2: Add the rendering and action logic**

Add inside the existing `<script>` block, following the file's `renderX` style and
reusing its `esc()` helper:

```javascript
let mgRepo = null, mgPr = null, mgHead = null;

// Mirrors github-checker's evaluate_gate — NINE predicates, same names, same
// order. Kept in sync by hand; see the drift warning under this block.
const MG_PREDICATES = [
  ['open',             d => d.state === 'OPEN'],
  ['not-draft',        d => d.is_draft === false],
  ['mergeable',        d => d.mergeable === 'MERGEABLE'],
  ['checks-green',     d => (d.checks || []).every(
                            c => ['SUCCESS','NEUTRAL','SKIPPED'].includes(c.state))],
  ['checks-complete',  d => d.checks_truncated !== true],
  ['approvals',        d => d.review_decision == null
                            || d.review_decision === 'APPROVED'],
  ['threads-resolved', d => (d.review_threads || []).every(t => t.is_resolved)],
  ['threads-complete', d => d.threads_truncated !== true],
  ['squash-allowed',   d => d.allows_squash === true],
];

// `pr_detail` is an unvalidated passthrough from github-checker (see the
// provisional-adapter constraints in File Structure). Check presence AND type
// of everything the screen reads, before reading it. A payload we cannot fully
// understand renders as "cannot read PR" with Merge disabled — never as a
// half-drawn screen, and never as a green gate: what cannot be read is not
// clean. Note `checks_truncated`/`threads_truncated` are NOT required here —
// their absence means "not truncated", which is the safe reading, and the
// predicates already treat only `=== true` as blocking.
const MG_REQUIRED = [
  ['number',       v => Number.isInteger(v)],
  ['title',        v => typeof v === 'string'],
  ['url',          v => typeof v === 'string'],
  ['state',        v => typeof v === 'string'],
  ['is_draft',     v => typeof v === 'boolean'],
  ['mergeable',    v => typeof v === 'string'],
  ['head_branch',  v => typeof v === 'string'],
  ['head_sha',     v => typeof v === 'string' && v.length > 0],
  ['base_branch',  v => typeof v === 'string'],
  ['checks',       v => Array.isArray(v)],
  ['files',        v => Array.isArray(v)],
  ['review_threads', v => Array.isArray(v)],
  // These two are legitimately null — null carries meaning, so require the key
  // to be PRESENT and of a known type rather than requiring a value.
  //
  // `review_decision` is the dangerous one and the reason this pair is listed
  // separately: its predicate passes on null (null = "this repo requires no
  // review"), and in JS `undefined == null` is true — so a payload that simply
  // lost the field would pass `approvals` as though no review were required.
  // Requiring presence here turns that into "cannot read PR".
  //
  // `allows_squash` is already safe by construction (its predicate demands
  // `=== true`, so a missing field blocks) — it is listed anyway so a missing
  // field reports the real reason instead of a confusing `squash-allowed`
  // refusal on a repo where squash is in fact allowed.
  ['review_decision', v => v === null || typeof v === 'string'],
  ['allows_squash',   v => v === null || typeof v === 'boolean'],
];

function mgPayloadProblems(d) {
  if (d === null || typeof d !== 'object') return ['pr_detail is not an object'];
  return MG_REQUIRED
    .filter(([k, ok]) => !ok(d[k]))
    .map(([k]) => `${k} missing or wrong type`);
}

function mgCannotRead(reason) {
  document.getElementById('mg-result').textContent = `cannot read PR: ${reason}`;
  document.getElementById('mg-merge').disabled = true;
  document.getElementById('mg-gate').innerHTML =
    '<span class="badge">unknown</span> payload not understood';
}

async function openMergeGate(repoDir, pr) {
  mgRepo = repoDir; mgPr = pr; mgHead = null;
  document.getElementById('merge-gate').hidden = false;
  document.getElementById('mg-result').textContent = 'loading…';
  document.getElementById('mg-merge').disabled = true;
  const res = await fetch(
    `/api/pr-detail?dir=${encodeURIComponent(repoDir)}&pr=${pr}`);
  const outcome = await res.json();
  if (!outcome.ok || !outcome.pr_detail) {
    mgCannotRead(outcome.error || res.status);
    return;
  }
  const problems = mgPayloadProblems(outcome.pr_detail);
  if (problems.length) {
    mgCannotRead(problems.join('; '));
    return;
  }
  renderMergeGate(outcome.pr_detail);
}

function renderMergeGate(d) {
  mgHead = d.head_sha;
  document.getElementById('mg-title').textContent = `#${d.number} ${d.title}`;
  document.getElementById('mg-head').innerHTML =
    `<a href="${esc(d.url)}" target="_blank">${esc(d.url)}</a> · ` +
    `${esc(d.head_branch)} → ${esc(d.base_branch)} · ${esc(d.head_sha.slice(0, 8))}`;

  const failed = MG_PREDICATES.filter(([, ok]) => !ok(d)).map(([name]) => name);
  document.getElementById('mg-gate').innerHTML = failed.length
    ? `<span class="badge">blocked</span> ${failed.map(esc).join(', ')}`
    : '<span class="badge">gate green</span>';

  const open = (d.review_threads || []).filter(t => !t.is_resolved);
  document.getElementById('mg-threads').innerHTML = open.length
    ? `${open.length} unresolved thread(s): ` + open.map(
        t => `${esc(t.author || '?')} — ${esc(t.excerpt || '')}`).join('<br>')
    : 'no unresolved threads';

  document.getElementById('mg-files').innerHTML = (d.files || []).map(
    f => `<div>${esc(f.path)} <span class="sub">+${f.additions} −${f.deletions}` +
         `</span></div>`).join('');
  const diff = document.getElementById('mg-diff');
  diff.hidden = !d.diff;
  diff.textContent = d.diff || '';

  // threads/checks truncation also BLOCKS the merge (threads-complete /
  // checks-complete); files/diff truncation is display-only.
  const cuts = [];
  if (d.files_truncated) cuts.push('file list');
  if (d.diff_truncated) cuts.push('diff');
  if (d.threads_truncated) cuts.push('threads (blocks merge)');
  if (d.checks_truncated) cuts.push('checks (blocks merge)');
  const trunc = document.getElementById('mg-truncation');
  trunc.hidden = cuts.length === 0;
  trunc.textContent = cuts.length
    ? `⚠ truncated: ${cuts.join(', ')} — open the PR on GitHub for the full view`
    : '';

  document.getElementById('mg-merge').disabled = failed.length > 0;
  document.getElementById('mg-retry-sync').hidden = true;
  document.getElementById('mg-result').textContent = '';
}

document.getElementById('mg-merge').addEventListener('click', async () => {
  const button = document.getElementById('mg-merge');
  const result = document.getElementById('mg-result');
  button.disabled = true;
  result.textContent = 'merging…';
  const token = await ensureActionToken();
  const res = await fetch('/api/actions/merge-and-sync', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Action-Token': token},
    body: JSON.stringify({dir: mgRepo, pr: mgPr, if_head: mgHead}),
  });
  const outcome = await res.json();
  if (res.status === 409) { result.textContent =
    'another action is in flight for this repo'; button.disabled = false; return; }
  if (!outcome.merged) {
    // ворота могли закрыться между чтением экрана и кликом — это норма
    result.textContent = `not merged: ${
      (outcome.gate_failed || []).join(', ') || outcome.error || 'refused'}`;
    button.disabled = false;
    await openMergeGate(mgRepo, mgPr);
    return;
  }
  if (outcome.local_sync === 'failed') {
    result.textContent =
      `PR merged; local sync needs attention: ${outcome.error || 'unknown'}`;
    document.getElementById('mg-retry-sync').hidden = false;
    return;
  }
  result.textContent = outcome.local_sync === 'not_applicable'
    ? 'PR merged (no local clone to sync)'
    : 'PR merged and local clone synced';
});

document.getElementById('mg-retry-sync').addEventListener('click', async () => {
  const result = document.getElementById('mg-result');
  result.textContent = 'syncing…';
  const token = await ensureActionToken();
  const res = await fetch('/api/actions/post-merge-sync', {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Action-Token': token},
    body: JSON.stringify({dir: mgRepo}),
  });
  const outcome = await res.json();
  result.textContent = outcome.ok
    ? 'local clone synced'
    : `local sync still failing: ${outcome.error || 'unknown'}`;
  document.getElementById('mg-retry-sync').hidden = outcome.ok;
});
```

- [ ] **Step 3: Wire the entry point**

In the existing project-card / PR rendering, make each open PR clickable so it calls
`openMergeGate(repoDir, prNumber)`. Follow whatever click pattern the surrounding
card code already uses — do not invent a second navigation style.

- [ ] **Step 4: Add a smoke test that the assets are wired**

```python
# append to tests/test_api.py
def test_merge_gate_markup_is_served(client) -> None:
    body = client.get("/").text
    assert 'id="merge-gate"' in body
    assert "openMergeGate" in body
    assert "/api/actions/merge-and-sync" in body
```

- [ ] **Step 5: Verify by hand against the real screen**

Run: `uv run dispatcher serve` and open http://127.0.0.1:8787.
Check, and **write down what you actually saw** for each:
1. a PR with a red check → Merge disabled, `checks-green` listed as blocking;
2. a PR with an unresolved thread → Merge disabled, the thread's author and excerpt shown;
3. a green PR → Merge enabled.

Do not merge anything real to test the click; the composite is covered by Task 2's
tests against the fake checker.

- [ ] **Step 6: Commit**

```bash
uv run ruff format . && uv run ruff check . --fix
git add dispatcher/server/static/index.html tests/test_api.py
git commit -m "feat(web): merge-gate view with truncation and partial-outcome states"
```

---

### Task 5: Correct the charter sentence

**Files:**
- Modify: `README.md:6-9`

**Why this is a task and not a footnote:** `README.md` currently ends its
description with "dispatcher itself never pushes or merges". After Task 3 that
sentence is false, and a charter that misdescribes the system is worse than no
charter. What changes is only this **self-description**. The cross-ecosystem rule —
ADR-ECO-004 D1, "dispatcher only shows and launches PR-only actions, never a second
SSOT" — is **not** touched: merging still happens through `github-checker`, only on
an explicit human click, and "a human merges" from the polyrepo git-workflow rule
still holds, because the human is the one clicking. No vault PR is needed for S1.
The D1 re-charter belongs to S2 (task authoring), where dispatcher would begin
*originating* work.

- [ ] **Step 1: Replace the sentence**

In `README.md`, change:

```
narrow, human-click-gated, PR-only whitelist (sync `pull`/`create-pr` +
spec-runner config editor, all delegated to `github-checker`; dispatcher
itself never pushes or merges).
```

to:

```
narrow, human-click-gated, PR-only whitelist (sync `pull`/`create-pr`, the
merge gate, and the spec-runner config editor, all delegated to
`github-checker`; dispatcher itself never talks to the GitHub API, and never
merges without an explicit human click).
```

- [ ] **Step 2: Document the merge gate in `README.md`**

Add a short section after the actions description:

````markdown
### Merge gate

Open a PR from the dashboard to see its state — checks, review threads, changed
files, diff — and merge it in one click. The click runs `github-checker merge
--if-head <sha>` followed by `post-merge-sync`, holding the repo's action lock
across both steps.

The screen is a **view**, not the authority: `merge` re-reads the PR and re-checks
every predicate server-side, so a state change between the screen and the click
(a new push, a red check, a fresh review thread, a draft conversion) refuses the
merge instead of racing it.

Because a merged PR cannot be un-merged, outcomes are two facts, not one:
`merged` and `local_sync`. `merged=true, local_sync=failed` means the PR landed
and only the local clone needs attention — it is shown as a warning with a retry,
never as a failed merge.
````

- [ ] **Step 3: Update `TODO.md`**

Add the completed entry in the format that file already uses. The PR number does not
exist while you are implementing — do **not** invent one. Write the entry without it,
or with an obviously-marked placeholder, and fill it in at merge time.

**Do not touch `@id:vendor-contracts-actions-v1`.** That item stays open and stays
`@blocked_by:github-checker#contracts-actions-v1`. This PR ships a provisional
passthrough adapter guarded by consumer tests; it does not discharge the vendoring
obligation, and marking it done would erase a real, tracked cross-repo dependency.
The same applies to the producer-side item in `github-checker/TODO.md` — that is
another repo's file and out of bounds regardless (see the scope rule in `CLAUDE.md`).

Do not remove or rewrite any existing line in `TODO.md`: the ecosystem's delta
counters read a disappeared line as "closed".

- [ ] **Step 4: Final verification**

```bash
uv run ruff format . && uv run ruff check .
uv run pytest -q
```

Expected: clean lint and a fully green suite. **Read the real output before
claiming success.** If anything fails, fix it — do not report completion over a
red or unexamined run.

- [ ] **Step 5: Commit**

```bash
git add README.md TODO.md
git commit -m "docs: dispatcher merges via github-checker on an explicit human click"
```

---

## Handoff

S1 is complete when both repos' plans have landed. Open this repo's PR under its own
rules (PR-only, Copilot review actioned, **human merges**) — and note the pleasant
recursion that this is the last PR you will merge the old way.

Out of scope, recorded so it is not rediscovered as a gap: TUI and VSCode parity for
the merge gate (a fast follow, mirroring FR-06); task authoring and `create-issue`
(S2, which does need the ADR-ECO-004 D1 amendment); an agent that resolves review
threads (S3); auto-merge in any form.
