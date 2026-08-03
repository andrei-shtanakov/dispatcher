"""Content-PR action: update-spec-runner-config (DESIGN-304, resolves X-02).

Deliberately NOT `core/actions.py`'s `ActionRunner` — this runner produces
file *content* (a diff limited to one project.yaml's `spec_runner:` block)
before delegating branch/commit/push/PR to github-checker, a different
mutation shape than the pure git-plumbing sync actions (pull/create-pr).
Own lock, own audit logger, so the two action classes stay independently
testable and reasoned about (explicit stakeholder requirement, spec §1).

The write path never touches the live tree (DESIGN-401): the rendered
`project.yaml` is written to a throwaway temp file and handed to
`github-checker propose-pr --edit project.yaml=<tmp> --if-match
project.yaml=<sha256 of the bytes actually read>`, which does its own
branch/commit/push/PR from a scoped worktree. This runner's mutation
surface on observed repos is therefore zero — every write, including a
no-op, comes back as a parsed `ActionOutcome`, never a live-tree diff.
`propose-pr` reports a no-op (nothing changed vs. the base branch) as
`ok=False, detail=="no-op"` while still exiting non-zero and printing
JSON on stdout; callers must check `detail`, not just the process's
return code, to tell a no-op apart from a real failure.
"""

from __future__ import annotations

import hashlib
import logging
import re
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

from pydantic import BaseModel
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.representer import RoundTripRepresenter
from ruamel.yaml.tokens import CommentToken

from dispatcher.core.actions import (
    PHASE_LAUNCHED_UNREADABLE,
    PHASE_PRE_LAUNCH,
    PHASE_READABLE,
    ActionOutcome,
    audit_unusable,
    consumer_failure,
    one_line,
    project_outcome,
    unusable_answer,
    verb_mismatch,
)
from dispatcher.core.contract import ContractViolation, ingest
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.spec_runner_config import TYPED_DEFAULTS, TYPED_FIELDS
from dispatcher.core.spec_runner_config_schema import (
    ConfigValidationError,
    validate_candidate,
)

_ACTION_TIMEOUT = 120
_SAFE_DIR_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")
_audit = logging.getLogger("dispatcher.actions.spec_runner_config")

# Wide enough that ruamel never re-wraps a long scalar it did not have to
# touch. Re-wrapping is the same class of harm as the comment loss below:
# a diff in somebody else's file that nobody asked for.
_NO_REWRAP = 4096

_SEQ_DASH_RE = re.compile(r"^(\s*)-(?:\s|$)")
_BLOCK_KEY_RE = re.compile(r"^(\s*)(?:[^\s#-][^:]*|\"[^\"]*\"|'[^']*'):\s*(?:#.*)?$")


def _sequence_offset(text: str) -> int | None:
    """The file's own block-sequence indentation, or None if unreadable.

    ruamel re-emits sequences with ITS defaults, not the file's, so a file
    that indents `- item` under its key comes back re-indented from end to
    end — churn across regions this editor never touched (the real
    research-bench/project.yaml re-indents its whole `domain:` tree on a
    no-op edit). Measured, never assumed: a file with no block sequence, or
    one that indents them inconsistently, gets ruamel's default rather than
    a guess that would be wrong in a new way.
    """
    offsets: set[int] = set()
    key_indent: int | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        dash = _SEQ_DASH_RE.match(line)
        if dash is not None:
            if key_indent is not None:
                offsets.add(len(dash.group(1)) - key_indent)
            continue
        key = _BLOCK_KEY_RE.match(line)
        key_indent = len(key.group(1)) if key is not None else None
    if len(offsets) != 1:
        return None
    offset = offsets.pop()
    return offset if offset >= 0 else None


_NULL_TOKEN_RE = re.compile(r":[ \t]+(null|Null|NULL|~)[ \t]*(?:#.*)?$", re.MULTILINE)
_NULL_REPRESENTERS: dict[str, type[RoundTripRepresenter]] = {}


def _null_style(text: str) -> str | None:
    """How this file spells null, or None if it never spells it explicitly.

    ruamel loads `k: null` as Python `None` and emits it back as `k:` —
    same value, but in a diff it reads as if the editor half-deleted the
    field. research-bench's `spec_gen_budget_usd: null` is exactly that
    case, and its own comment says the field must be *explicitly* nulled.
    A file that mixes spellings gets ruamel's default: reproducing one of
    them everywhere would be churn of its own.
    """
    tokens = set(_NULL_TOKEN_RE.findall(text))
    return tokens.pop() if len(tokens) == 1 else None


def _representer_spelling_null(style: str) -> type[RoundTripRepresenter]:
    """A representer subclass that writes None as `style`.

    Subclass, not `add_representer` on the shared class: that method is a
    classmethod, so calling it through an instance would rewrite null
    rendering for every other ruamel user in the process.
    """
    cached = _NULL_REPRESENTERS.get(style)
    if cached is not None:
        return cached

    def represent_none(self: RoundTripRepresenter, data: None) -> Any:
        return self.represent_scalar("tag:yaml.org,2002:null", style)

    class _NullStyled(RoundTripRepresenter):
        pass

    cls: type[RoundTripRepresenter] = _NullStyled
    # `add_representer` copies the inherited table into the subclass before
    # writing, so the shared RoundTripRepresenter is left alone.
    cls.add_representer(type(None), represent_none)
    _NULL_REPRESENTERS[style] = cls
    return cls


