"""The advisory drift reporter for github-checker-actions/v1 (guarantee B).

Structural difference from the plan-fields sibling
(`tests/test_upstream_drift_report.py`): upstream publishes no manifest of
its own, so the reporter recomputes upstream's tree hash with
`vendor_manifest.build_manifest` and compares it against the `tree_sha256`
already sitting in the vendored `manifest.json` — never against a manifest
upstream does not have. That makes the exclusion list
(`vendor_manifest.EXCLUDED_NAMES`) a decision this reporter makes about
upstream's tree, which is exactly the case this file pins down: an upstream
file that happens to be named `manifest.json` or `PINNED.txt` must not
silently vanish from the comparison.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from actions_drift_report import DRIFT, NO_DRIFT, UNAVAILABLE, compare

_FILES = {
    "actions.schema.json": '{"type":"object"}',
    "README.md": "# actions\n",
    "fixtures/ex.json": '{"a":1}',
}
_PROVENANCE = {
    "commit": "ef03fefcded37676b19ef1c6f88b956a09a26d3f",
    "remote": "https://github.com/andrei-shtanakov/github-checker",
    "ref": "master",
}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _tree_hash(files: dict[str, str]) -> str:
    """Reproduces `vendor_manifest.build_manifest`'s tree hash independently,
    so these tests do not lean on the module under test to grade itself."""
    entries = sorted((rel, _sha(files[rel])) for rel in files)
    digest = "".join(f"{path}:{sha}\n" for path, sha in entries)
    return hashlib.sha256(digest.encode()).hexdigest()


def _manifest(
    files: dict[str, str], producer_commit: str = _PROVENANCE["commit"]
) -> dict:
    surface = [{"path": rel, "sha256": _sha(files[rel])} for rel in sorted(files)]
    return {
        "contract": "github-checker-actions",
        "contract_version": 1,
        "producer_commit": producer_commit,
        "tree_sha256": _tree_hash(files),
        "surface": surface,
    }


def _write_files(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


def _write_upstream(tmp_path: Path, files: dict[str, str]) -> Path:
    return _write_files(tmp_path / "upstream", files)


_DERIVE = object()  # "generate a matching manifest", distinct from "write none"


def _write_vendored(
    tmp_path: Path, files: dict[str, str], *, manifest: object = _DERIVE
) -> Path:
    """A vendored copy carrying only what `compare()` actually reads: its
    manifest. `manifest=None` omits the file entirely (unreadable); the
    default derives one from `files` so callers get a matching pair."""
    root = tmp_path / "vendored"
    root.mkdir(parents=True, exist_ok=True)
    body = _manifest(files) if manifest is _DERIVE else manifest
    if body is not None:
        (root / "manifest.json").write_text(json.dumps(body))
    return root


def _both(tmp_path: Path, upstream: dict[str, str], vendored: dict[str, str]):
    return _write_upstream(tmp_path, upstream), _write_vendored(tmp_path, vendored)


def test_matching_upstream_reports_no_drift(tmp_path: Path) -> None:
    upstream, vendored = _both(tmp_path, _FILES, _FILES)
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == NO_DRIFT
    assert result.exit_code == 0


def test_a_changed_upstream_file_is_drift_and_names_it(tmp_path: Path) -> None:
    upstream, vendored = _both(tmp_path, {**_FILES, "README.md": "# changed\n"}, _FILES)
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == DRIFT
    assert result.exit_code == 1
    assert "README.md" in result.summary


def test_an_added_upstream_file_is_drift(tmp_path: Path) -> None:
    upstream, vendored = _both(tmp_path, {**_FILES, "extra.json": "{}"}, _FILES)
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == DRIFT
    assert result.exit_code == 1
    assert "only upstream" in result.summary
    assert "extra.json" in result.summary


def test_a_removed_upstream_file_is_drift(tmp_path: Path) -> None:
    """The vendored copy has a fixture upstream no longer ships."""
    upstream, vendored = _both(tmp_path, _FILES, {**_FILES, "fixtures/gone.json": "{}"})
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == DRIFT
    assert result.exit_code == 1
    assert "only in the vendored copy" in result.summary
    assert "fixtures/gone.json" in result.summary


def test_a_missing_upstream_directory_is_unavailable_not_no_drift(
    tmp_path: Path,
) -> None:
    vendored = _write_vendored(tmp_path, _FILES)
    result = compare(tmp_path / "nowhere", vendored, _PROVENANCE)
    assert result.outcome == UNAVAILABLE
    assert result.exit_code == 2


def test_an_upstream_missing_the_probe_file_is_unavailable(tmp_path: Path) -> None:
    """A directory that exists but is not really the contract must not be
    hashed as an (empty-ish) tree and reported as drift."""
    upstream = _write_files(tmp_path / "upstream", {"README.md": "# x\n"})
    vendored = _write_vendored(tmp_path, _FILES)
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == UNAVAILABLE
    assert result.exit_code == 2


def test_a_missing_vendored_manifest_is_unavailable(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path, _FILES)
    vendored = _write_vendored(tmp_path, _FILES, manifest=None)
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == UNAVAILABLE
    assert result.exit_code == 2


def test_an_unreadable_vendored_manifest_is_unavailable(tmp_path: Path) -> None:
    upstream = _write_upstream(tmp_path, _FILES)
    vendored = _write_vendored(tmp_path, _FILES)
    (vendored / "manifest.json").write_text("{not json")
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == UNAVAILABLE
    assert result.exit_code == 2


def test_a_vendored_manifest_missing_required_keys_is_unavailable(
    tmp_path: Path,
) -> None:
    upstream = _write_upstream(tmp_path, _FILES)
    vendored = _write_vendored(tmp_path, _FILES, manifest={"surface": []})
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == UNAVAILABLE
    assert result.exit_code == 2


def test_a_vendored_manifest_with_malshaped_surface_entries_is_unavailable(
    tmp_path: Path,
) -> None:
    upstream = _write_upstream(tmp_path, _FILES)
    manifest = _manifest(_FILES)
    manifest["surface"] = ["not-a-dict"]
    vendored = _write_vendored(tmp_path, _FILES, manifest=manifest)
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == UNAVAILABLE
    assert result.exit_code == 2


def test_an_upstream_file_named_manifest_json_is_drift_not_silence(
    tmp_path: Path,
) -> None:
    """The case the whole module docstring is about: upstream ships a file
    whose name collides with our exclusion list, so a naive recompute would
    drop it and could report "no drift" about a tree that gained a file."""
    upstream, vendored = _both(
        tmp_path, {**_FILES, "manifest.json": '{"unexpected":true}'}, _FILES
    )
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == DRIFT
    assert result.exit_code == 1
    assert "manifest.json" in result.summary


def test_an_upstream_file_named_pinned_txt_is_also_drift(tmp_path: Path) -> None:
    upstream, vendored = _both(tmp_path, {**_FILES, "PINNED.txt": "surprise\n"}, _FILES)
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == DRIFT
    assert "PINNED.txt" in result.summary


def test_a_nested_collision_file_is_still_caught(tmp_path: Path) -> None:
    """`vendor_manifest.build_manifest` filters by filename at any depth
    (`p.name not in EXCLUDED_NAMES`, not just top-level) — the reporter's own
    collision check has to match that, not just check the top level."""
    upstream, vendored = _both(
        tmp_path, {**_FILES, "fixtures/nested/manifest.json": "{}"}, _FILES
    )
    result = compare(upstream, vendored, _PROVENANCE)
    assert result.outcome == DRIFT
    assert "fixtures/nested/manifest.json" in result.summary


def test_the_summary_records_which_upstream_was_read(tmp_path: Path) -> None:
    upstream, vendored = _both(tmp_path, _FILES, _FILES)
    result = compare(upstream, vendored, _PROVENANCE)
    assert _PROVENANCE["commit"] in result.summary
    assert _PROVENANCE["remote"] in result.summary
    assert _PROVENANCE["ref"] in result.summary
    assert _tree_hash(_FILES) in result.summary  # recomputed, not copied


def test_the_summary_records_the_pinned_producer_commit(tmp_path: Path) -> None:
    upstream, vendored = _both(tmp_path, _FILES, _FILES)
    result = compare(upstream, vendored, _PROVENANCE)
    assert _PROVENANCE["commit"] in result.summary
    assert result.vendored_pin == _PROVENANCE["commit"]


def test_the_summary_never_tells_the_reader_to_edit_the_hash(
    tmp_path: Path,
) -> None:
    upstream, vendored = _both(tmp_path, {**_FILES, "README.md": "# changed\n"}, _FILES)
    summary = compare(upstream, vendored, _PROVENANCE).summary.lower()
    assert "re-vendor" in summary
    assert "update the expected" not in summary


# ----------------------------------------------------------------------------
# The commit-log section: a different question from the hash comparison
# ----------------------------------------------------------------------------


def _git_repo(root: Path) -> Path:
    subprocess.run(["git", "init", "--quiet", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "t@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    return root


def _commit(root: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "--quiet", "-m", message], check=True
    )
    out = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return out.stdout.strip()


def test_commits_since_pin_lists_commits_touching_the_subdir(
    tmp_path: Path,
) -> None:
    from actions_drift_report import _commits_since_pin

    repo = _git_repo(tmp_path / "repo")
    (repo / "contracts" / "actions" / "v1").mkdir(parents=True)
    (repo / "contracts" / "actions" / "v1" / "README.md").write_text("v1\n")
    pin = _commit(repo, "initial")
    (repo / "contracts" / "actions" / "v1" / "README.md").write_text("v2\n")
    resolved = _commit(repo, "touch actions/v1")

    section = _commits_since_pin(repo, pin, resolved, "contracts/actions/v1")
    assert "Commits since the pin" in section
    assert "touch actions/v1" in section


def test_commits_since_pin_says_so_when_pin_absent_from_the_checkout(
    tmp_path: Path,
) -> None:
    """A shallow clone (or a pin that predates this checkout's history) must
    say plainly that it cannot answer — never guess, never go quiet."""
    from actions_drift_report import _commits_since_pin

    repo = _git_repo(tmp_path / "repo")
    (repo / "contracts" / "actions" / "v1").mkdir(parents=True)
    (repo / "contracts" / "actions" / "v1" / "README.md").write_text("v1\n")
    resolved = _commit(repo, "initial")

    bogus_pin = "0" * 40
    section = _commits_since_pin(repo, bogus_pin, resolved, "contracts/actions/v1")
    assert "not present in this checkout" in section


def test_commits_since_pin_with_no_commits_says_so(tmp_path: Path) -> None:
    from actions_drift_report import _commits_since_pin

    repo = _git_repo(tmp_path / "repo")
    (repo / "contracts" / "actions" / "v1").mkdir(parents=True)
    (repo / "contracts" / "actions" / "v1" / "README.md").write_text("v1\n")
    pin = _commit(repo, "initial")

    section = _commits_since_pin(repo, pin, pin, "contracts/actions/v1")
    assert "No commits touched" in section


def test_commits_since_pin_with_unknown_pin_says_so(tmp_path: Path) -> None:
    from actions_drift_report import _commits_since_pin

    section = _commits_since_pin(tmp_path, "?", "?", "contracts/actions/v1")
    assert "unknown" in section


def test_main_appends_the_commit_log_only_when_upstream_root_is_given(
    tmp_path: Path, capsys
) -> None:
    from actions_drift_report import main

    repo = _git_repo(tmp_path / "repo")
    contract_dir = repo / "contracts" / "actions" / "v1"
    contract_dir.mkdir(parents=True)
    for rel, body in _FILES.items():
        p = contract_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    resolved = _commit(repo, "initial")

    vendored = _write_vendored(
        tmp_path, _FILES, manifest=_manifest(_FILES, producer_commit=resolved)
    )

    code = main([str(contract_dir), "--vendored", str(vendored)])
    assert code == 0  # no drift: upstream matches the pinned vendored copy
    assert "Commits since the pin" not in capsys.readouterr().out

    code = main(
        [
            str(contract_dir),
            "--vendored",
            str(vendored),
            "--upstream-root",
            str(repo),
            "--ref",
            "master",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Commits since the pin" in out
    assert "No commits touched" in out
