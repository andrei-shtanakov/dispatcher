#!/usr/bin/env bash
# Extract the impresario mirror at the vendored pin for the live smoke.
#
# The pin is READ from all five vendored manifests, which must agree — a
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
# The printed directory outlives this script — the caller owns its lifetime
# (the test above reads it after we exit). Only the bare object store we
# create for a network fetch is cleaned up on exit; a caller-supplied
# --from store is never touched.
#
# Exit: 0 ok · 1 usage · 2 source or commit unavailable ·
#       3 provenance failure (manifest disagreement, unreadable/malformed
#       manifest, or PP-101 absent)
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
PINS="$(python3 - "$REPO_ROOT" << 'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1]) / "contracts"
names = (
    "impresario-product-proposal",
    "impresario-gate-decision",
    "impresario-loop-state",
    "impresario-ranked-backlog",
    "impresario-loop-resume-decision",
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
[ "${#PIN_LIST[@]}" -eq 5 ] ||
  die 3 "could not read producer_commit from the vendored manifests: $PINS"
for pin in "${PIN_LIST[@]}"; do
  [[ "$pin" =~ ^[0-9a-f]{40}$ ]] ||
    die 3 "not a full 40-hex producer_commit: $pin"
  [ "$pin" = "${PIN_LIST[0]}" ] ||
    die 3 "the five manifests disagree on producer_commit: ${PIN_LIST[*]}"
done
PIN="${PIN_LIST[0]}"

WORK="$(mktemp -d)"
if [ -n "$FROM" ]; then
  FROM="$(cd "$FROM" && pwd)" || die 2 "--from path does not exist"
  STORE="$FROM"
else
  STORE="$WORK/store"
  trap 'rm -rf "$STORE"' EXIT
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
