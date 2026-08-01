"""The instrument that proves the live binary IS the vendored pin.

DESIGN-405 level 3 is only worth running if "a github-checker answered" also
means "*the pinned* github-checker answered". A binary resolved off PATH
carries no version flag, so identity comes from PEP 610 (`direct_url.json`),
which records the commit a VCS install resolved to.

These tests cover the instrument itself, because a check that cannot fail is
indistinguishable from no check: absence must fail, a mismatched commit must
fail, and an install that proves no commit at all must fail.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from pinned_producer import (
    PRODUCER_COMMIT,
    ProducerBinaryProblem,
    commit_of_direct_url,
    pinned_producer_binary,
)


def _fake_install(root: Path, direct_url: object) -> Path:
    """A venv-shaped directory whose interpreter reports `direct_url`.

    Not a mock of the helper: the real resolution path runs — PATH lookup,
    the sibling interpreter, a subprocess, the JSON. Only the installed
    package is fabricated, which is exactly the thing under test.
    """
    binary = root / "bin"
    binary.mkdir(parents=True)
    (binary / "github-checker").write_text("#!/bin/sh\nexit 0\n")
    (binary / "github-checker").chmod(0o755)
    payload = "" if direct_url is None else json.dumps(direct_url)
    python = binary / "python"
    python.write_text(f"#!/bin/sh\ncat <<'EOF'\n{payload}\nEOF\n")
    python.chmod(0o755)
    return binary


class TestCommitOfDirectUrl:
    """The pure half: what a PEP 610 record does and does not prove."""

    def test_a_vcs_install_proves_the_commit_it_resolved(self) -> None:
        commit = commit_of_direct_url(
            json.dumps(
                {
                    "url": "https://github.com/andrei-shtanakov/github-checker",
                    "vcs_info": {
                        "vcs": "git",
                        "commit_id": "ef03fefcded37676b19ef1c6f88b956a09a26d3f",
                        "requested_revision": "ef03fef",
                    },
                }
            )
        )
        assert commit == "ef03fefcded37676b19ef1c6f88b956a09a26d3f"

    def test_the_resolved_commit_wins_over_the_requested_revision(self) -> None:
        """`@master` resolves to whatever master was — that is the drift."""
        commit = commit_of_direct_url(
            json.dumps(
                {
                    "url": "https://github.com/andrei-shtanakov/github-checker",
                    "vcs_info": {
                        "vcs": "git",
                        "commit_id": "0" * 40,
                        "requested_revision": "master",
                    },
                }
            )
        )
        assert commit == "0" * 40

    def test_an_editable_checkout_proves_no_commit(self) -> None:
        """A local directory install is a working tree, not a pin."""
        with pytest.raises(ProducerBinaryProblem, match="not a pinned VCS install"):
            commit_of_direct_url(
                json.dumps(
                    {
                        "url": "file:///Users/someone/github-checker",
                        "dir_info": {"editable": True},
                    }
                )
            )

    def test_a_wheel_from_an_index_proves_no_commit(self) -> None:
        with pytest.raises(ProducerBinaryProblem, match="not a pinned VCS install"):
            commit_of_direct_url(json.dumps({"url": "https://example/gc.whl"}))

    def test_a_distribution_without_the_record_proves_no_commit(self) -> None:
        """`read_text` returns None for a plain `pip install .` — not a crash."""
        with pytest.raises(ProducerBinaryProblem, match="no PEP 610 record"):
            commit_of_direct_url("")

    def test_an_unparseable_record_is_refused_not_guessed(self) -> None:
        with pytest.raises(ProducerBinaryProblem, match="unreadable"):
            commit_of_direct_url("{not json")


class TestPinnedProducerBinary:
    """The end-to-end half: what makes the job red."""

    def test_an_absent_binary_fails_it_never_skips(self, monkeypatch) -> None:
        """The whole point of the slice: `skipped` reads as `verified`."""
        monkeypatch.setenv("PATH", "")
        with pytest.raises(ProducerBinaryProblem, match="no `github-checker` on PATH"):
            pinned_producer_binary()

    def test_a_wrong_commit_fails_and_names_both(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        wrong = "1" * 40
        binary = _fake_install(
            tmp_path / "wrong",
            {"url": "https://x", "vcs_info": {"vcs": "git", "commit_id": wrong}},
        )
        monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
        with pytest.raises(ProducerBinaryProblem) as caught:
            pinned_producer_binary()
        message = str(caught.value)
        assert wrong in message
        assert PRODUCER_COMMIT in message

    def test_the_pinned_commit_is_accepted(self, tmp_path: Path, monkeypatch) -> None:
        """Non-vacuity: the same path that rejects a wrong commit accepts the pin."""
        binary = _fake_install(
            tmp_path / "right",
            {
                "url": "https://x",
                "vcs_info": {"vcs": "git", "commit_id": PRODUCER_COMMIT},
            },
        )
        monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
        assert pinned_producer_binary() == (binary / "github-checker").resolve()

    def test_an_unrunnable_interpreter_fails_as_this_check_not_as_an_OSError(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Fail-closed has to cover the instrument, not only its subject.

        A `python` that exists but cannot be executed raises `PermissionError`
        out of `subprocess.run`. Escaping as a bare `OSError` would make the
        failure read as "the test harness broke", not "the binary could not be
        identified" — and only the second is true.
        """
        binary = _fake_install(
            tmp_path / "unrunnable",
            {"url": "https://x", "vcs_info": {"vcs": "git", "commit_id": "2" * 40}},
        )
        (binary / "python").chmod(0o644)
        monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
        with pytest.raises(ProducerBinaryProblem, match="could not be run"):
            pinned_producer_binary()

    def test_a_failing_probe_reports_what_the_interpreter_said(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """`(exit 1)` alone sends the reader to the CI log for nothing."""
        binary = _fake_install(tmp_path / "noisy", None)
        (binary / "python").write_text(
            "#!/bin/sh\necho 'ModuleNotFoundError: github-checker' >&2\nexit 1\n"
        )
        (binary / "python").chmod(0o755)
        monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
        with pytest.raises(ProducerBinaryProblem) as caught:
            pinned_producer_binary()
        assert "ModuleNotFoundError: github-checker" in str(caught.value)

    def test_a_flood_of_probe_stderr_is_bounded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A megabyte of traceback in an assertion message helps nobody."""
        binary = _fake_install(tmp_path / "flood", None)
        (binary / "python").write_text(
            "#!/bin/sh\nawk 'BEGIN{while(i++<5000)printf \"x\"}' >&2\nexit 1\n"
        )
        (binary / "python").chmod(0o755)
        monkeypatch.setenv("PATH", f"{binary}{os.pathsep}{os.environ['PATH']}")
        with pytest.raises(ProducerBinaryProblem) as caught:
            pinned_producer_binary()
        assert len(str(caught.value)) < 1000

    def test_an_install_without_a_sibling_interpreter_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A bare script on PATH can answer without being identifiable."""
        lonely = tmp_path / "lonely" / "bin"
        lonely.mkdir(parents=True)
        (lonely / "github-checker").write_text("#!/bin/sh\nexit 0\n")
        (lonely / "github-checker").chmod(0o755)
        monkeypatch.setenv("PATH", f"{lonely}{os.pathsep}{os.environ['PATH']}")
        with pytest.raises(ProducerBinaryProblem, match="no interpreter beside"):
            pinned_producer_binary()


def test_the_installer_carries_no_second_copy_of_the_pin() -> None:
    """A hardcoded commit in the script is a pin that re-vendoring won't move.

    The failure would be silent and inverted: the schema copy advances, the
    installed binary does not, and level 3 goes green against a producer the
    vendored contract no longer describes.
    """
    script = (
        Path(__file__).parent.parent / "scripts" / "install_pinned_checker.sh"
    ).read_text()
    assert not re.search(r"\b[0-9a-f]{40}\b", script), (
        "the installer must read producer_commit from the vendored manifest, "
        "not carry its own copy of the commit"
    )


def test_the_human_readable_pin_agrees_with_the_machine_readable_one() -> None:
    """`PINNED.txt` is excluded from the manifest, so nothing else checks it.

    The install script and the humans reading the directory take the commit
    from two different files. Left unchecked, the copy nobody verifies is the
    one that drifts.
    """
    pinned = (
        Path(__file__).parent.parent
        / "contracts"
        / "github-checker-actions"
        / "v1"
        / "PINNED.txt"
    ).read_text()
    assert f"commit: {PRODUCER_COMMIT}" in pinned
