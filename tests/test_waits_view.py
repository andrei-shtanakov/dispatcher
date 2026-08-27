"""Waits view (spec §5): edges vs loose text, stale as a state, honest source.

The invariants here are the ones a refactor loses silently: a stale edge
disappearing from `edges` reads as "no obligation"; a skipped manifest repo
turns NO-TODO into UNRESOLVABLE; a partial fleet calling itself `read`.
"""

from __future__ import annotations

from pathlib import Path

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.waits import build_waits

NOW = "2026-08-27T10:00:00Z"


def _workspace(
    tmp_path: Path, todos: dict[str, str | bytes | None]
) -> DispatcherConfig:
    """Manifest + checkouts. None = checkout without TODO.md; bytes = raw file."""
    umbrella = tmp_path / "ai-orchestrators-workspace"
    umbrella.mkdir(parents=True)
    manifest = ['schema_version = "0.3.0"']
    for name, text in todos.items():
        repo = tmp_path / name
        (repo / ".git").mkdir(parents=True)
        if isinstance(text, bytes):
            (repo / "TODO.md").write_bytes(text)
        elif text is not None:
            (repo / "TODO.md").write_text(text, encoding="utf-8")
        manifest.append(f'[apps.{name}]\ngit_dir = "{name}"')
    (umbrella / "workspace-manifest.toml").write_text(
        "\n".join(manifest) + "\n", encoding="utf-8"
    )
    return DispatcherConfig(roots=(tmp_path,))


def _declare(tmp_path: Path, name: str) -> None:
    """Add a manifest entry with no checkout at all."""
    manifest = tmp_path / "ai-orchestrators-workspace" / "workspace-manifest.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + f'[apps.{name}]\ngit_dir = "{name}"\n',
        encoding="utf-8",
    )


def test_canonical_reference_becomes_a_waiting_edge(tmp_path: Path) -> None:
    config = _workspace(
        tmp_path,
        {
            "alpha": "- [ ] consumer @id:cons @blocked_by:todo://beta/prod\n",
            "beta": "- [ ] producer @id:prod\n",
        },
    )
    view = build_waits(config, now=NOW)
    assert [(e.source.node_id, e.target.node_id, e.state) for e in view.edges] == [
        ("todo://alpha/cons", "todo://beta/prod", "waiting")
    ]
    assert view.todo_plane.state == "read"
    assert view.generated_at == NOW


def test_closed_target_keeps_the_edge_as_stale(tmp_path: Path) -> None:
    # The delivered wait must stay VISIBLE: dropping the edge is exactly the
    # defect the surface exists to cure (the maestro#216/deployer incident).
    config = _workspace(
        tmp_path,
        {
            "alpha": "- [ ] consumer @id:cons @blocked_by:todo://beta/prod\n",
            "beta": "- [x] producer @id:prod\n",
        },
    )
    view = build_waits(config, now=NOW)
    assert [e.state for e in view.edges] == ["stale"]
    # ...and the same fact is NOT double-reported as a finding
    assert all(f.code != "PF-BLOCKER-STALE" for f in view.findings)


def test_unique_legacy_match_is_text_not_an_edge(tmp_path: Path) -> None:
    config = _workspace(
        tmp_path,
        {
            "alpha": "- [ ] consumer @id:cons @blocked_by:beta#prod\n",
            "beta": "- [ ] producer @id:prod\n",
        },
    )
    view = build_waits(config, now=NOW)
    assert view.edges == []
    assert [(r.raw_ref, r.normalized) for r in view.loose_refs] == [
        ("beta#prod", "beta#prod")
    ]
    # the package stays deliberately silent on a unique match — no finding
    assert view.findings == []


def test_ambiguous_legacy_gets_a_finding(tmp_path: Path) -> None:
    config = _workspace(
        tmp_path,
        {
            "alpha": "- [ ] consumer @id:cons @blocked_by:beta#thing\n",
            "beta": "- [ ] a thing @id:one\n- [ ] same thing @id:two\n",
        },
    )
    view = build_waits(config, now=NOW)
    assert view.edges == []
    assert len(view.loose_refs) == 1
    assert [f.code for f in view.findings] == ["PF-LEGACY-AMBIGUOUS"]


