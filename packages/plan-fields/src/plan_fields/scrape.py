"""Operational scrape: the syntactic fact of "what is a checkbox item".

This is the shared substrate under every plan-fields consumer. It answers only
the syntactic question — bullet, checkbox state, section, text, and the inline
tags — and deliberately does NOT synthesize identity, require an ``@id``, or
emit diagnostics. Those belong to the canonical projection (`parser.parse_todo`,
which is built on top of this) and to each consumer's own adapter.

Layering::

    scrape_items()
        ├── parse_todo()  -> canonical nodes/references/diagnostics (requires @id)
        ├── Robin adapter -> its own snapshot identity (file + normalized text)
        ├── devtools      -> fleet graph / cross-repo checks
        └── ATP adapter   -> citations / source-owner checks

There is ONE tag tokenizer (`_TAG_RE`), shared by the canonical parser and every
operational consumer, so the fleet cannot drift on what a tag is.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

# A checklist item: `-`/`*` bullet, any of `[ ]` / `[x]` / `[X]`, then the text.
# Leniency is deliberate — this must see the pre-@id fleet exactly as its authors
# wrote it, so no consumer needs its own regex.
_ITEM_RE = re.compile(r"^\s*([-*])\s*\[([ xX])\]\s+(.*)$")
_HEADING_RE = re.compile(r"^#{1,6}\s+(.+)")
# The one tag tokenizer: `@key:value`, value bare or quoted when it has spaces.
# The `@` must start the line or follow whitespace, so a tag *written about* in
# prose — `` `@blocked_by:` `` inside backticks, an email, a decorator — is not
# mistaken for a real tag (and neither its value nor the surrounding text is
# corrupted). This boundary is what operational consumers already relied on.
_TAG_RE = re.compile(r'(?:(?<=\s)|^)@([a-z][a-z_-]*):(?:"([^"]*)"|(\S+))')


@dataclass(frozen=True)
class ScrapedItem:
    """One checklist item as written — no identity, no validation.

    `raw_text` is the item text with surrounding whitespace trimmed but otherwise
    verbatim — tags and inner spacing are kept. `display_text` is the same with
    every recognized tag removed and whitespace collapsed. `item_id` is
    `tags.get("id")`, surfaced for convenience but never required.

    Two tag views: `tags` is the last-wins `{key: value}` map (convenient, and
    all a single-valued key needs); `tag_list` is the lossless `(key, value)`
    sequence in source order, keeping every occurrence — a key can legitimately
    repeat (e.g. two `@blocked_by:` on one item), which the map cannot hold."""

    checked: bool
    bullet: Literal["-", "*"]
    line: int
    section: str | None
    raw_text: str
    display_text: str
    tags: Mapping[str, str]
    item_id: str | None
    tag_list: tuple[tuple[str, str], ...]

    def values(self, key: str) -> tuple[str, ...]:
        """Every value for `key` in source order (empty when the key is absent)."""
        return tuple(value for tag, value in self.tag_list if tag == key)


def _tag_pairs(rest: str) -> tuple[tuple[str, str], ...]:
    """Every `(key, value)` tag on one line, in source order (shared tokenizer).

    The lossless form: repeats are kept, so a doubled `@blocked_by:` survives."""
    return tuple(
        (m.group(1), m.group(2) if m.group(2) is not None else m.group(3))
        for m in _TAG_RE.finditer(rest)
    )


def _canonical_title(rest: str) -> str:
    """Text before the first tag — the canonical parser's node title.

    Internal helper for `parser.parse_todo`, not operational API (consumers use
    `display_text`). Distinct from it: the canonical projection stops the title
    at the first `@tag`, whereas operational consumers strip every tag in place."""
    first = _TAG_RE.search(rest)
    return (rest[: first.start()] if first else rest).strip()


def _display_text(rest: str) -> str:
    """`rest` with every recognized tag removed and whitespace collapsed."""
    return " ".join(_TAG_RE.sub("", rest).split())


def scrape_items(text: str) -> list[ScrapedItem]:
    """Every checklist item in `text`, open and closed, in document order.

    Purely syntactic: items without an `@id` are returned like any other, no
    diagnostics are emitted, and no identity is synthesized."""
    items: list[ScrapedItem] = []
    section: str | None = None
    for lineno, line in enumerate(text.splitlines(), start=1):
        heading = _HEADING_RE.match(line)
        if heading:
            section = heading.group(1).strip()
            continue
        m = _ITEM_RE.match(line)
        if not m:
            continue
        rest = m.group(3).strip()
        if not rest:
            continue  # a bare checkbox with no text is not an item
        pairs = _tag_pairs(rest)
        tags = dict(pairs)  # last-wins convenience view
        bullet: Literal["-", "*"] = "-" if m.group(1) == "-" else "*"
        items.append(
            ScrapedItem(
                checked=m.group(2) in ("x", "X"),
                bullet=bullet,
                line=lineno,
                section=section,
                raw_text=rest,
                display_text=_display_text(rest),
                tags=MappingProxyType(tags),
                item_id=tags.get("id"),
                tag_list=pairs,
            )
        )
    return items
