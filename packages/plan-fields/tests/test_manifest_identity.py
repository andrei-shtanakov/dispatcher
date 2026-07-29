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
