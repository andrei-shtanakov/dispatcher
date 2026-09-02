"""BEH-10 (WS-dispatcher-229): single-repo parser stays grammar-only.

Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-10

Given a syntactically valid ``@owner:repo:<key>`` with no fleet provenance and
no frozen manifest, ``parse_todo`` (the single-repo parser) must keep
returning a valid typed repository owner under the existing contract and must
never emit an identity-verdict or the ``PF-OWNER-REPO-SELF`` diagnostic — that
classification needs a frozen manifest and only exists in the fleet layer
(``parse_fleet`` / ``fleet_api``). Existing valid fixtures and parser
contracts (``PF-OWNER-GRAMMAR`` for malformed owners) must keep their result
unchanged.
"""

from __future__ import annotations

from plan_fields import parse_todo, validate_document
from plan_fields.parser import parse_owner

_IDENTITY_VERDICT_CODES = ("PF-OWNER-REPO-SELF", "PF-OWNER-REPO-UNKNOWN")


def test_repo_owner_naming_its_own_source_repo_stays_grammar_only() -> None:
    # The owner names the exact same repo the document was parsed from —
    # the shape that a manifest-aware layer would classify as self-owner.
    # `parse_todo` never sees a manifest, so it must not attempt that call.
    doc = parse_todo("- [ ] work @id:x @owner:repo:dispatcher\n", "dispatcher")
    validate_document(doc)
    node = next(n for n in doc["nodes"] if n["id"] == "x")
    assert node["owner_ref"] == {
        "kind": "repository",
        "id": "dispatcher",
        "raw": "repo:dispatcher",
    }
    assert not [d for d in doc["diagnostics"] if d["code"].startswith("PF-OWNER")]


def test_repo_owner_naming_another_repo_also_stays_grammar_only() -> None:
    doc = parse_todo("- [ ] work @id:x @owner:repo:maestro\n", "dispatcher")
    validate_document(doc)
    node = next(n for n in doc["nodes"] if n["id"] == "x")
    assert node["owner_ref"]["kind"] == "repository"
    assert node["owner_ref"]["id"] == "maestro"
    assert not [d for d in doc["diagnostics"] if d["code"].startswith("PF-OWNER")]


def test_public_owner_parser_never_returns_an_identity_verdict() -> None:
    # `parse_owner` is the same function parse_todo calls per-item; it takes
    # no manifest and no fleet provenance, so it structurally cannot produce
    # PF-OWNER-REPO-SELF/-UNKNOWN — only PF-OWNER-GRAMMAR for bad syntax.
    owner_ref, owner_role, owner_diag = parse_owner("repo:dispatcher")
    assert owner_ref == {
        "kind": "repository",
        "id": "dispatcher",
        "raw": "repo:dispatcher",
    }
    assert owner_role is None
    assert owner_diag is None


def test_no_document_in_this_module_ever_carries_an_identity_verdict() -> None:
    text = (
        "- [ ] self-shaped @id:a @owner:repo:dispatcher\n"
        "- [ ] other-repo @id:b @owner:repo:maestro\n"
        "- [ ] unknown-repo @id:c @owner:repo:unheard-of\n"
    )
    doc = parse_todo(text, "dispatcher")
    validate_document(doc)
    codes = {d["code"] for d in doc["diagnostics"]}
    assert not codes.intersection(_IDENTITY_VERDICT_CODES)


def test_grammar_invalid_repository_owner_keeps_its_existing_verdict() -> None:
    # Existing single-repo parser contract (pre-dating this workstream) must
    # not change: malformed repo-owner syntax stays PF-OWNER-GRAMMAR, never an
    # identity verdict.
    owner_ref, _owner_role, owner_diag = parse_owner("repo:UPPER")
    assert owner_ref is None
    assert owner_diag == "PF-OWNER-GRAMMAR"

    doc = parse_todo("- [ ] work @id:x @owner:repo:UPPER\n", "dispatcher")
    validate_document(doc)
    codes = [
        d["code"]
        for d in doc["diagnostics"]
        if d["subject_uri"] == "todo://dispatcher/x"
    ]
    assert "PF-OWNER-GRAMMAR" in codes
    assert not any(c in _IDENTITY_VERDICT_CODES for c in codes)