_NULL_ONLY_KEY_RE = re.compile(r"^(\s*)(?:[^\s#-][^:]*|\"[^\"]*\"|'[^']*'):\s*$")


def _mapping_indent(text: str) -> int | None:
    """The file's own nesting step, or None if it is not consistent.

    A guess, and allowed to be one: Check A verifies the result, so a wrong
    measurement produces a refusal rather than a bad PR.
    """
    steps: set[int] = set()
    parent: int | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith("-"):
            parent = None
            continue
        if parent is not None and indent > parent:
            steps.add(indent - parent)
        parent = indent if _NULL_ONLY_KEY_RE.match(line) else None
    if len(steps) != 1:
        return None
    step = steps.pop()
    return step if 1 <= step <= 8 else None


@dataclass(frozen=True)
class _SourceStyle:
    """Everything about the source's layout that ruamel would otherwise
    replace with its own defaults."""

    bom: bool
    crlf: bool
    final_newline: bool
    explicit_start: bool
    explicit_end: bool
    mapping_indent: int
    sequence_offset: int | None
    null_style: str | None


def _inspect_style(text: str) -> _SourceStyle:
    body = text.lstrip("﻿")
    stripped_lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    newlines = body.count("\n")
    return _SourceStyle(
        bom=text.startswith("﻿"),
        crlf=newlines > 0 and body.count("\r\n") == newlines,
        final_newline=text.endswith("\n"),
        explicit_start=bool(stripped_lines) and stripped_lines[0] == "---",
        explicit_end=bool(stripped_lines) and stripped_lines[-1] == "...",
        mapping_indent=_mapping_indent(body) or 2,
        sequence_offset=_sequence_offset(body),
        null_style=_null_style(body),
    )


