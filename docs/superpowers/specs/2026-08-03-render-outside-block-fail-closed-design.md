# Fail-closed rendering of `project.yaml` (`@id:render-outside-block-fail-closed`)

**Status:** design approved 2026-08-03. Follows PR #113
(`@id:ruamel-standalone-comment-loss`), which stopped the renderer from
rebuilding the `spec_runner:` block and losing comments, key order and
unknown keys.

## Problem

`build_new_yaml_text` renders a whole `project.yaml` through ruamel and hands
the result to `github-checker propose-pr`. The file belongs to a neighbour
repo. #113 made the renderer reproduce both real `project.yaml` files in the
workspace byte for byte on a no-op edit — but that is a property observed on
two files, not a guarantee. Where ruamel cannot reproduce a construct, the
editor still silently ships the difference as part of somebody else's PR.

Measured inventory of what escapes the block today (each verified by probe,
not assumed):

| # | Silent out-of-block change | Kind |
|---|---|---|
| 1 | `---` / `...` document markers stripped | style, reproducible |
| 2 | missing final newline added | style, reproducible |
| 3 | CRLF → LF | style, reproducible |
| 4 | BOM stripped | style, reproducible |
| 5 | mapping indent ≠ 2 normalised | style, reproducible |
| 6 | aligned values (`project:␣␣␣␣␣␣alpha`) collapsed | style, **not** reproducible |
| 7 | alias expanded when its anchor lived in the block | real effect of the edit |

Row 7 is not cosmetic and not a style problem: destroying an anchor inside
`spec_runner:` makes ruamel expand every alias to it elsewhere in the file, so
`elsewhere: *over` becomes `elsewhere:` + an inlined copy. No amount of style
matching prevents it.

Constructs that already round-trip exactly, and therefore need no work:
anchors, aliases, merge keys, block scalars (`|`), folded scalars (`>`), flow
collections, all quote styles, unicode, and long lines.

## Approach

Two layers that do not substitute for each other. Preservation makes ordinary
files editable; the gate independently verifies the result. If the
preservation machinery is wrong, the gate still refuses the mutation.

```
source bytes
  → UTF-8 decode
  → inspect source style
  → configure + render reproducing BOM, line endings, markers, indentation
  → UTF-8 encode, exactly once
  → Check A: an unmodified round-trip must equal the source, byte for byte
  → Check C: no node inside the block is reachable from outside it
  → apply the candidate, render with it applied
  → Check B: outside the owned span, source and result must be identical
  → write_bytes those exact bytes; propose-pr sends them
```

### Non-negotiable: the comparators forgive nothing

Rows 1–5 are fixed by making the *rendering* reproduce the source, never by
teaching a comparator to accept their differences. Neither check may hold a
list of allowed normalisations. For Check A that forgiveness is not even
expressible: the whole check is `render(load(text)) == text`.

A useful consequence: **every style-detection heuristic below is an unverified
guess made safe by Check A.** Detection can therefore stay simple. When it
guesses wrong the result is a refusal, never a bad PR.

## The byte pipeline

A check that compares `str` and a write that re-encodes independently do not
compose into "we verified these bytes and sent those bytes". Today the path is
textual end to end: `run()` does `base_bytes.decode()`, `build_new_yaml_text`
returns `str`, and the temp file is written with `Path.write_text`.

`write_text` opens in text mode with `newline=None`, which translates `\n` to
`os.linesep` on write. On Windows the CRLF this design deliberately preserves
would be translated **a second time**, producing `\r\r\n` — bytes that no check
ever saw.

So the boundary is fixed at bytes:

- `build_new_yaml_text(base_bytes: bytes, candidate) -> tuple[bytes, ...]`.
  The decode moves *inside* the function, which also brings it under the safe
  exception below.
- Encode to UTF-8 exactly once. Both checks compare `bytes`, never `str`.
- The caller writes the returned bytes with `write_bytes`. The bytes that
  passed the checks are the bytes `propose-pr` receives.
- `--if-match` continues to hash the `base_bytes` actually read; unchanged.

Byte coordinates in diagnostics are computed on the **encoded bytes**. After
any non-ASCII character a Python string index and a byte offset diverge, and
the reported number has to name the thing that was compared.

