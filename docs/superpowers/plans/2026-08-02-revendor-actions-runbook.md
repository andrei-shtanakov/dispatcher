# Re-vendor runbook for `github-checker-actions/v1` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make re-vendoring `contracts/github-checker-actions/v1` a repeatable, single-input operation — one canonical runbook plus one dev script that takes the new producer commit as its only argument and proves the bytes it writes come from that commit.

**Architecture:** A bash script (`scripts/revendor_github_checker_actions.sh <NEW_PIN> [--from <repo>]`) fetches the named commit into a throwaway bare object store (default: the canonical producer URL; `--from`: an existing local git repository, explicitly marked as weaker provenance), extracts `contracts/actions/v1` from the object database into a **staging directory beside the vendored copy**, verifies file-by-file that the staged bytes are the commit's blobs, writes `PINNED.txt` and regenerates `manifest.json` from the same SHA, re-verifies, and only then swaps staging into place with a restore trap. `scripts/vendor_manifest.py` loses its hardcoded pin and takes `--producer-commit` / `--root`. The runbook at `docs/revendor-github-checker-actions.md` becomes the canonical procedure; the historical implementation plan is left alone.

**Tech Stack:** bash (matching `scripts/install_pinned_checker.sh`), Python 3.12/3.13 stdlib (`argparse`, `hashlib`, `json`, `pathlib`), pytest with `subprocess`, git plumbing (`archive`, `ls-tree`, `rev-parse`, `hash-object`, `cat-file`).

## Global Constraints

- Line length 88; `uv run ruff check .` and `uv run ruff format --check .` must pass; `uv run pyrefly check` must pass.
- Tests run offline. No test may reach the network, and no test may `skip` — a skip that reads as verified is the defect this repo keeps removing (`tests/pinned_producer.py`, `tests/test_task_authoring_js.py`).
- Shipped runtime code never reads `../github-checker`. This script is a **dev tool**, not shipped runtime; it is the vendoring procedure itself, which by definition touches the producer.
- `tests/conftest.py` already puts `scripts/` on `sys.path` — `import vendor_manifest` works from tests with no path juggling.
- `scripts/vendor_manifest.py` must stay **stdlib-only**: the re-vendor script invokes it as `python3`, not `uv run python`, because it also runs inside a bare temp skeleton with no uv project.
- The producer URL is `https://github.com/andrei-shtanakov/github-checker` — the same literal `scripts/install_pinned_checker.sh` already uses. The two must never name different sources.
- No direct commits to `master`. Branch `docs/revendor-actions-runbook`, PR, act on the GitHub Copilot review, and let the user merge.
- Do not edit any neighbouring repository.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `scripts/revendor_github_checker_actions.sh` | **new** — the whole procedure from one SHA: fetch, extract, verify provenance, write pin + manifest, re-verify, swap safely, report which provenance it proved. |
| `scripts/vendor_manifest.py` | **modify** — pin and root become arguments; the module-level `PRODUCER_COMMIT` literal (line 19) disappears, so the generator stops being a place where the pin is hand-edited. |
| `tests/test_vendor_manifest.py` | **new** — the generator reproduces the committed `manifest.json` byte for byte, and refuses to run without a pin. |
| `tests/test_revendor_script.py` | **new** — the script's behaviour, exercised offline through `--from` against a purpose-built temp producer repo inside a temp dispatcher skeleton. |
| `docs/revendor-github-checker-actions.md` | **new** — the canonical operator runbook, including the four-contract matrix and the honest statement that no drift signal exists for this contract. |
| `README.md` | **modify** — one pointer to the runbook. |
| `TODO.md` | **modify** — the missing upstream-drift signal for actions/v1, recorded as a debt with a trigger. |

**Why the script and not more Python:** the two steps that carry the guarantee (`git archive` from an object store, `git hash-object` against the tree) are git plumbing, and `install_pinned_checker.sh` already establishes bash as this repo's language for pin-handling dev tooling. A Python wrapper would add a process layer over the same `git` calls without adding a check.

**Why staging is inside `contracts/`, not `$TMPDIR`:** the final swap must be a rename on the same filesystem. On macOS `$TMPDIR` is a different volume, so `mv` there degrades to a copy that can die halfway — reintroducing exactly the partial-tree failure the staging model removes.

---

### Task 1: The generator takes the pin as an argument

**Files:**
- Modify: `scripts/vendor_manifest.py:1-62`
- Test: `tests/test_vendor_manifest.py` (new)

**Interfaces:**
- Produces: `build_manifest(root: pathlib.Path, producer_commit: str) -> dict[str, object]`, and a CLI `python3 scripts/vendor_manifest.py --producer-commit <sha> [--root <dir>]`. Task 2's script calls exactly this CLI.
- Consumes: nothing from other tasks.

- [ ] **Step 1: Write the failing test**

Create `tests/test_vendor_manifest.py`:

