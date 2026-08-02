# WS-005 WS-B: gate-verdicts vendor + governance-collector — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Vendor the `gate-verdicts/v1` contract from steward @ `4836345a4250735ebce9de7616a4a42b463da654` and implement the governance-collector that classifies an observed repo's bundle into six states (`pass | blocked | no-data | unreadable | stale | unresolvable`) — inbox issue #106, slug `ws005-governance-collector`.

**Architecture:** The vendored copy follows the `contracts/github-checker-actions/v1` pattern exactly: pinned bytes + `PINNED.txt` + `manifest.json` (per-file sha256 + `tree_sha256`), produced by a one-input re-vendor script, guarded by an offline copy-integrity test in the PR gate and a *separate* scheduled upstream-drift job (dispatcher #99 rule). The collector (`dispatcher/core/governance.py`) reads `<repo>/.steward/gate_verdicts.jsonl`, validates each line against the vendored JSON Schema (runtime pattern of `core/contract.py`), asks an injectable git-facts provider about provenance freshness, and classifies. It never computes verdicts and never imports steward (ARCH-C1/C3).

**Tech Stack:** Python 3.12+, pydantic, jsonschema (already a runtime dep), pytest, bash (re-vendor script), GitHub Actions (drift job).

## Global Constraints

- Package management: `uv` only (`uv run pytest`, `uv run ruff`, `pyrefly check`). Never pip.
- Line length 88; type hints everywhere; public APIs get docstrings.
- Pin: steward commit `4836345a4250735ebce9de7616a4a42b463da654`, source subdir `contracts/gate-verdicts/v1` (SCHEMA.json + README.md + 5 fixtures).
- Vendored destination: `contracts/steward-gate-verdicts/v1/` (naming pattern `<producer>-<contract>/v<N>`, like `github-checker-actions`).
- ARCH-C1: no `import steward` / `from steward` anywhere under `dispatcher/` (structural test).
- ARCH-C3: collector classifies only — it never computes verdicts, never runs steward's CLI (review evidence, stated in PR body).
- CON-03: shipped code never resolves sibling-repo paths; tests read fixtures only from the *vendored* copy.
- NFR-02 (fail-closed): no read/parse error path may produce `pass`. Missing file → `no-data`; every other failure → `unreadable` with a reason.
- Two guarantees stay separate: copy-integrity = offline test in the ordinary suite (never skipped); upstream-drift = scheduled advisory workflow, never a PR check.
- Git workflow: branch `feat/ws005-governance-collector`, PR via `gh pr create`, no direct pushes to master, no merging (the user merges).

## State classification (single source for all tasks)

Precedence, first match wins:

1. `no-data` — `.steward/gate_verdicts.jsonl` absent (`FileNotFoundError` on read; no pre-check `exists()` — race-free).
2. `unreadable` — any other `OSError`, non-UTF-8 bytes, empty file, line 1 not a schema-valid header (special reason when `schema_version != "1"`: `unsupported schema_version <v>`), any later line invalid JSON or schema-invalid, or a `header` record on line > 1. Reason always names the line number / error class.
3. `stale` — header `dirty: true`, or the git-facts provider says the bundle content at `header.source_commit` differs from the bundle's current state, or freshness is unknown (git facts unavailable — unknown must not look green). Reason carries both commits (or the detail).
4. `unresolvable` — ≥1 finding whose `artifact` path is not in the file's own artifact inventory.
5. `blocked` — ≥1 finding (all resolvable).
6. `pass` — none of the above.

---

### Task 1: Branch + accept issue #106 in TODO.md

**Files:**
- Modify: `TODO.md` (new section item under «Governance-плоскость (ADR-ECO-004)»)

**Interfaces:**
- Produces: the branch `feat/ws005-governance-collector` all later tasks commit to; the plan item `@id:ws005-governance-collector`.

- [ ] **Step 1: Create the branch**

```bash
git switch master && git pull --ff-only && git switch -c feat/ws005-governance-collector
```

- [ ] **Step 2: Add the acceptance item to TODO.md**

Under `## Governance-плоскость (ADR-ECO-004)`, after the existing item, add (tags on ONE line with the checkbox — the parser is line-based):

```markdown
- [ ] WS-005 WS-B: вендор `gate-verdicts/v1` + governance-collector (6 состояний бандла) @owner:andrei @id:ws005-governance-collector
      Принятие inbox-issue #106 от steward (ADR-ECO-006). Канон:
      `steward/contracts/gate-verdicts/v1` @ `4836345`; копия —
      `contracts/steward-gate-verdicts/v1/` с раздельными copy-integrity
      (PR-гейт) и upstream-drift (scheduled). Collector читает
      `<repo>/.steward/gate_verdicts.jsonl` + git-факты и только
      классифицирует (ARCH-C1/C3): pass | blocked | no-data | unreadable |
      stale | unresolvable. Панель — WS-C, отдельная inbox-issue после.
```

- [ ] **Step 3: Commit**

```bash
git add TODO.md && git commit -m "docs(todo): accept inbox #106 — ws005-governance-collector (WS-B)"
```

---

### Task 2: Parameterize `vendor_manifest.py` for a second contract

**Files:**
- Modify: `scripts/vendor_manifest.py`
- Test: `tests/test_vendor_manifest.py`

**Interfaces:**
- Consumes: existing `build_manifest(root, producer_commit)`.
- Produces: `build_manifest(root: pathlib.Path, producer_commit: str, contract: str = "github-checker-actions", contract_version: int = 1) -> dict[str, object]` and CLI flags `--contract` / `--contract-version` with those defaults. Task 3's script calls it with `--contract steward-gate-verdicts`.

The generator currently hardcodes `"contract": "github-checker-actions"`. Defaults keep the existing byte-for-byte reproduction test green; explicit flags let the steward script reuse the generator instead of growing a second one.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_vendor_manifest.py`)

```python
def test_build_manifest_records_the_contract_it_was_given(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    manifest = vendor_manifest.build_manifest(
        tmp_path, "0" * 40, contract="steward-gate-verdicts", contract_version=1
    )
    assert manifest["contract"] == "steward-gate-verdicts"
    assert manifest["contract_version"] == 1


def test_contract_name_defaults_to_the_original_consumer(tmp_path: Path) -> None:
    """Existing callers pass no name; their manifests must not change shape."""
    (tmp_path / "a.txt").write_text("x")
    manifest = vendor_manifest.build_manifest(tmp_path, "0" * 40)
    assert manifest["contract"] == "github-checker-actions"
    assert manifest["contract_version"] == 1
```

- [ ] **Step 2: Run to verify the first fails**

Run: `uv run pytest tests/test_vendor_manifest.py -v`
Expected: `test_build_manifest_records_the_contract_it_was_given` FAILS with `TypeError: build_manifest() got an unexpected keyword argument 'contract'`.

- [ ] **Step 3: Implement**

In `scripts/vendor_manifest.py`: add keyword args to `build_manifest` and use them in the returned dict; add matching `parser.add_argument("--contract", default="github-checker-actions")` and `parser.add_argument("--contract-version", type=int, default=1)` in `main()`, passing them through. Update the module docstring's usage line to mention the flags.

- [ ] **Step 4: Run the whole file to verify everything passes (including byte-for-byte reproduction)**

Run: `uv run pytest tests/test_vendor_manifest.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/vendor_manifest.py tests/test_vendor_manifest.py
git commit -m "feat(vendor): vendor_manifest takes --contract/--contract-version"
```

---

### Task 3: Re-vendor script for steward gate-verdicts + perform the initial vendoring

**Files:**
- Create: `scripts/revendor_steward_gate_verdicts.sh` (adapted copy of `scripts/revendor_github_checker_actions.sh` — that script's own test suite pins its constants by content, so it is copied, not generalized; the shared logic that *could* move is the manifest generator, and Task 2 already shares it)
- Create: `contracts/steward-gate-verdicts/v1/` (by RUNNING the script — never by hand-copying)
- Create: `docs/revendor-steward-gate-verdicts.md`
- Test: `tests/test_revendor_steward_script.py`

**Interfaces:**
- Consumes: `scripts/vendor_manifest.py --contract steward-gate-verdicts` (Task 2).
- Produces: the vendored tree `contracts/steward-gate-verdicts/v1/{SCHEMA.json,README.md,fixtures/*.jsonl,PINNED.txt,manifest.json}` that Tasks 4–8 read.

- [ ] **Step 1: Write the script**

Copy `scripts/revendor_github_checker_actions.sh` to `scripts/revendor_steward_gate_verdicts.sh` and change ONLY:

```bash
PRODUCER_URL="https://github.com/andrei-shtanakov/steward"
SRC_SUBDIR="contracts/gate-verdicts/v1"
DST="$REPO_ROOT/contracts/steward-gate-verdicts/v1"
```

(`--strip-components=3` stays correct: `contracts/gate-verdicts/v1` is 3 segments.) The `PINNED.txt` heredoc becomes:

```
source: steward contracts/gate-verdicts/v1
commit: $NEW_PIN
vendored: $(date -u +%Y-%m-%d)
note: pinned copy (repo-boundaries vendoring, ADR-ECO-003). Do not edit here —
  re-vendor with scripts/revendor_steward_gate_verdicts.sh, which derives every
  value in this directory from the one commit it is given. Procedure:
  docs/revendor-steward-gate-verdicts.md. Nothing in shipped code may read
  ../steward at run time.
```

The `vendor_manifest.py` call gains `--contract steward-gate-verdicts` (version default 1 is right). The final `next:` hint should say: `uv run pytest tests/test_gate_verdicts_vendor.py tests/test_governance_collector.py -v`. Update the header comment's usage line to the new script name. `chmod +x` the script.

- [ ] **Step 2: Write the script's tests**

`tests/test_revendor_steward_script.py` — self-contained miniature producer (do NOT import from `tests/test_revendor_script.py`; that module is script-specific):

```python
"""The steward gate-verdicts re-vendor script, exercised offline via --from."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT_NAME = "revendor_steward_gate_verdicts.sh"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_GIT_ENV},
    )
    return result.stdout.strip()


@pytest.fixture
def producer(tmp_path: Path) -> dict[str, object]:
    """A miniature steward: two commits over contracts/gate-verdicts/v1.

    The second commit drops a fixture the first had — the file-deleted-
    upstream case is what a copy-over-the-top re-vendor gets wrong.
    """
    repo = tmp_path / "steward"
    src = repo / "contracts" / "gate-verdicts" / "v1"
    (src / "fixtures").mkdir(parents=True)
    (src / "SCHEMA.json").write_text('{"title": "v1"}\n')
    (src / "fixtures" / "clean.jsonl").write_text('{"kind": "header"}\n')
    (src / "fixtures" / "dropped.jsonl").write_text('{"kind": "header"}\n')
    _git(repo, "init", "--quiet")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "one")
    first = _git(repo, "rev-parse", "HEAD")
    (src / "fixtures" / "dropped.jsonl").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "two")
    second = _git(repo, "rev-parse", "HEAD")
    return {"repo": repo, "first": first, "second": second}


@pytest.fixture
def skeleton(tmp_path: Path) -> Path:
    """A copy of the minimal dispatcher layout the script needs."""
    root = tmp_path / "dispatcher"
    (root / "scripts").mkdir(parents=True)
    (root / "contracts").mkdir()
    for name in (SCRIPT_NAME, "vendor_manifest.py"):
        shutil.copy(REPO_ROOT / "scripts" / name, root / "scripts" / name)
    return root


def _run(skeleton: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(skeleton / "scripts" / SCRIPT_NAME), *args],
        capture_output=True,
        text=True,
    )


def test_happy_path_writes_pin_manifest_and_bytes(
    producer: dict[str, object], skeleton: Path
) -> None:
    pin = str(producer["second"])
    result = _run(skeleton, pin, "--from", str(producer["repo"]))
    assert result.returncode == 0, result.stderr
    dst = skeleton / "contracts" / "steward-gate-verdicts" / "v1"
    manifest = json.loads((dst / "manifest.json").read_text())
    assert manifest["contract"] == "steward-gate-verdicts"
    assert manifest["producer_commit"] == pin
    assert f"commit: {pin}" in (dst / "PINNED.txt").read_text()
    assert (dst / "SCHEMA.json").exists()
    assert (dst / "fixtures" / "clean.jsonl").exists()


def test_a_file_upstream_deleted_does_not_survive(
    producer: dict[str, object], skeleton: Path
) -> None:
    repo = str(producer["repo"])
    assert _run(skeleton, str(producer["first"]), "--from", repo).returncode == 0
    dst = skeleton / "contracts" / "steward-gate-verdicts" / "v1"
    assert (dst / "fixtures" / "dropped.jsonl").exists()
    assert _run(skeleton, str(producer["second"]), "--from", repo).returncode == 0
    assert not (dst / "fixtures" / "dropped.jsonl").exists()


def test_a_failed_run_leaves_the_previous_copy_intact(
    producer: dict[str, object], skeleton: Path
) -> None:
    repo = str(producer["repo"])
    assert _run(skeleton, str(producer["first"]), "--from", repo).returncode == 0
    dst = skeleton / "contracts" / "steward-gate-verdicts" / "v1"
    before = sorted(p.name for p in dst.rglob("*") if p.is_file())
    result = _run(skeleton, "0" * 40, "--from", repo)  # commit that does not exist
    assert result.returncode == 2
    assert sorted(p.name for p in dst.rglob("*") if p.is_file()) == before
```

- [ ] **Step 3: Run the tests**

Run: `uv run pytest tests/test_revendor_steward_script.py -v`
Expected: ALL PASS (script already written in Step 1; if any fail, fix the script, not the test).

- [ ] **Step 4: Perform the real vendoring from the local steward checkout**

```bash
scripts/revendor_steward_gate_verdicts.sh 4836345a4250735ebce9de7616a4a42b463da654 \
  --from ../steward
```

Expected on stderr: `re-vendored contracts/gate-verdicts/v1 at 4836345…`, `files: 7`. Then verify the copy matches the canon by eye: `ls contracts/steward-gate-verdicts/v1/fixtures/` → 5 jsonl files.

(Dev-time `--from` against the sibling checkout is exactly what the flag is for; the CON-03 rule binds *shipped* code, not this one-off command. The report will note the canonical remote was not consulted — acceptable, the pin is the merge commit of steward PR #33 already on GitHub.)

- [ ] **Step 5: Write the runbook**

`docs/revendor-steward-gate-verdicts.md` — mirror `docs/revendor-github-checker-actions.md`'s structure but for this contract: when to re-vendor (drift job red / steward announces v1 change), the one command (`scripts/revendor_steward_gate_verdicts.sh <pin>` or `--from ../steward`), what the script guarantees (staging + provenance verification + atomic swap), and what to run afterwards (`uv run pytest tests/test_gate_verdicts_vendor.py tests/test_governance_collector.py`). Read the existing doc first and keep the same headings.

- [ ] **Step 6: Commit**

```bash
git add scripts/revendor_steward_gate_verdicts.sh contracts/steward-gate-verdicts \
  docs/revendor-steward-gate-verdicts.md tests/test_revendor_steward_script.py
git commit -m "feat(vendor): pin steward gate-verdicts/v1 @ 4836345 via one-input script"
```

---

### Task 4: Copy-integrity test (guarantee A, offline, never skipped)

**Files:**
- Test: `tests/test_gate_verdicts_vendor.py`

**Interfaces:**
- Consumes: `contracts/steward-gate-verdicts/v1/` (Task 3).
- Produces: the PR-gate guarantee that the vendored copy matches its own manifest.

Mirror the integrity block of `tests/test_contract_ingest.py` (lines ~40–100 — read it first and keep the same assertions style):

- [ ] **Step 1: Write the tests**

```python
"""Copy-integrity of the vendored gate-verdicts/v1 contract (guarantee A).

Offline and consumer-owned: reads only the vendored copy, never a sibling
checkout, and therefore never skips. Upstream drift is guarantee B — the
scheduled advisory workflow — and is deliberately not asserted here.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

VENDORED_ROOT = (
    Path(__file__).parent.parent / "contracts" / "steward-gate-verdicts" / "v1"
)
PRODUCER_COMMIT = "4836345a4250735ebce9de7616a4a42b463da654"
_EXCLUDED_NAMES = {"PINNED.txt", "manifest.json"}


def _manifest() -> dict:
    return json.loads((VENDORED_ROOT / "manifest.json").read_text())


def _on_disk() -> dict[str, str]:
    return {
        str(p.relative_to(VENDORED_ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in VENDORED_ROOT.rglob("*")
        if p.is_file() and p.name not in _EXCLUDED_NAMES
    }


def test_manifest_names_the_pin_and_contract() -> None:
    manifest = _manifest()
    assert manifest["producer_commit"] == PRODUCER_COMMIT
    assert manifest["contract"] == "steward-gate-verdicts"
    assert manifest["contract_version"] == 1


def test_every_vendored_file_matches_its_manifest_hash() -> None:
    listed = {e["path"]: e["sha256"] for e in _manifest()["surface"]}
    assert listed == _on_disk()  # both directions: no extra, no missing, no drift


def test_tree_hash_is_reproducible_from_the_surface_list() -> None:
    manifest = _manifest()
    entries = sorted(manifest["surface"], key=lambda e: e["path"])
    recomputed = hashlib.sha256(
        "".join(f"{e['path']}:{e['sha256']}\n" for e in entries).encode()
    ).hexdigest()
    assert recomputed == manifest["tree_sha256"]


def test_pinned_txt_names_the_same_commit() -> None:
    pinned = (VENDORED_ROOT / "PINNED.txt").read_text()
    match = re.search(r"^commit: ([0-9a-f]{40})$", pinned, re.MULTILINE)
    assert match is not None
    assert match.group(1) == PRODUCER_COMMIT


def test_the_expected_surface_is_present() -> None:
    """The five canon fixtures and the schema are what the collector tests
    stand on; a re-vendor that silently loses one must fail here, not there."""
    paths = set(_on_disk())
    assert {
        "SCHEMA.json",
        "README.md",
        "fixtures/clean.jsonl",
        "fixtures/findings.jsonl",
        "fixtures/malformed_line.jsonl",
        "fixtures/future_schema.jsonl",
        "fixtures/dangling_artifact.jsonl",
    } == paths
```

- [ ] **Step 2: Run**

Run: `uv run pytest tests/test_gate_verdicts_vendor.py -v`
Expected: ALL PASS against the copy Task 3 produced.

- [ ] **Step 3: Commit**

```bash
git add tests/test_gate_verdicts_vendor.py
git commit -m "test(vendor): offline copy-integrity gate for steward-gate-verdicts/v1"
```

---

### Task 5: Upstream-drift job for steward (guarantee B, scheduled, advisory)

**Files:**
- Modify: `.github/workflows/upstream-drift.yml`

**Interfaces:**
- Consumes: `scripts/upstream_drift_report.py` (already takes `<canon-dir> --vendored <dir> --upstream-root <dir> --ref <ref>`; `_META` already excludes `PINNED.txt`/`manifest.json`).

- [ ] **Step 1: Add a second job**

Append a `drift-steward-gate-verdicts` job to the existing workflow, same shape as the `drift` job: checkout self; checkout `andrei-shtanakov/steward` at `ref: master` (the MOVING branch on purpose — same rationale comment as the existing job) into `_upstream/steward`; `setup-uv` + `uv sync`; run:

```yaml
      - name: compare steward canon against the vendored copy
        run: |
          set +e
          uv run python scripts/upstream_drift_report.py \
            _upstream/steward/contracts/gate-verdicts/v1 \
            --vendored contracts/steward-gate-verdicts/v1 \
            --upstream-root _upstream/steward \
            --ref master
          code=$?
          set -e
          case "$code" in
            0) echo "no upstream drift" ;;
            1) echo "::error::upstream drift — a deliberate re-vendor PR is due" ;;
            2) echo "::error::canon unavailable — nothing was compared (not 'no drift')" ;;
            *) echo "::error::reporter failed with $code" ;;
          esac
          exit "$code"
```

Keep the exit-code comment from the existing job (0/1/2, unavailable is red).

- [ ] **Step 2: Sanity-check the reporter locally against the sibling checkout**

```bash
uv run python scripts/upstream_drift_report.py \
  ../steward/contracts/gate-verdicts/v1 \
  --vendored contracts/steward-gate-verdicts/v1 \
  --upstream-root ../steward --ref local
```

Expected: exit 0, "No upstream drift" (local steward is at the pin).

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/upstream-drift.yml
git commit -m "ci: scheduled upstream-drift job for steward gate-verdicts/v1"
```

---

### Task 6: Verdict-file reading — records, parse, `no-data`/`unreadable` (BEH-02/03/04 + part of BEH-08)

**Files:**
- Create: `dispatcher/core/governance.py`
- Test: `tests/test_governance_collector.py`

**Interfaces:**
- Consumes: vendored `SCHEMA.json` (loaded at runtime the way `core/contract.py` does: `Path(__file__).resolve().parents[2] / "contracts" / "steward-gate-verdicts" / "v1" / "SCHEMA.json"`, `jsonschema.Draft202012Validator`, cached with `functools.cache`).
- Produces (used by Tasks 7–8 and by WS-C later):

```python
BundleState = Literal["pass", "blocked", "no-data", "unreadable", "stale", "unresolvable"]
VERDICTS_REL_PATH = Path(".steward") / "gate_verdicts.jsonl"

class VerdictHeader(BaseModel): ...    # schema_version, source_commit, dirty, generated_at, profile, bundle
class VerdictArtifact(BaseModel): ...  # path, node_id, status, owner_roles
class VerdictFinding(BaseModel): ...   # gate_id, verdict, artifact, message (+ reserved optionals)

class BundleFreshness(BaseModel):
    """What the git-facts provider learned; fresh=None means 'could not tell'."""
    fresh: bool | None
    current_commit: str | None = None
    detail: str | None = None

GitFactsProvider = Callable[[Path, str, str], BundleFreshness]
# (repo_root, bundle_rel_path, source_commit) -> BundleFreshness

class BundleGovernance(BaseModel):
    state: BundleState
    reason: str | None = None
    header: VerdictHeader | None = None
    artifacts: list[VerdictArtifact] = []
    findings: list[VerdictFinding] = []
    unresolvable_findings: list[VerdictFinding] = []

def collect_governance(
    repo_root: Path, *, git_facts: GitFactsProvider | None = None
) -> BundleGovernance: ...
```

In this task `git_facts=None` short-circuits freshness as fresh-unknown is NOT yet relevant: use fixtures whose classification is decided before the freshness step (`no-data`/`unreadable`), plus pass a stub for the valid-file smoke. Default wiring of the real provider happens in Task 7.

Module docstring must state the ARCH constraints: classification only (ARCH-C3), no steward import or CLI (ARCH-C1, FR-02), fail-closed (NFR-02), file location per ARCH-D1.

- [ ] **Step 1: Write the failing tests**

`tests/test_governance_collector.py`:

```python
"""Governance-collector classification (WS-005 WS-B, inbox #106).

Fixtures come ONLY from the vendored contract copy — the canon negative
classes are part of the contract surface, not hand-made test data (CON-03).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from dispatcher.core.governance import (
    VERDICTS_REL_PATH,
    BundleFreshness,
    collect_governance,
)

FIXTURES = (
    Path(__file__).parent.parent
    / "contracts"
    / "steward-gate-verdicts"
    / "v1"
    / "fixtures"
)


def fresh(_repo: Path, _bundle: str, _commit: str) -> BundleFreshness:
    return BundleFreshness(fresh=True, current_commit="ab" * 20)


def repo_with(tmp_path: Path, fixture: str) -> Path:
    target = tmp_path / "observed"
    target.mkdir()
    (target / VERDICTS_REL_PATH).parent.mkdir()
    shutil.copy(FIXTURES / fixture, target / VERDICTS_REL_PATH)
    return target


def test_missing_file_is_no_data_not_pass_and_not_an_error(tmp_path: Path) -> None:
    (tmp_path / "observed").mkdir()
    result = collect_governance(tmp_path / "observed", git_facts=fresh)
    assert result.state == "no-data"
    assert result.header is None


def test_malformed_line_is_unreadable_with_the_line_number(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, "malformed_line.jsonl")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"
    assert result.reason is not None and "line 3" in result.reason


def test_future_schema_version_is_unreadable_naming_the_version(
    tmp_path: Path,
) -> None:
    repo = repo_with(tmp_path, "future_schema.jsonl")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"
    assert result.reason is not None and "99" in result.reason


def test_empty_file_is_unreadable_not_pass(tmp_path: Path) -> None:
    repo = tmp_path / "observed"
    (repo / VERDICTS_REL_PATH).parent.mkdir(parents=True)
    (repo / VERDICTS_REL_PATH).write_text("")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"


def test_header_on_a_later_line_is_unreadable(tmp_path: Path) -> None:
    repo = tmp_path / "observed"
    (repo / VERDICTS_REL_PATH).parent.mkdir(parents=True)
    header = (FIXTURES / "clean.jsonl").read_text().splitlines()[0]
    (repo / VERDICTS_REL_PATH).write_text(header + "\n" + header + "\n")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"
    assert result.reason is not None and "line 2" in result.reason


def test_clean_fixture_with_fresh_facts_parses_to_pass(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, "clean.jsonl")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "pass"
    assert result.header is not None
    assert result.header.source_commit == "ab" * 20
    assert len(result.artifacts) == 2
    assert result.findings == []
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_governance_collector.py -v`
Expected: FAIL with `ModuleNotFoundError`/`ImportError` (`dispatcher.core.governance` does not exist).

- [ ] **Step 3: Implement `dispatcher/core/governance.py`**

Implementation notes (keep functions small; classification order from the header of this plan):

```python
def collect_governance(repo_root, *, git_facts=None):
    path = repo_root / VERDICTS_REL_PATH
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return BundleGovernance(state="no-data", reason="no gate_verdicts.jsonl")
    except OSError as err:
        return _unreadable(f"{type(err).__name__}: {err}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as err:
        return _unreadable(f"not UTF-8: {err}")
    parsed = _parse_lines(text)          # -> header/artifacts/findings or _unreadable
    if isinstance(parsed, BundleGovernance):
        return parsed
    ...freshness + inventory + findings classification (Tasks 6–7)...
```

`_parse_lines`: split on `\n`, skip trailing empty line only; line 1 must validate as header (`jsonschema` against `$defs/header` — validate the record against the full `oneOf` and then check `kind`); if header invalid because `schema_version` is a string ≠ `"1"`, reason = `unsupported schema_version <v> (line 1)`; each further line: `json.loads` failure → `unreadable` `"invalid JSON (line N)"`; schema failure → `unreadable` `"record does not match gate-verdicts/v1 (line N)"`; `kind == "header"` on line > 1 → `unreadable` `"unexpected second header (line N)"`. Build pydantic models from validated dicts (`extra="forbid"` mirrors `additionalProperties: false`).

Freshness step for this task: header `dirty` → stale (reason `"emitted from a dirty tree"`); then if `git_facts is None` → treat as unknown → stale with detail `"git facts unavailable"` (fail-closed); else call it. Inventory: `unresolvable_findings = [f for f in findings if f.artifact not in {a.path for a in artifacts}]` → state `unresolvable`, reason names the paths. Else findings → `blocked`. Else `pass`.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_governance_collector.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Type-check and lint**

Run: `uv run ruff format . && uv run ruff check . && pyrefly check`
Expected: clean (fix anything reported).

- [ ] **Step 6: Commit**

```bash
git add dispatcher/core/governance.py tests/test_governance_collector.py
git commit -m "feat(governance): verdicts reader — no-data/unreadable classes (BEH-02/03/04)"
```

---

### Task 7: Freshness (stale), inventory (unresolvable), findings (blocked) + real git-facts provider (BEH-05/06/07)

**Files:**
- Modify: `dispatcher/core/governance.py`
- Test: `tests/test_governance_collector.py` (append)

**Interfaces:**
- Produces: `git_bundle_freshness(repo_root: Path, bundle: str, source_commit: str) -> BundleFreshness` — the default provider `collect_governance` uses when `git_facts is None` is replaced by: default parameter stays `None`, but `None` now means "use `git_bundle_freshness`". (Task 6's temporary unknown-when-None rule is replaced here; the fail-closed behavior moves into the provider itself.)

- [ ] **Step 1: Write the failing classification tests (mock git facts — BEH-05 target)**

```python
def stale_facts(_repo: Path, _bundle: str, commit: str) -> BundleFreshness:
    return BundleFreshness(
        fresh=False, current_commit="cd" * 20, detail="bundle tree differs"
    )


def unknown_facts(_repo: Path, _bundle: str, _commit: str) -> BundleFreshness:
    return BundleFreshness(fresh=None, detail="not a git repository")


def test_source_commit_mismatch_is_stale_with_both_commits(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, "clean.jsonl")
    result = collect_governance(repo, git_facts=stale_facts)
    assert result.state == "stale"
    assert result.reason is not None
    assert "ab" * 20 in result.reason and "cd" * 20 in result.reason


def test_unknown_freshness_is_stale_never_pass(tmp_path: Path) -> None:
    """Fail-closed: a repo whose git facts cannot be read must not look green."""
    repo = repo_with(tmp_path, "clean.jsonl")
    result = collect_governance(repo, git_facts=unknown_facts)
    assert result.state == "stale"
    assert result.reason is not None and "not a git repository" in result.reason


def test_dirty_header_is_stale_even_with_fresh_facts(tmp_path: Path) -> None:
    repo = tmp_path / "observed"
    (repo / VERDICTS_REL_PATH).parent.mkdir(parents=True)
    lines = (FIXTURES / "clean.jsonl").read_text().splitlines()
    lines[0] = lines[0].replace('"dirty": false', '"dirty": true')
    (repo / VERDICTS_REL_PATH).write_text("\n".join(lines) + "\n")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "stale"


def test_findings_classify_as_blocked_with_findings_exposed(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, "findings.jsonl")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "blocked"
    assert {f.artifact for f in result.findings} == {
        "15-behaviour-spec.md",
        "10-requirements.md",
    }
    assert result.unresolvable_findings == []


def test_dangling_finding_is_unresolvable_not_pass_not_blocked(
    tmp_path: Path,
) -> None:
    repo = repo_with(tmp_path, "dangling_artifact.jsonl")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unresolvable"
    assert [f.artifact for f in result.unresolvable_findings] == ["99-ghost.md"]


def test_stale_wins_over_findings(tmp_path: Path) -> None:
    """Precedence: content of a stale file is not trusted enough to rank it."""
    repo = repo_with(tmp_path, "findings.jsonl")
    result = collect_governance(repo, git_facts=stale_facts)
    assert result.state == "stale"
```

- [ ] **Step 2: Run to verify the new ones fail**

Run: `uv run pytest tests/test_governance_collector.py -v`
Expected: Task 6 tests PASS; new tests FAIL (stale/unresolvable/blocked not implemented yet — or partially; drive the remaining logic from the failures).

- [ ] **Step 3: Implement classification + the real provider**

Classification per the precedence table. The provider:

```python
def git_bundle_freshness(repo_root, bundle, source_commit):
    """Compare the bundle's content at source_commit with its current state.

    Reads git only; unknown is reported as fresh=None, never as fresh=True —
    the caller classifies unknown as stale-grade (NFR-02).
    """
```

Implementation via `subprocess.run(["git", "-C", str(repo_root), ...], capture_output=True, text=True)`, helper `_git(repo_root, *args) -> str | None` returning `None` on nonzero exit / `OSError`:

1. `rev-parse --verify {source_commit}^{{commit}}` fails → `BundleFreshness(fresh=False, current_commit=head, detail="source commit unknown to this clone")` (head from `rev-parse HEAD`, may be `None`).
2. `rev-parse {source_commit}:{bundle}` vs `rev-parse HEAD:{bundle}` — differing tree ids → `fresh=False, current_commit=head, detail="bundle tree differs"`; the reason string in the classifier must include both `source_commit` and `current_commit`.
3. `status --porcelain -- {bundle}` non-empty → `fresh=False, detail="uncommitted changes under the bundle"`.
4. Any command unrunnable (`git` missing, not a repo) → `fresh=None, detail=<what failed>`.
5. Otherwise `fresh=True, current_commit=head`.

Wire as the default: in `collect_governance`, `provider = git_facts or git_bundle_freshness`.

- [ ] **Step 4: Add one real-git integration test (the provider itself)**

```python
def _seed_git_repo(tmp_path: Path) -> tuple[Path, str]:
    """A real observed repo whose bundle matches the clean fixture's layout."""
    import subprocess

    repo = tmp_path / "real"
    bundle = repo / "workstreams" / "WS-005-gate-verdicts" / "spec"
    bundle.mkdir(parents=True)
    (bundle / "10-requirements.md").write_text("r\n")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }

    def git(*args: str) -> str:
        import os

        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, **env},
        ).stdout.strip()

    git("init", "--quiet")
    git("add", "-A")
    git("commit", "--quiet", "-m", "seed")
    return repo, git("rev-parse", "HEAD")


def test_real_git_facts_fresh_then_stale(tmp_path: Path) -> None:
    from dispatcher.core.governance import git_bundle_freshness

    repo, head = _seed_git_repo(tmp_path)
    bundle = "workstreams/WS-005-gate-verdicts/spec"
    assert git_bundle_freshness(repo, bundle, head).fresh is True
    (repo / bundle / "10-requirements.md").write_text("changed\n")
    assert git_bundle_freshness(repo, bundle, head).fresh is False


def test_real_git_facts_outside_a_repo_are_unknown(tmp_path: Path) -> None:
    from dispatcher.core.governance import git_bundle_freshness

    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_bundle_freshness(plain, "spec", "ab" * 20).fresh is None
```

- [ ] **Step 5: Run everything**

Run: `uv run pytest tests/test_governance_collector.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Format, lint, type-check**

Run: `uv run ruff format . && uv run ruff check . && pyrefly check`

- [ ] **Step 7: Commit**

```bash
git add dispatcher/core/governance.py tests/test_governance_collector.py
git commit -m "feat(governance): stale/unresolvable/blocked + git-facts provider (BEH-05/06/07)"
```

---

### Task 8: IO-error property sweep (BEH-08) + ARCH-C1 structural test

**Files:**
- Test: `tests/test_governance_collector.py` (append)

**Interfaces:**
- Consumes: `collect_governance` (Tasks 6–7).

- [ ] **Step 1: Write the failing/green sweep**

The "property" is NFR-02 stated as a sweep, not examples: *no error class reaches pass*. (No hypothesis dep in this repo — a parametrized sweep over the whole `OSError` family plays that role.)

```python
_OS_ERRORS = [
    PermissionError("denied"),
    IsADirectoryError("is a dir"),
    InterruptedError("interrupted"),
    TimeoutError("timed out"),
    BlockingIOError("would block"),
    OSError("generic I/O failure"),
]


@pytest.mark.parametrize("err", _OS_ERRORS, ids=lambda e: type(e).__name__)
def test_every_io_error_class_is_unreadable_never_pass_never_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: OSError
) -> None:
    repo = repo_with(tmp_path, "clean.jsonl")

    def boom(_self: Path) -> bytes:
        raise err

    monkeypatch.setattr(Path, "read_bytes", boom)
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"
    assert result.reason is not None and type(err).__name__ in result.reason


def test_file_vanishing_between_listing_and_read_is_no_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FileNotFoundError is the one OSError that means no-data, and the
    collector must reach that via the read attempt itself — no exists()
    pre-check, no TOCTOU window."""
    repo = repo_with(tmp_path, "clean.jsonl")

    def gone(_self: Path) -> bytes:
        raise FileNotFoundError("vanished")

    monkeypatch.setattr(Path, "read_bytes", gone)
    assert collect_governance(repo, git_facts=fresh).state == "no-data"


def test_non_utf8_bytes_are_unreadable(tmp_path: Path) -> None:
    repo = tmp_path / "observed"
    (repo / VERDICTS_REL_PATH).parent.mkdir(parents=True)
    (repo / VERDICTS_REL_PATH).write_bytes(b"\xff\xfe{ not utf8")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"


def test_truncated_last_line_is_unreadable(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, "clean.jsonl")
    full = (repo / VERDICTS_REL_PATH).read_text()
    (repo / VERDICTS_REL_PATH).write_text(full[:-20])  # cut mid-record
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"


def test_dispatcher_never_imports_steward() -> None:
    """ARCH-C1, stated structurally (the import-detector obligation)."""
    package_root = Path(__file__).parent.parent / "dispatcher"
    offenders = [
        str(p)
        for p in package_root.rglob("*.py")
        for line in p.read_text().splitlines()
        if line.strip().startswith(("import steward", "from steward"))
    ]
    assert offenders == []
```

- [ ] **Step 2: Run**

Run: `uv run pytest tests/test_governance_collector.py -v`
Expected: ALL PASS (if any fails, fix `collect_governance` — e.g. a missed exception class — not the test).

- [ ] **Step 3: Full suite + checks**

Run: `uv run ruff format . && uv run ruff check . && pyrefly check && uv run pytest`
Expected: full suite green (~660+ tests), 0 skipped-that-should-run.

- [ ] **Step 4: Commit**

```bash
git add tests/test_governance_collector.py
git commit -m "test(governance): IO-error sweep is fail-closed (BEH-08) + ARCH-C1 no-steward-import"
```

---

### Task 9: PR + reviews

**Files:**
- Modify: `TODO.md` (flip the item to `[x]` + PR number, repo convention)

- [ ] **Step 1: Push and open the PR**

```bash
git push -u origin feat/ws005-governance-collector
gh pr create --title "feat: vendor gate-verdicts/v1 + governance-collector (WS-005 WS-B, inbox #106)" --body "$(cat <<'EOF'
Accepts inbox issue #106 (slug `ws005-governance-collector`, ADR-ECO-006).

- Vendored pinned copy of `steward/contracts/gate-verdicts/v1` @ `4836345a4250735ebce9de7616a4a42b463da654` (SCHEMA + README + 5 canon fixtures), produced by the one-input script `scripts/revendor_steward_gate_verdicts.sh`; per-file sha256 + tree_sha256 manifest.
- Two separate guarantees (dispatcher #99 rule): offline copy-integrity in the PR gate (`tests/test_gate_verdicts_vendor.py`, never skipped) + scheduled advisory `upstream-drift` job against steward master.
- `dispatcher/core/governance.py`: classifies a bundle into `pass | blocked | no-data | unreadable | stale | unresolvable` from the verdicts file + git facts. Fail-closed: no error path reaches pass (parametrized OSError sweep); missing file is no-data via the read attempt itself (no TOCTOU).
- ARCH-C1 held structurally (no-steward-import test); ARCH-C3 held by construction: the collector only classifies — it never computes verdicts and never executes steward's CLI (review evidence: `collect_governance` + `git_bundle_freshness` are the only entry points; the only subprocess is `git`).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 2: Flip the TODO item**

Change the Task 1 item to `- [x] … — PR #<N> @owner:andrei @id:ws005-governance-collector` (same line format), commit `docs(todo): ws005-governance-collector — PR #<N>`, push.

- [ ] **Step 3: Comment on issue #106**

```bash
gh issue comment 106 --body "Принято под slug \`ws005-governance-collector\` (пункт в TODO.md); реализация — PR #<N>."
```

- [ ] **Step 4: Read Copilot's review and iterate**

Wait for GitHub Copilot review on the PR (`gh pr view <N> --comments`, re-check after a few minutes). Valid findings → fix with new commits on the branch; invalid → reply with reasoning. Do NOT merge — the user merges.

---

## Self-review notes

- Spec coverage: BEH-02 (T6 missing-file), BEH-03 (T6 malformed), BEH-04 (T6 future schema), BEH-05 (T7 mock + real git), BEH-06 (T7 dangling), BEH-07 (T7 findings/blocked), BEH-08 (T8 sweep), NFR-01 (disk-only: no network anywhere on the read path — the only subprocess is local `git`), NFR-02 (T8), CON-02 (T3/T4/T5 two guarantees), CON-03 (tests read the vendored fixtures; runtime reads only configured repo roots), ARCH-C1 (T8 structural), ARCH-C3 (PR body evidence), issue item 1/2/3 → T2–T5 / T6–T7 / T6–T8. BEH-01/BEH-07 UI halves and BEH-09 are WS-C (out of scope, per decomposition).
- `pass` state name: it's a `Literal` string, not an identifier — no clash with the keyword.
- Precedence pinned in one table at the top; T7's `test_stale_wins_over_findings` locks the one genuinely debatable ordering.
