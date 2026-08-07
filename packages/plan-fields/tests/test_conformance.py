"""The killer test: the parser reproduces every vendored PF-1 fixture."""

from __future__ import annotations

from plan_fields.parser import parse_todo
from plan_fields.validator import load_schema, run_conformance, validate_document


def test_all_fixtures_conform():
    results = run_conformance()
    failures = [f"{r.name}: {r.detail}" for r in results if not r.ok]
    assert not failures, "non-conforming fixtures:\n" + "\n".join(failures)
    # 7 simple pairs + 1 history bundle
    assert len(results) == 8


def test_schema_is_valid():
    # load_schema raising or a malformed schema would surface here
    schema = load_schema()
    assert schema["$id"] == "urn:ecosystem:plan-fields:v2:schema"


def test_parser_output_validates_against_schema():
    doc = parse_todo(
        "# demo\n\n- [ ] A thing @id:x @owner:tech-lead\n",
        "demo",
        generated_at="2026-07-28T00:00:00Z",
    )
    validate_document(doc)  # raises on non-conformance
    assert doc["nodes"][0]["node_id"] == "todo://demo/x"