```python
"""The manifest generator, whose only inputs are a directory and a pin.

The pin used to be a module-level literal here, which made re-vendoring a
matter of remembering three separate hand-edits. It is an argument now, so
the re-vendor script can derive it — and this file's job is to prove the
generator still reproduces the committed manifest exactly, byte for byte,
rather than merely producing something that parses.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import vendor_manifest

REPO_ROOT = Path(__file__).parent.parent
VENDORED_ROOT = REPO_ROOT / "contracts" / "github-checker-actions" / "v1"


def test_regenerating_reproduces_the_committed_manifest_byte_for_byte(
    tmp_path: Path,
) -> None:
    """A generator that produces *a* manifest is not the same as one that
    produces *this* manifest: whitespace, key order and the trailing newline
    are all part of what the committed file is."""
    committed = (VENDORED_ROOT / "manifest.json").read_bytes()
    pin = json.loads(committed)["producer_commit"]

    root = tmp_path / "v1"
    shutil.copytree(VENDORED_ROOT, root)
    (root / "manifest.json").unlink()

    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "vendor_manifest.py"),
            "--producer-commit",
            pin,
            "--root",
            str(root),
        ],
        check=True,
    )

    assert (root / "manifest.json").read_bytes() == committed


def test_the_generator_refuses_to_run_without_a_pin() -> None:
    """No default, and no fallback to whatever the root already contains:
    a manifest silently regenerated at the previous commit is how new bytes
    get certified as coming from an old one."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "vendor_manifest.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "--producer-commit" in result.stderr


def test_the_pin_is_not_hardcoded_in_the_generator() -> None:
    """The literal's absence is the point of the change, so it is asserted
    rather than left to a reviewer's memory."""
    assert not hasattr(vendor_manifest, "PRODUCER_COMMIT")


def test_build_manifest_records_the_pin_it_was_given(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    manifest = vendor_manifest.build_manifest(tmp_path, "0" * 40)
    assert manifest["producer_commit"] == "0" * 40
    assert [e["path"] for e in manifest["surface"]] == ["a.txt"]


@pytest.mark.parametrize("excluded", ["PINNED.txt", "manifest.json"])
def test_the_two_meta_files_stay_out_of_the_surface(
    tmp_path: Path, excluded: str
) -> None:
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / excluded).write_text("meta")
    manifest = vendor_manifest.build_manifest(tmp_path, "0" * 40)
    assert [e["path"] for e in manifest["surface"]] == ["a.txt"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_vendor_manifest.py -v`
Expected: FAIL — `build_manifest()` takes 1 positional argument, and the CLI has no `--producer-commit`.

- [ ] **Step 3: Rewrite `scripts/vendor_manifest.py`**

Replace the whole file with:

```python
"""Regenerate manifest.json for a vendored github-checker actions/v1 copy.

Dev tool, not shipped runtime. Normally invoked by
``scripts/revendor_github_checker_actions.sh``, which passes the commit it
extracted from and the staging directory it extracted into:

    python3 scripts/vendor_manifest.py --producer-commit <sha> --root <dir>

The pin is an argument and has no default. It used to be a literal in this
file, which made it one of three copies a human had to edit in step — and
three literals changed together prove only that they agree with each other,
never that the files on disk came from the commit they name.

It hashes every file under the root (excluding the manifest itself and
PINNED.txt), writes a per-file sha256 plus a tree_sha256 computed over the
sorted (path, sha256) pairs, and records the given producer commit so the
consumer's offline tests can assert on it without touching the network.

Stdlib only, on purpose: the re-vendor script runs it as plain ``python3``,
outside any uv project.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib

ROOT = pathlib.Path("contracts/github-checker-actions/v1")
EXCLUDED_NAMES = {"PINNED.txt", "manifest.json"}


def build_manifest(root: pathlib.Path, producer_commit: str) -> dict[str, object]:
    """Compute the per-file and tree-level hashes for the vendored surface.

    The tree hash is derived from the same sorted (path, sha256) pairs
    that the per-file entries carry, so any test that recomputes it from
    the manifest's own `surface` list reproduces this exact value.
    """
    surface = sorted(
        p for p in root.rglob("*") if p.is_file() and p.name not in EXCLUDED_NAMES
    )
    entries = [
        {
            "path": str(p.relative_to(root)),
            "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in surface
    ]
    tree_sha256 = hashlib.sha256(
        "".join(f"{e['path']}:{e['sha256']}\n" for e in entries).encode()
    ).hexdigest()
    return {
        "contract": "github-checker-actions",
        "contract_version": 1,
        "producer_commit": producer_commit,
        "surface_note": (
            "sha256 of every vendored file; excludes PINNED.txt and this manifest"
        ),
        "tree_sha256": tree_sha256,
        "surface": entries,
    }


def main() -> None:
    """Write manifest.json for the vendored actions/v1 copy at a given pin."""
    parser = argparse.ArgumentParser(description="regenerate manifest.json")
    parser.add_argument(
        "--producer-commit",
        required=True,
        help="the github-checker commit the vendored bytes were extracted from",
    )
    parser.add_argument(
        "--root",
        type=pathlib.Path,
        default=ROOT,
        help="directory holding the vendored copy (default: the in-tree one)",
    )
    args = parser.parse_args()
    manifest = build_manifest(args.root, args.producer_commit)
    (args.root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    main()
```

Note the rename `EXCLUDED_NAMES` stays as it was — only `PRODUCER_COMMIT` is removed and the signature grows a parameter.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_vendor_manifest.py -v`
Expected: PASS — 6 tests.

- [ ] **Step 5: Run the existing suite for regressions**

Run: `uv run pytest tests/test_contract_ingest.py tests/test_pinned_producer.py -v`
Expected: PASS except `test_write_path_live_smoke_real_binary`, which needs the pinned binary on PATH (Task 4 runs the full gate). If that one errors with a missing-binary message, that is expected here and is not a regression.

- [ ] **Step 6: Lint, format and typecheck**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git switch -c docs/revendor-actions-runbook
git add scripts/vendor_manifest.py tests/test_vendor_manifest.py
git commit -m "refactor(vendor): the pin is an argument to the generator, not a literal"
```

