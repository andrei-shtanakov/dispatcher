# PP gate_waiting PR-1: vendor + collector + API — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Vendor impresario's `product-proposal/v1` + `gate-decision/v1` contracts at one pin and implement the read-only `gate_waiting` classification (`core/product_proposals.py`), the impresario discovery collector, and `GET /api/projects/{name}/product-proposals` — PR-1 of spec `docs/superpowers/specs/2026-08-12-product-proposal-gate-waiting-design.md` (inbox #129).

**Architecture:** Mirrors WS-005. `collectors/impresario.py` does content-based discovery only (two anchors); `core/product_proposals.py` owns discovery of proposal bundles, strict-YAML + jsonschema validation against the vendored copies, version-matched active-approve classification, and a lossless diagnostics model; `read_api.product_proposals` + a GET route are thin pass-throughs. Nothing is stored in the snapshot cache; nothing is ever written.

**Tech Stack:** Python 3.12+, pydantic, jsonschema, PyYAML (strict loader), pytest + anyio + httpx, bash (re-vendor + smoke-checkout scripts), GitHub Actions.

## Global Constraints

- Package management: `uv` only (`uv run pytest`, `uv run ruff format .`, `uv run ruff check .`, `pyrefly check`). Never pip.
- Line length 88; type hints everywhere; public APIs get docstrings.
- **Pin:** impresario commit `28727ff76a3983744596137706c844c95a5ad12b`; source subdirs `contracts/product-proposal/v1` (schema.json + 4 fixtures) and `contracts/gate-decision/v1` (schema.json + 6 fixtures).
- Vendored destinations: `contracts/impresario-product-proposal/v1/`, `contracts/impresario-gate-decision/v1/` (pattern `<producer>-<contract>/v<N>`).
- No `import impresario` anywhere; shipped code never resolves sibling-repo paths — `collect_product_proposals` takes `mirror_root` as an argument (CON-03). Dev tooling (re-vendor script `--from`, smoke checkout) may read the sibling's git object store; shipped runtime may not.
- Fail-closed: no read/parse/validation error path may produce `state="ok"` or an empty `waits` that reads as «nothing waits» (spec «Fail-closed invariants»).
- Two guarantees stay separate: copy-integrity = offline PR-gate test; upstream-drift = scheduled advisory workflow (red on its own machinery failing).
- Git workflow: branch `feat/pp-gate-waiting-collector` (already exists, carries the spec), PR via `gh pr create`, no direct pushes to master, no merging (the user merges).

## State classification (single source for all tasks)

Bundle `state` is derived from collected diagnostics, highest priority first:
`conflict` (global `proposal_id` conflict) → `unreadable` (any of
`proposal-unreadable`, `proposal-schema-invalid`, `proposal-path-escape`) →
`unknown` (any decision/supersedes-level diagnostic) → `ok` (no diagnostics).
Waits are computed ONLY for `ok`. Active approve = `decision == "approve"` ∧
`gate_id` match ∧ `subject.kind == "product_proposal"` ∧
`subject.ref == "proposal://<proposal_id>"` ∧
`subject.version == proposal.version` ∧ decision_id not referenced by any
other record's `supersedes`. `status ready_for_business` → Gate A
(`qg5_business`, `business_owner`); `status business_approved` → Gate B
(`qg5_committee`, `committee_chair`); all other statuses → no wait.

Sort orders: bundles by normalized relpath; waits by
`(proposal_id, gate_id, version, bundle_path)`; diagnostics by
`(path or "", code, message)`.

---

### Task 1: Vendor both impresario contracts at one pin

**Files:**
- Create: `scripts/revendor_impresario_contracts.sh`
- Create (by running it): `contracts/impresario-product-proposal/v1/*`, `contracts/impresario-gate-decision/v1/*`

**Interfaces:**
- Produces: the two vendored directories every later task validates against; `manifest.json` in each with `producer_commit == 28727ff76a3983744596137706c844c95a5ad12b`; the script later tasks test.
- Consumes: `scripts/vendor_manifest.py` (exists; parameterized via `--root`, `--contract`, `--producer-commit`).

- [ ] **Step 1: Write the re-vendor script**

Adaptation of `scripts/revendor_steward_gate_verdicts.sh` for TWO subdirs
from ONE commit: both are staged and verified first, then both are swapped
in; a failure anywhere restores every directory. Create
`scripts/revendor_impresario_contracts.sh` (mode 755):

```bash
#!/usr/bin/env bash
# Re-vendor BOTH impresario contracts at one producer commit.
#
# The pin is the ONE input. contracts/impresario-product-proposal/v1 and
# contracts/impresario-gate-decision/v1 are always re-vendored together from
# the same SHA, so the two manifests can never name different commits — the
# machine-readable anti-mix guarantee the PR-gate test asserts.
#
# Both directories are staged and fully verified before either is touched;
# the swap is same-filesystem renames with a restoring trap. A failure
# anywhere leaves both previous vendored copies exactly as they were.
#
# Usage:
#   scripts/revendor_impresario_contracts.sh <NEW_PIN> [--from <git-repo>]
#
# Default: fetch NEW_PIN from the canonical producer URL into a throwaway
# bare object store. --from: read it out of an existing local repository's
# object database instead — no working tree is read, and the report says
# the canonical remote was NOT consulted.
#
# Exit: 0 ok · 1 usage · 2 source or commit unavailable ·
#       3 provenance mismatch · 4 manifest generation or read-back ·
#       5 internal failure (working copies left as they were found)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRODUCER_URL="https://github.com/andrei-shtanakov/impresario"
# src-subdir|dst-dir|contract-name; both src depths are 3 segments, which
# --strip-components=3 below is coupled to (same trade-off as the steward
# script: a depth mismatch is fail-closed via the file-set check or tar).
CONTRACTS=(
  "contracts/product-proposal/v1|contracts/impresario-product-proposal/v1|impresario-product-proposal"
  "contracts/gate-decision/v1|contracts/impresario-gate-decision/v1|impresario-gate-decision"
)

die() { echo "revendor: $2" >&2; exit "$1"; }

NEW_PIN=""
FROM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from)
      [ $# -ge 2 ] || die 1 "--from needs a path"
      [ -z "$FROM" ] || die 1 "--from may only be given once"
      case "$2" in -*) die 1 "--from wants a path, not an option: $2" ;; esac
      FROM="$2"; shift 2 ;;
    -h | --help)
      awk 'NR>1{if (/^set -euo pipefail/) exit; print}' "${BASH_SOURCE[0]}" >&2
      exit 0 ;;
    -*) die 1 "unknown option: $1" ;;
    *)
      [ -z "$NEW_PIN" ] || die 1 "exactly one commit may be given"
      NEW_PIN="$1"; shift ;;
  esac
done

[ -n "$NEW_PIN" ] || die 1 "usage: $(basename "$0") <NEW_PIN> [--from <git-repo>]"
[[ "$NEW_PIN" =~ ^[0-9a-f]{40}$ ]] || die 1 "not a full 40-hex commit id: $NEW_PIN"

WORK="$(mktemp -d)" || die 5 "could not create a scratch directory"

cleanup() {
  local code=$? entry dst
  for entry in "${CONTRACTS[@]}"; do
    dst="$REPO_ROOT/$(cut -d'|' -f2 <<< "$entry")"
    rm -rf "$dst.staging"
    if [ -d "$dst.prev" ]; then
      [ -e "$dst" ] || mv "$dst.prev" "$dst"
      rm -rf "$dst.prev"
    fi
  done
  rm -rf "$WORK"
  exit "$code"
}
trap cleanup EXIT

if [ -n "$FROM" ]; then
  FROM="$(cd "$FROM" 2>/dev/null && pwd)" || die 2 "--from path does not exist"
  git -C "$FROM" rev-parse --git-dir > /dev/null 2>&1 ||
    die 2 "--from is not a git repository: $FROM"
  STORE="$FROM"
  PROVENANCE="local object store at $FROM"
  PROVENANCE_NOTE="availability in the canonical remote was NOT verified"
else
  STORE="$WORK/store"
  git init --quiet --bare "$STORE"
  git -C "$STORE" fetch --quiet --depth=1 "$PRODUCER_URL" "$NEW_PIN" ||
    die 2 "could not fetch $NEW_PIN from $PRODUCER_URL"
  PROVENANCE="$PRODUCER_URL"
  PROVENANCE_NOTE="the commit was served by the canonical remote"
fi

git -C "$STORE" cat-file -e "$NEW_PIN^{commit}" 2> /dev/null ||
  die 2 "$NEW_PIN is not a commit in $PROVENANCE"
command -v python3 > /dev/null 2>&1 || die 4 "python3 not found on PATH"

# Every staged file IS the commit's blob and the staged set IS the commit's
# set — in both directions.
verify_provenance() {
  local mode="$1" src_subdir="$2" staging="$3" rel want got
  git -c core.quotePath=false -C "$STORE" ls-tree -r --name-only "$NEW_PIN" \
    -- "$src_subdir" |
    sed "s|^$src_subdir/||" | LC_ALL=C sort > "$WORK/want.txt" ||
    die 3 "could not read the tree of $NEW_PIN from $STORE"
  (cd "$staging" && find . -type f | sed 's|^\./||' | LC_ALL=C sort) > "$WORK/got.txt"
  if [ "$mode" = "with-meta" ]; then
    grep -vxF -e 'PINNED.txt' -e 'manifest.json' "$WORK/got.txt" > "$WORK/got.meta" || true
    mv "$WORK/got.meta" "$WORK/got.txt"
  fi
  diff "$WORK/want.txt" "$WORK/got.txt" >&2 ||
    die 3 "the staged file set is not the file set of $NEW_PIN ($src_subdir)"
  while IFS= read -r rel; do
    want="$(git -C "$STORE" rev-parse "$NEW_PIN:$src_subdir/$rel")" ||
      die 3 "could not read the blob $NEW_PIN has at $src_subdir/$rel"
    got="$(git -C "$STORE" hash-object -- "$staging/$rel")" ||
      die 3 "could not hash staged $rel"
    [ "$want" = "$got" ] ||
      die 3 "staged $rel is not the blob $NEW_PIN has at $src_subdir/$rel"
  done < "$WORK/want.txt"
}

# Phase 1: stage + verify + meta for BOTH contracts, touching nothing live.
for entry in "${CONTRACTS[@]}"; do
  IFS='|' read -r SRC_SUBDIR DST_REL CONTRACT_NAME <<< "$entry"
  DST="$REPO_ROOT/$DST_REL"
  STAGING="$DST.staging"
  rm -rf "$STAGING"
  mkdir -p "$STAGING"
  git -C "$STORE" archive "$NEW_PIN" "$SRC_SUBDIR" |
    tar -x --strip-components=3 -C "$STAGING" ||
    die 2 "$NEW_PIN has no $SRC_SUBDIR to extract"

  verify_provenance exact "$SRC_SUBDIR" "$STAGING"

  cat > "$STAGING/PINNED.txt" << EOF
source: impresario $SRC_SUBDIR
commit: $NEW_PIN
vendored: $(date -u +%Y-%m-%d)
note: pinned copy (repo-boundaries vendoring, ADR-ECO-003). Do not edit here —
  re-vendor with scripts/revendor_impresario_contracts.sh, which re-vendors
  BOTH impresario contract directories from the one commit it is given, so
  the two manifests can never name different commits. Nothing in shipped
  code may read ../impresario at run time.
EOF

  python3 "$REPO_ROOT/scripts/vendor_manifest.py" \
    --producer-commit "$NEW_PIN" --root "$STAGING" \
    --contract "$CONTRACT_NAME" ||
    die 4 "manifest generation failed for $CONTRACT_NAME"

  if ! python3 - "$STAGING/manifest.json" "$NEW_PIN" "$STAGING" << 'PY'
import json
import pathlib
import sys

manifest_path, pin, root = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
manifest = json.load(open(manifest_path))
if manifest.get("producer_commit") != pin:
    sys.exit(1)
surface = manifest.get("surface") or []
if not surface:
    sys.exit(1)
excluded = {"PINNED.txt", "manifest.json"}
on_disk = {
    str(p.relative_to(root))
    for p in root.rglob("*")
    if p.is_file() and p.name not in excluded
}
listed = {entry["path"] for entry in surface}
sys.exit(0 if listed == on_disk else 1)
PY
  then
    die 4 "the generated manifest for $CONTRACT_NAME does not record the pin, is empty, or its surface does not match the staged files"
  fi

  verify_provenance with-meta "$SRC_SUBDIR" "$STAGING"
done

# Phase 2: both candidates verified — swap both in.
for entry in "${CONTRACTS[@]}"; do
  DST="$REPO_ROOT/$(cut -d'|' -f2 <<< "$entry")"
  if [ -e "$DST" ]; then
    mv "$DST" "$DST.prev" || die 5 "could not move $DST aside"
  fi
  mv "$DST.staging" "$DST" || die 5 "could not move the staged copy into $DST"
  rm -rf "$DST.prev"
done

cat >&2 << EOF
re-vendored both impresario contracts at $NEW_PIN
  provenance: $PROVENANCE
              $PROVENANCE_NOTE
  next:       uv run pytest tests/test_impresario_contracts_vendor.py \\
                tests/test_product_proposals.py -v
EOF
```

