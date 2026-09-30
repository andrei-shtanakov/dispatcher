# "My turn" in VSCode — slice B1

**Status:** 2026-09-30, draft. Spec and code travel in one PR (owner decision
2026-09-30: lighter process for this slice, to land within half a day).
**Parent:** `docs/superpowers/specs/2026-09-29-human-control-plane-design.md` §4
(slice B). **Consumes:** `GET /api/human-queue`
(`docs/superpowers/specs/2026-09-29-human-queue-a1-design.md`).

## 1. Scope

B1 renders the human queue in the `dispatcher-monitor` VSCode extension: one tree view
and one status-bar item. Nothing new on the server.

| Deferred | Where it goes | Why |
|---|---|---|
| Web tab | B2 | The web shell has a closed tab registry and a loader protocol with its own harness contract (`tests/web/tabs_harness.js`); it is a slice of its own |
| TUI tab | B2 | The TUI already lags behind newer views (no Waits, Epics, Launchpad tabs) |
| Executing any act | never in B | Parent §4 and owner decision 4: commands are prepared, never executed by the extension |

This narrows the parent's "three surfaces, identical words"; the deviation is recorded
here and in `TODO.md`.

## 2. The view `dispatcherMyTurn` ("My turn")

Root nodes, in order:

1. **Completeness banner** — present when `complete` is false: one node
   "queue incomplete", its children one line per source whose state is neither `ok`
   nor `not_configured` (`<source>: <state> — <detail>`). A2 closes the two
   `not_connected` forge lines; until then the banner is always there, by design.
2. **One group per reason**, in the fixed order `launch_unknown`, `run_needs_review`,
   `run_awaiting_approval`, `loop_needs_human`, `proposal_gate`, `backlog_gate`; only
   non-empty groups appear. A wait is placed by `reasons[0]` (the server sorts
   `reasons`); its description lists every reason when there are several. Inside a
   group the server's order is kept (known ages oldest first, then unknown ages).
3. **Empty states** — no waits and complete: "nothing waits for you"; no waits and
   incomplete: "no known waits — queue incomplete" (never a reassuring empty list
   while a source is unread).

Wait item: label = `title`; description = age ("3h", "2d", or "age unknown") and
`repo`; tooltip = key, `since_basis`, and the prepared act. Overdue waits carry a
warning icon.

Failure states: server offline → the shared offline node; `/api/human-queue`
unreachable or failing while the server is up (e.g. an older server) → one node
"human queue unavailable: <detail>". Unknown never renders as zero.

## 3. Acts — prepared, never executed

Clicking a wait runs `dispatcher.myTurnAct`, mapped from the typed `act`:

| `act.kind` | What the extension does |
|---|---|
| `run_view` | Opens `<dispatcher.url>/#launchpad/<request_id>` in the browser — the run view where `/resolve` and the verbs live |
| `maestro_verb` | Opens a terminal named `maestro · <repo_key>` with `maestro <verb> <task_id> --run <run_id>` typed in and **not** executed, and says that it must run from a checkout of `<repo_key>` (maestro resolves the run's repository from its cwd — slice-0 finding) |
| `open_artifact` | Opens the file (or reveals the folder) under the observed impresario mirror's path when the overview knows it; otherwise copies the mirror-relative path to the clipboard and says so |

No act writes anything, calls a POST endpoint, or runs a process.

## 4. Status bar

A second status-bar item next to the existing one:

- `⏳ N` — N = number of waits (unique by key, as served);
- `⏳ N · ?` — when `complete` is false;
- `⏳ ?` — when the queue could not be read;
- error background when any wait with a known age is older than
  `dispatcher.myTurnOverdueHours` (new setting, default 24 — parent §8 decision 5).
  Overdue and incomplete are independent and can show together.

Clicking it focuses the view.

## 5. Structure

- `vscode-ext/src/myTurn.ts` — pure, vscode-free: types' consumers, grouping, age,
  overdue, status text, banner lines, act → prepared action. Pinned by vitest.
- `vscode-ext/src/myTurnView.ts` — the tree provider and the status item: thin
  adapters over `myTurn.ts`.
- `vscode-ext/src/api.ts` — the wire types and `humanQueue()`.
- `vscode-ext/src/extension.ts` — poll wiring and the act command.
- `vscode-ext/package.json` — view, command, setting.

## 6. Acceptance (vitest on `myTurn.ts`)

1. Status text: `⏳ 3`, `⏳ 3 · ?` when incomplete, `⏳ ?` when unread.
2. Overdue: a wait older than the threshold is overdue; a wait with `since: null` is
   never overdue; the threshold is honoured exactly.
3. Grouping follows the fixed reason order and keeps server order inside a group.
4. Banner lists exactly the sources that are neither `ok` nor `not_configured`.
5. Empty + incomplete never produces the "nothing waits" text.
6. Act mapping: run_view → URL with `#launchpad/<request_id>` (trailing slash on the
   base URL tolerated); maestro_verb → the exact command line, never containing a
   newline; open_artifact → joined path when the mirror is known, clipboard fallback
   otherwise.