---

### Task 2: The re-vendor script

**Files:**
- Create: `scripts/revendor_github_checker_actions.sh`
- Test: `tests/test_revendor_script.py` (new)

**Interfaces:**
- Consumes: `python3 scripts/vendor_manifest.py --producer-commit <sha> --root <dir>` from Task 1.
- Produces: `scripts/revendor_github_checker_actions.sh <NEW_PIN> [--from <git-repo>]`, exit codes `0` ok · `1` usage · `2` source or commit unavailable · `3` provenance mismatch · `4` manifest generation or read-back. Task 3's runbook documents exactly these.

- [ ] **Step 1: Write the test scaffolding and the argument-handling tests**

Create `tests/test_revendor_script.py`:

```python
"""The re-vendor script, exercised offline.

Everything here runs through `--from` against a purpose-built git repository
in tmp_path, inside a copy of the minimal dispatcher layout the script needs.
That copy is why the script has no `--destination` flag: a test-only way to
redirect where the vendored copy lands would be a production-visible way to
overwrite the wrong directory, and the guarantee this script exists to make
is about not damaging the working copy.

The network path is deliberately untested — testing it would mean either a
network call in CI or a mock of the one step whose whole value is that it
really talks to the canonical remote.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT_NAME = "revendor_github_checker_actions.sh"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    """Run one git command in `repo`, with identity supplied by env.

    Identity from the environment, not `git config`: the test must not
    depend on — or write to — whatever global git config the machine has.
    """
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
    """A miniature github-checker: two commits over contracts/actions/v1.

    The second drops a fixture the first had. A file that upstream deleted
    is the case a copy-over-the-top re-vendor gets wrong, so the fixture
    exists to make that case reachable.
    """
    repo = tmp_path / "producer"
    (repo / "contracts" / "actions" / "v1" / "fixtures").mkdir(parents=True)
    _git(repo.parent, "init", "--quiet", str(repo))

    root = repo / "contracts" / "actions" / "v1"
    (root / "README.md").write_text("first\n")
    (root / "actions.schema.json").write_text('{"first": true}\n')
    (root / "fixtures" / "kept.json").write_text('{"kept": 1}\n')
    (root / "fixtures" / "dropped.json").write_text('{"dropped": 1}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "first")
    first = _git(repo, "rev-parse", "HEAD")

    (root / "README.md").write_text("second\n")
    (root / "fixtures" / "dropped.json").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "second")
    second = _git(repo, "rev-parse", "HEAD")

    return {"path": repo, "first": first, "second": second}


@pytest.fixture
def skeleton(tmp_path: Path) -> Path:
    """The smallest dispatcher layout the script needs, with a sentinel.

    The sentinel file is what proves "the working copy was not touched":
    asserting only that the directory still exists would pass even if the
    script had replaced it with a half-built candidate.
    """
    repo = tmp_path / "dispatcher"
    (repo / "scripts").mkdir(parents=True)
    vendored = repo / "contracts" / "github-checker-actions" / "v1"
    vendored.mkdir(parents=True)
    for name in (SCRIPT_NAME, "vendor_manifest.py"):
        shutil.copy2(REPO_ROOT / "scripts" / name, repo / "scripts" / name)
    (vendored / "README.md").write_text("SENTINEL\n")
    (vendored / "PINNED.txt").write_text("commit: old\n")
    return repo


def _run(skeleton: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(skeleton / "scripts" / SCRIPT_NAME), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )


def _vendored(skeleton: Path) -> Path:
    return skeleton / "contracts" / "github-checker-actions" / "v1"


def _assert_untouched(skeleton: Path) -> None:
    vendored = _vendored(skeleton)
    assert (vendored / "README.md").read_text() == "SENTINEL\n"
    assert (vendored / "PINNED.txt").read_text() == "commit: old\n"
    assert not (vendored.parent / "v1.staging").exists()
    assert not (vendored.parent / "v1.prev").exists()


def test_no_argument_is_a_usage_error(skeleton: Path) -> None:
    result = _run(skeleton)
    assert result.returncode == 1
    _assert_untouched(skeleton)


def test_an_abbreviated_sha_is_refused(
    skeleton: Path, producer: dict[str, object]
) -> None:
    """A 12-char prefix resolves fine in git and would be written into the
    manifest as if it identified a commit forever. Ambiguity is cheap to
    refuse here and expensive to discover later."""
    result = _run(skeleton, str(producer["first"])[:12], "--from", str(producer["path"]))
    assert result.returncode == 1
    _assert_untouched(skeleton)


def test_an_unknown_commit_leaves_the_working_copy_alone(
    skeleton: Path, producer: dict[str, object]
) -> None:
    result = _run(skeleton, "0" * 40, "--from", str(producer["path"]))
    assert result.returncode == 2
    _assert_untouched(skeleton)


def test_a_from_path_that_is_not_a_repository_is_refused(
    skeleton: Path, producer: dict[str, object], tmp_path: Path
) -> None:
    (tmp_path / "not-a-repo").mkdir()
    result = _run(
        skeleton, str(producer["second"]), "--from", str(tmp_path / "not-a-repo")
    )
    assert result.returncode == 2
    _assert_untouched(skeleton)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_revendor_script.py -v`
Expected: FAIL at the `skeleton` fixture — `scripts/revendor_github_checker_actions.sh` does not exist.

