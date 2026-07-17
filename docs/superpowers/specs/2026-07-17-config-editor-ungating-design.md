# Design — Config-editor un-gating: write path via `propose-pr`

> **Context (2026-07-17, evening):** the spec-runner config editor shipped in
> PR #37 with its write path explicitly gated (`SPEC_RUNNER_CONFIG_WRITE_GATED`,
> tested 503) because `github-checker open-pr` never branches/commits/pushes —
> the final whole-branch review caught that every test had masked this with
> `{"ok": true}` stubs. github-checker has since shipped the `propose-pr`
> command designed to close exactly this gap (its PR #11, merge `7684d27`;
> spec/plan in its PRs #9/#10). The cross-repo handoff
> (`prograph-vault/authored/notes/2026-07-17-github-checker-open-pr-needs-branch-commit-push.md`)
> is marked resolved. This design is the consumer-side delta: un-gate the
> write path and rework it onto `propose-pr`. It amends
> `2026-07-17-spec-runner-config-editor-design.md` (DESIGN-3xx); numbering
> continues as DESIGN-4xx. No new stakeholder decision is required — every
> choice below was fixed by the prior design, the final review's findings, or
> the propose-pr contract.

## 1. What `propose-pr` provides (consumed contract)

`github-checker propose-pr <dir> --message <msg> --edit <repo-path>=<content-file>
[--if-match <repo-path>=<sha256>] [--branch <name>]` — applies the given
content in a temporary worktree branched from `origin/<default>`, commits,
pushes a fresh branch, opens a PR via `gh pr create --fill`. Prints one JSON
`ActionResult`: `ok/detail/error/pr_url` plus additive `branch`,
`base_branch`, `commit_sha`, `changed_paths`. The operator's live
working-tree files are never read as content source nor modified by it.

Two contract sharp edges dispatcher MUST honor:
- **No-op**: `ok=false`, exit 1, `detail == "no-op"` — a structural marker,
  by design. Branch on `detail`, never on ok/exit-code, or "save an
  unchanged config" surfaces as a failure.
- **`--if-match`** compares against the **raw blob bytes at
  `origin/<default>`**, not the live tree. See DESIGN-401 for how dispatcher
  derives the hash and what a divergence means.

## 2. Components

### DESIGN-401: Runner rework (`core/spec_runner_config_actions.py`)

`SpecRunnerConfigActionRunner.run()` no longer writes `project.yaml` in the
live tree. New flow:

1. Validate candidate (unchanged: typed unknown-field check,
   `validate_candidate`, `_target`, busy-lock, mtime conflict check — all
   guards and their order stay exactly as shipped).
2. Read the on-disk `project.yaml` bytes **once** (read-only — the same file
   the read-model already reads); compute `sha256` of those raw bytes. The
   mtime check just proved the file equals what the form was rendered from,
   so this hash represents the render-time base.
3. Render the new YAML text with the existing `build_new_yaml_text`
   round-trip logic (reads the live file as base, per DESIGN-402's key
   emission) — but write it to a **temp file** under a
   `tempfile.TemporaryDirectory()`, never to the live tree.
4. Invoke:
   `[*self._command, "propose-pr", str(target_dir), "--message", <msg>,
   "--edit", f"project.yaml={tmp_file}",
   "--if-match", f"project.yaml={sha256}"]`
   with the existing timeout/JSON-parse handling. Parse ALL four additive
   fields into `ActionOutcome` (new optional fields, additive: `branch`,
   `base_branch`, `commit_sha`, `changed_paths`) — propose-pr already emits
   them and they are audit/debug gold; the UI may ignore them.
   `pr_url/detail/error` as today.
5. Temp dir cleanup in `finally`.

`--message` format: `chore(spec-runner): update config (<comma-separated
changed typed keys>[, extra_executor_config])` — derived from the DESIGN-402
diff, stable and greppable. **Fallback:** when the changed-keys list is
empty (structural-only change, or anything else that yields no listable
keys), the message is the bare `chore(spec-runner): update config` — never
empty parentheses.

**`--if-match` divergence semantics (accepted, by design):** the hash is of
the live on-disk file; `propose-pr` compares against `origin/<default>`. If
the observed repo's `project.yaml` has local uncommitted changes or the
clone is ahead/behind origin on that file, the guard mismatches and
`propose-pr` refuses with "base file changed; reload required". That is the
honest outcome — the form was rendered from a state that is not what the PR
would be based on. No dispatcher-side special-casing. **Known limitation
(named, accepted):** git filters/EOL normalization could make worktree
bytes differ from the raw blob bytes even without a semantic difference;
`project.yaml` is treated as ordinary text without custom filters, and a
filter/EOL mismatch is simply a reload-required refusal, not something
dispatcher compensates for.

The live tree is now **read-only** for this action class: `run()` reads
`project.yaml` (as the read-model always has) and writes nothing outside its
own temp dir. The original design's §4 caveat (cross-class lock race between
a `pull` and a live-tree config write) is closed by construction — there is
no live-tree write left to race. Per the amended §5 of the original design,
the "simultaneous `pull` is unaffected (different lock)" test stands.