def _configured_yaml(style: _SourceStyle) -> YAML:
    """A fresh YAML per use.

    Precaution, not a fix for an observed bug: dumping one document twice was
    measured stable, anchors included, both across instances and on a reused
    one. A fresh instance keeps the fidelity render from sharing any emitter
    state with the real one, so that stability does not become load-bearing.
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = _NO_REWRAP
    yaml.explicit_start = style.explicit_start
    yaml.explicit_end = style.explicit_end
    offset = style.sequence_offset if style.sequence_offset is not None else 0
    yaml.indent(mapping=style.mapping_indent, sequence=offset + 2, offset=offset)
    if style.null_style is not None:
        yaml.Representer = _representer_spelling_null(style.null_style)
    return yaml


def _render(doc: CommentedMap, style: _SourceStyle) -> bytes:
    """Emit `doc` as the source's own bytes: ruamel first, then the three
    things its emitter has no setting for."""
    buf = StringIO()
    _configured_yaml(style).dump(doc, buf)
    text = buf.getvalue()
    if not style.final_newline:
        text = text.rstrip("\n")
    if style.crlf:
        text = text.replace("\n", "\r\n")
    if style.bom:
        text = "﻿" + text
    return text.encode("utf-8")


def _first_differing_byte(left: bytes, right: bytes) -> int:
    """Index of the first differing byte; the shorter length if one is a
    prefix of the other."""
    limit = min(len(left), len(right))
    for index in range(limit):
        if left[index] != right[index]:
            return index
    return limit


def _owned_span(doc: CommentedMap, lines: list[str]) -> tuple[int, int] | None:
    """Inclusive 0-based line range of the `spec_runner:` block's DATA.

    Trailing blank and comment lines are deliberately left OUT, which puts
    the text following the block into the compared region — that text is
    exactly what #113 was about. The end is derived from the next top-level
    key rather than by walking the block's own subtree, because ruamel's line
    info for a block scalar points at the `|`, not at the scalar's last line,
    and walking would truncate the span.

    The `...` explicit-end marker is stripped the same way: it is an emitter
    option (`style.explicit_end`), not a key's data, so when `spec_runner` is
    the mapping's last key and has no following key to bound it, the
    fallback to `len(lines)` would otherwise pull that marker line into the
    span on one side of a comparison but not the other (no block at all on
    the source side means the whole file, marker included, is "outside").
    The marker check is column-0 exact (`rstrip` of the line ending only,
    not `.strip()`): an indented `...` inside a block scalar is data, not a
    document marker, and treating it as one would truncate the span past
    real content.

    `doc.lc.data.get(...)` rather than `"spec_runner" in doc` plus indexing:
    a top-level merge key (`<<: *anchor`) makes membership true through
    merge resolution while `lc.data` only ever holds literally-present
    keys, so the two can disagree and the index would raise `KeyError`.
    """
    pos = doc.lc.data.get("spec_runner")
    if pos is None:
        return None
    start = pos[0]
    following = [p[0] for p in doc.lc.data.values() if p[0] > start]
    end = (min(following) if following else len(lines)) - 1
    while end > start and (
        not lines[end].strip()
        or lines[end].lstrip().startswith("#")
        or lines[end].rstrip("\r\n") == "..."
    ):
        end -= 1
    return start, end


def _outside(lines: list[str], span: tuple[int, int] | None) -> tuple[str, str]:
    """(before, after) the owned span. No span -> the whole text is outside."""
    if span is None:
        return "".join(lines), ""
    start, end = span
    return "".join(lines[:start]), "".join(lines[end + 1 :])


def _first_differing_line(left: str, right: str) -> int:
    """0-based index of the first line that differs, within the region."""
    left_lines = left.splitlines()
    right_lines = right.splitlines()
    for index in range(min(len(left_lines), len(right_lines))):
        if left_lines[index] != right_lines[index]:
            return index
    return min(len(left_lines), len(right_lines))


def _first_differing_output_line(
    source_lines: list[str], new_lines: list[str], new_span: tuple[int, int]
) -> int:
    """0-based OUTPUT line index of the first divergence, for the case where
    the source has no span of its own (the block did not exist there).

    There is no shared boundary to index a concatenated comparison against,
    so the coordinate has to be found by walking `new_lines` directly,
    skipping `new_span`, rather than by indexing into a string built from
    pieces that skip the span — that index is not a real output line
    wherever the divergence falls after the span.

    The caller only reaches here when a difference is already known to
    exist, but the walk below can still run out of `new_lines` before
    finding one directly: that happens when the OUTPUT is shorter outside
    the span than the source is (`source_lines` has trailing content the
    output does not). There is no real output line at that position — the
    last real line is reported instead of `len(new_lines)`, which would
    name a line that does not exist.
    """
    start, end = new_span
    src_index = 0
    new_index = 0
    while new_index < len(new_lines):
        if start <= new_index <= end:
            new_index += 1
            continue
        if (
            src_index >= len(source_lines)
            or source_lines[src_index] != new_lines[new_index]
        ):
            return new_index
        src_index += 1
        new_index += 1
    return max(new_index - 1, 0)


def _last_line_slot(node: Any) -> tuple[Any, Any] | None:
    """(container, key) whose comment slot owns the block's LAST rendered line.

    ruamel hangs the text that follows a block — the blank line and the
    col-0 comment after it — off whatever key emitted that last line, which
    for a nested last entry is a leaf several levels down, not the block's
    own key. Anything that removes or displaces that leaf takes the
    following text with it unless the text is moved first.
    """
    slot: tuple[Any, Any] | None = None
    while True:
        if isinstance(node, CommentedMap) and node:
            key: Any = next(reversed(node))
        elif isinstance(node, CommentedSeq) and node:
            key = len(node) - 1
        else:
            return slot
        slot = (node, key)
        node = node[key]


def _commented(value: Any) -> Any:
    """Rebuild plain containers as ruamel ones, leaving loaded ones alone.

    A plain dict assigned into the tree cannot carry comments, so
    `_last_line_slot` cannot see into it and the block's tail would be
    re-attached above the overlay instead of after it. Values that came
    from the file are returned untouched — they already carry their own.
    """
    if isinstance(value, (CommentedMap, CommentedSeq)):
        return value
    if isinstance(value, dict):
        return CommentedMap((key, _commented(item)) for key, item in value.items())
    if isinstance(value, list):
        return CommentedSeq(_commented(item) for item in value)
    return value


def _take_tail(block: CommentedMap) -> CommentToken | None:
    """Detach the lines following the block's last line; leave inline alone.

    A post-key comment token is emitted verbatim starting on the key's own
    line, so its first line is that key's inline comment and belongs to the
    key; only what comes after it is the block's tail.
    """
    found = _last_line_slot(block)
    if found is None:
        return None
    owner, key = found
    slot = owner.ca.items.get(key)
    if slot is None or slot[2] is None:
        return None
    token = slot[2]
    inline, _, tail = token.value.partition("\n")
    if not tail:
        return None
    if inline:
        token.value = inline + "\n"
    else:
        slot[2] = None
    return CommentToken("\n" + tail, token.start_mark, token.end_mark)


def _put_tail(doc: CommentedMap, block: CommentedMap, token: CommentToken) -> None:
    """Re-attach the tail after the block's new last line."""
    found = _last_line_slot(block)
    # An emptied block has no line to hang it off; the parent's own slot for
    # `spec_runner` is emitted right after it, which is the same place.
    owner, key = found if found is not None else (doc, "spec_runner")
    slot = owner.ca.items.setdefault(key, [None, None, None, None])
    if slot[2] is None:
        slot[2] = token
    else:
        # Both end and begin with a newline; keeping both would invent a
        # blank line that was not in the file.
        slot[2].value = slot[2].value.removesuffix("\n") + token.value


