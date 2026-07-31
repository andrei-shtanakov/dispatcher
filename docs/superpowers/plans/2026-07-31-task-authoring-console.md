# Task-authoring console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a discussed idea into a routed cross-repo request: a screen where an operator writes a task request and one click files it as an `inbox` issue in the target repo, with duplicate slugs surfaced rather than duplicated.

**Architecture:** dispatcher gains no new authority over GitHub — it shells out to `github-checker`'s new `issue-lookup` and `issue-create` verbs, exactly as every other mutation does. `issue-lookup` is a lock-free read the screen uses to show whether a slug is taken. `request-task` is a single guarded action: it holds the per-repo lock and invokes `issue-create`, which itself re-checks and reads back. The `created` field is three-valued — `true` / `false` / **`null` = unknown** — and the screen must never render `null` as "not created".

**Tech Stack:** Python 3.11+, FastAPI, pydantic v2, vanilla-JS single-file SPA (`dispatcher/server/static/index.html`), pytest (anyio), uv.

**Depends on:** `github-checker/docs/superpowers/plans/2026-07-31-inbox-issue-verbs.md`. That plan lands **first**. Every test here fakes the `github-checker` binary (the established pattern in `tests/test_actions.py`), so this is implementable before the real verbs ship — but must not be *released* before them.