- [ ] **Step 2: Make it executable and run it at the pin (local object store)**

```bash
chmod +x scripts/revendor_impresario_contracts.sh
scripts/revendor_impresario_contracts.sh 28727ff76a3983744596137706c844c95a5ad12b --from ../impresario
```

Expected: `re-vendored both impresario contracts at 28727ff7…` on stderr.

- [ ] **Step 3: Verify the vendored surfaces by eye**

```bash
find contracts/impresario-product-proposal contracts/impresario-gate-decision -type f | sort
```

Expected files (plus `PINNED.txt` + `manifest.json` in each `v1/`):
`impresario-product-proposal/v1`: `schema.json`, `fixtures/valid/pp-001.yaml`,
`fixtures/invalid/status-ready-for-committee.yaml`,
`fixtures/invalid/status-recycle.yaml`, `fixtures/invalid/version-zero.yaml`.
`impresario-gate-decision/v1`: `schema.json`, `fixtures/valid/gd-approve.yaml`,
`fixtures/valid/gd-recycle.yaml`, `fixtures/valid/gd-select.yaml`,
`fixtures/invalid/agent-authority.yaml`, `fixtures/invalid/qg4-approve.yaml`,
`fixtures/invalid/recycle-without-return-to.yaml`.

- [ ] **Step 4: Commit**

```bash
git add scripts/revendor_impresario_contracts.sh contracts/impresario-product-proposal contracts/impresario-gate-decision
git commit -m "feat: vendor impresario product-proposal/v1 + gate-decision/v1 @ 28727ff (one pin, one script)"
```

---

### Task 2: Copy-integrity + re-vendor script tests

**Files:**
- Test: `tests/test_impresario_contracts_vendor.py`
- Test: `tests/test_revendor_impresario_script.py`

**Interfaces:**
- Consumes: the two vendored directories and the script from Task 1.
- Produces: the PR-gate guarantee A (offline copy-integrity + anti-mix) later tasks rely on.

- [ ] **Step 1: Write the copy-integrity test**

`tests/test_impresario_contracts_vendor.py`:

```python
"""Copy-integrity of BOTH vendored impresario contracts (guarantee A).

Offline and consumer-owned: reads only the vendored copies, never a sibling
checkout, and therefore never skips. Upstream drift is guarantee B — the
scheduled advisory workflow — and is deliberately not asserted here.

PRODUCER_COMMIT stays a hand-maintained literal on purpose — it is the
independent assertion about what the manifests should say; a test that reads
the value it checks proves nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

CONTRACTS_ROOT = Path(__file__).parent.parent / "contracts"
PRODUCER_COMMIT = "28727ff76a3983744596137706c844c95a5ad12b"
_EXCLUDED_NAMES = {"PINNED.txt", "manifest.json"}
VENDORED = {
    "impresario-product-proposal": CONTRACTS_ROOT / "impresario-product-proposal" / "v1",
    "impresario-gate-decision": CONTRACTS_ROOT / "impresario-gate-decision" / "v1",
}
EXPECTED_SURFACES = {
    "impresario-product-proposal": {
        "schema.json",
        "fixtures/valid/pp-001.yaml",
        "fixtures/invalid/status-ready-for-committee.yaml",
        "fixtures/invalid/status-recycle.yaml",
        "fixtures/invalid/version-zero.yaml",
    },
    "impresario-gate-decision": {
        "schema.json",
        "fixtures/valid/gd-approve.yaml",
        "fixtures/valid/gd-recycle.yaml",
        "fixtures/valid/gd-select.yaml",
        "fixtures/invalid/agent-authority.yaml",
        "fixtures/invalid/qg4-approve.yaml",
        "fixtures/invalid/recycle-without-return-to.yaml",
    },
}


def _manifest(root: Path) -> dict:
    return json.loads((root / "manifest.json").read_text(encoding="utf-8"))


def _on_disk(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file() and p.name not in _EXCLUDED_NAMES
    }


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_manifest_names_the_pin_and_contract(name: str) -> None:
    manifest = _manifest(VENDORED[name])
    assert manifest["producer_commit"] == PRODUCER_COMMIT
    assert manifest["contract"] == name
    assert manifest["contract_version"] == 1


def test_both_manifests_share_one_producer_commit() -> None:
    """The anti-mix guarantee: the two contracts can never silently sit at
    different upstream commits — pin and provenance are machine-readable."""
    pins = {_manifest(root)["producer_commit"] for root in VENDORED.values()}
    assert pins == {PRODUCER_COMMIT}


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_every_vendored_file_matches_its_manifest_hash(name: str) -> None:
    root = VENDORED[name]
    listed = {e["path"]: e["sha256"] for e in _manifest(root)["surface"]}
    assert listed == _on_disk(root)


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_tree_hash_is_reproducible_from_the_surface_list(name: str) -> None:
    manifest = _manifest(VENDORED[name])
    entries = sorted(manifest["surface"], key=lambda e: e["path"])
    recomputed = hashlib.sha256(
        "".join(f"{e['path']}:{e['sha256']}\n" for e in entries).encode()
    ).hexdigest()
    assert recomputed == manifest["tree_sha256"]


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_pinned_txt_names_the_same_commit(name: str) -> None:
    pinned = (VENDORED[name] / "PINNED.txt").read_text(encoding="utf-8")
    match = re.search(r"^commit: ([0-9a-f]{40})$", pinned, re.MULTILINE)
    assert match is not None
    assert match.group(1) == PRODUCER_COMMIT


@pytest.mark.parametrize("name", sorted(VENDORED))
def test_the_expected_surface_is_present(name: str) -> None:
    """The schema + upstream fixtures the collector tests stand on; a
    re-vendor that silently loses one must fail here, not there."""
    assert set(_on_disk(VENDORED[name])) == EXPECTED_SURFACES[name]
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/test_impresario_contracts_vendor.py -v`
Expected: all PASS (the vendored copy exists from Task 1).

- [ ] **Step 3: Write the re-vendor script test**

`tests/test_revendor_impresario_script.py` — same discipline as
`tests/test_revendor_steward_script.py`, plus the two-directory atomicity
this script adds:

```python
"""The impresario re-vendor script, exercised offline via --from.

Copy-specific risks asserted here: the pin/manifests/bytes land in BOTH
directories, a file upstream deleted does not survive, one bad source
subdir means NEITHER directory changes, and a failed run restores the tree.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT = REPO_ROOT / "scripts" / "revendor_impresario_contracts.sh"
DSTS = (
    "contracts/impresario-product-proposal/v1",
    "contracts/impresario-gate-decision/v1",
)
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


@pytest.fixture
def producer(tmp_path: Path) -> dict[str, object]:
    """A miniature impresario: two commits over BOTH contract subdirs.

    The second commit drops a fixture the first had — the file-deleted-
    upstream case a copy-over-the-top re-vendor gets wrong.
    """
    repo = tmp_path / "impresario"
    pp = repo / "contracts" / "product-proposal" / "v1"
    gd = repo / "contracts" / "gate-decision" / "v1"
    (pp / "fixtures").mkdir(parents=True)
    (gd / "fixtures").mkdir(parents=True)
    (pp / "schema.json").write_text('{"title": "pp"}\n')
    (pp / "fixtures" / "ok.yaml").write_text("a: 1\n")
    (pp / "fixtures" / "dropped.yaml").write_text("b: 2\n")
    (gd / "schema.json").write_text('{"title": "gd"}\n')
    (gd / "fixtures" / "ok.yaml").write_text("c: 3\n")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "one")
    first = _git(repo, "rev-parse", "HEAD")
    (pp / "fixtures" / "dropped.yaml").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "two")
    second = _git(repo, "rev-parse", "HEAD")
    return {"repo": repo, "first": first, "second": second}


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    """A copy of the repo skeleton the script needs, so the REAL vendored
    directories are never touched by this test."""
    box = tmp_path / "dispatcher"
    (box / "scripts").mkdir(parents=True)
    for name in ("revendor_impresario_contracts.sh", "vendor_manifest.py"):
        src = REPO_ROOT / "scripts" / name
        dst = box / "scripts" / name
        dst.write_bytes(src.read_bytes())
        dst.chmod(0o755)
    for rel in DSTS:
        (box / rel).mkdir(parents=True)
        (box / rel / "stale.txt").write_text("previous copy\n")
    return box


def _run(box: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(box / "scripts" / "revendor_impresario_contracts.sh"), *args],
        capture_output=True,
        text=True,
    )


def test_both_directories_land_with_the_same_pin(
    producer: dict[str, object], sandbox: Path
) -> None:
    result = _run(
        sandbox, str(producer["second"]), "--from", str(producer["repo"])
    )
    assert result.returncode == 0, result.stderr
    pins = set()
    for rel in DSTS:
        manifest = json.loads((sandbox / rel / "manifest.json").read_text())
        pins.add(manifest["producer_commit"])
        assert not (sandbox / rel / "stale.txt").exists()
    assert pins == {producer["second"]}


def test_upstream_deleted_file_does_not_survive(
    producer: dict[str, object], sandbox: Path
) -> None:
    assert _run(
        sandbox, str(producer["first"]), "--from", str(producer["repo"])
    ).returncode == 0
    assert _run(
        sandbox, str(producer["second"]), "--from", str(producer["repo"])
    ).returncode == 0
    pp = sandbox / DSTS[0]
    assert not (pp / "fixtures" / "dropped.yaml").exists()


def test_failure_restores_both_previous_copies(
    producer: dict[str, object], sandbox: Path, tmp_path: Path
) -> None:
    """A commit that has product-proposal/v1 but NO gate-decision/v1: the
    second extraction fails, and neither live directory may have changed."""
    repo = tmp_path / "half"
    pp = repo / "contracts" / "product-proposal" / "v1"
    pp.mkdir(parents=True)
    (pp / "schema.json").write_text("{}\n")
    _git(repo, "init", "--quiet")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "half")
    half = _git(repo, "rev-parse", "HEAD")
    result = _run(sandbox, half, "--from", str(repo))
    assert result.returncode != 0
    for rel in DSTS:
        assert (sandbox / rel / "stale.txt").read_text() == "previous copy\n"
        assert not (sandbox / rel).with_suffix(".staging").exists()


def test_rejects_a_non_full_sha(sandbox: Path) -> None:
    result = _run(sandbox, "main")
    assert result.returncode == 1
```

- [ ] **Step 4: Run it**

Run: `uv run pytest tests/test_revendor_impresario_script.py -v`
Expected: all PASS.

- [ ] **Step 5: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . && pyrefly check
git add tests/test_impresario_contracts_vendor.py tests/test_revendor_impresario_script.py
git commit -m "test: copy-integrity + anti-mix + re-vendor script for impresario contracts"
```

---

### Task 3: Scheduled upstream-drift job for both contracts

**Files:**
- Modify: `.github/workflows/upstream-drift.yml` (append a job)

**Interfaces:**
- Consumes: `scripts/upstream_drift_report.py` (exists; args: `canon_dir --vendored <dir> --upstream-root <dir> --ref <ref>`; exit 0 no drift · 1 drift · 2 canon unavailable).

- [ ] **Step 1: Dry-run the generic reporter against the local sibling**

```bash
uv run python scripts/upstream_drift_report.py \
  ../impresario/contracts/product-proposal/v1 \
  --vendored contracts/impresario-product-proposal/v1 \
  --upstream-root ../impresario --ref local-dry-run
