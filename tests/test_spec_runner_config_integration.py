"""DESIGN-405 level 2: the write path against REAL git.

The gate this feature replaces existed because {"ok": true} stubs masked a
broken contract. Here the fake github-checker performs propose-pr's
observable contract with real git: branch off origin/<default> in a temp
worktree, apply the --edit content, commit, push to a real bare origin.
Level 3 (live smoke with the real binary) is at the bottom, skipif.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.spec_runner_config import TYPED_DEFAULTS
from dispatcher.core.spec_runner_config_actions import (
    ConfigCandidate,
    SpecRunnerConfigActionRunner,
)

_PROJECT_YAML = "project: alpha\nspec_runner:\n  max_retries: 3\nworkstreams: []\n"

_FAKE_PROPOSE_PR = '''\
#!/usr/bin/env python3
"""Fake github-checker honoring propose-pr's observable contract, real git."""
import json, subprocess, sys, tempfile
from pathlib import Path


def git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(json.dump(
            {"ok": False, "error": r.stderr.strip()}, sys.stdout) or 1)
    return r.stdout.strip()


def main():
    assert sys.argv[1] == "propose-pr"
    target = Path(sys.argv[2])
    args = sys.argv[3:]
    message, edits = None, []
    i = 0
    while i < len(args):
        if args[i] == "--message":
            message = args[i + 1]; i += 2
        elif args[i] == "--edit":
            edits.append(args[i + 1]); i += 2
        elif args[i] == "--if-match":
            i += 2  # verified real-side by github-checker's own tests
        else:
            i += 1
    git(target, "fetch", "--prune")
    branch = "propose/fake-test"
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td) / "wt"
        git(target, "worktree", "add", str(wt), "-b", branch, "origin/main")
        paths = []
        for e in edits:
            repo_path, content_file = e.split("=", 1)
            (wt / repo_path).write_bytes(Path(content_file).read_bytes())
            paths.append(repo_path)
        git(wt, "add", "--", *paths)
        git(wt, "commit", "-m", message)
        sha = git(wt, "rev-parse", "HEAD")
        git(wt, "push", "-u", "origin", branch)
        git(target, "worktree", "remove", "--force", str(wt))
    git(target, "branch", "-D", branch)
    json.dump({"ok": True, "detail": "pull request created",
               "pr_url": "https://example/pr/42", "branch": branch,
               "base_branch": "main", "commit_sha": sha,
               "changed_paths": paths}, sys.stdout)


main()
'''


def _git(path: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def _workspace_with_origin(tmp_path: Path) -> tuple[Path, Path]:
    """A real bare origin + a workspace clone containing project.yaml."""
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _git(origin, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@example.com")
    _git(seed, "config", "user.name", "t")
    (seed / "project.yaml").write_text(_PROJECT_YAML)
    _git(seed, "add", "project.yaml")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "-u", "origin", "main")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(workspace / "alpha")],
        check=True,
        capture_output=True,
    )
    clone = workspace / "alpha"
    _git(clone, "config", "user.email", "t@example.com")
    _git(clone, "config", "user.name", "t")
    return origin, workspace


def test_write_path_end_to_end_real_git(tmp_path: Path) -> None:
    origin, workspace = _workspace_with_origin(tmp_path)
    clone = workspace / "alpha"
    live_before = (clone / "project.yaml").read_bytes()
    script = tmp_path / "fake_propose_pr.py"
    script.write_text(_FAKE_PROPOSE_PR)

    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(workspace,)),
        command=("python3", str(script)),
    )
    candidate = ConfigCandidate(
        typed={**TYPED_DEFAULTS, "max_retries": 9},
        base_mtime=(clone / "project.yaml").stat().st_mtime,
    )
    outcome = runner.run("alpha", candidate)

    assert outcome.ok, outcome.error
    assert outcome.commit_sha
    # the edit landed as a real commit on a real branch in the bare origin
    blob = _git(origin, "show", f"{outcome.branch}:project.yaml")
    assert "max_retries: 9" in blob
    assert "workstreams: []" in blob  # rest of file survived the round-trip
    # implicit defaults were NOT materialized (DESIGN-402, end to end)
    assert "task_timeout_minutes" not in blob
    # origin default branch did not move
    # (the fake pushed only propose/fake-test)
    assert _git(origin, "rev-parse", "main") != outcome.commit_sha
    # the live workspace clone is byte-for-byte untouched
    assert (clone / "project.yaml").read_bytes() == live_before


@pytest.mark.skipif(
    shutil.which("github-checker") is None,
    reason="live smoke: real github-checker binary not on PATH",
)
def test_write_path_live_smoke_real_binary(tmp_path: Path, monkeypatch) -> None:
    """DESIGN-405 level 3: the REAL binary + a fake gh on PATH."""
    origin, workspace = _workspace_with_origin(tmp_path)
    clone = workspace / "alpha"
    live_before = (clone / "project.yaml").read_bytes()
    fake_gh_dir = tmp_path / "bin"
    fake_gh_dir.mkdir()
    gh = fake_gh_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        'case "$1 $2" in\n'
        '"pr create") echo "https://example/pr/7"; exit 0 ;;\n'
        '"pr view") exit 1 ;;\n'
        "*) exit 1 ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)
    import os

    monkeypatch.setenv("PATH", f"{fake_gh_dir}:{os.environ['PATH']}")

    runner = SpecRunnerConfigActionRunner(DispatcherConfig(roots=(workspace,)))
    candidate = ConfigCandidate(
        typed={**TYPED_DEFAULTS, "max_retries": 9},
        base_mtime=(clone / "project.yaml").stat().st_mtime,
    )
    outcome = runner.run("alpha", candidate)

    assert outcome.ok, outcome.error
    assert outcome.pr_url == "https://example/pr/7"
    blob = _git(origin, "show", f"{outcome.branch}:project.yaml")
    assert "max_retries: 9" in blob
    assert (clone / "project.yaml").read_bytes() == live_before
