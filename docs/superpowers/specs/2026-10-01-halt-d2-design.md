# The halt, slice D2 — admission checks

**Status:** 2026-10-01. **Parent:** `2026-09-29-human-control-plane-design.md` §6.1
(cooperative layer); D1 is `2026-09-30-halt-d1-design.md`. **Rule:**
github-checker `contracts/halt-admission/v1` (github-checker#51), vendored by
consumers with a pin; reference verb `github-checker halt-gate <dir>`.

## 1. Why

The ruleset (D1) stops code **landing** on a default branch; it does not stop agents
**working**. Without D2 a halted fleet keeps taking tasks, spending model budget and
opening PRs that cannot merge. D2 makes every actor ask before starting NEW work.

## 2. The rule (owner decision 2026-10-01, variant B)

Decided by the one thing a write-level token sees — the enforcement of the ruleset
named `darkfactory-halt` (D0: a non-admin gets `bypass_actors: null`):

| reader sees | admit | code |
|---|---|---|
| `disabled` | yes | `admit_off` |
| no such ruleset | **yes** | `admit_missing` |
| non-GitHub origin (consumer rule) | yes | `admit_not_github` |
| `active` | no | `refuse_on` |
| duplicate name / other enforcement | no | `refuse_duplicate` / `refuse_enforcement` |
| anything unreadable | no | `refuse_unknown` |

`admit_missing` instead of the ratified "only confirmed off": only an admin can delete
or disable the ruleset, and an admin bypasses the halt anyway — a missing ruleset is a
forgotten arming (dispatcher's admin read shows it as a deviation), never an agent's
way around the halt. Refusing would block DarkFactory-polygon projects, sdd-framework
and every new repository. The fleet (23 repos) was armed 2026-10-01 (23/23 `off`).

Unit of admission: a **new** run. Admitted work drains; its merge is refused by the
ruleset and, earlier and in its own words, by `merge-pr.sh`.

## 3. D2a — always on (tools that exist only for DarkFactory)

- **dispatcher** — `halt_admission` (on in a loaded config, off in a bare `DispatcherConfig` for hermetic tests; a non-boolean is a load error). `RunController.submit_v2` asks `ActionRunner.halt_gate(checkout)`
  (github-checker `halt-gate`) right after the checkout is resolved and before the
  guard: refused → `AdmissionRefused(409, "halted", …)`, nothing reserved; a gate that
  raises refuses (`refuse_unknown`). Only a stated `admit: true` admits.
- **devtools** — `governance/halt_gate.py` (stdlib, vendored vectors): `merge-pr.sh`
  checks right after the profile preflight, with the agent's own token, and exits
  **6** ("стоп-кран") before reading PR facts; the governance runner refuses
  `start` / `verify` / `reopen` with exit 6 (resume drains). `approve_node` names
  code 6.

## 4. D2b — spec-runner and maestro, opt-in

They are products used outside DarkFactory; an unconditional `gh` call on every
start would break them wherever `gh` is absent or unauthenticated. They check only
when `DARKFACTORY_HALT_CHECK=1` is in the environment, which the DarkFactory
launchers set (dispatcher when spawning maestro; the devtools runner when spawning
spec-runner). spec-runner: before `_run_tasks`; maestro: in `bootstrap_run`'s
fresh-run branch. Both apply `admit_not_github` (their checkouts may be local or on
another forge). Separate PRs in each repo.

## 4a. As shipped (2026-10-01)

| Repo | PR | Where the halt is asked |
|---|---|---|
| github-checker | #51 | `halt-gate` + `contracts/halt-admission/v1` (vectors, origin_vectors) |
| dispatcher | #289, #290 | `submit_v2` (409 `halted`); maestro spawned with the flag |
| devtools | #531 | `merge-pr.sh` (6 halted / 2 unread), runner `start`/`verify`/`reopen` |
| spec-runner | #631 | `run` / `retry` / `watch` (watch pauses per task) |
| maestro | #248 | the ENTRY of `run` / `orchestrate` / `service run --stage orchestrate` |

Two refinements from review:

- **maestro asks at the entry of every invocation**, fresh or `--resume` or `--db`:
  under a halt it neither starts nor resumes work (a running process drains).
  Three review rounds found the same class — work starting without the check — because
  "is this a new run" was decided from CLI flags (`--resume` over an empty `--db`
  mints a run). devtools' runner still lets `resume` drain: its resume continues a
  recorded run, it cannot mint one.
- **The review stage is not gated** (`maestro review-pr`, `service run --stage
  review`): reviewing PRs that already exist lands nothing.

Codes across consumers: 6 halt in force, 2 halt unread (retry fits), maestro 1 for a
config that cannot name a repository.

## 5. Out of D2

- Stopping tasks INSIDE an admitted run (a draining contract) — not v1.
- Web/TUI surfaces of the halt.