uv run python scripts/upstream_drift_report.py \
  ../impresario/contracts/gate-decision/v1 \
  --vendored contracts/impresario-gate-decision/v1 \
  --upstream-root ../impresario --ref local-dry-run
```

Expected: both exit 0 (no drift — we vendored at the sibling's HEAD). If the
reporter rejects the directories for a structural reason, STOP and re-read
its module docstring — do not fork a new reporter without need.

- [ ] **Step 2: Append the job to `.github/workflows/upstream-drift.yml`**

After the last existing job, same indentation as its siblings:

```yaml
  # Both impresario contracts, one job: they share a single pin by
  # construction (one re-vendor script), so one checkout answers for both.
  drift-impresario-contracts:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      # The MOVING default branch on purpose — drift can only be seen against
      # where upstream actually is now; the resolved SHA is in the run log.
      - uses: actions/checkout@v6
        with:
          repository: andrei-shtanakov/impresario
          ref: master
          path: _upstream/impresario
      - uses: astral-sh/setup-uv@v8.1.0
      - run: uv sync
      - name: compare canon against both vendored copies
        run: |
          overall=0
          for pair in \
            "contracts/product-proposal/v1|contracts/impresario-product-proposal/v1" \
            "contracts/gate-decision/v1|contracts/impresario-gate-decision/v1"; do
            src="${pair%%|*}"; dst="${pair##*|}"
            set +e
            uv run python scripts/upstream_drift_report.py \
              "_upstream/impresario/$src" \
              --vendored "$dst" \
              --upstream-root _upstream/impresario \
              --ref master
            code=$?
            set -e
            # 0 no drift · 1 drift · 2 canon unavailable. Unavailable and
            # any machinery failure are red too: unknown must not look green.
            case "$code" in
              0) echo "no upstream drift: $dst" ;;
              1) echo "::error::upstream drift in $dst — a deliberate re-vendor PR is due" ;;
              2) echo "::error::canon unavailable for $src — nothing was compared (not 'no drift')" ;;
              *) echo "::error::reporter failed with $code for $dst" ;;
            esac
            [ "$code" -gt "$overall" ] && overall=$code
          done
          exit "$overall"
```

- [ ] **Step 3: Validate the workflow syntax and commit**

```bash
uv run python -c "import yaml, pathlib; yaml.safe_load(pathlib.Path('.github/workflows/upstream-drift.yml').read_text())"
git add .github/workflows/upstream-drift.yml
git commit -m "ci: scheduled upstream-drift job for both impresario contracts"
```

---

### Task 4: Core module — models, strict YAML, schema validators

**Files:**
- Create: `dispatcher/core/product_proposals.py`
- Test: `tests/test_product_proposals.py`

**Interfaces:**
- Produces (used by every later task):
  - `Diagnostic(code: str, message: str, path: str | None = None)`
  - `GateWait(proposal_id, gate_id, gate_label, authority, artifact_ref, bundle_path, version, proposal_updated_at)`
  - `ProposalBundle(path, state, diagnostics, proposal_id, status, version, updated_at, waits)`
  - `ProductProposalsReport(mirror_path, bundles, waits, diagnostics, attention)`
  - `ANCHOR_FILES: tuple[str, str]`
  - `collect_product_proposals(mirror_root: Path) -> ProductProposalsReport` (stub in this task; full behaviour lands in Tasks 5–7)
  - internal `_strict_load(text: str) -> object` (raises `yaml.YAMLError` on duplicate keys)

- [ ] **Step 1: Write the failing tests (strict YAML + models + vendored-fixture validation)**

`tests/test_product_proposals.py`:

```python
"""core/product_proposals.py — read-only gate_waiting classification.

Spec: docs/superpowers/specs/2026-08-12-product-proposal-gate-waiting-design.md
(inbox #129). Fixtures come from the VENDORED contract copies and from
synthetic bundles built in tmp_path — never from ../impresario (CON-03).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from dispatcher.core.product_proposals import (
    ANCHOR_FILES,
    Diagnostic,
    GateWait,
    ProductProposalsReport,
    ProposalBundle,
    _strict_load,
    collect_product_proposals,
)

PP_SCHEMA_FIXTURES = (
    Path(__file__).parent.parent
    / "contracts"
    / "impresario-product-proposal"
    / "v1"
    / "fixtures"
)
GD_SCHEMA_FIXTURES = (
    Path(__file__).parent.parent
    / "contracts"
    / "impresario-gate-decision"
    / "v1"
    / "fixtures"
)


def test_strict_load_rejects_duplicate_mapping_keys() -> None:
    """yaml.safe_load keeps the last duplicate silently — fail-closed for
    decision_id / subject.version / gate_id / status requires rejection."""
    with pytest.raises(yaml.YAMLError, match="duplicate"):
        _strict_load("status: approved\nstatus: draft\n")


def test_strict_load_rejects_nested_duplicate_keys() -> None:
    with pytest.raises(yaml.YAMLError, match="duplicate"):
        _strict_load("subject:\n  version: 1\n  version: 2\n")


def test_strict_load_accepts_plain_mappings() -> None:
    assert _strict_load("a: 1\nb:\n  c: 2\n") == {"a": 1, "b": {"c": 2}}


def test_anchor_files_are_the_two_impresario_markers() -> None:
    assert ANCHOR_FILES == (
        "contracts/product-proposal/v1/schema.json",
        "docs/semantics.md",
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_product_proposals.py -v`
Expected: FAIL — `ModuleNotFoundError: dispatcher.core.product_proposals`.

- [ ] **Step 3: Write the module skeleton**

`dispatcher/core/product_proposals.py`:

```python
"""Product-proposal gate_waiting collector: classify impresario bundles.

Inbox #129 phase 1 (spec: docs/superpowers/specs/
2026-08-12-product-proposal-gate-waiting-design.md). Reads proposal bundles
(`proposal.yaml` + `decisions/*.yaml`) out of the impresario mirror and says
which product decisions are waiting for a human — Gate A (`qg5_business`,
business_owner) and Gate B (`qg5_committee`, committee_chair).

Constraints this module lives under:

- Classification only (ARCH-C3/D1): impresario is never imported, its CLI is
  never executed, and no governance model is built here — status + decision
  records are read and rendered.
- CON-03: no sibling-repo path is resolved; the mirror root is an argument.
- Fail-closed: an unreadable/invalid proposal or decision is never rendered
  as «nothing waits». Every found `proposal.yaml` yields a bundle row; waits
  are computed only for `ok` bundles, and `waits: []` on a non-ok bundle
  means «suppressed». Duplicate YAML keys are rejected (plain safe_load
  keeps the last value silently).
- Version-matched activeness: an approve extinguishes the current wait only
  when it targets the proposal's CURRENT version and is not superseded.
  After a recycle the old approve is history, not permission; an approve
  recorded before the status update already extinguishes the wait.
- Read-only: nothing under the mirror is ever created or modified.
"""

from __future__ import annotations

import functools
import json
import os
from pathlib import Path
from typing import Literal

import jsonschema
import yaml
from pydantic import BaseModel, Field

_CONTRACTS = Path(__file__).resolve().parents[2] / "contracts"
_PROPOSAL_SCHEMA = _CONTRACTS / "impresario-product-proposal" / "v1" / "schema.json"
_DECISION_SCHEMA = _CONTRACTS / "impresario-gate-decision" / "v1" / "schema.json"

ANCHOR_FILES = (
    "contracts/product-proposal/v1/schema.json",
    "docs/semantics.md",
)

GateId = Literal["qg5_business", "qg5_committee"]
BundleState = Literal["ok", "unreadable", "unknown", "conflict"]

_STATUS_GATE: dict[str, GateId] = {
    "ready_for_business": "qg5_business",
    "business_approved": "qg5_committee",
}
_GATE_LABEL = {"qg5_business": "Gate A", "qg5_committee": "Gate B"}
_GATE_AUTHORITY = {
    "qg5_business": "business_owner",
    "qg5_committee": "committee_chair",
}
# Diagnostics whose presence makes the PROPOSAL untrusted (state unreadable);
# any other bundle-level diagnostic is decision-grade (state unknown).
_UNREADABLE_CODES = {
    "proposal-unreadable",
    "proposal-schema-invalid",
    "proposal-path-escape",
}


class Diagnostic(BaseModel):
    """One structured problem; `code` is the stable API contract."""

    code: str
    message: str
    path: str | None = None


class GateWait(BaseModel):
    """One «a human is being waited for» record."""

    proposal_id: str
    gate_id: GateId
    gate_label: str
    authority: str
    artifact_ref: str
    bundle_path: str
    version: int
    # proposal.updated_at — when the proposal last changed, NOT a proven
    # wait-start time; UI labels it «Proposal updated».
    proposal_updated_at: str


class ProposalBundle(BaseModel):
    """Every discovered bundle, lossless: non-ok rows keep ALL diagnostics."""

    path: str
    state: BundleState
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    proposal_id: str | None = None
    status: str | None = None
    version: int | None = None
    updated_at: str | None = None
    # Computed ONLY for state == "ok". On any other state an empty list
    # means «suppressed», never «nothing waits».
    waits: list[GateWait] = Field(default_factory=list)


class ProductProposalsReport(BaseModel):
    """The read model of one scan of the impresario mirror."""

    mirror_path: str
    bundles: list[ProposalBundle] = Field(default_factory=list)
    waits: list[GateWait] = Field(default_factory=list)
    diagnostics: list[Diagnostic] = Field(default_factory=list)
    # Any non-ok bundle or report-level diagnostic. A plain GateWait does
    # NOT raise attention — waiting is expected business work.
    attention: bool = False


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys (fail-closed)."""


def _mapping_no_duplicates(
    loader: _StrictLoader, node: yaml.MappingNode
) -> dict[object, object]:
    seen: set[str] = set()
    for key_node, _value_node in node.value:
        key = repr(loader.construct_object(key_node, deep=True))
        if key in seen:
            raise yaml.YAMLError(f"duplicate mapping key {key}")
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping_no_duplicates
)


def _strict_load(text: str) -> object:
    """Parse YAML, rejecting duplicate mapping keys at any depth."""
    return yaml.load(text, Loader=_StrictLoader)  # noqa: S506 — SafeLoader subclass


@functools.cache
def _proposal_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_PROPOSAL_SCHEMA.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


@functools.cache
def _decision_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_DECISION_SCHEMA.read_text(encoding="utf-8"))
    return jsonschema.Draft202012Validator(schema)


def collect_product_proposals(mirror_root: Path) -> ProductProposalsReport:
    """Scan the impresario mirror and classify every proposal bundle.

    Filled in by Tasks 5–7 of the implementation plan; this stub keeps the
    module importable while the pieces land test-first.
    """
    raise NotImplementedError
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_product_proposals.py -v`
Expected: 4 PASS.

- [ ] **Step 5: Add vendored-fixture validation tests (they pass immediately — they pin the validators to the vendored schemas)**

Append to `tests/test_product_proposals.py`:

```python
def test_vendored_valid_fixtures_pass_their_schemas() -> None:
    from dispatcher.core.product_proposals import (
        _decision_validator,
        _proposal_validator,
    )

    pp = _strict_load((PP_SCHEMA_FIXTURES / "valid" / "pp-001.yaml").read_text())
    assert _proposal_validator().is_valid(pp)
    for name in ("gd-approve.yaml", "gd-recycle.yaml", "gd-select.yaml"):
        gd = _strict_load((GD_SCHEMA_FIXTURES / "valid" / name).read_text())
        assert _decision_validator().is_valid(gd), name


def test_vendored_invalid_fixtures_fail_their_schemas() -> None:
    from dispatcher.core.product_proposals import (
        _decision_validator,
        _proposal_validator,
    )

    for name in (
        "status-ready-for-committee.yaml",
        "status-recycle.yaml",
        "version-zero.yaml",
    ):
        pp = _strict_load((PP_SCHEMA_FIXTURES / "invalid" / name).read_text())
        assert not _proposal_validator().is_valid(pp), name
    for name in (
        "agent-authority.yaml",
        "qg4-approve.yaml",
        "recycle-without-return-to.yaml",
    ):
        gd = _strict_load((GD_SCHEMA_FIXTURES / "invalid" / name).read_text())
        assert not _decision_validator().is_valid(gd), name
