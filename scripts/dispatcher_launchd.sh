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
ROTATE_LABEL="dev.atp.dispatcher.logrotate"
SNAPSHOT_LABEL="dev.atp.dispatcher.snapshot"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
ROTATE_PLIST="$HOME/Library/LaunchAgents/$ROTATE_LABEL.plist"
SNAPSHOT_PLIST="$HOME/Library/LaunchAgents/$SNAPSHOT_LABEL.plist"
#: bin dir of the pinned github-checker (scripts/install_pinned_checker.sh);
#: publish-snapshot needs it on PATH, and launchd inherits no shell PATH.
CHECKER_BIN="$HOME/.local/share/dispatcher-pinned-checker/bin"
ROTATE_MAX_BYTES="${DISPATCHER_LOG_MAX_BYTES:-10485760}"   # 10 MiB
ROTATE_KEEP=5
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

# Rotate by COPY-TRUNCATE, not rename.
#
# launchd opens StandardOutPath/StandardErrorPath once and holds the
# descriptor for the life of the service. Renaming the file — which is what
# `newsyslog`, the obvious platform answer, does — leaves the service writing
# into the *rotated* file while the fresh one stays empty forever. Measured,
# not assumed: with a held descriptor, `rename` then `write` put the bytes in
# `app.log.1` and left `app.log` at zero. That is rotation which looks
# configured and does nothing, and it would be discovered by a full disk.
#
# Copying the content aside and truncating the SAME inode keeps the
# descriptor valid: the service goes on writing, into a file that now starts
# empty. The cost is a window between copy and truncate in which a write can
# be lost; for a service log that is the right trade against losing the whole
# file to a rename that silently detaches it.
rotate_logs() {
    local f archived=0
    # EVERY log this directory grows, not a hand-kept pair (codex on #196):
    # the snapshot agent's log was added and the rotator still named only
    # out/err — the exact slow-full-disk failure rotation exists to stop,
    # reintroduced by the very PR that added a new writer. The glob keeps
    # the next new agent covered without anyone remembering this loop.
    for f in "$LOG_DIR"/*.log; do
        [ -f "$f" ] || continue
        local size
        # BSD stat first (the launchd host is macOS), GNU stat as the
        # fallback — CI exercises this rotator on Linux, where `-f%z`
        # is an unknown option and `set -e` turned it into exit 1.
        size="$(stat -f%z "$f" 2>/dev/null || stat -c%s "$f")"
        [ "$size" -ge "$ROTATE_MAX_BYTES" ] || continue
        local i
        for (( i = ROTATE_KEEP - 1; i >= 1; i-- )); do
            [ -f "$f.$i" ] && mv "$f.$i" "$f.$((i + 1))"
        done
        cp "$f" "$f.1"
        : > "$f"
        archived=$((archived + 1))
        echo "rotated $f ($size bytes)" >&2
    done
    [ "$archived" -gt 0 ] || echo "nothing to rotate (both under $ROTATE_MAX_BYTES bytes)" >&2
}

# A companion agent, not a cron line and not root. `newsyslog` would need
# /etc/newsyslog.d and sudo, and — see `rotate_logs` — would rotate by rename,
# which does not work on a descriptor launchd holds open. A user-level agent
# calling this same script keeps the whole mechanism inside the repo, visible
# and testable by the person who installs it.
generate_rotate_plist() {
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$ROTATE_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$REPO_ROOT/scripts/dispatcher_launchd.sh</string>
    <string>rotate</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_ROOT</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>17</integer></dict>
  <key>StandardOutPath</key><string>$LOG_DIR/rotate.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/rotate.log</string>
</dict>
</plist>
PLIST
}

# Cross-machine sync only means anything when EVERY machine publishes at
# most an hour apart (README, "Sync snapshots") — a screen fed by one stale
# snapshot renders "unknown" per repo and looks broken while telling the
# truth. This agent is that publisher. The pinned github-checker's bin dir
# goes onto PATH explicitly: launchd starts with no shell profile, so "on
# PATH" must be arranged here, not assumed.
generate_snapshot_plist() {
    local cfg="$1"
    cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$SNAPSHOT_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(command -v uv)</string>
    <string>run</string>
    <string>dispatcher</string>
    <string>publish-snapshot</string>
    <string>--config</string>
    <string>$cfg</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO_ROOT</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$CHECKER_BIN:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>StartInterval</key><integer>1800</integer>
  <key>StandardOutPath</key><string>$LOG_DIR/snapshot.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/snapshot.log</string>
</dict>
</plist>
PLIST
}

# bootout is asynchronous: an immediate bootstrap of the same label can hit
# "Bootstrap failed: 5: Input/output error" while launchd tears the old
# instance down. ONE retry helper for every agent (codex on #196): the main
# service had the retry, the snapshot agent reused bootout/bootstrap without
# it, and under set -e one transient aborted the whole install with the
# publisher left unloaded — the same lesson, unlearned one call site over.
bootstrap_with_retry() {
    local label="$1" plist="$2" consequence="$3"
    local attempt
    for attempt in 1 2 3 4 5; do
        if launchctl bootstrap "gui/$UID" "$plist" 2>/dev/null; then
            return 0
        fi
        [ "$attempt" = 5 ] && die "bootstrap of $label kept failing — $consequence; try: launchctl bootstrap gui/$UID $plist"
        sleep 1
    done
}

install_agent() {
    local cfg port holder
    cfg="$(resolve_config "${1:-}")"
    port="$(config_port "$cfg")"

    # Refuse rather than fight for the port. A KeepAlive agent losing a bind
    # race restarts forever, and the operator sees a service that is "loaded"
    # and unreachable — worse than a refusal that says what to stop.
    if holder="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null)" && [ -n "$holder" ]; then
        # OUR OWN service does not count as a conflict: `install` is also
        # the upgrade path, and the bootstrap below replaces the running
        # process anyway. Refusing here made re-install impossible without
        # a downtime `uninstall` first — discovered by hitting it while
        # upgrading a live service. Anything else on the port still dies.
        # launchd's pid is the `uv run` WRAPPER; the port is held by its
        # python CHILD — so equality alone never matches (found by running
        # it, not by reading it). Walk a few parents up from the holder.
        # `|| true`: with the job not loaded `launchctl print` fails, and
        # under set -euo pipefail the bare assignment aborted install with
        # NO message at all (review on #196) — an absent own job just means
        # own="" and any listener is foreign.
        own="$(launchctl print "gui/$UID/$LABEL" 2>/dev/null | awk '/^\tpid = /{print $3}' || true)"
        # A match requires a LITERAL, non-empty PID: own="" (job loaded but
        # currently pid-less) meeting a parent walk that bottoms out empty
        # used to compare "" == "" and adopt a FOREIGN listener as ours.
        matched=false
        probe="$holder"
        for _ in 1 2 3; do
            [ -n "$probe" ] || break
            if [ -n "$own" ] && [ "$probe" = "$own" ]; then
                matched=true
                break
            fi
            probe="$(ps -o ppid= -p "$probe" 2>/dev/null | tr -d ' ' || true)"
        done
        if [ "$matched" != true ]; then
            die "port $port is already served by PID $holder — stop it first, or set a different \`port\` in $cfg"
        fi
    fi

    mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"
    generate "$cfg" > "$PLIST"
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    # bootout is asynchronous: an immediate bootstrap of the same label can
    # hit "Bootstrap failed: 5: Input/output error" while launchd is still
    # tearing the old instance down — which left the service DOWN after an
    # upgrade, the exact opposite of what install is for. Found live.
    bootstrap_with_retry "$LABEL" "$PLIST" "service is NOT running"

    # Installed together on purpose: a service whose logs grow without bound
    # is a slow failure, and one installed separately is one forgotten.
    generate_rotate_plist > "$ROTATE_PLIST"
    launchctl bootout "gui/$UID/$ROTATE_LABEL" 2>/dev/null || true
    bootstrap_with_retry "$ROTATE_LABEL" "$ROTATE_PLIST" "log rotation is NOT running"

    if [ -x "$CHECKER_BIN/github-checker" ]; then
        generate_snapshot_plist "$cfg" > "$SNAPSHOT_PLIST"
        launchctl bootout "gui/$UID/$SNAPSHOT_LABEL" 2>/dev/null || true
        bootstrap_with_retry "$SNAPSHOT_LABEL" "$SNAPSHOT_PLIST" "the snapshot publisher is NOT running"
        echo "installed $SNAPSHOT_LABEL -> every 30 min" >&2
    else
        # Absent is a stated fact, not a silent skip: without the publisher
        # the Sync screen on OTHER machines shows this host as missing. It is
        # also the FINAL installed state: an older loaded agent must not keep
        # firing after its checker disappeared while `install` claims it was
        # skipped (codex review on PR #196).
        launchctl bootout "gui/$UID/$SNAPSHOT_LABEL" 2>/dev/null || true
        rm -f "$SNAPSHOT_PLIST"
        echo "SKIPPED $SNAPSHOT_LABEL: no pinned github-checker at $CHECKER_BIN" >&2
        echo "  run scripts/install_pinned_checker.sh \"\$HOME/.local/share/dispatcher-pinned-checker\" first" >&2
    fi

    echo "installed $LABEL -> port $port, logs in $LOG_DIR" >&2
    echo "installed $ROTATE_LABEL -> daily, keeps $ROTATE_KEEP x $ROTATE_MAX_BYTES bytes" >&2
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
    if launchctl print "gui/$UID/$ROTATE_LABEL" >/dev/null 2>&1; then
        echo "rotation: $ROTATE_LABEL loaded ($ROTATE_KEEP x $ROTATE_MAX_BYTES bytes)"
    else
        echo "rotation: NOT loaded — logs will grow without bound"
    fi
    if launchctl print "gui/$UID/$SNAPSHOT_LABEL" >/dev/null 2>&1; then
        echo "snapshot: $SNAPSHOT_LABEL loaded (every 30 min); last:"
        tail -1 "$LOG_DIR/snapshot.log" 2>/dev/null | sed 's/^/  /' || true
    else
        echo "snapshot: NOT loaded — other machines see this host as missing"
    fi
}

uninstall() {
    launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true
    launchctl bootout "gui/$UID/$ROTATE_LABEL" 2>/dev/null || true
    launchctl bootout "gui/$UID/$SNAPSHOT_LABEL" 2>/dev/null || true
    rm -f "$PLIST" "$ROTATE_PLIST" "$SNAPSHOT_PLIST"
    echo "removed $LABEL, $ROTATE_LABEL and $SNAPSHOT_LABEL (logs in $LOG_DIR are left alone)" >&2
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
    rotate)   rotate_logs ;;
    install)  install_agent "$cfg" ;;
    status)   status ;;
    uninstall) uninstall ;;
    *) die "usage: $(basename "$0") generate|install|status|uninstall|rotate [--config PATH]" ;;
esac