- [ ] **Step 3: Write the script**

Create `scripts/revendor_github_checker_actions.sh` (and `chmod +x` it):

```bash
#!/usr/bin/env bash
# Re-vendor contracts/github-checker-actions/v1 at a new producer commit.
#
# The pin is the ONE input. Everything written — the extracted bytes,
# PINNED.txt, manifest.json — is derived from the SHA on argv, so the
# guarantee is not "three literals agree with each other" but "the bytes on
# disk are, byte for byte, the blobs of the commit the manifest names".
#
# The working copy is never touched until a fully verified candidate exists:
# extraction and generation happen in a staging directory beside it, and the
# swap is a same-filesystem rename with a restoring trap. A failure anywhere
# leaves the previous vendored copy exactly as it was.
#
# Usage:
#   scripts/revendor_github_checker_actions.sh <NEW_PIN> [--from <git-repo>]
#
# Default: fetch NEW_PIN from the canonical producer URL into a throwaway
# bare object store. --from: read it out of an existing local repository's
# object database instead — no working tree is read, no clean `git status`
# is required (an irrelevant check: we extract from objects, not the tree),
# and the report says the canonical remote was NOT consulted.
#
# Exit: 0 ok · 1 usage · 2 source or commit unavailable ·
#       3 provenance mismatch · 4 manifest generation or read-back
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRODUCER_URL="https://github.com/andrei-shtanakov/github-checker"
SRC_SUBDIR="contracts/actions/v1"
DST="$REPO_ROOT/contracts/github-checker-actions/v1"
STAGING="$DST.staging"
PREV="$DST.prev"

die() { echo "revendor: $2" >&2; exit "$1"; }

NEW_PIN=""
FROM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from)
      [ $# -ge 2 ] || die 1 "--from needs a path"
      FROM="$2"
      shift 2
      ;;
    -h | --help)
      sed -n '2,25p' "${BASH_SOURCE[0]}" >&2
      exit 1
      ;;
    -*) die 1 "unknown option: $1" ;;
    *)
      [ -z "$NEW_PIN" ] || die 1 "exactly one commit may be given"
      NEW_PIN="$1"
      shift
      ;;
  esac
done

[ -n "$NEW_PIN" ] || die 1 "usage: $(basename "$0") <NEW_PIN> [--from <git-repo>]"
# A full 40-hex commit id only. A branch name or an abbreviation resolves
# perfectly well here and would then be written into the manifest as though
# it identified a commit for good.
[[ "$NEW_PIN" =~ ^[0-9a-f]{40}$ ]] || die 1 "not a full 40-hex commit id: $NEW_PIN"

WORK="$(mktemp -d)"

cleanup() {
  local code=$?
  rm -rf "$WORK" "$STAGING"
  # Died between the two renames: the working copy is in $PREV and $DST is
  # gone. Put it back — a failed re-vendor must leave the tree as it found it.
  if [ -d "$PREV" ]; then
    [ -e "$DST" ] || mv "$PREV" "$DST"
    rm -rf "$PREV"
  fi
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

# Extract into a fresh staging directory, never over the top of the current
# copy: a file upstream deleted would otherwise survive as ours and be
# certified by the manifest we are about to generate.
rm -rf "$STAGING"
mkdir -p "$STAGING"
git -C "$STORE" archive "$NEW_PIN" "$SRC_SUBDIR" | tar -x --strip-components=3 -C "$STAGING" ||
  die 2 "$NEW_PIN has no $SRC_SUBDIR to extract"

# The guarantee, stated as a check: every staged file IS the commit's blob,
# and the staged set IS the commit's set — in both directions.
verify_provenance() {
  # $1: "exact" (before our two meta files exist) or "with-meta" (after).
  local mode="$1" rel want got
  git -C "$STORE" ls-tree -r --name-only "$NEW_PIN" -- "$SRC_SUBDIR" |
    sed "s|^$SRC_SUBDIR/||" | LC_ALL=C sort > "$WORK/want.txt"
  (cd "$STAGING" && find . -type f | sed 's|^\./||' | LC_ALL=C sort) > "$WORK/got.txt"
  if [ "$mode" = "with-meta" ]; then
    grep -vx -e 'PINNED.txt' -e 'manifest.json' "$WORK/got.txt" > "$WORK/got.meta" || true
    mv "$WORK/got.meta" "$WORK/got.txt"
  fi
  diff "$WORK/want.txt" "$WORK/got.txt" >&2 ||
    die 3 "the staged file set is not the file set of $NEW_PIN"
  while IFS= read -r rel; do
    want="$(git -C "$STORE" rev-parse "$NEW_PIN:$SRC_SUBDIR/$rel")"
    got="$(git -C "$STORE" hash-object -- "$STAGING/$rel")"
    [ "$want" = "$got" ] ||
      die 3 "staged $rel is not the blob $NEW_PIN has at $SRC_SUBDIR/$rel"
  done < "$WORK/want.txt"
}

verify_provenance exact

cat > "$STAGING/PINNED.txt" << EOF
source: github-checker $SRC_SUBDIR
commit: $NEW_PIN
vendored: $(date -u +%Y-%m-%d)
note: pinned copy (repo-boundaries vendoring, ADR-ECO-003). Do not edit here —
  re-vendor with scripts/revendor_github_checker_actions.sh, which derives every
  value in this directory from the one commit it is given. Procedure:
  docs/revendor-github-checker-actions.md. Nothing in shipped code may read
  ../github-checker at run time.
EOF

python3 "$REPO_ROOT/scripts/vendor_manifest.py" \
  --producer-commit "$NEW_PIN" --root "$STAGING" ||
  die 4 "manifest generation failed"

python3 - "$STAGING/manifest.json" "$NEW_PIN" << 'PY' ||
import json, sys
sys.exit(0 if json.load(open(sys.argv[1]))["producer_commit"] == sys.argv[2] else 1)
PY
  die 4 "the generated manifest does not record the pin it was given"

# Second pass: the generator writes into the staging directory, so it is in a
# position to change the very bytes the first pass approved.
verify_provenance with-meta

# Only now is the working copy touched, and the swap is two renames on one
# filesystem with the trap above standing behind them.
if [ -e "$DST" ]; then mv "$DST" "$PREV"; fi
mv "$STAGING" "$DST"
rm -rf "$PREV"

cat >&2 << EOF
re-vendored $SRC_SUBDIR at $NEW_PIN
  provenance: $PROVENANCE
              $PROVENANCE_NOTE
  files:      $(wc -l < "$WORK/want.txt" | tr -d ' ')
  next:       update PRODUCER_COMMIT in tests/test_contract_ingest.py, then
              PATH="\$(scripts/install_pinned_checker.sh):\$PATH" uv run pytest tests/ -v
EOF
```