```

Run: `uv run pytest tests/test_product_proposals.py -v` — all PASS.

- [ ] **Step 6: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . && pyrefly check
git add dispatcher/core/product_proposals.py tests/test_product_proposals.py
git commit -m "feat: product_proposals read model, strict YAML loader, vendored-schema validators"
```

---

### Task 5: Core module — bundle discovery

**Files:**
- Modify: `dispatcher/core/product_proposals.py`
- Test: `tests/test_product_proposals.py`

**Interfaces:**
- Produces: `_discover(mirror_root: Path) -> tuple[list[Path], list[Diagnostic]]` — sorted bundle dirs + report-level `walk-error` diagnostics; `_inside(path: Path, root: Path) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_product_proposals.py`:

```python
def _mk(root: Path, rel: str, text: str = "x: 1\n") -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_discover_finds_bundles_sorted_and_excludes_segments(
    tmp_path: Path,
) -> None:
    from dispatcher.core.product_proposals import _discover

    _mk(tmp_path, "pilot/b/pp-2/proposal.yaml")
    _mk(tmp_path, "pilot/a/pp-1/proposal.yaml")
    _mk(tmp_path, "contracts/examples/pp-0/proposal.yaml")  # excluded: contracts
    _mk(tmp_path, "_drafts/pp-3/proposal.yaml")  # excluded: _ prefix
    _mk(tmp_path, "pilot/.hidden/pp-4/proposal.yaml")  # excluded: . prefix
    _mk(tmp_path, "deep/nested/contracts/pp-5/proposal.yaml")  # excluded: any segment
    bundles, diags = _discover(tmp_path)
    assert diags == []
    rels = [b.relative_to(tmp_path).as_posix() for b in bundles]
    assert rels == ["pilot/a/pp-1", "pilot/b/pp-2"]


def test_discover_does_not_follow_directory_symlinks(tmp_path: Path) -> None:
    from dispatcher.core.product_proposals import _discover

    outside = tmp_path / "outside"
    _mk(outside, "pp-x/proposal.yaml")
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    (mirror / "linked").symlink_to(outside, target_is_directory=True)
    bundles, diags = _discover(mirror)
    assert bundles == [] and diags == []


def test_walk_error_is_a_report_diagnostic_not_zero_bundles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """os.walk swallows enumeration errors unless onerror is passed — the
    diagnostic proves the callback is wired."""
    from dispatcher.core import product_proposals as pp

    real_walk = os.walk

    def failing_walk(top, **kwargs):  # type: ignore[no-untyped-def]
        onerror = kwargs.get("onerror")
        assert onerror is not None, "walk must pass onerror (spec: fail-loud)"
        onerror(OSError(13, "Permission denied", str(Path(top) / "locked")))
        return real_walk(top, **kwargs)

    monkeypatch.setattr(pp.os, "walk", failing_walk)
    bundles, diags = pp._discover(tmp_path)
    assert [d.code for d in diags] == ["walk-error"]
    assert diags[0].path == "locked"


import os  # noqa: E402 — used by the walk-error test above
```