def _apply_block(doc: CommentedMap, block: CommentedMap, emit: dict[str, Any]) -> None:
    """Write `emit` into the loaded block instead of rebuilding it.

    Assigning a freshly built dict over `doc["spec_runner"]` threw away the
    loaded CommentedMap, and with it every comment inside the block, the
    file's key order, and every key this editor has no field for. Only keys
    this code actually decided about are touched; the rest of the block is
    somebody else's and is left exactly as parsed.
    """
    dropped = "extra_executor_config" not in emit and "extra_executor_config" in block
    appended = any(key not in block for key in emit)
    # Swapping a container for a different one destroys the nested leaf the
    # tail hangs off just as surely as deleting it does.
    swapped = any(
        isinstance(block.get(key), (CommentedMap, CommentedSeq))
        and block[key] is not value
        for key, value in emit.items()
    )
    # Only when the block's last line is about to move: an edit that merely
    # updates keys in place must be able to reproduce the file byte for byte.
    tail = _take_tail(block) if dropped or appended or swapped else None
    if dropped:
        del block["extra_executor_config"]
    for key, value in emit.items():
        block[key] = value
    if tail is not None:
        _put_tail(doc, block, tail)


class ConfigCandidate(BaseModel):
    """A proposed spec_runner: block, as submitted by the editor UI."""

    typed: dict[str, Any]
    # Tri-state: None -> preserve the current file's overlay untouched;
    # {} -> intentional clear; non-empty dict -> replace (X-02 Copilot
    # round 1 — a bare {} default was indistinguishable from "clear").
    extra_executor_config: dict[str, Any] | None = None
    base_mtime: float  # project.yaml's mtime when the form was rendered


class SpecRunnerConfigRejectedError(Exception):
    """Bad target or invalid candidate (API turns this into 422)."""


class SpecRunnerConfigBusyError(Exception):
    """This repo's project.yaml already has an update in flight (-> 409)."""


class SpecRunnerConfigConflictError(Exception):
    """project.yaml changed on disk since the form was rendered (-> 409)."""


class UnsafeEditError(Exception):
    """project.yaml cannot be edited safely; the message carries no content.

    One type for every refusal on the render path, not one per stage: two
    types would invite callers to treat them differently, and they are the
    same decision — this file cannot be edited safely.

    `project.yaml` belongs to a neighbour repo and routinely holds secrets
    (`telegram_bot_token` is in this repo's own fixtures), and this message
    reaches an HTTP response body and the audit log. So it carries the stage,
    the cause's CLASS name, coordinates and sizes — never a source line, a
    scalar value, or a diff.

    Carries NO reference to the exception it replaces — not an attribute,
    not `__cause__`, not `__context__`. The original is proven to carry
    secrets in its own message and attributes (`_safe_cause` exists because
    of it), so keeping it anywhere would make this boundary's safety depend
    on the behaviour of some future logger, serialiser or error reporter —
    weaker than "safe by construction". Same rule `core/contract.py:636-642`
    already applies to `ContractViolation`: `from None` only suppresses
    *display*, it does not clear `__context__`. `build_new_yaml_bytes` is
    the choke point that enforces this (see below); `_stage`'s `from None`
    is defense in depth, not the guarantee itself.
    """

    def __init__(self, message: str, *, stage: str) -> None:
        super().__init__(message)
        self.stage = stage


def _safe_cause(err: BaseException) -> str:
    """Name the failure without quoting the file.

    Deliberately NOT `str(err)`: ruamel's DuplicateKeyError prints both the
    new and the original value verbatim. Deliberately not `str(mark)` or
    `err.problem` either — both embed source text. Only the class name and
    the mark's integer coordinates are safe.
    """
    name = type(err).__name__
    mark = getattr(err, "problem_mark", None)
    if mark is not None:
        return f"{name} at line {mark.line + 1}, column {mark.column + 1}"
    start = getattr(err, "start", None)
    if isinstance(start, int):
        return f"{name} at byte {start}"
    return name


@contextmanager
def _stage(name: str) -> Iterator[None]:
    """Convert anything raised inside into a content-free UnsafeEditError."""
    try:
        yield
    except UnsafeEditError:
        raise
    except Exception as err:
        raise UnsafeEditError(
            f"cannot safely edit project.yaml: {name} failed ({_safe_cause(err)})",
            stage=name,
        ) from None


