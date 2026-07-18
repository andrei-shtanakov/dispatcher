"""Screens pushed from the main app: project detail and full error text."""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, Static

from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.onboarding import OnboardingView


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


def _onboarding_sections(view: OnboardingView) -> list[tuple[str, list[str]]]:
    """Onboarding blocks rendered ABOVE the raw snapshot sections."""
    pos = view.roadmap_position
    position = (
        []
        if pos is None
        else [
            escape(
                f"readiness {pos.summary.readiness:.0%} "
                f"({pos.summary.done}/{pos.summary.total})"
                + (" · LAGGING" if pos.summary.lagging else "")
                + (" · CONTRACT DRIFT" if pos.summary.contract_drift else "")
            ),
            *[
                escape(
                    f"phase {p.phase or '—'}: "
                    + ", ".join(f"{k}={v}" for k, v in sorted(p.counts.items()))
                )
                for p in pos.phases
            ],
        ]
    )
    return [
        (
            "description",
            [escape(f"{view.project.description} [{view.project.description_source}]")]
            if view.project.description
            else [],
        ),
        ("roadmap position", position),
        (
            "next items",
            [
                escape(
                    f"{'▶' if n.actionable else '⛔'} {n.id} · {n.title} "
                    f"· {n.computed_status}"
                    + (
                        f" · blocked by: {', '.join(n.blocked_by)}"
                        if n.blocked_by
                        else ""
                    )
                )
                for n in view.next_items
            ],
        ),
        (
            "live tasks",
            [
                escape(f"{t.task_id} · {t.status} · {t.title or ''}")
                for t in view.live_tasks
            ],
        ),
    ]


class ProjectDetailScreen(Screen[None]):
    """Read-only drill-down into one project's snapshot."""

    BINDINGS = [("escape,q", "app.pop_screen", "Back")]

    def __init__(
        self, snap: ProjectSnapshot, onboarding: OnboardingView | None = None
    ) -> None:
        super().__init__()
        self._snap = snap
        self._onboarding = onboarding

    def _render_texts(self) -> list[str]:
        """Static bodies in render order (plain list — unit-testable)."""
        s = self._snap
        texts = [
            f"[bold]{escape(s.name)}[/bold] — {escape(s.path)}\n"
            f"freshness: {escape(s.freshness or 'unknown')}\n"
            f"collected: {escape(s.collected_at.isoformat())} · "
            f"detected: {s.detected}"
        ]
        if self._onboarding is not None:
            texts.extend(
                _section(title, lines)
                for title, lines in _onboarding_sections(self._onboarding)
            )
        texts.extend(_section(title, lines) for title, lines in _sections(s))
        return texts

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            for text in self._render_texts():
                yield Static(text, classes="detail-section")
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