Make it executable: `chmod +x scripts/revendor_github_checker_actions.sh`

- [ ] **Step 4: Run the argument tests to verify they pass**

Run: `uv run pytest tests/test_revendor_script.py -v`
Expected: PASS — 4 tests.

- [ ] **Step 5: Write the happy-path and safety tests**

Append to `tests/test_revendor_script.py`:

```python
def test_it_vendors_the_named_commit_even_when_the_source_tree_is_dirty(
    skeleton: Path, producer: dict[str, object]
) -> None:
    """Extraction reads the object database, so an uncommitted edit in the
    source is invisible to it. This is why the script does not demand a
    clean `git status`: that check would be about a tree it never reads."""
    repo = producer["path"]
    assert isinstance(repo, Path)
    (repo / "contracts" / "actions" / "v1" / "README.md").write_text("DIRTY\n")

    result = _run(skeleton, str(producer["second"]), "--from", str(repo))

    assert result.returncode == 0, result.stderr
    assert (_vendored(skeleton) / "README.md").read_text() == "second\n"


def test_a_file_deleted_upstream_disappears_from_the_vendored_copy(
    skeleton: Path, producer: dict[str, object]
) -> None:
    result = _run(skeleton, str(producer["second"]), "--from", str(producer["path"]))

    assert result.returncode == 0, result.stderr
    vendored = _vendored(skeleton)
    assert (vendored / "fixtures" / "kept.json").exists()
    assert not (vendored / "fixtures" / "dropped.json").exists()
    assert (vendored / "README.md").read_text() != "SENTINEL\n"


def test_pinned_txt_and_the_manifest_carry_the_sha_that_was_passed(
    skeleton: Path, producer: dict[str, object]
) -> None:
    pin = str(producer["second"])
    result = _run(skeleton, pin, "--from", str(producer["path"]))

    assert result.returncode == 0, result.stderr
    vendored = _vendored(skeleton)
    assert f"commit: {pin}" in (vendored / "PINNED.txt").read_text()
    manifest = json.loads((vendored / "manifest.json").read_text())
    assert manifest["producer_commit"] == pin
    assert {e["path"] for e in manifest["surface"]} == {
        "README.md",
        "actions.schema.json",
        "fixtures/kept.json",
    }


def test_every_vendored_byte_is_the_blob_of_that_commit(
    skeleton: Path, producer: dict[str, object]
) -> None:
    """The assertion the whole procedure exists to support, restated by the
    test against the same object database the script read."""
    repo = producer["path"]
    assert isinstance(repo, Path)
    pin = str(producer["second"])
    assert _run(skeleton, pin, "--from", str(repo)).returncode == 0

    for rel in ("README.md", "actions.schema.json", "fixtures/kept.json"):
        expected = subprocess.run(
            ["git", "-C", str(repo), "show", f"{pin}:contracts/actions/v1/{rel}"],
            capture_output=True,
            check=True,
        ).stdout
        assert (_vendored(skeleton) / rel).read_bytes() == expected


def test_a_failing_generator_leaves_the_working_copy_alone(
    skeleton: Path, producer: dict[str, object]
) -> None:
    """Injected by replacing the generator in the skeleton — the copy the
    script actually invokes — rather than by mocking anything."""
    (skeleton / "scripts" / "vendor_manifest.py").write_text(
        "import sys\nsys.exit(1)\n"
    )
    result = _run(skeleton, str(producer["second"]), "--from", str(producer["path"]))

    assert result.returncode == 4
    _assert_untouched(skeleton)


def test_a_generator_that_corrupts_the_surface_is_caught(
    skeleton: Path, producer: dict[str, object]
) -> None:
    """The second verification pass, tested by making the step between the
    two passes misbehave: the generator writes a valid manifest and also
    flips a byte it had no business touching."""
    (skeleton / "scripts" / "vendor_manifest.py").write_text(
        "import json, pathlib, sys\n"
        "root = pathlib.Path(sys.argv[sys.argv.index('--root') + 1])\n"
        "pin = sys.argv[sys.argv.index('--producer-commit') + 1]\n"
        "(root / 'README.md').write_text('tampered\\n')\n"
        "(root / 'manifest.json').write_text(\n"
        "    json.dumps({'producer_commit': pin, 'surface': []}) + '\\n'\n"
        ")\n"
    )
    result = _run(skeleton, str(producer["second"]), "--from", str(producer["path"]))

    assert result.returncode == 3
    _assert_untouched(skeleton)


def test_a_manifest_recording_the_wrong_pin_is_caught(
    skeleton: Path, producer: dict[str, object]
) -> None:
    (skeleton / "scripts" / "vendor_manifest.py").write_text(
        "import json, pathlib, sys\n"
        "root = pathlib.Path(sys.argv[sys.argv.index('--root') + 1])\n"
        "(root / 'manifest.json').write_text(\n"
        "    json.dumps({'producer_commit': '0' * 40, 'surface': []}) + '\\n'\n"
        ")\n"
    )
    result = _run(skeleton, str(producer["second"]), "--from", str(producer["path"]))

    assert result.returncode == 4
    _assert_untouched(skeleton)


def test_both_scripts_name_the_same_producer(tmp_path: Path) -> None:
    """`install_pinned_checker.sh` fetches the binary and this one fetches
    the contract. Pointed at different sources they would prove nothing
    about each other, and the divergence would be invisible."""
    revendor = (REPO_ROOT / "scripts" / SCRIPT_NAME).read_text()
    install = (REPO_ROOT / "scripts" / "install_pinned_checker.sh").read_text()
    url = "https://github.com/andrei-shtanakov/github-checker"
    assert f'PRODUCER_URL="{url}"' in revendor
    assert f'PRODUCER_URL="{url}"' in install
```

