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
| A change does not escape the `spec_runner:` block | **Check B** |
| Unknown in-block data survives the candidate mutation | **in-place mutation (#113) + targeted tests** — *not* Check A |

The third row matters. Check A runs *before* the candidate is applied, so a
candidate-mutation bug that drops an unknown key or a comment inside the block
passes Check A untouched, and Check B excludes the block from comparison by
construction. Nothing in this slice detects that class of bug; the tests in
"In-block preservation" below are its only defence.

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

Covers row 7 and any future edit-induced escape.

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

## Failure surface

Both checks raise **one** dedicated exception type, carrying which check
failed. Two types would invite callers to treat the two refusals differently,
and they are the same decision: this file cannot be edited safely. `run()`'s
existing catch-all turns it into
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
style detection, load, mutation, dump, encode, and both checks — and it is the
only exception type the function raises.

What may escape: the stage that failed (`decode`, `parse`, `render`, `encode`,
`check-a`, `check-b`), the *class name* of the underlying cause, coordinates,
lengths, counts, and which side of the span diverged. Class names are type
identifiers and carry no file content.

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
- A candidate-mutation bug that damages unknown in-block data is caught only by
  the targeted tests above, never by the gate.
- A `project.yaml` with duplicate keys is already unusable — ruamel's round-trip
  loader raises. This design does not change that, only stops the refusal from
  printing both values.
- Diagnostic coordinates and lengths do disclose *where* and *how large* a file
  is. Accepted: they name the comparison, not its content.