def build_new_yaml_bytes(
    base_bytes: bytes, candidate: ConfigCandidate
) -> tuple[bytes, list[str], bool]:
    """Render project.yaml bytes; see `_build_new_yaml_bytes` for the how.

    The choke point for the no-reference-survives guarantee on
    `UnsafeEditError` (see its docstring). `from None` inside `_stage`
    only suppresses *display*; it leaves the original exception reachable
    on `__context__` — and `contextlib` re-raising inside a
    `@contextmanager` re-establishes that link on the way out regardless,
    so clearing it inside `_stage` itself does not stick. This function is
    the one place outside any exception handler where the raised
    `UnsafeEditError` can be scrubbed for good: no reference to the
    original survives past this line, not on an attribute, not via
    `__cause__`, not via `__context__`.

    The second `except` below is not decorative: every statement in
    `_build_new_yaml_bytes` outside a `_stage` block is (today) just the
    final `return`, but that is an invariant of the current source, not a
    property of this function's construction. `_safe_cause` itself is not
    total — `mark.line + 1` assumes a `problem_mark` shaped like ruamel's —
    and Tasks that extend this function may add a helper, or a statement,
    outside a `_stage` block. Anything that escapes `_stage`'s own coverage
    must still come out as a scrubbed `UnsafeEditError`, or the guarantee
    this docstring makes is false the day either of those happens.

    This guarantee covers the exception OBJECT only — its message, `args`,
    attributes, `__cause__` and `__context__`. It does NOT cover the
    traceback's frame locals: `base_bytes`/`base_text` (the whole neighbour
    file, secret included) remain reachable from `err.__traceback__`'s
    frames for as long as the traceback object is alive, and any reporter
    configured to capture frame locals (e.g. Sentry's
    `include_local_variables`, `better-exceptions`) recovers them. Walking
    that same traceback also reaches `contextlib`'s own `__exit__` frame (the
    `_stage` context manager's machinery, not this repo's code) — its local
    named `value` is bound to the ORIGINAL exception object itself, the one
    this function exists to scrub, with the secret rendered verbatim in its
    message. That is a third channel, not a restatement of the first two:
    `base_bytes`/`base_text` are the raw file, `value` is the unscrubbed
    exception the guarantee above is specifically about. This is inherent to
    how Python exceptions carry their traceback and is not something a
    handler at this boundary can close — noted here as an accepted residual,
    not silently assumed away.
    """
    unexpected_cause_name: str | None = None
    try:
        return _build_new_yaml_bytes(base_bytes, candidate)
    except UnsafeEditError as err:
        # The original is proven to carry secrets in its own message, so no
        # reference to it may leave this function — not on an attribute, not
        # via __cause__, not via __context__. Retaining it would make this
        # boundary's safety depend on some future logger or error reporter.
        # Same rule as core/contract.py:636-642. A bare `raise` re-raises
        # the SAME object already being handled, so — unlike the arm below —
        # nothing re-populates `__context__` on the way out.
        err.__cause__ = None
        err.__context__ = None
        err.__suppress_context__ = True
        raise
    except Exception as err:  # nothing else may leave this function
        # Anything reaching here escaped every `_stage` block — e.g. a bug
        # in `_safe_cause` itself, raised while it was handling the very
        # exception it exists to make safe. Only the class name is pulled
        # out here; `err` itself must not survive this function on any
        # attribute, `__cause__`, or `__context__` of the replacement.
        unexpected_cause_name = type(err).__name__
    # Raised OUTSIDE the handler, unlike the arm above: building a NEW
    # exception object while `except Exception as err` is still open sets
    # its `__context__` to `err` at the moment of the `raise` statement
    # itself — verified with a canary, because trusting the obvious
    # `raise UnsafeEditError(...) from None` INSIDE that block is exactly
    # what looked right and wasn't: `from None` only sets `__suppress_
    # context__`, and manually clearing `__context__` before that `raise`
    # does not stick, since raising re-populates it right there. Ending the
    # `except` block first means nothing is "currently being handled" by the
    # time this line runs, so `__context__` is never set to begin with. Same
    # technique as core/contract.py's own `ContractViolation` raises.
    #
    # The operative rule, more general than "this function happens to be a
    # plain try/except": raising a NEW exception outside the handler is safe
    # iff nothing is being handled anywhere up the call stack at that point.
    # A plain function like this one satisfies that by construction — until
    # some future caller invokes it from inside ITS OWN `except` block, at
    # which point that caller's exception is what gets chained onto this
    # raise's `__context__`, not anything from in here. Today's sole call
    # site (`SpecRunnerConfigActionRunner.run`) calls this from inside a
    # `try:`, not an `except:`, so the guarantee holds — but that is a
    # property of the CALLER, not of this function, and would need
    # rechecking if a future caller wrapped this call in an `except` block.
    raise UnsafeEditError(
        f"cannot safely edit project.yaml: unexpected failure "
        f"({unexpected_cause_name})",
        stage="unknown",
    )