- [ ] **Step 6: Run the full script test file**

Run: `uv run pytest tests/test_revendor_script.py -v`
Expected: PASS — 12 tests. If `test_a_generator_that_corrupts_the_surface_is_caught` returns 4 instead of 3, the second `verify_provenance` call is missing or placed before the generator; fix the script, not the test.

- [ ] **Step 7: Prove the script against the real pin, without changing anything**

Run:

```bash
scripts/revendor_github_checker_actions.sh \
  ef03fefcded37676b19ef1c6f88b956a09a26d3f --from ../github-checker
git status --porcelain contracts/github-checker-actions/v1
```

Expected: the script exits 0 and prints `provenance: local object store …`; `git status` reports **no changes** — re-vendoring the commit already vendored reproduces the committed bytes exactly, including `manifest.json`. Only `PINNED.txt`'s `vendored:` date and its `note:` wording may differ; if so, that is the intended change from this task and stays. Anything else differing means the script is not reproducing the copy and must be fixed before continuing.

If `../github-checker` is absent on this machine, skip this step and say so in the PR body — do not substitute the network path for it here.

- [ ] **Step 8: Lint, format and typecheck**

Run: `uv run ruff format . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add scripts/revendor_github_checker_actions.sh tests/test_revendor_script.py \
        contracts/github-checker-actions/v1/PINNED.txt
git commit -m "feat(vendor): re-vendor from one SHA, with provenance proved before the swap"
```

Include `PINNED.txt` only if step 7 rewrote its date/note; otherwise drop it from the `git add`.

---

### Task 3: The runbook, and the debt it exposes

**Files:**
- Create: `docs/revendor-github-checker-actions.md`
- Modify: `README.md` (the contracts section, after the `upstream_drift_report.py` block)
- Modify: `TODO.md` (a new item under the open work, and one under «Наблюдения»)

**Interfaces:**
- Consumes: the CLI and exit codes from Task 2, the generator CLI from Task 1.
- Produces: nothing other tasks consume.

- [ ] **Step 1: Write the runbook**

Create `docs/revendor-github-checker-actions.md`:

````markdown
# Runbook: re-vendor `github-checker-actions/v1`

Canonical procedure for moving dispatcher's vendored copy of
`github-checker`'s `contracts/actions/v1` to a new producer commit. This
file is the procedure; `docs/superpowers/plans/2026-07-31-vendor-actions-v1.md`
records how the copy was first created and is not maintained.

## When this runs

**Manually, after a change to `contracts/actions/v1` in github-checker has
been accepted there. There is no automatic drift signal for this contract.**

That is a real gap, not an omission in this document. The `plan-fields`
contract has two guarantees — offline integrity in every PR, plus a
scheduled `upstream-drift.yml` that watches canon — while actions/v1 has
only the first. Nothing will tell you the producer moved; someone has to
notice. The debt is tracked in `TODO.md` as `actions-v1-no-drift-signal`.

Do not start from a green test suite as evidence that the pin is current:
the suite proves the vendored copy matches its own manifest, which stays
true forever no matter how far upstream travels.

## The guarantee

The procedure exists to support one sentence:

> The files in `contracts/github-checker-actions/v1/` are, byte for byte,
> the blobs of the commit recorded in that directory's `manifest.json`.

Note what it does not say. Three SHAs agreeing with each other — the
manifest's, `PINNED.txt`'s, and the literal in `tests/test_contract_ingest.py`
— proves only that someone edited three files consistently. Changed together
and wrongly, they leave every test green while the manifest certifies bytes
that came from somewhere else. That is why the pin is an **input** to the
script and the bytes are checked **against the object database**, not against
the other copies of the number.

## Procedure

### 1. Pick the commit

A full 40-hex commit id from github-checker, already reviewed and merged
there. Abbreviations and branch names are refused: they resolve today and
identify nothing tomorrow.

