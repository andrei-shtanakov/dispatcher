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

if ! python3 - "$STAGING/manifest.json" "$NEW_PIN" << 'PY'
import json, sys
sys.exit(0 if json.load(open(sys.argv[1]))["producer_commit"] == sys.argv[2] else 1)
PY
then
  die 4 "the generated manifest does not record the pin it was given"
fi

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
