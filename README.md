# Dispatcher

Read-heavy dashboard and guarded action surface for the AI-orchestrators
ecosystem (atp-platform, Maestro, arbiter, spec-runner, proctor). It reads
on-disk artifacts directly — monitored projects don't need to be running or
even installed; missing ones simply don't show up. Mutations are limited to a
narrow, human-click-gated, PR-only whitelist (sync `pull`/`create-pr`, the
merge gate, and the spec-runner config editor, all delegated to
`github-checker`; dispatcher itself never talks to the GitHub API, and never
merges without an explicit human click).

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
with projects, recent errors, and a Sync view (row actions: pull / open PR /
track / ignore); status-bar health indicator; the server is auto-started
when unreachable (`dispatcher.projectDir` setting must point at this repo).
"Dispatcher: Edit Spec-Runner Config" command opens a QuickPick flow to
propose spec-runner config changes via PR (github-checker). "Dispatcher:
Project Onboarding" (command palette or a project's context menu) opens a
read-only markdown preview of the FR-04 onboarding screen (description,
roadmap position, next items, live tasks); running it again refreshes the
same document. Settings:
`dispatcher.url`, `dispatcher.projectDir`, `dispatcher.autoStart`,
`dispatcher.pollSeconds`.

## AI suggestions

Dispatcher can propose spec-runner configuration changes using Claude.
The "Suggest values" button in the web dashboard prefills config values
based on project context and peer distributions; you review and edit the
proposal before it becomes a PR.

**Requirements:** `claude` CLI on PATH, or explicitly configured:

    suggest_claude_cli = "/absolute/path/to/claude"

The path must be absolute, and the basename must be exactly `claude`.
This is distinct from the `spec_runner.claude_command` configuration key.

**Secret handling:** The authentication secret (API token) lives in the
Claude CLI's configuration on the same host — it is relocated to the CLI's
isolated storage, not eliminated from your configuration. Dispatcher itself
holds no secrets.

**Cost:** Requests are charged to your Anthropic account.

**Availability:** The dashboard probes `GET
/api/spec-runner-config/suggest-availability` when the config panel loads;
if the `claude` CLI is not available, the "Suggest values" button is
disabled with a tooltip explaining why. If the probe itself fails (e.g. a
network hiccup), the button stays enabled — a click-time 503 still surfaces
an inline error, so the feature degrades honestly either way.

## Config editor

The web dashboard's config editor panel includes an `extra_executor_config`
overlay section with three explicit states: **Preserve** (default, content
hidden; displays only "overlay present (N keys)"), **Edit** (JSON textarea
with live syntax validation blocking invalid input), and **Clear** (removal
with a confirmation warning). Local validation catches syntax errors and
rejects non-object values (arrays, scalars, null); schema validation runs
server-side when you click "Confirm & open PR", returning a 422 error list
if the overlay violates the executor-config contract. The Terminal UI and
VSCode extension keep `extra_executor_config` read-only.

## Merge gate

From a project's detail panel, enter a PR number and open its merge gate —
there is no PR list to click through; the collector's read model carries
GitHub state only as an opaque payload, with no per-repo PR listing endpoint
(see `TODO.md`'s `merge-gate-pr-listing` item for the follow-up that would add
one). Opening the gate reads the PR through `github-checker pr-detail`: title,
head SHA, checks, review threads, changed files, and diff (large PRs show a
truncation warning alongside the partial list). It also shows github-checker's
own **nine-predicate gate** — `open`, `not-draft`, `mergeable`,
`checks-green`, `checks-complete`, `approvals`, `threads-resolved`,
`threads-complete`, `squash-allowed` — and greys out the Merge button when any
predicate fails.

That greyed-out button is a convenience, not the authority: `github-checker`
is what actually decides. Clicking Merge sends the PR's head SHA back as
`--if-head`, and `merge` re-reads the PR and re-evaluates all nine predicates
itself before touching anything — a push, a new red check, or a fresh review
thread between opening the gate and clicking refuses the merge instead of
racing it. A payload the screen cannot fully validate renders as "cannot read
PR" with Merge disabled, never as a half-drawn or falsely-green screen.

A successful click runs `github-checker merge --if-head <sha>` followed by
`post-merge-sync`, holding the repo's action lock across both steps so no
other action can wedge into the gap. The outcome's `merged` field is
**three-valued**, not a boolean: `true` means it merged; `false` means
github-checker read the PR and refused (the gate or the SHA check failed);
`null` means the gate call itself failed — timeout, missing binary,
unparseable output — so whether the PR merged is genuinely unknown. The UI
never reports an unknown outcome as "not merged"; it says "unknown — check
the PR". The composite reports whatever github-checker answered and never
synthesizes a verdict of its own — claiming a merge it was not told about is
the same defect as claiming a non-merge it was not told about. The composite's
`ok` follows the merge step, not the local sync: a merged
PR is finished work even if resyncing the clone afterwards fails, so
`merged=true, local_sync=failed` renders as a warning with a retry button,
never as a failed merge.