(Put the `import os` with the other imports at the top of the test file —
the trailing line here only marks that the test needs it.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_product_proposals.py -k discover -v`
Expected: FAIL — `_discover` does not exist.

- [ ] **Step 3: Implement discovery**

Add to `dispatcher/core/product_proposals.py` (below the validators):

```python
def _relpath(path: Path, mirror_root: Path) -> str:
    try:
        return path.relative_to(mirror_root).as_posix()
    except ValueError:
        return path.as_posix()


def _inside(path: Path, root: Path) -> bool:
    """True when `path` still resolves inside `root` (symlink escape guard)."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _excluded(name: str) -> bool:
    return name.startswith((".", "_")) or name == "contracts"


def _discover(mirror_root: Path) -> tuple[list[Path], list[Diagnostic]]:
    """Find proposal-bundle roots: every directory holding a proposal.yaml.

    Exclusions apply to ANY path segment; directory symlinks are pruned; an
    enumeration failure becomes a walk-error diagnostic via onerror — the
    default os.walk behaviour is to swallow it, which would read as
    «0 bundles» (spec: fail-loud).
    """
    diagnostics: list[Diagnostic] = []

    def onerror(err: OSError) -> None:
        target = getattr(err, "filename", None) or str(mirror_root)
        diagnostics.append(
            Diagnostic(
                code="walk-error",
                message=f"{type(err).__name__}: {err}",
                path=_relpath(Path(target), mirror_root),
            )
        )

    bundles: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(
        mirror_root, topdown=True, onerror=onerror, followlinks=False
    ):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d
            for d in dirnames
            if not _excluded(d) and not (here / d).is_symlink()
        )
        if "proposal.yaml" in filenames:
            bundles.append(here)
    bundles.sort(key=lambda p: _relpath(p, mirror_root))
    return bundles, diagnostics
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_product_proposals.py -v`
Expected: all PASS.

- [ ] **Step 5: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . && pyrefly check
git add dispatcher/core/product_proposals.py tests/test_product_proposals.py
git commit -m "feat: deterministic bundle discovery with fail-loud walk"
```

---

### Task 6: Core module — bundle loading, supersession, wait computation

**Files:**
- Modify: `dispatcher/core/product_proposals.py`
- Test: `tests/test_product_proposals.py`

**Interfaces:**
- Produces: `_load_bundle(mirror_root: Path, bundle_dir: Path) -> ProposalBundle` — a fully classified bundle row (state, diagnostics, waits) except the global conflict pass.
- Consumes: `_discover`, `_inside`, `_strict_load`, validators (Tasks 4–5).

- [ ] **Step 1: Add test helpers for schema-valid synthetic bundles**

Append to `tests/test_product_proposals.py`:

```python
def proposal_yaml(
    pid: str = "PP-101",
    version: int = 8,
    status: str = "approved",
    updated: str = "2026-08-12T04:12:30Z",
) -> str:
    return (
        f"proposal_id: {pid}\n"
        "idea_ref: idea://IDEA-101\n"
        f"version: {version}\n"
        f"status: {status}\n"
        "iteration: 2\n"
        "refs:\n"
        "  exchange_log: exchange-log://XL-101\n"
        "created_at: '2026-08-12T02:08:53Z'\n"
        f"updated_at: '{updated}'\n"
    )


def decision_yaml(
    did: str = "GD-001",
    gate: str = "qg5_business",
    version: int = 8,
    decision: str = "approve",
    ref: str = "proposal://PP-101",
    supersedes: str | None = None,
) -> str:
    text = (
        f"decision_id: {did}\n"
        f"gate_id: {gate}\n"
        "subject:\n"
        "  kind: product_proposal\n"
        f"  ref: {ref}\n"
        f"  version: {version}\n"
        f"decision: {decision}\n"
        "decided_by:\n"
        "  kind: human\n"
        "  id: andrei\n"
        "  role: business_owner\n"
        "decided_at: '2026-08-12T04:09:21Z'\n"
        "reason: test\n"
    )
    if decision == "recycle":
        text += "return_to: in_iteration\nrequired_changes:\n- fix\n"
    if supersedes is not None:
        text += f"supersedes: gate-decision://{supersedes}\n"
    return text


def make_bundle(
    root: Path,
    rel: str = "pilot/pp-101",
    proposal: str | None = None,
    decisions: dict[str, str] | None = None,
) -> Path:
    bundle = root / rel
    (bundle / "decisions").mkdir(parents=True, exist_ok=True)
    _mk(root, f"{rel}/proposal.yaml", proposal or proposal_yaml())
    for name, text in (decisions or {}).items():
        _mk(root, f"{rel}/decisions/{name}", text)
    return bundle
```

- [ ] **Step 2: Write the failing classification tests**

Append:

```python
def _bundle(root: Path, rel: str = "pilot/pp-101") -> "ProposalBundle":
    from dispatcher.core.product_proposals import _load_bundle

    return _load_bundle(root, root / rel)


def test_ready_for_business_with_no_decisions_waits_for_gate_a(
    tmp_path: Path,
) -> None:
    make_bundle(
        tmp_path, proposal=proposal_yaml(status="ready_for_business", version=6)
    )
    b = _bundle(tmp_path)
    assert b.state == "ok"
    assert [
        (w.gate_id, w.gate_label, w.authority, w.artifact_ref, w.version)
        for w in b.waits
    ] == [("qg5_business", "Gate A", "business_owner", "proposal://PP-101", 6)]
    assert b.waits[0].proposal_updated_at == "2026-08-12T04:12:30Z"
    assert b.waits[0].bundle_path == "pilot/pp-101"


def test_business_approved_without_committee_approve_waits_for_gate_b(
    tmp_path: Path,
) -> None:
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="business_approved", version=7),
        decisions={"gd-001.yaml": decision_yaml(version=6)},
    )
    b = _bundle(tmp_path)
    assert b.state == "ok"
    assert [(w.gate_id, w.authority) for w in b.waits] == [
        ("qg5_committee", "committee_chair")
    ]


def test_terminal_and_iteration_statuses_have_no_wait(tmp_path: Path) -> None:
    for status in ("draft", "in_iteration", "approved", "on_hold", "killed"):
        make_bundle(
            tmp_path, rel=f"p/{status}", proposal=proposal_yaml(status=status)
        )
        b = _bundle(tmp_path, rel=f"p/{status}")
        assert b.state == "ok" and b.waits == []


def test_regression_recycle_old_approve_does_not_extinguish_new_wait(
    tmp_path: Path,
) -> None:
    """Pinned semantics: after recycle the un-superseded old approve (v6) is
    history, not permission — the v8 Gate A wait IS shown."""
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={"gd-001.yaml": decision_yaml(version=6)},
    )
    b = _bundle(tmp_path)
    assert b.state == "ok"
    assert [(w.gate_id, w.version) for w in b.waits] == [("qg5_business", 8)]


def test_regression_version_matched_approve_extinguishes_before_status_update(
    tmp_path: Path,
) -> None:
    """Pinned semantics (torn write): a version-matched approve already
    recorded extinguishes the wait even though status has not caught up."""
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={"gd-001.yaml": decision_yaml(version=8)},
    )
    b = _bundle(tmp_path)
    assert b.state == "ok" and b.waits == []


def test_superseded_version_matched_approve_does_not_extinguish(
    tmp_path: Path,
) -> None:
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={
            "gd-001.yaml": decision_yaml(did="GD-001", version=8),
            "gd-002.yaml": decision_yaml(
                did="GD-002", version=8, decision="recycle", supersedes="GD-001"
            ),
        },
    )
    b = _bundle(tmp_path)
    assert b.state == "ok"
    assert [w.gate_id for w in b.waits] == ["qg5_business"]


def test_other_gate_ref_or_kind_is_history_not_permission(tmp_path: Path) -> None:
    """A decision for another subject.ref or gate_id never touches the wait."""
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={
            "gd-001.yaml": decision_yaml(version=8, ref="proposal://PP-999"),
            "gd-002.yaml": decision_yaml(
                did="GD-002", gate="qg5_committee", version=8
            ),
        },
    )
    b = _bundle(tmp_path)
    assert [w.gate_id for w in b.waits] == ["qg5_business"]


def test_proposal_schema_invalid_is_unreadable(tmp_path: Path) -> None:
    make_bundle(tmp_path, proposal="proposal_id: PP-101\n")  # misses required
    b = _bundle(tmp_path)
    assert b.state == "unreadable"
    assert [d.code for d in b.diagnostics] == ["proposal-schema-invalid"]
    assert b.waits == [] and b.proposal_id is None


def test_proposal_not_utf8_is_unreadable(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    (bundle / "proposal.yaml").write_bytes(b"\xff\xfe broken")
    b = _bundle(tmp_path)
    assert b.state == "unreadable"
    assert [d.code for d in b.diagnostics] == ["proposal-unreadable"]


def test_proposal_oserror_is_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The I/O-error branch, patched instead of chmod (unstable in CI)."""
    make_bundle(tmp_path)
    real = Path.read_bytes

    def failing(self: Path) -> bytes:
        if self.name == "proposal.yaml":
            raise OSError(5, "Input/output error")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", failing)
    b = _bundle(tmp_path)
    assert b.state == "unreadable"
    assert [d.code for d in b.diagnostics] == ["proposal-unreadable"]


def test_decision_oserror_is_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The decision I/O-error branch — patched, not chmod (spec «Testing»:
    unreadability is invalid UTF-8; the OSError branch gets its own test)."""
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={"gd-001.yaml": decision_yaml(version=8)},
    )
    real = Path.read_bytes

    def failing(self: Path) -> bytes:
        if self.name == "gd-001.yaml":
            raise OSError(5, "Input/output error")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", failing)
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["decision-unreadable"]
    assert b.waits == []


def test_invalid_decision_makes_bundle_unknown_not_clean(tmp_path: Path) -> None:
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="approved"),
        decisions={"gd-001.yaml": "decision_id: GD-001\n"},  # schema-invalid
    )
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["decision-schema-invalid"]
    assert b.waits == []
    assert b.proposal_id == "PP-101"  # proposal fields stay filled


def test_all_decision_errors_are_collected_not_just_the_first(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(
        tmp_path,
        decisions={
            "a.yaml": "decision_id: GD-001\n",  # schema-invalid
            "b.yaml": "x: 1\nx: 2\n",  # duplicate keys -> unreadable
        },
    )
    (bundle / "decisions" / "c.yaml").write_bytes(b"\xff\xfe")  # not UTF-8
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert sorted(d.code for d in b.diagnostics) == [
        "decision-schema-invalid",
        "decision-unreadable",
        "decision-unreadable",
    ]


def test_unparseable_proposal_still_collects_decision_read_errors(
    tmp_path: Path,
) -> None:
    """No trusted subject -> no semantic classification of decisions, but
    their READ errors are still collected; the schema-invalid decision is
    deliberately NOT reported (that is semantic classification)."""
    bundle = make_bundle(
        tmp_path,
        proposal="status: [broken\n",
        decisions={"a.yaml": "decision_id: GD-001\n"},
    )
    (bundle / "decisions" / "b.yaml").write_bytes(b"\xff\xfe")
    b = _bundle(tmp_path)
    assert b.state == "unreadable"
    assert sorted(d.code for d in b.diagnostics) == [
        "decision-unreadable",
        "proposal-unreadable",
    ]


def test_duplicate_decision_id_is_unknown(tmp_path: Path) -> None:
    make_bundle(
        tmp_path,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={
            "a.yaml": decision_yaml(did="GD-001", version=8),
            "b.yaml": decision_yaml(did="GD-001", version=8, gate="qg5_committee"),
        },
    )
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["decision-id-duplicate"]
    assert b.waits == []


def test_dangling_supersedes_is_unknown(tmp_path: Path) -> None:
    make_bundle(
        tmp_path,
        decisions={
            "a.yaml": decision_yaml(did="GD-002", version=8, supersedes="GD-777")
        },
    )
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["supersedes-dangling"]


def test_supersedes_cycle_is_unknown(tmp_path: Path) -> None:
    make_bundle(
        tmp_path,
        decisions={
            "a.yaml": decision_yaml(did="GD-001", version=8, supersedes="GD-002"),
            "b.yaml": decision_yaml(did="GD-002", version=8, supersedes="GD-001"),
        },
    )
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert "supersedes-cycle" in {d.code for d in b.diagnostics}


def test_self_supersede_is_unknown(tmp_path: Path) -> None:
    make_bundle(
        tmp_path,
        decisions={
            "a.yaml": decision_yaml(did="GD-001", version=8, supersedes="GD-001")
        },
    )
    b = _bundle(tmp_path)
    assert b.state == "unknown"
    assert "supersedes-cycle" in {d.code for d in b.diagnostics}


def test_decision_symlink_escape_is_unknown(tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text(decision_yaml(version=8))
    mirror = tmp_path / "mirror"
    bundle = make_bundle(
        mirror, proposal=proposal_yaml(status="ready_for_business", version=8)
    )
    (bundle / "decisions" / "gd-x.yaml").symlink_to(outside)
    b = _bundle(mirror)
    assert b.state == "unknown"
    assert [d.code for d in b.diagnostics] == ["decision-path-escape"]
    assert b.waits == []


def test_proposal_symlink_escape_is_unreadable(tmp_path: Path) -> None:
    outside = tmp_path / "outside.yaml"
    outside.write_text(proposal_yaml())
    mirror = tmp_path / "mirror"
    (mirror / "pilot" / "pp-101").mkdir(parents=True)
    (mirror / "pilot" / "pp-101" / "proposal.yaml").symlink_to(outside)
    b = _bundle(mirror)
    assert b.state == "unreadable"
    assert [d.code for d in b.diagnostics] == ["proposal-path-escape"]


def test_in_mirror_file_symlink_stays_readable(tmp_path: Path) -> None:
    """The rule is escape, not symlink-ness: a link resolving inside the
    mirror is fine."""
    mirror = tmp_path / "mirror"
    bundle = make_bundle(
        mirror,
        proposal=proposal_yaml(status="ready_for_business", version=8),
    )
    real = bundle / "decisions" / "real-gd.txt"
    real.write_text(decision_yaml(version=8))
    (bundle / "decisions" / "gd-001.yaml").symlink_to(real)
    b = _bundle(mirror)
    assert b.state == "ok" and b.waits == []


def test_missing_decisions_dir_is_a_valid_bundle(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    _mk(
        mirror,
        "pilot/pp-101/proposal.yaml",
        proposal_yaml(status="ready_for_business", version=6),
    )
    b = _bundle(mirror)
    assert b.state == "ok"
    assert [w.gate_id for w in b.waits] == ["qg5_business"]
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_product_proposals.py -v`
Expected: the new tests FAIL — `_load_bundle` does not exist.

- [ ] **Step 4: Implement bundle loading + classification**

Add to `dispatcher/core/product_proposals.py`:

```python
def _load_yaml_file(
    path: Path, mirror_root: Path, code_prefix: str
) -> tuple[object | None, Diagnostic | None]:
    """Read one YAML file fail-closed: escape guard, UTF-8, strict parse."""
    rel = _relpath(path, mirror_root)
    if not _inside(path, mirror_root):
        return None, Diagnostic(
            code=f"{code_prefix}-path-escape",
            message="resolves outside the mirror root; not read",
            path=rel,
        )
    try:
        data = _strict_load(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as err:
        return None, Diagnostic(
            code=f"{code_prefix}-unreadable",
            message=f"{type(err).__name__}: {err}",
            path=rel,
        )
    return data, None


def _load_decisions(
    bundle_dir: Path, mirror_root: Path, *, classify: bool
) -> tuple[list[dict[str, object]], list[Diagnostic]]:
    """Read decisions/*.yaml, collecting ALL problems (never just the first).

    classify=False (untrusted proposal): read/parse errors are still
    collected, but schema validation — semantic classification — is skipped.
    An absent decisions/ directory is a valid bundle with no decisions.
    """
    decisions_dir = bundle_dir / "decisions"
    diagnostics: list[Diagnostic] = []
    records: list[dict[str, object]] = []
    try:
        files = sorted(
            p for p in decisions_dir.iterdir() if p.name.endswith(".yaml")
        )
    except FileNotFoundError:
        return [], []
    except OSError as err:
        return [], [
            Diagnostic(
                code="decision-unreadable",
                message=f"cannot list decisions/: {type(err).__name__}: {err}",
                path=_relpath(decisions_dir, mirror_root),
            )
        ]
    for path in files:
        data, diag = _load_yaml_file(path, mirror_root, "decision")
        if diag is not None:
            diagnostics.append(diag)
            continue
        if not classify:
            continue
        if not isinstance(data, dict):
            diagnostics.append(
                Diagnostic(
                    code="decision-unreadable",
                    message="not a YAML mapping",
                    path=_relpath(path, mirror_root),
                )
            )
        elif not _decision_validator().is_valid(data):
            diagnostics.append(
                Diagnostic(
                    code="decision-schema-invalid",
                    message="does not match the vendored gate-decision/v1",
                    path=_relpath(path, mirror_root),
                )
            )
        else:
            records.append(data)
    return records, diagnostics


def _supersedes_target(record: dict[str, object]) -> str | None:
    raw = record.get("supersedes")
    if isinstance(raw, str):
        return raw.removeprefix("gate-decision://")
    return None


def _supersession_integrity(records: list[dict[str, object]]) -> list[Diagnostic]:
    """Duplicate ids, dangling supersedes, self/cyclic supersession.

    Runs only over a fully schema-valid decision set; any hit means the
    decision history is unprovable — the caller classifies unknown.
    """
    ids = [str(r["decision_id"]) for r in records]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        return [
            Diagnostic(
                code="decision-id-duplicate",
                message=f"decision_id {d} appears more than once in the bundle",
            )
            for d in duplicates
        ]
    chain = {
        str(r["decision_id"]): target
        for r in records
        if (target := _supersedes_target(r)) is not None
    }
    diagnostics = [
        Diagnostic(
            code="supersedes-dangling",
            message=f"{source} supersedes {target}, which is not in the bundle",
        )
        for source, target in sorted(chain.items())
        if target not in set(ids)
    ]
    cycles: set[frozenset[str]] = set()
    for start in chain:
        seen: list[str] = []
        current: str | None = start
        while current in chain:
            if current in seen:
                cycles.add(frozenset(seen[seen.index(current) :]))
                break
            seen.append(current)
            current = chain[current]
    diagnostics.extend(
        Diagnostic(
            code="supersedes-cycle",
            message=f"supersession cycle: {', '.join(sorted(members))}",
        )
        for members in sorted(cycles, key=sorted)
    )
    return diagnostics


def _gate_wait(
    bundle_path: str,
    proposal: dict[str, object],
    decisions: list[dict[str, object]],
) -> GateWait | None:
    """The version-matched active-approve rule (spec «Classification»)."""
    gate_id = _STATUS_GATE.get(str(proposal["status"]))
    if gate_id is None:
        return None
    proposal_id = str(proposal["proposal_id"])
    version = int(proposal["version"])  # type: ignore[arg-type]
    ref = f"proposal://{proposal_id}"
    superseded = {
        target for r in decisions if (target := _supersedes_target(r)) is not None
    }
    for record in decisions:
        subject = record["subject"]
        assert isinstance(subject, dict)  # schema-guaranteed
        if (
            record["decision"] == "approve"
            and record["gate_id"] == gate_id
            and subject["kind"] == "product_proposal"
            and subject["ref"] == ref
            and subject["version"] == version
            and str(record["decision_id"]) not in superseded
        ):
            return None
    return GateWait(
        proposal_id=proposal_id,
        gate_id=gate_id,
        gate_label=_GATE_LABEL[gate_id],
        authority=_GATE_AUTHORITY[gate_id],
        artifact_ref=ref,
        bundle_path=bundle_path,
        version=version,
        proposal_updated_at=str(proposal["updated_at"]),
    )


def _load_bundle(mirror_root: Path, bundle_dir: Path) -> ProposalBundle:
    """Classify one bundle; the global conflict pass runs in the caller."""
    rel = _relpath(bundle_dir, mirror_root) or "."
    diagnostics: list[Diagnostic] = []
    proposal: dict[str, object] | None = None

    data, diag = _load_yaml_file(bundle_dir / "proposal.yaml", mirror_root, "proposal")
    if diag is not None:
        diagnostics.append(diag)
    elif not isinstance(data, dict):
        diagnostics.append(
            Diagnostic(
                code="proposal-unreadable",
                message="not a YAML mapping",
                path=f"{rel}/proposal.yaml",
            )
        )
    elif not _proposal_validator().is_valid(data):
        diagnostics.append(
            Diagnostic(
                code="proposal-schema-invalid",
                message="does not match the vendored product-proposal/v1",
                path=f"{rel}/proposal.yaml",
            )
        )
    else:
        proposal = data

    decisions, decision_diags = _load_decisions(
        bundle_dir, mirror_root, classify=proposal is not None
    )
    diagnostics.extend(decision_diags)
    if proposal is not None and not decision_diags:
        diagnostics.extend(_supersession_integrity(decisions))

    diagnostics.sort(key=lambda d: (d.path or "", d.code, d.message))
    if any(d.code in _UNREADABLE_CODES for d in diagnostics):
        state: BundleState = "unreadable"
    elif diagnostics:
        state = "unknown"
    else:
        state = "ok"

    waits: list[GateWait] = []
    if state == "ok" and proposal is not None:
        wait = _gate_wait(rel, proposal, decisions)
        if wait is not None:
            waits.append(wait)

    return ProposalBundle(
        path=rel,
        state=state,
        diagnostics=diagnostics,
        proposal_id=str(proposal["proposal_id"]) if proposal else None,
        status=str(proposal["status"]) if proposal else None,
        version=int(proposal["version"]) if proposal else None,  # type: ignore[arg-type]
        updated_at=str(proposal["updated_at"]) if proposal else None,
        waits=waits,
    )
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_product_proposals.py -v`
Expected: all PASS. If the supersession or torn-write tests fail, fix the
implementation — never the pinned regression tests.

- [ ] **Step 6: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . && pyrefly check
git add dispatcher/core/product_proposals.py tests/test_product_proposals.py
git commit -m "feat: bundle classification — version-matched approve, supersession integrity, lossless diagnostics"
```

---

### Task 7: Core module — conflicts, report assembly, anchors, read-only

**Files:**
- Modify: `dispatcher/core/product_proposals.py`
- Test: `tests/test_product_proposals.py`

**Interfaces:**
- Produces: the final `collect_product_proposals(mirror_root: Path) -> ProductProposalsReport` every consumer calls.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_product_proposals.py`:

```python
import hashlib


def make_mirror(tmp_path: Path) -> Path:
    """A minimal detectable impresario mirror (both anchors present)."""
    mirror = tmp_path / "impresario"
    for rel in ANCHOR_FILES:
        _mk(mirror, rel, "{}\n" if rel.endswith(".json") else "# semantics\n")
    return mirror


def test_missing_anchor_is_anchors_missing_not_zero_bundles(
    tmp_path: Path,
) -> None:
    mirror = make_mirror(tmp_path)
    (mirror / "docs" / "semantics.md").unlink()
    report = collect_product_proposals(mirror)
    assert report.bundles == []
    assert [d.code for d in report.diagnostics] == ["mirror-anchors-missing"]
    assert report.diagnostics[0].path == "docs/semantics.md"
    assert report.attention is True


def test_zero_bundles_on_a_healthy_mirror_is_explicit_and_calm(
    tmp_path: Path,
) -> None:
    report = collect_product_proposals(make_mirror(tmp_path))
    assert report.bundles == [] and report.waits == []
    assert report.diagnostics == [] and report.attention is False


def test_proposal_id_conflict_suppresses_all_participants(tmp_path: Path) -> None:
    mirror = make_mirror(tmp_path)
    make_bundle(
        mirror,
        rel="pilot/a",
        proposal=proposal_yaml(status="ready_for_business", version=6),
    )
    make_bundle(
        mirror,
        rel="pilot/b",
        proposal=proposal_yaml(status="approved"),
        decisions={"bad.yaml": "decision_id: GD-1\n"},  # earlier diagnostic
    )
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.bundles] == ["conflict", "conflict"]
    assert report.waits == []  # the Gate A wait of pilot/a is suppressed
    for bundle in report.bundles:
        conflict = [d for d in bundle.diagnostics if d.code == "proposal-id-conflict"]
        assert len(conflict) == 1
        assert "pilot/a" in conflict[0].message and "pilot/b" in conflict[0].message
    # earlier diagnostics are preserved, not replaced (spec section 1 refinements)
    b_codes = {d.code for d in report.bundles[1].diagnostics}
    assert "decision-schema-invalid" in b_codes
    assert report.attention is True


def test_waits_aggregate_and_sort_deterministically(tmp_path: Path) -> None:
    mirror = make_mirror(tmp_path)
    make_bundle(
        mirror,
        rel="pilot/z",
        proposal=proposal_yaml(
            pid="PP-100", status="business_approved", version=3
        ),
    )
    make_bundle(
        mirror,
        rel="pilot/a",
        proposal=proposal_yaml(
            pid="PP-200", status="ready_for_business", version=1
        ),
    )
    report = collect_product_proposals(mirror)
    assert [b.path for b in report.bundles] == ["pilot/a", "pilot/z"]
    assert [(w.proposal_id, w.gate_id) for w in report.waits] == [
        ("PP-100", "qg5_committee"),
        ("PP-200", "qg5_business"),
    ]
    assert report.attention is False  # plain waits are business, not defects


def test_repeated_scans_are_byte_identical(tmp_path: Path) -> None:
    mirror = make_mirror(tmp_path)
    make_bundle(
        mirror, proposal=proposal_yaml(status="ready_for_business", version=6)
    )
    first = collect_product_proposals(mirror)
    second = collect_product_proposals(mirror)
    assert first.model_dump_json() == second.model_dump_json()


def _tree_state(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): (
            hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
        )
        for p in root.rglob("*")
    }


def test_collect_is_read_only_paths_and_bytes(tmp_path: Path) -> None:
    """Path SET equality too: creating files/dirs is a violation, not just
    modifying them (spec «Fail-closed invariants» #4)."""
    mirror = make_mirror(tmp_path)
    make_bundle(
        mirror,
        proposal=proposal_yaml(status="ready_for_business", version=8),
        decisions={"gd-001.yaml": decision_yaml(version=6)},
    )
    before = _tree_state(mirror)
    collect_product_proposals(mirror)
    assert _tree_state(mirror) == before


def test_report_mirror_path_is_the_scanned_root(tmp_path: Path) -> None:
    mirror = make_mirror(tmp_path)
    assert collect_product_proposals(mirror).mirror_path == str(mirror)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_product_proposals.py -k "conflict or anchors or read_only or byte_identical or aggregate or zero_bundles or mirror_path" -v`
Expected: FAIL — `collect_product_proposals` raises `NotImplementedError`.

- [ ] **Step 3: Implement the report assembly**

Replace the `collect_product_proposals` stub in
`dispatcher/core/product_proposals.py`:

```python
def _mark_conflicts(bundles: list[ProposalBundle]) -> None:
    """Global proposal_id check AFTER all bundles parsed: every participant
    gets the identical deterministic path list; earlier diagnostics are
    preserved; conflict outranks every other state and suppresses waits."""
    by_id: dict[str, list[ProposalBundle]] = {}
    for bundle in bundles:
        if bundle.proposal_id is not None:
            by_id.setdefault(bundle.proposal_id, []).append(bundle)
    for proposal_id, group in sorted(by_id.items()):
        if len(group) < 2:
            continue
        paths = ", ".join(sorted(b.path for b in group))
        for bundle in group:
            bundle.state = "conflict"
            bundle.waits = []
            bundle.diagnostics.append(
                Diagnostic(
                    code="proposal-id-conflict",
                    message=(
                        f"proposal_id {proposal_id} claimed by several "
                        f"bundles: {paths}"
                    ),
                )
            )
            bundle.diagnostics.sort(key=lambda d: (d.path or "", d.code, d.message))


def collect_product_proposals(mirror_root: Path) -> ProductProposalsReport:
    """Scan the impresario mirror and classify every proposal bundle.

    Anchors are re-checked before every scan — a mirror that vanished or
    degraded after discovery is a visible diagnostic, never «0 bundles».
    """
    missing = [rel for rel in ANCHOR_FILES if not (mirror_root / rel).is_file()]
    if missing:
        return ProductProposalsReport(
            mirror_path=str(mirror_root),
            diagnostics=[
                Diagnostic(
                    code="mirror-anchors-missing",
                    message="expected impresario anchor is not a file",
                    path=rel,
                )
                for rel in missing
            ],
            attention=True,
        )
    bundle_dirs, report_diags = _discover(mirror_root)
    bundles = [_load_bundle(mirror_root, d) for d in bundle_dirs]
    _mark_conflicts(bundles)
    report_diags.sort(key=lambda d: (d.path or "", d.code, d.message))
    waits = sorted(
        (w for b in bundles if b.state == "ok" for w in b.waits),
        key=lambda w: (w.proposal_id, w.gate_id, w.version, w.bundle_path),
    )
    return ProductProposalsReport(
        mirror_path=str(mirror_root),
        bundles=bundles,
        waits=waits,
        diagnostics=report_diags,
        attention=bool(report_diags)
        or any(b.state != "ok" for b in bundles),
    )
```

- [ ] **Step 4: Run the full module suite**

Run: `uv run pytest tests/test_product_proposals.py -v`
Expected: all PASS.

- [ ] **Step 5: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . && pyrefly check
git add dispatcher/core/product_proposals.py tests/test_product_proposals.py
git commit -m "feat: conflict pass, anchors re-check, deterministic read-only report"
```

---

### Task 8: Discovery collector + registration

**Files:**
- Create: `dispatcher/core/collectors/impresario.py`
- Modify: `dispatcher/core/collectors/__init__.py`
- Test: `tests/test_impresario_collector.py`

**Interfaces:**
- Produces: `ImpresarioCollector` with `name = "impresario"`, registered in `COLLECTORS` — which is what makes the fleet-level «not detected» card exist when no mirror is discovered.
- Consumes: `ANCHOR_FILES` from `dispatcher.core.product_proposals` (single source for the anchors); `newest_mtime` from `collectors.base`.

- [ ] **Step 1: Write the failing tests**

`tests/test_impresario_collector.py`:

```python
"""Discovery-only impresario collector (spec «Architecture»).

Light by design: detection needs BOTH anchors; collect() carries no bundles
and no waits — classification never enters the snapshot cache.
"""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.collectors import COLLECTORS
from dispatcher.core.collectors.base import CollectContext
from dispatcher.core.collectors.impresario import ImpresarioCollector
from dispatcher.core.product_proposals import ANCHOR_FILES


def _mirror(tmp_path: Path) -> Path:
    mirror = tmp_path / "impresario"
    for rel in ANCHOR_FILES:
        p = mirror / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    return mirror


def test_detect_requires_both_anchors(tmp_path: Path) -> None:
    collector = ImpresarioCollector()
    mirror = _mirror(tmp_path)
    assert collector.detect(mirror) is True
    (mirror / "docs" / "semantics.md").unlink()
    assert collector.detect(mirror) is False


def test_one_incidental_anchor_is_not_impresario(tmp_path: Path) -> None:
    only_docs = tmp_path / "other"
    (only_docs / "docs").mkdir(parents=True)
    (only_docs / "docs" / "semantics.md").write_text("x\n")
    assert ImpresarioCollector().detect(only_docs) is False


def test_collect_is_light_and_stores_no_classification(tmp_path: Path) -> None:
    mirror = _mirror(tmp_path)
    snap = ImpresarioCollector().collect(mirror, CollectContext(home=tmp_path))
    assert snap.name == "impresario"
    assert snap.path == str(mirror)
    assert snap.tasks == [] and snap.errors == [] and snap.warnings == []
    assert snap.freshness is not None


def test_collector_is_registered() -> None:
    assert "impresario" in {c.name for c in COLLECTORS}
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_impresario_collector.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the collector**

`dispatcher/core/collectors/impresario.py`:

```python
"""Discovery-only collector for the impresario product-governance mirror.

Inbox #129: detection is content-based and requires BOTH anchors, so one
incidental file cannot masquerade as the mirror. The snapshot stays light
on purpose — gate_waiting classification runs on demand in
`core/product_proposals.py` and never enters the snapshot cache.
"""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.collectors.base import CollectContext, newest_mtime
from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.product_proposals import ANCHOR_FILES


class ImpresarioCollector:
    """Detects the impresario mirror; collects a light snapshot only."""

    name = "impresario"

    def detect(self, path: Path) -> bool:
        return all((path / rel).is_file() for rel in ANCHOR_FILES)

    def collect(self, path: Path, ctx: CollectContext) -> ProjectSnapshot:
        snap = ProjectSnapshot(name=self.name, path=str(path))
        snap.freshness = newest_mtime([path / rel for rel in ANCHOR_FILES])
        return snap
```

Register it — in `dispatcher/core/collectors/__init__.py`:

```python
from dispatcher.core.collectors.arbiter import ArbiterCollector
from dispatcher.core.collectors.atp import AtpCollector
from dispatcher.core.collectors.base import CollectContext, Collector
from dispatcher.core.collectors.impresario import ImpresarioCollector
from dispatcher.core.collectors.maestro import MaestroCollector
from dispatcher.core.collectors.proctor import ProctorCollector
from dispatcher.core.collectors.spec_runner import SpecRunnerCollector

COLLECTORS: list[Collector] = [
    AtpCollector(),
    MaestroCollector(),
    ArbiterCollector(),
    SpecRunnerCollector(),
    ProctorCollector(),
    ImpresarioCollector(),
]

__all__ = ["COLLECTORS", "CollectContext", "Collector"]
```

- [ ] **Step 4: Run the tests — including the whole suite (registration touches every discovery test)**

Run: `uv run pytest tests/test_impresario_collector.py -v && uv run pytest -q`
Expected: all PASS. If an existing overview/service test pins the exact
collector list, extend its expectation with `impresario` — that row is the
fleet-level coverage diagnostic and belongs there.

- [ ] **Step 5: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . && pyrefly check
git add dispatcher/core/collectors/impresario.py dispatcher/core/collectors/__init__.py tests/test_impresario_collector.py
git commit -m "feat: impresario discovery collector (two anchors; light snapshot)"
```

---

### Task 9: read_api + GET route + API tests

**Files:**
- Modify: `dispatcher/core/read_api.py`
- Modify: `dispatcher/server/app.py`
- Test: `tests/test_product_proposals_api.py`

**Interfaces:**
- Produces: `read_api.product_proposals(cache: SnapshotService, name: str) -> ProductProposalsReport`; `read_api.NotImpresarioMirrorError`; `GET /api/projects/{name}/product-proposals` with the spec's exact 404/200 case split (404 bodies are structured: `detail = {"code": ..., "message": ...}`).
- Consumes: `collect_product_proposals`, `ProductProposalsReport`, `Diagnostic` (Task 7); `read_api.project` (exists).

- [ ] **Step 1: Write the failing API tests**

`tests/test_product_proposals_api.py`:

```python
"""GET /api/projects/{name}/product-proposals — the spec's API case split.

The endpoint is a pass-through of the core read model: tests seed real tmp
mirrors and exercise the serialized public response.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import make_arbiter

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.product_proposals import ANCHOR_FILES
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio


def make_impresario(root: Path) -> Path:
    mirror = root / "impresario"
    for rel in ANCHOR_FILES:
        p = mirror / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    return mirror


def _client(tmp_path: Path) -> httpx.AsyncClient:
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def _seed_wait(mirror: Path) -> None:
    bundle = mirror / "pilot" / "pp-101"
    (bundle / "decisions").mkdir(parents=True)
    (bundle / "proposal.yaml").write_text(
        "proposal_id: PP-101\n"
        "idea_ref: idea://IDEA-101\n"
        "version: 6\n"
        "status: ready_for_business\n"
        "iteration: 2\n"
        "refs:\n"
        "  exchange_log: exchange-log://XL-101\n"
        "created_at: '2026-08-12T02:08:53Z'\n"
        "updated_at: '2026-08-12T04:12:30Z'\n"
    )


async def test_unknown_project_is_404_project_not_found(tmp_path: Path) -> None:
    make_impresario(tmp_path)
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/nonesuch/product-proposals")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "project-not-found"


async def test_known_non_impresario_project_is_404_not_impresario_mirror(
    tmp_path: Path,
) -> None:
    make_arbiter(tmp_path)
    make_impresario(tmp_path)
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/arbiter/product-proposals")
    assert resp.status_code == 404
    assert resp.json()["detail"]["code"] == "not-impresario-mirror"


async def test_undetected_mirror_is_200_mirror_not_detected(
    tmp_path: Path,
) -> None:
    """No impresario under the roots: the negative snapshot row answers with
    a report-level diagnostic — safe under direct request, no Path('')."""
    make_arbiter(tmp_path)  # some OTHER project, so discovery runs fine
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/impresario/product-proposals")
    assert resp.status_code == 200
    data = resp.json()
    assert data["bundles"] == []
    assert [d["code"] for d in data["diagnostics"]] == ["mirror-not-detected"]
    assert data["attention"] is True


async def test_healthy_empty_mirror_is_200_zero_bundles(tmp_path: Path) -> None:
    make_impresario(tmp_path)
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/impresario/product-proposals")
    assert resp.status_code == 200
    data = resp.json()
    assert data["bundles"] == [] and data["waits"] == []
    assert data["diagnostics"] == [] and data["attention"] is False


async def test_anchors_lost_after_discovery_is_200_anchors_missing(
    tmp_path: Path,
) -> None:
    mirror = make_impresario(tmp_path)
    async with _client(tmp_path) as client:
        # First call populates the snapshot cache with the detected mirror…
        first = await client.get("/api/projects/impresario/product-proposals")
        assert first.status_code == 200
        # …then the mirror degrades within the cache TTL.
        (mirror / "docs" / "semantics.md").unlink()
        resp = await client.get("/api/projects/impresario/product-proposals")
    assert resp.status_code == 200
    data = resp.json()
    assert [d["code"] for d in data["diagnostics"]] == ["mirror-anchors-missing"]
    assert data["attention"] is True


async def test_partial_result_is_200_with_attention(tmp_path: Path) -> None:
    mirror = make_impresario(tmp_path)
    _seed_wait(mirror)
    broken = mirror / "pilot" / "pp-999"
    broken.mkdir(parents=True)
    (broken / "proposal.yaml").write_bytes(b"\xff\xfe")
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/impresario/product-proposals")
    assert resp.status_code == 200
    data = resp.json()
    assert [b["state"] for b in data["bundles"]] == ["ok", "unreadable"]
    assert [
        (w["proposal_id"], w["gate_id"], w["authority"]) for w in data["waits"]
    ] == [("PP-101", "qg5_business", "business_owner")]
    assert data["waits"][0]["proposal_updated_at"] == "2026-08-12T04:12:30Z"
    assert data["attention"] is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_product_proposals_api.py -v`
Expected: FAIL — 404 on every request (route missing).

- [ ] **Step 3: Implement read_api + route**

In `dispatcher/core/read_api.py` — import at the top (with the other
`dispatcher.core` imports):

```python
from dispatcher.core.product_proposals import (
    Diagnostic,
    ProductProposalsReport,
    collect_product_proposals,
)
```

Below `governance(...)` add:

```python
class NotImpresarioMirrorError(Exception):
    """The named project exists but is not the impresario mirror."""


def product_proposals(cache: SnapshotService, name: str) -> ProductProposalsReport:
    """Inbox #129: gate_waiting read model for the impresario mirror.

    A pass-through of core/product_proposals (spec «Architecture»): this
    layer scans nothing and classifies nothing. The undetected mirror is
    answered here — with a report-level diagnostic, never a scan of an
    empty path.
    """
    snap = project(cache, name)  # ReadLookupError -> 404 project-not-found
    if snap.name != "impresario":
        raise NotImpresarioMirrorError(f"{name} is not the impresario mirror")
    if not snap.detected or not snap.path:
        return ProductProposalsReport(
            mirror_path="",
            diagnostics=[
                Diagnostic(
                    code="mirror-not-detected",
                    message=(
                        "no impresario mirror was discovered under the "
                        "configured roots"
                    ),
                )
            ],
            attention=True,
        )
    return collect_product_proposals(Path(snap.path))
```

In `dispatcher/server/app.py` — import `ProductProposalsReport`:

```python
from dispatcher.core.product_proposals import ProductProposalsReport
```

and add the route right after `project_governance`:

```python
    @app.get(
        "/api/projects/{name}/product-proposals",
        response_model=ProductProposalsReport,
    )
    def project_product_proposals(name: str) -> ProductProposalsReport:
        """Inbox #129: read-only gate_waiting (GET only; producer decides —
        dispatcher renders). 404 codes are structured so the panel can tell
        «not this kind of project» from «unknown project»."""
        try:
            return read_api.product_proposals(cache, name)
        except read_api.ReadLookupError as err:
            raise HTTPException(
                status_code=404,
                detail={"code": "project-not-found", "message": str(err)},
            ) from err
        except read_api.NotImpresarioMirrorError as err:
            raise HTTPException(
                status_code=404,
                detail={"code": "not-impresario-mirror", "message": str(err)},
            ) from err
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_product_proposals_api.py -v && uv run pytest -q`
Expected: all PASS (the full run guards the route table and read_api imports).

- [ ] **Step 5: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . && pyrefly check
git add dispatcher/core/read_api.py dispatcher/server/app.py tests/test_product_proposals_api.py
git commit -m "feat: GET /api/projects/{name}/product-proposals with structured 404 codes"
```

---

### Task 10: Pinned PP-101 fixture + acceptance tests

**Files:**
- Create: `tests/fixtures/product_proposals/pp-101/proposal.yaml`
- Create: `tests/fixtures/product_proposals/pp-101/decisions/gd-001.yaml`
- Create: `tests/fixtures/product_proposals/pp-101/decisions/gd-002.yaml`
- Create: `tests/fixtures/product_proposals/pp-101/PROVENANCE.txt`
- Test: `tests/test_product_proposals_acceptance.py`

**Interfaces:**
- Consumes: `collect_product_proposals`, `ANCHOR_FILES` (Task 7).
- Produces: the acceptance evidence issue #129 names.

- [ ] **Step 1: Pin the PP-101 copy from the SAME commit as the contract pin**

```bash
mkdir -p tests/fixtures/product_proposals/pp-101/decisions
git -C ../impresario show 28727ff76a3983744596137706c844c95a5ad12b:pilot/forconcept/pp-101/proposal.yaml \
  > tests/fixtures/product_proposals/pp-101/proposal.yaml
git -C ../impresario show 28727ff76a3983744596137706c844c95a5ad12b:pilot/forconcept/pp-101/decisions/gd-001.yaml \
  > tests/fixtures/product_proposals/pp-101/decisions/gd-001.yaml
git -C ../impresario show 28727ff76a3983744596137706c844c95a5ad12b:pilot/forconcept/pp-101/decisions/gd-002.yaml \
  > tests/fixtures/product_proposals/pp-101/decisions/gd-002.yaml
```

Write `tests/fixtures/product_proposals/pp-101/PROVENANCE.txt`:

```text
source: impresario pilot/forconcept/pp-101 (the first live bundle)
commit: 28727ff76a3983744596137706c844c95a5ad12b  (same as the contract pin)
copied: 2026-08-12
files: proposal.yaml, decisions/gd-001.yaml, decisions/gd-002.yaml — only the
  contract-relevant files; cd-*/rp-*/idea/exchange-log/loop.state/trace do not
  affect gate_waiting classification and are deliberately omitted.
note: pinned test fixture (read from git objects, not the working tree).
  Acceptance scenarios mutate a tmp_path copy of THIS directory — the files
  here are never edited. Refresh together with a contract re-vendor.
```

Sanity-check the copy: `proposal.yaml` must say `version: 8`,
`status: approved`; `gd-001.yaml` — `gate_id: qg5_business`,
`subject.version: 6`; `gd-002.yaml` — `gate_id: qg5_committee`,
`subject.version: 7`.

- [ ] **Step 2: Write the acceptance tests (verbatim from issue #129)**

`tests/test_product_proposals_acceptance.py`:

```python
"""Acceptance from inbox #129, run on the pinned copy of the real PP-101.

1. ready_for_business copy with GD-001 REMOVED (deleted — not marked, not
   corrupted; the other decision retained) -> exactly one gate_waiting
   record (Gate A, business_owner, proposal://PP-101).
2. The true approved copy -> ok, zero waits.
3. A copy with an unreadable decision file -> unknown, NOT «zero waits».
"""

from __future__ import annotations

import shutil
from pathlib import Path

from dispatcher.core.product_proposals import (
    ANCHOR_FILES,
    collect_product_proposals,
)

PP101 = Path(__file__).parent / "fixtures" / "product_proposals" / "pp-101"


def _mirror_with_pp101(tmp_path: Path) -> tuple[Path, Path]:
    mirror = tmp_path / "impresario"
    for rel in ANCHOR_FILES:
        p = mirror / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x\n")
    bundle = mirror / "pilot" / "forconcept" / "pp-101"
    shutil.copytree(PP101, bundle)
    (bundle / "PROVENANCE.txt").unlink()  # fixture metadata, not bundle content
    return mirror, bundle


def test_acceptance_1_ready_for_business_without_gd001_waits_for_gate_a(
    tmp_path: Path,
) -> None:
    mirror, bundle = _mirror_with_pp101(tmp_path)
    proposal = (bundle / "proposal.yaml").read_text()
    (bundle / "proposal.yaml").write_text(
        proposal.replace("status: approved", "status: ready_for_business")
    )
    (bundle / "decisions" / "gd-001.yaml").unlink()  # removed, not corrupted
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.bundles] == ["ok"]
    assert [
        (w.gate_id, w.gate_label, w.authority, w.artifact_ref)
        for w in report.waits
    ] == [("qg5_business", "Gate A", "business_owner", "proposal://PP-101")]
    assert report.waits[0].bundle_path == "pilot/forconcept/pp-101"
    assert report.attention is False


