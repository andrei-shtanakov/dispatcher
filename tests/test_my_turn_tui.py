"""«My turn» in the TUI (spec 2026-09-30-my-turn-b2-design §3)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from textual.widgets import DataTable, TabbedContent

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.human_queue import (
    HumanMergeAct,
    HumanQueueView,
    HumanWait,
    RunViewAct,
    SourceStatus,
)
from dispatcher.tui.app import DispatcherApp
from dispatcher.tui.my_turn import age_label, rows, tab_label

_NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _wait(key: str, since: str | None = None, **act: object) -> HumanWait:
    return HumanWait(
        key=key,
        reasons=["pr_human_merge"],
        source="forge_labelled_prs",
        repo="andrei-shtanakov/deployer",
        ref=key,
        title=f"wait {key}",
        since=since,
        since_basis=None if since is None else "label added",
        act=HumanMergeAct(
            repo="andrei-shtanakov/deployer",
            number=7,
            url="https://github.com/andrei-shtanakov/deployer/pull/7",
            head_sha=None,
        )
        if not act
        else RunViewAct(request_id=str(act["request_id"])),
    )


def _view(waits: list[HumanWait], **extra: object) -> HumanQueueView:
    fields: dict = {"waits": waits, "sources": {}, "complete": True}
    fields.update(extra)
    return HumanQueueView(generated_at=_NOW.isoformat(), **fields)


def test_age_buckets_match_the_other_surfaces() -> None:
    assert age_label(None, _NOW) == "age unknown"
    assert age_label("2026-09-30T11:30:00+00:00", _NOW) == "<1h"
    assert age_label("2026-09-29T12:00:00+00:00", _NOW) == "24h"
    assert age_label("2026-09-27T12:00:00+00:00", _NOW) == "3d"


def test_known_first_then_the_age_unknown_divider() -> None:
    got = rows(_view([_wait("a", "2026-09-29T12:00:00+00:00"), _wait("b")]), _NOW)
    assert [r.wait for r in got] == [
        "deployer · wait a",
        "age unknown (1)",
        "deployer · wait b",
    ]
    assert got[0].reason == "PR awaits your merge"
    assert got[0].todo == "sh devtools/human-merge.sh deployer 7"
    assert got[1].prepared is None


def test_unknown_is_never_zero() -> None:
    assert [r.wait for r in rows(None, _NOW)] == ["human queue not read"]
    incomplete = _view(
        [],
        complete=False,
        sources={
            "forge_labelled_prs": SourceStatus(state="unavailable", detail="gh"),
            "impresario": SourceStatus(state="not_configured"),
        },
    )
    assert [r.wait for r in rows(incomplete, _NOW)] == [
        "⚠ incomplete — forge_labelled_prs: unavailable — gh",
        "no known waits — queue incomplete",
    ]
    assert [r.wait for r in rows(_view([]), _NOW)] == ["nothing waits for you"]


def test_the_tab_label_carries_count_and_completeness() -> None:
    assert tab_label(None) == "My turn · ?"
    assert tab_label(_view([_wait("a")])) == "My turn · 1"
    assert tab_label(_view([], complete=False)) == "My turn · 0 · ?"


@pytest.mark.anyio
async def test_enter_copies_the_command_and_runs_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    app = DispatcherApp(DispatcherConfig(roots=(tmp_path,)))
    copied: list[str] = []
    monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
    view = _view([_wait("a", "2026-09-29T12:00:00+00:00")])
    monkeypatch.setattr(app, "_read_queue", lambda: view)
    async with app.run_test() as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        tabs = app.query_one(TabbedContent)
        assert str(tabs.get_tab("tab-my-turn").label) == "My turn · 1"
        tabs.active = "tab-my-turn"
        await pilot.pause()
        table = app.query_one("#my-turn-table", DataTable)
        assert table.row_count == 1
        table.focus()
        await pilot.press("enter")
        await pilot.pause()
    assert copied == ["sh devtools/human-merge.sh deployer 7"]
