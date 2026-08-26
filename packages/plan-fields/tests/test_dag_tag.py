"""@dag grammar unit surface (canon fixtures pin the document level)."""

from plan_fields.parser import parse_dag, parse_todo
from plan_fields.scrape import last_tag_is_quoted, scrape_items


def _item(line: str):
    return scrape_items(f"# demo\n\n{line}\n")[0]


def test_valid_dag_matches_id():
    item = _item("- [ ] T @id:alpha @owner:github:u @dag:dags/alpha.yaml")
    value, diags = parse_dag(item, "alpha")
    assert value == "dags/alpha.yaml"
    assert diags == ()


def test_structural_codes_fire_on_closed_items_too():
    # the @epic precedent: a malformed tag on a closed item is still malformed
    doc = parse_todo(
        "# demo\n\n- [x] Done @id:z @owner:github:u @dag:dags/other.yaml\n",
        "demo",
        generated_at="2026-07-28T00:00:00Z",
    )
    codes = [d["code"] for d in doc["diagnostics"]]
    assert "PF-DAG-MISMATCH" in codes
    assert "dag" not in doc["nodes"][0]


def test_quoted_detection_follows_last_wins():
    assert last_tag_is_quoted('T @dag:"dags/x.yaml"', "dag")
    assert not last_tag_is_quoted("T @dag:dags/x.yaml", "dag")
    # last-wins: the surviving occurrence decides
    assert not last_tag_is_quoted('T @dag:"dags/a.yaml" @dag:dags/x.yaml', "dag")
    assert last_tag_is_quoted('T @dag:dags/a.yaml @dag:"dags/x.yaml"', "dag")
    # prose about a tag inside backticks is not a tag (tokenizer boundary)
    assert not last_tag_is_quoted('see `@dag:"x"` in docs', "dag")


def test_last_wins_on_repeated_dag_tags():
    # single-valued key convention (like @owner): the tags map is last-wins;
    # no DAG-MULTIPLE code exists in the r2 registry, so none is emitted
    item = _item("- [ ] T @id:a @owner:github:u @dag:dags/b.yaml @dag:dags/a.yaml")
    value, diags = parse_dag(item, "a")
    assert value == "dags/a.yaml"
    assert diags == ()
