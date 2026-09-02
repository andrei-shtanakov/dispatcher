"""BEH-08 (WS-dispatcher-229): self-owner excluded from valid external repo-owned state.

Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-08

Given a fleet snapshot containing a canonical-key self-owner node, a git_dir
self-owner node and a valid external repo-owner node, the reporter-facing
owner views the read-model builds over that snapshot must expose self-owner
as its own machine-distinguishable state — never counted among nodes validly
repo-owned by an external repository — while the external node stays in that
class. Consumers read the verdict the fleet layer already computed; they must
never re-derive it by re-normalizing ``owner_ref.raw`` themselves (that would
duplicate ``ManifestIndex.resolve_ref`` identity logic between reporters).
"""

from __future__ import annotations

from plan_fields import ManifestIndex, RepoInput, parse_fleet, validate_document
from plan_fields.views import (
    REPO_OWNER_EXTERNAL,
    REPO_OWNER_SELF,
    REPO_OWNER_UNKNOWN,
    repo_owned_node_ids,
    repo_owner_verdicts,
)


def _index() -> ManifestIndex:
    return ManifestIndex(
        frozenset({"dispatcher", "maestro"}), {"legacy-checkout-dir": "dispatcher"}
    )


def _snapshot() -> dict:
    text = (
        "- [ ] canonical self @id:a @owner:repo:dispatcher\n"
        "- [ ] git_dir self @id:b @owner:repo:legacy-checkout-dir\n"
        "- [ ] external @id:c @owner:repo:maestro\n"
    )
    doc = parse_fleet([RepoInput("dispatcher", text)], _index())
    validate_document(doc)
    return doc


def test_self_owner_is_a_distinct_state_excluded_from_repo_owned() -> None:
    doc = _snapshot()
    verdicts = repo_owner_verdicts(doc)

    assert verdicts["todo://dispatcher/a"] == REPO_OWNER_SELF
    assert verdicts["todo://dispatcher/b"] == REPO_OWNER_SELF
    assert verdicts["todo://dispatcher/c"] == REPO_OWNER_EXTERNAL


def test_repo_owned_view_keeps_external_and_drops_both_self_forms() -> None:
    doc = _snapshot()
    owned = repo_owned_node_ids(doc)

    assert owned == ["todo://dispatcher/c"]
    assert "todo://dispatcher/a" not in owned
    assert "todo://dispatcher/b" not in owned


def test_unknown_repo_owner_is_also_excluded_from_repo_owned() -> None:
    text = "- [ ] unknown @id:z @owner:repo:unheard-of\n"
    doc = parse_fleet([RepoInput("dispatcher", text)], _index())
    validate_document(doc)

    assert repo_owner_verdicts(doc)["todo://dispatcher/z"] == REPO_OWNER_UNKNOWN
    assert repo_owned_node_ids(doc) == []


def test_consumer_never_needs_to_renormalize_the_raw_owner_string() -> None:
    # git_dir-spelled and canonical-key-spelled self-owner reach the identical
    # verdict without the caller inspecting owner_ref.raw or calling
    # ManifestIndex.resolve_ref itself.
    doc = _snapshot()
    verdicts = repo_owner_verdicts(doc)
    assert verdicts["todo://dispatcher/a"] == verdicts["todo://dispatcher/b"]


def test_non_repository_owner_kind_is_absent_from_verdicts_and_repo_owned() -> None:
    text = "- [ ] github owner @id:g @owner:github:octocat\n"
    doc = parse_fleet([RepoInput("dispatcher", text)], _index())
    validate_document(doc)

    assert "todo://dispatcher/g" not in repo_owner_verdicts(doc)
    assert repo_owned_node_ids(doc) == []


def test_repo_owned_view_is_sorted_across_multiple_external_nodes() -> None:
    text = (
        "- [ ] second @id:z @owner:repo:maestro\n"
        "- [ ] first @id:a @owner:repo:maestro\n"
    )
    doc = parse_fleet([RepoInput("dispatcher", text)], _index())
    validate_document(doc)

    assert repo_owned_node_ids(doc) == [
        "todo://dispatcher/a",
        "todo://dispatcher/z",
    ]