### DESIGN-402: Emit only explicit-or-changed typed keys

`build_new_yaml_text` currently materializes all 12 typed fields. New rule —
the rendered `spec_runner:` block contains a typed key iff:

- the key is **explicit** in the current on-disk block (preserve the user's
  prior intent, even when its value equals the default), OR
- the candidate value **differs from `TYPED_DEFAULTS[key]`** (the user set
  something non-default in the form).

Keys that are implicit-and-left-at-default are omitted — dispatcher's
mirrored defaults are never materialized into the file, closing both
final-review Importants at once: the in-app preview (which already renders
only changed keys) now matches the real PR diff, and a stale
`TYPED_DEFAULTS` mirror can no longer silently bake a wrong default into an
observed repo.

`extra_executor_config` passthrough is unchanged: included iff non-empty
(as shipped).

### DESIGN-403: No-op surfaced as benign

The runner passes `detail` through as today. The web UI's submit handler
branches: `data.detail === "no-op"` → neutral result line («конфиг уже в
этом состоянии — PR не нужен»), styled as info, not `✗` error. All other
`ok=false` outcomes render as failures, unchanged. Server-side mapping is
untouched (HTTP 200 with the outcome body, same as `pull`/`create-pr`
failures).

### DESIGN-404: Gate removal

- Delete `SPEC_RUNNER_CONFIG_WRITE_GATED`, its route check, and its TODO
  comment (`server/app.py`).
- Delete `test_spec_runner_config_update_gated_returns_503`; remove the
  `monkeypatch.setattr(app_module, "SPEC_RUNNER_CONFIG_WRITE_GATED", False)`
  lines (and their comments) from the four kept post-gate tests.
- Web UI: restore the two-step preview→confirm flow (remove the disabled
  "PR gated" state; the `data-armed` confirm step returns).

### DESIGN-405: Testing — the anti-stub mandate

The gate existed because `{"ok": true}` stubs masked a broken contract.
The un-gated write path is therefore tested at three levels:

1. **Unit (existing tests, reworked):** the runner tests assert the NEW
   contract — `project.yaml` in the workspace is byte-for-byte untouched
   after `run()`; the invoked argv is `propose-pr` with `--message`,
   `--edit project.yaml=<existing temp file>`, `--if-match project.yaml=<
   correct sha256 of the on-disk bytes>`; the temp content file contains the
   DESIGN-402-filtered YAML. The fake binary remains a JSON-printing script
   here (unit scope), but the assertions are about dispatcher's side of the
   contract, not the binary's.
2. **Integration (new, real git):** a fake `github-checker` SCRIPT that
   implements propose-pr's observable contract with REAL git — creates a
   branch off `origin/<default>` in a temp worktree, applies the `--edit`
   content, commits, pushes to a real temp bare origin, prints the real
   `ActionResult` JSON. The test asserts end-to-end: the edit lands as a
   commit on a fresh branch in the bare origin, the live workspace clone is
   byte-for-byte untouched, and the API route returns the PR-ish result.
3. **Live smoke (new, skipped when unavailable):**
   `@pytest.mark.skipif(shutil.which("github-checker") is None, ...)` — runs
   the REAL binary against a temp origin+clone pair with a fake `gh` on
   PATH, asserting the same observables. Runs on dev machines where
   github-checker is installed; skipped in dispatcher CI. The binary's own
   contract is covered by its repo's 162 tests.