def test_dangling_canonical_reference(tmp_path: Path) -> None:
    config = _workspace(
        tmp_path,
        {
            "alpha": "- [ ] consumer @id:cons @blocked_by:todo://beta/ghost\n",
            "beta": "- [ ] producer @id:prod\n",
        },
    )
    view = build_waits(config, now=NOW)
    assert view.edges == []
    assert [r.raw_ref for r in view.loose_refs] == ["todo://beta/ghost"]
    assert [f.code for f in view.findings] == ["PF-ID-DANGLING"]


def test_reference_into_a_missing_checkout(tmp_path: Path) -> None:
    config = _workspace(
        tmp_path,
        {"alpha": "- [ ] consumer @id:cons @blocked_by:todo://gamma/x\n"},
    )
    _declare(tmp_path, "gamma")
    view = build_waits(config, now=NOW)
    assert view.todo_plane.state == "partial"
    assert [(a.repo, a.reason) for a in view.absent_repos] == [("gamma", "no checkout")]
    assert [f.code for f in view.findings] == ["PF-BLOCKER-UNRESOLVABLE"]
    assert [r.raw_ref for r in view.loose_refs] == ["todo://gamma/x"]


def test_checkout_without_todo_is_no_todo_not_unresolvable(tmp_path: Path) -> None:
    # Inputs must carry EVERY manifest repo: skipping the empty checkout (as
    # the epics plane does) would collapse NO-TODO into UNRESOLVABLE.
    config = _workspace(
        tmp_path,
        {
            "alpha": "- [ ] consumer @id:cons @blocked_by:todo://gamma/x\n",
            "gamma": None,
        },
    )
    view = build_waits(config, now=NOW)
    assert view.todo_plane.state == "partial"
    assert [(a.repo, a.reason) for a in view.absent_repos] == [("gamma", "no TODO.md")]
    assert [f.code for f in view.findings] == ["PF-BLOCKER-NO-TODO"]


def test_unreadable_todo_is_disclosed_and_does_not_kill_the_pass(
    tmp_path: Path,
) -> None:
    config = _workspace(
        tmp_path,
        {
            "alpha": "- [ ] consumer @id:cons\n",
            "gamma": b"\xff\xfe\x00broken",
        },
    )
    view = build_waits(config, now=NOW)
    assert view.todo_plane.state == "partial"
    assert [(a.repo, a.reason) for a in view.absent_repos] == [
        ("gamma", "unreadable: UnicodeDecodeError")
    ]
    assert view.todo_plane.repos_read == 1


def test_missing_manifest_is_unavailable_with_http_semantics_left_to_route(
    tmp_path: Path,
) -> None:
    view = build_waits(DispatcherConfig(roots=(tmp_path,)), now=NOW)
    assert view.todo_plane.state == "unavailable"
    assert view.edges == [] and view.loose_refs == [] and view.triggers == []


def test_unparseable_manifest_is_unavailable(tmp_path: Path) -> None:
    umbrella = tmp_path / "ai-orchestrators-workspace"
    umbrella.mkdir(parents=True)
    (umbrella / "workspace-manifest.toml").write_text("not = [valid", encoding="utf-8")
    view = build_waits(DispatcherConfig(roots=(tmp_path,)), now=NOW)
    assert view.todo_plane.state == "unavailable"
    assert view.todo_plane.detail is not None


def test_whole_fleet_without_todos_is_unavailable_not_partial(
    tmp_path: Path,
) -> None:
    # The boundary the pair review caught: checkouts everywhere, zero TODO.md
    # anywhere → the plane was never read, and `partial` would overstate it.
    config = _workspace(tmp_path, {"alpha": None, "beta": None})
    view = build_waits(config, now=NOW)
    assert view.todo_plane.state == "unavailable"
    assert view.todo_plane.detail == "no TODO.md checked out"