def test_acceptance_2_true_approved_bundle_has_zero_waits(
    tmp_path: Path,
) -> None:
    mirror, _ = _mirror_with_pp101(tmp_path)
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.bundles] == ["ok"]
    assert [b.status for b in report.bundles] == ["approved"]
    assert report.waits == [] and report.attention is False


def test_acceptance_3_unreadable_decision_is_unknown_not_zero_waits(
    tmp_path: Path,
) -> None:
    mirror, bundle = _mirror_with_pp101(tmp_path)
    (bundle / "decisions" / "gd-001.yaml").write_bytes(b"\xff\xfe not utf-8")
    report = collect_product_proposals(mirror)
    assert [b.state for b in report.bundles] == ["unknown"]
    assert [d.code for d in report.bundles[0].diagnostics] == [
        "decision-unreadable"
    ]
    assert report.bundles[0].waits == []  # suppressed, not «nothing waits»
    assert report.attention is True
```

- [ ] **Step 3: Run the acceptance tests**

Run: `uv run pytest tests/test_product_proposals_acceptance.py -v`
Expected: 3 PASS.

- [ ] **Step 4: Format, lint, typecheck, commit**

```bash
uv run ruff format . && uv run ruff check . && pyrefly check
git add tests/fixtures/product_proposals tests/test_product_proposals_acceptance.py
git commit -m "test: issue #129 acceptance on the pinned PP-101 copy"
```

---

### Task 11: Live smoke — pinned impresario checkout + HTTP surface

**Files:**
- Create: `scripts/checkout_pinned_impresario.sh`
- Test: `tests/test_product_proposals_live_smoke.py`
- Modify: `.github/workflows/ci.yml` (one step in the `test` job)

**Interfaces:**
- Produces: `IMPRESARIO_PINNED_DIR` env contract between the script and the test.
- Consumes: both manifests' `producer_commit` (Task 1); `create_app` (Task 9).

- [ ] **Step 1: Write the checkout script**

`scripts/checkout_pinned_impresario.sh` (mode 755):

```bash
#!/usr/bin/env bash
# Extract the impresario mirror at the vendored pin for the live smoke.
#
# The pin is READ from the two vendored manifests, which must agree — a
# disagreement is exactly the mixed-versions state the PR gate forbids, so
# it fails here too. PP-101 must exist at that commit: its absence is a
# provenance FAILURE (the pin does not contain the bundle the smoke is
# specified against), never a skip.
#
# Usage:
#   scripts/checkout_pinned_impresario.sh [--from <git-repo>]
#
# Prints the extracted mirror directory on stdout (everything else goes to
# stderr). Wire it into a test run as:
#   IMPRESARIO_PINNED_DIR="$(scripts/checkout_pinned_impresario.sh)" \
#     uv run pytest tests/test_product_proposals_live_smoke.py -v
#
# Exit: 0 ok · 1 usage · 2 source or commit unavailable ·
#       3 provenance failure (manifest disagreement, or PP-101 absent)
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRODUCER_URL="https://github.com/andrei-shtanakov/impresario"
PP101_REL="pilot/forconcept/pp-101/proposal.yaml"

