"""Integration tests for the HTTP API over a fixtures root."""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

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


async def test_the_contract_row_wire_shape_is_pinned(tmp_path: Path) -> None:
    """`/api/contracts` is consumed by the SPA and the VS Code extension.

    `kind` is part of the shape, not a detail: two rows can share one contract
    name while answering different questions, so a client that drops the field
    cannot tell an integrity verdict from an upstream observation — and would
    render "n/a" beside "in sync" for the same contract with no explanation.
    """
    async with _client(tmp_path) as client:
        contracts = (await client.get("/api/contracts")).json()
    assert set(contracts[0]) == {
        "name",
        "canonical_path",
        "vendored_path",
        "kind",
        "in_sync",
        "detail",
    }
    assert {c["kind"] for c in contracts} <= {
        "vendored_integrity",
        "upstream_drift",
        "listing",
    }
    plan_fields = [c for c in contracts if c["name"] == "plan-fields-v1"]
    by_kind = {c["kind"]: c for c in plan_fields}
    # both verdicts are served, side by side and separately labelled
    assert set(by_kind) == {"vendored_integrity", "upstream_drift"}
    assert by_kind["vendored_integrity"]["in_sync"] is True
    # no canon checkout in this fixture: unknown, and it says so
    assert by_kind["upstream_drift"]["in_sync"] is None
    assert "no canon" in by_kind["upstream_drift"]["detail"]


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
    assert data["report"]["top_line"] in ("ok", "sync-first", "no-data", "unknown")
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
    assert verdict["verdict"] in ("sync-first", "unknown")  # unknown если stale


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


async def test_spec_runner_config_view_is_directory_keyed_not_name_keyed(
    tmp_path: Path, monkeypatch
) -> None:
    """The route matches the ON-DISK clone dirname, never a display name.

    `project.yaml`'s own `project:` field is a display name that can differ
    from the directory it lives in (the collector reports one such name,
    "Maestro", for a clone checked out as "maestro" — see CLAUDE.md). The
    server side of this contract must key on `Path(project_yaml_path)
    .parent.name` only: a query by the directory succeeds, and a query by
    the display name recorded inside the very same file must 404, not
    quietly resolve to the same config.
    """
    import subprocess

    repo = tmp_path / "repo-dir"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "project.yaml").write_text(
        "project: DisplayName\nspec_runner:\n  max_retries: 5\nworkstreams: []\n"
    )
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(config)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        by_dir = await client.get("/api/projects/repo-dir/spec-runner-config")
        assert by_dir.status_code == 200
        assert by_dir.json()["typed"]["max_retries"]["value"] == 5

        by_display_name = await client.get(
            "/api/projects/DisplayName/spec-runner-config"
        )
        assert by_display_name.status_code == 404


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


