"""Contract cases for manifest-declared identity (ADR-ECO-005 / locator alias).

`git_dir` is a locator, never an identity. These pin that distinction where it
is decided, so a future refactor cannot quietly reintroduce origin-derived
identity — the failure mode the contract exists to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from plan_fields.fleet import (
    ManifestIndex,
    checkout_map,
    manifest_index,
    manifest_repos,
    resolve_checkout,
    scan_workspace,
)

VAULT_MANIFEST = """
[tools.ecosystem-kb]
package_name = "ecosystem-kb"
git_dir      = "prograph-vault"

[apps.arbiter]
package_name = "arbiter"
git_dir      = "arbiter"

[cores.atp-platform]
package_name = "atp-platform"
git_dir      = "atp-platform"

[cores.atp-platform-sdk]
package_name = "atp-platform-sdk"
git_dir      = "atp-platform"
member       = true
"""


def _manifest(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "workspace-manifest.toml"
    p.write_text(text, encoding="utf-8")
    return p


def _checkout(root: Path, name: str, origin: str, todo: str | None = None) -> Path:
    """A directory that looks like a git checkout to canonical_name()."""
    d = root / name
    (d / ".git").mkdir(parents=True)
    (d / ".git" / "config").write_text(
        f'[remote "origin"]\n\turl = git@github.com:owner/{origin}.git\n',
        encoding="utf-8",
    )
    if todo is not None:
        (d / "TODO.md").write_text(todo, encoding="utf-8")
    return d


# --- case 1: the vault checkout scans as its key -----------------------------
def test_vault_checkout_resolves_to_its_manifest_key(tmp_path: Path) -> None:
    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    d = _checkout(tmp_path, "prograph-vault", "prograph-vault")
    assert resolve_checkout(d, idx) == "ecosystem-kb"


# --- case 2: the key wins over package_name and git_dir ----------------------
def test_canonical_name_is_the_key_not_package_name(tmp_path: Path) -> None:
    text = """
[tools.canon-key]
package_name = "different-package"
git_dir      = "third-name"
"""
    path = _manifest(tmp_path, text)
    assert manifest_repos(path) == {"canon-key"}
    idx = manifest_index(path)
    assert isinstance(idx, ManifestIndex)
    assert idx.canonical_keys == frozenset({"canon-key"})
    assert idx.git_dir_to_key["third-name"] == "canon-key"


# --- case 3: a member mints no identity and collides with nothing ------------
def test_member_entry_contributes_no_identity(tmp_path: Path) -> None:
    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    assert "atp-platform-sdk" not in idx.canonical_keys
    d = _checkout(tmp_path, "atp-platform", "atp-platform")
    # The member shares atp-platform's git_dir; it must neither win the alias
    # nor make the manifest look ambiguous.
    assert resolve_checkout(d, idx) == "atp-platform"


# --- case 6: two non-member entries on one git_dir is a loud error -----------
def test_ambiguous_alias_raises_naming_both_keys(tmp_path: Path) -> None:
    text = """
[apps.one]
package_name = "one"
git_dir      = "shared"

