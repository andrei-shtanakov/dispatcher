"""Operational scrape — the pre-@id substrate under every consumer (PF-7)."""

from __future__ import annotations

from plan_fields.parser import parse_todo
from plan_fields.scrape import scrape_items


def test_returns_items_without_an_id() -> None:
    # the whole point: the fleet is pre-@id, and scrape must still see it
    items = scrape_items("- [ ] plain item with no tags at all\n")
    assert len(items) == 1
    it = items[0]
    assert it.item_id is None and it.tags == {}
    assert it.checked is False and it.bullet == "-"
    assert it.raw_text == it.display_text == "plain item with no tags at all"


def test_star_bullet_and_uppercase_checkbox() -> None:
    items = scrape_items("* [X] done via star bullet\n* [ ] open via star\n")
    assert [(i.bullet, i.checked) for i in items] == [("*", True), ("*", False)]


def test_headings_become_section() -> None:
    md = "# Top\n- [ ] under top\n## Nested\n- [ ] under nested\n- [ ] still nested\n"
    items = scrape_items(md)
    assert [i.section for i in items] == ["Top", "Nested", "Nested"]


def test_item_before_any_heading_has_no_section() -> None:
    assert scrape_items("- [ ] orphan\n")[0].section is None


def test_display_text_removes_all_tags_not_truncates_at_first() -> None:
    # the correction: display_text strips every recognized tag in place, it does
    # NOT cut the line at the first '@' (that is the canonical title's job)
    it = scrape_items('- [ ] do X @owner:a and then Y @trigger:"p95 > 200ms"\n')[0]
    assert it.display_text == "do X and then Y"
    assert it.raw_text == 'do X @owner:a and then Y @trigger:"p95 > 200ms"'


def test_multiple_tags_extracted_including_unmodeled() -> None:
    it = scrape_items(
        "- [x] ship @id:x @owner:tech-lead @blocked_by:maestro#dogfood "
        "@source-ref:PR-1 @trigger:launch\n"
    )[0]
    assert it.item_id == "x"
    assert dict(it.tags) == {
        "id": "x",
        "owner": "tech-lead",
        "blocked_by": "maestro#dogfood",
        "source-ref": "PR-1",
        "trigger": "launch",
    }


def test_tag_written_about_in_prose_is_not_a_tag() -> None:
    # the `@` must start the line or follow whitespace: a tag NAMED in prose
    # (here inside backticks) is not a real tag, so neither its "value" nor the
    # surrounding text is corrupted. Regression for the 1/203 fleet divergence.
    it = scrape_items(
        "- [ ] graph has 15 `@blocked_by:` and 20 `@trigger:` edges @owner:andrei\n"
    )[0]
    assert dict(it.tags) == {"owner": "andrei"}
    assert it.item_id is None
    assert it.display_text == "graph has 15 `@blocked_by:` and 20 `@trigger:` edges"


def test_raw_text_is_preserved_verbatim() -> None:
    it = scrape_items("- [ ]   spaced   out   @owner:a  \n")[0]
    assert it.raw_text == "spaced   out   @owner:a"  # inner spacing kept, ends trimmed


def test_bare_checkbox_with_no_text_is_skipped() -> None:
    assert scrape_items("- [ ] \n- [x]\n") == []


def test_line_numbers_are_one_based_document_positions() -> None:
    items = scrape_items("# H\n\n- [ ] first\nprose\n- [ ] second\n")
    assert [i.line for i in items] == [3, 5]


def test_repeated_tag_kept_in_tag_list_but_last_wins_in_tags() -> None:
    # a key can legitimately repeat (two blockers); tag_list keeps both in order,
    # the last-wins `tags` map keeps the convenience single value (Robin's view).
    it = scrape_items(
        "- [ ] c @blocked_by:maestro#a @blocked_by:arbiter#b @owner:andrei\n"
    )[0]
    assert it.tag_list == (
        ("blocked_by", "maestro#a"),
        ("blocked_by", "arbiter#b"),
        ("owner", "andrei"),
    )
    assert it.tags["blocked_by"] == "arbiter#b"  # last-wins, unchanged
    assert it.tags["owner"] == "andrei"


def test_values_helper_returns_all_values_for_a_key_in_order() -> None:
    it = scrape_items("- [ ] c @blocked_by:x#1 @blocked_by:y#2\n")[0]
    assert it.values("blocked_by") == ("x#1", "y#2")
    assert it.values("owner") == ()  # absent key -> empty


def test_parse_todo_emits_one_reference_per_blocker() -> None:
    # the lossy case that blocked devtools: two @blocked_by on one item must
    # become two references (and two edges), never collapsed to the last one.
    md = (
        "- [ ] a @id:a\n"
        "- [ ] b @id:b\n"
        "- [ ] c @id:c @blocked_by:todo://demo/a @blocked_by:todo://demo/b\n"
    )
    doc = parse_todo(md, repo="demo")
    refs = [r for r in doc["references"] if r["source_node_id"] == "todo://demo/c"]
    assert len(refs) == 2
    assert {r["resolved_target"] for r in refs} == {"todo://demo/a", "todo://demo/b"}
    edges = [e for e in doc["edges"] if e["source_node_id"] == "todo://demo/c"]
    assert {e["target_node_id"] for e in edges} == {"todo://demo/a", "todo://demo/b"}


def test_scrape_is_the_substrate_parse_todo_gates_on_id() -> None:
    # scrape sees the pre-@id item; the canonical projection drops it (PF-ID-MISSING).
    # This is exactly why consumers need scrape, not parse_todo, before PF-2B.
    md = "- [ ] no id here yet\n"
    assert len(scrape_items(md)) == 1
    doc = parse_todo(md, repo="demo")
    assert doc["nodes"] == []
    assert any(d["code"] == "PF-ID-MISSING" for d in doc["diagnostics"])