async def test_update_spec_runner_config_response_carries_no_file_content(
    tmp_path: Path,
) -> None:
    """The refusal reaches an HTTP body. Assert on the body, not the
    exception."""
    import subprocess

    secret = "s3cr3t-telegram-token-ABC123"
    repo = tmp_path / "alpha"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "project.yaml").write_text(
        f'a: "{secret}"\na: 2\nspec_runner:\n  max_retries: 3\nworkstreams: []\n'
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
        assert resp.json()["ok"] is False
        assert secret not in resp.text


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


HEAD = "a" * 40


async def test_merge_and_sync_requires_the_action_token(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.post(
            "/api/actions/merge-and-sync",
            json={"dir": "alpha", "pr": 7, "if_head": HEAD},
        )
        assert resp.status_code == 403


async def test_merge_and_sync_returns_the_composite_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dispatcher.core.actions import ActionOutcome, ActionRunner

    def fake_merge_and_sync(
        self: ActionRunner, repo_dir: str, pr: int, if_head: str
    ) -> ActionOutcome:
        return ActionOutcome(
            action="merge-and-sync",
            dir=repo_dir,
            ok=True,
            merged=True,
            local_sync="ok",
        )

    monkeypatch.setattr(ActionRunner, "merge_and_sync", fake_merge_and_sync)
    async with _client(tmp_path) as client:
        token = await _token(client)
        resp = await client.post(
            "/api/actions/merge-and-sync",
            json={"dir": "alpha", "pr": 7, "if_head": HEAD},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "merge-and-sync"
        assert body["merged"] is True
        assert body["local_sync"] == "ok"


async def test_merged_tri_state_survives_fastapi_serialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asserted on the WIRE, not on an ActionOutcome.

    The tri-state is the property the whole gate rests on, and the layer that
    can drop it silently is this one: `response_model_exclude_none=True` on
    the endpoint removes `merged` from the response body entirely while every
    model-level test still passes. `null` and an absent key are the same thing
    to the screen only by accident (`index.html` checks `undefined` too) — the
    contract is that the key is present.
    """
    from dispatcher.core.actions import ActionOutcome, ActionRunner

    # pr 7 = transport failure (unknown), pr 8 = parsed gate refusal
    def fake_merge_and_sync(
        self: ActionRunner, repo_dir: str, pr: int, if_head: str
    ) -> ActionOutcome:
        return ActionOutcome(
            action="merge-and-sync",
            dir=repo_dir,
            ok=False,
            merged=None if pr == 7 else False,
            local_sync="not_attempted",
        )

    monkeypatch.setattr(ActionRunner, "merge_and_sync", fake_merge_and_sync)
    async with _client(tmp_path) as client:
        token = await _token(client)
        for pr, expected in ((7, None), (8, False)):
            resp = await client.post(
                "/api/actions/merge-and-sync",
                json={"dir": "alpha", "pr": pr, "if_head": HEAD},
                headers={"X-Action-Token": token},
            )
            assert resp.status_code == 200
            body = json.loads(resp.text)
            assert "merged" in body, f"PR {pr}: the key was dropped from the wire"
            assert body["merged"] is expected


async def test_merge_and_sync_maps_busy_to_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dispatcher.core.actions import ActionBusyError, ActionOutcome, ActionRunner

    def busy(self: ActionRunner, repo_dir: str, pr: int, if_head: str) -> ActionOutcome:
        raise ActionBusyError("alpha: action already in flight")

    monkeypatch.setattr(ActionRunner, "merge_and_sync", busy)
    async with _client(tmp_path) as client:
        token = await _token(client)
        resp = await client.post(
            "/api/actions/merge-and-sync",
            json={"dir": "alpha", "pr": 7, "if_head": HEAD},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 409


async def test_merge_and_sync_maps_rejection_to_422(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        token = await _token(client)
        resp = await client.post(
            "/api/actions/merge-and-sync",
            json={"dir": "../etc", "pr": 7, "if_head": HEAD},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 422


async def test_pr_detail_is_readable_without_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dispatcher.core.actions import ActionOutcome, ActionRunner

    def fake_pr_detail(self: ActionRunner, repo_dir: str, pr: int) -> ActionOutcome:
        return ActionOutcome(
            action="pr-detail", dir=repo_dir, ok=True, pr_detail={"number": pr}
        )

    monkeypatch.setattr(ActionRunner, "pr_detail", fake_pr_detail)
    async with _client(tmp_path) as client:
        resp = await client.get("/api/pr-detail", params={"dir": "alpha", "pr": 7})
        assert resp.status_code == 200
        assert resp.json()["pr_detail"]["number"] == 7


async def test_pr_detail_maps_rejection_to_422(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/api/pr-detail", params={"dir": "../etc", "pr": 7})
        assert resp.status_code == 422


async def test_post_merge_sync_endpoint_retries_the_local_half(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dispatcher.core.actions import ActionOutcome, ActionRunner

    def fake_run(self: ActionRunner, action: str, repo_dir: str) -> ActionOutcome:
        return ActionOutcome(action=action, dir=repo_dir, ok=True, local_sync="ok")

    monkeypatch.setattr(ActionRunner, "run", fake_run)
    async with _client(tmp_path) as client:
        token = await _token(client)
        resp = await client.post(
            "/api/actions/post-merge-sync",
            json={"dir": "alpha"},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["local_sync"] == "ok"


async def test_request_task_requires_the_action_token(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        response = await client.post(
            "/api/actions/request-task",
            json={"dir": "alpha", "slug": "wanted", "title": "t", "prose": "p"},
        )
    assert response.status_code == 403


async def test_request_task_returns_the_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dispatcher.core.actions import ActionOutcome, ActionRunner

    monkeypatch.setattr(
        ActionRunner,
        "request_task",
        lambda self, repo_dir, **kw: ActionOutcome(
            action="request-task",
            dir=repo_dir,
            ok=True,
            created=True,
            issue={"number": 9, "url": "https://x/9"},
        ),
    )
    async with _client(tmp_path) as client:
        token = await _token(client)
        response = await client.post(
            "/api/actions/request-task",
            json={"dir": "alpha", "slug": "wanted", "title": "t", "prose": "p"},
            headers={"X-Action-Token": token},
        )
    assert response.status_code == 200
    assert response.json()["created"] is True


async def test_request_task_never_takes_from_from_the_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`from` lands in the issue's structural block; the client cannot set it."""
    seen: dict[str, Any] = {}

    from dispatcher.core.actions import ActionOutcome, ActionRunner

    def capture(self: ActionRunner, repo_dir: str, **kw: Any) -> ActionOutcome:
        seen.update(kw)
        return ActionOutcome(action="request-task", dir=repo_dir, ok=True, created=True)

    monkeypatch.setattr(ActionRunner, "request_task", capture)
    async with _client(tmp_path) as client:
        token = await _token(client)
        await client.post(
            "/api/actions/request-task",
            json={
                "dir": "alpha",
                "slug": "wanted",
                "title": "t",
                "prose": "p",
                "sender": "spoofed",
                "from": "spoofed",
            },
            headers={"X-Action-Token": token},
        )
    assert seen["sender"] == "dispatcher"


async def test_request_task_maps_busy_to_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dispatcher.core.actions import ActionBusyError, ActionRunner

    def busy(self: ActionRunner, repo_dir: str, **kw: Any) -> None:
        raise ActionBusyError("alpha: action already in flight")

    monkeypatch.setattr(ActionRunner, "request_task", busy)
    async with _client(tmp_path) as client:
        token = await _token(client)
        response = await client.post(
            "/api/actions/request-task",
            json={"dir": "alpha", "slug": "wanted", "title": "t", "prose": "p"},
            headers={"X-Action-Token": token},
        )
    assert response.status_code == 409


async def test_request_task_maps_rejection_to_422(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        token = await _token(client)
        response = await client.post(
            "/api/actions/request-task",
            json={"dir": "../etc", "slug": "wanted", "title": "t", "prose": "p"},
            headers={"X-Action-Token": token},
        )
    assert response.status_code == 422


async def test_issue_lookup_preserves_null_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`matches=None` means github-checker could not read the inbox
    exhaustively (200-issue cap hit, or a candidate didn't map) — a real
    `[]` means it positively confirmed nothing is there. Collapsing null
    into [] on the wire is exactly how a taken slug ends up looking free
    and a duplicate issue gets filed."""
    from dispatcher.core.actions import ActionOutcome, ActionRunner

    monkeypatch.setattr(
        ActionRunner,
        "issue_lookup",
        lambda self, repo_dir, slug: ActionOutcome(
            action="issue-lookup", dir=repo_dir, ok=True, matches=None, malformed=[]
        ),
    )
    async with _client(tmp_path) as client:
        response = await client.get(
            "/api/issue-lookup", params={"dir": "alpha", "slug": "wanted"}
        )
    body = response.json()
    assert "matches" in body
    assert body["matches"] is None


async def test_issue_lookup_preserves_null_malformed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same null-vs-`[]` distinction as `matches`, mirrored for `malformed`:
    None means the read was not exhaustive, [] means it positively found
    no malformed candidates."""
    from dispatcher.core.actions import ActionOutcome, ActionRunner

    monkeypatch.setattr(
        ActionRunner,
        "issue_lookup",
        lambda self, repo_dir, slug: ActionOutcome(
            action="issue-lookup", dir=repo_dir, ok=True, matches=[], malformed=None
        ),
    )
    async with _client(tmp_path) as client:
        response = await client.get(
            "/api/issue-lookup", params={"dir": "alpha", "slug": "wanted"}
        )
    body = response.json()
    assert "malformed" in body
    assert body["malformed"] is None


async def test_issue_lookup_is_readable_without_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dispatcher.core.actions import ActionOutcome, ActionRunner

    monkeypatch.setattr(
        ActionRunner,
        "issue_lookup",
        lambda self, repo_dir, slug: ActionOutcome(
            action="issue-lookup", dir=repo_dir, ok=True, matches=[], malformed=[]
        ),
    )
    async with _client(tmp_path) as client:
        response = await client.get(
            "/api/issue-lookup", params={"dir": "alpha", "slug": "wanted"}
        )
    assert response.status_code == 200
    assert response.json()["matches"] == []


async def test_issue_lookup_maps_rejection_to_422(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        response = await client.get(
            "/api/issue-lookup", params={"dir": "../etc", "slug": "wanted"}
        )
    assert response.status_code == 422


def _isinstance_strict(value: object, expected: type | tuple[type, ...]) -> bool:
    """Like `isinstance`, but a `bool` never satisfies a plain `int` check.

    Python's `bool` is an `int` subclass, so plain `isinstance(True, int)` is
    True — but JS's `Number.isInteger(true)` is False. Without this, the two
    mirrors (this file's dicts and `MG_REQUIRED`/`MG_FILE_ITEM_REQUIRED` in
    index.html) would silently mean different things for every int-typed
    field (M-2), and a real `additions: true` would pass here but fail there.
    """
    if isinstance(value, bool):
        expected_types = expected if isinstance(expected, tuple) else (expected,)
        if bool not in expected_types:
            return False
    return isinstance(value, expected)


# Mirrors MG_REQUIRED in index.html at BOTH levels: top-level fields here,
# plus PR_DETAIL_FILE_ITEM_REQUIRED / PR_DETAIL_THREAD_ITEM_REQUIRED further
# down mirror MG_FILE_ITEM_REQUIRED / MG_THREAD_ITEM_REQUIRED for what's
# inside `files` / `review_threads`. If github-checker's payload changes
# shape at either level, this fails here — loudly, in CI, with the offending
# index and field named — instead of silently blanking the merge-gate screen
# at runtime.
PR_DETAIL_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "number": int,
    "title": str,
    "url": str,
    "state": str,
    "is_draft": bool,
    "mergeable": str,
    "head_branch": str,
    "head_sha": str,
    "base_branch": str,
    "checks": list,
    "files": list,
    "review_threads": list,
}

# Legitimately nullable, so checked for PRESENCE plus type — see the matching
# comment in index.html. `review_decision` especially: its predicate passes on
# null, so a field that vanished would read as "no review required".
PR_DETAIL_NULLABLE: dict[str, type | tuple[type, ...]] = {
    "review_decision": str,
    "allows_squash": bool,
}

# Mirrors MG_FILE_ITEM_REQUIRED in index.html: `files` is validated as a
# container above (PR_DETAIL_REQUIRED["files"] == list), this validates what's
# inside it. `additions`/`deletions` reach the DOM unescaped-then-esc()'d at
# render time — a wrong type there was a real XSS-shaped finding, not just
# display corruption, so item shape matters as much as container shape.
PR_DETAIL_FILE_ITEM_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "path": str,
    "additions": int,
    "deletions": int,
}

# Mirrors MG_THREAD_ITEM_REQUIRED in index.html. `is_resolved` feeds the
# `threads-resolved` gate predicate; it is required and non-nullable.
PR_DETAIL_THREAD_ITEM_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "is_resolved": bool,
}

# `author`/`excerpt` are rendered but nullable — same PRESENCE-plus-type
# reasoning as PR_DETAIL_NULLABLE above: an absent key must not be conflated
# with a genuinely-null value.
PR_DETAIL_THREAD_ITEM_NULLABLE: dict[str, type | tuple[type, ...]] = {
    "author": str,
    "excerpt": str,
}

# Mirrors MG_CHECK_ITEM_REQUIRED in index.html (I-4): `checks` was the one
# array the item-level sweep missed — MG_PREDICATES reads `c.state` on every
# entry with no guard, so `checks: [null]` passed validation and then threw
# mid-render in the JS, past the "cannot read PR" branch entirely.
PR_DETAIL_CHECK_ITEM_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "state": str,
}


def _item_shape_problems(
    items: list[Any],
    label: str,
    required: dict[str, type | tuple[type, ...]],
    nullable: dict[str, type | tuple[type, ...]] | None = None,
) -> list[str]:
    """Per-item diagnostics, same index+field shape as mgArrayItemProblems in
    index.html — a drifted item is diagnosable from the failure message, not
    just "files is wrong somehow"."""
    nullable = nullable or {}
    problems: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            problems.append(f"{label}[{i}] is not an object")
            continue
        problems += [
            f"{label}[{i}].{key} missing or wrong type"
            for key, expected in required.items()
            if not _isinstance_strict(item.get(key), expected)
        ]
        problems += [
            f"{label}[{i}].{key} missing or wrong type"
            for key, expected in nullable.items()
            if key not in item
            or not (item[key] is None or _isinstance_strict(item[key], expected))
        ]
    return problems


# Captured 2026-07-30 via:
#   uv run --project ../github-checker github-checker pr-detail \
#     ../github-checker 14 --diff-lines 5 --file-limit 5
# The limiting flags matter: without them this fixture is ~85KB (a full diff
# with a ~2000-line plan doc embedded) instead of ~4KB. Content is not part
# of the shape this test pins, so keep recapturing it small.
# `github-checker --version` doesn't exist; the pin is the producer commit,
# confirmed via `git -C ../github-checker rev-parse HEAD` == f05cf8d (HEAD at
# capture time, PR #14's merge commit).
FIXTURE = Path(__file__).parent / "fixtures" / "pr_detail_github_checker_f05cf8d.json"


def test_real_pr_detail_payload_has_every_field_the_console_reads() -> None:
    """Consumer check against github-checker's ACTUAL output, not a fake.

    Provisional-adapter guard: `pr_detail` is an opaque passthrough until
    `contracts/actions/v1` is published and vendored
    (TODO @id:vendor-contracts-actions-v1).
    """
    envelope = json.loads(FIXTURE.read_text())
    assert envelope["ok"] is True
    detail = envelope["pr_detail"]
    missing = [
        key
        for key, expected in PR_DETAIL_REQUIRED.items()
        if not _isinstance_strict(detail.get(key), expected)
    ]
    # M-1: mirrors the URL-scheme guard on MG_REQUIRED's `url` predicate —
    # esc() blocks attribute breakout, not the scheme, so a `javascript:`
    # URL from the passthrough would render as a live link.
    if isinstance(detail.get("url"), str) and not detail["url"].startswith("https://"):
        missing.append("url")
    # `head_sha` is `str && length > 0` in MG_REQUIRED — a type-only check here
    # would let the two mirrors mean different things for an empty SHA.
    if isinstance(detail.get("head_sha"), str) and not detail["head_sha"]:
        missing.append("head_sha")
    # Nullable fields: the KEY must exist, and its value must be null or the
    # expected type. `.get()` would conflate "absent" with "null" — which for
    # review_decision is the difference between "cannot read" and "no review
    # required".
    missing += [
        key
        for key, expected in PR_DETAIL_NULLABLE.items()
        if key not in detail
        or not (detail[key] is None or _isinstance_strict(detail[key], expected))
    ]
    # Item-level checks only make sense once the container is confirmed a
    # real list — the PR_DETAIL_REQUIRED check above already gates that, but
    # re-check defensively rather than assume dict-comprehension order.
    if isinstance(detail.get("files"), list):
        missing += _item_shape_problems(
            detail["files"], "files", PR_DETAIL_FILE_ITEM_REQUIRED
        )
    if isinstance(detail.get("review_threads"), list):
        missing += _item_shape_problems(
            detail["review_threads"],
            "review_threads",
            PR_DETAIL_THREAD_ITEM_REQUIRED,
            PR_DETAIL_THREAD_ITEM_NULLABLE,
        )
    if isinstance(detail.get("checks"), list):
        missing += _item_shape_problems(
            detail["checks"], "checks", PR_DETAIL_CHECK_ITEM_REQUIRED
        )
    assert missing == [], f"github-checker payload no longer provides: {missing}"


ISSUE_REF_REQUIRED: dict[str, type | tuple[type, ...]] = {
    "number": int,
    "title": str,
    "state": str,
    "url": str,
    "author": str,
    "labels": list,
}

# Captured 2026-07-31 via:
#   uv run --project ../github-checker github-checker issue-lookup \
#     ../prograph-vault --slug amend-adr-eco-004-d1-task-authoring
# Pinned to the producer commit: `git -C ../github-checker rev-parse HEAD` ==
# 4532a8a (master, both inbox-issue verbs merged). The slug has a real,
# closed inbox issue (prograph-vault#54) — a state the console must also
# render correctly, not just the open case.
ISSUE_FIXTURE = (
    Path(__file__).parent / "fixtures" / "issue_lookup_github_checker_4532a8a.json"
)


def test_real_issue_lookup_payload_has_every_field_the_console_reads() -> None:
    """Consumer check against github-checker's ACTUAL output, not a fake.

    Provisional-adapter guard: the issue payload is an opaque passthrough
    until contracts/actions/v1 is published and vendored
    (TODO @id:vendor-contracts-actions-v1).
    """
    envelope = json.loads(ISSUE_FIXTURE.read_text())
    assert envelope["ok"] is True
    assert envelope["matches"], "fixture must pin a slug that actually exists"
    for ref in envelope["matches"]:
        missing = [
            key
            for key, expected in ISSUE_REF_REQUIRED.items()
            if not isinstance(ref.get(key), expected)
        ]
        assert missing == [], f"github-checker payload no longer provides: {missing}"
    # The producer normalises `state` to lowercase (`gh issue list` itself
    # emits OPEN/CLOSED); assert the lowercase form rather than adapting to
    # whatever the fixture happens to contain — an uppercase value here would
    # be a producer regression to report, not a shape to accept.
    assert all(ref["state"] == ref["state"].lower() for ref in envelope["matches"])


async def test_merge_gate_markup_is_served(tmp_path: Path) -> None:
    """The brief's sample used a sync `client` fixture that doesn't exist here
    (Task 3's plan-defect pattern, `progress.md`); this file is anyio-async
    throughout, so it uses `_client()` like every other endpoint test."""
    async with _client(tmp_path) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="merge-gate"' in body
    assert "openMergeGate" in body
    assert "/api/actions/merge-and-sync" in body


async def test_task_authoring_markup_is_served(tmp_path: Path) -> None:
    """Same convention as test_merge_gate_markup_is_served above: the
    module-level `pytestmark = pytest.mark.anyio` already covers this test,
    so no per-test decorator is needed (the brief's sample used both a
    decorator and a sync `client` fixture that doesn't exist here)."""
    async with _client(tmp_path) as client:
        resp = await client.get("/")
    assert resp.status_code == 200
    body = resp.text
    assert 'id="task-authoring"' in body
    assert "taCheckSlug" in body
    assert "/api/actions/request-task" in body


# --- F-1 at the HTTP boundary ----------------------------------------------
#
# A status code alone would not settle this: the whole reason F-1 mattered is
# that the attempt vanished from the audit log, so each test asserts the line
# as well. Driven through the endpoints on purpose — the defect was reachable
# from a request body, and a runner-level test would not have shown the 500.

_NUL = "\x00"


def _make_git_repo(tmp_path: Path, name: str = "nultest") -> str:
    """A real workspace repo, so `_target` passes and the control-character
    refusal is what the request actually meets."""
    (tmp_path / name / ".git").mkdir(parents=True, exist_ok=True)
    return name


async def test_issue_lookup_with_a_nul_byte_is_refused_and_audited(
    tmp_path: Path, caplog
) -> None:
    """A NUL in the query is a 422 refusal with an audit line, never a 500.

    `subprocess.run` raises `ValueError("embedded null byte")` while
    validating argv — before it forks. Uncaught, that surfaced as a 500 AND
    left no audit line at all, which is the part that breaks ADR-ECO-004a
    D1a-4 ("every attempt leaves a line").
    """
    repo = _make_git_repo(tmp_path)
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        async with _client(tmp_path) as client:
            resp = await client.get(
                "/api/issue-lookup", params={"dir": repo, "slug": f"a{_NUL}b"}
            )
    assert resp.status_code != 500
    assert resp.status_code == 422
    assert "control character" in resp.json()["detail"]
    assert "action=issue-lookup" in caplog.text
    assert "rejected=" in caplog.text


async def test_request_task_with_a_nul_byte_is_refused_and_audited(
    tmp_path: Path, caplog
) -> None:
    """Same at the mutating endpoint, with a real NUL in the POST body.

    JSON permits `\\u0000` in a string, so this is what an HTTP client can
    actually send. The audit line must say `created=False`: nothing ran, so
    "unknown" would overstate our ignorance.
    """
    repo = _make_git_repo(tmp_path)
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        async with _client(tmp_path) as client:
            token = (await client.get("/api/actions/session")).json()["token"]
            resp = await client.post(
                "/api/actions/request-task",
                json={
                    "dir": repo,
                    "slug": f"wan{_NUL}ted",
                    "title": "t",
                    "prose": "because Y, done when Z",
                },
                headers={"X-Action-Token": token},
            )
    assert resp.status_code != 500
    assert resp.status_code == 422
    assert "control character" in resp.json()["detail"]
    assert "action=request-task" in caplog.text
    assert "created=False" in caplog.text


async def test_request_task_with_a_nul_title_is_refused_and_audited(
    tmp_path: Path, caplog
) -> None:
    """`title` is a structural argv element too (`--title <title>`), and a
    validator that covered only `slug` would leave the same 500 reachable
    one field over."""
    repo = _make_git_repo(tmp_path)
    with caplog.at_level(logging.INFO, logger="dispatcher.actions"):
        async with _client(tmp_path) as client:
            token = (await client.get("/api/actions/session")).json()["token"]
            resp = await client.post(
                "/api/actions/request-task",
                json={
                    "dir": repo,
                    "slug": "wanted",
                    "title": f"ti{_NUL}tle",
                    "prose": "because Y, done when Z",
                },
                headers={"X-Action-Token": token},
            )
    assert resp.status_code != 500
    assert resp.status_code == 422
    assert "action=request-task" in caplog.text
    assert "created=False" in caplog.text


# --- ActionOutcome wire shape (golden) --------------------------------
#
# `ActionOutcome` is the `response_model` of eight endpoints, so its field
# set *is* the HTTP contract the SPA and the VS Code extension read. Task
# 3 rewires what fills it — subprocess → `ingest` → typed `Ingested` →
# explicit legacy projection — and the point of that shape is that none
# of it reaches the wire. These pin the wire so the refactor has to prove
# it, rather than being believed.

_ACTION_OUTCOME_WIRE_KEYS = sorted(
    [
        "action",
        "dir",
        "ok",
        "detail",
        "error",
        "pr_url",
        "local_behind",
        "local_dirty",
        "branch",
        "base_branch",
        "commit_sha",
        "changed_paths",
        "merged",
        "local_sync",
        "gate_failed",
        "pr_detail",
        "matches",
        "malformed",
        "created",
        "issue",
        "phase",
    ]
)


async def test_the_action_outcome_wire_shape_is_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact JSON an action endpoint emits, including the nulls.

    Unset optional fields serialise as `null` because no route sets
    `response_model_exclude_unset=True`. That is the current contract, and
    turning it on would be a wire change for the SPA and the extension —
    so it is pinned here rather than left as an implementation detail that
    a later "cleanup" could flip silently."""
    from dispatcher.core.actions import ActionOutcome, ActionRunner

    def fake_pr_detail(self: ActionRunner, repo_dir: str, pr: int) -> ActionOutcome:
        return ActionOutcome(action="pr-detail", dir=repo_dir, ok=True)

    monkeypatch.setattr(ActionRunner, "pr_detail", fake_pr_detail)
    async with _client(tmp_path) as client:
        resp = await client.get("/api/pr-detail", params={"dir": "alpha", "pr": 7})
    assert resp.status_code == 200
    body = resp.json()
    assert sorted(body) == _ACTION_OUTCOME_WIRE_KEYS
    assert body["action"] == "pr-detail"
    assert body["ok"] is True
    # every field the outcome did not set comes back as an explicit null
    for key in _ACTION_OUTCOME_WIRE_KEYS:
        if key not in {"action", "dir", "ok"}:
            assert body[key] is None, key


def test_every_action_endpoint_shares_one_response_model(tmp_path: Path) -> None:
    """Eight routes declare `response_model=ActionOutcome`. Asserting it
    off the route table rather than by calling each one is what catches a
    single route drifting to a different model — the failure a per-route
    golden would miss, because it would simply pin the drift."""
    from fastapi.routing import APIRoute

    from dispatcher.core.actions import ActionOutcome

    make_atp(tmp_path)
    config = DispatcherConfig(roots=(tmp_path,), maestro_db=make_maestro_home(tmp_path))
    paths = {
        route.path
        for route in create_app(config).routes
        if isinstance(route, APIRoute) and route.response_model is ActionOutcome
    }
    assert paths == {
        "/api/actions/pull",
        "/api/actions/create-pr",
        "/api/pr-detail",
        "/api/actions/merge-and-sync",
        "/api/actions/post-merge-sync",
        "/api/issue-lookup",
        "/api/actions/request-task",
        "/api/actions/update-spec-runner-config",
    }