[apps.two]
package_name = "two"
git_dir      = "shared"
"""
    with pytest.raises(ValueError) as excinfo:
        manifest_index(_manifest(tmp_path, text))
    message = str(excinfo.value)
    assert "one" in message and "two" in message and "shared" in message


# --- case 7: a checkout absent from the manifest falls back ------------------
def test_checkout_absent_from_manifest_falls_back_to_origin(tmp_path: Path) -> None:
    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    d = _checkout(tmp_path, "some-scratch-dir", "unlisted-repo")
    assert resolve_checkout(d, idx) == "unlisted-repo"


# --- case 8: directory name differs from git_dir -----------------------------
def test_directory_named_differently_resolves_via_origin(tmp_path: Path) -> None:
    """Lookup step 2: the folder is arbitrary, the origin still names the locator."""
    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    d = _checkout(tmp_path, "vault-checkout", "prograph-vault")
    assert resolve_checkout(d, idx) == "ecosystem-kb"


def test_scan_workspace_keys_by_manifest_when_given_an_index(tmp_path: Path) -> None:
    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    _checkout(tmp_path, "prograph-vault", "prograph-vault", todo="- [ ] a @id:x\n")
    fleet = scan_workspace(tmp_path, idx)
    assert set(fleet) == {"ecosystem-kb"}


def test_scan_workspace_without_an_index_keeps_the_old_behaviour(
    tmp_path: Path,
) -> None:
    """The index is optional so existing callers keep working until Task 4."""
    _checkout(tmp_path, "prograph-vault", "prograph-vault", todo="- [ ] a @id:x\n")
    fleet = scan_workspace(tmp_path)
    assert set(fleet) == {"prograph-vault"}


# --- resolve_checkout: one predicate per candidate ---------------------------
def test_basename_equal_to_a_key_resolves_despite_a_different_git_dir(
    tmp_path: Path,
) -> None:
    """The asymmetry `resolve_ref` had and `resolve_checkout` did not.

    `[tools.ecosystem-kb]` declares `git_dir = "prograph-vault"`, so a folder
    named exactly like the key matched no alias and — with an origin that names
    neither spelling — fell through to the origin fallback. A declared repo was
    reported as an undeclared checkout.
    """
    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    d = _checkout(tmp_path, "ecosystem-kb", "some-fork")
    assert resolve_checkout(d, idx) == "ecosystem-kb"


def test_origin_derived_name_equal_to_a_key_resolves_as_declared(
    tmp_path: Path,
) -> None:
    """Candidate 2 is subject to the same predicate as candidate 1."""
    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    d = _checkout(tmp_path, "scratch-clone", "ecosystem-kb")
    assert resolve_checkout(d, idx) == "ecosystem-kb"


def test_unknown_basename_and_origin_never_becomes_canonical(tmp_path: Path) -> None:
    """The fallback is provenance, not identity, and must not mint a key.

    Nothing indexes `repo_url`, deliberately: a checkout whose folder and origin
    match neither the key nor the declared `git_dir` degrades VISIBLY to an
    undeclared checkout instead of borrowing an identity it was not given.
    """
    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    d = _checkout(tmp_path, "some-scratch-dir", "unlisted-repo")
    key = resolve_checkout(d, idx)
    assert key == "unlisted-repo"
    assert key not in idx.canonical_keys


def test_declared_git_dir_still_resolves_after_the_rewrite(tmp_path: Path) -> None:
    """Case 1's resolution is the one the rewrite must not cost."""
    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    d = _checkout(tmp_path, "prograph-vault", "prograph-vault")
    assert resolve_checkout(d, idx) == "ecosystem-kb"


# --- manifest_repos: the member exclusion, pinned at the public function -----
def test_manifest_repos_excludes_member_entries(tmp_path: Path) -> None:
    """A member is a package inside another repo — it is never a fleet repo.

    Pinned here and not only on `ManifestIndex`, because this is the function
    the `fleet-graph` snapshot's node list is built from: `atp-platform-sdk`
    leaving the set is a real change of answer, not a no-op.
    """
    repos = manifest_repos(_manifest(tmp_path, VAULT_MANIFEST))
    assert repos == {"ecosystem-kb", "arbiter", "atp-platform"}
    assert "atp-platform-sdk" not in repos


# --- scan_workspace: a collision is loud, never last-one-wins ---------------
def test_two_checkouts_resolving_to_one_key_raise(tmp_path: Path) -> None:
    """Identity must not be decided by `sorted()` over directory names."""
    _checkout(tmp_path, "prograph-vault", "prograph-vault", todo="- [ ] a\n")
    _checkout(tmp_path, "vault-copy", "ecosystem-kb", todo="- [ ] b\n")
    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    with pytest.raises(ValueError) as excinfo:
        scan_workspace(tmp_path, idx)
    message = str(excinfo.value)
    assert "ecosystem-kb" in message
    assert "prograph-vault" in message and "vault-copy" in message


def test_checkout_map_collides_without_an_index_too(tmp_path: Path) -> None:
    """The defect is the ambiguity, not the index: two clones of one origin."""
    _checkout(tmp_path, "one", "same-origin")
    _checkout(tmp_path, "two", "same-origin")
    with pytest.raises(ValueError):
        checkout_map(tmp_path)


