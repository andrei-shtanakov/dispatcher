# The halt ("stop-crane"), slice D1 — toggle and read-back

**Status:** 2026-09-30. Spec and code in one PR (owner decision 2026-09-30).
**Parent:** `2026-09-29-human-control-plane-design.md` §6. **Producer:** github-checker
`halt-read` / `halt-set` (github-checker#50).

## 0. D0 — what execution showed (2026-09-30, `andrei-shtanakov/halt-sandbox`)

The owner's account was used only in that sandbox repository, by explicit permission.

| # | Check | Result |
|---|---|---|
| 1 | PR merge by `ai-prosto` (write) while the ruleset is active | refused (`gh pr merge`, `--admin`, REST merge 405) |
| 2 | Direct write by `ai-prosto` | refused (contents API 409, `git push` GH013) |
| 3 | GitHub Actions `GITHUB_TOKEN` push | refused (same workflow succeeded before the halt) |
| 4 | Admin (`andrei-shtanakov`) write | passes — the bypass works |
| 5 | Ruleset disabled → `ai-prosto` merge | merged |
| 6 | Read states | `on` / `off` / `missing` / `misconfigured` / `unknown` all produced |
| 7 | Background default-branch writers | none: the runner and criteria-close push feature branches; snapshot publish writes `derived-snapshots` — unaffected by the halt |

Findings that shape D1:

- **A write-level token cannot confirm a halt.** `ai-prosto` sees the ruleset and its
  enforcement, but `bypass_actors: null`. The full read — the one that can say "this is
  the halt, not something like it" — needs the admin profile. dispatcher's service runs
  under the owner's default `gh` profile, which is admin; admission checks under the
  agent (D2) can only use enforcement and their own `current_user_can_bypass`.
- **The dangerous misconfiguration is silent.** A ruleset on the wrong branch reads
  `active` while the default branch has no effective rule; the agent wrote `master`
  under it. The read compares the whole definition, not the flag.
- An `Integration` in the bypass list cannot be added on a personal repository unless
  the app is installed (API 422), so the "copied bypass list" danger was not
  reproduced; the read accepts exactly the admin role and nothing else.

## 1. What dispatcher owns

- **Which repos** — `halt_fleet` in `dispatcher.toml`: workspace directory names, as
  `ActionRunner` resolves them. Explicit on purpose: a halt acts only on listed repos.
  Empty (the default) → the halt is `not_configured` and nothing can be toggled; this
  PR changes nothing on any production repository.
- **The request** — `run_state_dir/halt-requests.jsonl`, append-only: request id, time,
  target (`on`/`off`), scope (`fleet`/`repos`), repos, the fleet at request time, the
  reason, the principal (the dispatcher process's `gh` profile — v1 is single-operator
  and says so), then the same request again with per-repo results. The first line is
  written **before** any repo is touched.
- **Never the effective state.** It is read back from GitHub per repo, every time.

## 2. API

- `GET /api/halt` → `{fleet: [{repo, state, ruleset_id, detail}], applying, halted,
  unhealthy, deviations, last_request, sources, complete, generated_at}`. Read in the
  background (`HaltReader`, TTL 600 s — 60 s exhausted the 5000/h API limit with a
  23-repo fleet on 2026-10-01 — one `halt-read` per repo; refreshed right after a
  request is applied). Before the first read: `unavailable` "in progress", never `off`.
  A repo whose read failed is `unknown` and makes `forge_halt` `partial`.
- `POST /api/halt` `{state: on|off, repos: [..] | null, reason}` with the action token →
  **202** and the recorded request; the writes run repo by repo in the background (a
  fleet is ~3 GitHub calls per repo). 403 bad token; 422 an empty fleet, a missing
  `run_state_dir`, a bad state, a reason that is empty / multi-line / over 500 chars,
  or a repo outside `halt_fleet`; 409 while another request is applying.
- A fleet request is N independent writes: what landed stays, nothing is rolled back;
  sending the same request again retries — repos already there answer `changed: false`.
- **Deviations:** repos that joined `halt_fleet` after the last fleet request, and
  repos of the last request that now read otherwise than requested (a halt lifted by
  hand in GitHub shows up here).

## 3. VSCode

The Factory floor view gains a top node — `Halt: off (N repos confirmed)`,
`Halt: ON for k/N`, `Halt: off, k/N not confirmed`, `Halt: unknown — not read` — with
per-repo lines, deviations and the last request's outcome. The view title gets
"Halt / lift the factory…": action → scope (fleet or one repo) → reason (required,
one line) → modal confirmation → POST. The result is the read-back in the node, not
the POST's answer.

## 4. Out of D1

- **D2** — admission checks in devtools (`merge-pr.sh`, the runner), spec-runner,
  maestro and dispatcher's own `RunController`: refuse on anything but confirmed `off`.
- Arming the production fleet (`halt-set --state off` creates a disabled ruleset) and
  choosing `halt_fleet` — the owner's call, per repository, under the owner's account.
- Web and TUI surfaces for the halt.