## The three guarantees, stated separately

These are distinct, and each is carried by exactly one mechanism. Conflating
them is how a gate comes to look like it covers more than it does.

| Guarantee | Carried by |
|---|---|
| The renderer does not normalise the file unconditionally | **Check A** |
| A change does not escape the `spec_runner:` block | **Check B** (bytes) + **Check C** (meaning) |
| Unknown in-block data survives the candidate mutation | **in-place mutation (#113) + targeted tests** — *not* Check A |

The third row matters. Check A runs *before* the candidate is applied, so a
candidate-mutation bug that drops an unknown key or a comment inside the block
passes Check A untouched, and Check B excludes the block from comparison by
construction. Nothing in this slice detects that class of bug; the tests in
"In-block preservation" below are its only defence.

**The overall guarantee is not narrowed by splitting it across two checks.**
It remains: a PR must not change the meaning of data outside the managed
block. Check B alone only ever bounded that to bytes; Check C carries the
half Check B cannot express — meaning that changes without any outside byte
moving. Together they carry the guarantee as originally stated, not a
weaker one.

Building a third, universal in-block diff engine would mean enumerating which
in-block differences are legitimate — the allowed-normalisations list wearing a
different hat. Explicitly out of scope.

## Check A — fidelity

Load the source, render it again with **no candidate applied at all** (a pure
`load` → `dump`, not `_apply_block` with an empty candidate), encode, and
require equality with the source **bytes**.

Covers rows 1–6 and any construct neither of us thought of, anywhere in the
file including inside the block. Needs no notion of a block boundary.

Refuses when the renderer cannot reproduce the file — row 6 being the known,
deliberate case.

## Check B — containment

With Check A passing, render with the candidate applied and require the source
and the result bytes to be identical outside the owned span.

Covers any escape that shows up as an outside **byte** difference. It does
**not** cover an escape that changes outside *meaning* while every outside
byte stays exactly where it was — an anchor inside the block aliased, or
merge-keyed, from outside it tracks the block's own edit without the alias
site's own text (`*sr`, `<<: *sr`) ever changing. That half of the
guarantee is Check C's job, below. Row 7 (an anchor destroyed *by* the
edit) is, in practice, caught earlier by Check C now: its precondition —
an anchor inside the block aliased outside it — is exactly Check C's
trigger, so Check C refuses on the source document before Check B's own
row-7 mechanism ever runs.

### The owned span

From the `spec_runner:` key line through the last line of its mapping's
**data**. Trailing blank and comment lines after that are *outside* the span,
and therefore protected.

That placement is deliberate: the tail comment is precisely what #113 was
about, and the tail-moving logic added there is what keeps it matching.

The span is located **independently in the source and in the output**, so a
tail that shifts down a line when a key is appended is not a difference — only
content is compared, never line numbers.

Computing the end: take the line of the next top-level key (from ruamel's
`lc` data; end of file if `spec_runner:` is last) and walk backwards over the
trailing run of blank and comment lines. Using the next top-level key rather
than walking the block's own subtree is what keeps multi-line block scalars
inside the overlay from truncating the span.

When `spec_runner:` is absent from the source, the source span is empty at the
insertion point and the output span is the appended block.

## Check C — semantic containment

Check B compares bytes; an outside node can change **meaning** without a
single outside byte moving, when it is the same ruamel object the block's
own edit mutates in place — an anchor inside `spec_runner:` aliased, or
merge-keyed, from outside it. Editing `max_retries` under such an anchor
changes every alias to it too, and nothing outside the span is textually
different: the alias site still reads `*sr`, or `<<: *sr`, verbatim.

Runs on the **source** document, right after Check A and before the
candidate is applied — it needs no candidate to answer "is this possible",
and the owner requires the refusal to happen before launch, not after a
render that merely demonstrates it.

**The rule:** refuse when any node inside the owned block — including the
block node itself — is reachable from outside the block. Simpler than
proving that this particular candidate touches the specific aliased node,
which the owner explicitly ruled unnecessary: the structural possibility is
enough to refuse, whether or not this candidate would exercise it. Anchors
that are defined and used wholly inside the block are not refused — they
never leave the boundary.