### 2. Run the script

```bash
scripts/revendor_github_checker_actions.sh <NEW_PIN>
```

It fetches `NEW_PIN` from `https://github.com/andrei-shtanakov/github-checker`
into a throwaway bare object store, extracts `contracts/actions/v1` into a
staging directory beside the vendored copy, verifies every staged file
against the commit's blobs and the file set in both directions, writes
`PINNED.txt`, regenerates `manifest.json` from the same SHA, checks the
manifest records that SHA, verifies the staged bytes a second time, and only
then swaps staging into place.

Until that last step the working copy is untouched. Any failure — including
a kill signal between the two renames — leaves it exactly as it was.

**Offline variant.** `--from <git-repo>` reads the commit out of a local
repository's object database instead:

```bash
scripts/revendor_github_checker_actions.sh <NEW_PIN> --from ../github-checker
```

It proves less, and the report says so: the bytes belong to `NEW_PIN` **in
that object store**, and whether the canonical remote has that commit was
not asked. A clean `git status` in the source is not required and would not
help — nothing reads its working tree. Use the default whenever you have
the network.

**Exit codes:** `0` ok · `1` usage · `2` source or commit unavailable ·
`3` provenance mismatch · `4` manifest generation or read-back.

A `3` means the staged bytes are not the commit's. Do not re-run hoping for
a different answer, and never adjust an expected value to match: something
between the object store and the disk is wrong, and that is the finding.

### 3. Update the independent literal

`tests/test_contract_ingest.py` holds its own copy of the pin:

```python
PRODUCER_COMMIT = "…"
```

This one stays a hand edit **on purpose**. It is the independent assertion
about what the manifest should say, and a test that reads the value it
checks proves nothing. The suite goes red here until you change it — that
red is the last checklist item, not an obstacle.

### 4. Run the full gate with the matching binary

```bash
PATH="$(scripts/install_pinned_checker.sh):$PATH" uv run pytest tests/ -v
```

`install_pinned_checker.sh` reads the commit from the manifest the script
just rewrote, so the binary moves with the contract automatically. The
level-3 smoke (`test_write_path_live_smoke_real_binary`) then exercises the
real producer at the new pin, and PEP 610 install metadata proves the binary
is that commit. Node 22 must be on PATH too — `test_task_authoring_js.py`
fails rather than skips without it.

### 5. Deliver as its own PR

Branch, push, `gh pr create`, act on the GitHub Copilot review, and let a
human merge — the repo's standing rule. Keep the re-vendor free of unrelated
changes: a diff of thousands of vendored lines plus a behaviour change is a
diff nobody can review.

State in the PR body which provenance mode was used, and paste the script's
report.

After the merge: `git switch master && git pull --ff-only`, then check that
CI on master is green — in particular that the `install the pinned producer
binary` step names the new commit.

## What this runbook does not cover

| Contract | Procedure |
|---|---|
| `contracts/github-checker-actions/v1` | this runbook |
| `contracts/github-checker-snapshot/v1` | legacy shape — a hash table in its README, no manifest; needs its own migration before any of this applies |
| `contracts/executor-config` | separate contract; procedure not established |
| `packages/plan-fields/src/plan_fields/contract` | its own mechanisms — offline integrity in `dispatcher/core/contracts.py` plus scheduled `upstream-drift.yml`; see the README's "Two contract guarantees" section |

The four surfaces have different pin formats, checks and sources. A single
unified procedure would be a false abstraction until at least the snapshot
contract is migrated — that is a project, not a documentation change.
````

- [ ] **Step 2: Link the runbook from `README.md`**

Insert after the `uv run python scripts/upstream_drift_report.py …` code block in the "Two contract guarantees" section (`README.md:283-288`):

```markdown
The other vendored contract, `contracts/github-checker-actions/v1`, is pinned
to a producer commit rather than a canon tree, and has no drift watcher of its
own — nothing announces that github-checker moved. Moving its pin is a manual,
single-input procedure: `docs/revendor-github-checker-actions.md`.
```

- [ ] **Step 3: Record the missing drift signal in `TODO.md`**

Add under «Наблюдения (работу не начинаем, пока не сработает триггер)», in the same style as the neighbouring items:

```markdown
- [ ] Для `contracts/github-checker-actions/v1` нет drift-сигнала: о том, что продюсер уехал, узнаём только вручную @owner:andrei @trigger:"actions/v1 изменился в github-checker и это заметили постфактум" @id:actions-v1-no-drift-signal
      У plan-fields две гарантии — offline integrity в PR-гейте и scheduled
      `upstream-drift.yml`; у actions/v1 только первая, и она по построению
      останется зелёной сколько угодно долго после того, как канон уехал.
      Симметричный advisory-workflow против `github-checker/contracts/actions/v1`
      — правильное решение, но это отдельная поверхность (расписание, права,
      коды 0/1/2, поведение при недоступном upstream), и в документационный PR
      она не входила осознанно. Процедура ре-вендоринга уже написана:
      `docs/revendor-github-checker-actions.md`.
```

And under the open work, an item this PR closes:

```markdown
- [ ] Ре-вендоринг actions/v1 — воспроизводимая процедура: runbook + скрипт с одним входом @owner:andrei @id:revendor-actions-runbook
      Процедура жила только в историческом плане `2026-07-31-vendor-actions-v1.md`,
      а пин правился руками в трёх местах: согласованная правка всех трёх
      оставляла сьют зелёным, заверяя новые байты старым коммитом.
```

