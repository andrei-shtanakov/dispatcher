"""GET /api/projects/{name}/governance (WS-005 WS-C, inbox #108).

The endpoint is a pass-through of the WS-B read model (ARCH-C4): tests seed
REAL tmp git repos so freshness comes from the true provider — no fixture
here may accidentally test a mock of the thing the panel exists to show.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
from conftest import make_arbiter

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.governance import VERDICTS_REL_PATH
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio

FIXTURES = (
    Path(__file__).parent.parent
    / "contracts"
    / "steward-gate-verdicts"
    / "v1"
    / "fixtures"
)
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}
BUNDLE = "workstreams/WS-005-gate-verdicts/spec"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


def _seed_verdicts(project_root: Path, fixture: str) -> None:
    """Make the detected project a real git repo whose verdicts file's
    header names its actual HEAD, so the live git-facts provider says fresh."""
    bundle = project_root / BUNDLE
    bundle.mkdir(parents=True)
    (bundle / "10-requirements.md").write_text("r\n")
    (bundle / "15-behaviour-spec.md").write_text("b\n")
    _git(project_root, "init", "--quiet")
    _git(project_root, "add", "-A")
    _git(project_root, "commit", "--quiet", "-m", "seed")
    head = _git(project_root, "rev-parse", "HEAD")
    lines = (FIXTURES / fixture).read_text().splitlines()
    header = json.loads(lines[0])
    header["source_commit"] = head
    lines[0] = json.dumps(header)
    target = project_root / VERDICTS_REL_PATH
    target.parent.mkdir()
    target.write_text("\n".join(lines) + "\n")


def _client(tmp_path: Path) -> httpx.AsyncClient:
    make_arbiter(tmp_path)
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_no_verdicts_file_is_no_data_not_404(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/arbiter/governance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "no-data"


async def test_clean_fixture_in_a_real_repo_is_pass_with_provenance(
    tmp_path: Path,
) -> None:
    _seed_verdicts(tmp_path / "arbiter", "clean.jsonl")
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/arbiter/governance")
    data = resp.json()
    assert data["state"] == "pass"
    assert data["header"]["generated_at"]
    assert len(data["header"]["source_commit"]) == 40
    assert len(data["artifacts"]) == 2


async def test_findings_fixture_is_blocked_with_findings(tmp_path: Path) -> None:
    _seed_verdicts(tmp_path / "arbiter", "findings.jsonl")
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/arbiter/governance")
    data = resp.json()
    assert data["state"] == "blocked"
    assert {f["artifact"] for f in data["findings"]} == {
        "10-requirements.md",
        "15-behaviour-spec.md",
    }


async def test_malformed_fixture_is_unreadable_never_pass(tmp_path: Path) -> None:
    """M-01 at the API layer, on the canon damage fixture."""
    _seed_verdicts(tmp_path / "arbiter", "malformed_line.jsonl")
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/arbiter/governance")
    data = resp.json()
    assert data["state"] == "unreadable"
    assert data["reason"]


async def test_unknown_project_is_404(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/projects/nope/governance")
    assert resp.status_code == 404


async def test_governance_surface_is_get_only(tmp_path: Path) -> None:
    """BEH-09/ARCH-C2 stated structurally: every route whose path mentions
    governance accepts GET (+ implicit HEAD) and nothing else."""
    make_arbiter(tmp_path)
    app = create_app(DispatcherConfig(roots=(tmp_path,)))
    governance_routes = [
        r for r in app.routes if "governance" in getattr(r, "path", "")
    ]
    assert governance_routes, "the governance route must exist to be constrained"
    for route in governance_routes:
        methods = getattr(route, "methods", set())
        assert set(methods) <= {"GET", "HEAD"}, getattr(route, "path", "?")
