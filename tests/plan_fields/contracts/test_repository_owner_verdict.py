"""BEH-06 (WS-dispatcher-229): repository owner verdicts are mutually exclusive.

Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-06

Given a syntactically valid ``owner_ref`` of kind ``repository`` and a frozen
manifest, fleet identity-classification must yield exactly one machine
verdict — self-owner, external repo-owner or unknown repo-owner. One owner
reference must never carry both ``PF-OWNER-REPO-SELF`` and
``PF-OWNER-REPO-UNKNOWN`` at once, and a syntactically invalid owner keeps its
existing grammar verdict (``PF-OWNER-GRAMMAR``) rather than being overridden
by a repository-identity diagnostic.
"""

from __future__ import annotations

import pytest
from plan_fields import ManifestIndex, RepoInput, parse_fleet, validate_document
from plan_fields.parser import parse_owner

_OWNER_VERDICT_CODES = ("PF-OWNER-REPO-SELF", "PF-OWNER-REPO-UNKNOWN")


def _index() -> ManifestIndex:
    return ManifestIndex(
        frozenset({"dispatcher", "maestro"}), {"legacy-checkout-dir": "dispatcher"}
    )


def _owner_codes(doc: dict, subject_uri: str) -> list[str]:
    return [
        d["code"]
        for d in doc["diagnostics"]
        if d["subject_uri"] == subject_uri and d["code"].startswith("PF-OWNER")
    ]


@pytest.mark.parametrize(
    ("owner_tag", "expected_verdict_codes"),
    [
        # self-owner: repository owner names the item's own source repo.
        ("repo:dispatcher", ["PF-OWNER-REPO-SELF"]),
        # self-owner spelled via a declared git_dir alias of the source repo.
        ("repo:legacy-checkout-dir", ["PF-OWNER-REPO-SELF"]),
        # external repo-owner: known manifest repo distinct from the source.
        ("repo:maestro", []),
        # unknown repo-owner: not declared in the frozen manifest at all.
        ("repo:unheard-of", ["PF-OWNER-REPO-UNKNOWN"]),
        # unknown repo-owner: textually similar to the source key but not
        # itself declared — identity is exact resolution, not resemblance.
        ("repo:dispatcher-fork", ["PF-OWNER-REPO-UNKNOWN"]),
    ],
)
def test_exactly_one_repository_verdict(
    owner_tag: str, expected_verdict_codes: list[str]
) -> None:
    doc = parse_fleet(
        [RepoInput("dispatcher", f"- [ ] work @id:x @owner:{owner_tag}\n")],
        _index(),
    )
    validate_document(doc)
    codes = _owner_codes(doc, "todo://dispatcher/x")
    assert codes == expected_verdict_codes
    # exactly one verdict means never both self and unknown at once, for any
    # owner_ref of kind repository.
    assert not ("PF-OWNER-REPO-SELF" in codes and "PF-OWNER-REPO-UNKNOWN" in codes)


@pytest.mark.parametrize(
    "owner_tag",
    ["repo:dispatcher", "repo:legacy-checkout-dir", "repo:maestro", "repo:unheard-of"],
)
def test_self_and_unknown_never_co_occur(owner_tag: str) -> None:
    doc = parse_fleet(
        [RepoInput("dispatcher", f"- [ ] work @id:x @owner:{owner_tag}\n")],
        _index(),
    )
    validate_document(doc)
    codes = set(_owner_codes(doc, "todo://dispatcher/x"))
    assert not {"PF-OWNER-REPO-SELF", "PF-OWNER-REPO-UNKNOWN"}.issubset(codes)


def test_grammar_invalid_owner_keeps_grammar_verdict_uncontested() -> None:
    # "repo:UPPER" fails the repository-owner grammar (lowercase-only), so it
    # must resolve to PF-OWNER-GRAMMAR and never gain a repository-identity
    # verdict — the parser, not the fleet layer, owns this decision.
    owner_ref, _owner_role, owner_diag = parse_owner("repo:UPPER")
    assert owner_ref is None
    assert owner_diag == "PF-OWNER-GRAMMAR"

    doc = parse_fleet(
        [RepoInput("dispatcher", "- [ ] work @id:x @owner:repo:UPPER\n")],
        _index(),
    )
    validate_document(doc)
    codes = [
        d["code"]
        for d in doc["diagnostics"]
        if d["subject_uri"] == "todo://dispatcher/x"
    ]
    assert "PF-OWNER-GRAMMAR" in codes
    assert not any(c in _OWNER_VERDICT_CODES for c in codes)


def test_verdicts_are_mutually_exclusive_across_mixed_fleet() -> None:
    # One repo contributing all four owner shapes at once: the invariant must
    # hold per-node, independent of what other nodes in the same scan resolve
    # to.
    text = (
        "- [ ] self @id:a @owner:repo:dispatcher\n"
        "- [ ] external @id:b @owner:repo:maestro\n"
        "- [ ] unknown @id:c @owner:repo:unheard-of\n"
        "- [ ] bad-grammar @id:d @owner:repo:UPPER\n"
    )
    doc = parse_fleet([RepoInput("dispatcher", text)], _index())
    validate_document(doc)

    by_node: dict[str, list[str]] = {}
    for d in doc["diagnostics"]:
        if d["code"].startswith("PF-OWNER"):
            by_node.setdefault(d["subject_uri"], []).append(d["code"])

    assert by_node["todo://dispatcher/a"] == ["PF-OWNER-REPO-SELF"]
    assert "todo://dispatcher/b" not in by_node
    assert by_node["todo://dispatcher/c"] == ["PF-OWNER-REPO-UNKNOWN"]
    assert by_node["todo://dispatcher/d"] == ["PF-OWNER-GRAMMAR"]

    for codes in by_node.values():
        assert not {"PF-OWNER-REPO-SELF", "PF-OWNER-REPO-UNKNOWN"}.issubset(codes)