- [ ] **Step 4: Verify the runbook's claims against the code**

Run each command the runbook states, and confirm each cross-reference resolves:

```bash
scripts/revendor_github_checker_actions.sh 2>&1 | head -3   # usage, exit 1
ls docs/superpowers/plans/2026-07-31-vendor-actions-v1.md
ls contracts/github-checker-snapshot/v1 contracts/executor-config
grep -n "PRODUCER_COMMIT = " tests/test_contract_ingest.py
grep -n "actions-v1-no-drift-signal" TODO.md
```

Expected: all resolve. A runbook that names a path that does not exist is worse than no runbook — it is read once, disbelieved, and abandoned.

- [ ] **Step 5: Commit**

```bash
git add docs/revendor-github-checker-actions.md README.md TODO.md
git commit -m "docs: a canonical runbook for re-vendoring actions/v1"
```

---

### Task 4: Full gate and PR

**Files:**
- Modify: `TODO.md` (close the runbook item with the PR number)

**Interfaces:**
- Consumes: everything above.

- [ ] **Step 1: Run the whole suite with the pinned binary**

Run:

```bash
PATH="$(scripts/install_pinned_checker.sh):$PATH" uv run pytest tests/ -v
```

Expected: PASS, including `test_write_path_live_smoke_real_binary` and
`test_task_authoring_js.py` (Node 22 on PATH — neither may skip).

- [ ] **Step 2: Lint, format and typecheck one more time**

Run: `uv run ruff format --check . && uv run ruff check . && uv run pyrefly check`
Expected: clean.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin docs/revendor-actions-runbook
gh pr create --title "docs+tooling: repeatable re-vendoring for actions/v1" --body "$(cat <<'BODY'
The re-vendor procedure lived only in the historical implementation plan, and
the pin was hand-edited in three places. Three literals changed together and
wrongly leave the suite green while the manifest certifies bytes from a
different commit — the guarantee is byte-provenance, not literal agreement.

- `scripts/revendor_github_checker_actions.sh <NEW_PIN> [--from <repo>]` —
  the SHA is the only input. Fetches into a throwaway bare object store,
  extracts from the object database into a staging directory, verifies every
  staged file against the commit's blobs and the file set both ways, writes
  `PINNED.txt`, regenerates the manifest, re-verifies, and only then swaps.
  Same-filesystem rename with a restoring trap: a failure anywhere leaves the
  working copy untouched.
- `scripts/vendor_manifest.py` — the pin is `--producer-commit` now; the
  hardcoded literal is gone.
- `docs/revendor-github-checker-actions.md` — the canonical runbook, plus the
  matrix of what it deliberately does not cover.
- The literal in `tests/test_contract_ingest.py` stays a hand edit on purpose:
  it is the independent assertion, and a test reading the value it checks
  proves nothing.

Not in scope, recorded as debt (`actions-v1-no-drift-signal`): actions/v1 has
no upstream-drift watcher, so nothing announces that the producer moved.
BODY
)"
```

- [ ] **Step 4: Close the TODO item with the PR number**

Edit the `revendor-actions-runbook` item to `- [x]` with the PR number appended, matching the file's convention, then:

```bash
git add TODO.md
git commit -m "docs(todo): close the re-vendor runbook item with its PR number"
git push
```

- [ ] **Step 5: Read the GitHub Copilot review**

Run: `gh pr view --comments`

Fix valid findings with new commits on the same branch; answer invalid ones with reasoning rather than applying them blind. Iterate until nothing is open. **Do not merge** — the user merges.

---

## Self-Review

**Spec coverage.** actions-only scope → Task 3's matrix. NEW_PIN as the sole input → Task 2 steps 1/3, plus Task 1 removing the generator's literal. Temp object store from PRODUCER_URL by default → Task 2 step 3. `--from` as an honestly-marked offline mode, git-repo not directory, `cat-file -e`, `git archive` only, no working-tree read, no clean-status requirement → Task 2 step 3 and its tests. Staging model with the swap last → Task 2 step 3, tested by `_assert_untouched`. Tests of the orchestration script itself, all six named cases → Task 2 step 5. Independent literal stays manual → Task 3 runbook §3. Live binary and the full gate as runbook steps, not script steps → Task 3 runbook §4 and Task 4. Drift-signal debt recorded → Task 3 step 3. Historical plan untouched → no task modifies it. No `docs/superpowers/specs/` file → none created.

**Placeholder scan.** No TBD/TODO markers, no "handle errors appropriately", no "similar to Task N". Every code step carries its content.

**Type consistency.** `build_manifest(root, producer_commit)` is defined in Task 1 and called nowhere else in Python; the script calls only the CLI. `verify_provenance` takes `exact` | `with-meta` in both call sites. Exit codes 1/2/3/4 are asserted in Task 2's tests with the same meanings the runbook documents in Task 3. `SCRIPT_NAME`, `_vendored`, `_assert_untouched` are defined in Task 2 step 1 and used in step 5.

**One residual gap, stated rather than papered over:** the pre-generation `verify_provenance exact` call has no test of its own — forcing a mismatch there would mean shimming `git archive`, and a shim that fakes the extraction is a test of the shim. It shares its implementation with the post-generation call, which *is* tested (`test_a_generator_that_corrupts_the_surface_is_caught`), and it must pass on every successful run. The network path is untested for the same reason: mocking the one step whose value is that it really contacts the canonical remote would assert nothing.
