"""BEH-03 (WS-dispatcher-229): a declared `git_dir` spelling of the source
repo's own identity is still self-owner.

Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-03

Given a frozen manifest that links a declared `git_dir` spelling to the source
repository, and a repository owner that uses that spelling, the fleet layer
must resolve the same self-owner verdict it resolves for the canonical key —
reusing the manifest identity model (NFR-02) rather than a second matching
algorithm — and emit exactly one `PF-OWNER-REPO-SELF` warning, attached to the
node's URI with provenance.
"""

from __future__ import annotations

from plan_fields import ManifestIndex, RepoInput, parse_fleet, validate_document


def _index() -> ManifestIndex:
    return ManifestIndex(
        frozenset({"dispatcher", "maestro"}), {"legacy-checkout-dir": "dispatcher"}
    )


def test_git_dir_alias_owner_is_self_owner() -> None:
    doc = parse_fleet(
        [RepoInput("dispatcher", "- [ ] work @id:x @owner:repo:legacy-checkout-dir\n")],
        _index(),
    )
    validate_document(doc)
    self_owner = [d for d in doc["diagnostics"] if d["code"] == "PF-OWNER-REPO-SELF"]
    assert len(self_owner) == 1
    diag = self_owner[0]
    assert diag["severity"] == "warning"
    assert diag["subject_uri"] == "todo://dispatcher/x"
    assert diag["provenance"]["repo"] == "dispatcher"
    assert not any(d["code"] == "PF-OWNER-REPO-UNKNOWN" for d in doc["diagnostics"])


def test_git_dir_alias_input_spelling_also_resolves() -> None:
    # The scanned repo itself is supplied under its git_dir spelling, not the
    # canonical key: the fleet layer normalises inputs the same way it
    # normalises owner refs (`_canonical_input_repos`), so identity still
    # matches and the node still gets a self-owner warning.
    doc = parse_fleet(
        [RepoInput("legacy-checkout-dir", "- [ ] work @id:x @owner:repo:dispatcher\n")],
        _index(),
    )
    validate_document(doc)
    self_owner = [d for d in doc["diagnostics"] if d["code"] == "PF-OWNER-REPO-SELF"]
    assert len(self_owner) == 1
    assert self_owner[0]["subject_uri"] == "todo://dispatcher/x"


def test_git_dir_alias_verdict_matches_canonical_key_verdict() -> None:
    via_alias = parse_fleet(
        [RepoInput("dispatcher", "- [ ] work @id:x @owner:repo:legacy-checkout-dir\n")],
        _index(),
    )
    via_key = parse_fleet(
        [RepoInput("dispatcher", "- [ ] work @id:x @owner:repo:dispatcher\n")],
        _index(),
    )
    validate_document(via_alias)
    validate_document(via_key)

    def owner_codes(doc: dict) -> list[str]:
        return sorted(
            d["code"] for d in doc["diagnostics"] if d["code"].startswith("PF-OWNER")
        )

    assert owner_codes(via_alias) == owner_codes(via_key) == ["PF-OWNER-REPO-SELF"]

    alias_diag = next(
        d for d in via_alias["diagnostics"] if d["code"] == "PF-OWNER-REPO-SELF"
    )
    key_diag = next(
        d for d in via_key["diagnostics"] if d["code"] == "PF-OWNER-REPO-SELF"
    )
    # identical verdict shape — only the raw owner spelling quoted in the
    # message differs between the two inputs.
    assert alias_diag["severity"] == key_diag["severity"] == "warning"
    assert alias_diag["subject_uri"] == key_diag["subject_uri"]
    assert alias_diag["provenance"] == key_diag["provenance"]


def test_git_dir_alias_naming_another_repo_is_not_self_owner() -> None:
    # `legacy-checkout-dir` aliases to `dispatcher`, not `maestro`: a maestro
    # item owned by that alias is an external (dispatcher) owner, the same
    # negative verdict the canonical-key spelling gets in
    # test_owner_repo_self.py::test_owner_naming_a_different_manifest_repo_is_not_self_owner.
    doc = parse_fleet(
        [RepoInput("maestro", "- [ ] work @id:x @owner:repo:legacy-checkout-dir\n")],
        _index(),
    )
    validate_document(doc)
    assert not any(d["code"].startswith("PF-OWNER") for d in doc["diagnostics"])


def test_git_dir_alias_owner_node_ref_carries_resolved_identity() -> None:
    doc = parse_fleet(
        [RepoInput("dispatcher", "- [ ] work @id:x @owner:repo:legacy-checkout-dir\n")],
        _index(),
    )
    validate_document(doc)
    node = next(n for n in doc["nodes"] if n["id"] == "x")
    # `owner_ref` preserves the raw written spelling — resolution to self-owner
    # happens via the manifest index, not by rewriting what the author wrote.
    assert node["owner_ref"] == {
        "kind": "repository",
        "id": "legacy-checkout-dir",
        "raw": "repo:legacy-checkout-dir",
    }
