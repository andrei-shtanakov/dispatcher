# PP needs_human (phase 2) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One vertical PR delivering phase 2 of #129 (inbox #136): atomic three-contract re-pin @ `51e3103`, vendored `loop-state/v1`, fail-closed `loop.state` classification with `needs_human` waits, additive API fields, panel chips + needs_human table, three-way checkout agreement, acceptance #136 — spec `docs/superpowers/specs/2026-08-12-product-proposal-needs-human-design.md`.

**Architecture:** A delta on the shipped phase-1 machinery: the re-vendor script grows to three contracts (one pin preserved), `core/product_proposals.py` gains `_load_loop_state` (strict JSON + vendored-schema validation + local proposal-id membership check) wired into `_load_bundle`, the report gains additive `loop_status`/`loop_waits`/`needs_human`, and the existing panel/harness are extended. No new routes, no new surfaces.

**Tech Stack:** Python 3.12+, pydantic, jsonschema, stdlib json (strict `object_pairs_hook`), pytest + anyio + httpx, bash, Node 22 harness.

## Global Constraints

- Package management: `uv` only (`uv run pytest`, `uv run ruff format .`, `uv run ruff check .`, `uv run pyrefly check`). Never pip. Line length 88.
- **Pin (all three contracts):** impresario commit `51e3103b5c88989a1d4a01a659d21790a92bb76b`. Anti-mix: the copy-integrity gate asserts ALL THREE manifests' `producer_commit` are equal.
- Evidence to preserve: `product-proposal/v1` and `gate-decision/v1` schemas are byte-identical between `28727ff` and `51e3103` (their schema.json sha256 in the manifests must not change; state this in the PR body).
- New diagnostic codes (all → bundle state `unknown`, NEVER `unreadable`): `loop-state-unreadable` (incl. duplicate JSON keys — no separate public code), `loop-state-schema-invalid`, `loop-state-path-escape`, `loop-state-proposal-mismatch` (message carries the relpath and BOTH proposal ids).
- Non-ok rule (owner-fixed): whenever the bundle's final state is non-`ok`, `loop_status` is `"unknown"` and `loop_waits` is empty — unconditionally, including when the file is absent.
- `LoopWait` identity = `(loop_id, iteration)`; `bundle_path` is a deterministic tie-break + provenance, NOT identity. Aggregate sort `(loop_id, iteration, bundle_path)`.
- A plain `LoopWait` does NOT raise `attention`.
- Out of scope: TUI/VSCode/MCP; impresario's `LOOPSTATE_IDEA_REF`/`IDEA_HASH`/`XLOG` cross-checks; new write-actions; phase-1 refactoring beyond what loop-state needs.
- Dev tooling may read `../impresario` git objects ONLY via `--from` script runs and the pinned `git show`/`git archive` commands this plan names; shipped code and tests never touch the sibling.
- Git: branch `feat/pp-needs-human` (exists, carries the spec), PR via `gh pr create`, no direct master pushes, no merging (the user merges).

---

### Task 1: Three-contract re-pin @ 51e3103 (script, run, gate tests, drift job)

**Files:**
- Modify: `scripts/revendor_impresario_contracts.sh` (CONTRACTS array)
- Create (by running it): `contracts/impresario-loop-state/v1/*`; regenerated `PINNED.txt`/`manifest.json` in the two existing dirs
- Modify: `tests/test_impresario_contracts_vendor.py`
- Modify: `tests/test_revendor_impresario_script.py`
- Modify: `.github/workflows/upstream-drift.yml` (third pair in the existing loop)