def _build_new_yaml_bytes(
    base_bytes: bytes, candidate: ConfigCandidate
) -> tuple[bytes, list[str], bool]:
    """Render project.yaml bytes with only its `spec_runner:` key replaced.

    Takes the CAPTURED base bytes (never re-reads the file — the caller
    hashed exactly these bytes for --if-match; a second read would reopen
    the TOCTOU window). Emits a typed key iff it is explicit in the current
    block OR its candidate value differs from the default (DESIGN-402) —
    implicit defaults are never materialized, so a stale TYPED_DEFAULTS
    mirror cannot leak into observed repos. `extra_executor_config` is
    tri-state: `None` preserves the current file's overlay untouched,
    `{}` is an intentional clear, and a non-empty dict replaces it.
    Returns (rendered bytes, changed typed keys, extra-changed flag) for
    the commit message.

    ruamel round-trip mode preserves comments/order elsewhere in the file.
    `YAML()` defaults to `typ="rt"` — as safe as yaml.safe_load(); never
    pass `typ="unsafe"`.

    The block is edited IN PLACE, and the emitter is matched to the file's
    own layout, because everything this function renders ships as a PR in
    somebody else's repo: a key it has no field for, a comment, a blank
    line and an indent level are all things it was not asked to change.
    A candidate that changes nothing renders the input back byte for byte.

    Takes and returns BYTES. A check that compares `str` and a write that
    re-encodes independently do not compose into "we verified these bytes and
    sent those bytes" — `Path.write_text` translates newlines on write.
    """
    with _stage("decode"):
        base_text = base_bytes.decode("utf-8")
    with _stage("parse"):
        style = _inspect_style(base_text)
        doc = _configured_yaml(style).load(StringIO(base_text))
    # Check A — fidelity. A pure load -> dump, with NO candidate applied:
    # this asks only whether the renderer can reproduce this file at all.
    # It covers every construct, including ones nobody enumerated, anywhere
    # in the file, and needs no notion of a block boundary. It is also what
    # makes the style heuristics above safe to guess at.
    with _stage("check-a"):
        fidelity = _render(doc, style)
    if fidelity != base_bytes:
        raise UnsafeEditError(
            "cannot safely edit project.yaml: YAML renderer cannot reproduce "
            "the source byte-for-byte (first mismatch at byte "
            f"{_first_differing_byte(base_bytes, fidelity)}; "
            f"source/output lengths {len(base_bytes)}/{len(fidelity)})",
            stage="check-a",
        )
    # Captured BEFORE `_apply_block` mutates `doc` below — once the block is
    # edited, the source's own line numbers are gone.
    source_lines = base_text.splitlines(keepends=True)
    source_span = _owned_span(doc, source_lines)
    with _stage("render"):
        existing = doc.get("spec_runner")
        # A `spec_runner:` that is not a mapping is a file this editor cannot
        # read, so it must not write one: raising here is what makes run()'s
        # catch-all report a pre-launch failure instead of overwriting a
        # value nobody understood. Checked explicitly rather than left to
        # `dict(existing or {})`, which only raised for the TRUTHY
        # non-mappings — `0`, `false` and `""` are falsey (and `dict("")` is
        # `{}` anyway), so they reached the writer and were silently
        # replaced.
        # `spec_runner:` with no value at all parses as None and is not this
        # case: it means "no block", and gets a fresh one.
        if existing is not None and not isinstance(existing, dict):
            raise TypeError(f"spec_runner: is not a mapping: {type(existing).__name__}")
        current: dict[str, Any] = dict(existing or {})
        emit: dict[str, Any] = {}
        changed_keys: list[str] = []
        for key in TYPED_FIELDS:
            default = TYPED_DEFAULTS[key]
            cand_val = candidate.typed.get(key, current.get(key, default))
            if key in current or cand_val != default:
                emit[key] = cand_val
            if cand_val != current.get(key, default):
                changed_keys.append(key)
        current_extra: dict[str, Any] = current.get("extra_executor_config") or {}
        if candidate.extra_executor_config is None:
            effective_extra = current_extra
        else:
            effective_extra = candidate.extra_executor_config
        extra_changed = effective_extra != current_extra
        if effective_extra:
            emit["extra_executor_config"] = _commented(effective_extra)
        if isinstance(existing, CommentedMap):
            block = existing
        else:
            block = CommentedMap()
            doc["spec_runner"] = block
        _apply_block(doc, block, emit)
    with _stage("encode"):
        new_bytes = _render(doc, style)
    # Check B — containment. Check A already proved the renderer reproduces
    # this file, so any difference now is attributable to the edit; this asks
    # only whether the edit stayed inside the block we own. Spans are located
    # independently in source and output, so a tail that shifts down a line
    # when a key is appended is not a difference — only content is compared.
    with _stage("check-b"):
        new_text = new_bytes.decode("utf-8")
        new_lines = new_text.splitlines(keepends=True)
        new_doc = _configured_yaml(style).load(StringIO(new_text))
        new_span = _owned_span(new_doc, new_lines)
    if source_span is not None and new_span is None:
        # Fail-closed direction: the source had a real, positioned block and
        # the output does not — a wholesale deletion the piecewise
        # comparison below would have caught as "after the block" changed
        # to nothing. Falling back to the weaker concatenated comparison
        # here would accept it instead. Unreachable today (`_apply_block`
        # never deletes the key), but the wrong default direction for a
        # fail-closed check to have on standby. Guarded on `source_span is
        # not None`: a `spec_runner:` reachable only through a top-level
        # merge key (`<<: *anchor`) has no literal position on EITHER side
        # (`_owned_span` returns None for both, never having had one to
        # lose), which is the concatenated-comparison case below, not a
        # deletion.
        raise UnsafeEditError(
            "cannot safely edit project.yaml: rendering changes bytes outside "
            "spec_runner (the rendered output has no spec_runner: block; "
            f"source/output lengths {len(base_bytes)}/{len(new_bytes)})",
            stage="check-b",
        )
    source_before, source_after = _outside(source_lines, source_span)
    new_before, new_after = _outside(new_lines, new_span)
    if source_span is None:
        # No literal top-level position for `spec_runner:` in the source —
        # either the block is being created fresh, or it is only reachable
        # through a merge key, which never gets one either. Either way
        # there is no shared boundary to compare "before" and "after"
        # separately against — only whether the source, taken whole, still
        # matches the new document once the new span (if any) is skipped.
        if source_before + source_after != new_before + new_after:
            differing = (
                _first_differing_output_line(source_lines, new_lines, new_span)
                if new_span is not None
                else _first_differing_line(source_before, new_before)
            )
            raise UnsafeEditError(
                "cannot safely edit project.yaml: rendering changes bytes "
                "outside spec_runner (no pre-existing span to compare "
                "before/after separately; first mismatch at output line "
                f"{differing + 1}; source/output lengths "
                f"{len(base_bytes)}/{len(new_bytes)})",
                stage="check-b",
            )
        return new_bytes, changed_keys, extra_changed
    # By construction: `source_span is None` returned above, and
    # `source_span is not None and new_span is None` raised above it — a
    # `None` here would mean neither branch matched, which cannot happen.
    assert new_span is not None
    for side, was, now, offset in (
        ("before the block", source_before, new_before, 0),
        ("after the block", source_after, new_after, new_span[1] + 1),
    ):
        if was == now:
            continue
        differing = _first_differing_line(was, now)
        raise UnsafeEditError(
            "cannot safely edit project.yaml: rendering changes bytes outside "
            f"spec_runner ({side}; first mismatch at output line "
            f"{offset + differing + 1}; source/output lengths "
            f"{len(base_bytes)}/{len(new_bytes)})",
            stage="check-b",
        )
    return new_bytes, changed_keys, extra_changed


