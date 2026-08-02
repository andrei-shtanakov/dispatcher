# Runbook: re-vendor `steward-gate-verdicts/v1`

Canonical procedure for moving dispatcher's vendored copy of steward's
`contracts/gate-verdicts/v1` to a new producer commit. The copy was first
created by this same script (WS-005 WS-B, inbox #106); there is no separate
creation document to consult.

## When this runs

**Manually, after a change to `contracts/gate-verdicts/v1` has been accepted
in steward — usually announced by a red `upstream-drift` run.**

This contract has both guarantees from day one: offline copy-integrity in
every PR (`tests/test_gate_verdicts_vendor.py`, never skipped) and the
scheduled `drift-steward-gate-verdicts` job in
`.github/workflows/upstream-drift.yml`, which compares the vendored copy
against steward's moving `master` weekly. A red drift run means a human owes
a deliberate re-vendor PR — never a hand edit of an expected hash.

Do not start from a green test suite as evidence that the pin is current:
the suite proves the vendored copy matches its own manifest, which stays
true forever no matter how far upstream travels.

A breaking change upstream arrives as a new `v2/` directory in steward, not
as edits to `v1/` (the contract's own versioning rule). That is a new
vendoring surface and a consumer-code change, not a re-vendor of this one.

## The guarantee

The procedure exists to support one sentence:

> The files in `contracts/steward-gate-verdicts/v1/` are, byte for byte,
> the blobs of the commit recorded in that directory's `manifest.json`.

Note what it does not say. Three SHAs agreeing with each other — the
manifest's, `PINNED.txt`'s, and the literal in
`tests/test_gate_verdicts_vendor.py` — proves only that someone edited three
files consistently. That is why the pin is an **input** to the script and the
bytes are checked **against the object database**, not against the other
copies of the number.

## Procedure

### 1. Pick the commit

A full 40-hex commit id from steward, already reviewed and merged there.
Abbreviations and branch names are refused: they resolve today and identify
nothing tomorrow.

### 2. Run the script

```bash
scripts/revendor_steward_gate_verdicts.sh <NEW_PIN>
```

It fetches `NEW_PIN` from `https://github.com/andrei-shtanakov/steward` into
a throwaway bare object store, extracts `contracts/gate-verdicts/v1` into a
staging directory beside the vendored copy, verifies every staged file
against the commit's blobs and the file set in both directions, writes
`PINNED.txt`, regenerates `manifest.json` from the same SHA (via
`scripts/vendor_manifest.py --contract steward-gate-verdicts`), checks the
manifest records that SHA, verifies the staged bytes a second time, and only
then swaps staging into place. Until that last step the working copy is
untouched; every ordinary failure runs the restoring trap and leaves the
working copy exactly as it was.

The script is an adapted copy of `revendor_github_checker_actions.sh` — same
staging/verify/swap machinery, different producer constants. The `SIGKILL`
caveat carries over: a `kill -9` landing between the two final renames can
leave `contracts/steward-gate-verdicts/v1/` missing with its contents parked
in an untracked `v1.prev/` sibling; the recovery is
`mv contracts/steward-gate-verdicts/v1.prev contracts/steward-gate-verdicts/v1`.

**Offline variant.** `--from <git-repo>` reads the commit out of a local
repository's object database instead:

```bash
scripts/revendor_steward_gate_verdicts.sh <NEW_PIN> --from ../steward
```

It proves less, and the report says so: the bytes belong to `NEW_PIN` **in
that object store**, and whether the canonical remote has that commit was
not asked. Use the default whenever you have the network.

**Exit codes:** `0` ok · `1` usage · `2` source or commit unavailable ·
`3` provenance mismatch · `4` manifest generation or read-back ·
`5` internal failure — the working copy was left as it was found.

A `3` means the staged bytes are not the commit's. Do not re-run hoping for
a different answer, and never adjust an expected value to match.

### 3. Update the independent literal

`tests/test_gate_verdicts_vendor.py` holds its own copy of the pin:

```python
PRODUCER_COMMIT = "…"
```

This stays a hand edit **on purpose**: it is the independent assertion about
what the manifest should say, and a test that reads the value it checks
proves nothing. The same file's `test_the_expected_surface_is_present` pins
the exact file set (schema, README, five fixtures) — if upstream added or
renamed a fixture, update that set deliberately, and extend
`tests/test_governance_collector.py` to cover any new negative class the
canon now ships.

### 4. Run the gate

```bash
uv run pytest tests/test_gate_verdicts_vendor.py tests/test_governance_collector.py -v
uv run pytest
```

The first command is the contract's own surface; the second is the repo
rule — a re-vendor must leave the whole suite green.

### 5. Deliver as its own PR

Branch, push, `gh pr create`, act on the GitHub Copilot review, and let a
human merge. Keep the re-vendor free of unrelated changes. State in the PR
body which provenance mode was used, and paste the script's report.

## Related surfaces

| Contract | Procedure |
|---|---|
| `contracts/steward-gate-verdicts/v1` | this runbook |
| `contracts/github-checker-actions/v1` | `docs/revendor-github-checker-actions.md` |
| `packages/plan-fields/src/plan_fields/contract` | offline integrity in `dispatcher/core/contracts.py` + the same `upstream-drift.yml` (its own job) |
