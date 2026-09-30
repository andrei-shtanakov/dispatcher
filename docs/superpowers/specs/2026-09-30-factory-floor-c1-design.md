# Factory floor, slice C1 — runs that have not ended

**Status:** 2026-09-30. Spec and code in one PR (owner decision 2026-09-30).
**Parent:** `2026-09-29-human-control-plane-design.md` §5 (slice C).

## 1. What it shows

`GET /api/factory-floor` → every maestro run that has not ended (`running`, `suspended`,
`interrupted` — the same `classified_runs` walk as the dashboard, with
`report_missing_state=True`), with its start time, the dispatcher launch record joined by
`(repo_key, run_id)` when there is one, and a **stale** flag: not `running` and **no
observed activity for 24 h** — `last_activity_at`, the newest mtime of the run's own files
(`state.db`, its WAL, `logs/*`). Not the start time: a run dispatcher launches is never
`running` (the holder is written only by maestro's service tick), so aging from the start
would call a live 25-hour run abandoned (review on #282). `running` is never stale; an
unknown activity time is never stale either. **Only `interrupted` can be stale:**
`suspended` is a run parked for a human, and however long it waits it is a wait, never an
orphan (review on #282). mtime here is observed activity, not a wait
start, so the human-queue rule against mtime as `since` does not apply.

It is an observation, not a wait: nothing here enters the human queue. A stale run carries
a prepared `maestro_run_end` act (`run_id`, `repo_key`, `maestro_home`, `maestro_cli`);
the outcome — `superseded` or `cancelled` — is the human's choice at the moment of acting,
because ending a run is a decision nothing may infer (maestro `run-end`). The act is the CLI
line for **every** stale run, including ones dispatcher launched: the web run view offers
`run-end` only inside the `launch_unknown` resolution flow, records predating `checkout`
are refused by the run verbs, and the extension executes nothing itself — so the CLI is
the one path a human can always take (review rounds 2–3 on #282). For a launched run the
consequence is named, not hidden: dispatcher's launch record stays `materialized` (the
Launchpad finding in §3). `--reason` is a documented `run-end` option ("Free-form detail
stored with the outcome").

Degradation as in the human queue: `dispatcher_runs` / `maestro` statuses, `complete`,
HTTP 200 always.

## 2. VSCode

View "Factory floor": "Stale — likely abandoned" first, then "In flight"; label
`<repo> · <work_id or run_id>`, description `status · age`. Clicking a stale run asks for
the outcome, then types `MAESTRO_HOME=… <cli> run-end <id> --outcome <o> --reason ` into a
terminal — not executed, the reason left for the human to write.

## 3. Out of C1

- **Agent acts in the last 24 h** (PRs merged by `ai-prosto`) — C2; needs a github-checker
  verb (GraphQL search returns `mergedBy`; the REST search cannot filter by merger).
- Finding recorded, not fixed here: Launchpad's "active" lists dispatcher records still in
  `materialized` although their maestro run completed long ago (4 of 5 on 2026-09-30).
