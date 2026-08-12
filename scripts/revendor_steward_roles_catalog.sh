#!/usr/bin/env bash
# Re-vendor contracts/steward-roles-catalog/v1 at a new producer commit.
#
# The pin is the ONE input. Everything written — the extracted bytes,
# PINNED.txt, manifest.json — is derived from the SHA on argv, so the
# guarantee is not "three literals agree with each other" but "the bytes on
# disk are, byte for byte, the blobs of the commit the manifest names".
#
# The vendored surface is a single file: steward's profiles/roles.yaml
# lands here as roles.yaml. The staging/verify/swap machinery is the
# same as scripts/revendor_steward_gate_catalog.sh, from which this script
# is an adapted copy (that one is itself adapted from
# scripts/revendor_steward_gate_verdicts.sh).
#
# The working copy is never touched until a fully verified candidate exists:
# extraction and generation happen in a staging directory beside it, and the
# swap is a same-filesystem rename with a restoring trap. A failure anywhere
# leaves the previous vendored copy exactly as it was.
#
# Usage:
#   scripts/revendor_steward_roles_catalog.sh <NEW_PIN> [--from <git-repo>]
#
# Default: fetch NEW_PIN from the canonical producer URL into a throwaway
# bare object store. --from: read it out of an existing local repository's
# object database instead — no working tree is read, no clean `git status`
# is required (an irrelevant check: we extract from objects, not the tree),
# and the report says the canonical remote was NOT consulted.
#
# Exit: 0 ok · 1 usage · 2 source or commit unavailable ·
#       3 provenance mismatch · 4 manifest generation or read-back ·
#       5 internal failure (working copy left as it was found)
#       Any other nonzero status (127, 128, …) is an unexpected internal
#       failure; the trap below has still restored the tree.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PRODUCER_URL="https://github.com/andrei-shtanakov/steward"
SRC_PATH="profiles/roles.yaml"
DST="$REPO_ROOT/contracts/steward-roles-catalog/v1"
STAGING="$DST.staging"
PREV="$DST.prev"

die() { echo "revendor: $2" >&2; exit "$1"; }

NEW_PIN=""
FROM=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from)
      [ $# -ge 2 ] || die 1 "--from needs a path"
      [ -z "$FROM" ] || die 1 "--from may only be given once"
      case "$2" in
        -*) die 1 "--from wants a path, not an option: $2" ;;
      esac
      FROM="$2"
      shift 2
      ;;
    -h | --help)
      # Print the header comment (lines 2..the line before `set -euo
      # pipefail`), bounded by a sentinel rather than a line number so the
      # printed table can never drift from the header it is quoting again.
      awk 'NR>1{if (/^set -euo pipefail/) exit; print}' "${BASH_SOURCE[0]}" >&2
      exit 0
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

WORK="$(mktemp -d)" || die 5 "could not create a scratch directory"

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

# Checked before extraction, not at first use: an absent interpreter should
# land on the documented "manifest generation" code, not on the shell's own
# 127 for a command it never found.
command -v python3 > /dev/null 2>&1 || die 4 "python3 not found on PATH"

# Extract into a fresh staging directory, never over the top of the current
# copy: a file upstream deleted would otherwise survive as ours and be
# certified by the manifest we are about to generate.
rm -rf "$STAGING"
mkdir -p "$STAGING"
# --strip-components=1 is coupled to SRC_PATH having its file one directory
# deep ("profiles/roles.yaml"); it is not derived from it. A future
# SRC_PATH at a different depth must update this number to match. Left
# uncoupled on purpose (same trade-off as the sibling scripts): a mismatch
# is fail-closed — either the file-set check below (exit 3) or `tar` itself
# (exit 2) catches it — so a computed depth would add a moving part for no
# extra guarantee.
git -C "$STORE" archive "$NEW_PIN" "$SRC_PATH" | tar -x --strip-components=1 -C "$STAGING" ||
  die 2 "$NEW_PIN has no $SRC_PATH to extract"

