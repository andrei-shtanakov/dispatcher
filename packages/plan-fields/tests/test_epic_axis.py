"""Stream axis (@epic / @defect) — grammar, layering, and the registry guard.

The point of these tests is not that the regexes work; it is that the delegation holds.
plan-fields must not own the epic grammar, must not claim registry membership it never
read, and must not let the two axes contaminate each other.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from plan_fields import (
    apply_registry,
    load_registry,
    parse_defect,
    parse_epic,
    parse_todo,
    validate_document,
)
from plan_fields.epic import EPIC_RE

_CONTRACT_EPICS = Path(__file__).parent.parent / "src/plan_fields/contract_epics"
_FIXTURES = _CONTRACT_EPICS / "fixtures"


def _one(text: str) -> dict:
    doc = parse_todo(text, "demo", "TODO.md", "2026-08-25T00:00:00Z")
    validate_document(doc)
    return doc


def _ep(doc: dict) -> list[str]:
    """Only the stream-axis codes: these inputs omit @owner, and PF-OWNER-MISSING
    is a different contract's finding — folding it in would make every assertion
    here also an assertion about owner semantics."""
    return [d["code"] for d in doc["diagnostics"] if d["code"].startswith("EP-")]


def test_grammar_comes_from_the_vendored_contract_not_from_this_package() -> None:
    """The regex must be the contract's, byte for byte — not a copy that drifts."""
    schema = json.loads((_CONTRACT_EPICS / "classification.schema.json").read_text())
    assert EPIC_RE.pattern == schema["$defs"]["EpicId"]["pattern"]


def test_well_formed_tag_is_tagged_with_both_axes() -> None:
    doc = _one("- [ ] Fix resume @id:a @epic:eco.ops @defect:pipeline\n")
    node = doc["nodes"][0]
    assert (node["epic"], node["defect"]) == ("eco.ops", "pipeline")
    assert node["epic_classification"] == "tagged"
    assert _ep(doc) == []


def test_open_item_without_an_epic_is_missing_not_invalid() -> None:
    doc = _one("- [ ] No stream @id:a\n")
    assert doc["nodes"][0]["epic_classification"] == "missing"
    assert _ep(doc) == ["EP-MISSING"]
    missing = [d for d in doc["diagnostics"] if d["code"] == "EP-MISSING"]
    assert missing[0]["severity"] == "warning"


def test_closed_item_carries_no_epic_obligation() -> None:
    """Ф4 marks open work only; a closed item must not manufacture debt."""
    doc = _one("- [x] Done long ago @id:a\n")
    assert doc["nodes"][0]["epic_classification"] == "missing"
    assert _ep(doc) == []


def test_malformed_epic_on_a_closed_item_still_reports() -> None:
    """Deferring EP-MISSING must not silence structural defects."""
    doc = _one("- [x] Done @id:a @epic:eco\n")
    assert _ep(doc) == ["EP-GRAMMAR"]


@pytest.mark.parametrize(
    "values,code",
    [
        (("eco",), "EP-GRAMMAR"),
        (("Eco.Ops",), "EP-GRAMMAR"),
        (("eco.a.b",), "EP-GRAMMAR"),
        (("eco.ops", "eco.ops"), "EP-MULTIPLE"),
    ],
)
def test_epic_grammar_and_multiplicity(values: tuple[str, ...], code: str) -> None:
    epic, classification, diag = parse_epic(values)
    assert (epic, classification, diag) == (None, "invalid", code)


def test_identical_duplicates_are_still_multiple() -> None:
    """A duplicate is a defect in the record, not a consensus."""
    assert parse_epic(("eco.ops", "eco.ops"))[2] == "EP-MULTIPLE"


def test_defect_axis_fails_independently_of_the_epic() -> None:
    """A bad @defect must never cost the item its stream classification."""
    doc = _one("- [ ] Fix @id:a @epic:eco.ops @defect:UI\n")
    node = doc["nodes"][0]
    assert node["epic"] == "eco.ops" and node["epic_classification"] == "tagged"
    assert node["defect"] is None
    assert _ep(doc) == ["EP-DEFECT-GRAMMAR"]