def test_triggers_carry_open_items_only(tmp_path: Path) -> None:
    config = _workspace(
        tmp_path,
        {
            "alpha": (
                '- [ ] armed @id:armed @trigger:"docker появился"\n'
                '- [x] done @id:done @trigger:"уже неважно"\n'
                "- [ ] plain @id:plain\n"
            )
        },
    )
    view = build_waits(config, now=NOW)
    assert [(t.node.node_id, t.condition) for t in view.triggers] == [
        ("todo://alpha/armed", "docker появился")
    ]


def test_two_blockers_make_two_edges(tmp_path: Path) -> None:
    config = _workspace(
        tmp_path,
        {
            "alpha": (
                "- [ ] consumer @id:cons "
                "@blocked_by:todo://beta/one @blocked_by:todo://beta/two\n"
            ),
            "beta": "- [ ] one @id:one\n- [ ] two @id:two\n",
        },
    )
    view = build_waits(config, now=NOW)
    assert [(e.target.node_id) for e in view.edges] == [
        "todo://beta/one",
        "todo://beta/two",
    ]


def test_same_fleet_renders_identically_modulo_generated_at(
    tmp_path: Path,
) -> None:
    config = _workspace(
        tmp_path,
        {
            "alpha": (
                "- [ ] consumer @id:cons @blocked_by:todo://beta/prod\n"
                '- [ ] armed @id:armed @trigger:"событие"\n'
                "- [ ] typo @id:typo @blocked_by:beta#nope\n"
            ),
            "beta": "- [ ] producer @id:prod\n",
        },
    )
    first = build_waits(config, now=NOW)
    second = build_waits(config, now="2026-08-27T11:00:00Z")
    a = first.model_dump()
    b = second.model_dump()
    a.pop("generated_at")
    b.pop("generated_at")
    assert a == b


def test_route_returns_200_on_both_paths(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from dispatcher.server.app import create_app

    config = _workspace(
        tmp_path,
        {
            "alpha": "- [ ] consumer @id:cons @blocked_by:todo://beta/prod\n",
            "beta": "- [ ] producer @id:prod\n",
        },
    )
    ok = TestClient(create_app(config)).get("/api/waits")
    assert ok.status_code == 200
    body = ok.json()
    assert body["todo_plane"]["state"] == "read"
    assert len(body["edges"]) == 1

    empty = TestClient(create_app(DispatcherConfig(roots=(tmp_path / "void",))))
    resp = empty.get("/api/waits")
    assert resp.status_code == 200  # unavailability is content, not transport
    assert resp.json()["todo_plane"]["state"] == "unavailable"


def test_unidd_item_with_delivered_legacy_blocker_is_visible(
    tmp_path: Path,
) -> None:
    # The canonical pipeline drops an @id-less item at PF-ID-MISSING, so its
    # wait would silently read as "no obligation" (ai-prosto review on #207).
    # The package's legacy pass reports its outcome — most critically STALE:
    # a delivered wait must never be invisible.
    config = _workspace(
        tmp_path,
        {
            "alpha": "- [ ] consumer without id @blocked_by:beta#prod\n",
            "beta": "- [x] prod work @id:prod\n",
        },
    )
    view = build_waits(config, now=NOW)
    stale = [f for f in view.findings if f.code == "PF-BLOCKER-STALE"]
    assert len(stale) == 1 and stale[0].repo == "alpha"
    assert "the wait is over" in stale[0].message


def test_legacy_pass_does_not_double_report_idd_sources(tmp_path: Path) -> None:
    # A source WITH an @id is the canonical plane's job: its legacy ref is
    # already a loose_ref, and the legacy pass must not add a second verdict.
    config = _workspace(
        tmp_path,
        {
            "alpha": "- [ ] consumer @id:cons @blocked_by:beta#nothere\n",
            "beta": "- [ ] prod work @id:prod\n",
        },
    )
    view = build_waits(config, now=NOW)
    assert len(view.loose_refs) == 1
    # exactly one verdict for the dangling ref — from the canonical plane's
    # diagnostics (PF-LEGACY-AMBIGUOUS with zero matches), not two
    assert len(view.findings) == 1
