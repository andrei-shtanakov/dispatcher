# Dispatcher Monitor

Ecosystem monitoring sidebar for VS Code over the
[dispatcher](https://github.com/andrei-shtanakov/dispatcher) HTTP API — the
read plane of the polyrepo workspace. The extension only *shows* state and
*launches PR-only actions* on an explicit click; it never writes to a
watched repo's working tree.

## Features

- **Dispatcher activity-bar container** with four views, refreshed on a
  poll interval:
  - **Projects** — every discovered project with health/error state; a
    context action opens the per-project onboarding document.
  - **Errors** — collector and project errors, with a command to show the
    full error body.
  - **Roadmap** — the roadmap read-model (phases, blockers, drift).
  - **Sync** — per-repo sync verdicts with inline **Pull** / **Open PR**
    actions, plus auto-discovered repo proposals with **Track** / **Ignore**.
- **Status-bar item** summarising the fleet-wide sync verdict at a glance.
- **Project onboarding document** (`Dispatcher: Project Onboarding`) — a
  rendered markdown overview of one project: plan state, governance,
  product-proposal gates (`gate_waiting` / `needs_human`), and sync facts.
- **Spec-runner config editor** (`Dispatcher: Edit Spec-Runner Config`) —
  edits go through the server's `propose-pr` write path: a branch and PR in
  the target repo, zero writes to the live tree.
- **Server auto-start** — if the configured URL is unreachable and
  `dispatcher.projectDir` is set, the extension spawns
  `uv run dispatcher serve` for you.

All mutations (pull, open PR, config edit) are whitelist actions executed
by the dispatcher server via `github-checker`, gated on an explicit human
click in the UI.

## Requirements

- A running dispatcher server (`uv run dispatcher serve`), or
  `dispatcher.projectDir` pointing at a dispatcher checkout so the
  extension can start one.
- VS Code `>= 1.90`.

## Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `dispatcher.url` | `http://127.0.0.1:8787` | Base URL of the dispatcher HTTP API. |
| `dispatcher.projectDir` | `""` | Path to the dispatcher repo, used to spawn `uv run dispatcher serve`. Empty disables auto-start. |
| `dispatcher.autoStart` | `true` | Spawn the server when the URL is unreachable (requires `projectDir`). |
| `dispatcher.pollSeconds` | `10` | Refresh interval in seconds (minimum 5). |

## Development

```bash
npm install
npm run typecheck   # tsc --noEmit
npm run test        # vitest
npm run build       # esbuild -> dist/extension.js
npm run package     # all of the above + vsce package
```

The extension is private to this workspace and installed from the built
`.vsix`; it is not published to the marketplace.
