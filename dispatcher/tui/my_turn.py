"""«My turn» for the TUI (spec 2026-09-30-my-turn-b2-design) — rows, no widgets.

Same words as the web tab and the VSCode tree: reason labels, age buckets,
empty and incomplete texts. The act's words come from the server-side
`prepared` field, so nothing here builds a command.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from dispatcher.core.human_queue import HumanQueueView, HumanWait
from dispatcher.core.prepared_acts import PreparedAct

REASON_LABEL = {
    "launch_unknown": "Launch outcome unknown",
    "run_needs_review": "Run task needs review",
    "run_awaiting_approval": "Run task awaits approval",
    "loop_needs_human": "Product loop needs a human",
    "proposal_gate": "Proposal gate",
    "backlog_gate": "Backlog gate (QG-4)",
    "pr_human_merge": "PR awaits your merge",
}
# `not_configured` is a feature that is off: nothing can wait there.
_SILENT = frozenset({"ok", "not_configured"})


@dataclass(frozen=True)
class MyTurnRow:
    """One table row; `prepared` is None for a divider or a notice."""

    age: str
    reason: str
    wait: str
    todo: str
    prepared: PreparedAct | None = None
    style: str = ""


def age_label(since: str | None, now: datetime) -> str:
    """`<1h`, hours under two days, then days; `age unknown` when unproven."""
    if since is None:
        return "age unknown"
    hours = int((now - datetime.fromisoformat(since)).total_seconds() // 3600)
    if hours < 1:
        return "<1h"
    return f"{hours}h" if hours < 48 else f"{hours // 24}d"


def tab_label(view: HumanQueueView | None) -> str:
    """`My turn · N`, `· ?` when incomplete, `?` when the queue was not read."""
    if view is None:
        return "My turn · ?"
    count = len(view.waits)
    return f"My turn · {count}" if view.complete else f"My turn · {count} · ?"


def incomplete_lines(view: HumanQueueView) -> list[str]:
    """One line per source that makes the queue incomplete, by name."""
    return [
        f"{name}: {st.state} — {st.detail}" if st.detail else f"{name}: {st.state}"
        for name, st in sorted(view.sources.items())
        if st.state not in _SILENT
    ]


def todo_text(prepared: PreparedAct) -> str:
    """What the "what to do" cell shows for one act."""
    if prepared.kind in ("command", "path"):
        return prepared.text or ""
    if prepared.kind == "link":
        return f"web run view: {prepared.url}"
    return f"refused: {prepared.note}"


def rows(view: HumanQueueView | None, now: datetime) -> list[MyTurnRow]:
    """The table: notices, known ages (server order), then age unknown."""
    if view is None:
        return [MyTurnRow("", "", "human queue not read", "", style="bold red")]
    out = [
        MyTurnRow("", "", f"⚠ incomplete — {line}", "", style="yellow")
        for line in incomplete_lines(view)
    ]
    if not view.waits:
        empty = (
            "nothing waits for you"
            if view.complete
            else "no known waits — queue incomplete"
        )
        return [*out, MyTurnRow("", "", empty, "", style="dim")]
    known = [w for w in view.waits if w.since is not None]
    unknown = [w for w in view.waits if w.since is None]
    out += [_row(w, now) for w in known]
    if unknown:
        out.append(MyTurnRow("", "", f"age unknown ({len(unknown)})", "", style="dim"))
        out += [_row(w, now) for w in unknown]
    return out


def _row(wait: HumanWait, now: datetime) -> MyTurnRow:
    repo = next((p for p in reversed((wait.repo or "").split("/")) if p), None)
    return MyTurnRow(
        age=age_label(wait.since, now),
        reason=" + ".join(REASON_LABEL.get(r, r) for r in wait.reasons),
        wait=f"{repo} · {wait.title}" if repo else wait.title,
        todo=todo_text(wait.prepared),
        prepared=wait.prepared,
    )
