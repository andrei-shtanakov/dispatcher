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

## Configure (optional `dispatcher.toml`)

    roots = ["/Users/you/labs/all_ai_orchestrators"]
    maestro_db = "~/.maestro/maestro.db"
    port = 8787

Without a config, dispatcher scans its own parent directory (monorepo
layout). Standalone installs must list `roots` explicitly.

## API

`/api/overview`, `/api/projects/{name}`, `/api/errors?limit=N`,
`/api/models`, `/api/contracts` — pydantic-typed JSON; this is the same
contract the future TUI and VSCode extension consume.

## Design

See `docs/superpowers/specs/2026-07-03-dispatcher-design.md`.
