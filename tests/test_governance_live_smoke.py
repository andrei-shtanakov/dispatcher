"""Cross-repo live smoke: the REAL steward emitter at the contract pin.

WS-005 WS-C acceptance ("сквозной smoke через настоящие бинари"): the real
`gate-check --emit-verdicts` — installed by scripts/install_pinned_steward.sh
at exactly the commit `contracts/steward-gate-verdicts/v1/manifest.json`
names — writes a real verdicts file into a real git repo, and dispatcher's
HTTP surface classifies and serves it. No mock stands in for either half.

The bundle below is deliberately minimal-but-imperfect: at the pin,
gate-check finds two warn-level findings (GC-TRACE-EMPTY and
GC-STALE-UNPINNED on 15-behaviour.md) and exits 0. Findings are findings —
the collector classifies warn as blocked, so the asserted end state is
`blocked` with those two gate ids. A different result after a re-vendor is a
real contract-behaviour change and must be reviewed, not re-pinned away.

`gate-check` on PATH is a HARD prerequisite — this test FAILS without it, it
does not skip (PR #98 discipline). CI installs it next to the pinned
github-checker; locally:
    PATH="$(scripts/install_pinned_steward.sh):$PATH" uv run pytest \
        tests/test_governance_live_smoke.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from conftest import make_arbiter

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.governance import VERDICTS_REL_PATH
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}

_MISSING_BINARY = (
    "gate-check is a required prerequisite of the governance live smoke — "
    "install it at the vendored pin with scripts/install_pinned_steward.sh "
    "(CI does; locally prepend its bin dir to PATH). Without it the "
    "cross-repo half of WS-005 WS-C is UNVERIFIED, and that must FAIL, "
    "not skip."
)

_REQUIREMENTS = """---
spec_stage: requirements
status: approved
---
#### FR-01: Something observable
**Priority**: 🔴 Must
"""

_BEHAVIOUR = """---
spec_stage: behaviour-spec
status: approved
---
#### BEH-01: Scenario `traces: [FR-01]`
- **checked_by**: `status: planned` `kind: e2e` `owner: @qa` `target: t.py::x`
"""

_PROFILE = """profile: team-exp-smoke
solo_auto_approve: true
artifacts:
  - id: requirements
    owner_role: "@product"
    upstream: []
  - id: behaviour-spec
    owner_role: "@qa"
    upstream: [requirements]
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_GIT_ENV},
    ).stdout.strip()


async def test_live_smoke_real_gate_check_to_http_state(tmp_path: Path) -> None:
    binary = shutil.which("gate-check")
    assert binary is not None, _MISSING_BINARY

    # A detectable project (arbiter markers) that is ALSO a real git repo
    # with a governance bundle — the emitter needs live provenance.
    make_arbiter(tmp_path)
    repo = tmp_path / "arbiter"
    spec = repo / "spec"
    spec.mkdir()
    (spec / "10-requirements.md").write_text(_REQUIREMENTS)
    (spec / "15-behaviour.md").write_text(_BEHAVIOUR)
    profile = tmp_path / "profile.yaml"
    profile.write_text(_PROFILE)
    # gate-check @ пине steward#50+ требует для --emit-verdicts sibling-файлы
    # профиля: gate-catalog.yaml (эмиттер-гейт по active-каталогу — берём наш
    # ВЕНДОРЕННЫЙ канон, он и есть предмет этого смоука) и roles.yaml
    # (минимальная валидная форма — ролей каталог не ссылается).
    vendored_catalog = (
        Path(__file__).resolve().parents[1]
        / "contracts"
        / "steward-gate-catalog"
        / "v1"
        / "gate-catalog.yaml"
    )
    (tmp_path / "gate-catalog.yaml").write_bytes(vendored_catalog.read_bytes())
    (tmp_path / "roles.yaml").write_text(
        'version: 1\nslug_pattern: "^[a-z][a-z0-9-]{1,31}$"\n'
        "roles:\n  - {slug: owner, display: Owner}\n"
    )
    _git(repo, "init", "--quiet", "-b", "master")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "seed")
    head = _git(repo, "rev-parse", "HEAD")

    run = subprocess.run(
        [binary, "spec", "--profile", str(profile), "--emit-verdicts"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert (repo / VERDICTS_REL_PATH).is_file()

    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/projects/arbiter/governance")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "blocked"
    assert data["header"]["source_commit"] == head
    assert {f["gate_id"] for f in data["findings"]} == {
        "GC-TRACE-EMPTY",
        "GC-STALE-UNPINNED",
    }
    assert data["unresolvable_findings"] == []
