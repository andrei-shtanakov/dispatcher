# Runbook: ATP benchmark view live-smoke test

Manual procedure to verify the ATP benchmark view panel works end-to-end
against a real eco server and handles network state transitions correctly.

## When this runs

- **Manually**, before closing the feature (`@id:atp-benchmark-view`), to
  confirm the panel integrates correctly with a running eco server.
- **Manually**, after any significant change to benchmark fetch logic or
  panel rendering (e.g., changes to `core/benchmarks.py`,
  `core/benchmark_service.py`, or the web panel component).

## The guarantee

The procedure verifies one sentence:

> The benchmark view panel displays live benchmark data from the eco server,
> renders the list correctly, can open a leaderboard, and transitions to
> `unavailable` (never to "0 benchmarks") when the server stops, within the
> worst-case bound of the fetch throttle plus the UI poll (~80s: up to 60s
> for the service's per-fetch throttle to clear, plus up to two 10s UI
> polls — one to start the next fetch, one to render its result).

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
  uv run uvicorn --factory \
    atp.dashboard.v2.factory:create_app --port 8600
```

`factory.py` has no module-level `app` — only the `create_app()` factory
function, so `uvicorn` must be invoked with `--factory` against
`create_app` (not `app`). `create_app(config=None)` reads its
configuration from the environment on each call, so the env vars above are
picked up correctly.

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

- **State label**: `not fetched yet` (or similar) on the very first render —
  the page's initial `refresh()` call fires immediately on load, but the
  background fetch it kicks off has not completed yet, so this first render
  always shows the not-fetched-yet state.
- **On the next poll (~10 seconds later)**: the panel transitions to showing
  a list of benchmarks (empty if none exist on the eco server, or populated
  if the server has seeded data) — the background fetch that started on load
  is normally done well within 10 seconds against a local server, so the
  next poll picks up its result.

### 5. Verify benchmark list rendering

Once the panel shows "not fetched yet", click or wait for the list to load.

Verify:

- The list of benchmarks appears (or is empty if no benchmarks were seeded).
- Each benchmark entry is clickable.
- Click on any benchmark entry to open its leaderboard view.
- Leaderboard displays runs and scores (or is empty if no runs exist).

### 6. Kill the eco server and observe the state transition

In the terminal running the eco server (step 1), press `Ctrl+C` to stop it.

**Observe the benchmark view panel** over the next ~80 seconds worst case:
the last successful fetch keeps the report `ok` (stale) until the service's
60-second fetch throttle clears, then the next 10-second UI poll starts a
new fetch (which fails fast against the stopped server), and the following
10-second poll renders the resulting `unavailable` report. The panel should:

- Transition to a state indicating the server is unavailable (e.g.,
  `unavailable` or similar).
- **Never** display "0 benchmarks" or an empty list as if the server is
  reachable but has no data.
- The distinction is critical: unavailable state ≠ empty list.

### 7. Restart the eco server and re-confirm

Restart the eco server using the command from step 1. By the same worst-case
~80-second bound as step 6 (60s fetch throttle + up to two 10s UI polls),
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

- `dispatcher/core/benchmarks.py` for fetch and state-classification errors.
- `dispatcher/core/benchmark_service.py` for freshness/throttle errors.
- The web panel JavaScript in `dispatcher/server/static/` for render bugs.
- The browser console for client-side errors.
- The server logs (`http://localhost:8787` startup messages) for HTTP errors.

## Related surfaces

| Runbook | Purpose |
|---|---|
| `docs/revendor-atp-benchmark-api.md` | Re-vendor the ATP benchmark API contract when upstream changes. |
