# Runbook: re-vendor `github-checker-actions/v1`

Canonical procedure for moving dispatcher's vendored copy of
`github-checker`'s `contracts/actions/v1` to a new producer commit. This
file is the procedure; `docs/superpowers/plans/2026-07-31-vendor-actions-v1.md`
records how the copy was first created and is not maintained.

## When this runs

**Manually, after a red run of the advisory drift watcher, or after you have
otherwise learned that `contracts/actions/v1` changed in github-checker.**

The `plan-fields` contract has two guarantees — offline integrity in every
PR, plus a scheduled `upstream-drift.yml` that watches canon — and actions/v1
now has both too. `.github/workflows/actions-upstream-drift.yml` runs
`scripts/actions_drift_report.py` on a schedule (and on demand via
`workflow_dispatch`): it checks out `github-checker` at its moving default
branch, recomputes upstream's tree hash with the same algorithm the vendored
copy was built with (`vendor_manifest.build_manifest` — upstream publishes no
manifest of its own for this contract, so there is nothing else to compare
against), and compares it against the `tree_sha256` already recorded in
`contracts/github-checker-actions/v1/manifest.json`. It also lists, when the
pin is still present in the checkout, the commits that touched
`contracts/actions/v1` since the pin — a different question from whether the
content actually changed (a reverted edit produces commits with no drift).

This is **advisory, like its plan-fields sibling: it never blocks a
dispatcher pull request**, because a commit in a neighbouring repository
must not be able to redden this repo's gate. The two red outcomes call for
different actions, and confusing them is exactly the failure this workstream
exists to prevent:

- **Exit 1, drift** — upstream was read and differs. A human owes a
  deliberate re-vendor PR: read what changed upstream, decide whether to
  take it, and run the procedure below. **The fix is never to hand-edit
  `manifest.json`'s `tree_sha256`; the hash is the signal, and editing it
  deletes the only thing the watcher produces.**
- **Exit 2, unavailable** — *nothing was compared* (upstream unreadable, the
  vendored copy broken, or the reporter itself failed). There is nothing to
  read about yet, and re-vendoring at a commit nobody could read would be the
  wrong action. The owed action is to repair the observation — the summary
  names what could not be read (a moved repository, a renamed path, a failed
  checkout, an unreadable file) — and only re-vendor once a real drift run
  says to.

Do not start from a green test suite as evidence that the pin is current:
the suite proves the vendored copy matches its own manifest, which stays
true forever no matter how far upstream travels. That is exactly the
guarantee the drift watcher exists to complement, not replace.

## The guarantee

The procedure exists to support one sentence:

> The files in `contracts/github-checker-actions/v1/` are, byte for byte,
> the blobs of the commit recorded in that directory's `manifest.json`.

Note what it does not say. Three SHAs agreeing with each other — the
manifest's, `PINNED.txt`'s, and the literal in `tests/test_contract_ingest.py`
— proves only that someone edited three files consistently. Changed together
and wrongly, they leave every test green while the manifest certifies bytes
that came from somewhere else. That is why the pin is an **input** to the
script and the bytes are checked **against the object database**, not against
the other copies of the number.

## Procedure

### 1. Pick the commit

A full 40-hex commit id from github-checker, already reviewed and merged
there. Abbreviations and branch names are refused: they resolve today and
identify nothing tomorrow.

### 2. Run the script

```bash
scripts/revendor_github_checker_actions.sh <NEW_PIN>
```

It fetches `NEW_PIN` from `https://github.com/andrei-shtanakov/github-checker`
into a throwaway bare object store, extracts `contracts/actions/v1` into a
staging directory beside the vendored copy, verifies every staged file
against the commit's blobs and the file set in both directions, writes
`PINNED.txt`, regenerates `manifest.json` from the same SHA, checks the
manifest records that SHA, verifies the staged bytes a second time, and only
then swaps staging into place.

Until that last step the working copy is untouched. Every ordinary failure —
a bad or unknown commit, a failed provenance check, a corrupted or hollow
manifest, `Ctrl-C` (`INT`), `TERM`, `HUP` — runs the restoring trap and
leaves the working copy exactly as it was. For `INT` and `TERM` this is
verified, not assumed:
`tests/test_revendor_script.py::test_a_signal_mid_run_leaves_the_working_copy_alone`
sends a real signal to the running script mid-extraction (not a simulation)
and asserts the working copy is untouched afterward — run it yourself with
`uv run pytest tests/test_revendor_script.py -k test_a_signal_mid_run_leaves_the_working_copy_alone -v`.
`HUP` shares the same `trap cleanup EXIT` and is not separately exercised by
a signal test.