# --- case 4: a legacy ref written with the locator resolves, and stays legacy -
def test_legacy_ref_by_alias_normalises_target_and_makes_no_edge(
    tmp_path: Path,
) -> None:
    """The alias resolves for lookup only.

    The contract's load-bearing split: edges come from identity, references
    come from text. A legacy `<repo>#<slug>` must never cross that line, however
    cleanly it resolves.
    """
    from plan_fields.fleet_api import RepoInput, check_legacy_fleet, parse_fleet

    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    inputs = [
        RepoInput("arbiter", todo_text="- [ ] a @blocked_by:prograph-vault#x\n"),
        RepoInput("ecosystem-kb", todo_text=None),  # cloned, keeps no TODO.md
    ]

    diags = check_legacy_fleet(inputs, idx)
    assert len(diags) == 1
    d = diags[0]
    assert d.code == "PF-BLOCKER-NO-TODO"
    assert d.target_repo == "ecosystem-kb"
    assert "prograph-vault#x" in d.raw_ref

    snapshot = parse_fleet(inputs, idx)
    assert snapshot["edges"] == []


def test_alias_spelled_legacy_ref_normalises_but_never_becomes_an_edge(
    tmp_path: Path,
) -> None:
    """The same rule where it costs something: the target IS present.

    The reference names a real, scanned repo once normalised, and still earns no
    `resolved_target` and no edge — `legacy_blocker_ref` carries the key so the
    repo is identifiable, `raw_ref` keeps the spelling the author wrote.
    """
    from plan_fields.fleet_api import RepoInput, parse_fleet

    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    inputs = [
        RepoInput("arbiter", "- [ ] a @blocked_by:prograph-vault#thing @id:a\n"),
        RepoInput("ecosystem-kb", "- [ ] the thing @id:thing\n"),
    ]
    snapshot = parse_fleet(inputs, idx)
    ref = next(r for r in snapshot["references"] if r["kind"] == "blocked_by")
    assert ref["raw_ref"] == "prograph-vault#thing"
    assert ref["legacy_blocker_ref"] == "ecosystem-kb#thing"
    assert ref["resolved_target"] is None  # required by the schema, never omitted
    assert snapshot["edges"] == []

    # The contrast that makes the rule visible: spelled with the key, the same
    # legacy reference still resolves transitionally. The alias spelling is the
    # one that buys nothing in the identity plane.
    keyed = parse_fleet(
        [
            RepoInput("arbiter", "- [ ] a @blocked_by:ecosystem-kb#thing @id:a\n"),
            RepoInput("ecosystem-kb", "- [ ] the thing @id:thing\n"),
        ],
        idx,
    )
    assert keyed["edges"] != []


# --- case 5: only the key is a canonical URI ---------------------------------
def test_canonical_uri_uses_the_key_only(tmp_path: Path) -> None:
    from plan_fields.fleet_api import RepoInput, parse_fleet

    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    inputs = [RepoInput("ecosystem-kb", todo_text="- [ ] a @id:thing\n")]
    snapshot = parse_fleet(inputs, idx)
    ids = [n["node_id"] for n in snapshot["nodes"]]
    assert ids == ["todo://ecosystem-kb/thing"]
    assert not any("prograph-vault" in i for i in ids)


# --- case 9a: git_dir moves, the key does not, identity holds ----------------
def test_renaming_git_dir_leaves_node_identity_untouched(tmp_path: Path) -> None:
    before = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    renamed = VAULT_MANIFEST.replace(
        'git_dir      = "prograph-vault"', 'git_dir      = "vault-new-name"'
    )
    after = manifest_index(_manifest(tmp_path, renamed))
    assert before.canonical_keys == after.canonical_keys
    d = _checkout(tmp_path, "vault-new-name", "vault-new-name")
    assert resolve_checkout(d, after) == "ecosystem-kb"


# --- case 9b: a retired spelling stops resolving, and that is correct --------
def test_retired_locator_no_longer_resolves(tmp_path: Path) -> None:
    """The manifest holds the current git_dir, not a history of former ones.

    So an old spelling resolves exactly as long as it is still declared. This
    asserts the boundary rather than pretending the alias is permanent.
    """
    renamed = VAULT_MANIFEST.replace(
        'git_dir      = "prograph-vault"', 'git_dir      = "vault-new-name"'
    )
    idx = manifest_index(_manifest(tmp_path, renamed))
    assert idx.resolve_ref("prograph-vault") == "prograph-vault"
    assert idx.resolve_ref("vault-new-name") == "ecosystem-kb"


