"""Inventory capture — one-generation ledger and dags/ facts (Task 3).

Every test builds a real throwaway git repo (`tmp_path`): the HEAD-blob
pinning (spec §5.1 cond. 7) needs actual git, not a fake.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from dispatcher.core import inventory
from dispatcher.core.dag_subset import Accepted
from dispatcher.core.inventory import DagFileInfo, PlanItem, capture_inventory
from dispatcher.core.run_identity import RepoKey

_GIT_TIMEOUT = 15


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )


def make_repo(
    tmp_path: Path,
    todo: str,
    dags: dict[str, str],
    *,
    remote: str | None = "git@github.com:andrei-shtanakov/demo.git",
    name: str = "repo",
) -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q")
    if remote is not None:
        _git(root, "remote", "add", "origin", remote)
    (root / "TODO.md").write_text(todo)
    for rel, text in dags.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-qm",
        "init",
    )
    return root


def find_dag(surface, rel_path: str) -> DagFileInfo:
    return next(f for f in surface.dag_files if f.rel_path == rel_path)


def find_item(surface, item_id: str) -> PlanItem:
    return next(i for i in surface.plan_items if i.item_id == item_id)


# --- 1. registered open item + clean committed DAG ---------------------


def test_registered_open_item_and_clean_dag(tmp_path):
    root = make_repo(
        tmp_path,
        "- [ ] Do the thing @id:demo @dag:dags/demo.yaml\n",
        {"dags/demo.yaml": "repo: /home/user/labs/demo\ntasks: []\n"},
    )
    surface = capture_inventory(root)

    item = find_item(surface, "demo")
    assert item.dag_raw == "dags/demo.yaml"
    assert item.dag_tag == "dags/demo.yaml"
    assert item.dag_diag is None
    assert item.open is True
    assert item.shipped is False

    info = find_dag(surface, "dags/demo.yaml")
    assert info.is_regular is True
    assert info.error is None
    assert info.blob_sha is not None
    assert info.head_blob_sha is not None
    assert info.blob_sha == info.head_blob_sha


# --- 2. DAG edited after commit -----------------------------------------


def test_dag_edited_after_commit_diverges_from_head(tmp_path):
    root = make_repo(
        tmp_path,
        "- [ ] Do the thing @id:demo @dag:dags/demo.yaml\n",
        {"dags/demo.yaml": "repo: /home/user/labs/demo\ntasks: []\n"},
    )
    (root / "dags" / "demo.yaml").write_text(
        "repo: /home/user/labs/demo\ntasks: []\n# edited\n"
    )

    surface = capture_inventory(root)
    info = find_dag(surface, "dags/demo.yaml")

    assert info.blob_sha is not None
    assert info.head_blob_sha is not None
    assert info.blob_sha != info.head_blob_sha


# --- 3. symlink / FIFO / symlinked dags/ root ---------------------------


def test_symlinked_dag_file_is_refused(tmp_path):
    root = make_repo(tmp_path, "- [ ] x @id:x\n", {})
    (root / "dags").mkdir(parents=True, exist_ok=True)
    target = tmp_path / "outside.yaml"
    target.write_text("repo: /x\ntasks: []\n")
    os.symlink(target, root / "dags" / "linked.yaml")

    surface = capture_inventory(root)
    info = find_dag(surface, "dags/linked.yaml")

    assert info.is_regular is False
    assert info.text is None
    assert info.error is not None
    assert "linked.yaml" in info.error


def test_fifo_dag_file_does_not_hang_and_is_not_read(tmp_path):
    root = make_repo(tmp_path, "- [ ] x @id:x\n", {})
    fifo_path = root / "dags" / "pipe.yaml"
    fifo_path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(fifo_path)

    surface = capture_inventory(root)
    info = find_dag(surface, "dags/pipe.yaml")

    assert info.is_regular is False
    assert info.text is None


def test_symlinked_dags_root_is_refused_at_the_root(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "real.yaml").write_text("repo: /x\ntasks: []\n")

    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "remote", "add", "origin", "git@github.com:andrei-shtanakov/demo.git")
    (root / "TODO.md").write_text("- [ ] x @id:x\n")
    os.symlink(elsewhere, root / "dags")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")

    surface = capture_inventory(root)

    assert surface.dag_dir_error is not None
    assert surface.dag_files == ()


# --- 4. Shipped section tracking, nested sub-headings -------------------


def test_shipped_section_nested_subheading_still_shipped(tmp_path):
    todo = (
        "- [ ] Open thing @id:open1\n## Shipped\n### sub\n- [x] Done thing @id:done1\n"
    )
    root = make_repo(tmp_path, todo, {})
    surface = capture_inventory(root)

    open_item = find_item(surface, "open1")
    done_item = find_item(surface, "done1")

    assert open_item.shipped is False
    assert done_item.shipped is True


def test_shipped_region_ends_at_next_level2_heading(tmp_path):
    todo = (
        "## Shipped\n"
        "- [x] Done thing @id:done1\n"
        "## Active\n"
        "- [ ] Open thing @id:open1\n"
    )
    root = make_repo(tmp_path, todo, {})
    surface = capture_inventory(root)

    assert find_item(surface, "done1").shipped is True
    assert find_item(surface, "open1").shipped is False


# --- 5. malformed @dag ----------------------------------------------------


def test_malformed_dag_tag_yields_grammar_diagnostic(tmp_path):
    todo = "- [ ] Do the thing @id:demo @dag:not-a-valid-path\n"
    root = make_repo(tmp_path, todo, {})
    surface = capture_inventory(root)

    item = find_item(surface, "demo")
    assert item.dag_raw == "not-a-valid-path"
    assert item.dag_tag is None
    assert item.dag_diag == "PF-DAG-GRAMMAR"


# --- 6. TODO.md unreadable -------------------------------------------------


def test_unreadable_todo_sets_plan_error(tmp_path):
    root = make_repo(tmp_path, "- [ ] x @id:x\n", {})
    todo_path = root / "TODO.md"
    todo_path.chmod(0)
    try:
        surface = capture_inventory(root)
    finally:
        todo_path.chmod(0o644)

    assert surface.plan_error is not None
    assert surface.plan_items == ()


# --- 7. dags/ unreadable / absent -------------------------------------------


def test_unreadable_dags_dir_sets_dag_dir_error(tmp_path):
    root = make_repo(
        tmp_path, "- [ ] x @id:x\n", {"dags/demo.yaml": "repo: /x\ntasks: []\n"}
    )
    dags_path = root / "dags"
    dags_path.chmod(0)
    try:
        surface = capture_inventory(root)
    finally:
        dags_path.chmod(0o700)

    assert surface.dag_dir_error is not None


def test_absent_dags_dir_is_not_an_error(tmp_path):
    root = make_repo(tmp_path, "- [ ] x @id:x\n", {})
    surface = capture_inventory(root)

    assert surface.dag_dir_error is None
    assert surface.dag_files == ()


# --- 8. grammar filtering of candidate names --------------------------------


def test_orphan_grammar_valid_file_is_still_listed(tmp_path):
    root = make_repo(
        tmp_path, "- [ ] x @id:x\n", {"dags/orphan.yaml": "repo: /x\ntasks: []\n"}
    )
    surface = capture_inventory(root)

    rel_paths = {f.rel_path for f in surface.dag_files}
    assert "dags/orphan.yaml" in rel_paths


def test_non_matching_names_are_not_listed(tmp_path):
    root = make_repo(
        tmp_path,
        "- [ ] x @id:x\n",
        {
            "dags/readme.txt": "not a dag",
            "dags/Bad-Name.yaml": "repo: /x\ntasks: []\n",
            "dags/ok.yaml": "repo: /x\ntasks: []\n",
        },
    )
    surface = capture_inventory(root)

    rel_paths = {f.rel_path for f in surface.dag_files}
    assert rel_paths == {"dags/ok.yaml"}


# --- 9. untracked file / HEAD moved mid-capture -----------------------------


def test_untracked_dag_file_has_no_head_blob_and_no_error(tmp_path):
    root = make_repo(tmp_path, "- [ ] x @id:x\n", {})
    (root / "dags").mkdir(parents=True, exist_ok=True)
    (root / "dags" / "untracked.yaml").write_text("repo: /x\ntasks: []\n")

    surface = capture_inventory(root)
    info = find_dag(surface, "dags/untracked.yaml")

    assert info.head_blob_sha is None
    assert info.error is None


def test_head_moved_mid_capture_uses_captured_revision(tmp_path, monkeypatch):
    root = make_repo(
        tmp_path,
        "- [ ] x @id:x\n",
        {"dags/demo.yaml": "repo: /x\ntasks: []\n"},
    )
    original_head = _git(root, "rev-parse", "HEAD").stdout.strip()
    original_blob = (
        _git(root, "ls-tree", original_head, "--", "dags/demo.yaml")
        .stdout.strip()
        .split()[2]
    )

    original_git = inventory._git

    def spy(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        result = original_git(repo, *args)
        if args[:2] == ("rev-parse", "HEAD"):
            (root / "dags" / "demo.yaml").write_text(
                "repo: /x\ntasks: []\n# second commit\n"
            )
            _git(root, "add", "-A")
            _git(
                root,
                "-c",
                "user.email=t@t",
                "-c",
                "user.name=t",
                "commit",
                "-qm",
                "second",
            )
        return result

    monkeypatch.setattr(inventory, "_git", spy)

    surface = capture_inventory(root)

    assert surface.head_revision == original_head
    info = find_dag(surface, "dags/demo.yaml")
    assert info.head_blob_sha == original_blob


# --- 10. unborn HEAD --------------------------------------------------------


def test_unborn_head_sets_capture_error(tmp_path):
    root = tmp_path / "unborn"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "remote", "add", "origin", "git@github.com:andrei-shtanakov/demo.git")

    surface = capture_inventory(root)

    assert surface.capture_error is not None
    assert surface.head_revision is None


# --- 11. no origin remote ----------------------------------------------------


def test_no_origin_remote_sets_capture_error_and_no_repo_key(tmp_path):
    root = make_repo(tmp_path, "- [ ] x @id:x\n", {}, remote=None)
    surface = capture_inventory(root)

    assert surface.repo_key is None
    assert surface.capture_error is not None


# --- 12. named_repo resolution, submit parity --------------------------------


def test_named_repo_by_repo_path_matches_this_checkout(tmp_path):
    root = make_repo(tmp_path, "- [ ] x @id:x\n", {})
    (root / "dags").mkdir(parents=True, exist_ok=True)
    (root / "dags" / "demo.yaml").write_text(f"repo: {root}\ntasks: []\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "dag")

    surface = capture_inventory(root)
    info = find_dag(surface, "dags/demo.yaml")

    assert isinstance(info.subset, Accepted)
    assert info.named_repo is not None
    assert info.named_repo == surface.repo_key


def test_named_repo_by_repo_url_matches_this_checkout(tmp_path):
    root = make_repo(tmp_path, "- [ ] x @id:x\n", {})
    (root / "dags").mkdir(parents=True, exist_ok=True)
    (root / "dags" / "demo.yaml").write_text(
        "repo_url: git@github.com:andrei-shtanakov/demo.git\ntasks: []\n"
    )
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "dag")

    surface = capture_inventory(root)
    info = find_dag(surface, "dags/demo.yaml")

    assert info.named_repo == surface.repo_key


def test_named_repo_by_repo_path_pointing_elsewhere(tmp_path):
    other = make_repo(
        tmp_path,
        "- [ ] y @id:y\n",
        {},
        remote="git@github.com:andrei-shtanakov/other.git",
        name="other",
    )
    root = make_repo(tmp_path, "- [ ] x @id:x\n", {})
    (root / "dags").mkdir(parents=True, exist_ok=True)
    (root / "dags" / "demo.yaml").write_text(f"repo: {other}\ntasks: []\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "dag")

    surface = capture_inventory(root)
    info = find_dag(surface, "dags/demo.yaml")

    assert info.named_repo is not None
    assert info.named_repo != surface.repo_key
    assert info.named_repo == RepoKey(
        host="github.com", owner="andrei-shtanakov", repo="other"
    )


def test_named_repo_unresolvable_path_sets_error(tmp_path):
    root = make_repo(tmp_path, "- [ ] x @id:x\n", {})
    (root / "dags").mkdir(parents=True, exist_ok=True)
    (root / "dags" / "demo.yaml").write_text(
        "repo: /this/path/does/not/exist/as/a/git/repo\ntasks: []\n"
    )
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "dag")

    surface = capture_inventory(root)
    info = find_dag(surface, "dags/demo.yaml")

    assert info.named_repo is None
    assert info.named_repo_error is not None
