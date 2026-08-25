#!/usr/bin/env bash
# Generate, install, inspect and remove a launchd agent for `dispatcher serve`.
#
# Why a GENERATOR and not a committed plist: every path a launchd agent needs
# is absolute and machine-specific — the repo, the `uv` binary, the config,
# the log files. A committed plist would be one machine's paths presented as
# a universal artifact, and the first person to copy it would get a service
# that silently fails to start. Generating it means the paths are DERIVED
# here, in front of the reader, from things that can be checked.
#
# What is deliberately NOT in the plist:
#
#   * `ATP_CATALOG`. It lives in dispatcher.toml (PR #176) and the controller
#     passes the CONFIGURED value to the maestro child, overriding anything
#     ambient. Repeating it here would create a second source of truth, and
#     the two would drift. This is not a stylistic point: the first pilot run
#     died two seconds after a receipt that said "started" precisely because
#     that value lived in an environment instead of a config.
#   * Any secret. A plist is world-readable and ends up in backups.
#
# launchd does not inherit an interactive shell's PATH, so `uv` is invoked by
# absolute path and `WorkingDirectory` is set to the repo — without it `uv run`
# cannot find the project.
#
# Usage:
#   scripts/dispatcher_launchd.sh generate [--config PATH]   # print the plist
#   scripts/dispatcher_launchd.sh install  [--config PATH]   # write + load it
#   scripts/dispatcher_launchd.sh status                     # is it running
#   scripts/dispatcher_launchd.sh uninstall                  # unload + remove
set -euo pipefail

LABEL="dev.atp.dispatcher"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/dispatcher"
DEFAULT_CONFIG="$HOME/.config/dispatcher/dispatcher.toml"

die() { echo "error: $*" >&2; exit 1; }

# A launchd agent outlives the shell that installed it; a git worktree does
# not. Running this from one would bake a disposable directory into
# `WorkingDirectory`, and the service would keep restarting into a path that
# no longer exists — installed, "loaded", and permanently broken. Found by
# generating from a worktree and reading the output, not by reasoning about
# it. `--git-dir` differs from `--git-common-dir` exactly inside a worktree.
assert_main_checkout() {
    local gd cd_
    gd="$(cd "$REPO_ROOT" && git rev-parse --absolute-git-dir 2>/dev/null)" || return 0
    cd_="$(cd "$REPO_ROOT" && cd "$(git rev-parse --git-common-dir)" && pwd -P)"
    if [ "$gd" != "$cd_" ]; then
        die "$REPO_ROOT is a git worktree, which is disposable. Run this from the main checkout: $(dirname "$cd_")"
    fi
}

# The port is READ FROM THE CONFIG, never assumed: `port` is optional and
# defaults to 8787 inside dispatcher, so a hardcoded number here would be a
# second copy of a default that can move. `status` needs the real one to
# probe, and `install` needs it to refuse a port that is already taken.
config_port() {
    python3 - "$1" <<'PY'
import re, sys, tomllib
from pathlib import Path
path = Path(sys.argv[1])
data = tomllib.loads(path.read_text()) if path.is_file() else {}
print(data.get("port", 8787))
PY
}

resolve_config() {
    local cfg="${1:-$DEFAULT_CONFIG}"
    [ -f "$cfg" ] || die "config not found: $cfg"
    python3 -c 'import sys,os; print(os.path.abspath(os.path.expanduser(sys.argv[1])))' "$cfg"
}

generate() {
    local cfg uv port
    assert_main_checkout
    cfg="$(resolve_config "${1:-}")"
    uv="$(command -v uv)" || die "uv not on PATH — launchd needs its absolute path"
    port="$(config_port "$cfg")"
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$uv</string>
    <string>run</string>
    <string>dispatcher</string>
    <string>serve</string>
    <string>--config</string>
    <string>$cfg</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/err.log</string>
</dict>
</plist>
PLIST
}

install_agent() {
    local cfg port holder
    cfg="$(resolve_config "${1:-}")"
    port="$(config_port "$cfg")"

    # Refuse rather than fight for the port. A KeepAlive agent losing a bind
    # race restarts forever, and the operator sees a service that is "loaded"
    # and unreachable — worse than a refusal that says what to stop.
    if holder="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null)" && [ -n "$holder" ]; then
        die "port $port is already served by PID $holder — stop it first, or set a different \`port\` in $cfg"
    fi

    mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"
    generate "$cfg" > "$PLIST"
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$UID" "$PLIST"
    echo "installed $LABEL -> port $port, logs in $LOG_DIR" >&2
}

status() {
    [ -f "$PLIST" ] || die "not installed: $PLIST is absent"
    local cfg port
    cfg="$(python3 -c '
import plistlib, sys
with open(sys.argv[1], "rb") as fh:
    args = plistlib.load(fh)["ProgramArguments"]
print(args[args.index("--config") + 1])
' "$PLIST")"
    port="$(config_port "$cfg")"
    launchctl print "gui/$UID/$LABEL" 2>/dev/null | grep -E "^\s+(state|pid) " || die "not loaded"
    echo "config: $cfg"
    echo -n "http: "
    curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:$port/" || echo "unreachable"
}

uninstall() {
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed $LABEL (logs in $LOG_DIR are left alone)" >&2
}

cmd="${1:-}"; shift || true
cfg=""
while [ $# -gt 0 ]; do
    case "$1" in
        --config) cfg="${2:?--config needs a path}"; shift 2 ;;
        *) die "unknown argument: $1" ;;
    esac
done

case "$cmd" in
    generate) generate "$cfg" ;;
    install)  install_agent "$cfg" ;;
    status)   status ;;
    uninstall) uninstall ;;
    *) die "usage: $(basename "$0") generate|install|status|uninstall [--config PATH]" ;;
esac