**Interfaces:**
- Produces: `contracts/impresario-loop-state/v1/schema.json` (loaded by Task 3's validator); all three manifests naming `51e3103b5c88989a1d4a01a659d21790a92bb76b`.

- [ ] **Step 1: Extend the CONTRACTS array**

In `scripts/revendor_impresario_contracts.sh` (lines 31-34) the array gains a third entry:

```bash
CONTRACTS=(
  "contracts/product-proposal/v1|contracts/impresario-product-proposal/v1|impresario-product-proposal"
  "contracts/gate-decision/v1|contracts/impresario-gate-decision/v1|impresario-gate-decision"
  "contracts/loop-state/v1|contracts/impresario-loop-state/v1|impresario-loop-state"
)
```

Also update the header comment sentence «contracts/impresario-product-proposal/v1 and contracts/impresario-gate-decision/v1 are always re-vendored together» to name all three (keep the rest verbatim).

- [ ] **Step 2: Run the re-vendor at the new pin**

```bash
scripts/revendor_impresario_contracts.sh 51e3103b5c88989a1d4a01a659d21790a92bb76b --from ../impresario
```

Expected: `re-vendored both impresario contracts at 51e3103…` on stderr (the wording says "both"; update that line to "all impresario contracts" while you are in the file). Then verify the byte-evidence:

```bash
git diff --stat contracts/impresario-product-proposal contracts/impresario-gate-decision
```

Expected: ONLY `PINNED.txt` and `manifest.json` changed in each (producer_commit + vendored date); `schema.json` and every fixture untouched. If a schema or fixture changed — STOP, that contradicts the verified evidence; report BLOCKED.

- [ ] **Step 3: Update the copy-integrity gate**

In `tests/test_impresario_contracts_vendor.py`:

```python
PRODUCER_COMMIT = "51e3103b5c88989a1d4a01a659d21790a92bb76b"
```

Add to `VENDORED`:

```python
    "impresario-loop-state": CONTRACTS_ROOT / "impresario-loop-state" / "v1",
```

Add to `EXPECTED_SURFACES`:

```python
    "impresario-loop-state": {
        "schema.json",
        "fixtures/invalid/bad-hash.json",
        "fixtures/invalid/empty-reason.json",
        "fixtures/invalid/extra-field.json",
        "fixtures/invalid/missing-at.json",
        "fixtures/invalid/unknown-verdict.json",
        "fixtures/valid/failed.json",
        "fixtures/valid/needs-human.json",
        "fixtures/valid/ready.json",
        "fixtures/valid/running.json",
    },
```

Update the anti-mix test's docstring wording from "two" to "three" (its body — `pins == {PRODUCER_COMMIT}` over `VENDORED.values()` — already spans all entries).

Run: `uv run pytest tests/test_impresario_contracts_vendor.py -v`
Expected: all PASS (now parametrized over three contracts).

- [ ] **Step 4: Extend the re-vendor script test's miniature producer**

In `tests/test_revendor_impresario_script.py`:

```python
DSTS = (
    "contracts/impresario-product-proposal/v1",
    "contracts/impresario-gate-decision/v1",
    "contracts/impresario-loop-state/v1",
)
```

In the `producer` fixture, after the gate-decision block add:

```python
    ls = repo / "contracts" / "loop-state" / "v1"
    (ls / "fixtures").mkdir(parents=True)
    (ls / "schema.json").write_text('{"title": "ls"}\n')
    (ls / "fixtures" / "ok.json").write_text('{"a": 1}\n')
```

(before the first commit, so both commits carry it). The `sandbox` fixture and all four tests already iterate `DSTS`; the `test_failure_restores_both_previous_copies` half-repo (only product-proposal present) now proves NEITHER of the three changes — update its docstring wording from "NEITHER directory" to "NONE of the three".

Run: `uv run pytest tests/test_revendor_impresario_script.py -v`
Expected: all PASS.

- [ ] **Step 5: Third pair in the drift job**

In `.github/workflows/upstream-drift.yml`, job `drift-impresario-contracts`, the `for pair in` list gains:

```yaml
            "contracts/loop-state/v1|contracts/impresario-loop-state/v1" \
```

(same line style as the two existing pairs; keep `--canon-probe schema.json`). Validate:
`uv run python -c "import yaml,pathlib; yaml.safe_load(pathlib.Path('.github/workflows/upstream-drift.yml').read_text())"`

- [ ] **Step 6: Dry-run the drift reporter for the new pair**

```bash
uv run python scripts/upstream_drift_report.py \
  ../impresario/contracts/loop-state/v1 \
  --vendored contracts/impresario-loop-state/v1 \
  --upstream-root ../impresario --ref local-dry-run --canon-probe schema.json
```

Expected: exit 0 (we vendored at the sibling's HEAD merge commit).

- [ ] **Step 7: Gates + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add scripts/revendor_impresario_contracts.sh contracts/impresario-product-proposal contracts/impresario-gate-decision contracts/impresario-loop-state tests/test_impresario_contracts_vendor.py tests/test_revendor_impresario_script.py .github/workflows/upstream-drift.yml
git commit -m "feat: re-pin all impresario contracts @ 51e3103; vendor loop-state/v1"
```

---

### Task 2: Checkout script three-way agreement + regression test

**Files:**
- Modify: `scripts/checkout_pinned_impresario.sh:43-65` (the pin-read block + header comment)
- Create: `tests/test_checkout_pinned_impresario.py`

**Interfaces:**
- Produces: the script now dies (exit 3) BEFORE any checkout when any of the THREE manifests disagrees; Task 6's live smoke uses the pin only after this check.

- [ ] **Step 1: Write the failing regression test**

`tests/test_checkout_pinned_impresario.py`:

```python
"""checkout_pinned_impresario.sh: the three-way pin-agreement gate.

Sandbox discipline of tests/test_revendor_impresario_script.py: the script
and the three vendored manifests are COPIED into tmp_path, so the real
contracts tree is never touched. Only the disagreement direction lives
here — the agreement-pass direction is proven by the live smoke, which
uses the pin only after this check succeeds.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
CONTRACT_DIRS = (
    "contracts/impresario-product-proposal/v1",
    "contracts/impresario-gate-decision/v1",
    "contracts/impresario-loop-state/v1",
)
OTHER_PIN = "a" * 40  # valid 40-hex, guaranteed different


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    box = tmp_path / "dispatcher"
    (box / "scripts").mkdir(parents=True)
    script = box / "scripts" / "checkout_pinned_impresario.sh"
    script.write_bytes(
        (REPO_ROOT / "scripts" / "checkout_pinned_impresario.sh").read_bytes()
    )
    script.chmod(0o755)
    for rel in CONTRACT_DIRS:
        (box / rel).mkdir(parents=True)
        manifest = json.loads((REPO_ROOT / rel / "manifest.json").read_text())
        (box / rel / "manifest.json").write_text(json.dumps(manifest))
    return box


def _run(box: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(box / "scripts" / "checkout_pinned_impresario.sh")],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_loop_state_pin_disagreement_fails_before_any_checkout(
    sandbox: Path,
) -> None:
    """ONLY the loop-state manifest names another pin: exit 3 with the
    provenance message, nothing fetched, nothing printed on stdout."""
    manifest_path = sandbox / CONTRACT_DIRS[2] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["producer_commit"] = OTHER_PIN
    manifest_path.write_text(json.dumps(manifest))
    result = _run(sandbox)
    assert result.returncode == 3
    assert "disagree" in result.stderr
    assert result.stdout == ""


def test_non_hex_pin_fails_closed(sandbox: Path) -> None:
    manifest_path = sandbox / CONTRACT_DIRS[2] / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["producer_commit"] = "not-a-sha"
    manifest_path.write_text(json.dumps(manifest))
    result = _run(sandbox)
    assert result.returncode == 3
    assert "40-hex" in result.stderr


def test_missing_loop_state_manifest_fails_closed(sandbox: Path) -> None:
    (sandbox / CONTRACT_DIRS[2] / "manifest.json").unlink()
    result = _run(sandbox)
    assert result.returncode == 3
    assert "could not read producer_commit" in result.stderr
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_checkout_pinned_impresario.py -v`
Expected: FAIL — the current script reads only two manifests, so the
loop-state mutation is invisible: the first test sees exit 0 (or a fetch
attempt), not exit 3.

- [ ] **Step 3: Rewrite the pin-read block**

In `scripts/checkout_pinned_impresario.sh`, update the header comment
(`the two vendored manifests` → `all three vendored manifests`) and replace
the block from `read -r PIN_A PIN_B …` through `PIN="$PIN_A"` (currently
after the `PINS="$(python3 … )"` capture) so the whole pin section reads:

```bash
command -v python3 > /dev/null 2>&1 || die 2 "python3 not found on PATH"
PINS="$(python3 - "$REPO_ROOT" << 'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]) / "contracts"
names = (
    "impresario-product-proposal",
    "impresario-gate-decision",
    "impresario-loop-state",
)
print(
    " ".join(
        json.load(open(root / name / "v1" / "manifest.json"))["producer_commit"]
        for name in names
    )
)
PY
)" || die 3 "could not read producer_commit from the vendored manifests"
read -r -a PIN_LIST <<< "$PINS"
[ "${#PIN_LIST[@]}" -eq 3 ] ||
  die 3 "could not read producer_commit from the vendored manifests: $PINS"
for pin in "${PIN_LIST[@]}"; do
  [[ "$pin" =~ ^[0-9a-f]{40}$ ]] ||
    die 3 "not a full 40-hex producer_commit: $pin"
done
[ "${PIN_LIST[0]}" = "${PIN_LIST[1]}" ] && [ "${PIN_LIST[1]}" = "${PIN_LIST[2]}" ] ||
  die 3 "the three manifests disagree on producer_commit: ${PIN_LIST[*]}"
PIN="${PIN_LIST[0]}"
```

(The `PINS="$(python3 …)" || die 3` capture-then-read pattern is the
PR #132-review fix — keep it exactly; only the python heredoc's `names`
tuple and the post-read validation change.)

- [ ] **Step 4: Run the tests + syntax + live direction**

```bash
bash -n scripts/checkout_pinned_impresario.sh
uv run pytest tests/test_checkout_pinned_impresario.py -v
IMPRESARIO_PINNED_DIR="$(scripts/checkout_pinned_impresario.sh --from ../impresario)" \
  uv run pytest tests/test_product_proposals_live_smoke.py -v
```

Expected: 3 PASS; live smoke still PASSES (agreement holds at 51e3103 after Task 1).

- [ ] **Step 5: Gates + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add scripts/checkout_pinned_impresario.sh tests/test_checkout_pinned_impresario.py
git commit -m "feat: three-way pin agreement in the impresario checkout (regression-tested)"
```

---

### Task 3: Core — LoopStatus/LoopWait models, strict JSON, `_load_loop_state`

**Files:**
- Modify: `dispatcher/core/product_proposals.py`
- Test: `tests/test_product_proposals.py`

**Interfaces:**
- Produces (Task 4 wires them):
  - `LoopStatus = Literal["absent","running","needs_human","ready_for_business","failed","unknown"]`
  - `LoopWait(loop_id: str, iteration: int, proposal_id: str, reason: str, stopped_at: str, bundle_path: str)`
  - `_load_loop_state(bundle_dir: Path, mirror_root: Path, proposal: dict[str, object] | None) -> tuple[LoopStatus, LoopWait | None, list[Diagnostic]]`
  - `ProposalBundle.loop_status: LoopStatus = "absent"`, `ProposalBundle.loop_waits: list[LoopWait]`
  - `ProductProposalsReport.needs_human: list[LoopWait]`

- [ ] **Step 1: Add test helpers + failing unit tests**

Append to `tests/test_product_proposals.py` (reuse `_mk`, `proposal_yaml`, `make_bundle`, `make_mirror`):

```python
import json as _json


def loop_state_json(
    pid: str = "PP-101",
    verdict: str | None = "ready_for_business",
    iteration: int = 2,
    reason: str = "done",
    at: str = "2026-08-12T04:01:21Z",
) -> str:
    stop = (
        None
        if verdict is None
        else {"verdict": verdict, "reason": reason, "iteration": iteration, "at": at}
    )
    return _json.dumps(
        {
            "loop_id": "LOOP-101",
            "idea_ref": "idea://IDEA-101",
            "idea_input_hash": "sha256:" + "f" * 64,
            "proposal_id": pid,
            "exchange_log_id": "XL-101",
            "max_iterations": 3,
            "stop": stop,
        }
    )


def _loop(
    tmp_path: Path, text: str | bytes, proposal: str | None = None
) -> "ProposalBundle":
    bundle = make_bundle(
        tmp_path,
        proposal=proposal or proposal_yaml(status="ready_for_business", version=6),
    )
    target = bundle / "loop.state"
    if isinstance(text, bytes):
        target.write_bytes(text)
    else:
        target.write_text(text)
    return _bundle(tmp_path)


def test_loop_state_absent_is_normal(tmp_path: Path) -> None:
    make_bundle(tmp_path, proposal=proposal_yaml(status="approved"))
    b = _bundle(tmp_path)
    assert b.state == "ok"
    assert b.loop_status == "absent" and b.loop_waits == []


def test_loop_state_running_has_no_wait(tmp_path: Path) -> None:
    b = _loop(tmp_path, loop_state_json(verdict=None))
    assert b.state == "ok"
    assert b.loop_status == "running" and b.loop_waits == []


def test_loop_state_terminals_are_not_human_waits(tmp_path: Path) -> None:
    for verdict in ("ready_for_business", "failed"):
        mirror = Path(str(tmp_path)) / verdict
        make_bundle(mirror, proposal=proposal_yaml(status="approved"))
        (mirror / "pilot" / "pp-101" / "loop.state").write_text(
            loop_state_json(verdict=verdict)
        )
        b = _bundle(mirror)
        assert b.state == "ok"
        assert b.loop_status == verdict and b.loop_waits == []


def test_loop_state_needs_human_yields_one_wait(tmp_path: Path) -> None:
    b = _loop(
        tmp_path,
        loop_state_json(verdict="needs_human", reason="решить exempt-семантику"),
    )
    assert b.state == "ok" and b.loop_status == "needs_human"
    assert [
        (w.loop_id, w.iteration, w.proposal_id, w.reason, w.stopped_at)
        for w in b.loop_waits
    ] == [
        (
            "LOOP-101",
            2,
            "PP-101",
            "решить exempt-семантику",
            "2026-08-12T04:01:21Z",
        )
    ]
    assert b.loop_waits[0].bundle_path == "pilot/pp-101"


def test_loop_state_not_json_is_unknown(tmp_path: Path) -> None:
    b = _loop(tmp_path, "not json {{{")
    assert b.state == "unknown" and b.loop_status == "unknown"
    assert [d.code for d in b.diagnostics] == ["loop-state-unreadable"]
    assert b.waits == [] and b.loop_waits == []


def test_loop_state_duplicate_json_keys_are_unreadable(tmp_path: Path) -> None:
    """json.loads keeps the last duplicate silently — fail-closed requires
    rejection (part of loop-state-unreadable, no separate code)."""
    text = loop_state_json()[:-1] + ', "proposal_id": "PP-999"}'
    b = _loop(tmp_path, text)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["loop-state-unreadable"]


def test_loop_state_not_utf8_is_unreadable(tmp_path: Path) -> None:
    b = _loop(tmp_path, b"\xff\xfe not utf8")
    assert [d.code for d in b.diagnostics] == ["loop-state-unreadable"]


def test_loop_state_oserror_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_bundle(tmp_path, proposal=proposal_yaml(status="approved"))
    (tmp_path / "pilot" / "pp-101" / "loop.state").write_text(loop_state_json())
    real = Path.read_bytes

    def failing(self: Path) -> bytes:
        if self.name == "loop.state":
            raise OSError(5, "Input/output error")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", failing)
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["loop-state-unreadable"]


def test_loop_state_schema_invalid_is_unknown(tmp_path: Path) -> None:
    b = _loop(tmp_path, '{"loop_id": "LOOP-101"}')
    assert b.state == "unknown" and b.loop_status == "unknown"
    assert [d.code for d in b.diagnostics] == ["loop-state-schema-invalid"]


def test_loop_state_proposal_mismatch_is_unknown(tmp_path: Path) -> None:
    b = _loop(tmp_path, loop_state_json(pid="PP-999"))
    assert b.state == "unknown" and b.loop_status == "unknown"
    diag = b.diagnostics[0]
    assert diag.code == "loop-state-proposal-mismatch"
    assert "PP-999" in diag.message and "PP-101" in diag.message
    assert diag.path == "pilot/pp-101/loop.state"
    assert b.waits == [] and b.loop_waits == []


def test_loop_state_symlink_escape_is_unknown(tmp_path: Path) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text(loop_state_json())
    mirror = tmp_path / "mirror"
    bundle = make_bundle(mirror, proposal=proposal_yaml(status="approved"))
    (bundle / "loop.state").symlink_to(outside)
    b = _bundle(mirror)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["loop-state-path-escape"]


def test_loop_state_in_mirror_symlink_stays_readable(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    bundle = make_bundle(mirror, proposal=proposal_yaml(status="approved"))
    real = bundle / "real-loop.json"
    real.write_text(loop_state_json())
    (bundle / "loop.state").symlink_to(real)
    b = _bundle(mirror)
    assert b.state == "ok" and b.loop_status == "ready_for_business"


def test_untrusted_proposal_collects_loop_read_errors_only(
    tmp_path: Path,
) -> None:
    """No trusted subject: read/parse errors collected; schema and the
    membership check skipped; the non-ok rule owns loop_status."""
    bundle = make_bundle(tmp_path, proposal="status: [broken\n")
    (bundle / "loop.state").write_bytes(b"\xff\xfe")
    b = _bundle(tmp_path)
    assert b.state == "unreadable"
    assert sorted(d.code for d in b.diagnostics) == [
        "loop-state-unreadable",
        "proposal-unreadable",
    ]
    assert b.loop_status == "unknown"


def test_untrusted_proposal_skips_loop_semantic_classification(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path, proposal="status: [broken\n")
    (bundle / "loop.state").write_text(loop_state_json(pid="PP-999"))
    b = _bundle(tmp_path)
    assert b.state == "unreadable"
    assert [d.code for d in b.diagnostics] == ["proposal-unreadable"]
    assert b.loop_status == "unknown"  # no mismatch diagnostic, no trust
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_product_proposals.py -k loop -v`
Expected: FAIL — `loop_status` attribute / `_load_loop_state` do not exist.

- [ ] **Step 3: Implement models + loader**

In `dispatcher/core/product_proposals.py`:

1. Imports: add `import json` next to the existing stdlib imports.
2. Below `BundleState` add:

```python
LoopStatus = Literal[
    "absent",              # no loop.state file — normal, NOT an error
    "running",             # valid file, stop: null
    "needs_human",         # valid file, active human wait
    "ready_for_business",  # valid file, terminal
    "failed",              # valid file, terminal
    "unknown",             # loop state untrusted (file problems, or the
                           # bundle is non-ok — the caller's rule)
]
_TERMINAL_LOOP: dict[str, LoopStatus] = {
    "ready_for_business": "ready_for_business",
    "failed": "failed",
}
```

3. Below `GateWait` add:

```python
class LoopWait(BaseModel):
    """One «the researcher↔creator loop waits for a human» record."""

    loop_id: str
    # stop.iteration; wait identity = (loop_id, iteration) — a repeated
    # needs_human after resume is a NEW wait, not a duplicate.
    iteration: int
    proposal_id: str
    reason: str
    # stop.at — the actual stop time (unlike proposal_updated_at, this one
    # IS a proven wait-start moment).
    stopped_at: str
    # Deterministic tie-break + provenance, NOT part of the identity.
    bundle_path: str
```

4. `ProposalBundle` gains (after `waits`):

```python
    # "absent" is normal; "unknown" whenever the file is untrusted OR the
    # bundle is non-ok. loop_waits computed ONLY for state == "ok" — empty
    # on any other state means «suppressed», never «no loop wait».
    loop_status: LoopStatus = "absent"
    loop_waits: list[LoopWait] = Field(default_factory=list)
```

5. `ProductProposalsReport` gains (after `waits`):

```python
    needs_human: list[LoopWait] = Field(default_factory=list)
```

6. Next to `_decision_validator` add:

```python
_LOOP_STATE_SCHEMA = _CONTRACTS / "impresario-loop-state" / "v1" / "schema.json"


@functools.cache
def _loop_state_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_LOOP_STATE_SCHEMA.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)
```

7. Below `_supersession_integrity` add:

```python
def _json_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """object_pairs_hook rejecting duplicate keys (plain json.loads keeps
    the last value silently — the strict-YAML discipline, applied to JSON)."""
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"duplicate JSON key {key!r}")
        obj[key] = value
    return obj


def _load_loop_state(
    bundle_dir: Path, mirror_root: Path, proposal: dict[str, object] | None
) -> tuple[LoopStatus, LoopWait | None, list[Diagnostic]]:
    """Read <bundle>/loop.state fail-closed (spec «Classification»).

    Absent is normal. Any problem yields decision-grade diagnostics (the
    caller's non-ok rule then forces "unknown"). A wait comes only from a
    fully validated stop.verdict == needs_human whose proposal_id matches
    the bundle's proposal — the ONLY producer cross-check replicated here;
    the other LOOPSTATE_* checks stay with impresario's validator.
    """
    path = bundle_dir / "loop.state"
    rel = _relpath(path, mirror_root)

    def problem(code: str, message: str) -> tuple[
        LoopStatus, LoopWait | None, list[Diagnostic]
    ]:
        return "unknown", None, [Diagnostic(code=code, message=message, path=rel)]

    if not _inside(path, mirror_root):
        return problem(
            "loop-state-path-escape",
            "resolves outside the mirror root; not read",
        )
    try:
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return "absent", None, []
    except (OSError, UnicodeDecodeError) as err:
        return problem("loop-state-unreadable", f"{type(err).__name__}: {err}")
    try:
        data = json.loads(text, object_pairs_hook=_json_no_duplicates)
    except ValueError as err:  # JSONDecodeError and duplicate keys alike
        return problem("loop-state-unreadable", f"{type(err).__name__}: {err}")
    if not isinstance(data, dict):
        return problem("loop-state-unreadable", "not a JSON object")
    if proposal is None:
        # No trusted subject: schema validation and the membership check
        # are semantic classification — skipped; the caller's non-ok rule
        # owns the final loop_status.
        return "unknown", None, []
    if not _loop_state_validator().is_valid(data):
        return problem(
            "loop-state-schema-invalid",
            "does not match the vendored loop-state/v1 (incompatible "
            "shape; the file carries no version field)",
        )
    loop_pid = str(data["proposal_id"])
    bundle_pid = str(proposal["proposal_id"])
    if loop_pid != bundle_pid:
        return problem(
            "loop-state-proposal-mismatch",
            f"{rel} belongs to {loop_pid!r}, but this bundle's proposal "
            f"is {bundle_pid!r}",
        )
    stop = data["stop"]
    if stop is None:
        return "running", None, []
    assert isinstance(stop, dict)  # schema-guaranteed
    verdict = str(stop["verdict"])
    if verdict in _TERMINAL_LOOP:
        return _TERMINAL_LOOP[verdict], None, []
    wait = LoopWait(
        loop_id=str(data["loop_id"]),
        iteration=int(stop["iteration"]),  # type: ignore[arg-type]
        proposal_id=loop_pid,
        reason=str(stop["reason"]),
        stopped_at=str(stop["at"]),
        bundle_path=_relpath(bundle_dir, mirror_root) or ".",
    )
    return "needs_human", wait, []
```

8. Wire into `_load_bundle` — after the `_supersession_integrity` extension
and BEFORE the `diagnostics.sort(...)` line, add:

```python
    loop_status, loop_wait, loop_diags = _load_loop_state(
        bundle_dir, mirror_root, proposal
    )
    diagnostics.extend(loop_diags)
```

and after the `waits` computation, before the `return`, add:

```python
    loop_waits: list[LoopWait] = []
    if state == "ok":
        if loop_wait is not None:
            loop_waits.append(loop_wait)
    else:
        # Owner-fixed rule: a non-ok bundle's loop state is unclassified —
        # unconditionally, including when the file is absent.
        loop_status = "unknown"
```

and extend the `return ProposalBundle(...)` with:

```python
        loop_status=loop_status,
        loop_waits=loop_waits,
```

- [ ] **Step 4: Run the module suite**

Run: `uv run pytest tests/test_product_proposals.py -v`
Expected: ALL pass — the new loop tests AND every phase-1 test (they build
bundles without loop.state, which is now `"absent"` and diagnostic-free).

- [ ] **Step 5: Vendored-fixture validation for the third schema**

Append to `tests/test_product_proposals.py`:

```python
LS_SCHEMA_FIXTURES = (
    Path(__file__).parent.parent
    / "contracts"
    / "impresario-loop-state"
    / "v1"
    / "fixtures"
)


def test_vendored_loop_state_fixtures_split_on_the_schema() -> None:
    from dispatcher.core.product_proposals import _loop_state_validator

    for name in ("failed.json", "needs-human.json", "ready.json", "running.json"):
        data = _json.loads((LS_SCHEMA_FIXTURES / "valid" / name).read_text())
        assert _loop_state_validator().is_valid(data), name
    for name in (
        "bad-hash.json",
        "empty-reason.json",
        "extra-field.json",
        "missing-at.json",
        "unknown-verdict.json",
    ):
        data = _json.loads((LS_SCHEMA_FIXTURES / "invalid" / name).read_text())
        assert not _loop_state_validator().is_valid(data), name
```

Run: `uv run pytest tests/test_product_proposals.py -k vendored -v` — PASS.

- [ ] **Step 6: Gates + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/product_proposals.py tests/test_product_proposals.py
git commit -m "feat: loop-state classification — strict JSON, membership check, needs_human waits"
```

---

### Task 4: Core — aggregate, conflicts, determinism, read-only

**Files:**
- Modify: `dispatcher/core/product_proposals.py` (`_mark_conflicts`, `collect_product_proposals`)
- Test: `tests/test_product_proposals.py`

**Interfaces:**
- Produces: `ProductProposalsReport.needs_human` filled and sorted `(loop_id, iteration, bundle_path)`; conflict participants get `loop_status="unknown"`, `loop_waits=[]`.

- [ ] **Step 1: Failing tests**

```python
def test_needs_human_aggregates_and_sorts(tmp_path: Path) -> None:
    mirror = make_mirror(tmp_path)
    for rel, pid, loop in (
        ("pilot/b", "PP-200", "LOOP-200"),
        ("pilot/a", "PP-100", "LOOP-100"),
    ):
        make_bundle(mirror, rel=rel, proposal=proposal_yaml(pid=pid, status="approved"))
        state = _json.loads(loop_state_json(pid=pid, verdict="needs_human"))
        state["loop_id"] = loop
        (mirror / rel / "loop.state").write_text(_json.dumps(state))
    report = collect_product_proposals(mirror)
    assert [(w.loop_id, w.bundle_path) for w in report.needs_human] == [
        ("LOOP-100", "pilot/a"),
        ("LOOP-200", "pilot/b"),
    ]
    assert report.attention is False  # a plain LoopWait is expected work


def test_conflict_suppresses_loop_waits_too(tmp_path: Path) -> None:
    mirror = make_mirror(tmp_path)
    for rel in ("pilot/a", "pilot/b"):
        make_bundle(mirror, rel=rel, proposal=proposal_yaml(status="approved"))
        (mirror / rel / "loop.state").write_text(
            loop_state_json(verdict="needs_human")
        )
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.bundles] == ["conflict", "conflict"]
    assert report.needs_human == []
    assert all(b.loop_status == "unknown" for b in report.bundles)
    assert all(b.loop_waits == [] for b in report.bundles)


def test_determinism_and_read_only_hold_with_loop_state(tmp_path: Path) -> None:
    mirror = make_mirror(tmp_path)
    make_bundle(
        mirror, proposal=proposal_yaml(status="ready_for_business", version=6)
    )
    (mirror / "pilot" / "pp-101" / "loop.state").write_text(
        loop_state_json(verdict="needs_human")
    )
    before = _tree_state(mirror)
    first = collect_product_proposals(mirror)
    second = collect_product_proposals(mirror)
    assert first.model_dump_json() == second.model_dump_json()
    assert _tree_state(mirror) == before
    assert len(first.needs_human) == 1 and len(first.waits) == 1  # both kinds coexist
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_product_proposals.py -k "needs_human_aggregates or conflict_suppresses_loop or determinism_and_read_only_hold" -v`
Expected: FAIL — `needs_human` empty / conflicts leave `loop_status` intact.

- [ ] **Step 3: Implement**

In `_mark_conflicts`, next to `bundle.waits = []` add:

```python
            bundle.loop_waits = []
            bundle.loop_status = "unknown"
```

In `collect_product_proposals`, next to the `waits` aggregation add:

```python
    needs_human = sorted(
        (w for b in bundles if b.state == "ok" for w in b.loop_waits),
        key=lambda w: (w.loop_id, w.iteration, w.bundle_path),
    )
```

and extend the final `ProductProposalsReport(...)` with `needs_human=needs_human,`.
(The two early-return reports — anchors-missing and the read_api
mirror-not-detected — keep the default `needs_human=[]`.)

- [ ] **Step 4: Full module suite + gates + commit**

```bash
uv run pytest tests/test_product_proposals.py -v
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/product_proposals.py tests/test_product_proposals.py
git commit -m "feat: needs_human aggregate; conflicts and non-ok suppress loop classification"
```

---

### Task 5: Pinned PP-101 refresh @ 51e3103 + acceptance matrix #136

**Files:**
- Modify: `tests/fixtures/product_proposals/pp-101/*` (re-copied + `loop.state` added)
- Modify: `tests/test_product_proposals_acceptance.py`

**Interfaces:**
- Consumes: `collect_product_proposals` with loop fields (Task 4).

- [ ] **Step 1: Refresh the fixture wholesale from the new pin**

```bash
PIN=51e3103b5c88989a1d4a01a659d21790a92bb76b
for f in proposal.yaml decisions/gd-001.yaml decisions/gd-002.yaml loop.state; do
  git -C ../impresario show "$PIN:pilot/forconcept/pp-101/$f" \
    > "tests/fixtures/product_proposals/pp-101/$f"
done
git diff --stat tests/fixtures/product_proposals
```

Expected diff: ONLY `loop.state` is new; the three phase-1 files are
byte-identical (nothing to re-verify — git shows no change). Update
`tests/fixtures/product_proposals/pp-101/PROVENANCE.txt`: commit line →
`51e3103b5c88989a1d4a01a659d21790a92bb76b  (same as the contract pin)`,
files line gains `loop.state`, and the omitted-files note drops
`loop.state` from its list. Sanity: the fixture's `loop.state` must say
`"verdict": "ready_for_business"`, `"iteration": 2`, `"loop_id": "LOOP-101"`.

- [ ] **Step 2: Extend the acceptance module docstring and add the matrix**

Append to `tests/test_product_proposals_acceptance.py` (its `_mirror_with_pp101`
helper is reused; add `import json` to its imports):

```python
def _mutate_loop(bundle: Path, **changes: object) -> None:
    state = json.loads((bundle / "loop.state").read_text())
    stop = state["stop"]
    if changes.get("stop", "keep") is None:
        state["stop"] = None
    else:
        stop.update({k: v for k, v in changes.items() if k != "stop"})
    (bundle / "loop.state").write_text(json.dumps(state))


def test_acceptance_p2_needs_human_mutation_yields_one_wait(
    tmp_path: Path,
) -> None:
    """#136: stop.verdict -> needs_human => exactly one record with identity
    (LOOP-101, 2) and freshness from stop.at."""
    mirror, bundle = _mirror_with_pp101(tmp_path)
    _mutate_loop(bundle, verdict="needs_human", reason="ждём человека")
    report = collect_product_proposals(mirror)
    assert [b.loop_status for b in report.bundles] == ["needs_human"]
    assert [
        (w.loop_id, w.iteration, w.reason, w.stopped_at)
        for w in report.needs_human
    ] == [("LOOP-101", 2, "ждём человека", "2026-08-12T04:01:21Z")]
    assert report.attention is False


def test_acceptance_p2_true_pp101_is_terminal_with_zero_loop_waits(
    tmp_path: Path,
) -> None:
    mirror, _ = _mirror_with_pp101(tmp_path)
    report = collect_product_proposals(mirror)
    assert [b.loop_status for b in report.bundles] == ["ready_for_business"]
    assert report.needs_human == [] and report.attention is False


def test_acceptance_p2_stop_null_is_running_no_wait(tmp_path: Path) -> None:
    mirror, bundle = _mirror_with_pp101(tmp_path)
    _mutate_loop(bundle, stop=None)
    report = collect_product_proposals(mirror)
    assert [b.loop_status for b in report.bundles] == ["running"]
    assert report.needs_human == []


def test_acceptance_p2_deleted_loop_state_keeps_phase1_working(
    tmp_path: Path,
) -> None:
    mirror, bundle = _mirror_with_pp101(tmp_path)
    (bundle / "loop.state").unlink()
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.bundles] == ["ok"]
    assert [b.loop_status for b in report.bundles] == ["absent"]
    assert report.needs_human == [] and report.attention is False


def test_acceptance_p2_invalid_loop_state_suppresses_both(
    tmp_path: Path,
) -> None:
    mirror, bundle = _mirror_with_pp101(tmp_path)
    (bundle / "loop.state").write_bytes(b"\xff\xfe")
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.bundles] == ["unknown"]
    assert [b.loop_status for b in report.bundles] == ["unknown"]
    assert report.bundles[0].waits == [] and report.bundles[0].loop_waits == []
    assert report.needs_human == [] and report.attention is True


def test_acceptance_p2_mismatched_loop_state_suppresses_both(
    tmp_path: Path,
) -> None:
    mirror, bundle = _mirror_with_pp101(tmp_path)
    state = json.loads((bundle / "loop.state").read_text())
    state["proposal_id"] = "PP-999"
    (bundle / "loop.state").write_text(json.dumps(state))
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.bundles] == ["unknown"]
    codes = [d.code for d in report.bundles[0].diagnostics]
    assert codes == ["loop-state-proposal-mismatch"]
    assert report.needs_human == [] and report.bundles[0].waits == []
```

NOTE: the phase-1 acceptance test
`test_acceptance_1_ready_for_business_without_gd001_waits_for_gate_a`
asserts `attention is False`; with `loop.state` now present in the fixture
(terminal, valid) that still holds — do NOT touch phase-1 acceptance tests.
If any fails, the implementation regressed; fix the code, not the tests.

- [ ] **Step 3: Run, gates, commit**

```bash
uv run pytest tests/test_product_proposals_acceptance.py -v
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add tests/fixtures/product_proposals tests/test_product_proposals_acceptance.py
git commit -m "test: PP-101 refreshed @ 51e3103 (incl. loop.state); acceptance matrix for #136"
```

---

### Task 6: API serialized fields + live smoke update

**Files:**
- Modify: `tests/test_product_proposals_api.py`
- Modify: `tests/test_product_proposals_live_smoke.py`

**Interfaces:**
- Consumes: the additive fields serialize automatically via `response_model=ProductProposalsReport` — NO changes to `read_api.py`/`app.py` are expected; if a test fails for a missing field, the bug is in the models, not the route.

- [ ] **Step 1: Failing test extensions (owner-fixed: EVERY HTTP case asserts the serialized loop fields)**

In `tests/test_product_proposals_api.py`:

- `test_undetected_mirror_is_200_mirror_not_detected`: add
  `assert data["needs_human"] == []`.
- `test_healthy_empty_mirror_is_200_zero_bundles`: add
  `assert data["needs_human"] == []`.
- `test_anchors_lost_after_discovery_is_200_anchors_missing`: add
  `assert data["needs_human"] == []`.
- `test_partial_result_is_200_with_attention`: add
  `assert [b["loop_status"] for b in data["bundles"]] == ["absent", "unknown"]`
  and `assert data["needs_human"] == []`.
  (`pilot/pp-101` has no loop.state → `absent`; `pilot/pp-999` is
  unreadable → non-ok rule → `unknown`.)
- Add one end-to-end case:

```python
async def test_needs_human_serializes_through_the_route(tmp_path: Path) -> None:
    mirror = make_impresario(tmp_path)
    _seed_wait(mirror)
    (mirror / "pilot" / "pp-101" / "loop.state").write_text(
        json.dumps(
            {
                "loop_id": "LOOP-101",
                "idea_ref": "idea://IDEA-101",
                "idea_input_hash": "sha256:" + "f" * 64,
                "proposal_id": "PP-101",
                "exchange_log_id": "XL-101",
                "max_iterations": 3,
                "stop": {
                    "verdict": "needs_human",
                    "reason": "ждём человека",
                    "iteration": 1,
                    "at": "2026-08-12T05:00:00Z",
                },
            }
        )
    )
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/impresario/product-proposals")
    assert resp.status_code == 200
    data = resp.json()
    assert [b["loop_status"] for b in data["bundles"]] == ["needs_human"]
    assert [
        (w["loop_id"], w["iteration"], w["reason"], w["stopped_at"])
        for w in data["needs_human"]
    ] == [("LOOP-101", 1, "ждём человека", "2026-08-12T05:00:00Z")]
    # both wait kinds coexist on one bundle: Gate A + loop
    assert [w["gate_id"] for w in data["waits"]] == ["qg5_business"]
```

(add `import json` to the file's imports).

- [ ] **Step 2: Run — the new/extended tests must pass already (additive
serialization); if `loop_status`/`needs_human` are missing from JSON, fix
the MODELS (Task 3), not the route**

Run: `uv run pytest tests/test_product_proposals_api.py -v`
Expected: all PASS.

- [ ] **Step 3: Live smoke expectations**

In `tests/test_product_proposals_live_smoke.py`, extend the assertions:

```python
    assert [(b["path"], b["state"], b["status"]) for b in data["bundles"]] == [
        ("pilot/forconcept/pp-101", "ok", "approved")
    ]
    assert [b["loop_status"] for b in data["bundles"]] == ["ready_for_business"]
    assert data["waits"] == []
    assert data["needs_human"] == []
```

Also update the module docstring's expectation sentence to mention the
terminal loop status. Run:

```bash
IMPRESARIO_PINNED_DIR="$(scripts/checkout_pinned_impresario.sh --from ../impresario)" \
  uv run pytest tests/test_product_proposals_live_smoke.py -v
```

Expected: PASS (the checkout now extracts `51e3103`, whose pp-101 carries
the terminal loop.state).

- [ ] **Step 4: Gates + commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add tests/test_product_proposals_api.py tests/test_product_proposals_live_smoke.py
git commit -m "test: serialized loop fields in every HTTP case; live smoke at the new pin"
```

---

### Task 7: Panel — loop chips, needs_human table, zero-label + harness cases

**Files:**
- Modify: `dispatcher/server/static/index.html` (`renderProductProposals` only)
- Modify: `tests/web/product_proposals_harness.js`

**Interfaces:**
- Consumes: report fields `bundles[].loop_status`, `bundles[].loop_waits`, `needs_human` (Tasks 3-4). The `detail()` wiring and `ppGen` guard are UNTOUCHED.

- [ ] **Step 1: Extend the harness first (failing)**

In `tests/web/product_proposals_harness.js`:

1. Fixture updates — `OK_BUNDLE` gains `loop_status: 'absent', loop_waits: []`;
   the `report()` helper gains `needs_human: []`; the `SUPPRESSED` bundle
   gains `loop_status: 'unknown', loop_waits: []`. Add:

```javascript
const LOOP_WAIT = {
  loop_id: 'LOOP-101', iteration: 2, proposal_id: 'PP-101',
  reason: 'решить exempt-семантику', stopped_at: '2026-08-12T04:01:21Z',
  bundle_path: 'pilot/forconcept/pp-101',
};
const LOOP_WAITING = report({
  bundles: [{...OK_BUNDLE, status: 'approved', waits: [],
    loop_status: 'needs_human', loop_waits: [LOOP_WAIT]}],
  needs_human: [LOOP_WAIT],
});
const LOOP_TERMINAL = report({
  bundles: [{...OK_BUNDLE, status: 'approved', waits: [],
    loop_status: 'ready_for_business', loop_waits: []}],
});
```

2. New cases (before the `main` block):

```javascript
testCase('a needs_human wait is readable off one screen', async () => {
  const env = await boot(() => ok(LOOP_WAITING));
  await openDetail(env);
  const text = screenText(env, 'product-proposals');
  check(text.includes('LOOP-101'), `loop id on screen (got: ${text})`);
  check(text.includes('решить exempt-семантику'), 'reason on screen');
  check(text.includes('Stopped at'), 'the column is labelled «Stopped at»');
  check(text.includes('2026-08-12T04:01:21Z'), 'stop.at value on screen');
  check(text.includes('loop: needs_human'), 'the loop chip names the status');
  check(!text.includes('0 loops waiting'), 'no zero-label next to a live wait');
});

testCase('terminal loop: chip + «0 loops waiting», no table', async () => {
  const env = await boot(() => ok(LOOP_TERMINAL));
  await openDetail(env);
  const text = screenText(env, 'product-proposals');
  check(text.includes('loop: ready_for_business'), 'terminal chip visible');
  check(text.includes('0 loops waiting'), 'explicit zero-label present');
  check(!text.includes('Stopped at'), 'no needs_human table for a terminal loop');
});

testCase('loop: absent is visible, not hidden (owner-fixed)', async () => {
  const env = await boot(() => ok(WAITING));
  await openDetail(env);
  const text = screenText(env, 'product-proposals');
  check(text.includes('loop: absent'),
    'absent is an explicit read-model state and stays visible');
});

testCase('suppressed bundle shows loop: unknown, never «0 loops waiting»', async () => {
  const env = await boot(() => ok(SUPPRESSED));
  await openDetail(env);
  const text = screenText(env, 'product-proposals');
  check(text.includes('loop: unknown'), 'unknown chip on the suppressed bundle');
  check(!text.includes('0 loops waiting'),
    'suppressed loop classification must not read as zero loops waiting');
});

testCase('a hostile loop reason arrives escaped', async () => {
  const env = await boot(() => ok(WAITING));
  const hostile = report({
    bundles: [{...OK_BUNDLE, status: 'approved', waits: [],
      loop_status: 'needs_human',
      loop_waits: [{...LOOP_WAIT, reason: '<img src=x onerror=alert(1)>'}]}],
    needs_human: [{...LOOP_WAIT, reason: '<img src=x onerror=alert(1)>'}],
  });
  const out = render(env, hostile);
  check(!out.includes('<img'), 'raw markup does not survive esc()');
  check(out.includes('&lt;img'), 'the reason is still readable, escaped');
});
```

(The `WAITING` fixture — Gate A wait, no loop.state — gains
`loop_status: 'absent'` via the OK_BUNDLE update, which the third case pins.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_product_proposals_js.py -v`
Expected: FAIL — the five new cases (chips/table/labels missing).

- [ ] **Step 3: Extend `renderProductProposals` in index.html**

1. In the bundle-row template, after the `version` segment and before the
   diagnostics sub-list, add the chip (ALWAYS rendered — `loop_status` is
   never absent from the API):

```javascript
        ${" · "}${b.loop_status === "unknown" ? "❓ " : ""}loop: ${esc(String(b.loop_status))}
```

(adjust to the template's existing string-building style: the chip text is
`· loop: <status>` with the ❓ attention mark only for `unknown`; `absent`
is normal and unmarked).

2. After the gate-waits block (`waits`) and before the bundle rows, add:

```javascript
  let loops = "";
  if ((r.needs_human || []).length) {
    loops = `<table><tr><th>Loop</th><th>Iteration</th><th>Reason</th>
      <th>Stopped at</th><th>Bundle</th></tr>${
      r.needs_human.map(l => `<tr>
        <td>${esc(l.loop_id)}</td>
        <td>${esc(String(l.iteration))}</td>
        <td>${esc(l.reason)}</td>
        <td>${esc(l.stopped_at)}</td>
        <td>${esc(l.bundle_path)}</td></tr>`).join("")}</table>`;
  } else if (bundles.length && !suppressed) {
    // Parallel to «0 gates waiting»: an explicit, distinct label — and
    // silence when classification is suppressed (unknown ≠ zero).
    loops = `<p class="dim">0 loops waiting</p>`;
  }
```

and include `loops` in the return concatenation right after `waits`
(`return reportDiags + note + waits + loops + rows;`).

- [ ] **Step 4: Run all three JS suites**

Run: `uv run pytest tests/test_product_proposals_js.py tests/test_governance_js.py tests/test_task_authoring_js.py -v`
Expected: all PASS (13 harness cases in the pp suite: 8 existing + 5 new).

- [ ] **Step 5: Commit**

```bash
git add dispatcher/server/static/index.html tests/web/product_proposals_harness.js
git commit -m "feat: loop chips, needs_human table and «0 loops waiting» in the panel"
```

---

### Task 8: TODO item (created AND closed), full gates, PR

**Files:**
- Modify: `TODO.md`

- [ ] **Step 1: Add the accepted-and-done item**

In `TODO.md`, section «## Product-governance (impresario)», after the
`product-proposal-parity` item, add (tags on ONE line; predict the PR
number as max(existing)+1 via `gh pr list --state all --limit 1 --json number --jq '.[0].number'`,
note the assumption in your report — the controller verifies after
`gh pr create`):

```markdown
- [x] Read-only `needs_human` из loop-state/v1 — фаза 2 gate_waiting — PR #<N> @owner:github:andrei-shtanakov @id:product-proposal-needs-human
      Принятие inbox-issue #136 от impresario (ADR-ECO-006), продолжение
      #129 (фаза 1 — PR #132..#135). Единый re-pin всех трёх контрактов @
      `51e3103` (anti-mix по трём манифестам, checkout — трёхсторонний
      agreement). `loop.state` классифицируется fail-closed
      (absent | running | needs_human | ready_for_business | failed |
      unknown), строгий JSON, локальный membership-чек `proposal_id`;
      identity ожидания `(loop_id, stop.iteration)`, freshness `stop.at`.
      Панель: чипы loop-статусов (включая absent) + таблица needs_human +
      «0 loops waiting». Acceptance #136 на пинованной копии PP-101.
      Producer-side `LOOPSTATE_*` кросс-чеки остаются за impresario.
```

Also extend the `product-proposal-parity` item's body: its parity scope now
includes `needs_human` (one added clause: «Parity включает и
`needs_human`/loop-статусы фазы 2.»).

- [ ] **Step 2: Full verification gate**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
IMPRESARIO_PINNED_DIR="$(scripts/checkout_pinned_impresario.sh --from ../impresario)" uv run pytest -q
```

Expected: green except exactly the two known env-gated failures
(`tests/test_governance_live_smoke.py`, `tests/test_spec_runner_config_integration.py` —
external binaries, CI-installed).

- [ ] **Step 3: Commit, push, PR**

```bash
git add TODO.md
git commit -m "docs(todo): accept and close product-proposal-needs-human (inbox #136)"
git push -u origin feat/pp-needs-human
gh pr create --title "feat: needs_human from loop-state/v1 — phase 2 of #129 (inbox #136)" --body "$(cat <<'EOF'
## Summary
- atomic re-pin of ALL THREE impresario contracts @ `51e3103` (one run of the extended re-vendor script; anti-mix gate now spans three manifests). Evidence: `product-proposal/v1` and `gate-decision/v1` schema/fixture bytes are UNCHANGED between `28727ff` and `51e3103` — only PINNED/manifest metadata moved
- vendored `loop-state/v1`; `checkout_pinned_impresario.sh` extended to a three-way pin agreement (regression-tested: a lone divergent loop-state manifest fails exit-3 BEFORE any checkout)
- `core/product_proposals.py`: `_load_loop_state` — strict JSON (duplicate keys rejected), vendored-schema validation, the ONLY replicated producer check (`loop.state.proposal_id == proposal.yaml.proposal_id` → `loop-state-proposal-mismatch`); `LoopStatus` with explicit `absent`/`unknown`; non-ok bundles force `loop_status="unknown"` and suppress BOTH wait kinds
- additive API fields (`loop_status`, `loop_waits`, `needs_human`) — no route changes; every HTTP case asserts the serialized fields
- panel: `loop: <status>` chip on every bundle row (incl. `absent` — explicit read-model states are not re-hidden), needs_human table, distinct «0 loops waiting» label; 5 new harness cases
- acceptance matrix from #136 on the refreshed pinned PP-101 (now @ `51e3103`, incl. its terminal loop.state); live smoke asserts the terminal loop status at the pin

Spec: `docs/superpowers/specs/2026-08-12-product-proposal-needs-human-design.md`. Closes the TODO item `@id:product-proposal-needs-human`; issue #136 is closed by a human after the merge. Out of scope: TUI/VSCode/MCP parity (existing item), impresario's `LOOPSTATE_*` producer checks.

## Test plan
- [ ] `uv run pytest tests/test_product_proposals.py tests/test_product_proposals_acceptance.py tests/test_product_proposals_api.py tests/test_checkout_pinned_impresario.py -q`
- [ ] `uv run pytest tests/test_product_proposals_js.py tests/test_governance_js.py tests/test_task_authoring_js.py -v`
- [ ] `IMPRESARIO_PINNED_DIR="$(scripts/checkout_pinned_impresario.sh --from ../impresario)" uv run pytest -q`
- [ ] `uv run ruff check .` + `uv run pyrefly check`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Verify the predicted TODO PR number; read the Copilot review**

If the created PR's number differs from the one written into TODO.md, fix
TODO.md in a follow-up commit and push. Then read GitHub Copilot's review:
fix valid comments with new commits, answer invalid ones with reasoning;
iterate until no open comments remain. Do NOT merge — the user merges.

## Self-review notes (already applied)

- Spec coverage: pin strategy + evidence (Task 1), three-way checkout +
  regression (Task 2), read model/classification incl. every diagnostic
  code, strict JSON, membership check, untrusted-proposal interplay,
  non-ok rule (Task 3), aggregate/conflicts/determinism/read-only (Task 4),
  acceptance matrix verbatim (Task 5), serialized HTTP fields + live smoke
  (Task 6), panel chips incl. `absent`, zero-label, escaping (Task 7),
  TODO created-and-closed + evidence (Task 8).
- Type consistency: `LoopStatus`/`LoopWait`/`_load_loop_state` signatures
  match across Tasks 3-7; harness fixture fields match the pydantic models.
- Phase-1 tests are never edited to accommodate phase 2 (the only fixture
  change is additive: `loop.state` appears in the pinned PP-101).