def test_canonical_uri_spelled_with_the_locator_resolves_to_the_key(
    tmp_path: Path,
) -> None:
    """A canonical `todo://` written with the locator names the same node.

    The identity plane accepts it because the URI's repo component IS a repo
    name and the manifest says which repo that name means. Left unnormalised it
    would produce a `PF-ID-DANGLING` against a node_id that can never exist.
    """
    from plan_fields.fleet_api import RepoInput, parse_fleet

    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    snapshot = parse_fleet(
        [
            RepoInput(
                "arbiter", "- [ ] a @blocked_by:todo://prograph-vault/thing @id:a\n"
            ),
            RepoInput("ecosystem-kb", "- [ ] the thing @id:thing\n"),
        ],
        idx,
    )
    assert [
        d["code"] for d in snapshot["diagnostics"] if d["code"] == "PF-ID-DANGLING"
    ] == []
    assert [(e["source_node_id"], e["target_node_id"]) for e in snapshot["edges"]] == [
        ("todo://arbiter/a", "todo://ecosystem-kb/thing")
    ]
    ref = next(r for r in snapshot["references"] if r["kind"] == "blocked_by")
    assert ref["raw_ref"] == "todo://prograph-vault/thing"
    assert ref["resolved_target"] == "todo://ecosystem-kb/thing"


# --- an input's OWN name is normalised on the same rule ----------------------
def test_repo_input_supplied_under_its_locator_mints_canonical_identity(
    tmp_path: Path,
) -> None:
    """The other end of the same rule (Copilot review, PR #87).

    Normalising only the names *references* are written with leaves a hole: a
    caller freezing inputs from disk without the index supplies the repo under
    its `git_dir` spelling, and then `todo://prograph-vault/<id>` is minted and
    `present` keyed by that spelling — so an inbound reference, correctly
    normalised to `ecosystem-kb`, resolves to nothing.
    """
    from plan_fields.fleet_api import RepoInput, parse_fleet

    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    snapshot = parse_fleet(
        [
            RepoInput("prograph-vault", "- [ ] the thing @id:thing\n"),
            RepoInput(
                "arbiter", "- [ ] a @blocked_by:todo://ecosystem-kb/thing @id:a\n"
            ),
        ],
        idx,
    )
    assert sorted(n["node_id"] for n in snapshot["nodes"]) == [
        "todo://arbiter/a",
        "todo://ecosystem-kb/thing",
    ]
    assert [(e["source_node_id"], e["target_node_id"]) for e in snapshot["edges"]] == [
        ("todo://arbiter/a", "todo://ecosystem-kb/thing")
    ]
    assert not [
        d for d in snapshot["diagnostics"] if d["code"].startswith("PF-BLOCKER")
    ]


def test_two_repo_inputs_naming_one_repo_are_a_loud_duplicate(tmp_path: Path) -> None:
    """Normalising input names makes the existing duplicate check see through
    the spelling — two names for one repo collide here rather than silently
    overwriting `present` later."""
    from plan_fields.fleet_api import RepoInput, parse_fleet

    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    with pytest.raises(ValueError) as excinfo:
        parse_fleet(
            [
                RepoInput("prograph-vault", "- [ ] a @id:a\n"),
                RepoInput("ecosystem-kb", "- [ ] b @id:b\n"),
            ],
            idx,
        )
    assert "ecosystem-kb" in str(excinfo.value)


def test_legacy_source_repo_is_the_key_not_the_spelling(tmp_path: Path) -> None:
    """`check_legacy_fleet` keys its scrape by the canonical name too."""
    from plan_fields.fleet_api import RepoInput, check_legacy_fleet

    idx = manifest_index(_manifest(tmp_path, VAULT_MANIFEST))
    diags = check_legacy_fleet(
        [
            RepoInput("prograph-vault", "- [ ] a @blocked_by:arbiter#gone\n"),
            RepoInput("arbiter", "- [ ] unrelated\n"),
        ],
        idx,
    )
    assert [(d.code, d.source_repo) for d in diags] == [
        ("PF-BLOCKER-DANGLING", "ecosystem-kb")
    ]
