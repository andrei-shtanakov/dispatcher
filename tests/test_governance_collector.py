"""Governance-collector classification (WS-005 WS-B, inbox #106).

Fixtures come ONLY from the vendored contract copy — the canon negative
classes are part of the contract surface, not hand-made test data (CON-03).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from dispatcher.core.governance import (
    VERDICTS_REL_PATH,
    BundleFreshness,
    collect_governance,
)

FIXTURES = (
    Path(__file__).parent.parent
    / "contracts"
    / "steward-gate-verdicts"
    / "v1"
    / "fixtures"
)


def fresh(_repo: Path, _bundle: str, _commit: str) -> BundleFreshness:
    return BundleFreshness(fresh=True, current_commit="ab" * 20)


def repo_with(tmp_path: Path, fixture: str) -> Path:
    target = tmp_path / "observed"
    target.mkdir()
    (target / VERDICTS_REL_PATH).parent.mkdir()
    shutil.copy(FIXTURES / fixture, target / VERDICTS_REL_PATH)
    return target


def test_missing_file_is_no_data_not_pass_and_not_an_error(tmp_path: Path) -> None:
    (tmp_path / "observed").mkdir()
    result = collect_governance(tmp_path / "observed", git_facts=fresh)
    assert result.state == "no-data"
    assert result.header is None


def test_malformed_line_is_unreadable_with_the_line_number(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, "malformed_line.jsonl")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"
    assert result.reason is not None and "line 3" in result.reason


def test_future_schema_version_is_unreadable_naming_the_version(
    tmp_path: Path,
) -> None:
    repo = repo_with(tmp_path, "future_schema.jsonl")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"
    assert result.reason is not None and "99" in result.reason


def test_empty_file_is_unreadable_not_pass(tmp_path: Path) -> None:
    repo = tmp_path / "observed"
    (repo / VERDICTS_REL_PATH).parent.mkdir(parents=True)
    (repo / VERDICTS_REL_PATH).write_text("")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"


def test_header_on_a_later_line_is_unreadable(tmp_path: Path) -> None:
    repo = tmp_path / "observed"
    (repo / VERDICTS_REL_PATH).parent.mkdir(parents=True)
    header = (FIXTURES / "clean.jsonl").read_text().splitlines()[0]
    (repo / VERDICTS_REL_PATH).write_text(header + "\n" + header + "\n")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"
    assert result.reason is not None and "line 2" in result.reason


def test_clean_fixture_with_fresh_facts_parses_to_pass(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, "clean.jsonl")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "pass"
    assert result.header is not None
    assert result.header.source_commit == "ab" * 20
    assert len(result.artifacts) == 2
    assert result.findings == []
