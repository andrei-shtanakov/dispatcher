# Vendoring contracts/actions/v1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consume `github-checker`'s published action contract through a vendored, pinned copy and a strict ingestion boundary, replacing the provisional `dict` passthrough that `@id:vendor-contracts-actions-v1` has been open against.

**Architecture:** The contract is copied inward and pinned to a producer commit — nothing reads the sibling repo at run time. Every payload crosses one boundary: raw JSON → **validate against the vendored schema** → typed model. Nothing downstream sees an unvalidated dict, and nothing upstream of the validator is trusted. `schema_version` and `result_kind` are checked before anything else; an envelope this build cannot interpret is a **consumer failure**, never an empty producer result.

**Tech Stack:** Python **>=3.12** (this repo's floor; 3.12.11 locally), pydantic v2,
`jsonschema>=4.26` (already a dependency), uv, pytest.

**Producer pin:**

```
repo    github-checker
commit  ef03fefcded37676b19ef1c6f88b956a09a26d3f   (PR #16, merge commit)
source  contracts/actions/v1/{actions.schema.json, README.md, fixtures/ (34)}
schema  sha256 b45e1536a5ec216260c98eedd8ebba403b587cf73a7f71969763b77f2a6f3e06
```

## Global Constraints

- **uv only**: `uv run pytest`, `uv run ruff`, `uv run pyrefly`. Never `pip`.
- Line length **88 characters** — count characters, not bytes (Cyrillic comments present).
- `uv run ruff format .` then `uv run ruff check . --fix`; `uv run pyrefly check` **0 errors** (CI runs it in two jobs).
- Full suite green — baseline measured on `dispatcher/master` @ `ac55a83`:
  **418 passed, 1 skipped**, JS harness **73/73**, runner self-test **11/11**.
  Re-measure before starting rather than trusting this line; a number carried
  over from another repo's run is how a regression reads as clean.
- **Never read `../github-checker` at run time.** Vendoring is the whole point; a sibling path in shipped code defeats it.
- Type hints everywhere; docstrings on public functions; comments sparse, explaining *why*.

## Plan review notes (defects found during execution)

- **`_fixture` was never defined.** Task 2's and Task 3's example tests call a
  `_fixture(name)` helper that this plan introduces nowhere. Implementers must
  write it (read the named JSON out of the vendored `fixtures/` directory)
  rather than hunt for it — a plan defect, not a missing import.
- **Task 3 may legitimately touch `contract.py`.** Its file list names only
  `actions.py` and `models.py`, but typing `PrDetail`/`IssueRef` reaches into
  the boundary where they are currently `dict[str, Any]`. Widening to
  `contract.py` is an accepted divergence: record the reason and cover it with
  a test rather than contorting the change to fit the original list.
- **Three of Task 2's four required guard mutations did not redden its own
  tests.** `jsonschema`'s `const`/`oneOf` already enforces the discriminators
  type-correctly, and two schema-validation tests were masked by the
  exit-code guard firing first. The manual prechecks earn their place through
  *diagnosis* — "unknown schema_version" instead of a wall of schema errors —
  not through being the only thing that rejects. Tests must isolate each guard
  from `jsonschema` and from the exit-code check, or they prove nothing.

## Out of scope — do not fold in

The hand-rolled DOM stub (`@id:web-tests-hand-rolled-dom`), the gate-floor gaps, and the rest of the UI debt. `DESIGN-405` level 3 is the **next** slice, deliberately after this one: it can then lean on the typed boundary and the vendored fixtures instead of inventing its own.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `contracts/github-checker-actions/v1/` | **new** — the vendored copy: schema, README, 34 fixtures, `PINNED.txt`, `manifest.json`. Named to sit beside the existing `github-checker-snapshot/v1`. |
| `dispatcher/core/contract.py` | **new** — the ingestion boundary: load the vendored schema once, check the envelope discriminators, validate, and hand back a typed result. The only place raw producer JSON is touched. |
| `dispatcher/core/models.py` | **new or extend** — `PrDetail`, `IssueRef`, `LocalStatus` as consumer-side pydantic models. |
| `dispatcher/core/actions.py` | **modify** — `ActionOutcome`'s four provisional `dict` fields become typed; `_invoke` routes through the boundary. |
| `tests/test_contract_ingest.py` | **new** — conformance against all 34 fixtures plus the negative cases. |
| `README.md`, `TODO.md` | **modify** — record the pin, drop the provisional-adapter wording, close the debt item. |

**Why a separate `contract.py` rather than more code in `actions.py`:** the boundary has one job — decide whether a payload may be believed. Mixing that with the runner's locking and audit concerns is how "validated" quietly becomes "parsed".

---

### Task 1: Vendor the contract and pin it

**Files:**
- Create: `contracts/github-checker-actions/v1/**` (copied), `PINNED.txt`, `manifest.json`
- Test: `tests/test_contract_ingest.py` (pin verification only)

**Interfaces:**
- Produces: the vendored tree; `VENDORED_ROOT`, `PRODUCER_COMMIT`, `manifest.json` with a per-file `sha256` and a `tree_sha256`.

**Follow the existing pattern.** This repo already vendors two contracts: `contracts/github-checker-snapshot/v1/` (README hash table) and `packages/plan-fields/src/plan_fields/contract/` (`PINNED.txt` + machine-readable `manifest.json`). Use the **second** shape — a table a human reads can drift unnoticed; a manifest a test iterates cannot.

- [ ] **Step 1: Copy the canonical tree**

**Copy from the git object database, not the working tree.** Checking that
`HEAD` equals the pin proves nothing about the files: an uncommitted edit in the
producer's tree would be copied in, and the manifest generated here would then
certify it as the pinned contract. The blobs of that commit are the pin.

```bash
PIN=ef03fefcded37676b19ef1c6f88b956a09a26d3f
DST=contracts/github-checker-actions/v1
mkdir -p "$DST"
git -C ../github-checker archive "$PIN" contracts/actions/v1 \
  | tar -x --strip-components=3 -C "$DST"
ls "$DST/fixtures" | wc -l   # expect 34
```

`git archive` fails outright if the commit is absent, so a wrong or unfetched
pin cannot silently produce a partial copy. Confirm the extraction is what the
commit holds, comparing against the object database rather than the tree:

```bash
git -C ../github-checker ls-tree -r --name-only "$PIN" contracts/actions/v1 \
  | sed 's|^contracts/actions/v1/||' | sort > /tmp/want.txt
(cd "$DST" && find . -type f | sed 's|^\./||' | sort) > /tmp/got.txt
diff /tmp/want.txt /tmp/got.txt && echo "extraction matches the pinned tree"
```

The consumer's own tests stay offline — they read the vendored copy only. It is
the *vendoring procedure* that must provably extract the named commit's blobs.

- [ ] **Step 2: Write `PINNED.txt`**

```
source: github-checker contracts/actions/v1
commit: ef03fefcded37676b19ef1c6f88b956a09a26d3f
vendored: 2026-07-31
note: pinned copy (repo-boundaries vendoring, ADR-ECO-003). Do not edit here —
  re-vendor from the pin and refresh manifest.json. Nothing in shipped code
  may read ../github-checker at run time.
```

- [ ] **Step 3: Generate `manifest.json`**

Every file of the normative surface — the schema, the README **and all 34 fixtures** — gets a `sha256`, plus a `tree_sha256` over the sorted list. The README is normative here: it carries the three-state rule and the evolution policy, and a consumer that vendored a schema without them would have the shape and not the meaning.

```python
# scripts/vendor_manifest.py (dev tool, not shipped runtime)
import hashlib, json, pathlib

root = pathlib.Path("contracts/github-checker-actions/v1")
surface = sorted(
    p for p in root.rglob("*")
    if p.is_file() and p.name not in {"PINNED.txt", "manifest.json"}
)
entries = [
    {"path": str(p.relative_to(root)),
     "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
    for p in surface
]
tree = hashlib.sha256(
    "".join(f"{e['path']}:{e['sha256']}\n" for e in entries).encode()
).hexdigest()
(root / "manifest.json").write_text(json.dumps({
    "contract": "github-checker-actions",
    "contract_version": 1,
    "producer_commit": "ef03fefcded37676b19ef1c6f88b956a09a26d3f",
    "surface_note": "sha256 of every vendored file; excludes PINNED.txt and this manifest",
    "tree_sha256": tree,
    "surface": entries,
}, indent=2) + "\n")
```

- [ ] **Step 4: Write the pin-verification test**

```python
def test_the_vendored_surface_matches_its_manifest() -> None:
    """A pinned copy nobody re-hashes is a copy that drifted quietly."""
    manifest = json.loads((VENDORED / "manifest.json").read_text())
    assert manifest["producer_commit"] == PRODUCER_COMMIT
    for entry in manifest["surface"]:
        blob = (VENDORED / entry["path"]).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == entry["sha256"], entry["path"]


def test_the_manifest_covers_every_vendored_file() -> None:
    """Per-file hashes are worthless if a file can be added without one."""
    listed = {e["path"] for e in json.loads((VENDORED / "manifest.json").read_text())["surface"]}
    on_disk = {
        str(p.relative_to(VENDORED))
        for p in VENDORED.rglob("*")
        if p.is_file() and p.name not in {"PINNED.txt", "manifest.json"}
    }
    assert listed == on_disk


def test_all_thirty_four_fixtures_are_present() -> None:
    assert len(list((VENDORED / "fixtures").glob("*.json"))) == 34


def test_the_tree_hash_is_recomputed_not_merely_stored() -> None:
    """Per-file hashes and coverage still leave `tree_sha256` unchecked: it
    could be anything and every other pin test would pass. Recompute it with
    the same canonical algorithm the manifest was built with."""
    manifest = json.loads((VENDORED / "manifest.json").read_text())
    entries = sorted(manifest["surface"], key=lambda e: e["path"])
    recomputed = hashlib.sha256(
        "".join(f"{e['path']}:{e['sha256']}\n" for e in entries).encode()
    ).hexdigest()
    assert recomputed == manifest["tree_sha256"]
```

- [ ] **Step 5: Prove the pin check has teeth**

Edit one byte of a vendored fixture, run the tests, confirm the hash test fails naming that file; restore; confirm green. Then add a file to `fixtures/` without touching the manifest and confirm the coverage test fails. Paste both runs.

- [ ] **Step 6: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . --fix && uv run pyrefly check && uv run pytest -q
git add contracts/github-checker-actions tests/test_contract_ingest.py scripts/
git commit -m "feat(contract): vendor github-checker actions/v1 pinned to ef03fef"
```

---

### Task 2: The strict ingestion boundary

**Files:**
- Create: `dispatcher/core/contract.py`
- Test: `tests/test_contract_ingest.py` (append)

**Interfaces:**
- Consumes: the vendored schema (Task 1).
- Produces: `ingest(raw: str, *, returncode: int) -> Ingested`, where `Ingested`
  is one of `ActionPayload | CliError | ContractError`, and every failure to
  interpret raises `ContractViolation`.

**The exit code is checked here, not later.** It is half the contract, and a
separate optional helper would be a guard callers can forget — the producer's
own suite was bitten by exactly that. `_invoke` passes the real
`proc.returncode`; the boundary refuses any combination the contract forbids:

| envelope | required exit |
|---|---|
| `action`, `ok: true` | `0` |
| `action`, `ok: false` | `1` |
| `cli_error` / `contract_error` | `1` |
| anything else | fail closed — an exit code the contract does not define is not a producer answer |

**The direction is one-way and must stay one-way:** raw JSON → schema validation → typed model. Nothing may construct a typed model from unvalidated input, and nothing downstream may see the raw dict. A boundary that can be bypassed is not a boundary.

**What fails closed, and why each is separate:**

| condition | outcome |
|---|---|
| stdout is not JSON | **consumer** failure — the producer said nothing we can read |
| `schema_version` missing or ≠ 1 | refuse; do **not** best-effort parse a version we do not know |
| `result_kind` unknown | refuse; an envelope variant we cannot interpret is not an empty result |
| payload fails schema validation | refuse, naming the validation error |
| `result_kind` is `cli_error` / `contract_error` | a typed error variant — **never** routed to an action payload, whatever `action` says |

**The prechecks must be type-strict, not value-loose.** Before any schema
work:

- the root must be a JSON **object** — an array or scalar is not an envelope;
- `type(schema_version) is int`, **not** `== 1`: in Python `True == 1`, so a
  payload carrying `schema_version: true` would sail through a value check;
- `result_kind` must be a **string** before it selects anything.

Only then is the schema leaf chosen.

**The trap to avoid:** `action` is diagnostic in the two error variants.
Selecting a verb's payload from it would resurrect exactly the conflation the
producer's contract removed.

- [ ] **Step 1: Write the failing test**

```python
def test_an_unknown_schema_version_is_refused() -> None:
    payload = _fixture("pull-success") | {"schema_version": 2}
    with pytest.raises(ContractViolation, match="schema_version"):
        ingest(json.dumps(payload), returncode=1)


def test_an_unknown_result_kind_is_refused() -> None:
    payload = _fixture("pull-success") | {"result_kind": "something_new"}
    with pytest.raises(ContractViolation, match="result_kind"):
        ingest(json.dumps(payload), returncode=1)


def test_a_missing_schema_version_is_refused_not_defaulted() -> None:
    payload = {k: v for k, v in _fixture("pull-success").items() if k != "schema_version"}
    with pytest.raises(ContractViolation):
        ingest(json.dumps(payload), returncode=1)


def test_non_json_is_a_consumer_failure_not_an_empty_result() -> None:
    with pytest.raises(ContractViolation, match="not JSON"):
        ingest("<html>gateway timeout</html>", returncode=1)


def test_a_cli_error_never_becomes_an_action_payload() -> None:
    """`action` is diagnostic there: it may name a verb, and must not
    select that verb's payload."""
    result = ingest(json.dumps(_fixture("cli-error")), returncode=1)
    assert isinstance(result, CliError)
    assert result.action == "merge", "kept for diagnosis"


@pytest.mark.parametrize(
    "raw, why",
    [
        ("[]", "a JSON array is not an envelope"),
        ('"a string"', "a scalar is not an envelope"),
        ('{"schema_version": true, "result_kind": "action"}', "True == 1 in Python"),
        ('{"schema_version": 1, "result_kind": null}', "kind must be a string"),
    ],
    ids=["array", "scalar", "bool-version", "null-kind"],
)
def test_the_prechecks_are_type_strict(raw: str, why: str) -> None:
    with pytest.raises(ContractViolation):
        ingest(raw, returncode=1)


@pytest.mark.parametrize(
    "fixture, returncode",
    [("pull-success", 1), ("pull-not-a-repo", 0), ("cli-error", 0)],
    ids=["ok-true-exit-1", "ok-false-exit-0", "cli-error-exit-0"],
)
def test_a_mismatched_exit_code_is_refused(fixture: str, returncode: int) -> None:
    """The exit code is contract: a producer that answers correctly and
    exits wrongly must not be accepted."""
    with pytest.raises(ContractViolation, match="exit"):
        ingest(json.dumps(_fixture(fixture)), returncode=returncode)


def test_a_payload_with_a_foreign_field_is_refused() -> None:
    payload = _fixture("pull-success") | {"merged": True}
    with pytest.raises(ContractViolation):
        ingest(json.dumps(payload), returncode=1)


def test_a_payload_missing_a_required_field_is_refused() -> None:
    payload = {k: v for k, v in _fixture("pull-success").items() if k != "local"}
    with pytest.raises(ContractViolation):
        ingest(json.dumps(payload), returncode=1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contract_ingest.py -q`
Expected: FAIL — `ImportError: cannot import name 'ingest'`

- [ ] **Step 3: Write the boundary**

Order matters and must be explicit: parse → discriminators → schema → model. Check the discriminators **before** full validation so an unknown version produces "unknown version", not a wall of schema errors about a shape that was never ours to judge.

- [ ] **Step 4: Run test to verify it passes**

- [ ] **Step 5: Prove each guard is load-bearing**

Mutate each in turn — accept any `schema_version`, accept an unknown `result_kind`, skip schema validation, route `cli_error` by `action` — and confirm each reddens a **named** test. Assert the target occurs exactly once before replacing and that the file changed after; a mutation that did not apply is not evidence.

- [ ] **Step 6: Commit**

---

### Task 3: Typed consumer models

**Files:**
- Modify: `dispatcher/core/actions.py`, `dispatcher/core/models.py`
- Test: `tests/test_contract_ingest.py`, `tests/test_actions.py`

**Interfaces:**
- Consumes: `ingest` (Task 2).
- Produces: `ActionOutcome.pr_detail: PrDetail | None`, `.matches: list[IssueRef] | None`, `.malformed: list[IssueRef] | None`, `.issue: IssueRef | None`.

**The semantics that must survive typing** — this is the whole reason the producer work happened:

- **absent** = the verb has no such concept;
- **`null`** = applicable, unknown;
- **`false`** = applicable, definitely negative.

**Never synthesise.** `merged`, `created`, `matches` come from the producer or
stay unknown. A consumer-side default is indistinguishable from a producer
answer once it is in the model, and that is how "could not read" becomes
"confirmed empty".

**Downstream receives the typed `Ingested`, and `ActionOutcome` does not
reassemble it.** Rebuilding the outcome field-by-field through pydantic
defaults would silently refill everything the producer deliberately omitted,
and the absent/`null` distinction — the entire reason for the producer
workstream — would be lost at the last step. `ActionOutcome` either *is* the
ingested value or wraps it by reference; it never reconstructs it. Pin that
with a test asserting `model_fields_set` after ingestion still lacks the
inapplicable fields.

- [ ] **Step 1: Write the failing test**

```python
def test_null_and_empty_survive_typing() -> None:
    unread = ingest(json.dumps(_fixture("issue-lookup-unread")), returncode=1)
    confirmed = ingest(json.dumps(_fixture("issue-lookup-free")), returncode=0)
    assert unread.matches is None, "the inbox was not read exhaustively"
    assert confirmed.matches == [], "read, and empty"


def test_a_verb_without_the_concept_has_no_field_at_all() -> None:
    pulled = ingest(json.dumps(_fixture("pull-success")), returncode=0)
    assert "matches" not in pulled.model_fields_set


def test_the_consumer_never_synthesises_a_value() -> None:
    """A default filled in here is indistinguishable from a producer answer
    once it is in the model."""
    unknown = ingest(json.dumps(_fixture("merge-unknown")), returncode=1)
    assert unknown.merged is None
    assert unknown.merged is not False
```

- [ ] **Step 2-4:** run, implement, re-run.

- [ ] **Step 5: Mutation**

Make the model default `matches` to `[]`; confirm `test_null_and_empty_survive_typing` reddens. Restore.

- [ ] **Step 6: Commit**

---

### Task 4: Consumer conformance

**Files:**
- Test: `tests/test_contract_ingest.py` (append)

**Every one of the 34 vendored fixtures must both validate and ingest.** A fixture that validates but cannot be turned into a model is a contract the consumer cannot actually consume.

- [ ] **Step 1: Fixture sweep**

```python
@pytest.mark.parametrize("path", VENDORED_FIXTURES, ids=lambda p: p.stem)
def test_every_vendored_fixture_ingests(path: Path) -> None:
    result = ingest(path.read_text(), returncode=_expected_exit(path))
    assert result is not None


def test_the_sweep_covers_every_fixture() -> None:
    """A glob that matched nothing would make the sweep vacuous."""
    assert len(VENDORED_FIXTURES) == 34
```

- [ ] **Step 2: Round-trip**

Ingest, re-serialise, and compare key sets: a significant `null` must not vanish and an inapplicable field must not appear. Compare **whole key sets**, not selected keys.

- [ ] **Step 3: The real S1/S2 paths**

Drive the paths this consumer actually has, against fixtures rather than fakes: the merge partial outcome (`merged: null`), `matches: []` versus `null`, the malformed and conflict lookups, and a nested non-null `PrDetail` with its non-empty `checks` / `files` / `review_threads`.

- [ ] **Step 4: Negative sweep**

A tampered hash, a missing required field, an extra field, an unknown `schema_version`, an unknown `result_kind`, and a mismatched `ok`/exit pair — each must fail closed with a message naming the reason.

- [ ] **Step 5: Commit**

---

### Task 5: Integration and documentation

**Files:**
- Modify: `README.md`, `TODO.md`

- [ ] **Step 1: Record the pin in `README.md`**

State the producer commit, that the copy is pinned and must not be edited in place, and that nothing reads the sibling repo at run time.

- [ ] **Step 2: Remove the provisional-adapter wording**

Search for every place that calls `pr_detail` / `matches` / `issue` an opaque passthrough or a provisional adapter. Those sentences were true and are now false — a doc that describes a boundary the code no longer has is worse than none.

- [ ] **Step 3: Close `@id:vendor-contracts-actions-v1`**

Mark it `[x]` with the **real** PR number, in the file's existing format. **Do
not delete or reword any other line** — the ecosystem's delta counters read a
vanished line as "closed".

**Do not commit a placeholder.** S1 shipped one and needed a separate cleanup
PR for it. The number does not exist while you implement, so the order is:

1. implementation complete, the TODO item still `[ ]`;
2. open the PR — now the number exists;
3. one further commit **into that same PR** flips the item to `[x]` with the
   real number;
4. re-run the gate and the review on that commit.

A placeholder is not a smaller version of this; it is a second PR.

- [ ] **Step 4: Full gate plus a live consumer smoke**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check && uv run pytest -q
node tests/web/task_authoring_harness.js dispatcher/server/static/index.html
node tests/web/runner_selftest.js dispatcher/server/static/index.html
```

Then a **live** smoke against the binary built from the pinned producer commit — verify `git -C ../github-checker rev-parse HEAD` is `ef03fef…` first — driving at least one read verb end to end and ingesting its real output. Read the real output before claiming success.

- [ ] **Step 5: Commit**

---

## Handoff

Open a PR under this repo's rules (PR-only, Copilot review actioned, **human merges**).

The next slice is `DESIGN-405` level 3: with the contract vendored and the boundary typed, the live-binary test can stop skipping. Today it skips everywhere — `github-checker` is not on `PATH` and CI does not install it — so `skipped` reads as "verified" while that rung runs nowhere. That is a separate, small change and does not belong in this PR.
