"""Integration tests for the HTTP API over a fixtures root."""

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from conftest import (
    make_arbiter,
    make_atp,
    make_maestro,
    make_maestro_home,
    make_spec_runner,
)

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.suggest_cli import SuggestRunner
from dispatcher.server.app import create_app

pytestmark = pytest.mark.anyio

_ONBOARDING_ROADMAP = """
version: 1
roadmap: onboarding-api-fixture
title: Fixture
items:
  - id: RD-OB-DONE
    title: Done dep
    phase: "1"
    owner_project: arbiter
    evidence_rules:
      - rule: project_detected
        kind: implementation
        project: arbiter
      - rule: work_item_chain
        kind: verification
        work_item_id: T-9
        min_links: 2
  - id: RD-OB-NEXT
    title: Actionable next
    phase: "2"
    owner_project: arbiter
    depends_on: [RD-OB-DONE]
    evidence_rules:
      - rule: file_exists
        kind: implementation
        project: arbiter
        path: contracts/nope.json
  - id: RD-OB-BLOCKED
    title: Blocked by ghost
    phase: "2"
    owner_project: arbiter
    depends_on: [RD-OB-GHOST]
    evidence_rules:
      - rule: file_exists
        kind: implementation
        project: arbiter
        path: contracts/also-nope.json
"""


