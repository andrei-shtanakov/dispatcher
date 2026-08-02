"""The re-vendor script, exercised offline.

Everything here runs through `--from` against a purpose-built git repository
in tmp_path, inside a copy of the minimal dispatcher layout the script needs.
That copy is why the script has no `--destination` flag: a test-only way to
redirect where the vendored copy lands would be a production-visible way to
overwrite the wrong directory, and the guarantee this script exists to make
is about not damaging the working copy.

The network path is deliberately untested — testing it would mean either a
network call in CI or a mock of the one step whose whole value is that it
really talks to the canonical remote.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SCRIPT_NAME = "revendor_github_checker_actions.sh"
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    """Run one git command in `repo`, with identity supplied by env.

    Identity from the environment, not `git config`: the test must not
    depend on — or write to — whatever global git config the machine has.
    """
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_GIT_ENV},
    )
    return result.stdout.strip()


@pytest.fixture
def producer(tmp_path: Path) -> dict[str, object]:
    """A miniature github-checker: two commits over contracts/actions/v1.

    The second drops a fixture the first had. A file that upstream deleted
    is the case a copy-over-the-top re-vendor gets wrong, so the fixture
    exists to make that case reachable.
    """
    repo = tmp_path / "producer"
    (repo / "contracts" / "actions" / "v1" / "fixtures").mkdir(parents=True)
    _git(repo.parent, "init", "--quiet", str(repo))

    root = repo / "contracts" / "actions" / "v1"
    (root / "README.md").write_text("first\n")
    (root / "actions.schema.json").write_text('{"first": true}\n')
    (root / "fixtures" / "kept.json").write_text('{"kept": 1}\n')
    (root / "fixtures" / "dropped.json").write_text('{"dropped": 1}\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "first")
    first = _git(repo, "rev-parse", "HEAD")

    (root / "README.md").write_text("second\n")
    (root / "fixtures" / "dropped.json").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "second")
    second = _git(repo, "rev-parse", "HEAD")

    return {"path": repo, "first": first, "second": second}


@pytest.fixture
def skeleton(tmp_path: Path) -> Path:
    """The smallest dispatcher layout the script needs, with a sentinel.

    The sentinel file is what proves "the working copy was not touched":
    asserting only that the directory still exists would pass even if the
    script had replaced it with a half-built candidate.
    """
    repo = tmp_path / "dispatcher"
    (repo / "scripts").mkdir(parents=True)
    vendored = repo / "contracts" / "github-checker-actions" / "v1"
    vendored.mkdir(parents=True)
    for name in (SCRIPT_NAME, "vendor_manifest.py"):
        shutil.copy2(REPO_ROOT / "scripts" / name, repo / "scripts" / name)
    (vendored / "README.md").write_text("SENTINEL\n")
    (vendored / "PINNED.txt").write_text("commit: old\n")
    return repo


def _run(skeleton: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(skeleton / "scripts" / SCRIPT_NAME), *args],
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV},
    )


def _vendored(skeleton: Path) -> Path:
    return skeleton / "contracts" / "github-checker-actions" / "v1"


def _assert_untouched(skeleton: Path) -> None:
    vendored = _vendored(skeleton)
    assert (vendored / "README.md").read_text() == "SENTINEL\n"
    assert (vendored / "PINNED.txt").read_text() == "commit: old\n"
    assert not (vendored.parent / "v1.staging").exists()
    assert not (vendored.parent / "v1.prev").exists()


def test_no_argument_is_a_usage_error(skeleton: Path) -> None:
    result = _run(skeleton)
    assert result.returncode == 1
    _assert_untouched(skeleton)


def test_an_abbreviated_sha_is_refused(
    skeleton: Path, producer: dict[str, object]
) -> None:
    """A 12-char prefix resolves fine in git and would be written into the
    manifest as if it identified a commit forever. Ambiguity is cheap to
    refuse here and expensive to discover later."""
    result = _run(
        skeleton, str(producer["first"])[:12], "--from", str(producer["path"])
    )
    assert result.returncode == 1
    _assert_untouched(skeleton)


def test_an_unknown_commit_leaves_the_working_copy_alone(
    skeleton: Path, producer: dict[str, object]
) -> None:
    """Pins the explicit `cat-file -e ...^{commit}` guard specifically, by
    its message: an unknown commit would also die 2 later, from `git
    archive` failing during extraction, but with a different message — so
    asserting on the text is what proves *this* guard fired, not the other
    one that happens to share its exit code."""
    result = _run(skeleton, "0" * 40, "--from", str(producer["path"]))
    assert result.returncode == 2
    assert "is not a commit in" in result.stderr
    _assert_untouched(skeleton)


def test_a_from_path_that_is_not_a_repository_is_refused(
    skeleton: Path, producer: dict[str, object], tmp_path: Path
) -> None:
    """Pins the explicit `rev-parse --git-dir` guard specifically, by its
    message: a non-repository path would also die 2 later, from the
    `cat-file -e` commit check failing against a non-repo, but with a
    different message."""
    (tmp_path / "not-a-repo").mkdir()
    result = _run(
        skeleton, str(producer["second"]), "--from", str(tmp_path / "not-a-repo")
    )
    assert result.returncode == 2
    assert "is not a git repository" in result.stderr
    _assert_untouched(skeleton)


def test_it_vendors_the_named_commit_even_when_the_source_tree_is_dirty(
    skeleton: Path, producer: dict[str, object]
) -> None:
    """Extraction reads the object database, so an uncommitted edit in the
    source is invisible to it. This is why the script does not demand a
    clean `git status`: that check would be about a tree it never reads."""
    repo = producer["path"]
    assert isinstance(repo, Path)
    (repo / "contracts" / "actions" / "v1" / "README.md").write_text("DIRTY\n")

    result = _run(skeleton, str(producer["second"]), "--from", str(repo))

    assert result.returncode == 0, result.stderr
    assert (_vendored(skeleton) / "README.md").read_text() == "second\n"


def test_a_file_deleted_upstream_disappears_from_the_vendored_copy(
    skeleton: Path, producer: dict[str, object]
) -> None:
    result = _run(skeleton, str(producer["second"]), "--from", str(producer["path"]))

    assert result.returncode == 0, result.stderr
    vendored = _vendored(skeleton)
    assert (vendored / "fixtures" / "kept.json").exists()
    assert not (vendored / "fixtures" / "dropped.json").exists()
    assert (vendored / "README.md").read_text() != "SENTINEL\n"


def test_pinned_txt_and_the_manifest_carry_the_sha_that_was_passed(
    skeleton: Path, producer: dict[str, object]
) -> None:
    pin = str(producer["second"])
    result = _run(skeleton, pin, "--from", str(producer["path"]))

    assert result.returncode == 0, result.stderr
    vendored = _vendored(skeleton)
    assert f"commit: {pin}" in (vendored / "PINNED.txt").read_text()
    manifest = json.loads((vendored / "manifest.json").read_text())
    assert manifest["producer_commit"] == pin
    assert {e["path"] for e in manifest["surface"]} == {
        "README.md",
        "actions.schema.json",
        "fixtures/kept.json",
    }


def test_every_vendored_byte_is_the_blob_of_that_commit(
    skeleton: Path, producer: dict[str, object]
) -> None:
    """The assertion the whole procedure exists to support, restated by the
    test against the same object database the script read."""
    repo = producer["path"]
    assert isinstance(repo, Path)
    pin = str(producer["second"])
    assert _run(skeleton, pin, "--from", str(repo)).returncode == 0

    for rel in ("README.md", "actions.schema.json", "fixtures/kept.json"):
        expected = subprocess.run(
            ["git", "-C", str(repo), "show", f"{pin}:contracts/actions/v1/{rel}"],
            capture_output=True,
            check=True,
        ).stdout
        assert (_vendored(skeleton) / rel).read_bytes() == expected


def test_a_failing_generator_leaves_the_working_copy_alone(
    skeleton: Path, producer: dict[str, object]
) -> None:
    """Injected by replacing the generator in the skeleton — the copy the
    script actually invokes — rather than by mocking anything."""
    (skeleton / "scripts" / "vendor_manifest.py").write_text(
        "import sys\nsys.exit(1)\n"
    )
    result = _run(skeleton, str(producer["second"]), "--from", str(producer["path"]))

    assert result.returncode == 4
    _assert_untouched(skeleton)


def test_a_generator_that_corrupts_the_surface_is_caught(
    skeleton: Path, producer: dict[str, object]
) -> None:
    """The second verification pass, tested by making the step between the
    two passes misbehave: the generator writes a manifest whose `surface`
    lists the real on-disk file set (so the read-back's own surface check
    lets it through) and also flips a byte it had no business touching."""
    (skeleton / "scripts" / "vendor_manifest.py").write_text(
        "import json, pathlib, sys\n"
        "root = pathlib.Path(sys.argv[sys.argv.index('--root') + 1])\n"
        "pin = sys.argv[sys.argv.index('--producer-commit') + 1]\n"
        "(root / 'README.md').write_text('tampered\\n')\n"
        "excluded = {'PINNED.txt', 'manifest.json'}\n"
        "surface = [\n"
        "    {'path': str(p.relative_to(root)), 'sha256': 'x'}\n"
        "    for p in sorted(root.rglob('*'))\n"
        "    if p.is_file() and p.name not in excluded\n"
        "]\n"
        "(root / 'manifest.json').write_text(\n"
        "    json.dumps({'producer_commit': pin, 'surface': surface}) + '\\n'\n"
        ")\n"
    )
    result = _run(skeleton, str(producer["second"]), "--from", str(producer["path"]))

    assert result.returncode == 3
    _assert_untouched(skeleton)


def test_a_manifest_recording_the_wrong_pin_is_caught(
    skeleton: Path, producer: dict[str, object]
) -> None:
    (skeleton / "scripts" / "vendor_manifest.py").write_text(
        "import json, pathlib, sys\n"
        "root = pathlib.Path(sys.argv[sys.argv.index('--root') + 1])\n"
        "(root / 'manifest.json').write_text(\n"
        "    json.dumps({'producer_commit': '0' * 40, 'surface': []}) + '\\n'\n"
        ")\n"
    )
    result = _run(skeleton, str(producer["second"]), "--from", str(producer["path"]))

    assert result.returncode == 4
    _assert_untouched(skeleton)


def test_a_hollow_manifest_is_caught(
    skeleton: Path, producer: dict[str, object]
) -> None:
    """A generator that records the right pin but lists no files would pass
    both the old read-back (pin-only) and the second verification pass (it
    only checks the files the manifest DOES list) and land with exit 0. The
    read-back must catch this on its own, not delegate to a downstream
    consumer test to notice later."""
    (skeleton / "scripts" / "vendor_manifest.py").write_text(
        "import json, pathlib, sys\n"
        "root = pathlib.Path(sys.argv[sys.argv.index('--root') + 1])\n"
        "pin = sys.argv[sys.argv.index('--producer-commit') + 1]\n"
        "(root / 'manifest.json').write_text(\n"
        "    json.dumps({'producer_commit': pin, 'surface': []}) + '\\n'\n"
        ")\n"
    )
    result = _run(skeleton, str(producer["second"]), "--from", str(producer["path"]))

    assert result.returncode == 4
    _assert_untouched(skeleton)


def test_a_non_ascii_filename_in_the_source_tree_is_not_misdiagnosed(
    skeleton: Path, tmp_path: Path
) -> None:
    """`git ls-tree` C-quotes any non-ASCII byte in a path by default (e.g.
    "\\321\\201\\321\\205...json"), while `find` — used to list what actually
    landed in staging — emits the raw bytes tar wrote. Without
    core.quotePath=false the two listings can never agree, and a perfectly
    fine non-ASCII filename gets reported as a provenance mismatch even
    though nothing is wrong."""
    repo = tmp_path / "unicode_producer"
    root = repo / "contracts" / "actions" / "v1"
    root.mkdir(parents=True)
    _git(repo.parent, "init", "--quiet", str(repo))
    (root / "схема.json").write_text('{"ok": true}\n', encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "unicode filename")
    pin = _git(repo, "rev-parse", "HEAD")

    result = _run(skeleton, pin, "--from", str(repo))

    assert result.returncode == 0, result.stderr
    vendored = _vendored(skeleton)
    assert (vendored / "схема.json").read_text(encoding="utf-8") == '{"ok": true}\n'


def test_a_blocked_swap_exits_5_and_leaves_the_working_copy_alone(
    skeleton: Path, producer: dict[str, object]
) -> None:
    """A `mv` failing at the swap is an internal failure, not a usage error
    — it must not collide with exit 1. Forced portably, without root and
    without a platform-specific flag like macOS's `chflags uchg`: a regular
    file is pre-placed at the "set aside" name the first rename targets.
    `mv` refuses to overwrite a non-directory with a directory on both BSD
    and GNU coreutils, so the very first rename of the swap fails before
    either one touches the working copy."""
    prev = _vendored(skeleton).parent / "v1.prev"
    prev.touch()

    result = _run(skeleton, str(producer["second"]), "--from", str(producer["path"]))

    assert result.returncode == 5
    vendored = _vendored(skeleton)
    assert (vendored / "README.md").read_text() == "SENTINEL\n"
    assert (vendored / "PINNED.txt").read_text() == "commit: old\n"
    assert not (vendored.parent / "v1.staging").exists()


def test_a_missing_python3_interpreter_exits_4_not_127(
    skeleton: Path, producer: dict[str, object], tmp_path: Path
) -> None:
    """The early `command -v python3` check exists so an absent interpreter
    lands on the documented "manifest generation" exit code, not on
    whatever the shell's own "command not found" happens to produce. Built
    by hand-picking, onto a bare PATH, exactly the binaries the script
    calls before and during that check — deliberately leaving every
    python3 off it. Every other tool must stay reachable, or a failure here
    would prove nothing about python3 specifically.

    Exit 4 alone does not pin this particular guard: with it removed, the
    first real `python3 ...` invocation would fail with the shell's own
    "command not found" (127), which the script's own `|| die 4 "manifest
    generation failed"` also turns into exit 4 — same code, different
    message. Asserting on the message is what proves the early check, not
    that fallback, produced this result."""
    tool_names = (
        "bash",
        "sh",
        "git",
        "tar",
        "sed",
        "sort",
        "find",
        "diff",
        "grep",
        "mv",
        "rm",
        "mkdir",
        "wc",
        "tr",
        "cat",
        "date",
        "dirname",
        "basename",
        "mktemp",
        "env",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in tool_names:
        resolved = shutil.which(name)
        assert resolved is not None, f"host is missing {name}"
        (bin_dir / name).symlink_to(resolved)

    result = subprocess.run(
        [
            str(skeleton / "scripts" / SCRIPT_NAME),
            str(producer["second"]),
            "--from",
            str(producer["path"]),
        ],
        capture_output=True,
        text=True,
        env={
            **_GIT_ENV,
            "PATH": str(bin_dir),
            "HOME": os.environ.get("HOME", ""),
        },
    )

    assert result.returncode == 4, (result.stdout, result.stderr)
    assert "python3 not found on PATH" in result.stderr
    _assert_untouched(skeleton)


def test_help_prints_the_full_exit_code_table_and_exits_0(skeleton: Path) -> None:
    """`--help` used to exit 1 — the code the published table reserves for a
    usage error — even though asking for help is not one. It also used to
    print a header slice bounded by a hardcoded line number, which a later
    commit made stale: this pins both the exit code and that the two lines
    describing "any other nonzero status" (the ones that were being cut)
    are present in the output."""
    result = _run(skeleton, "--help")
    assert result.returncode == 0
    assert "5 internal failure (working copy left as it was found)" in result.stderr
    assert "unexpected internal" in result.stderr
    assert "trap below has still restored the tree" in result.stderr
    _assert_untouched(skeleton)


def test_a_repeated_from_is_a_usage_error(
    skeleton: Path, producer: dict[str, object], tmp_path: Path
) -> None:
    """A second `--from` used to silently overwrite the first, which
    mislabels a usage mistake as if it were the operator's intent."""
    other = tmp_path / "not-a-repo"
    other.mkdir()
    result = _run(
        skeleton,
        str(producer["second"]),
        "--from",
        str(producer["path"]),
        "--from",
        str(other),
    )
    assert result.returncode == 1
    _assert_untouched(skeleton)


def test_a_from_value_beginning_with_dash_is_a_usage_error(skeleton: Path) -> None:
    """`--from -x` used to take `-x` as the path and fail with exit 2
    ("source or commit unavailable") — the wrong code for what is really a
    missing argument to `--from`."""
    result = _run(skeleton, "--from", "-x")
    assert result.returncode == 1
    _assert_untouched(skeleton)


def test_both_scripts_name_the_same_producer(tmp_path: Path) -> None:
    """`install_pinned_checker.sh` fetches the binary and this one fetches
    the contract. Pointed at different sources they would prove nothing
    about each other, and the divergence would be invisible."""
    revendor = (REPO_ROOT / "scripts" / SCRIPT_NAME).read_text()
    install = (REPO_ROOT / "scripts" / "install_pinned_checker.sh").read_text()
    url = "https://github.com/andrei-shtanakov/github-checker"
    assert f'PRODUCER_URL="{url}"' in revendor
    assert f'PRODUCER_URL="{url}"' in install
