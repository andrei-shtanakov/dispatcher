"""BEH-07 (WS-dispatcher-229): non-repository owners never enter self-classification.

Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-07

Given an owner that is a typed person, a typed team, ``TBD`` or any other
non-repository variant, the fleet layer must form no repository
self/unknown/external verdict for that owner — regardless of source
repository — and must never emit ``PF-OWNER-REPO-SELF`` merely because some
raw fragment of the owner value happens to match the repository identity
(the source repo's own canonical key or a declared ``git_dir`` alias).

The fleet layer's owner-verdict block (``fleet_api.py``) only runs when
``owner_ref.kind == "repository"``; these tests pin that boundary so a future
change cannot start pattern-matching raw owner text against repo identity.
"""

from __future__ import annotations

import pytest
from plan_fields import ManifestIndex, RepoInput, parse_fleet, validate_document

_OWNER_VERDICT_CODES = ("PF-OWNER-REPO-SELF", "PF-OWNER-REPO-UNKNOWN")


def _index() -> ManifestIndex:
    return ManifestIndex(
        frozenset({"dispatcher", "maestro"}), {"legacy-checkout-dir": "dispatcher"}
    )


def _verdict_codes(doc: dict, subject_uri: str) -> list[str]:
    return [
        d["code"]
        for d in doc["diagnostics"]
        if d["subject_uri"] == subject_uri and d["code"] in _OWNER_VERDICT_CODES
    ]


@pytest.mark.parametrize(
    ("owner_tag", "expected_kind"),
    [
        # typed person whose id textually equals the source repo's own
        # canonical key — must not read as self-owner.
        ("github:dispatcher", "github_user"),
        # typed team whose id textually contains the source repo's own
        # canonical key as a path segment — must not read as self-owner.
        ("github-team:dispatcher/core", "github_team"),
        # typed person whose id textually equals a declared git_dir alias of
        # the source repo — must not read as self-owner via the alias either.
        ("github:legacy-checkout-dir", "github_user"),
        # typed team whose id textually equals a different manifest repo's
        # canonical key — must not read as external repo-owner.
        ("github-team:maestro/core", "github_team"),
        # TBD carries no textual owner identity at all.
        ("TBD", "tbd"),
    ],
)
def test_non_repository_owner_gets_no_repository_verdict(
    owner_tag: str, expected_kind: str
) -> None:
    doc = parse_fleet(
        [RepoInput("dispatcher", f"- [ ] work @id:x @owner:{owner_tag}\n")],
        _index(),
    )
    validate_document(doc)
    node = next(n for n in doc["nodes"] if n["id"] == "x")
    assert node["owner_ref"]["kind"] == expected_kind
    assert _verdict_codes(doc, "todo://dispatcher/x") == []


def test_non_repository_owners_from_any_source_repository_get_no_verdict() -> None:
    # The exclusion is not source-repo-specific: an owner of another type
    # emits no repository verdict regardless of which repo the node lives in.
    doc = parse_fleet(
        [RepoInput("maestro", "- [ ] work @id:x @owner:github:maestro\n")],
        _index(),
    )
    validate_document(doc)
    assert _verdict_codes(doc, "todo://maestro/x") == []


def test_mixed_fleet_only_repository_kind_gets_a_verdict() -> None:
    # One repo contributing a repository-kind self-owner alongside person,
    # team and TBD owners: only the repository-kind node may carry a verdict,
    # the others must stay entirely free of PF-OWNER-REPO-* diagnostics.
    text = (
        "- [ ] self @id:a @owner:repo:dispatcher\n"
        "- [ ] person @id:b @owner:github:dispatcher\n"
        "- [ ] team @id:c @owner:github-team:dispatcher/core\n"
        "- [ ] tbd @id:d @owner:TBD\n"
    )
    doc = parse_fleet([RepoInput("dispatcher", text)], _index())
    validate_document(doc)

    by_node: dict[str, list[str]] = {}
    for d in doc["diagnostics"]:
        if d["code"] in _OWNER_VERDICT_CODES:
            by_node.setdefault(d["subject_uri"], []).append(d["code"])

    assert by_node == {"todo://dispatcher/a": ["PF-OWNER-REPO-SELF"]}
    for node_id in ("b", "c", "d"):
        assert _verdict_codes(doc, f"todo://dispatcher/{node_id}") == []
