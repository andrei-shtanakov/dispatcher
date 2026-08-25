from plan_fields import (
    ManifestIndex,
    RepoInput,
    parse_fleet,
    parse_owner,
    parse_todo,
    validate_document,
)


def test_public_owner_parser_covers_items_without_an_id() -> None:
    assert parse_owner("github:andrei-shtanakov") == (
        {
            "kind": "github_user",
            "id": "andrei-shtanakov",
            "raw": "github:andrei-shtanakov",
        },
        None,
        None,
    )
    repo_ref, _, _ = parse_owner("repo:devtools")
    tbd_ref, _, _ = parse_owner("TBD")
    assert repo_ref is not None and repo_ref["kind"] == "repository"
    assert tbd_ref is not None and tbd_ref["kind"] == "tbd"
    assert parse_owner(None) == (None, None, "PF-OWNER-MISSING")
    assert parse_owner("tech-lead") == (None, "tech-lead", "PF-OWNER-LEGACY-ROLE")
    assert parse_owner("not valid!") == (None, None, "PF-OWNER-GRAMMAR")


def test_typed_owner_forms_project_to_owner_ref() -> None:
    text = (
        "- [ ] human @id:h @owner:github:andrei-shtanakov\n"
        "- [ ] team @id:t @owner:github-team:example/platform\n"
        "- [ ] repo @id:r @owner:repo:dispatcher\n"
        "- [ ] tbd @id:x @owner:TBD\n"
    )
    doc = parse_todo(text, "demo")
    validate_document(doc)
    by_id = {node["id"]: node for node in doc["nodes"]}
    assert by_id["h"]["owner_ref"]["kind"] == "github_user"
    assert by_id["t"]["owner_ref"]["id"] == "example/platform"
    assert by_id["r"]["owner_ref"]["kind"] == "repository"
    assert by_id["x"]["owner_ref"] == {"kind": "tbd", "id": None, "raw": "TBD"}
    assert not [d for d in doc["diagnostics"] if d["code"].startswith("PF-OWNER")]


def test_legacy_role_is_explicit_transition() -> None:
    doc = parse_todo("- [ ] old @id:x @owner:tech-lead\n", "demo")
    assert doc["nodes"][0]["owner_ref"] is None
    assert doc["nodes"][0]["owner_role"] == "tech-lead"
    # the item carries no @epic, so v3 also reports EP-MISSING; this test is about
    # the owner axis and must not become an assertion about the stream axis too
    assert [
        d["code"] for d in doc["diagnostics"] if not d["code"].startswith("EP-")
    ] == ["PF-OWNER-LEGACY-ROLE"]


def test_unknown_repository_owner_is_a_fleet_diagnostic() -> None:
    index = ManifestIndex(frozenset({"demo", "dispatcher"}), {})
    doc = parse_fleet(
        [RepoInput("demo", "- [ ] work @id:x @owner:repo:ghost\n")], index
    )
    validate_document(doc)
    assert "PF-OWNER-REPO-UNKNOWN" in {d["code"] for d in doc["diagnostics"]}
