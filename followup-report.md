# Copilot follow-up report — PR #167

Branch `fix/run-copilot-followups`, based on `master` at `7118234`.
Three commits, one per finding.

## Finding 1 — `run_state_dir`/`maestro_cli` comments described behaviour the code does not have

**What changed** (`dispatcher/core/discovery.py:49-60`, commit `cef72fc`):
rewrote both comments. They previously said `None` means "the control
plane is off: no submit endpoint, no controller." That was false —
`create_app()` registers `/api/runs/*` unconditionally. The comments now
say what actually happens: `submit` answers with a refusal receipt
(`accepted: false`), and the other three endpoints (`read`, `resolve`,
`verb`) answer 409 (`ControlPlaneOff`).

**Covering test**: `tests/test_run_api.py::test_resolve_and_verb_still_answer_with_the_control_plane_off`
— hits `/resolve` and `/verb` with the control plane off by default and
asserts both come back 409 (not "route not found" / 404 / anything else).
Combined with the pre-existing `test_submit_with_the_control_plane_off_is_a_refusal_not_a_crash`
and `test_control_plane_off_reads_409_not_404`, all three corrected claims
in the comment are now exercised.

**Command**:
```
uv run pytest tests/test_run_api.py -k "control_plane_off_reads or resolve_and_verb_still" -v
```
**Output**:
```
tests/test_run_api.py::test_control_plane_off_reads_409_not_404 PASSED   [ 50%]
tests/test_run_api.py::test_resolve_and_verb_still_answer_with_the_control_plane_off PASSED [100%]
======================= 2 passed, 11 deselected in 0.43s =======================
```

## Finding 2 — `/resolve` invents an empty `outcome`

**What changed** (`dispatcher/server/app.py:611-627` in `resolve_run`,
commit `c307357`): before calling `runs.end_orphan`, the endpoint now
checks `request.run_id is not None and request.outcome is None` and
raises `HTTPException(422, ...)` naming both fields (`run_id`, `outcome`)
and the two permitted outcomes (`cancelled`/`superseded`), instead of
letting `request.outcome or ""` synthesize an empty string that the
controller then reports as `got ''` — as if the caller had sent that
value. The controller's own check in `RunController.end_orphan` (`_OPERATOR_ENDINGS`)
is untouched: it is still reachable from the TUI, MCP, and VSCode surfaces,
which don't go through this HTTP endpoint.

**Covering test**: `tests/test_run_api.py::test_resolve_with_a_run_id_and_no_outcome_is_422_naming_both_fields`
— POSTs `{"run_id": "01AAA"}` (no `outcome`) to `/resolve` with the
control plane on, and asserts a 422 whose `detail` names `run_id`,
`outcome`, `cancelled`, and `superseded`.

**Command**:
```
uv run pytest tests/test_run_api.py -k "resolve_with_a_run_id_and_no_outcome" -v
```
**Output**:
```
tests/test_run_api.py::test_resolve_with_a_run_id_and_no_outcome_is_422_naming_both_fields PASSED [100%]
======================= 1 passed, 13 deselected in 0.17s =======================
```

Regression check — the controller's own check still fires when reached
directly (e.g. via a future non-HTTP caller):
```
uv run pytest tests/test_run_controller.py -k "end_orphan_rejects_an_outcome_outside" -v
```
passes (part of the full run below).

## Finding 3 — `_listing()` swallowed every `OSError` (the important one)

**What changed** (`dispatcher/core/run_controller.py`, commit `1d14969`):

- `_listing()` now catches only `FileNotFoundError` → `[]` (a genuinely
  absent `runs/` stays clean-empty). Every other `OSError` — a
  permissions fault, a bad mount, `runs` colliding with a plain file
  (`NotADirectoryError`) — now propagates instead of reading as empty.
- `_candidates()` (used by both `resolve_unknown` and `end_orphan`) wraps
  its call to `_listing()` and re-raises any `OSError` as
  `RunRejectedError("cannot list runs at {runs}: {err}")` — caught by the
  existing 422 handler in `resolve_run` (`dispatcher/server/app.py`), no
  new exception type needed.
- `submit()`'s pre-launch snapshot call to `_listing()` was already inside
  a `try` whose `except (RunStoreError, OSError)` converts to a refusal
  receipt — no change needed there beyond `_listing`'s new contract.
- `_await_materialization()` (the post-launch poll, called from `_launch`
  **after** the maestro child is already running with
  `start_new_session=True`) is not one of the two call graphs named in
  the finding, but it also calls `_listing()` and had no wrapping
  try/except anywhere up its call chain to `submit_run`. Letting an
  `OSError` propagate there would abandon a live background process
  behind an unhandled 500 with zero diagnostics recorded — a strictly
  worse regression than the transient read failure itself. I added a
  `_listing_since()` helper used only by this poll: it catches `OSError`,
  logs via the existing `_audit` logger, and degrades to "nothing new
  this round" (the same behaviour `_listing` used to have everywhere).
  Correctness for the two API-facing decisions (`submit`'s snapshot,
  `_candidates`' recount) is unaffected — this helper doesn't touch
  either.

**`NotADirectoryError` decision**: grouped with the *fault*, not with
absence — the opposite of `_subdirs` (`dispatcher/core/collectors/maestro.py:222-232`),
which groups it with `FileNotFoundError`. `_subdirs` backs the dashboard's
general listing, where tolerating an odd path is a reasonable soft
degradation. `_listing` backs correlation logic where "the path exists but
is a plain file" is a genuine anomaly (e.g. `runs` colliding with a stray
file), not the ordinary "no runs yet" case `FileNotFoundError` already
covers — and the caller-facing report says exactly this, so I did not
default to matching `_subdirs`'s shape. Verified directly:
`tests/test_run_controller.py::test_listing_refuses_when_runs_is_a_plain_file`.

