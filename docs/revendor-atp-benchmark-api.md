# Runbook: re-vendor `atp-benchmark-api/v1`

Canonical procedure for moving dispatcher's vendored copy of the ATP eco
server's openapi surface — pruned to the two `GET` paths
`dispatcher.core.benchmarks` consumes — to a new producer commit. The copy
was first created by this same script (2026-08-15, spec
`docs/superpowers/specs/2026-08-15-atp-benchmark-view-design.md`); there is
no separate creation document to consult.

## When this runs

**Two independent triggers, both guarantees from day one:**

- **Manually**, after a change to `atp.dashboard.benchmark.schemas` or the
  `/api/v1/benchmarks*` routes has been accepted in atp-platform — usually
  noticed because a field dispatcher wants to consume is missing, or
  because the fixture-pin test
  (`tests/test_benchmarks.py::test_vendored_fixtures_parse_as_ok`) or the
  copy-integrity test starts failing against a hand-authored fixture that
  no longer matches reality.
- **Automatically, advisory**: `.github/workflows/atp-openapi-drift.yml`
  runs weekly (`workflow_dispatch` also available) and boots the eco app
  from a fresh checkout of atp-platform's default branch to regenerate the
  pruned `openapi.json`, then compares it against the vendored pin with
  `scripts/atp_openapi_drift_report.py` (exit codes: `0` no drift · `1`
  drift · `2` unavailable — a red `Regenerate` step is the exit-2 class,
  read as "observation broken", never as "no drift"). Unlike the
  git-object vendors, there is no blob in atp-platform's tree to diff
  against without booting the app, so this job pays that cost itself
  instead of skipping the check. It is **not a pull-request check and
  never required**: a commit in atp-platform must not be able to redden
  dispatcher's PRs. A red run means a human owes a deliberate re-vendor PR
  — nothing here writes to the vendored copy.

Do not start from a green test suite as evidence that the pin is current:
the suite proves the vendored copy matches its own manifest, which stays
true forever no matter how far upstream travels. The drift job is the
thing that actually looks at where upstream is now.

## The guarantee

The procedure exists to support one sentence:

> The `openapi.json` in `contracts/atp-benchmark-api/v1/` is, byte for
> byte, the pruning of the schema atp-platform's eco app served for the
> commit recorded in that directory's `manifest.json`.

