"""BEH-01 (WS-dispatcher-229): `repo:<own key>` is a self-owner, not a fleet edge.

Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-01

Given a frozen manifest that declares the canonical key `dispatcher` and a plan
node whose provenance is `dispatcher/TODO.md` with `owner_ref.kind=repository`
and `owner_ref.raw=repo:dispatcher`, the fleet layer must classify the owner as
self-owner regardless of checkbox state, emit exactly one `PF-OWNER-REPO-SELF`
warning attached to the node's URI, and must not also emit
`PF-OWNER-REPO-UNKNOWN` for the same owner.
"""

from __future__ import annotations

from plan_fields import ManifestIndex, RepoInput, parse_fleet, validate_document


def _index() -> ManifestIndex:
    return ManifestIndex(frozenset({"dispatcher", "maestro"}), {})


def test_canonical_key_self_owner_open_item() -> None:
    doc = parse_fleet(
        [RepoInput("dispatcher", "- [ ] work @id:x @owner:repo:dispatcher\n")],
        _index(),
    )
    validate_document(doc)
    self_owner = [d for d in doc["diagnostics"] if d["code"] == "PF-OWNER-REPO-SELF"]
    assert len(self_owner) == 1
    diag = self_owner[0]
    assert diag["severity"] == "warning"
    assert diag["subject_uri"] == "todo://dispatcher/x"
    assert not any(d["code"] == "PF-OWNER-REPO-UNKNOWN" for d in doc["diagnostics"])


def test_canonical_key_self_owner_verdict_independent_of_checkbox_state() -> None:
    open_doc = parse_fleet(
        [RepoInput("dispatcher", "- [ ] work @id:x @owner:repo:dispatcher\n")],
        _index(),
    )
    closed_doc = parse_fleet(
        [RepoInput("dispatcher", "- [x] work @id:x @owner:repo:dispatcher\n")],
        _index(),
    )
    for doc in (open_doc, closed_doc):
        validate_document(doc)
        codes = [
            d["code"] for d in doc["diagnostics"] if d["code"].startswith("PF-OWNER")
        ]
        assert codes == ["PF-OWNER-REPO-SELF"]


def test_exactly_one_diagnostic_per_self_owner_reference() -> None:
    text = (
        "- [ ] first @id:a @owner:repo:dispatcher\n"
        "- [ ] second @id:b @owner:repo:dispatcher\n"
    )
    doc = parse_fleet([RepoInput("dispatcher", text)], _index())
    validate_document(doc)
    by_subject: dict[str, int] = {}
    for d in doc["diagnostics"]:
        if d["code"] == "PF-OWNER-REPO-SELF":
            by_subject[d["subject_uri"]] = by_subject.get(d["subject_uri"], 0) + 1
    assert by_subject == {"todo://dispatcher/a": 1, "todo://dispatcher/b": 1}


def test_raw_owner_and_node_provenance_preserved() -> None:
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
    assert diag["provenance"]["repo"] == "dispatcher"
    assert diag["provenance"]["path"] == "TODO.md"
    assert diag["provenance"]["line"] == 1
    # explains the hand-off gap and points to a typed principal or TBD, without
    # pinning exact wording — the behaviour-spec's semantic requirement, not a
    # golden string
    assert "dispatcher" in diag["message"]
    assert "external principal" in diag["message"]
    assert "TBD" in diag["message"]


def test_self_owner_via_declared_git_dir_alias() -> None:
    index = ManifestIndex(
        frozenset({"dispatcher", "maestro"}), {"legacy-checkout-dir": "dispatcher"}
    )
    doc = parse_fleet(
        [RepoInput("dispatcher", "- [ ] work @id:x @owner:repo:legacy-checkout-dir\n")],
        index,
    )
    validate_document(doc)
    codes = [d["code"] for d in doc["diagnostics"] if d["code"].startswith("PF-OWNER")]
    assert codes == ["PF-OWNER-REPO-SELF"]


def test_owner_naming_a_different_manifest_repo_is_not_self_owner() -> None:
    doc = parse_fleet(
        [RepoInput("dispatcher", "- [ ] work @id:x @owner:repo:maestro\n")],
        _index(),
    )
    validate_document(doc)
    assert not any(
        d["code"].startswith("PF-OWNER") for d in doc["diagnostics"]
    )
