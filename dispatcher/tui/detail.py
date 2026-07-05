"""Screens pushed from the main app: project detail and full error text."""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, Static

from dispatcher.core.models import ProjectSnapshot


def _section(title: str, lines: list[str]) -> str:
    body = "\n".join(lines) if lines else "(none)"
    return f"[bold]{title}[/bold]\n{body}"


def _sections(s: ProjectSnapshot) -> list[tuple[str, list[str]]]:
    """Every ProjectSnapshot field, pre-escaped for Rich markup."""
    return [
        (
            "schema versions",
            [
                escape(
                    f"{c.database}: found={c.found} expected={c.expected} "
                    + ("ok" if c.ok else "DRIFT" if c.ok is False else "unknown")
                )
                for c in s.schema_versions
            ],
        ),
        (
            "models",
            [
                escape(
                    f"{m.model_id} · harness={m.harness or '—'} "
                    f"· role={m.role} · vendor={m.vendor or '—'} "
                    f"· status={m.status or '—'}"
                )
                for m in s.models
            ],
        ),
        (
            "tasks",
            [
                escape(
                    f"{t.task_id} · {t.status} · {t.title or ''} "
                    f"({t.started_at or '?'} → {t.completed_at or '…'})"
                )
                for t in s.tasks
            ],
        ),
        (
            "test runs",
            [
                escape(
                    f"{r.name} · passed={r.passed} failed={r.failed} "
                    f"score={r.score} at {r.timestamp or '—'}"
                )
                for r in s.test_results
            ],
        ),
        (
            "configs",
            [escape(f"{c.path} ({c.format}): {c.summary}") for c in s.configs],
        ),
        (
            "errors",
            [
                escape(f"{e.timestamp or '—'} {e.service or '—'}: {e.body}")
                for e in s.errors
            ],
        ),
        ("warnings", [escape(w) for w in s.warnings]),
    ]


class ProjectDetailScreen(Screen[None]):
    """Read-only drill-down into one project's snapshot."""

    BINDINGS = [("escape,q", "app.pop_screen", "Back")]

    def __init__(self, snap: ProjectSnapshot) -> None:
        super().__init__()
        self._snap = snap

    def compose(self) -> ComposeResult:
        yield Header()
        s = self._snap
        with VerticalScroll():
            yield Static(
                f"[bold]{escape(s.name)}[/bold] — {escape(s.path)}\n"
                f"freshness: {escape(s.freshness or 'unknown')}\n"
                f"collected: {escape(s.collected_at.isoformat())} · "
                f"detected: {s.detected}"
            )
            for title, lines in _sections(s):
                yield Static(_section(title, lines), classes="detail-section")
        yield Footer()


class ErrorMessageScreen(ModalScreen[None]):
    """Full text of a truncated errors-table message."""

    BINDINGS = [("escape,q,enter", "app.pop_screen", "Close")]
    DEFAULT_CSS = """
    ErrorMessageScreen { align: center middle; }
    #error-message {
        width: 80%; height: 60%;
        background: $surface; border: solid $accent; padding: 1 2;
    }
    """

    def __init__(self, body: str) -> None:
        super().__init__()
        self._body = body

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="error-message"):
            yield Static(escape(self._body))