Note what it does not say. Three SHAs agreeing with each other — the
manifest's, `PINNED.txt`'s, and the literal in
`tests/test_atp_benchmark_api_vendor.py` — proves only that someone edited
three files consistently. That is why the pin is an **input** to the
script, and provenance is checked **against a worktree's own `git
rev-parse HEAD`** after checkout, not against the other copies of the
number.

Fixtures are a *separate* guarantee: they are consumer-maintained examples
(dispatcher.core.benchmarks's own test data), not a projection of the pin.
The one thing tying them to the contract is that they must keep validating
against the pruned schema (`test_vendored_fixtures_parse_as_ok`) — the
re-vendor script never derives them, it only carries the existing files
forward untouched.

## Procedure

### 1. Pick the commit

A full 40-hex commit id from atp-platform's `main`, already reviewed and
merged there.

```bash
git -C ../atp-platform rev-parse main
```

Abbreviations and branch names are refused by the script: they resolve
today and identify nothing tomorrow.

### 2. Run the script

```bash
scripts/revendor_atp_benchmark_api.sh <NEW_PIN> [--from ../atp-platform]
```

(`--from ../atp-platform` is the default; the flag only needs to be spelled
out when pointing at a different local checkout.)

It checks out `NEW_PIN` into a throwaway `git worktree` of the `--from`
checkout, verifies the worktree's `HEAD` is exactly that commit, boots the
eco app (`ATP_SERVER_PROFILE=eco`, a fixed placeholder `ATP_SECRET_KEY` —
schema generation touches no database and no real secret) and calls
`create_app().openapi()`, prunes the result with
`scripts/prune_atp_openapi.py` to the two consumed `GET` paths and their
transitively-referenced component schemas, carries the existing
`fixtures/*.json` over unchanged, regenerates `manifest.json` from the same
SHA (via `scripts/vendor_manifest.py --contract atp-benchmark-api
--contract-version 1`), checks the manifest records that SHA and its
surface matches the staged files, and only then swaps staging into place.
Until that last step the working copy is untouched; every ordinary failure
runs the restoring trap and leaves the working copy exactly as it was. The
temporary worktree is always removed, even on failure — `../atp-platform`
itself is read-only reference and is never left in a modified state.

**Exit codes:** `0` ok · `1` usage · `2` source or commit unavailable ·
`3` provenance mismatch · `4` manifest generation or read-back ·
`5` internal failure — the working copy was left as it was found.

A `3` means the checked-out worktree is not the commit given. Do not
re-run hoping for a different answer.

### 3. Refresh fixtures by hand, if the shape moved

Fixtures are consumer-maintained and are **not** touched by step 2. If the
re-vendor was triggered by a schema change that also changes what a valid
fixture looks like:

1. **Preferred — live capture.** Boot the eco server from the new checkout
   and seed one benchmark (with a completed run, for a populated
   leaderboard) and one benchmark with no runs (for the empty-leaderboard
   fixture), then capture the real responses:

   ```bash
   ATP_SECRET_KEY=<placeholder> ATP_SERVER_PROFILE=eco \
     ATP_DATABASE_URL=sqlite+aiosqlite:////tmp/atp-revendor/db.sqlite \
     uv run --project ../atp-platform python - <<'PY'
   # boot create_app(), seed via the ORM or the authenticated HTTP flow,
   # then GET /api/v1/benchmarks and /api/v1/benchmarks/{id}/leaderboard
   # (populated and empty) and write the three JSON bodies to
   # contracts/atp-benchmark-api/v1/fixtures/*.json
   PY
   ```

2. **Fallback.** If the server cannot boot without external setup this
   run, hand-author the three files to conform to the new pruned component
   schemas in `contracts/atp-benchmark-api/v1/openapi.json`, and mark them
   `"authored, schema-validated"` in `PINNED.txt` — a recorded deviation,
   not a silent one.

Either way, finish with:

```bash
uv run pytest tests/test_benchmarks.py::test_vendored_fixtures_parse_as_ok -v
```

then regenerate the manifest so it certifies the new fixture bytes too
(the re-vendor script in step 2 already did this if fixtures were updated
*before* running it; if fixtures changed *after*, regenerate directly):

```bash
python3 scripts/vendor_manifest.py --producer-commit <NEW_PIN> \
  --root contracts/atp-benchmark-api/v1 \
  --contract atp-benchmark-api --contract-version 1
```

### 4. Update the independent literal

`tests/test_atp_benchmark_api_vendor.py` holds its own copy of the pin:

```python
PRODUCER_COMMIT = "…"
```

This stays a hand edit **on purpose**: it is the independent assertion
about what the manifest should say, and a test that reads the value it
checks proves nothing.

### 5. Run the gate

```bash
uv run pytest tests/test_atp_benchmark_api_vendor.py tests/test_benchmarks.py -v
uv run pytest
```

The first command is the contract's own surface; the second is the repo
rule — a re-vendor must leave the whole suite green.

### 6. Deliver as its own PR

Branch, push, `gh pr create`, act on the GitHub Copilot review, and let a
human merge. Keep the re-vendor free of unrelated changes. State in the PR
body which pin was used and whether fixtures were refreshed (and, if so,
live-captured or authored).

## Related surfaces

| Contract | Procedure |
|---|---|
| `contracts/atp-benchmark-api/v1` | this runbook + the scheduled `atp-openapi-drift.yml` (its own job) |
| `contracts/steward-gate-catalog/v1` | `docs/revendor-steward-gate-catalog.md` |
| `contracts/steward-roles-catalog/v1` | `docs/revendor-steward-roles-catalog.md` |
| `contracts/steward-gate-verdicts/v1` | `docs/revendor-steward-gate-verdicts.md` |
| `contracts/github-checker-actions/v1` | `docs/revendor-github-checker-actions.md` |
| `packages/plan-fields/src/plan_fields/contract` | offline integrity in `dispatcher/core/contracts.py` + the scheduled `upstream-drift.yml` (its own job) |
