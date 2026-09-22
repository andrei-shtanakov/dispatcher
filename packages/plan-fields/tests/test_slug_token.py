"""`mentions_slug` — acceptance of a cross-repo request as a TOKEN, not a substring.

The pair from dispatcher#254 (devtools' inbox.is_accepted): `benchmark-2` must
not be "accepted" by an item that only says `benchmark-20`.
"""

from __future__ import annotations

import pytest

from plan_fields import mentions_slug, scrape_items


def test_prefix_of_a_longer_id_is_not_a_match() -> None:
    assert (
        mentions_slug("- [ ] deliver benchmark-20 @id:benchmark-20", "benchmark-2")
        is False
    )


def test_whole_token_matches() -> None:
    assert (
        mentions_slug("- [ ] deliver benchmark-2 @id:benchmark-2", "benchmark-2")
        is True
    )


def test_suffix_of_a_longer_id_is_not_a_match() -> None:
    assert mentions_slug("@id:pre-benchmark-2", "benchmark-2") is False


@pytest.mark.parametrize(
    "text",
    [
        "@id:benchmark-2",
        "todo://maestro/benchmark-2",
        "`benchmark-2`",
        "slug benchmark-2.",  # sentence-final dot ends the token
        "(benchmark-2)",
        "benchmark-2",
        "принятие benchmark-2, см. issue",
    ],
)
def test_id_grammar_boundaries_delimit_the_token(text: str) -> None:
    assert mentions_slug(text, "benchmark-2") is True


@pytest.mark.parametrize(
    "text",
    [
        "benchmark-2.1",  # dot followed by an id char continues the id
        "benchmark-2_x",
        "benchmark-2-x",
        "v1.benchmark-2",
        "benchmark-2x",
        "",
    ],
)
def test_id_grammar_characters_continue_the_token(text: str) -> None:
    assert mentions_slug(text, "benchmark-2") is False


def test_regex_metacharacters_in_a_slug_are_literal() -> None:
    # `.` is legal in an id; it must not act as a wildcard
    assert mentions_slug("bench.mark", "bench.mark") is True
    assert mentions_slug("benchXmark", "bench.mark") is False


@pytest.mark.parametrize("slug", ["", "Benchmark-2", "bench mark", "-lead", "x" * 65])
def test_a_slug_outside_the_id_grammar_never_matches(slug: str) -> None:
    # not an id ⇒ cannot be an id token; False rather than an exception, so a
    # malformed request body reads as "not accepted", never as a crash
    assert mentions_slug(f"- [ ] {slug}", slug) is False


def test_is_accepted_shape_over_scraped_items() -> None:
    # the consumer's call replaces its substring test with the shared rule
    todo = "# Plan\n- [ ] ship benchmark-20 @id:benchmark-20\nprose says benchmark-2\n"
    accepted = any(mentions_slug(i.raw_text, "benchmark-2") for i in scrape_items(todo))
    assert accepted is False
