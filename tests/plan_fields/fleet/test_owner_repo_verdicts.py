"""BEH-04/BEH-05 (WS-dispatcher-229): repository owner verdicts.

Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-04
Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-05

BEH-04: given a repository owner that is textually similar to the source
repo's canonical key or to a declared `git_dir` spelling, but is not itself
declared in the frozen manifest (as a key or as a `git_dir` alias), the fleet
layer must resolve the existing unknown repo-owner verdict
(`PF-OWNER-REPO-UNKNOWN`) and must never fall back to `PF-OWNER-REPO-SELF` on
the strength of the resemblance — identity is decided by exact canonical
resolution (`ManifestIndex.resolve_ref`), never by string similarity.

BEH-05: given an owner repository that resolves through the frozen manifest
to a canonical identity distinct from the source repository's, the verdict is
external repo-owner — the node gets neither `PF-OWNER-REPO-SELF` nor
`PF-OWNER-REPO-UNKNOWN` and remains a valid external repo-owned item.
"""

from __future__ import annotations

from plan_fields import ManifestIndex, RepoInput, parse_fleet, validate_document


def _index() -> ManifestIndex:
    return ManifestIndex(
        frozenset({"dispatcher", "maestro"}), {"legacy-checkout-dir": "dispatcher"}
    )


def test_name_similar_to_canonical_key_is_not_self_owner() -> None:
    # "dispatcher-fork" reads like the source repo's own key but is not
    # declared anywhere in the manifest.
    doc = parse_fleet(
        [RepoInput("dispatcher", "- [ ] work @id:x @owner:repo:dispatcher-fork\n")],
        _index(),
    )
    validate_document(doc)
    owner_codes = [
        d["code"] for d in doc["diagnostics"] if d["code"].startswith("PF-OWNER")
    ]
    assert owner_codes == ["PF-OWNER-REPO-UNKNOWN"]
    diag = next(d for d in doc["diagnostics"] if d["code"] == "PF-OWNER-REPO-UNKNOWN")
    assert diag["subject_uri"] == "todo://dispatcher/x"
    assert "dispatcher-fork" in diag["message"]


def test_name_similar_to_declared_git_dir_alias_is_not_self_owner() -> None:
    # "legacy-checkout-dir-2" reads like the declared git_dir alias
    # "legacy-checkout-dir", but only the exact alias is declared.
    doc = parse_fleet(
        [
            RepoInput(
                "dispatcher", "- [ ] work @id:x @owner:repo:legacy-checkout-dir-2\n"
            )
        ],
        _index(),
    )
    validate_document(doc)
    owner_codes = [
        d["code"] for d in doc["diagnostics"] if d["code"].startswith("PF-OWNER")
    ]
    assert owner_codes == ["PF-OWNER-REPO-UNKNOWN"]


def test_prefix_of_declared_git_dir_alias_is_not_self_owner() -> None:
    # "legacy-checkout" is a strict prefix of the declared alias, not the
    # alias itself.
    doc = parse_fleet(
        [RepoInput("dispatcher", "- [ ] work @id:x @owner:repo:legacy-checkout\n")],
        _index(),
    )
    validate_document(doc)
    owner_codes = [
        d["code"] for d in doc["diagnostics"] if d["code"].startswith("PF-OWNER")
    ]
    assert owner_codes == ["PF-OWNER-REPO-UNKNOWN"]


def test_known_external_repo_by_canonical_key_is_valid_repo_owner() -> None:
    # "maestro" is declared in the frozen manifest and differs from the
    # source repo "dispatcher" — a valid external repo-owner, no verdict
    # diagnostic either way.
    doc = parse_fleet(
        [RepoInput("dispatcher", "- [ ] work @id:x @owner:repo:maestro\n")],
        _index(),
    )
    validate_document(doc)
    owner_codes = [
        d["code"] for d in doc["diagnostics"] if d["code"].startswith("PF-OWNER")
    ]
    assert owner_codes == []


def test_known_external_repo_by_git_dir_alias_is_valid_repo_owner() -> None:
    # The declared git_dir alias resolves to "dispatcher" itself, which is the
    # source repo here — an *external* owner is checked with an alias that
    # resolves to the OTHER declared repo instead.
    index = ManifestIndex(
        frozenset({"dispatcher", "maestro"}),
        {"legacy-checkout-dir": "dispatcher", "maestro-checkout": "maestro"},
    )
    doc = parse_fleet(
        [RepoInput("dispatcher", "- [ ] work @id:x @owner:repo:maestro-checkout\n")],
        index,
    )
    validate_document(doc)
    owner_codes = [
        d["code"] for d in doc["diagnostics"] if d["code"].startswith("PF-OWNER")
    ]
    assert owner_codes == []
