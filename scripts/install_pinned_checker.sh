#!/usr/bin/env bash
# Install the github-checker binary at exactly the commit the vendored
# contract is pinned to, into an isolated virtualenv, and print that
# virtualenv's bin directory on stdout.
#
# The commit is READ FROM THE VENDORED MANIFEST, never written here. A second
# copy of the pin is a second thing to forget: the schema copy and the live
# binary must move together or level 3 stops proving anything about this
# contract. Everything except the bin directory goes to stderr so callers can
# do:  PATH="$(scripts/install_pinned_checker.sh):$PATH"
#
# Isolated on purpose: the producer drags in a TUI stack (textual, rich).
# Merging it into dispatcher's own environment would mean the suite no longer
# tests dispatcher's dependency set.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MANIFEST="$REPO_ROOT/contracts/github-checker-actions/v1/manifest.json"
PRODUCER_URL="https://github.com/andrei-shtanakov/github-checker"
VENV="${1:-${TMPDIR:-/tmp}/dispatcher-pinned-checker}"

COMMIT="$(python3 -c '
import json, sys
print(json.load(open(sys.argv[1]))["producer_commit"])
' "$MANIFEST")"

echo "installing github-checker @ $COMMIT into $VENV" >&2
uv venv --allow-existing "$VENV" >&2
uv pip install --quiet --python "$VENV" "github-checker @ git+$PRODUCER_URL@$COMMIT" >&2

echo "$VENV/bin"