def _commit_message(changed_keys: list[str], extra_changed: bool) -> str:
    """`--message` for propose-pr; stable, greppable, no empty parentheses."""
    parts = list(changed_keys)
    if extra_changed:
        parts.append("extra_executor_config")
    base = "chore(spec-runner): update config"
    return f"{base} ({', '.join(parts)})" if parts else base


class SpecRunnerConfigActionRunner:
    """Serialized executor of the update-spec-runner-config action."""

    def __init__(
        self,
        config: DispatcherConfig,
        *,
        command: tuple[str, ...] = ("github-checker",),
    ) -> None:
        self._config = config
        self._command = command
        self._lock = threading.Lock()
        self._busy: set[str] = set()

    def _target(self, repo_dir: str) -> Path:
        if not _SAFE_DIR_RE.fullmatch(repo_dir) or repo_dir in (".", ".."):
            raise SpecRunnerConfigRejectedError(f"unsafe repo dir: {repo_dir!r}")
        existing = [r for r in self._config.roots if r.is_dir()]
        if not existing:
            raise SpecRunnerConfigRejectedError("no existing workspace root configured")
        # Iterate ALL roots in discovery order — a config found by
        # discover_project_configs in a later root must resolve to that
        # root, never to a same-named dir in an earlier one.
        for root in existing:
            project_yaml = root / repo_dir / "project.yaml"
            if project_yaml.is_file():
                return project_yaml
        raise SpecRunnerConfigRejectedError(f"no project.yaml in: {repo_dir}")

    def run(self, repo_dir: str, candidate: ConfigCandidate) -> ActionOutcome:
        """Validate, diff, write, and hand off to github-checker. Always audits."""
        try:
            unknown = set(candidate.typed) - set(TYPED_FIELDS)
            if unknown:
                raise SpecRunnerConfigRejectedError(
                    f"unknown typed field(s): {unknown}"
                )
            try:
                validate_candidate(
                    candidate.typed, candidate.extra_executor_config or {}
                )
            except ConfigValidationError as verr:
                raise SpecRunnerConfigRejectedError(str(verr)) from verr
            project_yaml = self._target(repo_dir)
            # Claim the busy slot before checking mtime: a concurrent run()
            # may already be writing project.yaml (which changes its mtime)
            # while blocked on _invoke below. Checking busy first means a
            # second caller sees SpecRunnerConfigBusyError, not a spurious
            # SpecRunnerConfigConflictError caused by that in-flight write.
            with self._lock:
                if repo_dir in self._busy:
                    raise SpecRunnerConfigBusyError(
                        f"{repo_dir}: update already in flight"
                    )
                self._busy.add(repo_dir)
            try:
                stale = project_yaml.stat().st_mtime != candidate.base_mtime
            except OSError as err:
                # project.yaml vanished between _target()'s is_file() and
                # here — release the just-claimed busy slot, don't leak it.
                with self._lock:
                    self._busy.discard(repo_dir)
                raise SpecRunnerConfigRejectedError(
                    f"{repo_dir}: project.yaml unreadable: {err}"
                ) from err
            if stale:
                with self._lock:
                    self._busy.discard(repo_dir)
                raise SpecRunnerConfigConflictError(
                    f"{repo_dir}: project.yaml changed since the form was loaded"
                )
        except (
            SpecRunnerConfigRejectedError,
            SpecRunnerConfigConflictError,
            SpecRunnerConfigBusyError,
        ) as err:
            _audit.info(
                "action=update-spec-runner-config repo=%s ok=False rejected=%s",
                repo_dir,
                err,
            )
            raise
        try:
            base_bytes = project_yaml.read_bytes()
            if_match_hex = hashlib.sha256(base_bytes).hexdigest()
            new_bytes, changed_keys, extra_changed = build_new_yaml_bytes(
                base_bytes, candidate
            )
            message = _commit_message(changed_keys, extra_changed)
            with tempfile.TemporaryDirectory(
                prefix="dispatcher-config-edit-"
            ) as tmp_dir:
                edit_file = Path(tmp_dir) / "project.yaml"
                edit_file.write_bytes(new_bytes)
                outcome = self._invoke(
                    project_yaml.parent,
                    message=message,
                    edit_file=edit_file,
                    if_match_hex=if_match_hex,
                )
        except Exception as err:  # noqa: BLE001 — spec §3: degrade, never raise
            # Everything past the guards becomes a failed outcome: temp-dir
            # creation, decode, render, even unexpected bugs. The trailing
            # audit line covers this path.
            #
            # `pre_launch` is not a guess: `_invoke` now labels every one of
            # its own exits, so anything reaching here happened before the
            # fork — rendering the YAML, writing the temp file, resolving the
            # target. Leaving the field unset made a pre-launch failure
            # indistinguishable from a mutation whose outcome is unknown,
            # which is the one inference `PHASE_*` exists to prevent.
            outcome = ActionOutcome(
                action="update-spec-runner-config",
                dir=repo_dir,
                ok=False,
                phase=PHASE_PRE_LAUNCH,
                error=str(err),
            )
        finally:
            with self._lock:
                self._busy.discard(repo_dir)
        _audit.info(
            "action=update-spec-runner-config repo=%s ok=%s detail=%s error=%s",
            repo_dir,
            outcome.ok,
            one_line(outcome.detail),
            one_line(outcome.error),
        )
        return outcome

    def _invoke(
        self,
        target: Path,
        *,
        message: str,
        edit_file: Path,
        if_match_hex: str,
    ) -> ActionOutcome:
        argv = [
            *self._command,
            "propose-pr",
            str(target),
            "--message",
            message,
            "--edit",
            f"project.yaml={edit_file}",
            "--if-match",
            f"project.yaml={if_match_hex}",
        ]
        try:
            # Bytes, NOT text=True — the same reason `core/actions.py` gives:
            # decoding inside the call puts a POST-execution failure inside a
            # pre-launch handler, and no ordering of handlers can undo that.
            # `propose-pr` branches, commits, pushes and opens a PR before it
            # writes a byte, so an undecodable answer is a mutation that
            # happened, not a launch that did not.
            proc = subprocess.run(argv, capture_output=True, timeout=_ACTION_TIMEOUT)
        except FileNotFoundError as err:
            # Deliberately narrow. The other pre-fork `OSError`s (EACCES on
            # the binary, E2BIG from an oversized argv, ENOMEM) reach
            # `run()`'s catch-all, which now stamps `pre_launch` itself — so
            # widening here buys nothing observable, and an arm no test can
            # distinguish is a claim no test can defend. `core/actions.py`
            # needs its own `except OSError` (N-3) because it has no outer
            # catch-all to fall back on.
            return ActionOutcome(
                action="update-spec-runner-config",
                dir=target.name,
                ok=False,
                phase=PHASE_PRE_LAUNCH,
                error=str(err),
            )
        except subprocess.TimeoutExpired as err:
            # The child started and was killed, so the PR may already
            # exist. `phase` is what stops that from being read as "nothing
            # happened"; it was left unset here while the success path set
            # it, which is a half-populated field — worse than an absent
            # one, because it looks answered.
            return ActionOutcome(
                action="update-spec-runner-config",
                dir=target.name,
                ok=False,
                phase=PHASE_LAUNCHED_UNREADABLE,
                error=str(err),
            )

        try:
            # stdout only: the answer lives there, and stderr's content
            # never travels now — only its byte count does — so its
            # encoding cannot make an answer unreadable.
            stdout = proc.stdout.decode("utf-8")
        except UnicodeDecodeError as err:
            # The child RAN: the PR may already exist. `phase` is what stops
            # that being read as "nothing happened".
            return ActionOutcome(
                action="update-spec-runner-config",
                dir=target.name,
                ok=False,
                phase=PHASE_LAUNCHED_UNREADABLE,
                error=str(err),
            )

        def unusable(reason: str) -> ActionOutcome:
            audit_unusable(
                _audit,
                verb="propose-pr",
                repo_dir=target.name,
                phase=PHASE_READABLE,
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
            return ActionOutcome(
                action="update-spec-runner-config",
                dir=target.name,
                ok=False,
                phase=PHASE_READABLE,
                error=reason,
            )

        # Same producer, same contract, same single ingestion path as
        # `core/actions.py`. This module used to keep its own `json.loads`
        # and its own `.get()` per field; two parse paths over one contract
        # are two sets of rules about what to believe, and they drift.
        try:
            ingested = ingest(stdout, returncode=proc.returncode)
        except ContractViolation as violation:
            return unusable(unusable_answer(str(violation)))
        except Exception as err:  # noqa: BLE001 - as in core/actions.py
            return unusable(consumer_failure(err))

        # The argv verb, not the DTO's label: this runner reports itself as
        # `update-spec-runner-config` while asking github-checker for
        # `propose-pr`, and it is the latter the answer must be about.
        mismatch = verb_mismatch(ingested, "propose-pr")
        if mismatch is not None:
            return unusable(unusable_answer(mismatch))
        return project_outcome(
            ingested, action="update-spec-runner-config", dir_name=target.name
        )