**Environment note**: not running as root (`whoami` → `Andrei_Shtanakov`,
`id` → `uid=501`), so `chmod 0o000` genuinely denies self-access here —
used real permission tests, not the `NotADirectoryError` fallback (though
that fallback is exercised anyway, as its own dedicated test of the
`NotADirectoryError` decision above). Each permission test guards itself
with a `_skip_if_root()` helper that skips loudly (not silently) if ever
run as root.

**Covering tests** (`tests/test_run_controller.py`, new section "I7: an
unreadable runs/ must not read the same as an absent one"):
- `test_listing_treats_a_genuinely_absent_runs_dir_as_clean_empty` — absent path → `[]`.
- `test_listing_refuses_an_unreadable_runs_dir` — `chmod 0o000` on a real directory (restored in `finally`) → `_listing` raises `OSError`, not `[]`.
- `test_listing_refuses_when_runs_is_a_plain_file` — path points at a file → `_listing` raises `NotADirectoryError`.
- `test_submit_refuses_rather_than_snapshot_an_unreadable_runs_dir` — pre-existing `runs/` with a run in it, made unreadable before `submit()`; asserts `accepted is False` and `"cannot use request_id"` in the reason (not a silent empty snapshot).
- `test_resolve_unknown_refuses_rather_than_zero_candidates_when_unreadable` — `launch_unknown` record, `runs/` made unreadable; asserts `resolve_unknown` raises `RunRejectedError` matching `"cannot list runs"` (not "zero candidates, stays unknown").
- `test_end_orphan_refuses_rather_than_a_stale_candidate_set_when_unreadable` — same fault via the other resolution path; asserts `end_orphan` raises `RunRejectedError` matching `"cannot list runs"` (not "not a candidate").

**Command**:
```
uv run pytest tests/test_run_controller.py -k "listing or unreadable or root" -v
```
**Output**:
```
tests/test_run_controller.py::test_listing_treats_a_genuinely_absent_runs_dir_as_clean_empty PASSED [ 16%]
tests/test_run_controller.py::test_listing_refuses_an_unreadable_runs_dir PASSED [ 33%]
tests/test_run_controller.py::test_listing_refuses_when_runs_is_a_plain_file PASSED [ 50%]
tests/test_run_controller.py::test_submit_refuses_rather_than_snapshot_an_unreadable_runs_dir PASSED [ 66%]
tests/test_run_controller.py::test_resolve_unknown_refuses_rather_than_zero_candidates_when_unreadable PASSED [ 83%]
tests/test_run_controller.py::test_end_orphan_refuses_rather_than_a_stale_candidate_set_when_unreadable PASSED [100%]
======================= 6 passed, 37 deselected in 1.20s =======================
```

## Full targeted regression (both changed test files together)

```
uv run pytest tests/test_run_controller.py tests/test_run_api.py -v
```
```
============================= 57 passed in 19.34s ==============================
```

## Formatting / lint / typecheck

```
uv run ruff format .
```
```
134 files left unchanged
```
(the one reformat that happened mid-work, collapsing a two-line
`_audit.error(...)` call, is already folded into the finding-3 commit.)

```
uv run ruff check .
```
```
All checks passed!
```

```
uv run pyrefly check dispatcher tests scripts
```
```
INFO 0 errors (43 suppressed, 22 warnings not shown)
```

## Full suite

```
uv run pytest
```
```
FAILED tests/test_governance_live_smoke.py::test_live_smoke_real_gate_check_to_http_state
FAILED tests/test_product_proposals_live_smoke.py::test_http_surface_on_the_pinned_real_mirror
FAILED tests/test_spec_runner_config_integration.py::test_write_path_live_smoke_real_binary
============ 3 failed, 1261 passed, 2 warnings in 113.17s (0:01:53) ============
```

Exactly the expected baseline: the same three binary-absent live-smoke
failures and the same two `test_benchmarks_stub_integration.py` warnings
named in the task brief. `1261 = 1253 (master baseline) + 8` (the new
tests: 1 for finding 1, 1 for finding 2, 6 for finding 3). Nothing else
failed.

## Commits

```
1d14969 fix(run): stop _listing reading an unreadable runs/ as empty (I7)
c307357 fix(run): validate the run_id/outcome pairing on /resolve before the controller
cef72fc fix(run): correct run_state_dir/maestro_cli comments to match reality
7118234 Merge pull request #167 from andrei-shtanakov/feat/dark-factory-control-plane-slice0  (base)
```

Working tree is clean; this report file is untracked and intentionally
not committed.