die() { echo "checkout-pinned-impresario: $2" >&2; exit "$1"; }

FROM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from)
      [ $# -ge 2 ] || die 1 "--from needs a path"
      FROM="$2"; shift 2 ;;
    *) die 1 "unknown argument: $1" ;;
  esac
done

command -v python3 > /dev/null 2>&1 || die 2 "python3 not found on PATH"
read -r PIN_A PIN_B <<< "$(python3 - "$REPO_ROOT" << 'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]) / "contracts"
pins = [
    json.load(open(root / name / "v1" / "manifest.json"))["producer_commit"]
    for name in ("impresario-product-proposal", "impresario-gate-decision")
]
print(pins[0], pins[1])
PY
)" || die 3 "could not read producer_commit from the vendored manifests"
[ "$PIN_A" = "$PIN_B" ] ||
  die 3 "the two manifests disagree on producer_commit: $PIN_A vs $PIN_B"
PIN="$PIN_A"

WORK="$(mktemp -d)"
if [ -n "$FROM" ]; then
  FROM="$(cd "$FROM" && pwd)" || die 2 "--from path does not exist"
  STORE="$FROM"
else
  STORE="$WORK/store"
  git init --quiet --bare "$STORE"
  git -C "$STORE" fetch --quiet --depth=1 "$PRODUCER_URL" "$PIN" ||
    die 2 "could not fetch $PIN from $PRODUCER_URL"
