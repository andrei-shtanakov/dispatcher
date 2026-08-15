# Runbook: ATP benchmark view live-smoke test

Manual procedure to verify the ATP benchmark view panel works end-to-end
against a real eco server and handles network state transitions correctly.

## When this runs

- **Manually**, before closing the feature (`@id:atp-benchmark-view`), to
  confirm the panel integrates correctly with a running eco server.
- **Manually**, after any significant change to benchmark fetch logic or
  panel rendering (e.g., changes to `core/benchmarks.py`,
  `core/collectors/atp.py`, or the web panel component).

## The guarantee

The procedure verifies one sentence:

> The benchmark view panel displays live benchmark data from the eco server,
> renders the list correctly, can open a leaderboard, and transitions to
> `unavailable` (never to "0 benchmarks") when the server stops within ~2
> poll cycles.

## Prerequisites

- `../atp-platform` checked out and accessible.
- `dispatcher` on the current branch with the benchmark view feature.
- `node` and `uv` on PATH.

## Procedure

### 1. Start the eco server

In one terminal, boot the ATP eco server with the required configuration:

```bash
cd ../atp-platform
ATP_SECRET_KEY=test-key-for-schema-only \
ATP_SERVER_PROFILE=eco \
  uv run uvicorn atp.dashboard.v2.factory:app --port 8600
```

**Do not run migrations or seed the database.** The eco server schema is
generated on startup; a real database is not needed for this smoke test.

Wait for the server to start:

```
INFO:     Uvicorn running on http://127.0.0.1:8600
```

Verify it is up by checking the profile:

```bash
curl -s http://127.0.0.1:8600/ | jq .profile
# Output: "eco"
```

### 2. Configure dispatcher to point at the eco server

In the dispatcher repo, create a temporary `dispatcher.toml` in the root:

```toml
[benchmarks]
url = "http://127.0.0.1:8600"
```

### 3. Start the dispatcher server

In a second terminal, from the dispatcher repo:

```bash
uv run dispatcher serve
```

The server will start on `http://localhost:8787` by default. The benchmark
view panel is not fetched yet until the web UI loads.

### 4. Open the web UI and observe initial state

Navigate to `http://localhost:8787` in your browser.

You should see the benchmark view panel. It should display:

- **State label**: `not fetched yet` (or similar) initially, before the first
  poll completes.
- **After ~1 second**: the panel transitions to showing a list of benchmarks
  (empty if none exist on the eco server, or populated if the server has
  seeded data).

### 5. Verify benchmark list rendering

Once the panel shows "not fetched yet", click or wait for the list to load.

Verify:

- The list of benchmarks appears (or is empty if no benchmarks were seeded).
- Each benchmark entry is clickable.
- Click on any benchmark entry to open its leaderboard view.
- Leaderboard displays runs and scores (or is empty if no runs exist).

### 6. Kill the eco server and observe the state transition

In the terminal running the eco server (step 1), press `Ctrl+C` to stop it.

**Observe the benchmark view panel** over the next ~20 seconds (the UI polls
every 10 seconds; the service fetch throttle is 60 seconds, so expect the
full transition within ~2 poll cycles). The panel should:

- Transition to a state indicating the server is unavailable (e.g.,
  `unavailable` or similar).
- **Never** display "0 benchmarks" or an empty list as if the server is
  reachable but has no data.
- The distinction is critical: unavailable state ≠ empty list.

### 7. Restart the eco server and re-confirm

Restart the eco server using the command from step 1. Within ~2 poll cycles,
the panel should:

- Transition back to showing benchmarks.
- Resume normal list rendering and leaderboard navigation.

### 8. Clean up

Stop both the eco server and the dispatcher server by pressing `Ctrl+C` in
their respective terminals.

Remove the temporary `dispatcher.toml`:

```bash
rm dispatcher.toml
```

## Exit criteria

All of the following must be true:

1. ✓ Panel displays "not fetched yet" initially.
2. ✓ Panel loads and renders the benchmark list correctly.
3. ✓ Clicking a benchmark opens its leaderboard.
4. ✓ After killing the eco server, the panel shows `unavailable` (not "0 benchmarks").
5. ✓ After restarting the eco server, the panel recovers and shows benchmarks again.

If any of these fail, check:

- `dispatcher/core/benchmarks.py` for fetch logic errors.
- `dispatcher/core/collectors/atp.py` for state classification errors.
- The web panel JavaScript in `dispatcher/server/static/` for render bugs.
- The browser console for client-side errors.
- The server logs (`http://localhost:8787` startup messages) for HTTP errors.

## Related surfaces

| Runbook | Purpose |
|---|---|
| `docs/revendor-atp-benchmark-api.md` | Re-vendor the ATP benchmark API contract when upstream changes. |