Detection is identity-based, not name-based: aliases are resolved at load
time, so the alias site itself carries no anchor name to match against.
Two things about the identity walk are load-bearing, each measured to cost
an iteration if missed:

- The block's own top-level slot is excluded **by key**, not by identity.
  Excluding by identity (`if val is block: continue`) would also skip an
  outside alias that resolves to the *whole block* — exactly the case being
  hunted, since that alias site's value *is* the block object.
- Identity is only meaningful for containers and anchored scalars. Plain
  scalars are interned (`3` is the same object everywhere in the process),
  so an identity test over them would refuse anchors that were never
  aliased at all.

Skipped when there is no block (`spec_runner:` absent, or not a mapping) —
nothing for an outside alias to reach into, and a non-mapping block is
refused on its own terms during "render" regardless.

Also skipped when the block has no literal top-level position of its own —
i.e. it is reachable **only** through a top-level merge key, nested inside
some other key (the merge-key residual below). Check B's span-less
fallback for that shape already requires the *whole* document to stay
byte-identical for any real edit, at least as strong as Check C for that
case; running Check C there too would only additionally refuse the
untouched no-op, which changes no meaning anywhere and has nothing to
protect against (`test_owned_span_handles_a_top_level_merge_key_cleanly`).

## Failure surface

All three checks raise **one** dedicated exception type, carrying which check
failed. Separate types would invite callers to treat the refusals
differently, and they are the same decision: this file cannot be edited
safely. `run()`'s existing catch-all turns it into
`ActionOutcome(ok=False, phase=PHASE_PRE_LAUNCH, error=...)`.

`pre_launch` is accurate rather than convenient: no subprocess was launched, so
no PR can exist. Nothing is written anywhere.

### Diagnostics carry no file content — across the whole render path

`project.yaml` is an untrusted file from a neighbour repo and routinely holds
secrets — `telegram_bot_token` appears in this repo's own test fixtures. The
error travels into an HTTP response body and the audit log, so **no diff, no
line text, and no scalar value may appear in it.**

Guarding only the two checks would leave the boundary open one step earlier,
where `run()`'s catch-all does `error=str(err)`. This is not hypothetical —
measured:

```
DuplicateKeyError: found duplicate key "a" with value "2"
                   (original value: "s3cr3t-telegram-token-ABC123")
```

A duplicate key anywhere in the file prints **both values verbatim**, straight
from `yaml.load`. `UnicodeDecodeError` similarly reports the offending byte.
(`ScannerError`, `ParserError` and `ComposerError` were checked and are clean —
but "clean today" is not a property to depend on.)

Therefore the safe exception wraps **all of `build_new_yaml_text`** — decode,
style detection, load, mutation, dump, encode, and all three checks — and it
is the only exception type the function raises.

What may escape: the stage that failed (`decode`, `parse`, `check-a`,
`check-c`, `render`, `encode`, `check-b`), the *class name* of the
underlying cause, coordinates, lengths, counts, and which side of the span
diverged. Class names are type identifiers and carry no file content.

**No reference to the original exception survives** — not on an attribute, not
via `__cause__`, not via `__context__`. The original object is proven to carry
secrets in its message and its attributes, so retaining it anywhere would make
this boundary's safety depend on the behaviour of some future logger,
serialiser or error reporter. That is weaker than "safe by construction".

This matches the rule this repo already set at `core/contract.py:636-642`:

> `from None` is not enough either: it only suppresses *display*, leaving the
> object on `__context__`. `str()` and the default traceback stay clean, but
> any reporter that serialises exception attributes recovers it.

Only pre-extracted safe values cross the boundary: stage, the cause's class
name as a string, coordinates, sizes and counts.

Measured, because the obvious implementation does not work: raising outside
the handler — `contract.py`'s technique — does **not** clear `__context__`
when it happens inside a `@contextmanager`, since `contextlib` re-raises
within the original exception's propagation. So the guarantee is enforced at
one choke point instead: every `UnsafeEditError` leaving `build_new_yaml_bytes`
passes through a boundary handler that nulls `__context__`, `__cause__` and
sets `__suppress_context__`. One place covers every path out of the function,
including the two checks, which do not go through the stage wrapper at all.

