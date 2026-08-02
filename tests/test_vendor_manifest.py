"""The manifest generator, whose only inputs are a directory and a pin.

The pin used to be a module-level literal here, which made re-vendoring a
matter of remembering three separate hand-edits. It is an argument now, so
the re-vendor script can derive it — and this file's job is to prove the
generator still reproduces the committed manifest exactly, byte for byte,
rather than merely producing something that parses.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import vendor_manifest

REPO_ROOT = Path(__file__).parent.parent
VENDORED_ROOT = REPO_ROOT / "contracts" / "github-checker-actions" / "v1"


def test_regenerating_reproduces_the_committed_manifest_byte_for_byte(
    tmp_path: Path,
) -> None:
    """A generator that produces *a* manifest is not the same as one that
    produces *this* manifest: whitespace, key order and the trailing newline
    are all part of what the committed file is."""
    committed = (VENDORED_ROOT / "manifest.json").read_bytes()
    pin = json.loads(committed)["producer_commit"]

    root = tmp_path / "v1"
    shutil.copytree(VENDORED_ROOT, root)
    (root / "manifest.json").unlink()

    subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "vendor_manifest.py"),
            "--producer-commit",
            pin,
            "--root",
            str(root),
        ],
        check=True,
    )

    assert (root / "manifest.json").read_bytes() == committed


def test_the_generator_refuses_to_run_without_a_pin() -> None:
    """No default, and no fallback to whatever the root already contains:
    a manifest silently regenerated at the previous commit is how new bytes
    get certified as coming from an old one."""
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "vendor_manifest.py")],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "--producer-commit" in result.stderr


def test_the_pin_is_not_hardcoded_in_the_generator() -> None:
    """The literal's absence is the point of the change, so it is asserted
    rather than left to a reviewer's memory."""
    assert not hasattr(vendor_manifest, "PRODUCER_COMMIT")


def test_build_manifest_records_the_pin_it_was_given(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    manifest = vendor_manifest.build_manifest(tmp_path, "0" * 40)
    assert manifest["producer_commit"] == "0" * 40
    surface = manifest["surface"]
    assert isinstance(surface, list)
    assert [e["path"] for e in surface] == ["a.txt"]


@pytest.mark.parametrize("excluded", ["PINNED.txt", "manifest.json"])
def test_the_two_meta_files_stay_out_of_the_surface(
    tmp_path: Path, excluded: str
) -> None:
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / excluded).write_text("meta")
    manifest = vendor_manifest.build_manifest(tmp_path, "0" * 40)
    surface = manifest["surface"]
    assert isinstance(surface, list)
    assert [e["path"] for e in surface] == ["a.txt"]


def test_build_manifest_records_the_contract_it_was_given(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    manifest = vendor_manifest.build_manifest(
        tmp_path, "0" * 40, contract="steward-gate-verdicts", contract_version=1
    )
    assert manifest["contract"] == "steward-gate-verdicts"
    assert manifest["contract_version"] == 1


def test_contract_name_defaults_to_the_original_consumer(tmp_path: Path) -> None:
    """Existing callers pass no name; their manifests must not change shape."""
    (tmp_path / "a.txt").write_text("x")
    manifest = vendor_manifest.build_manifest(tmp_path, "0" * 40)
    assert manifest["contract"] == "github-checker-actions"
    assert manifest["contract_version"] == 1
