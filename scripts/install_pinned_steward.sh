#!/usr/bin/env bash
# Install the steward CLI (gate-check) at exactly the commit the vendored
# gate-verdicts contract is pinned to, into an isolated virtualenv, and print
# that virtualenv's bin directory on stdout.
#
# The commit is READ FROM THE VENDORED MANIFEST, never written here. A second
# copy of the pin is a second thing to forget: the schema copy and the live
# emitter must move together or the governance live smoke stops proving
# anything about this contract. Everything except the bin directory goes to
# stderr so callers can do:
#   PATH="$(scripts/install_pinned_steward.sh):$PATH"
#
# Isolated on purpose: merging the producer into dispatcher's own environment
# would mean the suite no longer tests dispatcher's dependency set.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$REPO_ROOT/contracts/steward-gate-verdicts/v1/manifest.json"
PRODUCER_URL="https://github.com/andrei-shtanakov/steward"
VENV="${1:-${TMPDIR:-/tmp}/dispatcher-pinned-steward}"

COMMIT="$(python3 -c '
import json, sys
print(json.load(open(sys.argv[1]))["producer_commit"])
' "$MANIFEST")"

echo "installing steward @ $COMMIT into $VENV" >&2
uv venv --allow-existing "$VENV" >&2
uv pip install --quiet --python "$VENV" "steward @ git+$PRODUCER_URL@$COMMIT" >&2

echo "$VENV/bin"