That choke point also catches `Exception` broadly, not only `UnsafeEditError`:
nothing but `_stage`'s own wrapping keeps an unrelated bug (e.g. in the class-
name/coordinate formatter itself, which runs while the secret-bearing original
is still being handled) from leaving this function with the original still
reachable on `__context__`. `build_new_yaml_bytes` is the only exception type
the function raises **by construction**, not because nothing outside a
`_stage` block happens to raise today.

**Accepted residual — this guarantee is about the exception object, not the
traceback.** The scrubbed `UnsafeEditError`'s traceback still carries frames
whose locals include `base_bytes`/`base_text` — the complete neighbour file,
secret included — for as long as the traceback object is alive. Walking that
same traceback also reaches `contextlib`'s own `__exit__` frame (the `_stage`
context manager's machinery): its local named `value` is bound to the
**original exception object itself** — the one this whole boundary exists to
scrub — with the secret rendered verbatim in its message. That is a third
channel, distinct from the file-content one: `base_bytes`/`base_text` are the
raw file; `value` is the unscrubbed exception. Any reporter configured to
capture frame locals (Sentry's `include_local_variables`,
`better-exceptions`) recovers all of it regardless of what this boundary
clears on the exception object. This is inherent to how Python tracebacks
work and is not something a handler here can close; it is named rather than
silently assumed away.

Local diagnosis is reproduced against the file, which the owner already has.
Re-running costs less than keeping a hidden leak channel open.

Example messages:

```
cannot safely edit project.yaml: YAML renderer cannot reproduce the source
byte-for-byte (first mismatch at byte 126; source/output lengths 842/839)

cannot safely edit project.yaml: rendering changes bytes outside spec_runner
(after the block; first mismatch at output line 17; source/output lengths 842/851)

cannot safely edit project.yaml: parse failed (DuplicateKeyError at line 12,
column 1)
```

No full-diff tool is built. `build_new_yaml_text` is a pure function, so a
maintainer reproduces any refusal locally in three lines with the file in hand.

## Style preservation (rows 1–5)

Verified reproducible, individually and in combination (BOM + CRLF + `---`/`...`
+ 3-space mapping indent + no final newline all at once).

| Row | Mechanism |
|---|---|
| 1 | `yaml.explicit_start` / `yaml.explicit_end`, set from a leading `---` / trailing `...` |
| 2 | strip the emitter's final newline when the source lacks one |
| 3 | translate `\n` → `\r\n` when the source is uniformly CRLF (mixed → leave LF, let Check A refuse) |
| 4 | re-prepend the BOM when the source has one |
| 5 | `yaml.indent(mapping=N)`, N measured from the source |

Existing `_sequence_offset` and `_null_style` are kept unchanged. They stop
being trusted and become guesses that Check A verifies.

Row 6 is **not** attempted. An aligned-value file becomes honestly
unsupported: it is refused rather than silently reflowed. This is a capability
removal, not only an added safety net — a file that is edited today, with
churn, will be refused after this change.

## Testing

**Style preservation** — each of rows 1–5 independently, plus the combined
BOM + CRLF + markers + no-final-newline case.

**Deliberate refusals** — row 6 alignment; the anchor-inside/alias-outside case
from row 7. Both assert a refusal, and assert that the message contains no
file content.

**Check C** — a plain alias to the block, a merge key to the block (a
separate case: `node.values()` alone never sees a merge key's source, so a
`_children` bug that forgets it is otherwise invisible), a nested anchor
inside the block used outside, and an anchored scalar inside the block used
outside — each asserts `stage == "check-c"`. Two controls assert the
guard does **not** fire: an anchor and its only alias both wholly inside the
block, and an anchor defined and used wholly outside the block — a guard
that refuses either is too broad and is a defect, not a safety win. A
reverse-mutation test disables the guard (monkeypatches
`_block_is_reachable_from_outside` to `False`) and confirms the plain-alias
case is then silently accepted with the alias's own text untouched,
proving the guard, not some other stage, is what refuses it. A last test
plants a distinctive anchor name and asserts it does not appear in the
message.

