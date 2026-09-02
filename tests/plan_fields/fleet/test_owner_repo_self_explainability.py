"""BEH-09 (WS-dispatcher-229): source owner and provenance survive unchanged.

Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-09

Given a self-owner recorded as a canonical key or a `git_dir` spelling, the
fleet analysis must preserve the exact source `owner_ref.raw`, the node URI
and the repository/file/location provenance of that same item — so a user can
match a `PF-OWNER-REPO-SELF` finding back to the concrete source line — and
must not rewrite `TODO.md`, the manifest, the contract or any other input
artifact.
"""

from __future__ import annotations

from plan_fields import ManifestIndex, RepoInput, parse_fleet, validate_document


def _index() -> ManifestIndex:
    return ManifestIndex(
        frozenset({"dispatcher", "maestro"}), {"legacy-checkout-dir": "dispatcher"}
    )


def test_canonical_key_raw_owner_and_provenance_preserved_verbatim() -> None:
    doc = parse_fleet(
        [RepoInput("dispatcher", "- [ ] work @id:x @owner:repo:dispatcher\n")],
        _index(),
    )
    validate_document(doc)
    node = next(n for n in doc["nodes"] if n["id"] == "x")
    assert node["owner_ref"] == {
        "kind": "repository",
        "id": "dispatcher",
        "raw": "repo:dispatcher",
    }
    diag = next(d for d in doc["diagnostics"] if d["code"] == "PF-OWNER-REPO-SELF")
    # the diagnostic's provenance is not a re-derived copy — it is the same
    # repo/file/location the node itself carries.
    assert diag["provenance"] == node["provenance"]
    assert diag["provenance"]["repo"] == "dispatcher"
    assert diag["provenance"]["path"] == "TODO.md"
    assert diag["provenance"]["line"] == 1


def test_git_dir_spelling_is_preserved_not_normalised_to_the_canonical_key() -> None:
    doc = parse_fleet(
        [RepoInput("dispatcher", "- [ ] work @id:x @owner:repo:legacy-checkout-dir\n")],
        _index(),
    )
    validate_document(doc)
    node = next(n for n in doc["nodes"] if n["id"] == "x")
    # the self-owner verdict is reached via manifest resolution, but the
    # recorded owner_ref keeps the operator's original spelling.
    assert node["owner_ref"] == {
        "kind": "repository",
        "id": "legacy-checkout-dir",
        "raw": "repo:legacy-checkout-dir",
    }
    diag = next(d for d in doc["diagnostics"] if d["code"] == "PF-OWNER-REPO-SELF")
    assert "legacy-checkout-dir" in diag["message"]
    assert diag["provenance"] == node["provenance"]


def test_diagnostic_lets_a_user_locate_the_exact_originating_item() -> None:
    # two self-owner findings in one repo must each point back to their own
    # line, never to each other's — the mapping subject_uri -> node ->
    # provenance is how a user walks from a finding to the source item.
    text = (
        "- [ ] first @id:a @owner:repo:dispatcher\n"
        "- [ ] second @id:b @owner:repo:legacy-checkout-dir\n"
    )
    doc = parse_fleet([RepoInput("dispatcher", text)], _index())
    validate_document(doc)

    by_id = {n["id"]: n for n in doc["nodes"]}
    diags = {
        d["subject_uri"]: d
        for d in doc["diagnostics"]
        if d["code"] == "PF-OWNER-REPO-SELF"
    }
    assert set(diags) == {"todo://dispatcher/a", "todo://dispatcher/b"}

    diag_a = diags["todo://dispatcher/a"]
    diag_b = diags["todo://dispatcher/b"]
    assert diag_a["provenance"]["line"] == by_id["a"]["provenance"]["line"] == 1
    assert diag_b["provenance"]["line"] == by_id["b"]["provenance"]["line"] == 2
    assert diag_a["provenance"] == by_id["a"]["provenance"]
    assert diag_b["provenance"] == by_id["b"]["provenance"]
    assert "repo:dispatcher" in diag_a["message"]
    assert "repo:legacy-checkout-dir" in diag_b["message"]


def test_pinned_commit_provenance_stays_attached_to_the_right_node() -> None:
    doc = parse_fleet(
        [
            RepoInput(
                "dispatcher",
                "- [ ] work @id:x @owner:repo:dispatcher\n",
                commit="deadbeef",
            )
        ],
        _index(),
    )
    validate_document(doc)
    node = next(n for n in doc["nodes"] if n["id"] == "x")
    diag = next(d for d in doc["diagnostics"] if d["code"] == "PF-OWNER-REPO-SELF")
    assert node["provenance"]["commit"] == "deadbeef"
    assert diag["provenance"]["commit"] == "deadbeef"


def test_analysis_never_rewrites_its_frozen_inputs() -> None:
    todo_text = "- [ ] work @id:x @owner:repo:dispatcher\n"
    repo_input = RepoInput("dispatcher", todo_text)
    index = _index()

    doc = parse_fleet([repo_input], index)
    validate_document(doc)

    # the frozen inputs passed in are untouched: same text, same identity
    # index, same RepoInput object — parse_fleet is read-only over its
    # arguments and writes to no file, manifest or contract.
    assert repo_input.todo_text == todo_text
    assert repo_input.repo == "dispatcher"
    assert index.canonical_keys == frozenset({"dispatcher", "maestro"})
    assert index.git_dir_to_key == {"legacy-checkout-dir": "dispatcher"}

    # calling it again on the same frozen inputs reproduces the identical
    # document — nothing about the inputs was consumed or mutated by the
    # first call.
    doc_again = parse_fleet([repo_input], index)
    assert doc_again == doc