No-op path — pinned at the subprocess boundary: the fake binary for this
test MUST exit with **returncode 1 while printing the `ActionResult` JSON
(`ok=false, detail="no-op"`) on stdout** — exactly propose-pr's real
behavior. `_invoke` parses stdout independently of the return code today;
this test exists so nobody later "fixes" non-zero exits into a blanket
"github-checker returned no JSON" error and breaks the no-op contract. The
API test client additionally confirms `detail` reaches the HTTP client
unmangled.

Note on level 3's reach: the live smoke skips wherever the binary is not on
PATH — which currently includes the primary dev machine, not just CI. Level
2 (real-git fake binary) is therefore the acceptance bar for this PR; the
live smoke is opportunistic extra assurance once `github-checker` is
installed.

### DESIGN-406: Documentation updates

- `2026-07-17-spec-runner-config-editor-design.md`: add a short
  "DESIGN-304 amendment (un-gated)" note — flow now renders content to a
  temp file and delegates to `propose-pr`; the §4 lock-race caveat is closed
  by construction (no live-tree writes); pointer to this document.
- `core/spec_runner_config_actions.py` module docstring: reflect the new
  flow (renders content, hands it to propose-pr; never writes the live
  tree — the module's mutation surface is now zero on observed repos).
- `README.md`: it still describes dispatcher as a "read-only monitoring
  dashboard" — no longer accurate once the write path is live. Amend the
  opening description to name the narrow, click-gated PR-only mutation
  whitelist (sync actions + the config-editor content-PR action).
- `CLAUDE.md`: verify the runtime-exception wording (the X-02 bullet) still
  reads correctly now that the write path is live rather than gated; adjust
  the gate reference if it names the flag.
- Project memory / COWORK_CONTEXT: gate removed, write path live.

## 3. Error handling (delta rows)

| failure | behaviour |
|---|---|
| propose-pr exits non-zero with its own error (if-match mismatch, branch collision, gh failure, ...) | surfaced as-is in `ActionOutcome.error` (HTTP 200, ok=false) — no auto-retry, matching every other action |
| propose-pr reports `detail="no-op"` | benign info in UI (DESIGN-403), not an error state |
| `github-checker` binary missing / times out | unchanged: failed outcome with the exception string (existing `_invoke` handling) |
| temp-dir creation fails | failed outcome (`ok=false`), never an exception out of `run()` |
| live `project.yaml` diverged from `origin/<default>` | propose-pr's if-match refuses ("base file changed; reload required") — honest, no special-casing (DESIGN-401) |

## 4. Out of scope

- TUI parity (FR-06 / DESIGN-308) and AI value suggestions (DESIGN-307) —
  still deferred, unchanged.
- `extra_executor_config` editing UI (the DESIGN-306 sub-forms gap) — still
  deferred; the overlay continues to round-trip unedited.
- Vendoring a pinned `contracts/actions/v1` schema for the ActionResult
  JSON — remains the optional follow-up noted in the propose-pr spec §10;
  dispatcher continues to parse the JSON tolerantly (unknown fields
  ignored).
- Any change to sync actions (`pull`/`create-pr`) or their lock.

## 5. Traceability

| Item | Design |
|---|---|
| Gate resolution (product decision, PR #37 gate) | DESIGN-404 |
| propose-pr contract (github-checker PR #11) | §1, DESIGN-401 |
| Final-review Important: preview fidelity | DESIGN-402 |
| Final-review Important: TYPED_DEFAULTS drift → write corruption | DESIGN-402 |
| Consumer sharp-edge: `detail=="no-op"` | §1, DESIGN-403, DESIGN-405 |
| Anti-stub mandate (gate's root cause) | DESIGN-405 |
| Original design §4 lock-race caveat | DESIGN-401 (closed by construction) |
| Handoff resolution | context note; prograph-vault note already updated |

## 6. Milestone

Single milestone: DESIGN-401..406 land together in one PR — the gate flip
is only honest when the reworked path and its real-git tests arrive in the
same change.
