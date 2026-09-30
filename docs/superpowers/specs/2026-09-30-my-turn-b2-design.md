# "My turn", slice B2 — the web tab and the TUI tab

**Status:** 2026-09-30. Spec and code in one PR (owner decision 2026-09-30).
**Parent:** `2026-09-29-human-control-plane-design.md` §4 (slice B: one view, three
surfaces, identical words — FR-06). B1 (VSCode) is `2026-09-30-my-turn-vscode-design.md`.

## 1. One set of words: `prepared` on every wait

B1 built the act's command in TypeScript. A web tab and a TUI tab doing the same in
JS and Python would be three builders of one command line, free to drift. Instead
each `HumanWait` on `/api/human-queue` gains a computed, additive field
`prepared: {kind, text?, url?, note}` (`dispatcher/core/prepared_acts.py`):

| act | `kind` | carries |
|---|---|---|
| `maestro_verb` | `command` | `[MAESTRO_HOME=… ATP_CATALOG=…] <maestro> <verb> <task> --run <run>` |
| `human_merge` | `command` | `sh devtools/human-merge.sh <name> <n> [--expect-head <sha>]` + the PR `url` |
| `run_view` | `link` | `#launchpad/<request_id>` — dispatcher's own run view, page-relative |
| `open_artifact` | `path` | the impresario-mirror-relative path |
| any, with a control character in a field | `refused` | why — a newline in a pasted line would run it |

The command is built from the act's typed fields only, never from titles or bodies;
a value a shell would split or expand is single-quoted. The extension keeps its own
builder (it needs a terminal and local paths), and **one fixture**
(`vscode-ext/test/fixtures/prepared-acts.json`, generated from Python) is checked
by both pytest (`tests/test_prepared_acts.py`) and vitest
(`vscode-ext/test/preparedParity.test.ts`): the same act yields the same command
and the same note on every surface.

## 2. Web: the "My turn" tab

Second in the strip, after Launchpad (hash `#my-turn`, no nested segment). It is a
registry screen like the others: `LOADERS["my-turn"] = loadMyTurn`, its own
generation guard, a `LOAD_*` outcome, refreshed only while active.

- Rows in the server's order — known ages oldest first — then an "age unknown (N)"
  divider and the waits with no proven start. Age buckets as in VSCode (`<1h`, hours
  under 48, then days); the `since` and its basis are the cell's tooltip.
- "What to do": a command as `<code>` text with its note (and the PR link for a
  human merge); a run view as a link; a path as text; a refusal as its reason.
  **Nothing on the page runs anything** — there is no button.
- Links: a PR link only when it is `https://`, a run link only when it is a `#`
  fragment. Escaping stops an attribute break-out, not a `javascript:` URL.
- Unknown is never zero: an incomplete queue lists its missing sources (the same
  `name: state — detail` lines as VSCode) and the empty text is
  "no known waits — queue incomplete"; a failed read says "not read", never
  "nothing waits for you". A wait without `prepared` (an older server) says so.

## 3. TUI: the "My turn" tab

Second, after Sync. Label `My turn · N`, `· ?` when incomplete, `My turn · ?` when
the queue could not be assembled. Same rows, texts and divider as the web tab.
Enter on a row **copies** the command (or path, or run-view fragment) to the
clipboard and says it was not run; a refused act shows its reason. The TUI builds
its own `ForgeReader` (same TTL, same background refresh) so its 10 s refresh never
becomes a GitHub search every 10 s; the queue is read after the snapshot cache and
reuses it.

## 4. Out of B2

- The factory floor (C1/C2) in web and TUI — not asked yet.
- An overdue threshold outside VSCode (a VSCode setting today).