**Canon:** [ADR-ECO-004a](https://github.com/andrei-shtanakov/prograph-vault/blob/master/authored/decisions/2026-07-30-adr-eco-004a-dispatcher-task-authoring.md), ratified 2026-07-30. It permits originating task-authoring proposals through explicit human actions and PR/issue channels, and forbids everything in "Non-goals" below.

**Design source:** `_cowork_output/2026-07-31-s2-task-authoring-design.md` (dev-only; shipped code never reads it). Everything needed is restated here.

## Global Constraints

- **uv only**: `uv run pytest`, `uv run ruff`, `uv run pyrefly`. Never `pip`.
- Line length **88** — count **characters**; this repo has Cyrillic comments and a byte count misreports them.
- **`uv run pyrefly check` must report 0 errors.** CI runs it in two jobs and `master` is clean, so any new error is ours.
- **Human-click-only**: every mutating endpoint requires the `X-Action-Token` header. No background trigger, no scheduled authoring.
- **dispatcher never calls the GitHub API itself** — everything goes through the `github-checker` binary.
- **Every action attempt leaves an audit line**, rejected and busy ones included.
- Type hints on all functions; docstrings on public functions.
- Comments sparse, explaining *why* not *what*; Russian where a non-obvious guard needs justifying. No narration.

## Non-goals (from the ratified ADR — these are load-bearing)

This slice creates **only** an inbox issue. It does **not** edit `TODO.md` in any repo; does **not** accept the request on the owner's behalf; does **not** create an implementation PR; does **not** run an executor; and does **not** store task state of its own. A later slice wanting any of these needs its own amendment.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `dispatcher/core/actions.py` | **modify** — `ActionOutcome` gains `matches` / `malformed` / `created` / `issue`; `issue_lookup()` (no lock) and `request_task()` (locked) added; a pre-existing non-object-JSON hole in `_invoke` closed. |
| `dispatcher/server/app.py` | **modify** — `GET /api/issue-lookup`, `POST /api/actions/request-task`. |
| `dispatcher/server/static/index.html` | **modify** — the authoring screen. |
| `README.md`, `TODO.md` | **modify** — document the slice; record it. |
| `tests/test_actions.py` | **modify** — lock span, the six outcomes, argv passthrough. |
| `tests/test_api.py` | **modify** — endpoint contracts, token gating, 409/422. |

**Deliberately not created:** a separate authoring module. The two new methods sit beside the existing whitelist actions because they share `_hold`, `_target`, `_invoke` and the audit discipline; splitting them out would duplicate that machinery for no gain.

**A simplification worth stating up front.** The design describes the lock spanning *lookup → create → read-back*. `github-checker`'s `issue-create` performs all three inside one verb, so dispatcher satisfies that by holding the lock across **one** `_invoke`. `request-task` is therefore not a multi-step composite like `merge-and-sync`; it is a single guarded call. The property holds; the mechanism is simpler than the design's prose implies, and pretending otherwise would add a step that does nothing.

---

### Task 1: Outcome fields and the lock-free lookup

**Files:**
- Modify: `dispatcher/core/actions.py`
- Test: `tests/test_actions.py` (append)

**Interfaces:**
- Consumes: existing `ActionRunner`, `ActionOutcome`, `_hold`, `_invoke`, `_target`, `_audit_outcome`.
- Produces: `ActionOutcome` with `matches: list[dict[str, Any]] | None`, `malformed: list[dict[str, Any]] | None`, `created: bool | None`, `issue: dict[str, Any] | None`; and `ActionRunner.issue_lookup(repo_dir: str, slug: str) -> ActionOutcome`.

**Why `dict` and not a typed `IssueRef` model here:** the same provisional-adapter reasoning that governs `pr_detail` on this branch's predecessor. `contracts/actions/v1` is still unpublished by the producer (`@id:vendor-contracts-actions-v1` is open and blocked), so there is nothing canonical to vendor. The passthrough is constrained instead — the screen validates presence and type of every field it reads before rendering, and a consumer fixture pins the real payload's shape.

**`issue_lookup` takes no lock.** It is a read: it must neither queue behind an in-flight action nor block one. It still validates the repo dir, and it still audits.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_actions.py
def test_outcome_carries_the_issue_fields(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-lookup",
        "dir": "alpha",
        "ok": True,
        "matches": [{"number": 7, "state": "open"}],
        "malformed": [],
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.issue_lookup("alpha", "wanted")
    assert outcome.ok is True
    assert outcome.matches[0]["number"] == 7
    assert outcome.malformed == []


def test_issue_lookup_takes_no_lock(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    payload = {"action": "issue-lookup", "dir": "alpha", "ok": True,
               "matches": [], "malformed": []}
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    runner.issue_lookup("alpha", "wanted")
    assert runner._busy == set()


@pytest.mark.parametrize("payload", ["[1, 2]", "5", '"a string"', "null"])
def test_non_object_json_is_a_failed_outcome_not_an_exception(
    tmp_path: Path, payload: str
) -> None:
    """Pre-existing hole: `data.get` sat outside the try, so a non-object
    top-level payload raised AttributeError out of the runner."""
    make_repo(tmp_path, "alpha")
    script = tmp_path / "bad_json.py"
    script.write_text(f"import sys; sys.stdout.write({payload!r})")
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    outcome = runner.run("pull", "alpha")
    assert outcome.ok is False
    assert "not an object" in (outcome.error or "")


def test_issue_lookup_still_validates_the_repo_dir(tmp_path: Path) -> None:
    runner = ActionRunner(DispatcherConfig(roots=(tmp_path,)))
    with pytest.raises(ActionRejectedError, match="unsafe"):
        runner.issue_lookup("../etc", "wanted")


def test_issue_lookup_passes_the_slug_through(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    script = tmp_path / "echo_argv.py"
    script.write_text(
        "import sys, json;"
        "json.dump({'action':'issue-lookup','dir':'alpha','ok':True,"
        "'detail':' '.join(sys.argv[1:])}, sys.stdout)"
    )
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    outcome = runner.issue_lookup("alpha", "wanted")
    assert "--slug wanted" in (outcome.detail or "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_actions.py -k "issue_lookup or issue_fields" -v`
Expected: FAIL — `AttributeError: 'ActionRunner' object has no attribute 'issue_lookup'`

- [ ] **Step 3: Write the implementation**

Add to `ActionOutcome` in `dispatcher/core/actions.py` (after `pr_detail`):

```python
    matches: list[dict[str, Any]] | None = None
    malformed: list[dict[str, Any]] | None = None
    created: bool | None = None
    issue: dict[str, Any] | None = None
```

and read them in `_invoke`'s success branch:

```python
            matches=data.get("matches"),
            malformed=data.get("malformed"),
            created=data.get("created"),
            issue=data.get("issue"),
```

**While you are in `_invoke`, close a pre-existing hole in it.** `local = data.get("local") or {}` sits *outside* the `try`, so any top-level JSON that is not an object raises `AttributeError` straight out of the runner. Verified on `master` against four payloads — `[1, 2]`, `5`, `"a string"`, `null` — all four raise. That contradicts this file's own contract, where a `github-checker` that returns nonsense becomes a failed `ActionOutcome`. It is the same defect class the producer side just fixed, so fix it here rather than leaving matching bugs on both sides of one contract:

```python
        if not isinstance(data, dict):
            return ActionOutcome(
                action=action,
                dir=target.name,
                ok=False,
                error="github-checker returned JSON that is not an object",
            )
```

Place it immediately after the `json.loads` block, before `local = data.get(...)`. Fail-closed by construction: an envelope we cannot read is never a success.

Add the method to `ActionRunner`:

```python
    def issue_lookup(self, repo_dir: str, slug: str) -> ActionOutcome:
        """Ask whether a slug already has an inbox issue. A read takes no lock."""
        target = self._target(repo_dir)
        outcome = self._invoke("issue-lookup", target, "--slug", slug)
        # not _audit_outcome: that helper's format has no room for the slug,
        # and the slug is the whole point of this line
        _audit.info(
            "action=issue-lookup repo=%s slug=%s ok=%s matches=%s",
            repo_dir, slug, outcome.ok, len(outcome.matches or []),
        )
        return outcome
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_actions.py -v`
Expected: PASS — new tests green **and** every pre-existing test in the file still green

- [ ] **Step 5: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add dispatcher/core/actions.py tests/test_actions.py
git commit -m "feat(actions): issue outcome fields and the lock-free issue-lookup"
```

---

### Task 2: `request_task` — one guarded call, six outcomes

**Files:**
- Modify: `dispatcher/core/actions.py`
- Test: `tests/test_actions.py` (append)

**Interfaces:**
- Consumes: `_hold`, `_invoke`, `_audit_outcome` (existing), `issue_lookup` (Task 1).
- Produces: `ActionRunner.request_task(repo_dir: str, *, slug: str, sender: str, title: str, prose: str) -> ActionOutcome`.

**The prose reaches the verb as a file.** `issue-create` takes `--body-file`, not `--body`: prose is multi-line and argv is where newlines and quoting go to die. Write it to a `NamedTemporaryFile`, pass the path, and delete it in a `finally` — the file holds user text and must not outlive the call.

**The six outcomes this must produce:**

| `created` | `issue` | `ok` | Meaning |
|---|---|---|---|
| `false` | found | `true` | slug already taken (including a lost race) |
| `true` | read back | `true` | created and confirmed |
| `false` | `null` | `false` | duplicate conflict — several matches |
| `false` | `null` | `false` | lookup unavailable **before** any mutation |
| `true` | `null` | **`true`** | created, but the read-back failed |
| `null` | `null` | `false` | the create call itself broke — **unknown** |

Rows five and six are different facts. In row five the issue exists and only our re-read failed; in row six we never learned whether it landed. Row six's `created=null` must never be rendered as "not created", and the only safe follow-up for either is to **look again**, never to create again.

Most of this classification is `github-checker`'s: dispatcher passes the fields through and adds the lock and the audit. What dispatcher must not do is flatten them.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_actions.py
def test_request_task_creates_and_confirms(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-create", "dir": "alpha", "ok": True,
        "created": True, "issue": {"number": 9, "url": "https://x/9"},
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.request_task(
        "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert outcome.ok is True
    assert outcome.created is True
    assert outcome.issue["number"] == 9


def test_request_task_reports_a_taken_slug_as_success(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-create", "dir": "alpha", "ok": True,
        "created": False, "issue": {"number": 5, "url": "https://x/5"},
        "detail": "an inbox issue for this slug already exists",
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.request_task(
        "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert outcome.ok is True
    assert outcome.created is False
    assert outcome.issue["number"] == 5


def test_request_task_preserves_created_none_on_a_broken_create(
    tmp_path: Path,
) -> None:
    """The call broke: whether it landed is unknown, not known-negative."""
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-create", "dir": "alpha", "ok": False,
        "created": None, "error": "gh issue create failed",
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.request_task(
        "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert outcome.ok is False
    assert outcome.created is None


def test_request_task_keeps_created_true_when_read_back_failed(
    tmp_path: Path,
) -> None:
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-create", "dir": "alpha", "ok": True,
        "created": True, "issue": None,
        "detail": "created, but reading it back failed",
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.request_task(
        "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert outcome.ok is True
    assert outcome.created is True
    assert outcome.issue is None


def test_request_task_passes_a_duplicate_conflict_through(tmp_path: Path) -> None:
    """Several issues claim the slug: a human decides, dispatcher does not."""
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-create", "dir": "alpha", "ok": False, "created": False,
        "error": "several inbox issues claim this slug",
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.request_task(
        "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert outcome.ok is False
    assert outcome.created is False  # definitively not created: no mutation ran
    assert outcome.issue is None


def test_request_task_reports_an_unavailable_lookup_as_not_created(
    tmp_path: Path,
) -> None:
    """The pre-create check failed, so nothing was attempted — created is False,
    not None: `None` would claim we might have mutated something."""
    make_repo(tmp_path, "alpha")
    payload = {
        "action": "issue-create", "dir": "alpha", "ok": False, "created": False,
        "error": "slug lookup failed before create",
    }
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    outcome = runner.request_task(
        "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
    )
    assert outcome.ok is False
    assert outcome.created is False
    assert outcome.issue is None


def test_request_task_audits_whether_it_created(tmp_path: Path, caplog) -> None:
    """D1a-4: the audit must distinguish created from already-existed —
    an idempotency rule whose log cannot show idempotency is not auditable."""
    make_repo(tmp_path, "alpha")
    payload = {"action": "issue-create", "dir": "alpha", "ok": True,
               "created": False, "issue": {"number": 5}}
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        runner.request_task(
            "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
        )
    assert "created=False" in caplog.text


def test_pull_audit_line_is_unchanged_by_the_created_field(
    tmp_path: Path, caplog
) -> None:
    """The new field must not leak into unrelated actions' audit lines."""
    make_repo(tmp_path, "alpha")
    payload = {"action": "pull", "dir": "alpha", "ok": True}
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=fake_checker(tmp_path, payload)
    )
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        runner.run("pull", "alpha")
    assert "created=" not in caplog.text


def test_request_task_passes_prose_through_a_file_not_argv(
    tmp_path: Path,
) -> None:
    """Multi-line prose must survive; argv would mangle it."""
    make_repo(tmp_path, "alpha")
    script = tmp_path / "echo_body.py"
    script.write_text(
        "import sys, json, pathlib\n"
        "i = sys.argv.index('--body-file')\n"
        "body = pathlib.Path(sys.argv[i + 1]).read_text()\n"
        "json.dump({'action':'issue-create','dir':'alpha','ok':True,"
        "'created':True,'detail':body}, sys.stdout)\n"
    )
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    outcome = runner.request_task(
        "alpha", slug="wanted", sender="dispatcher", title="t",
        prose="line one\nline two\n",
    )
    assert outcome.detail == "line one\nline two\n"


def test_request_task_holds_the_repo_lock(tmp_path: Path) -> None:
    make_repo(tmp_path, "alpha")
    script = tmp_path / "blocking.py"
    script.write_text(
        "import sys, json, pathlib, time\n"
        f"flag = pathlib.Path({str(tmp_path / 'in_create')!r})\n"
        f"gate = pathlib.Path({str(tmp_path / 'go')!r})\n"
        "flag.touch()\n"
        "while not gate.exists():\n"
        "    time.sleep(0.01)\n"
        "json.dump({'action':'issue-create','dir':'alpha','ok':True,"
        "'created':True}, sys.stdout)\n"
    )
    runner = ActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=("python3", str(script))
    )
    result: list[ActionOutcome] = []
    worker = threading.Thread(
        target=lambda: result.append(
            runner.request_task(
                "alpha", slug="wanted", sender="dispatcher", title="t", prose="p"
            )
        )
    )
    worker.start()
    deadline = time.monotonic() + 10
    while not (tmp_path / "in_create").exists():
        if time.monotonic() > deadline:
            pytest.fail("worker never entered issue-create")
        time.sleep(0.01)
    with pytest.raises(ActionBusyError):
        runner.run("pull", "alpha")
    (tmp_path / "go").touch()
    worker.join(timeout=10)
    assert not worker.is_alive(), "worker wedged"
    assert result[0].ok is True
    runner.run("pull", "alpha")  # lock released
```

Add `import time` and `import logging` to the test file's imports if not already there.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_actions.py -k request_task -v`
Expected: FAIL — `AttributeError: 'ActionRunner' object has no attribute 'request_task'`

- [ ] **Step 3: Make the audit line carry `created`**

`_audit_outcome` today appends `merged`/`local_sync` only when they are set, keeping a plain `pull` line as terse as it was before the merge gate existed. Extend the same conditional with `created`, so an authoring attempt is auditable as *created* versus *already existed* — D1a-4 makes idempotency a rule, and a rule whose log cannot show whether it held is not auditable:

```python
        merge_fields = ""
        if outcome.merged is not None or outcome.local_sync is not None:
            merge_fields = f" merged={outcome.merged} local_sync={outcome.local_sync}"
        if outcome.created is not None:
            merge_fields += f" created={outcome.created}"
```

`is not None`, not truthiness: `created=False` is the *idempotent* case, which is precisely the one worth recording. A truthy test would drop every deduplicated attempt from the audit — the opposite of what D1a-4 asks for.

- [ ] **Step 4: Write the implementation**

Add to `ActionRunner`, with `import tempfile` and `from pathlib import Path` at the top of the module (Path is already imported):

```python
    def request_task(
        self, repo_dir: str, *, slug: str, sender: str, title: str, prose: str
    ) -> ActionOutcome:
        """File an inbox issue in *repo_dir*, holding the repo's lock.

        github-checker's `issue-create` re-checks for an existing slug and
        reads the result back inside one verb, so a single guarded call
        covers the whole lookup → create → read-back sequence.

        `created` is passed through untouched: `true` / `false` / `None`
        mean created / not created / unknown, and flattening `None` into
        `false` would tell the operator a request does not exist when it
        may well do.
        """
        with self._hold("request-task", repo_dir) as target:
            # prose is multi-line; argv is where newlines and quoting die
            with tempfile.NamedTemporaryFile(
                "w", suffix=".md", delete=False, encoding="utf-8"
            ) as handle:
                handle.write(prose)
                body_file = handle.name
            try:
                outcome = self._invoke(
                    "issue-create", target,
                    "--slug", slug,
                    "--from", sender,
                    "--title", title,
                    "--body-file", body_file,
                )
            finally:
                Path(body_file).unlink(missing_ok=True)
        outcome.action = "request-task"
        self._audit_outcome("request-task", repo_dir, outcome)
        return outcome
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_actions.py -v`
Expected: PASS

- [ ] **Step 6: Prove the lock test has teeth**

A test for this property that has never been seen to fail is worth little. Temporarily replace the `with self._hold(...)` block with an unguarded call (no `_hold` at all), run `uv run pytest tests/test_actions.py -k holds_the_repo_lock`, confirm it **fails**, then restore and confirm it passes. Paste both runs in your report.

- [ ] **Step 7: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add dispatcher/core/actions.py tests/test_actions.py
git commit -m "feat(actions): request-task files an inbox issue under the repo lock"
```

---

### Task 3: API endpoints

**Files:**
- Modify: `dispatcher/server/app.py`
- Test: `tests/test_api.py` (append)

**Interfaces:**
- Consumes: `ActionRunner.issue_lookup`, `.request_task`.
- Produces: `GET /api/issue-lookup?dir=<repo>&slug=<slug>` → `ActionOutcome` (read; no token); `POST /api/actions/request-task` body `{dir, slug, title, prose}` → `ActionOutcome` (token required).

**`from` is not a request field.** The server supplies the literal `"dispatcher"`. It is written into the issue's structural block, so accepting it from the client would let a caller forge the sender — and `github-checker` rejects CR/LF in it precisely because that value can inject body lines. Keeping it server-side means the form has no way to reach it at all.

**Status codes**, matching the pattern `_run_action` already establishes: `ActionRejectedError` → 422, `ActionBusyError` → 409, bad/missing token → 403.

**Note on the test convention:** `tests/test_api.py` is entirely `pytest.mark.anyio`-driven — `async def` tests using the file's `_client(tmp_path)` helper and `await _token(client)`. Follow that; do not introduce a second, synchronous client style.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_api.py
@pytest.mark.anyio
async def test_request_task_requires_the_action_token(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        response = await client.post(
            "/api/actions/request-task",
            json={"dir": "alpha", "slug": "wanted", "title": "t", "prose": "p"},
        )
    assert response.status_code == 403


@pytest.mark.anyio
async def test_request_task_returns_the_outcome(tmp_path: Path, monkeypatch) -> None:
    from dispatcher.core.actions import ActionOutcome, ActionRunner

    monkeypatch.setattr(
        ActionRunner, "request_task",
        lambda self, repo_dir, **kw: ActionOutcome(
            action="request-task", dir=repo_dir, ok=True, created=True,
            issue={"number": 9, "url": "https://x/9"},
        ),
    )
    async with _client(tmp_path) as client:
        token = await _token(client)
        response = await client.post(
            "/api/actions/request-task",
            json={"dir": "alpha", "slug": "wanted", "title": "t", "prose": "p"},
            headers={"X-Action-Token": token},
        )
    assert response.status_code == 200
    assert response.json()["created"] is True


@pytest.mark.anyio
async def test_request_task_never_takes_from_from_the_client(
    tmp_path: Path, monkeypatch
) -> None:
    """`from` lands in the issue's structural block; the client cannot set it."""
    seen: dict = {}

    from dispatcher.core.actions import ActionOutcome, ActionRunner

    def capture(self, repo_dir, **kw):
        seen.update(kw)
        return ActionOutcome(action="request-task", dir=repo_dir, ok=True,
                             created=True)

    monkeypatch.setattr(ActionRunner, "request_task", capture)
    async with _client(tmp_path) as client:
        token = await _token(client)
        await client.post(
            "/api/actions/request-task",
            json={"dir": "alpha", "slug": "wanted", "title": "t", "prose": "p",
                  "sender": "spoofed", "from": "spoofed"},
            headers={"X-Action-Token": token},
        )
    assert seen["sender"] == "dispatcher"


@pytest.mark.anyio
async def test_request_task_maps_busy_to_409(tmp_path: Path, monkeypatch) -> None:
    from dispatcher.core.actions import ActionBusyError, ActionRunner

    def busy(self, repo_dir, **kw):
        raise ActionBusyError("alpha: action already in flight")

    monkeypatch.setattr(ActionRunner, "request_task", busy)
    async with _client(tmp_path) as client:
        token = await _token(client)
        response = await client.post(
            "/api/actions/request-task",
            json={"dir": "alpha", "slug": "wanted", "title": "t", "prose": "p"},
            headers={"X-Action-Token": token},
        )
    assert response.status_code == 409


@pytest.mark.anyio
async def test_issue_lookup_is_readable_without_a_token(
    tmp_path: Path, monkeypatch
) -> None:
    from dispatcher.core.actions import ActionOutcome, ActionRunner

    monkeypatch.setattr(
        ActionRunner, "issue_lookup",
        lambda self, repo_dir, slug: ActionOutcome(
            action="issue-lookup", dir=repo_dir, ok=True, matches=[], malformed=[]
        ),
    )
    async with _client(tmp_path) as client:
        response = await client.get(
            "/api/issue-lookup", params={"dir": "alpha", "slug": "wanted"}
        )
    assert response.status_code == 200
    assert response.json()["matches"] == []


@pytest.mark.anyio
async def test_issue_lookup_maps_rejection_to_422(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        response = await client.get(
            "/api/issue-lookup", params={"dir": "../etc", "slug": "wanted"}
        )
    assert response.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api.py -k "request_task or issue_lookup" -v`
Expected: FAIL — 404, the routes do not exist

- [ ] **Step 3: Write the implementation**

Add a request model next to `ActionRequest` in `dispatcher/server/app.py`:

```python
class TaskRequest(BaseModel):
    """POST /api/actions/request-task body.

    `from` is deliberately absent: the server supplies it. It is written
    into the issue's structural block, so a client-settable value would be
    a way to forge the sender.
    """

    dir: str
    slug: str
    title: str
    prose: str
```

and the endpoints after `action_post_merge_sync`:

```python
    @app.get("/api/issue-lookup", response_model=ActionOutcome)
    def issue_lookup(dir: str, slug: str) -> ActionOutcome:
        """Read-through to github-checker; no mutation, so no token."""
        try:
            return actions.issue_lookup(dir.strip(), slug.strip())
        except ActionRejectedError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    @app.post("/api/actions/request-task", response_model=ActionOutcome)
    def action_request_task(
        request: TaskRequest,
        x_action_token: str | None = Header(default=None),
    ) -> ActionOutcome:
        """Явный клик человека: завести inbox-issue в целевом репо."""
        if x_action_token != action_token:
            raise HTTPException(status_code=403, detail="bad or missing action token")
        try:
            outcome = actions.request_task(
                request.dir.strip(),
                slug=request.slug.strip(),
                sender="dispatcher",  # never from the client — see TaskRequest
                title=request.title.strip(),
                prose=request.prose,
            )
        except ActionRejectedError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        except ActionBusyError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        return outcome
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_api.py -v`
Expected: PASS

- [ ] **Step 5: Pin a consumer fixture of the real verb's output**

Everything above tests against hand-written fakes, so nothing yet proves this code agrees with what `github-checker` actually emits. Capture the real output once and commit it, pinned to the producer commit it came from.

`github-checker` is **not** on PATH; the working invocation from the worktree is `uv run --project ../github-checker github-checker …`, and `gh` is authenticated. Use a slug that genuinely has an inbox issue — `prograph-vault` has one from S2's own amendment request:

```bash
uv run --project ../github-checker github-checker issue-lookup \
  ../prograph-vault --slug amend-adr-eco-004-d1-task-authoring \
  > tests/fixtures/issue_lookup_github_checker.json
```

Record the producer commit beside it (`git -C ../github-checker rev-parse --short HEAD`) and name the fixture file after it, as the merge-gate fixture is named.

**One field needs care.** `github-checker` normalises `state` to lowercase
(`open`/`closed`) because `gh issue list` actually emits `OPEN`/`CLOSED` and the
CLI's own `--state` flag speaks lowercase. Assert the lowercase form, and if the
captured fixture shows uppercase, that is a producer regression — report it, do
not adapt the assertion to it.

Then assert the fixture satisfies every field the screen requires:

```python
# append to tests/test_api.py
ISSUE_REF_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "number": int,
    "title": str,
    "state": str,
    "url": str,
    "author": str,
    "labels": list,
}


def test_real_issue_lookup_payload_has_every_field_the_console_reads() -> None:
    """Consumer check against github-checker's ACTUAL output, not a fake.

    Provisional-adapter guard: the issue payload is an opaque passthrough
    until contracts/actions/v1 is published and vendored
    (TODO @id:vendor-contracts-actions-v1).
    """
    envelope = json.loads(ISSUE_FIXTURE.read_text())
    assert envelope["ok"] is True
    assert envelope["matches"], "fixture must pin a slug that actually exists"
    for ref in envelope["matches"]:
        missing = [
            key
            for key, expected in ISSUE_REF_REQUIRED.items()
            if not isinstance(ref.get(key), expected)
        ]
        assert missing == [], f"github-checker payload no longer provides: {missing}"
```

If you cannot produce the fixture (no authenticated `gh`, no readable issue), do **not** hand-write one and do **not** skip the step silently — a fabricated fixture asserts that the producer emits whatever we guessed. Stop and report it.

- [ ] **Step 6: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add dispatcher/server/app.py tests/test_api.py tests/fixtures/
git commit -m "feat(api): issue-lookup read and request-task action endpoints"
```

---

### Task 4: The authoring screen

**Files:**
- Modify: `dispatcher/server/static/index.html`
- Test: `tests/test_api.py` (smoke assertion)

**Interfaces:**
- Consumes: `GET /api/issue-lookup`, `POST /api/actions/request-task`, and `ensureActionToken()` (already in the file).
- Produces: a `#task-authoring` section, opened from the project panel.

**The four rules this screen must obey:**

1. **A taken slug is a normal state, not an error.** The button changes from **Create request** to **Open existing issue**, and the number, state and URL are shown beside it. Nothing about it should read as a failure.
2. **`created` is three-valued and must not be flattened.** In JavaScript `!null` is `true`, so a falsy check placed before an explicit null check silently turns "unknown" into "not created". Check `=== null` (and `undefined`) **first**. This exact bug shipped in the merge gate's first draft and cost a fix round.
3. **After a failed read-back or a broken create, the only offered action is to look again** — never to create again. Creating again after an unknown outcome is how duplicates are born.
4. **Validate the payload before rendering.** The issue payload is an opaque passthrough; check presence and type of every field the screen reads, and render "cannot read the result" with the create button disabled if anything is missing or wrongly typed.

**Structural fields are not form fields.** The form collects slug, title and prose only. `from:` is the server's, and the form must offer no way to set it.

**The markup below needs CSS, and the plan's first version shipped without it.**
A bare `<label>` is `display: inline`, so every field flowed onto one line: the
on-screen order came out slug → prose → title → button, the submit button shared a
row with an input, and the status `<span>` stretched to 900px and pushed `title`
below the textarea. Every logic test passed throughout — only a browser showed it.
Make the labels and inputs block-level, cap the field width, and put the buttons in
their own `flex` row that wraps.

- [ ] **Step 1: Add the markup**

Insert after the `#merge-gate` section:

```html
<section id="task-authoring" hidden>
  <h2>Task request <span id="ta-repo" class="sub"></span></h2>
  <label>slug <input id="ta-slug" type="text" spellcheck="false"></label>
  <span id="ta-slug-state" class="fresh"></span>
  <label>title <input id="ta-title" type="text"></label>
  <textarea id="ta-prose" rows="8" spellcheck="false"
    placeholder="what is needed, and by what observable condition it is done"></textarea>
  <div id="ta-existing" class="fresh" hidden></div>
  <button id="ta-create" type="button" disabled>Create request</button>
  <button id="ta-open" type="button" hidden>Open existing issue</button>
  <button id="ta-recheck" type="button" hidden>Re-check</button>
  <span id="ta-result"></span>
</section>
```

- [ ] **Step 2: Add the lookup and validation logic**

```javascript
let taRepo = null;

// Mirrors ISSUE_REF_REQUIRED in tests/test_api.py — the two must move
// together. The payload is an opaque passthrough from another repo's
// binary, so presence AND type are checked before anything is rendered.
const TA_REF_REQUIRED = [
  ['number', v => Number.isInteger(v)],
  ['title',  v => typeof v === 'string'],
  ['state',  v => typeof v === 'string'],
  ['url',    v => typeof v === 'string' && v.startsWith('https://')],
  ['author', v => typeof v === 'string'],
  ['labels', v => Array.isArray(v)],
];

function taRefProblems(ref) {
  if (ref === null || typeof ref !== 'object') return ['issue is not an object'];
  return TA_REF_REQUIRED.filter(([k, ok]) => !ok(ref[k]))
    .map(([k]) => `${k} missing or wrong type`);
}

function taShow(id, on) { document.getElementById(id).hidden = !on; }

async function taCheckSlug() {
  const slug = document.getElementById('ta-slug').value.trim();
  const state = document.getElementById('ta-slug-state');
  taShow('ta-existing', false); taShow('ta-open', false);
  document.getElementById('ta-create').disabled = true;
  if (!slug) { state.textContent = ''; return; }
  let outcome;
  try {
    const res = await fetch(
      `/api/issue-lookup?dir=${encodeURIComponent(taRepo)}` +
      `&slug=${encodeURIComponent(slug)}`);
    outcome = await res.json();
    if (!res.ok) { state.textContent = `cannot check: ${res.status}`; return; }
  } catch (err) {
    state.textContent = `cannot check: ${err}`;
    return;
  }
  if (!outcome.ok) {
    // malformed candidates land here — a human has to look, and creating
    // on top of an unreadable inbox would compound it
    state.textContent = `cannot check: ${outcome.error || 'unreadable'}`;
    return;
  }
  const matches = outcome.matches || [];
  if (matches.length === 0) {
    state.textContent = 'free';
    document.getElementById('ta-create').disabled = false;
    return;
  }
  if (matches.length > 1) {
    state.textContent = `${matches.length} issues claim this slug — conflict`;
    document.getElementById('ta-existing').innerHTML = matches.map(
      m => `<a href="${esc(m.url)}" target="_blank">#${esc(m.number)}</a>`
    ).join(' · ');
    taShow('ta-existing', true);
    return;
  }
  const ref = matches[0];
  const problems = taRefProblems(ref);
  if (problems.length) { state.textContent = `cannot read: ${problems.join('; ')}`; return; }
  state.textContent = 'already requested';
  document.getElementById('ta-existing').innerHTML =
    `#${esc(ref.number)} · ${esc(ref.state)} · ` +
    `<a href="${esc(ref.url)}" target="_blank">${esc(ref.url)}</a>`;
  taShow('ta-existing', true);
  const open = document.getElementById('ta-open');
  open.hidden = false;
  open.onclick = () => window.open(ref.url, '_blank');
}

document.getElementById('ta-slug').addEventListener('change', () => {
  taCheckSlug().catch(err => {
    document.getElementById('ta-slug-state').textContent = `cannot check: ${err}`;
  });
});
```

- [ ] **Step 3: Add the create handler**

```javascript
document.getElementById('ta-create').addEventListener('click', async () => {
  const btn = document.getElementById('ta-create');
  const result = document.getElementById('ta-result');
  btn.disabled = true;
  result.textContent = 'filing…';
  taShow('ta-recheck', false);
  let outcome, res;
  try {
    const token = await ensureActionToken();
    res = await fetch('/api/actions/request-task', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Action-Token': token},
      body: JSON.stringify({
        dir: taRepo,
        slug: document.getElementById('ta-slug').value.trim(),
        title: document.getElementById('ta-title').value.trim(),
        prose: document.getElementById('ta-prose').value,
      }),
    });
    outcome = await res.json();
  } catch (err) {
    // the request itself broke: whether it landed is unknown
    result.textContent = `issue may have been created; state unknown (${err})`;
    taShow('ta-recheck', true);
    return;
  }
  if (!res.ok && res.status !== 200) {
    if (res.status === 409) { result.textContent = 'another action is in flight'; }
    else { result.textContent = `request rejected: ${outcome.detail || res.status}`; }
    btn.disabled = false;
    return;
  }
  // `!created` would fold null into false — check unknown FIRST
  if (outcome.created === null || outcome.created === undefined) {
    result.textContent =
      `issue may have been created; state unknown: ${outcome.error || 'no answer'}`;
    taShow('ta-recheck', true);
    return;
  }
  if (outcome.created === false) {
    result.textContent = 'a request for this slug already exists';
    await taCheckSlug();
    return;
  }
  if (!outcome.issue) {
    result.textContent = 'issue created, but reading it back failed';
    taShow('ta-recheck', true);
    return;
  }
  const problems = taRefProblems(outcome.issue);
  if (problems.length) {
    result.textContent = `created, but cannot read the result: ${problems.join('; ')}`;
    taShow('ta-recheck', true);
    return;
  }
  result.innerHTML = `created · <a href="${esc(outcome.issue.url)}" ` +
    `target="_blank">#${esc(outcome.issue.number)}</a>`;
});

// Re-check never re-creates: after an unknown outcome, creating again is
// how a duplicate is born.
document.getElementById('ta-recheck').addEventListener('click', () => {
  taCheckSlug().catch(err => {
    document.getElementById('ta-result').textContent = `re-check failed: ${err}`;
  });
});
```

- [ ] **Step 4: Wire the entry point**

Give the project detail panel a **Create task request** button that sets `taRepo` to the project's **directory basename** (the `data-dir` attribute the merge-gate entry point already uses — *not* the display name, which diverges from the directory and only works on a case-insensitive filesystem), reveals `#task-authoring`, and clears the form fields.

- [ ] **Step 5: Add a smoke assertion**

```python
# append to tests/test_api.py
@pytest.mark.anyio
async def test_task_authoring_markup_is_served(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        body = (await client.get("/")).text
    assert 'id="task-authoring"' in body
    assert "taCheckSlug" in body
    assert "/api/actions/request-task" in body
```

- [ ] **Step 6: Verify the behaviour you cannot reach from pytest**

The screen is client JS with no JS runner in this project. Do **not** invent a pytest assertion that pretends to cover it. Instead extract the shipped functions into Node with a stub DOM and a scripted fetch queue — the technique this repo used for the merge gate — and exercise: a free slug enables Create; a taken slug shows the issue and offers Open existing; several matches show a conflict and keep Create disabled; `created: null` renders "state unknown" and offers only Re-check; `created: true` with `issue: null` renders "created, but reading it back failed"; a malformed `issue` object renders "cannot read the result". Paste the output.

- [ ] **Step 7: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check
git add dispatcher/server/static/index.html tests/test_api.py
git commit -m "feat(web): task-authoring screen with slug dedup and unknown-state handling"
```

---

### Task 5: Document the slice and record it

**Files:**
- Modify: `README.md`, `TODO.md`

- [ ] **Step 1: Document in `README.md`**

Add a section describing the slice as it actually shipped: a **Create task request** action on the project panel opens a form for slug, title and prose; the slug is checked against the target repo's inbox as you type it; a taken slug offers the existing issue instead of creating a second one; several matches are a conflict a human resolves. State plainly that this files an `inbox` issue **only** — it does not edit `TODO.md`, does not accept the request on the owner's behalf, and does not run anything.

Document the three-valued `created` and its consequence: `null` means the outcome is genuinely unknown, it is never shown as "not created", and the offered follow-up is always to look again rather than to create again.

Note that `from:` is written by the server, not the form.

- [ ] **Step 2: Record in `TODO.md`**

Add a completed entry in the file's existing format. The PR number does not exist while you implement — **do not invent one**; use an obviously-marked placeholder and fill it at merge time. **Do not delete or reword any existing line** — the ecosystem's delta counters read a vanished line as "closed". In particular, leave `@id:vendor-contracts-actions-v1` open: this slice ships another provisional passthrough and does not discharge that obligation.

- [ ] **Step 3: Final verification**

```bash
uv run ruff format . && uv run ruff check .
uv run pytest -q
uv run pyrefly check
```

Read the real output before claiming success.

- [ ] **Step 4: Commit**

```bash
git add README.md TODO.md
git commit -m "docs: the task-authoring console and its limits"
```

---

## Handoff

S2 is complete when both repos' plans have landed. Open this repo's PR under its own rules (PR-only, Copilot review actioned, **human merges**).

Before the slice is accepted, run a **visual smoke of the authoring screen in a browser**, as S1 required: the logic is covered by tests, but usability is not. That step found three rendering defects in S1, two of which made the console unusable on real data — including a diff that pushed the action button 53 viewport-heights down the page.
