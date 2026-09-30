# Factory floor, slice C1 — runs that have not ended

**Status:** 2026-09-30. Spec and code in one PR (owner decision 2026-09-30).
**Parent:** `2026-09-29-human-control-plane-design.md` §5 (slice C).

## 1. What it shows

`GET /api/factory-floor` → every maestro run that has not ended (`running`, `suspended`,
`interrupted` — the same `classified_runs` walk as the dashboard, with
`report_missing_state=True`), with its start time, the dispatcher launch record joined by
`(repo_key, run_id)` when there is one, and a **stale** flag: not `running` and started
more than 24 h ago. `running` needs a live holder pid, so it is never stale; an unknown
(naive or missing) start is never stale either.

It is an observation, not a wait: nothing here enters the human queue. A stale run carries
a prepared `maestro_run_end` act (`run_id`, `repo_key`, `maestro_home`, `maestro_cli`);
the outcome — `superseded` or `cancelled` — is the human's choice at the moment of acting,
because ending a run is a decision nothing may infer (maestro `run-end`).

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