def test_defect_without_epic_is_still_missing_a_stream() -> None:
    doc = _one("- [ ] Fix @id:a @defect:pipeline\n")
    assert _ep(doc) == ["EP-MISSING"]
    assert doc["nodes"][0]["defect"] == "pipeline"


def test_parse_defect_is_public_and_orthogonal() -> None:
    assert parse_defect(("pipeline",)) == ("pipeline", None)
    assert parse_defect(()) == (None, None)


# ── registry layer ───────────────────────────────────────────────────────────


def test_single_repo_layer_never_claims_registry_membership() -> None:
    """A repo-local parse cannot know the registry; `tagged` here means well-formed."""
    doc = _one("- [ ] Ship @id:a @epic:eco.totally-made-up\n")
    assert doc["nodes"][0]["epic_classification"] == "tagged"
    assert _ep(doc) == []


def test_registry_downgrades_unknown_epic() -> None:
    doc = _one("- [ ] Ship @id:a @epic:eco.dark-factroy\n")
    registry = load_registry(_FIXTURES / "registry.toml")
    extra = apply_registry(doc, registry)
    assert [d["code"] for d in extra] == ["EP-UNKNOWN"]
    assert doc["nodes"][0]["epic_classification"] == "invalid"


def test_registry_reports_retired_epic_and_resolves_it_to_the_final_id() -> None:
    registry = load_registry(_FIXTURES / "registry.toml")
    assert registry.resolve("eco.codex-review") == (
        "eco.codex-review-rollout",
        "EP-MOVED",
    )


def test_absent_registry_downgrades_nothing_and_says_so() -> None:
    """No registry is not the same as a clean registry."""
    registry = load_registry(_FIXTURES / "does-not-exist.toml")
    assert [d["code"] for d in registry.diagnostics] == ["EP-REG-POLICY-INVALID"]
    assert registry.epics == {}


def test_registry_fixtures_reproduce_their_pinned_diagnostics() -> None:
    cases = [(_FIXTURES / "registry.toml", _FIXTURES / "registry/valid.expected.json")]
    cases += [
        (p, p.with_suffix(".expected.json"))
        for p in sorted((_FIXTURES / "registry").glob("*.toml"))
    ]
    assert len(cases) == 12
    for toml_path, expected_path in cases:
        want = sorted(
            d["code"] for d in json.loads(expected_path.read_text())["diagnostics"]
        )
        got = sorted(d["code"] for d in load_registry(toml_path).diagnostics)
        assert got == want, f"{toml_path.name}: expected {want}, got {got}"


def test_program_kind_is_the_ecosystem_versus_external_filter() -> None:
    registry = load_registry(_FIXTURES / "registry.toml")
    assert registry.kind_of("eco.ops") == "ecosystem"
    assert registry.kind_of("airun.kapelle-m3") == "external"
    assert registry.kind_of("nosuch.stream") is None


def test_registry_also_guards_the_defect_class_without_touching_the_stream() -> None:
    """An unknown defect class is a defect-axis finding, full stop.

    Downgrading `epic_classification` here would drop the item out of its stream's
    aggregate because of a mislabelled fix — and in-epic defects are exactly what
    the orthogonal axis exists to keep countable.
    """
    doc = parse_todo(
        "- [ ] Fix @id:a @epic:eco.ops @defect:nosuchclass\n",
        "demo",
        "TODO.md",
        "2026-08-25T00:00:00Z",
    )
    registry = load_registry(_FIXTURES / "registry.toml")
    extra = apply_registry(doc, registry)
    assert [d["code"] for d in extra] == ["EP-DEFECT-UNKNOWN"]
    assert doc["nodes"][0]["epic_classification"] == "tagged"


