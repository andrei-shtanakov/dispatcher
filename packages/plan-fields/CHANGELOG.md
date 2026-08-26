# Changelog — plan-fields

## 0.10.0 — 2026-08-26

Re-vendored the plan-fields v3 contract to r2 (canon commit `dc12b0e`) and
implemented its optional `@dag` launch-registration tag.

- `parse_dag(item, item_id, repo) -> (dag, diagnostics)` is new public API,
  beside `parse_owner` — operational reporters can classify an item's `@dag`
  without copying the grammar. `repo` is the caller's own identity, used
  only to compose the `todo://` URI in diagnostic text; diagnostics are
  finished message strings, not templates for the caller to `.format()`.
- `scrape.last_tag_is_quoted(raw_text, key) -> bool` is new public API — the
  tokenizer unquotes values into `tags`, so the grammar re-asks the same
  tokenizer whether the LAST occurrence of a key was quoted (last-wins).
- Node output gains an optional `dag` key: present **only** when `@dag`
  passed the grammar (a bare, normalized `dags/<name>.yaml` token) AND
  equals `dags/<id>.yaml` for the item's own `@id`; absent — never null —
  otherwise. `@dag` has no presence obligation: an item without it gets no
  diagnostic.
- Two new diagnostics, both `warning`, both structural (they fire on closed
  items too, the `@epic` precedent): `PF-DAG-GRAMMAR` (malformed or quoted
  value; quoting is rejected — the grammar takes a bare token) and
  `PF-DAG-MISMATCH` (well-formed but names a different item's artifact).
- The line-based parser reads no continuations: `@dag` on a continuation
  line is invisible, same as every other tag.
- Conformance fixtures: 11 → 18 (7 new `@dag` cases).

## 0.7.0 — 2026-07-29

Manifest-declared repo identity threaded through resolution (ADR-ECO-005).

**BREAKING (behaviour)** — a legacy `<repo>#<slug>` reference **never becomes an
edge**, whichever way its repo component is spelled and at whichever layer it is
resolved. Previously a slug matching exactly one item produced an edge and a
`resolved_target`: across repos in `parse_fleet`, and within one repo in
`parse_todo`. Both are gone. Edge-eligibility is decided by the reference's
*syntax*, never by whether its slug happens to match, so `edges` holds only
resolved `todo:// → canonical` relations — the contract's load-bearing split
between the dependency graph (identity) and references (text).

For every `<repo>#<slug>`, key- or `git_dir`-spelled:

- `legacy_blocker_ref` carries the full normalised ref, `<canonical-key>#<slug>`
- `raw_ref` keeps the spelling exactly as written — the only field that does
- `resolved_target` is emitted as `null` (the schema has it in
  `Reference.required` with `oneOf: [CanonicalUri, null]`; it is never omitted)
- no edge, and a unique match emits no diagnostic either — it is a reference,
  not a defect. `PF-LEGACY-AMBIGUOUS` still reports zero or several matches.

Two consequences worth grepping for if you consume the output:

- **`PF-LEGACY-AMBIGUOUS`'s message names the repo canonically.** It read
  `…matches no item in repo {as-written}` and now reads
  `…matches no item in repo {canonical-key}` — the repo that was actually
  searched, which the old text could misname. The reference itself is still
  quoted exactly as the author wrote it, so `<repo>#<slug>` still appears
  verbatim in the message; only the trailing repo name changed.
- **An alias-spelled SELF-reference is now diagnosed, where it used to be
  silent.** `parse_todo` has no manifest, so it read `prograph-vault#x` inside
  `ecosystem-kb` as naming another repo and said nothing, while `ecosystem-kb#x`
  got a `PF-LEGACY-AMBIGUOUS` from it — the two spellings of one repo
  disagreed. `parse_fleet` normalises the name and, when it denotes the source
  repo, runs the *same* self-resolution `parse_todo` uses (literally the same
  function), emitting the *same existing* code. It stays silent when
  `parse_todo` has already spoken, so the key-spelled case is never
  double-reported. Nothing else moves: `parse_todo` on its own is unchanged, the
  conformance fixtures pass byte-for-byte, and the live `fleet-graph` output is
  identical — including the workspace's one same-repo legacy reference.

Migrating a blocker to `@blocked_by:todo://<repo>/<id>` is now precisely what
puts the relation into the graph. `todo://` resolution is untouched and remains
the only path to a cross-repo edge. No effect on the current fleet snapshot: no
legacy reference in the workspace resolves uniquely today, so the live
`fleet-graph` output is unchanged (161 nodes, 15 edges, 109 diagnostics).

**BREAKING (API)** — the manifest parameter changes type on three public functions:
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
  repo component into `legacy_blocker_ref` only (contract, *Identity &
  provenance*) — and, per the behavioural break above, so does one written with
  the key. The two spellings are indistinguishable in every emitted field except
  `raw_ref` and the diagnostic text that quotes it back.
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
  `TODO.md` raise `AmbiguousIdentityError` (previously the later one silently
  overwrote the earlier); neither having one leaves the *derived* answer
  unchanged, since such a checkout contributes no node, reference or diagnostic
  and its commit is never emitted — though the `Path` `checkout_map` returns for
  it is then arbitrary, which matters to a consumer that reads files through it.
  So a bare second clone beside a real checkout keeps working, and a second
  *plan-bearing* clone of a manifest repo now aborts the command.
- New `AmbiguousIdentityError` (a `ValueError` subclass, exported): raised
  wherever two declarations, checkouts or inputs claim one repo identity.
- `parse_fleet` and `check_legacy_fleet` raise it when two
  `RepoInput`s normalise to one repo. `check_legacy_fleet` previously had no
  such guard: normalisation could merge two spellings, the second input
  overwrote the first, and the whole losing `TODO.md` — with any diagnostic in
  it — disappeared depending on argument order.
- The CLI turns exactly those refusals (and `manifest_index`'s pre-existing
  ambiguous-`git_dir` error, which used to surface as a traceback) into a
  `plan-fields: <message>` line on stderr with **exit code 2**, distinct from
  the exit code 1 that means "invalid document" or "drift found". Only
  `AmbiguousIdentityError` is caught: a malformed JSON/TOML input, or a genuine
  bug raising `ValueError`, still reaches the caller rather than being printed
  as if it were the operator's mistake.
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