The one case the trap cannot cover is `SIGKILL` (`kill -9`) landing in the
narrow window between the two renames at the very end of the swap. A trap
does not run on `SIGKILL`, so a kill in that exact window can leave the
vendored directory absent from the working copy with its real contents
parked, untouched, in an untracked sibling directory. If `git status` ever
shows `contracts/github-checker-actions/v1/` missing alongside an untracked
`contracts/github-checker-actions/v1.prev/`, that is what happened, and the
recovery is one command:

```bash
mv contracts/github-checker-actions/v1.prev contracts/github-checker-actions/v1
```

The window is two renames wide and `SIGKILL` in it is rare, but it is the
one failure this procedure cannot restore automatically.

**Offline variant.** `--from <git-repo>` reads the commit out of a local
repository's object database instead:

```bash
scripts/revendor_github_checker_actions.sh <NEW_PIN> --from ../github-checker
```

It proves less, and the report says so: the bytes belong to `NEW_PIN` **in
that object store**, and whether the canonical remote has that commit was
not asked. A clean `git status` in the source is not required and would not
help — nothing reads its working tree. Use the default whenever you have
the network.

**Exit codes:** `0` ok · `1` usage · `2` source or commit unavailable ·
`3` provenance mismatch · `4` manifest generation or read-back ·
`5` internal failure — the working copy was left as it was found. Any
other nonzero status (127, 128, …) is an unexpected internal failure; the
restoring trap has already put the working copy back.

A `3` means the staged bytes are not the commit's. Do not re-run hoping for
a different answer, and never adjust an expected value to match: something
between the object store and the disk is wrong, and that is the finding.

### 3. Update the independent literal — and, if the surface changed, what goes with it

`tests/test_contract_ingest.py` holds its own copy of the pin:

```python
PRODUCER_COMMIT = "…"
```

This one stays a hand edit **on purpose**. It is the independent assertion
about what the manifest should say, and a test that reads the value it
checks proves nothing.

For a **same-surface** re-vendor — the fixture files, the schema's `$defs`,
and the action verbs are unchanged from the previous pin — this literal is
the only edit this step requires. The suite goes red here until you change
it; that red is the last checklist item, not an obstacle.

A re-vendor that also changes the surface reddens more than the pin literal,
because a few counts in this repo are hand-maintained by design rather than
derived from the vendored copy, and they move independently of it:

- `tests/test_contract_ingest.py:107` — `test_all_thirty_four_fixtures_are_present`
  asserts the fixture count (and says so in its own name)
- `tests/test_contract_ingest.py:661` — `assert len(_verb_defs()) == 8`
- `_VARIANT_DEFS` in `dispatcher/core/contract.py` — the suite itself
  describes it as "hand-maintained and load-bearing"

Update these **deliberately**, to match what actually changed upstream —
never to silence a red you have not accounted for. An operator following
this runbook who hits an unexpected red on a *vendoring* run naturally reads
it as "the script is broken"; it usually means the surface moved and one of
these three still names the old shape.

### 4. Run the full gate with the matching binary

```bash
PATH="$(scripts/install_pinned_checker.sh):$PATH" uv run pytest tests/ -v
```

`install_pinned_checker.sh` reads the commit from the manifest the script
just rewrote, so the binary moves with the contract automatically. The
level-3 smoke (`test_write_path_live_smoke_real_binary`) then exercises the
real producer at the new pin, and PEP 610 install metadata proves the binary
is that commit. Node 22 must be on PATH too — `test_task_authoring_js.py`
fails rather than skips without it.

### 5. Deliver as its own PR

Branch, push, `gh pr create`, act on the GitHub Copilot review, and let a
human merge — the repo's standing rule. Keep the re-vendor free of unrelated
changes: a diff of thousands of vendored lines plus a behaviour change is a
diff nobody can review.

State in the PR body which provenance mode was used, and paste the script's
report.

After the merge: `git switch master && git pull --ff-only`, then check that
CI on master is green — in particular that the `install the pinned producer
binary` step names the new commit.

## What this runbook does not cover

| Contract | Procedure |
|---|---|
| `contracts/github-checker-actions/v1` | this runbook |
| `contracts/github-checker-snapshot/v1` | legacy shape — a hash table in its README, no manifest; needs its own migration before any of this applies |
| `contracts/executor-config` | separate contract; procedure not established |
| `packages/plan-fields/src/plan_fields/contract` | its own mechanisms — offline integrity in `dispatcher/core/contracts.py` plus scheduled `upstream-drift.yml`; see the README's "Two contract guarantees" section |

The four surfaces have different pin formats, checks and sources. A single
unified procedure would be a false abstraction until at least the snapshot
contract is migrated — that is a project, not a documentation change.