def test_every_emitted_code_is_declared_by_the_vendored_epics_registry() -> None:
    """No EP-* code may exist only in this package's head.

    This is the invariant that actually broke while Ф1a was being written: the parser
    needed a code for a duplicate @defect, epics/v1 had none, and for a while the
    package could emit something the contract did not declare. A consumer inventing
    codes is how a "closed registry" quietly stops being closed — so the check is a
    test rather than a habit.

    The registry is read as text, not with a YAML parser: pyyaml is not a dependency
    of this package (jsonschema is the only one) and adding one for a test would put a
    runtime dependency on the standalone-by-design parser.
    """
    from plan_fields.epic import EPIC_MESSAGES

    declared = {
        line.strip().rstrip(":")
        for line in (_CONTRACT_EPICS / "diagnostics.yaml").read_text().splitlines()
        if line.startswith("  EP-") and line.rstrip().endswith(":")
    }
    assert declared, "vendored diagnostics.yaml declares no codes — wrong copy?"
    emitted = set(EPIC_MESSAGES) | {"EP-UNKNOWN", "EP-MOVED", "EP-DEFECT-UNKNOWN"}
    assert emitted <= declared, f"undeclared codes: {sorted(emitted - declared)}"


def test_registry_schema_agrees_with_the_explicit_checks() -> None:
    """The schema and the hand-written checks must agree on every registry fixture.

    `_structural_diagnostics` classifies defects explicitly instead of reading
    jsonschema's error text, which buys precise codes and costs a guarantee: the two
    can drift apart. This test is that guarantee — the docstring used to claim it
    before it existed, which is the same defect class as a named-but-untested rule.

    The split it pins comes from the contract's own fixtures/README: some registry
    defects are schema-expressible, the rest are referential (they hold BETWEEN keys)
    and no JSON Schema can state them.
    """
    from jsonschema import Draft202012Validator

    schema = json.loads((_CONTRACT_EPICS / "registry.schema.json").read_text())
    validator = Draft202012Validator(schema)
    referential = {
        "program-unknown.toml",
        "moved-dangling.toml",
        "moved-chain.toml",
        "moved-cycle.toml",
    }
    checked = 0
    for toml_path in sorted((_FIXTURES / "registry").glob("*.toml")):
        rejected = bool(
            list(validator.iter_errors(tomllib.loads(toml_path.read_text())))
        )
        expected = toml_path.name not in referential
        assert rejected is expected, (
            f"{toml_path.name}: schema {'rejects' if rejected else 'accepts'}, "
            f"fixtures/README says it should {'reject' if expected else 'accept'}"
        )
        checked += 1
    assert checked == 11  # every registry case except the baseline `registry.toml`
    # and the baseline itself must be clean under both halves
    assert not list(
        validator.iter_errors(tomllib.loads((_FIXTURES / "registry.toml").read_text()))
    )
    assert load_registry(_FIXTURES / "registry.toml").diagnostics == ()


def test_a_section_of_the_wrong_shape_is_reported_not_raised() -> None:
    """`load_registry` promises never to raise; a non-table section is the case that would.

    "The tool crashed" and "the registry has a defect" look identical to an operator,
    so the malformed section must come back as a finding.
    """
    registry = load_registry(_FIXTURES / "registry/malformed-section.toml")
    assert [d["code"] for d in registry.diagnostics] == ["EP-REG-MALFORMED"]
    assert registry.epics == {}


def test_the_schema_backstop_catches_what_no_explicit_check_names(tmp_path) -> None:
    """A schema violation with no dedicated code must still surface."""
    bad = tmp_path / "epics.toml"
    bad.write_text(
        (_FIXTURES / "registry.toml")
        .read_text()
        .replace('schema_version = "1.0.0"', 'schema_version = "9.9.9-nope"')
    )
    codes = [d["code"] for d in load_registry(bad).diagnostics]
    assert codes == ["EP-REG-MALFORMED"]


def test_the_backstop_does_not_suppress_findings_that_merely_share_a_name(
    tmp_path,
) -> None:
    """Dedup is by section and entry key, never by any matching path segment.

    An epic whose own key collides with an unrelated schema path segment must not make
    that unrelated finding vanish: a backstop that silently drops diagnostics is worse
    than no backstop, because it looks like a clean registry.
    """
    text = (
        (_FIXTURES / "registry.toml")
        .read_text()
        .replace('schema_version = "1.0.0"', 'schema_version = "nope"')
    )
    bad = tmp_path / "epics.toml"
    bad.write_text(text)
    codes = [d["code"] for d in load_registry(bad).diagnostics]
    assert codes == ["EP-REG-MALFORMED"], codes
