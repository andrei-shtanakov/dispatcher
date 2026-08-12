# Runbook: re-vendor `steward-roles-catalog/v1`

Canonical procedure for moving dispatcher's vendored copy of steward's
`profiles/roles.yaml` to a new producer commit. The copy was first created
by this same script (inbox #128); there is no separate creation document to
consult.

## When this runs

**Manually, after a change to `profiles/roles.yaml` has been accepted in
steward — usually announced by a red `upstream-drift` run.**

This contract has both guarantees from day one: offline copy-integrity in
every PR (`tests/test_roles_catalog_vendor.py`, never skipped) and the
scheduled `drift-steward-roles-catalog` job in
`.github/workflows/upstream-drift.yml`, which compares the vendored copy
against steward's moving `master` weekly. A red drift run means a human owes
a deliberate re-vendor PR — never a hand edit of an expected hash.

Do not start from a green test suite as evidence that the pin is current:
the suite proves the vendored copy matches its own manifest, which stays
true forever no matter how far upstream travels.

The catalog's own stability policy shapes what a re-vendor may bring
(DEC-007 §1): a role slug is a stable machine name, and the composition is
pinned to the catalog's `version` field — any composition change upstream
arrives with a version bump. `tests/test_roles_catalog_vendor.py` pins the
v1 composition (six slugs and the slug grammar) on this side; a re-vendor
that changes the composition updates that assertion deliberately, together
with any consumer behaviour the change implies.

## The guarantee

The procedure exists to support one sentence:

> The file in `contracts/steward-roles-catalog/v1/` is, byte for byte,
> the blob of the commit recorded in that directory's `manifest.json`.

Note what it does not say. Three SHAs agreeing with each other — the
manifest's, `PINNED.txt`'s, and the literal in
`tests/test_roles_catalog_vendor.py` — proves only that someone edited three
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
scripts/revendor_steward_roles_catalog.sh <NEW_PIN>
```

It fetches `NEW_PIN` from `https://github.com/andrei-shtanakov/steward` into
a throwaway bare object store, extracts `profiles/roles.yaml` into a staging
directory beside the vendored copy, verifies the staged file against the
commit's blob and the file set in both directions, writes `PINNED.txt`,
regenerates `manifest.json` from the same SHA (via
`scripts/vendor_manifest.py --contract steward-roles-catalog`), checks the
manifest records that SHA, verifies the staged bytes a second time, and only
then swaps staging into place. Until that last step the working copy is
untouched; every ordinary failure runs the restoring trap and leaves the
working copy exactly as it was.

The script is an adapted copy of `revendor_steward_gate_catalog.sh` — same
staging/verify/swap machinery, single-file surface. The `SIGKILL` caveat
carries over: a `kill -9` landing between the two final renames can leave
`contracts/steward-roles-catalog/v1/` missing with its contents parked in an
untracked `v1.prev/` sibling; the recovery is
`mv contracts/steward-roles-catalog/v1.prev contracts/steward-roles-catalog/v1`.

**Offline variant.** `--from <git-repo>` reads the commit out of a local
repository's object database instead:

```bash
scripts/revendor_steward_roles_catalog.sh <NEW_PIN> --from ../steward
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

`tests/test_roles_catalog_vendor.py` holds its own copy of the pin:

```python
PRODUCER_COMMIT = "…"
```

This stays a hand edit **on purpose**: it is the independent assertion about
what the manifest should say, and a test that reads the value it checks
proves nothing. The same file pins the catalog's *content* — v1's six role
slugs and the slug grammar; a composition change updates those assertions
deliberately (see the stability policy above).

### 4. Run the gate

```bash
uv run pytest tests/test_roles_catalog_vendor.py \
  tests/test_roles_catalog_drift.py \
  tests/test_revendor_roles_catalog_script.py -v
uv run pytest
```

The first command is the contract's own surface; the second is the repo
rule — a re-vendor must leave the whole suite green.

### 5. Deliver as its own PR

Branch, push, `gh pr create`, act on the GitHub Copilot review, and let a
human merge. Keep the re-vendor free of unrelated changes. State in the PR
body which provenance mode was used, and paste the script's report.

## The next gate-check binary re-pin

Inbox #128 carried operational warnings that belong to the *binary* re-pin
(`scripts/install_pinned_steward.sh` / the live governance smoke), not to
this contract's copy — recorded here so the runbook that gets opened when
the smoke goes red explains why:

- From steward `b79c858` on, `roles.yaml` is a **mandatory neighbour of the
  profile on every gate-check run**; the vendored copy is the correct
  neighbour to place. A minimal one-slug catalog does not resolve the test
  bundle's roles (`product`, `qa`) — exit 2, a designed loud refusal, not a
  bug.
- The smoke's test profile must be in canonical form: a legacy `"@product"`
  role reference in a profile is a hard `ProfileError`, exit 2.
- `role-assignments.yaml` is NOT needed for the solo smoke
  (`solo_auto_approve: true`); it is mandatory only for non-solo profiles.
- Identities are exact strings without case-folding; the canonical spelling
  is `github:andrei-shtanakov`.

## Related surfaces

| Contract | Procedure |
|---|---|
| `contracts/steward-roles-catalog/v1` | this runbook |
| `contracts/steward-gate-catalog/v1` | `docs/revendor-steward-gate-catalog.md` |
| `contracts/steward-gate-verdicts/v1` | `docs/revendor-steward-gate-verdicts.md` |
| `contracts/github-checker-actions/v1` | `docs/revendor-github-checker-actions.md` |
| `packages/plan-fields/src/plan_fields/contract` | offline integrity in `dispatcher/core/contracts.py` + the same `upstream-drift.yml` (its own job) |