## Task authoring

From a project's detail panel, "Create task request" opens a form for a
`slug`, a `title` and free-text `prose` describing what is needed and by
what observable condition it is done. Leaving the slug field re-checks it
against the target repo's inbox (`github-checker issue-lookup`); a taken
slug offers a link to the existing issue instead of letting you file a
second one, and several issues claiming the same slug render as a conflict
for a human to resolve rather than picked between automatically.

**This files an `inbox` issue and nothing else.** Per the ratified
[ADR-ECO-004a](https://github.com/andrei-shtanakov/prograph-vault/blob/master/authored/decisions/2026-07-30-adr-eco-004a-dispatcher-task-authoring.md),
the screen does **not** edit `TODO.md` in any repo, does **not** accept the
request on the owner's behalf, does **not** open an implementation PR, does
**not** run an executor, and stores no task state of its own — a later
slice wanting any of those needs its own amendment. `from:` in the filed
issue is written by the server, never by the form: a client-settable sender
would let a caller forge who the request is "from".

The lookup is a lock-free read and may run while another action is in
flight; the mutating `POST /api/actions/request-task` shares the same
per-repo lock as every other action, so a request that meets an in-flight
one gets an immediate 409 rather than a silent wait. `created` on the
result is **three-valued**, mirroring `merged` above: `true` means the
issue was filed; `false` means it was not (most commonly the idempotent
case — the slug already had one, and the screen re-runs the lookup to
show it); `null` means the create call itself broke (timeout, missing
binary, unparseable output) and whether an issue exists is genuinely
unknown. The screen never renders `null` as "not created", and the only
follow-up it offers on an unknown outcome is Re-check.

Create is forbidden **while the state is unknown, and until a Re-check
succeeds** — not forever. Creating again straight off an unknown outcome is
how a duplicate is born; refusing permanently would strand the operator with
no way forward. So Re-check re-runs the lookup, and what happens next depends
on the answer it gets. A **definite** answer supersedes the unknown state:
the warning is cleared, Re-check is withdrawn, and the fresh verdict stands
on its own — an explicit `matches: []` means Create is available again
(`issue-lookup` runs `gh issue list`, which asks the API directly rather than
the lagging search index, so an explicit empty answer is the best evidence
obtainable), while a match found means the issue is shown and Create stays
disabled. An **indefinite** answer — transport failure, non-`ok` status,
unreadable envelope, a malformed item — changes nothing: the "state unknown"
warning stays up, Re-check stays offered, and Create stays disabled, because
clearing the warning there would delete the very fact the operator clicked
Re-check to resolve. What the screen must never show is the contradiction —
a stale "state unknown" warning sitting beside a live Create button, as
though both described the same moment. The same unknown-vs-empty
distinction applies to the lookup's `matches`: `null` (or an absent or
wrong-typed field) means the inbox could not be read exhaustively, `[]`
means it was read and is confirmed empty, and only the latter with
`ok: true` enables Create.

The screen's client-side rules — the null-vs-`[]` handling above, per-item
validation of the opaque issue payload, the three-valued `created` logic,
and the 4xx-vs-5xx distinction on a failed create (a 5xx must not
re-enable Create, since the request may already have reached
github-checker) — are exercised by a Node harness (`tests/web/`) that
loads the real `index.html` and runs its actual script, invoked as part of
the Python suite (`tests/test_task_authoring_js.py`). Node is a hard
prerequisite of that gate: if `node` is not on PATH, the test **fails**, it
does not skip — CI pins Node 22.

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
`/api/projects/{name}/spec-runner-config`, `/api/spec-runner-configs`,
`/api/projects/{name}/onboarding`,
`/api/actions/update-spec-runner-config`
— pydantic-typed JSON; this is the same contract the VSCode extension consumes.
`/api/spec-runner-config/suggest-availability`,
`POST /api/projects/{name}/spec-runner-config/suggest`, `POST /api/projects/{name}/spec-runner-config/suggest/cancel`
are web-dashboard-only; the VSCode extension does not call them.

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

## MCP server

Agents (robin, Maestro, Claude Code) get the same read API as MCP tools
via `dispatcher mcp --config /path/dispatcher.toml`. Register with:

    claude mcp add dispatcher -- uv run --project /path/to/dispatcher dispatcher mcp --config /path/dispatcher.toml

Exposes 15 read-only tools: `overview`, `project`, `errors`, `models`,
`contracts`, `work_items`, `roadmap`, `roadmap_item`, `roadmap_summary`,
`roadmap_drift`, `roadmap_phases`, `roadmap_blockers`, `sync_status`,
`spec_runner_configs`, `onboarding`. No action tools by design — mutations require a
human click in the UI; `sync_status` never triggers a background fetch
(`start_fetch=False`).

## Design

See `docs/superpowers/specs/2026-07-03-dispatcher-design.md` (Stage 1) and
`docs/superpowers/specs/2026-07-05-dispatcher-tui-design.md` (Stage 2, TUI).