fi
git -C "$STORE" cat-file -e "$PIN^{commit}" 2> /dev/null ||
  die 2 "$PIN is not a commit in the source"

DEST="$WORK/impresario"
mkdir -p "$DEST"
git -C "$STORE" archive "$PIN" | tar -x -C "$DEST" ||
  die 2 "could not extract $PIN"
[ -f "$DEST/$PP101_REL" ] ||
  die 3 "provenance failure: $PP101_REL is absent at $PIN — the smoke is specified against PP-101"

echo "extracted impresario @ $PIN" >&2
echo "$DEST"
```

- [ ] **Step 2: Write the smoke test (hard prerequisite, serialized public response)**

`tests/test_product_proposals_live_smoke.py`:

```python
"""Cross-repo live smoke: the REAL impresario tree at the contract pin.

`IMPRESARIO_PINNED_DIR` (exported by scripts/checkout_pinned_impresario.sh)
is a HARD prerequisite — this test FAILS without it, it does not skip (the
PR #98 discipline: a skip is how a suite goes green while covering nothing).
Locally:
    IMPRESARIO_PINNED_DIR="$(scripts/checkout_pinned_impresario.sh --from ../impresario)" \
      uv run pytest tests/test_product_proposals_live_smoke.py -v

Asserted against the SERIALIZED public response — attention, diagnostics,
bundles, waits — not the core function's return value. At the pin the tree
holds exactly one bundle outside contracts/ (pp-101, approved): a different
result after a re-vendor is a real contract-behaviour change and must be
reviewed, not re-pinned away. (contracts/examples/pp-001 doubles as live
proof of the contracts/-segment exclusion.)
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio

_MISSING = (
    "IMPRESARIO_PINNED_DIR is a required prerequisite of the product-proposals "
    "live smoke — run scripts/checkout_pinned_impresario.sh (CI does; locally "
    "add --from ../impresario). Without it the end-to-end path is UNVERIFIED, "
    "and that must FAIL, not skip."
)


async def test_http_surface_on_the_pinned_real_mirror() -> None:
    pinned = os.environ.get("IMPRESARIO_PINNED_DIR")
    assert pinned, _MISSING
    mirror = Path(pinned)
    assert (mirror / "pilot" / "forconcept" / "pp-101" / "proposal.yaml").is_file()

    config = DispatcherConfig(roots=(mirror.parent,))
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        resp = await client.get("/api/projects/impresario/product-proposals")

    assert resp.status_code == 200
    data = resp.json()
    assert data["attention"] is False
    assert data["diagnostics"] == []
    assert [(b["path"], b["state"], b["status"]) for b in data["bundles"]] == [
        ("pilot/forconcept/pp-101", "ok", "approved")
    ]
    assert data["waits"] == []
```

- [ ] **Step 3: Run it locally via the sibling object store**

```bash
chmod +x scripts/checkout_pinned_impresario.sh
IMPRESARIO_PINNED_DIR="$(scripts/checkout_pinned_impresario.sh --from ../impresario)" \
  uv run pytest tests/test_product_proposals_live_smoke.py -v
```

Expected: PASS. Also verify the hard-prereq path:
`uv run pytest tests/test_product_proposals_live_smoke.py -v` (no env var)
must FAIL with the `_MISSING` message.

- [ ] **Step 4: Wire CI**

In `.github/workflows/ci.yml`, in the `test` job, right after the
`install the pinned steward binary` step, add:

```yaml
      # Same discipline for the product-proposals live smoke (inbox #129):
      # the real impresario tree at the vendored pin, or the test FAILS.
      - name: checkout pinned impresario (product-proposals live smoke)
        run: echo "IMPRESARIO_PINNED_DIR=$(scripts/checkout_pinned_impresario.sh)" >> "$GITHUB_ENV"
```

- [ ] **Step 5: Format, lint, commit**

```bash
uv run ruff format . && uv run ruff check . && pyrefly check
git add scripts/checkout_pinned_impresario.sh tests/test_product_proposals_live_smoke.py .github/workflows/ci.yml
git commit -m "test: live smoke on the real impresario tree at the vendored pin"
```

---

### Task 12: TODO evidence, full gates, PR

**Files:**
- Modify: `TODO.md` (the `@id:product-proposal-gate-waiting` item body)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Add PR-1 evidence to the TODO item (item stays `[ ]`)**

In `TODO.md`, in the body of the item
`@id:product-proposal-gate-waiting` (under «Product-governance
(impresario)»), append to the item's continuation text (NOT the checkbox
line — tags live there):

```markdown
      Прогресс: PR-1 (вендор @ 28727ff, `core/product_proposals.py`,
      `collectors/impresario.py`, `GET /api/projects/{name}/product-proposals`,
      acceptance на пине PP-101, live smoke) — открыт; спека
      `docs/superpowers/specs/2026-08-12-product-proposal-gate-waiting-design.md`.
      Закрытие пункта — только после PR-2 (web-панель + Node harness).
```

- [ ] **Step 2: Run the FULL verification gate**

```bash
uv run ruff format . && uv run ruff check . && pyrefly check
IMPRESARIO_PINNED_DIR="$(scripts/checkout_pinned_impresario.sh --from ../impresario)" uv run pytest -q
```

Expected: everything green. Fix anything red before the PR — never push a
known-red branch.

- [ ] **Step 3: Commit, push, open the PR**

```bash
git add TODO.md
git commit -m "docs(todo): PR-1 evidence for product-proposal-gate-waiting (stays open until PR-2)"
git push -u origin feat/pp-gate-waiting-collector
gh pr create --title "feat: product_proposal gate_waiting — vendor + collector + API (inbox #129, PR-1)" --body "$(cat <<'EOF'
## Summary
- vendors impresario `product-proposal/v1` + `gate-decision/v1` at one pin (`28727ff7`), one re-vendor script, copy-integrity + anti-mix PR gate, scheduled drift job
- `core/product_proposals.py`: deterministic bundle discovery (fail-loud walk, symlink-escape guards), strict-YAML + vendored-schema validation, version-matched active-approve `gate_waiting` classification, lossless structured diagnostics
- `collectors/impresario.py`: two-anchor content-based discovery (light snapshot; the undetected row is the fleet-level coverage diagnostic)
- `GET /api/projects/{name}/product-proposals` with the spec's 404/200 case split (structured 404 codes)
- acceptance on the pinned PP-101 copy + live smoke on the real tree at the pin

Spec: `docs/superpowers/specs/2026-08-12-product-proposal-gate-waiting-design.md`. PR-2 (web panel + Node harness) follows; the TODO item stays open until then.

ARCH evidence: classification only — impresario is never imported and never executed; the only filesystem root touched is the mirror path the snapshot names; no write path exists (read-only test pins path-set + hashes).

## Test plan
- [ ] `uv run pytest -q` (incl. acceptance + API case split + vendor integrity)
- [ ] `IMPRESARIO_PINNED_DIR="$(scripts/checkout_pinned_impresario.sh --from ../impresario)" uv run pytest tests/test_product_proposals_live_smoke.py -v`
- [ ] `uv run ruff check .` + `pyrefly check`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Read the GitHub Copilot review**

After the PR opens, fetch Copilot's review comments; fix valid ones with new
commits on this branch, answer invalid ones with reasoning — never apply
blindly. Iterate until no open comments remain. Do NOT merge — the user
merges.

## Self-review notes (already applied)

- Spec coverage: every spec section maps to a task — vendoring (1–3), read
  model + strict YAML (4), discovery contract (5), classification semantics
  incl. both pinned regressions (6), conflicts/anchors/read-only/determinism
  (7), fleet-level negative row (8), API case split (9), acceptance (10),
  live smoke + machinery-red drift (3, 11), TODO evidence discipline (12).
- The web panel and Node harness are deliberately NOT here — they are PR-2
  (`docs/superpowers/plans/2026-08-12-pp-gate-waiting-panel.md`).