# The guarantee, stated as a check: every staged file IS the commit's blob,
# and the staged set IS the commit's set — in both directions. For this
# contract the commit's set is the one catalog file, and the check states
# that rather than assuming it.
verify_provenance() {
  # $1: "exact" (before our two meta files exist) or "with-meta" (after).
  local mode="$1" rel want got
  # core.quotePath=false: without it, ls-tree C-quotes any non-ASCII byte in
  # a path, while `find` below emits the raw bytes tar wrote — the two lists
  # could never agree, and a perfectly fine non-ASCII filename would be
  # misdiagnosed as a provenance mismatch.
  git -c core.quotePath=false -C "$STORE" ls-tree -r --name-only "$NEW_PIN" \
    -- "$SRC_PATH" |
    sed "s|^profiles/||" | LC_ALL=C sort > "$WORK/want.txt" ||
    die 3 "could not read the tree of $NEW_PIN from $STORE"
  [ -s "$WORK/want.txt" ] ||
    die 3 "$NEW_PIN has nothing at $SRC_PATH to verify against"
  (cd "$STAGING" && find . -type f | sed 's|^\./||' | LC_ALL=C sort) > "$WORK/got.txt"
  if [ "$mode" = "with-meta" ]; then
    grep -vxF -e 'PINNED.txt' -e 'manifest.json' "$WORK/got.txt" > "$WORK/got.meta" || true
    mv "$WORK/got.meta" "$WORK/got.txt"
  fi
  diff "$WORK/want.txt" "$WORK/got.txt" >&2 ||
    die 3 "the staged file set is not the file set of $NEW_PIN"
  while IFS= read -r rel; do
    want="$(git -C "$STORE" rev-parse "$NEW_PIN:profiles/$rel")" ||
      die 3 "could not read the blob $NEW_PIN has at profiles/$rel"
    got="$(git -C "$STORE" hash-object -- "$STAGING/$rel")" ||
      die 3 "could not hash staged $rel"
    [ "$want" = "$got" ] ||
      die 3 "staged $rel is not the blob $NEW_PIN has at profiles/$rel"
  done < "$WORK/want.txt"
}

verify_provenance exact

cat > "$STAGING/PINNED.txt" << EOF
source: steward $SRC_PATH
commit: $NEW_PIN
vendored: $(date -u +%Y-%m-%d)
note: pinned copy (repo-boundaries vendoring, ADR-ECO-003). Do not edit here —
  re-vendor with scripts/revendor_steward_roles_catalog.sh, which derives every
  value in this directory from the one commit it is given. Procedure:
  docs/revendor-steward-roles-catalog.md. Nothing in shipped code may read
  ../steward at run time.
EOF

python3 "$REPO_ROOT/scripts/vendor_manifest.py" \
  --producer-commit "$NEW_PIN" --root "$STAGING" \
  --contract steward-roles-catalog ||
  die 4 "manifest generation failed"

# Checks the manifest's own claims, not merely that it parses: the pin it
# records, that it lists at least one file (a generator emitting
# {"producer_commit": <pin>, "surface": []} would otherwise pass this and
# the second verification pass below, since that pass only checks the files
# the manifest DOES list), and that its file list is exactly the staged set.
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
  die 4 "the generated manifest does not record the pin it was given, is empty, or its surface does not match the staged files"
fi

# Second pass: the generator writes into the staging directory, so it is in a
# position to change the very bytes the first pass approved.
verify_provenance with-meta

# Only now is the working copy touched, and the swap is two renames on one
# filesystem with the trap above standing behind them. Either mv failing is
# an internal failure, not a usage error — the trap restores $DST from
# $PREV if the second rename is what failed.
if [ -e "$DST" ]; then
  mv "$DST" "$PREV" || die 5 "could not move $DST aside to $PREV"
fi
mv "$STAGING" "$DST" || die 5 "could not move the staged copy into $DST"
rm -rf "$PREV"

cat >&2 << EOF
re-vendored $SRC_PATH at $NEW_PIN
  provenance: $PROVENANCE
              $PROVENANCE_NOTE
  files:      $(wc -l < "$WORK/want.txt" | tr -d ' ')
  next:       uv run pytest tests/test_roles_catalog_vendor.py -v
EOF