def _client(tmp_path: Path) -> httpx.AsyncClient:
    make_atp(tmp_path)
    make_arbiter(tmp_path)
    make_spec_runner(tmp_path)
    db = make_maestro_home(tmp_path)
    config = DispatcherConfig(roots=(tmp_path,), maestro_db=db)
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_overview(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/overview")
    assert resp.status_code == 200
    data = resp.json()
    by_name = {p["name"]: p for p in data["projects"]}
    assert by_name["arbiter"]["detected"] is True
    assert by_name["arbiter"]["counts"]["tasks"] == 1
    assert by_name["Maestro"]["detected"] is False  # no project dir in root
    assert by_name["proctor"]["detected"] is False


async def test_project_detail_and_404(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        ok = await client.get("/api/projects/arbiter")
        missing = await client.get("/api/projects/unknown")
    assert ok.status_code == 200
    assert ok.json()["tasks"][0]["task_id"] == "T-9"
    assert missing.status_code == 404


async def test_errors_feed(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/errors", params={"limit": 5})
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) <= 5
    assert any(e["body"] == "subprocess failed" for e in events)


async def test_errors_negative_limit_rejected(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/errors", params={"limit": -1})
    assert resp.status_code == 422


async def test_errors_sorted_newest_first(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        events = (await client.get("/api/errors")).json()
    stamps = [e["timestamp"] or "" for e in events]
    assert stamps == sorted(stamps, reverse=True)


async def test_errors_project_filter(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        all_events = (await client.get("/api/errors")).json()
        arbiter_only = (
            await client.get("/api/errors", params={"project": "arbiter"})
        ).json()
        unknown = (await client.get("/api/errors", params={"project": "nope"})).json()
    assert 0 < len(arbiter_only) < len(all_events)
    # spec-runner fixture errors must not leak into the arbiter view
    assert not any("lint failed" in e["body"] for e in arbiter_only)
    assert unknown == []


async def test_errors_service_filter(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        all_events = (await client.get("/api/errors")).json()
        svc_only = (await client.get("/api/errors", params={"service": "svc"})).json()
        unknown = (await client.get("/api/errors", params={"service": "nope"})).json()
    assert 0 < len(svc_only) < len(all_events)
    assert all(e["service"] == "svc" for e in svc_only)
    assert unknown == []


async def test_errors_days_filter(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        all_events = (await client.get("/api/errors")).json()
        recent = (await client.get("/api/errors", params={"days": 1})).json()
        huge = (await client.get("/api/errors", params={"days": 36500})).json()
        bad = await client.get("/api/errors", params={"days": 0})
    assert len(recent) <= len(all_events)
    assert len(huge) == len(all_events)
    assert bad.status_code == 422


def test_recent_errors_helper() -> None:
    from datetime import UTC, datetime

    from dispatcher.core.models import ErrorEvent
    from dispatcher.server.app import recent_errors

    now = datetime(2026, 7, 3, 12, 0, 0, tzinfo=UTC)
    events = [
        ErrorEvent(timestamp="2026-07-02T10:00:00+00:00", body="new", source="s"),
        ErrorEvent(timestamp="2026-02-01T10:00:00", body="old-naive", source="s"),
        ErrorEvent(timestamp=None, body="undated", source="s"),
    ]
    kept = {e.body for e in recent_errors(events, days=14, now=now)}
    assert kept == {"new", "undated"}  # undated events are never dropped


async def test_models_and_contracts(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        models = (await client.get("/api/models")).json()
        contracts = (await client.get("/api/contracts")).json()
    assert any(m["project"] == "arbiter" and m["role"] == "routable" for m in models)
    # DESIGN-702: the endpoint now carries a response model; the JSON
    # shape is unchanged (same keys as the old ad-hoc dict)
    row = models[0]
    assert set(row) == {
        "project",
        "model_id",
        "vendor",
        "harness",
        "role",
        "status",
        "source",
    }
    catalog = next(c for c in contracts if c["name"] == "agents-catalog")
    assert catalog["in_sync"] is False  # fixture vendored copy differs


async def test_index_served(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    assert 'id="projects"' in resp.text
    assert 'id="errors-toggle"' in resp.text
    # Errors live in a collapsible box, collapsed by default (no `open` attr)
    assert '<details id="errors-box">' in resp.text
    assert 'id="errors-service"' in resp.text
    # Regression guard: cards use data-name + a delegated listener; inline
    # onclick would be XSS-prone (project names reach a JS-string context).
    assert "data-name=" in resp.text
    assert "spec-runner-config-suggest" in resp.text
    assert "spec-runner-config-suggest-cancel" in resp.text
    assert "suggest-marker" in resp.text
    assert "suggest-dropped" in resp.text
    assert "onclick=" not in resp.text
    # Roadmap table carries Contract + Freshness columns; empty row spans all 8
    assert "<th>Contract</th>" in resp.text
    assert "<th>Freshness</th>" in resp.text
    assert 'colspan="8"' in resp.text
    assert "/onboarding" in resp.text  # detail() fetches the onboarding view
    assert "onboarding-next" in resp.text  # structured sections replaced raw JSON
    # extra_executor_config overlay editing UI (DESIGN-1001/1002)
    assert "overlay-editor" in resp.text
    assert "overlay-edit" in resp.text
    assert "overlay-clear" in resp.text
    assert "overlay-cancel" in resp.text
    assert "overlay-warning" in resp.text
    assert "overlay-summary" in resp.text
    assert "readSpecRunnerConfigOverlay" in resp.text


async def test_sync_track_endpoint_writes_sidecar(tmp_path: Path) -> None:
    tracking_file = tmp_path / "dispatcher-sync.toml"
    config = DispatcherConfig(roots=(tmp_path,), tracking_file=tracking_file)
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/sync/track", json={"dir": "fresh-clone", "action": "track"}
        )
        assert resp.status_code == 200
        assert resp.json()["tracked"] == ["fresh-clone"]

        resp = await client.post(
            "/api/sync/track", json={"dir": "fresh-clone", "action": "ignore"}
        )
        assert resp.json() == {"tracked": [], "ignored": ["fresh-clone"]}

        resp = await client.post(
            "/api/sync/track", json={"dir": "x", "action": "delete"}
        )
        assert resp.status_code == 422

        # пробелы срезаются ДО персиста — «  padded  » не зависнет вечным предложением
        resp = await client.post(
            "/api/sync/track", json={"dir": "  padded  ", "action": "track"}
        )
        assert "padded" in resp.json()["tracked"]
    assert tracking_file.is_file()


async def test_sync_track_unconfigured_is_409(tmp_path: Path) -> None:
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/sync/track", json={"dir": "a", "action": "track"}
        )
    assert resp.status_code == 409


async def test_roadmap_summary_endpoint(tmp_path: Path) -> None:
    roadmaps = tmp_path / "prograph-vault" / "authored" / "roadmaps"
    roadmaps.mkdir(parents=True)
    (roadmaps / "eco.yaml").write_text(
        """
version: 1
roadmap: eco
items:
  - id: E-1
    title: Detected project
    owner_project: atp-platform
    evidence_rules:
      - rule: project_detected
        kind: implementation
        project: atp-platform
  - id: E-2
    title: Never detected
    owner_project: ghost
    evidence_rules:
      - rule: project_detected
        kind: implementation
        project: ghost
"""
    )
    async with _client(tmp_path) as client:
        resp = await client.get("/api/roadmap/summary")
    assert resp.status_code == 200
    data = resp.json()
    by_name = {p["project"]: p for p in data["projects"]}
    assert by_name["atp-platform"]["readiness"] == 1.0
    assert by_name["ghost"]["readiness"] == 0.0
    assert by_name["ghost"]["lagging"] is True


async def test_sync_endpoint_shape(tmp_path: Path, monkeypatch) -> None:
    # детерминизм: live-путь выключен явно, а не через отсутствие
    # github-checker в PATH конкретной машины
    from dispatcher.core.sync import SyncSourceError

    def no_live(*args, **kwargs):
        raise SyncSourceError("disabled in test")

    monkeypatch.setattr("dispatcher.core.sync.run_live_snapshot", no_live)
    async with _client(tmp_path) as client:
        resp = await client.get("/api/sync")
    assert resp.status_code == 200
    data = resp.json()
    assert data["report"]["top_line"] in ("ok", "pull-first", "no-data", "unknown")
    assert isinstance(data["fetch_in_flight"], bool)
    assert "report_generated_at" in data
    assert isinstance(data["report"]["hosts"], list)
    assert isinstance(data["report"]["proposals"], list)
    # live отключён → честный unknown + warning, независимо от окружения
    assert data["report"]["top_line"] == "unknown"
    assert any("live snapshot unavailable" in w for w in data["report"]["warnings"])


async def test_sync_hosts_endpoint_shape(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/sync/hosts")
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"current_host", "fetch_in_flight", "hosts"}
    assert isinstance(data["hosts"], list)


async def test_sync_hosts_reads_published_kb_snapshot(tmp_path: Path) -> None:
    snapshots_dir = tmp_path / "prograph-vault" / "derived" / "snapshots"
    snapshots_dir.mkdir(parents=True)
    snapshots_dir.joinpath("mac-remote.json").write_text(
        """
{"schema_version": 1, "workspace": "/ws", "host": "mac-remote",
 "generated_at": "2026-07-14T12:00:00Z", "gh_error": null,
 "repos": [{"dir": "alpha", "remote": null,
            "local": {"branch": "main", "ahead": 0, "behind": 2,
                      "dirty": false, "error": null},
            "github": null}]}
"""
    )
    async with _client(tmp_path) as client:
        resp = await client.get("/api/sync/hosts")
    data = resp.json()
    panel = next(h for h in data["hosts"] if h["host"] == "mac-remote")
    assert panel["source"] == "kb"
    assert panel["age_seconds"] is not None
    verdict = next(v for v in panel["verdicts"] if v["repo"] == "alpha")
    assert verdict["verdict"] in ("pull-first", "unknown")  # unknown если stale


async def test_web_page_wires_sync_and_summary(tmp_path: Path) -> None:
    """Статика связана с sync-API: секция, спиннер, track-POST, summary-таблица."""
    async with _client(tmp_path) as client:
        resp = await client.get("/")
        assert resp.status_code == 200
        page = resp.text
    for marker in (
        'id="sync-section"',
        'id="sync-fetch"',  # шестерёнка в углу (FR-01 acceptance)
        'id="sync-proposals"',  # авто-обнаружение (FR-02)
        'id="roadmap-summary"',  # сводный roadmap (FR-03)
        '"/api/sync"',
        '"/api/roadmap/summary"',
        '"/api/sync/track"',
        '"/api/actions/session"',  # CSRF-токен живых действий (M2)
        "X-Action-Token",
    ):
        assert marker in page, f"index.html потерял {marker}"


async def test_action_endpoints_require_token_and_delegate(
    tmp_path: Path, monkeypatch
) -> None:
    from dispatcher.core.actions import ActionOutcome, ActionRunner

    calls = []

    def fake_run(self, action, repo_dir):
        calls.append((action, repo_dir))
        return ActionOutcome(action=action, dir=repo_dir, ok=True, detail="done")

    monkeypatch.setattr(ActionRunner, "run", fake_run)
    async with _client(tmp_path) as client:
        # без токена — 403, действие не вызвано
        resp = await client.post("/api/actions/pull", json={"dir": "alpha"})
        assert resp.status_code == 403
        assert calls == []

        token = (await client.get("/api/actions/session")).json()["token"]
        resp = await client.post(
            "/api/actions/pull",
            json={"dir": "alpha"},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert calls == [("pull", "alpha")]

        resp = await client.post(
            "/api/actions/create-pr",
            json={"dir": "alpha"},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 200
        assert calls[-1] == ("open-pr", "alpha")


async def test_action_busy_maps_to_409(tmp_path: Path, monkeypatch) -> None:
    from dispatcher.core.actions import ActionBusyError, ActionRunner

    def busy_run(self, action, repo_dir):
        raise ActionBusyError("alpha: action already in flight")

    monkeypatch.setattr(ActionRunner, "run", busy_run)
    async with _client(tmp_path) as client:
        token = (await client.get("/api/actions/session")).json()["token"]
        resp = await client.post(
            "/api/actions/pull",
            json={"dir": "alpha"},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 409


async def test_spec_runner_config_view_and_update(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    repo = tmp_path / "alpha"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "project.yaml").write_text(
        "project: alpha\nspec_runner:\n  max_retries: 3\nworkstreams: []\n"
    )
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        view = await client.get("/api/projects/alpha/spec-runner-config")
        assert view.status_code == 200
        assert view.json()["typed"]["max_retries"]["value"] == 3

        missing = await client.get("/api/projects/no-such-project/spec-runner-config")
        assert missing.status_code == 404

        token = (await client.get("/api/actions/session")).json()["token"]
        base_mtime = (repo / "project.yaml").stat().st_mtime
        live_before = (repo / "project.yaml").read_bytes()
        resp = await client.post(
            "/api/actions/update-spec-runner-config",
            headers={"X-Action-Token": token},
            json={
                "dir": "alpha",
                "typed": {"max_retries": 9},
                "base_mtime": base_mtime,
            },
        )
        # github-checker isn't installed in the test env — expect a failed
        # ActionOutcome (200 with ok=False), not a 5xx: the runner degrades
        # to a failed outcome rather than raising. The write path never
        # touches the live tree (DESIGN-401) — it renders to a temp file
        # and delegates to `propose-pr`, so project.yaml is unchanged here
        # regardless of whether the subprocess could run.
        assert resp.status_code == 200
        assert resp.json()["ok"] is False
        assert (repo / "project.yaml").read_bytes() == live_before

        bad_token = await client.post(
            "/api/actions/update-spec-runner-config",
            headers={"X-Action-Token": "wrong"},
            json={
                "dir": "alpha",
                "typed": {},
                "base_mtime": 0,
            },
        )
        assert bad_token.status_code == 403


async def test_spec_runner_config_invalid_candidate_maps_to_422(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    repo = tmp_path / "alpha"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "project.yaml").write_text(
        "project: alpha\nspec_runner:\n  max_retries: 3\nworkstreams: []\n"
    )
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token = (await client.get("/api/actions/session")).json()["token"]
        base_mtime = (repo / "project.yaml").stat().st_mtime
        resp = await client.post(
            "/api/actions/update-spec-runner-config",
            headers={"X-Action-Token": token},
            json={
                "dir": "alpha",
                # string where int is expected -> ConfigValidationError ->
                # SpecRunnerConfigRejectedError -> 422 (app.py's own mapping).
                "typed": {"max_retries": "not-an-int"},
                "base_mtime": base_mtime,
            },
        )
        assert resp.status_code == 422
        # the rejected write must never touch disk
        assert "max_retries: 3" in (repo / "project.yaml").read_text()


async def test_spec_runner_config_busy_maps_to_409(tmp_path: Path, monkeypatch) -> None:
    from dispatcher.core.spec_runner_config_actions import (
        SpecRunnerConfigActionRunner,
        SpecRunnerConfigBusyError,
    )

    def busy_run(self, repo_dir, candidate):
        raise SpecRunnerConfigBusyError(f"{repo_dir}: update already in flight")

    monkeypatch.setattr(SpecRunnerConfigActionRunner, "run", busy_run)
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token = (await client.get("/api/actions/session")).json()["token"]
        resp = await client.post(
            "/api/actions/update-spec-runner-config",
            headers={"X-Action-Token": token},
            json={
                "dir": "alpha",
                "typed": {},
                "base_mtime": 0,
            },
        )
        assert resp.status_code == 409


async def test_spec_runner_config_stale_mtime_maps_to_409(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    repo = tmp_path / "alpha"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "project.yaml").write_text(
        "project: alpha\nspec_runner:\n  max_retries: 3\nworkstreams: []\n"
    )
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token = (await client.get("/api/actions/session")).json()["token"]
        stale_mtime = (repo / "project.yaml").stat().st_mtime - 1000
        resp = await client.post(
            "/api/actions/update-spec-runner-config",
            headers={"X-Action-Token": token},
            json={
                "dir": "alpha",
                "typed": {"max_retries": 9},
                "base_mtime": stale_mtime,
            },
        )
        assert resp.status_code == 409
        # SpecRunnerConfigConflictError must not have written the file
        assert "max_retries: 3" in (repo / "project.yaml").read_text()


async def test_spec_runner_config_noop_reaches_client(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    from dispatcher.core.actions import ActionOutcome
    from dispatcher.core.spec_runner_config_actions import (
        SpecRunnerConfigActionRunner,
    )

    def noop_run(self, repo_dir, candidate):
        return ActionOutcome(
            action="update-spec-runner-config",
            dir=repo_dir,
            ok=False,
            detail="no-op",
            error="no changes vs main",
        )

    monkeypatch.setattr(SpecRunnerConfigActionRunner, "run", noop_run)

    repo = tmp_path / "alpha"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "project.yaml").write_text(
        "project: alpha\nspec_runner:\n  max_retries: 3\nworkstreams: []\n"
    )
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        token = (await client.get("/api/actions/session")).json()["token"]
        base_mtime = (repo / "project.yaml").stat().st_mtime
        resp = await client.post(
            "/api/actions/update-spec-runner-config",
            headers={"X-Action-Token": token},
            json={
                "dir": "alpha",
                "typed": {"max_retries": 9},
                "base_mtime": base_mtime,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["detail"] == "no-op"


async def test_spec_runner_configs_list_reaches_non_overview_projects(
    tmp_path: Path,
) -> None:
    """DESIGN-601: enumeration across roots — incl. dirs that are NOT
    overview cards (a bare steward/project.yaml). This is the discovery
    gap the per-name GET can't close (it needs a known name)."""
    # workspace with one collector project (overview card) and one bare
    # config-only dir (no collector match)
    make_atp(tmp_path)
    steward = tmp_path / "steward"
    steward.mkdir()
    (steward / "project.yaml").write_text(
        "project: steward\nspec_runner:\n  max_retries: 5\nworkstreams: []\n"
    )
    config = DispatcherConfig(roots=(tmp_path,))
    transport = httpx.ASGITransport(app=create_app(config))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/spec-runner-configs")
    assert resp.status_code == 200
    data = resp.json()
    dirs = [Path(c["project_yaml_path"]).parent.name for c in data]
    assert "steward" in dirs  # not an overview card, still listed
    entry = next(c for c in data if c["project"] == "steward")
    assert entry["typed"]["max_retries"]["value"] == 5
    assert entry["typed"]["max_retries"]["explicit"] is True
    assert entry["base_mtime"] > 0


async def test_onboarding_endpoint(tmp_path: Path) -> None:
    make_arbiter(tmp_path)
    (tmp_path / "arbiter" / "README.md").write_text("Arbiter routes agents.\n")
    # RD-OB-DONE's work_item_chain rule needs min_links=2 for T-9; arbiter's
    # own fixture only contributes one (its `decisions` row). Add a second
    # link via Maestro's task DB, same as test_roadmap_endpoint's identical
    # RD-A rule in tests/test_roadmap.py.
    make_maestro(tmp_path)
    maestro_db = make_maestro_home(tmp_path)
    with sqlite3.connect(maestro_db) as conn:
        conn.execute(
            "INSERT INTO tasks VALUES ('T-9', 'Route me', 'done', 'auto', "
            "'2026-07-02T09:58:00', '2026-07-02T09:59:00', "
            "'2026-07-02T10:06:00')"
        )
    vault = tmp_path / "prograph-vault" / "authored" / "roadmaps"
    vault.mkdir(parents=True)
    (vault / "fixture.yaml").write_text(_ONBOARDING_ROADMAP)
    app = create_app(DispatcherConfig(roots=(tmp_path,), maestro_db=maestro_db))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        roadmap = (await client.get("/api/roadmap")).json()
        statuses = {i["id"]: i["computed_status"] for i in roadmap["items"]}
        assert statuses["RD-OB-DONE"] == "verified"  # fixture precondition

        resp = await client.get("/api/projects/arbiter/onboarding")
        assert resp.status_code == 200
        body = resp.json()
        assert body["project"]["description"] == "Arbiter routes agents."
        assert body["project"]["description_source"] == "readme"
        pos = body["roadmap_position"]
        assert pos["summary"]["project"] == "arbiter"
        ids = [n["id"] for n in body["next_items"]]
        assert ids == ["RD-OB-NEXT", "RD-OB-BLOCKED"]  # actionable first
        by_id = {n["id"]: n for n in body["next_items"]}
        assert by_id["RD-OB-NEXT"]["actionable"] is True
        assert by_id["RD-OB-BLOCKED"]["blocked_by"] == ["RD-OB-GHOST"]
        assert any("unknown dependency id" in w for w in body["warnings"])

        missing = await client.get("/api/projects/no-such/onboarding")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "unknown project: no-such"


def _fake_cli(tmp_path: Path, envelope: dict, sleep_s: float = 0.0) -> tuple[str, ...]:
    """A stand-in claude binary: reads stdin, prints the given envelope."""
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import json, sys, time\n"
        "_ = sys.stdin.read()\n"
        f"time.sleep({sleep_s})\n"
        f"print(json.dumps({envelope!r}))\n"
    )
    return ("python3", str(script))


def _envelope(result_payload: dict, **extra: object) -> dict:
    return {"type": "result", "result": json.dumps(result_payload), **extra}


def _suggest_workspace(tmp_path: Path) -> None:
    steward = tmp_path / "steward"
    steward.mkdir()
    (steward / "project.yaml").write_text(
        "project: steward\nspec_runner:\n  max_retries: 5\nworkstreams: []\n"
    )


async def _token(client: httpx.AsyncClient) -> str:
    return (await client.get("/api/actions/session")).json()["token"]


async def test_suggest_endpoint_happy_and_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _suggest_workspace(tmp_path)
    envelope = _envelope(
        {"suggestions": {"claude_model": {"value": "sonnet", "rationale": "r"}}},
        total_cost_usd=0.02,
    )
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(
        config,
        suggest_runner=SuggestRunner(config, command=_fake_cli(tmp_path, envelope)),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        token = await _token(client)
        mtime = (tmp_path / "steward" / "project.yaml").stat().st_mtime

        # 403 without token
        resp = await client.post(
            "/api/projects/steward/spec-runner-config/suggest",
            json={"base_mtime": mtime},
        )
        assert resp.status_code == 403

        # 200 happy path
        with caplog.at_level("INFO", logger="dispatcher.actions.spec_runner_config"):
            resp = await client.post(
                "/api/projects/steward/spec-runner-config/suggest",
                json={"base_mtime": mtime},
                headers={"X-Action-Token": token},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["suggestions"]["claude_model"]["value"] == "sonnet"
        assert body["cost_usd"] == 0.02
        assert "cli_version" not in body  # response_model_exclude pin
        assert any(
            "action=suggest project=steward outcome=ok" in r.message
            and "cost=0.02" in r.message
            for r in caplog.records
        )

        # 409 stale base_mtime
        resp = await client.post(
            "/api/projects/steward/spec-runner-config/suggest",
            json={"base_mtime": mtime - 10},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 409
        assert "config changed" in resp.json()["detail"]

        # 404 unknown project
        resp = await client.post(
            "/api/projects/nope/spec-runner-config/suggest",
            json={"base_mtime": 1.0},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 404

        # cancel with nothing in flight: idempotent 200 false
        resp = await client.post(
            "/api/projects/steward/spec-runner-config/suggest/cancel",
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 200 and resp.json() == {"cancelled": False}


async def test_suggest_unavailable_is_503(tmp_path: Path) -> None:
    _suggest_workspace(tmp_path)
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(
        config,
        suggest_runner=SuggestRunner(config, command=(str(tmp_path / "missing"),)),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        token = await _token(client)
        mtime = (tmp_path / "steward" / "project.yaml").stat().st_mtime
        resp = await client.post(
            "/api/projects/steward/spec-runner-config/suggest",
            json={"base_mtime": mtime},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 503


async def test_suggest_invalid_is_422_and_audited(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _suggest_workspace(tmp_path)
    envelope = {"type": "result", "result": "not json"}
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(
        config,
        suggest_runner=SuggestRunner(config, command=_fake_cli(tmp_path, envelope)),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        token = await _token(client)
        mtime = (tmp_path / "steward" / "project.yaml").stat().st_mtime
        with caplog.at_level("INFO", logger="dispatcher.actions.spec_runner_config"):
            resp = await client.post(
                "/api/projects/steward/spec-runner-config/suggest",
                json={"base_mtime": mtime},
                headers={"X-Action-Token": token},
            )
        assert resp.status_code == 422
        assert any(
            "action=suggest" in r.message and "outcome=invalid" in r.message
            for r in caplog.records
        )


async def test_suggest_availability_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DESIGN-904: gate the suggest button on CLI availability, not just click."""
    _suggest_workspace(tmp_path)
    config = DispatcherConfig(roots=(tmp_path,))

    # unavailable: no configured command and `claude` not on PATH
    monkeypatch.setattr("shutil.which", lambda _: None)
    app = create_app(config, suggest_runner=SuggestRunner(config))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/api/spec-runner-config/suggest-availability")
        assert resp.status_code == 200
        body = resp.json()
        assert body["available"] is False
        assert body["detail"] == "claude CLI not found on PATH"

    # available: injected fake CLI resolves without touching shutil.which
    envelope = _envelope({"suggestions": {}})
    app = create_app(
        config,
        suggest_runner=SuggestRunner(config, command=_fake_cli(tmp_path, envelope)),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/api/spec-runner-config/suggest-availability")
        assert resp.status_code == 200
        assert resp.json() == {"available": True, "detail": None}


def test_static_index_pins_suggest_availability_endpoint() -> None:
    static_path = (
        Path(__file__).parent.parent / "dispatcher" / "server" / "static" / "index.html"
    )
    assert "suggest-availability" in static_path.read_text()