**In-block preservation** (the third guarantee, which no check covers) — a
candidate must preserve, inside `spec_runner:`: an unknown key, that key's
quote style, standalone and inline comments, the relative order of unknown
keys, and values absent from the candidate. Some of these exist from #113;
quote style and unknown-key ordering are new.

**The byte pipeline** — a file whose content is non-ASCII *before* the first
difference, asserting the reported coordinate is a **byte** offset and not a
string index (the two diverge exactly there). And: the bytes written to the
temp edit file are byte-identical to the bytes that passed the checks — asserted
at the temp file, since that is what `propose-pr` reads.

**Diagnostics leak nothing** — invalid UTF-8, and malformed YAML including the
duplicate-key case, must produce a refusal that discloses neither the offending
value nor any source line. The duplicate-key case is the regression test for
the measured leak above.

Asserted on the **surfaces this design claims to protect**, not on
`str(exception)`:

- `ActionOutcome.error` as returned by `run()`;
- the HTTP response body of the route that serves the action;
- the audit line actually emitted by `_audit`.

A test of the exception alone proves only that the exception is clean. It
cannot show that a caller has not since appended `repr(original)`, a traceback,
or other unsafe context on the way to one of those three surfaces — and those
are what a neighbour's secret would actually escape through. Each test plants a
recognisable secret in the fixture and asserts its absence from the surface.

**Span placement** — `spec_runner:` as the first, a middle, and the last
top-level key; several top-level keys following it; a block scalar immediately
before the span boundary; and `spec_runner:` absent from a file that ends in a
trailing comment and a `...` marker.

**No regression** — both real `project.yaml` files in the workspace still edit
cleanly, and the byte-exact no-op bar from #113 still holds.

## Known residuals

- The span's backward walk treats a trailing run of blank and `#` lines as the
  tail. An overlay ending in a block scalar whose final lines begin with `#`
  would truncate the span. The failure mode is a refusal, never a bad PR.
- Mixed line endings within one file are not reproduced; Check A refuses.
- A second capability removal, alongside row 6: a `project.yaml` whose
  `spec_runner:` is reachable only through a top-level merge key
  (`<<: *anchor`) is now effectively un-editable. `_apply_block` mutates the
  anchored mapping in place — typically nested under a key like `defaults:` —
  which has no literal position of its own (`_owned_span` never had one to
  give it), so it lies outside every span and Check B refuses the edit. This
  is fail-closed and the right direction, not a bug, but it is a second file
  shape this editor stops handling, and it must be named rather than
  discovered (measured during Task 5; `test_owned_span_handles_a_top_level_
  merge_key_cleanly` covers the no-op case, which still works — only an
  actual mutation through the merge key is refused). Check C (Task 7)
  deliberately does not widen this residual further: it skips its own check
  whenever the block has no literal top-level position, precisely this
  shape, because Check B's span-less fallback there is already at least as
  strong for a real edit, and Check C would otherwise additionally refuse
  the untouched no-op for no safety gain (nothing outside changes meaning
  when nothing changes at all).
- A third capability removal, alongside row 6 and the merge-key case above:
  a `project.yaml` whose `spec_runner:` block is anchored and aliased (or
  merge-keyed) elsewhere in the file becomes un-editable outright — Check C
  refuses on the source document before the candidate is even applied,
  whether or not this particular candidate would touch the aliased data.
  The owner ruled this the correct trade: a structural possibility of
  escape is enough to refuse, rather than proving the specific node is
  touched. This also fully subsumes row 7's own mechanism (an anchor
  destroyed by the edit): row 7 requires exactly the source shape Check C's
  precondition looks for, so `test_an_alias_expanded_outside_the_block_is_
  refused` now surfaces as `stage="check-c"` rather than `stage="check-b"`
  — Check B's row-7 detection is not broken, just never reached, because
  Check C always fires first on any input that could trigger it.
- A candidate-mutation bug that damages unknown in-block data is caught only by
  the targeted tests above, never by the gate.
- A `project.yaml` with duplicate keys is already unusable — ruamel's round-trip
  loader raises. This design does not change that, only stops the refusal from
  printing both values.
- Diagnostic coordinates and lengths do disclose *where* and *how large* a file
  is. Accepted: they name the comparison, not its content.
