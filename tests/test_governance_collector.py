"""Governance-collector classification (WS-005 WS-B, inbox #106).

Fixtures come ONLY from the vendored contract copy — the canon negative
classes are part of the contract surface, not hand-made test data (CON-03).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

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


def test_header_missing_schema_version_gets_the_generic_reason(
    tmp_path: Path,
) -> None:
    """A header invalid for another reason must not be reported as an
    'unsupported schema_version None' — that points away from the defect
    (PR #107 review)."""
    import json

    header = json.loads((FIXTURES / "clean.jsonl").read_text().splitlines()[0])
    del header["schema_version"]
    repo = tmp_path / "observed"
    (repo / VERDICTS_REL_PATH).parent.mkdir(parents=True)
    (repo / VERDICTS_REL_PATH).write_text(json.dumps(header) + "\n")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"
    assert result.reason is not None
    assert "unsupported schema_version" not in result.reason
    assert "does not match gate-verdicts/v1" in result.reason


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


def stale_facts(_repo: Path, _bundle: str, _commit: str) -> BundleFreshness:
    return BundleFreshness(
        fresh=False, current_commit="cd" * 20, detail="bundle tree differs"
    )


def unknown_facts(_repo: Path, _bundle: str, _commit: str) -> BundleFreshness:
    return BundleFreshness(fresh=None, detail="not a git repository")


def test_source_commit_mismatch_is_stale_with_both_commits(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, "clean.jsonl")
    result = collect_governance(repo, git_facts=stale_facts)
    assert result.state == "stale"
    assert result.reason is not None
    assert "ab" * 20 in result.reason and "cd" * 20 in result.reason


def test_unknown_freshness_is_stale_never_pass(tmp_path: Path) -> None:
    """Fail-closed: a repo whose git facts cannot be read must not look green."""
    repo = repo_with(tmp_path, "clean.jsonl")
    result = collect_governance(repo, git_facts=unknown_facts)
    assert result.state == "stale"
    assert result.reason is not None and "not a git repository" in result.reason


def test_dirty_header_is_stale_even_with_fresh_facts(tmp_path: Path) -> None:
    repo = tmp_path / "observed"
    (repo / VERDICTS_REL_PATH).parent.mkdir(parents=True)
    lines = (FIXTURES / "clean.jsonl").read_text().splitlines()
    lines[0] = lines[0].replace('"dirty": false', '"dirty": true')
    (repo / VERDICTS_REL_PATH).write_text("\n".join(lines) + "\n")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "stale"


def test_findings_classify_as_blocked_with_findings_exposed(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, "findings.jsonl")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "blocked"
    assert {f.artifact for f in result.findings} == {
        "15-behaviour-spec.md",
        "10-requirements.md",
    }
    assert result.unresolvable_findings == []


def test_chained_ledger_is_read_not_unreadable(tmp_path: Path) -> None:
    """A hash-chained file (steward#105, emitted since steward PR #109) must
    classify like its unchained twin — ``prev_hash`` is additive, and a copy
    still pinned to the pre-chain schema read every new ledger as unreadable
    (inbox #173)."""
    repo = repo_with(tmp_path, "chained.jsonl")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "blocked"
    assert {f.artifact for f in result.findings} == {
        "15-behaviour-spec.md",
        "10-requirements.md",
    }
    assert all(a.prev_hash is not None for a in result.artifacts)
    assert all(f.prev_hash is not None for f in result.findings)


def test_broken_chain_reads_the_same_because_the_chain_is_not_verified(
    tmp_path: Path,
) -> None:
    """Characterization, not an endorsement: every line of the canon
    broken-chain fixture is schema-valid on its own, and this collector does
    not (yet) run the chain verification of README §'Целостность' — so a
    tampered ledger classifies exactly like the intact one. Running the
    verifier is an explicit open follow-up (TODO.md
    ``@id:gate-verdicts-chain-verification``); when it lands, this test must
    flip to expecting ``unreadable``."""
    repo = repo_with(tmp_path, "broken_chain.jsonl")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "blocked"


def test_dangling_finding_is_unresolvable_not_pass_not_blocked(
    tmp_path: Path,
) -> None:
    repo = repo_with(tmp_path, "dangling_artifact.jsonl")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unresolvable"
    assert [f.artifact for f in result.unresolvable_findings] == ["99-ghost.md"]


def test_stale_wins_over_findings(tmp_path: Path) -> None:
    """Precedence: content of a stale file is not trusted enough to rank it."""
    repo = repo_with(tmp_path, "findings.jsonl")
    result = collect_governance(repo, git_facts=stale_facts)
    assert result.state == "stale"


def _findings_with_obligation(tmp_path: Path, obligation: str) -> Path:
    """The findings fixture with `obligation` stamped onto its last finding.

    Built from the vendored fixture, not hand-made from scratch (CON-03);
    only the field under test is added."""
    import json

    lines = (FIXTURES / "findings.jsonl").read_text().splitlines()
    record = json.loads(lines[-1])
    record["obligation"] = obligation
    lines[-1] = json.dumps(record)
    repo = tmp_path / "observed"
    (repo / VERDICTS_REL_PATH).parent.mkdir(parents=True)
    (repo / VERDICTS_REL_PATH).write_text("\n".join(lines) + "\n")
    return repo


def test_catalog_obligation_on_a_finding_is_accepted(tmp_path: Path) -> None:
    """A value from the vendored gate-catalog vocabulary (inbox #125) parses
    and is carried through; absent obligation is the older producer and is
    covered by the untouched fixture tests above."""
    repo = _findings_with_obligation(tmp_path, "quality")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "blocked"
    assert "quality" in {f.obligation for f in result.findings}


def test_unknown_obligation_is_unreadable_naming_value_and_line(
    tmp_path: Path,
) -> None:
    """Fail-closed: an obligation outside the catalog vocabulary means the
    producer and the vendored catalog diverged — never silently passed
    through, and the reason points at the vendored copy to re-vendor."""
    repo = _findings_with_obligation(tmp_path, "vibes")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"
    assert result.reason is not None
    assert "'vibes'" in result.reason
    assert "line 6" in result.reason
    assert "contracts/steward-gate-catalog/v1/" in result.reason


def _seed_git_repo(tmp_path: Path) -> tuple[Path, str]:
    """A real observed repo whose bundle matches the clean fixture's layout."""
    import os
    import subprocess

    repo = tmp_path / "real"
    bundle = repo / "workstreams" / "WS-005-gate-verdicts" / "spec"
    bundle.mkdir(parents=True)
    (bundle / "10-requirements.md").write_text("r\n")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, **env},
        ).stdout.strip()

    git("init", "--quiet")
    git("add", "-A")
    git("commit", "--quiet", "-m", "seed")
    return repo, git("rev-parse", "HEAD")


