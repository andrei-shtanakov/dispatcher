#!/usr/bin/env bash
# Re-vendor all impresario contracts at one producer commit.
#
# The pin is the ONE input. contracts/impresario-product-proposal/v1,
# contracts/impresario-gate-decision/v1, contracts/impresario-loop-state/v1,
# contracts/impresario-ranked-backlog/v1, and
# contracts/impresario-loop-resume-decision/v1 are always re-vendored together
# from the same SHA, so the five manifests can never name different commits —
# the machine-readable anti-mix guarantee the PR-gate test asserts.
#
# All directories are staged and fully verified before any is touched;
# the swap is same-filesystem renames with a restoring trap. A failure
# anywhere leaves all previous vendored copies exactly as they were.
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
  "contracts/loop-state/v1|contracts/impresario-loop-state/v1|impresario-loop-state"
  "contracts/ranked-backlog/v1|contracts/impresario-ranked-backlog/v1|impresario-ranked-backlog"
  "contracts/loop-resume-decision/v1|contracts/impresario-loop-resume-decision/v1|impresario-loop-resume-decision"
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

# Phase 1: stage + verify + meta for all contracts, touching nothing live.
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
  ALL impresario contract directories from the one commit it is given, so
  their manifests can never name different commits. Nothing in shipped
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
re-vendored all impresario contracts at $NEW_PIN
  provenance: $PROVENANCE
              $PROVENANCE_NOTE
  next:       uv run pytest tests/test_impresario_contracts_vendor.py \\
                tests/test_product_proposals.py -v
EOF
