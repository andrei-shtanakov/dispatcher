# Factory floor, slice C2 — what the agent merged in the last 24 h

**Status:** 2026-09-30. Spec and code in one PR (owner decision 2026-09-30).
**Parent:** `2026-09-29-human-control-plane-design.md` §5 (slice C);
C1 is `2026-09-30-factory-floor-c1-design.md` §3.

## 1. What it shows

`GET /api/factory-floor` gains the PRs merged by the agent's account
(`agent_merge_login`, default `ai-prosto` — ADR-ECO-011: the agent merges from
its own account) in the last `merges_window_hours` (24), newest first:
`agent_merges: [{repo, number, title, url, merged_at}]`, plus
`agent_merge_login` and `merges_window_hours`. Its completeness is the
`agent_merges` source, and it counts toward `complete` like every other source.

The read is github-checker `merged-prs <own checkout> --since <now − 24 h>`
(github-checker#49, actions/v1 re-vendored at 85168e4): one paginated GraphQL
search across the owner — REST search cannot return the merger. The verb
reports every merge with `merged_by`; filtering is dispatcher's policy:

- only `merged_by` equal to the login (case-insensitive — GitHub logins are)
  is the agent's; a human's merge is not listed here;
- `merged_by` null (the account no longer exists) is never claimed for the
  agent;
- `merges` null or `ok: false` — a failed or non-exhaustive search, or
  github-checker not runnable — is `unavailable`, never "the agent merged
  nothing". An empty list from an `ok` read is a real zero.

## 2. Off the request path

The search runs in the background, as the human queue's forge source (A2 §1):
the view is polled every few seconds, a paginated search must not run that
often, and a search on the request path could outlast a client's timeout.
`BackgroundReader` (extracted from `ForgeReader`, which now subclasses it)
serves the last answer at once, refreshes one older than 60 s in ONE
background thread, and before the first answer says `unavailable`
("first merged-prs in progress"), never an empty list. A raising search or a
reader that cannot start its thread is the source's `unavailable`, never a
500. `agent_merge_login` unset (a bare `DispatcherConfig`, or `""` in
`dispatcher.toml`) → `not_configured`, which keeps tests hermetic; a
non-string is a load-time error.

## 3. VSCode

After the run groups, "Merged by <login> · 24h (N)", collapsed; each item is
`<repo>#<n> · <title>`, described by its age, and opens the PR. The section is
shown only when its source is `ok` — an unread section is explained by the
"sources incomplete" banner, not rendered as a zero. The floor's empty text
("nothing is running") is judged by the run sources only, so an unread merges
section does not make an empty floor look uncertain.

## 4. Out of C2

- Other agent acts (issues opened, reviews posted) — no consumer asks yet.
- Human merges in the same window — the verb returns them; not shown until
  there is a question they answer.