def test_real_git_facts_fresh_then_stale(tmp_path: Path) -> None:
    from dispatcher.core.governance import git_bundle_freshness

    repo, head = _seed_git_repo(tmp_path)
    bundle = "workstreams/WS-005-gate-verdicts/spec"
    assert git_bundle_freshness(repo, bundle, head).fresh is True
    (repo / bundle / "10-requirements.md").write_text("changed\n")
    assert git_bundle_freshness(repo, bundle, head).fresh is False


def test_real_git_facts_outside_a_repo_are_unknown(tmp_path: Path) -> None:
    from dispatcher.core.governance import git_bundle_freshness

    plain = tmp_path / "plain"
    plain.mkdir()
    assert git_bundle_freshness(plain, "spec", "ab" * 20).fresh is None


_OS_ERRORS = [
    PermissionError("denied"),
    IsADirectoryError("is a dir"),
    InterruptedError("interrupted"),
    TimeoutError("timed out"),
    BlockingIOError("would block"),
    OSError("generic I/O failure"),
]


@pytest.mark.parametrize("err", _OS_ERRORS, ids=lambda e: type(e).__name__)
def test_every_io_error_class_is_unreadable_never_pass_never_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: OSError
) -> None:
    """NFR-02 stated as a sweep, not examples: no OSError class (beyond the
    one no-data case below) may reach pass or vanish without a reason."""
    repo = repo_with(tmp_path, "clean.jsonl")

    def boom(_self: Path) -> bytes:
        raise err

    monkeypatch.setattr(Path, "read_bytes", boom)
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"
    assert result.reason is not None and type(err).__name__ in result.reason


def test_file_vanishing_between_listing_and_read_is_no_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FileNotFoundError is the one OSError that means no-data, and the
    collector must reach that via the read attempt itself — no exists()
    pre-check, no TOCTOU window."""
    repo = repo_with(tmp_path, "clean.jsonl")

    def gone(_self: Path) -> bytes:
        raise FileNotFoundError("vanished")

    monkeypatch.setattr(Path, "read_bytes", gone)
    assert collect_governance(repo, git_facts=fresh).state == "no-data"


def test_non_utf8_bytes_are_unreadable(tmp_path: Path) -> None:
    repo = tmp_path / "observed"
    (repo / VERDICTS_REL_PATH).parent.mkdir(parents=True)
    (repo / VERDICTS_REL_PATH).write_bytes(b"\xff\xfe{ not utf8")
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"


def test_truncated_last_line_is_unreadable(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, "clean.jsonl")
    full = (repo / VERDICTS_REL_PATH).read_text()
    (repo / VERDICTS_REL_PATH).write_text(full[:-20])  # cut mid-record
    result = collect_governance(repo, git_facts=fresh)
    assert result.state == "unreadable"


def test_dispatcher_never_imports_steward() -> None:
    """ARCH-C1, stated structurally (the import-detector obligation)."""
    package_root = Path(__file__).parent.parent / "dispatcher"
    offenders = [
        str(p)
        for p in package_root.rglob("*.py")
        for line in p.read_text().splitlines()
        if line.strip().startswith(("import steward", "from steward"))
    ]
    assert offenders == []
