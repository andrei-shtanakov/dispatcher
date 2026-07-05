# Dispatcher

Read-only monitoring dashboard for the AI-orchestrators ecosystem
(atp-platform, Maestro, arbiter, spec-runner, proctor-a). Reads on-disk
artifacts directly — monitored projects don't need to be running or even
installed; missing ones simply don't show up.

## Run

    uv run dispatcher serve            # http://127.0.0.1:8787
    uv run dispatcher serve --port 9000 --config /path/dispatcher.toml

Port precedence: the CLI `--port` flag overrides the config file's `port`,
which overrides the default 8787.

### Terminal UI

    uv run dispatcher tui                     # tabs: Projects / Errors / Models / Contracts
    uv run dispatcher tui --config dispatcher.toml

Keys: `r` refresh · `a` toggle errors 14d/all · `e` errors for selected
project · `Enter` drill down · `Esc` back · `q` quit. Auto-refresh: 10 s.

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

## API

`/api/overview`, `/api/projects/{name}`, `/api/errors?limit=N`,
`/api/models`, `/api/contracts` — pydantic-typed JSON; this is the same
contract the future VSCode extension consumes.

## Design

See `docs/superpowers/specs/2026-07-03-dispatcher-design.md` (Stage 1) and
`docs/superpowers/specs/2026-07-05-dispatcher-tui-design.md` (Stage 2, TUI).
