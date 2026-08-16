# ATP Benchmark run-status (phase 2) — design

**Date:** 2026-08-16
**Status:** proposed (spec-first per plan item; implementation follows after
review)
**Plan item:** `@id:atp-benchmark-runs-phase2` — continuation of
`atp-benchmark-view` (spec 2026-08-15, §2 non-goals), which explicitly
deferred run status: token-gated, owner-scoped, first stored secret, run-id
discovery story.

## 1. Goal & producer facts (verified against atp-platform @ `da3a264`)

Render the status of one benchmark run of the configured eco server, on
explicit human request. Producer surface
(`atp/dashboard/v2/routes/benchmark_api.py`):

- `GET /api/v1/runs/{run_id}/status` — requires a Bearer **user token**
  (`atp_u_*`; dependency `BenchmarkCaller` = authenticated user, tournament
  tokens 403). Rate-limited `120/minute`.
- **Owner-scoped with a deliberate 404**: a run that does not exist and a run
  owned by another user both return 404 ("We deliberately return 404 rather
  than 403 to avoid leaking" — producer comment). Dispatcher must render that
  ambiguity honestly: *"run not found — or not owned by this token"*.
- Response `RunStatusResponse`: `id: int`, `status: str`
  (`pending|in_progress|completed|failed|cancelled|partial`),
  `current_task_index: int`, `tasks_count: int`, `total_score: float|null`,
  `score_semantics: dict`, `score_components: dict`,
  `completed_tasks: list`. Note: `score_semantics`/`score_components`
  **already shipped** producer-side — the phase-1 TODO note "status carries
  only total_score" is overtaken; this spec consumes them from day one.
- There is **no run-listing endpoint** (no `GET /api/v1/runs`); discovery is
  a dispatcher-side story (§4).

## 2. Non-goals

- **No background polling of the token-gated surface.** The token is used
  only on an explicit human click (§5) — no secret ever rides the periodic
  fetch cycle, and NFR-02 (no network on render) is untouched because a
  click is not a render.
- **No mutations**: `POST /runs/{id}/cancel`, `/submit`, `/events`,
  `/benchmarks/{id}/start` are never called (ADR-ECO-004 D1). GET-only.
- **No run watchlist / persistence** in phase 2. Manual run-id entry only,
  mirroring the merge-gate precedent (PR #93: "вход — ручной ввод номера
  PR, не список"). A persisted watchlist is state we do not need until the
  discovery story improves (§4).
- **No `completed_tasks` rendering.** The per-task list is unbounded and
  belongs to a drill-down we have no consumer for; `extra="ignore"` drops it.
- **No TUI/VSCode/MCP parity** — follows the standing parity item
  (`@id:atp-benchmark-view-parity`) once the web surface settles. MCP
  additionally must never gain a tool that spends the stored secret on an
  agent's initiative — if parity ever reaches MCP, that is a design question
  (X-02: a tool call is an agent action, not a human click), recorded here
  so it cannot slip in as "just parity".
- **No `atp-platform-sdk`** — same reasoning as phase 1; `httpx` directly.

## 3. The first stored secret: token configuration

New optional `dispatcher.toml` key, valid **only inside `[benchmarks]`**:

```toml
[benchmarks]
url = "http://127.0.0.1:8000"
token_file = "~/.config/dispatcher/atp-token"
```

- `dispatcher.toml` itself stays secret-free: it carries a **path**, never
  the token. An inline `token = "..."` key is rejected at load time with an
  error naming `token_file` — a config that tries to inline the secret must
  fail loudly, not work quietly (closest precedent: the 0600 git-ignored
  sidecar `dispatcher-sync.toml`, but that file holds no secrets; this one
  does, so the rules below are stricter).
- `token_file` without `[benchmarks].url` is a load-time error (a token with
  nothing to spend it on is a misconfiguration, not a feature half-on).
- The path is expanded (`~`) at load; the **file is read per request**
  (§5), never cached in config or service state — rotation and revocation
  need no restart, and no long-lived object carries the secret.

**Read rules (fail-closed, pinned by tests):**

1. Must be a regular file **by `lstat`** (`S_ISREG` on the unfollowed
   path): symlinks are **rejected**, classified `token_file_insecure`. Two
   reasons, both load-bearing: the §3.2 permission gate is ambiguous
   through a link (the link's own mode is 0777; gating the target invites
   a check-vs-open race), and a token reached through a symlink into a
   dotfiles checkout is exactly the layout this design must not
   encourage. An absent path is `token_file_missing`; a
   directory/other-non-regular is `token_file_unreadable`.
2. Permission gate: `stat.S_IMODE & 0o077 == 0` — group/other access of any
   kind refuses the token (`token_file_insecure`), same spirit as SSH key
   handling. Refusal message names the mode, never the content.
3. Content: exactly one non-empty line after stripping trailing whitespace;
   empty or multi-line content is `token_file_unreadable`. No format check
   beyond that — dispatcher does not parse `atp_u_*` prefixes; the server
   is the authority on token validity.

**Secrecy invariants (pinned by a canary test):** the token string never
appears in any report model, error line, log record, or HTTP response of
dispatcher — a canary token written to a fixture file must not be found in
any serialized `RunStatusReport` (all states, including the error ones) nor
in the app's responses. The token travels only in the outbound
`Authorization: Bearer` header; never in a URL.

## 4. Run-id discovery

The public surface has no run listing, and the owner-scoped one does not
exist at all. Phase 2 therefore uses **manual run-id entry** (the operator
knows the id from the agent run that created it), exactly like merge-gate
takes a PR number. Two recorded upgrade paths, deliberately not taken now:

- an owner-scoped `GET /api/v1/runs` on the producer — would be an inbox
  issue to atp-platform (ADR-ECO-006); filed only when the owner wants the
  panel to grow a list (same shape as the open `merge-gate-pr-listing`
  item);
- a dispatcher-side persisted watchlist of previously-checked ids — state
  with a lifecycle (staleness, deletion) that manual entry does not have.

## 5. Dispatcher endpoint

`GET /api/benchmarks/runs/{run_id}` (`run_id: int`, ge=1) in
`server/app.py`, `response_model=RunStatusReport`. 200 always; state lives
in the body (house pattern). GET-only — it is a read that *performs one
outbound GET* on explicit human action, precedent `GET /api/pr-detail`
(merge-gate, live subprocess on click).

The handler calls `BenchmarkService.run_status(run_id)` which:

1. resolves configuration → `unconfigured` (no `[benchmarks].url`) or
   `token_unconfigured` (url set, no `token_file`);
2. reads the token file per the §3 rules → `token_file_missing` /
   `token_file_insecure` / `token_file_unreadable`;
3. performs a single synchronous
   `GET {base}/api/v1/runs/{run_id}/status` with
   `Authorization: Bearer <token>`, `timeout=10.0`,
   `follow_redirects=False` (a redirect must not re-send the token
   somewhere else — with redirects disabled the header cannot travel);
   the run id is `str(int)` quoted into one path segment (phase-1 rule);
4. classifies:

| Outcome | `status` |
|---|---|
| 200 + body validates | `ok` |
| 401 / 403 | `unauthorized` (token rejected — expired, revoked, wrong kind) |
| 404 | `not_found` — rendered as "run not found, or not owned by this token" (§1) |
| other non-2xx (incl. 429) | `unavailable` |
| transport error / timeout | `unavailable` |
| 2xx + body fails validation | `unreadable` |

Error lines: one line, length-capped, status code + exception class + URL
only — never a response body, never the token (phase-1 rule, §3 canary).

No dispatcher-side throttle: the endpoint is click-driven; the producer's
`120/minute` limit surfaces as a 429 → `unavailable` with the code visible.

## 6. Data model — additions to `dispatcher/core/benchmarks.py`

Same model config as phase 1 (`extra="ignore"`, `strict=True`):

```python
class RunStatusInfo(BaseModel):        # mirrors ATP RunStatusResponse
    id: int
    status: str                        # producer vocabulary, passed through
    current_task_index: int
    tasks_count: int
    total_score: float | None
    score_semantics: dict[str, Any]    # opaque passthrough, rendered shallow
    score_components: dict[str, Any]   # opaque passthrough, rendered shallow

RunStatusStatusLiteral = Literal[
    "unconfigured", "token_unconfigured", "token_file_missing",
    "token_file_insecure", "token_file_unreadable",
    "unauthorized", "not_found", "unavailable", "unreadable", "ok",
]

class RunStatusReport(BaseModel):
    status: RunStatusStatusLiteral
    run_id: int
    fetched_at: datetime | None        # completion time of THIS attempt;
                                       # null only for the config/token
                                       # states where no request was made
    error: str | None                  # one line, set iff status not in
                                       # ("ok",); config/token states carry
                                       # a human-readable reason here too
    run: RunStatusInfo | None          # set iff status == "ok"
```

`run.status` (producer vocabulary) is **passed through verbatim**, not
re-classified — the run's lifecycle state is the producer's judgment; the
report's `status` field classifies only *our read of it* (same split as
governance ARCH-C3). An unknown producer status word renders as itself.

## 7. Web panel

Inside the existing **Benchmarks** section (visible only when configured):
a "Run status" row — numeric input + button, merge-gate style — always
visible when the section is; every state §5 defines renders explicitly:

- `token_*` states: the exact reason ("token file is group/other-readable
  (0644) — chmod 600", "no [benchmarks].token_file configured", …). A
  token problem is a *configuration* answer, never rendered as if the run
  were missing.
- `not_found`: the two-sided wording from §1 verbatim — the panel must not
  claim the run does not exist when it may simply belong to another token.
- `ok`: producer status chip (verbatim word), `current_task_index /
  tasks_count`, `total_score` (or —), and shallow key:value lists of
  `score_semantics` / `score_components`. All producer text through
  `esc()`.
- In-flight: button disabled while the request runs; a second click must
  not stack requests. Result replacement is keyed by `run_id` so a late
  response for a previously-entered id never renders under a new one
  (ppGen-style guard applies here — this *is* a per-entity fetch).
- Zero-state rule: this panel never has a "0" state — it renders exactly
  one run or one explicit non-`ok` state.

## 8. Vendored contract — re-vendor

`contracts/atp-benchmark-api/v1/` is re-pinned by the existing procedure
(`docs/revendor-atp-benchmark-api.md`), with two changes:

- `scripts/prune_atp_openapi.py` `KEPT_PATHS` grows
  `/api/v1/runs/{run_id}/status`; transitively-referenced components
  (`RunStatusResponse`, `TaskResultResponse`, the security scheme) ride
  along via the existing transitive-closure logic.
- `fixtures/` gains a captured `run_status_*.json` (at least one completed
  run with non-empty `score_components`, and ideally one `in_progress`).
  Fixtures must not contain a real token (they are response bodies — no
  token appears in responses; the vendor test asserts the canary rule
  anyway).

Copy-integrity and drift guarantees are unchanged in kind; the drift
artifact is still the pruned `openapi.json` alone. `PINNED.txt` moves to
the producer commit the re-vendor runs against (currently `da3a264`, the
same commit this spec was verified against).

## 9. Testing

- **Token-file gate units**: each §3 failure mode; the permission gate at
  0600/0400 (pass) vs 0640/0604/0644 (refuse); a symlink to an otherwise
  valid 0600 file refuses (`token_file_insecure` — the lstat rule, not the
  target's mode); single-line rule.
- **Canary test (the secrecy pin)**: a fixture token; run every state the
  service can produce (mock transport for 200/401/404/500/garbage/refused);
  assert the canary substring appears in **no** serialized report and no
  error string. This test is the design's teeth — it must exist before the
  panel ships.
- **Classification units**: the §5 table row by row; unknown producer
  status word passes through; `extra` fields (incl. `completed_tasks`)
  ignored; missing required field → `unreadable`.
- **Integration stub**: the phase-1 in-test FastAPI app grows the status
  route with a required Bearer header — asserts the header is actually
  sent, correct token → `ok`, wrong token → 401 → `unauthorized`, foreign
  run → 404 → `not_found`.
- **Route level**: golden serialization of `RunStatusReport`; the new route
  present in the GET-only audit.
- **Web harness**: input rules, in-flight lock, per-run_id stale guard,
  every non-`ok` state rendered explicitly, `esc()` on producer text.
- **Live smoke**: extends the manual runbook
  (`docs/atp-benchmark-live-smoke.md`) — seed a run via the SDK, mint a
  token, point `token_file` at it. Not in CI (phase-1 residual carries
  over).

## 10. Delivery plan

- **PR-1 (this spec).** Review is the decision point for §3 (storage
  shape) and §4 (manual entry).
- **PR-2** — re-vendor (prune + fixtures + pin) + config (`token_file`) +
  token reader + `RunStatusInfo`/`RunStatusReport` + service method +
  route + unit/stub/route/canary tests.
- **PR-3** — web panel + Node harness + live-smoke runbook extension +
  TODO checkbox flip.

## 11. Known residuals (accepted)

- **Manual run-id entry** (§4): the operator must know the id. Upgrade
  paths recorded, neither taken.
- **No wall-clock deadline** on the one outbound request (phase-1 residual,
  same shape): a trickling response can hold the click open; the UI stays
  responsive (the fetch is client-async), the browser gives up with its own
  limits.
- **`fetched_at` is per-click**: there is no cached run-status tier, so
  navigating away loses the answer. Deliberate — caching the output of a
  secret-spending call is state phase 2 does not need.
- **Producer rate limit** surfaces as `unavailable` with a 429 code rather
  than a dedicated state; acceptable for a click-driven surface.
