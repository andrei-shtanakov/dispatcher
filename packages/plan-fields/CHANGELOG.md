# Changelog — plan-fields

## 0.7.0 — 2026-07-29

Manifest-declared repo identity threaded through resolution (ADR-ECO-005).

**BREAKING** — the manifest parameter changes type on three public functions:
`parse_fleet(inputs, index)`, `check_legacy_fleet(inputs, index, exclude=None)`
and `fleet.classify_legacy(fleet, index)` now take a `ManifestIndex` where they
took a `set[str]`. A set has nowhere to record that `prograph-vault` and
`ecosystem-kb` name one repo, so a reference written with the checkout spelling
could never resolve. Build one with `manifest_index(path)`; a caller with no
manifest can pass `ManifestIndex(frozenset(names), {})`.

- Every written repo name is normalised to its canonical key before membership,
  availability or target lookup is decided — including a `RepoInput`'s own
  `repo`, so a caller supplying a repo under its `git_dir` spelling still mints
  `todo://<key>/<id>`. `parse_fleet`'s duplicate check runs on the normalised
  names, so two spellings of one repo now raise instead of overwriting each
  other. `LegacyRef.target_repo` and
  `LegacyDiagnostic.target_repo` carry the key; `raw` / the new
  `LegacyDiagnostic.raw_ref` keep the spelling as written.
- A legacy `<repo>#<slug>` written with a declared `git_dir` normalises its
  repo component into `legacy_blocker_ref` only: it gains no `resolved_target`
  and never becomes an edge (contract, *Identity & provenance*).
- `manifest_repos()` now also skips `member = true` entries — this **changes the
  answer**: `atp-platform-sdk` describes a package inside `atp-platform`, is
  never cloned on its own, and leaves the repo set and the `fleet-graph` nodes.
- `resolve_checkout()` applies one predicate per candidate spelling (folder name,
  then origin-derived name): normalise it, accept it only if the result is a
  declared key. A folder named exactly like its key now resolves even when the
  entry declares a different `git_dir`; an undeclared checkout still degrades
  visibly to its origin-derived name and never borrows a key.
- New `checkout_map(root, index)`; `scan_workspace` is built on it. Two
  checkouts resolving to one canonical name are settled by what they supply,
  never by directory order: the one with a `TODO.md` wins; **two** with a
  `TODO.md` raise `ValueError` (previously the later one silently overwrote the
  earlier); neither having one is immaterial, since such a checkout contributes
  no node, reference or diagnostic and its commit is never emitted. So a bare
  second clone beside a real checkout keeps working, and a second *plan-bearing*
  clone of a manifest repo now aborts the command.
- `parse_fleet` and `check_legacy_fleet` raise `ValueError` when two
  `RepoInput`s normalise to one repo. `check_legacy_fleet` previously had no
  such guard: normalisation could merge two spellings, the second input
  overwrote the first, and the whole losing `TODO.md` — with any diagnostic in
  it — disappeared depending on argument order.
- The CLI turns these refusals (and `manifest_index`'s pre-existing ambiguous-
  `git_dir` error, which used to surface as a traceback) into a
  `plan-fields: <message>` line on stderr with **exit code 2**, distinct from
  the exit code 1 that means "invalid document" or "drift found".
- `fleet-graph` and `fleet-legacy` resolve checkouts through the manifest, so a
  repo cloned into its declared `git_dir` is no longer reported as absent.

## 0.1.0 — 2026-07-28

Initial offline package (PF-3, ADR-ECO-005).

- Parser for a single repo `TODO.md` → canonical plan-fields document (nodes /
  references / edges / diagnostics).
- Canonicalization: ordering by identity with the `(path, line)` collision
  tie-breaker; canonical JSON dump.
- Validator against the vendored `schema.json`; `run_conformance()` over the
  vendored fixture suite (6 pairs + 1 history bundle).
- CLI: `parse`, `validate`, `conformance`.
- Vendored, pinned copy of the plan-fields v1 contract under
  `src/plan_fields/contract/` (`PINNED.txt` records the source commit).
- Standalone: no dispatcher import (enforced by a test).
