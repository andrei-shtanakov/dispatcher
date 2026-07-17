# Dispatcher

Primarily a read-only monitoring dashboard for the AI-orchestrators ecosystem
(atp-platform, Maestro, arbiter, spec-runner, proctor). Reads on-disk
artifacts directly — monitored projects don't need to be running or even
installed; missing ones simply don't show up. The only mutations are a
narrow, human-click-gated, PR-only whitelist (sync `pull`/`create-pr` +
spec-runner config editor, all delegated to `github-checker`; dispatcher
itself never pushes or merges).

## Run

    uv run dispatcher serve            # http://127.0.0.1:8787
    uv run dispatcher serve --port 9000 --config /path/dispatcher.toml

Port precedence: the CLI `--port` flag overrides the config file's `port`,
which overrides the default 8787.

### Terminal UI

    uv run dispatcher tui                     # tabs: Sync / Projects / Errors / Models / Contracts / Roadmap / Config
    uv run dispatcher tui --config dispatcher.toml

Keys: `r` refresh · `a` toggle errors 14d/all · `e` errors for selected
project · `p` pull · `o` open PR · `t`/`i` track/ignore (Sync) · `Enter` edit config
(Config) · `ctrl+d` diff · `ctrl+y` confirm · `Esc` back · `q` quit. Auto-refresh: 10 s.

### VSCode extension

    cd vscode-ext && npm install && npm run package   # builds .vsix

Install via "Extensions: Install from VSIX…". Adds a Dispatcher sidebar
(projects + recent errors) and a status-bar health indicator; the server
is auto-started when unreachable (`dispatcher.projectDir` setting must
point at this repo). Settings: `dispatcher.url`, `dispatcher.projectDir`,
`dispatcher.autoStart`, `dispatcher.pollSeconds`.

## Configure (optional `dispatcher.toml`)

    roots = ["/Users/you/labs/all_ai_orchestrators"]
    maestro_db = "~/.maestro/maestro.db"
    port = 8787

Without a config, dispatcher scans its own parent directory (monorepo
layout). Standalone installs must list `roots` explicitly.

## Sync snapshots (per-machine cron)

    uv run dispatcher publish-snapshot               # snapshot → KB → commit+push
    uv run dispatcher publish-snapshot --no-push     # local commit only (testing)

Publishes this host's workspace state (via `github-checker snapshot`,
must be on PATH) to `prograph-vault/derived/snapshots/<host>.json` —
the KB tool zone (prograph-vault#24). Cross-machine sync verdicts need
this running on **every** machine at most an hour apart; any failure
exits non-zero so a dead job is visible in cron mail / launchd logs.

crontab (every 30 min):

    */30 * * * * cd /path/to/dispatcher && uv run dispatcher publish-snapshot

macOS launchd: a `LaunchAgent` with `StartInterval` 1800 running the
same command works; staleness beyond 1 h renders the host's panel as
`stale` on the Sync screen rather than failing anything.

## API

`/api/overview`, `/api/projects/{name}`, `/api/errors?limit=N`,
`/api/models`, `/api/contracts`,
`/api/work-items?cross_only=bool&limit=N`,
`/api/roadmap`, `/api/roadmap/{item_id}`,
`/api/projects/{name}/spec-runner-config`, `/api/actions/update-spec-runner-config`
— pydantic-typed JSON; this is the same contract the future VSCode extension consumes.

`/api/work-items` is the read-side correlation view: tasks from all
projects grouped by their shared task id (Maestro passes `task.id`
verbatim to arbiter's `route_task`), with `pipeline_id` links scavenged
from Maestro session logs. Statuses stay in each project's local
vocabulary — this is a lossy drill-down view, not a semantic mapping.

`/api/roadmap` renders human-authored roadmap intent
(`prograph-vault/authored/roadmaps/*.yaml`, override with
`roadmap_dirs` in dispatcher.toml) as computed status — never manual
ticks. Evidence is a closed set of typed rules (`project_detected`,
`file_exists`, `sqlite_has_row`, `contract_in_sync`,
`work_item_chain`); items whose evidence is not expressible with these
rules stay `unknown`. Status ladder: `planned / implemented / verified
/ unknown`, plus `blocked` when a `depends_on` item is not
implemented+.

## Design

See `docs/superpowers/specs/2026-07-03-dispatcher-design.md` (Stage 1) and
`docs/superpowers/specs/2026-07-05-dispatcher-tui-design.md` (Stage 2, TUI).
