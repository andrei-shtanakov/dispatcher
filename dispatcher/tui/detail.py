"""Screens pushed from the main app: project detail and full error text."""

from __future__ import annotations

from rich.markup import escape
from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, Static

from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.onboarding import OnboardingView
from dispatcher.core.product_proposals import ProductProposalsReport


def _section(title: str, lines: list[str]) -> str:
    body = "\n".join(lines) if lines else "(none)"
    return f"[bold]{title}[/bold]\n{body}"


# TODO maestro-runs-panel-parity: identical badge words across web/TUI/
# VSCode. Formatting only — the collector classified (#147, fail-closed);
# this layer never re-classifies.
_RUN_BADGES = {
    "running": "▶ running",
    "suspended": "⏸ suspended (waiting on a human)",
    "interrupted": "⚠ interrupted / unknown",
    "completed": "✅ completed",
    "failed": "⛔ failed",
    "cancelled": "∅ cancelled",
    "superseded": "↻ superseded",
    "legacy": "🗄 legacy (frozen pre-#147 file)",
    "unreadable": "✖ unreadable",
}


def _run_badge(status: str) -> str:
    return _RUN_BADGES.get(status, f"✖ {status}")


def _is_run_warning(w: str) -> bool:
    """The collector's degradation signal (prefix pinned in
    tests/test_maestro.py): `run <id>: …` / `runs enumeration: …`."""
    return w.startswith(("run ", "runs "))


def _runs_lines(s: ProjectSnapshot) -> list[str]:
    """Zero-state rule: a bare «(none)» is a confident zero and is only
    allowed on a clean enumeration — a degraded one says unknown."""
    lines = [
        escape(
            f"{r.repo_key} · {r.run_id or '—'} · {_run_badge(r.status)} "
            f"({r.started_at or '?'} → {r.ended_at or '…'})"
            + (f" · {r.reason}" if r.reason else "")
        )
        for r in s.runs
    ]
    if not lines and any(_is_run_warning(w) for w in s.warnings):
        return ["⚠ unknown — run enumeration degraded (see warnings), not zero"]
    return lines


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
        ("orchestration runs", _runs_lines(s)),
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
                + (
                    f" · median {pos.median_readiness:.0%}"
                    if pos.median_readiness is not None
                    else " · median —"
                )
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


_STATE_MARKS = {"ok": "✅", "unreadable": "✖", "unknown": "❓", "conflict": "⛔"}


def _pp_badge(state: str) -> str:
    """Web-parity badge: '✅ ok' is the only green word — every other
    state reads as «classification suppressed», never as zero waits."""
    if state == "ok":
        return "✅ ok"
    return f"{_STATE_MARKS.get(state, '✖')} classification suppressed — {state}"


def _pp_sections(report: ProductProposalsReport) -> list[tuple[str, list[str]]]:
    """Producer decides — this renderer never re-classifies (inbox #129).

    Zero-state rule (spec, refined for parity): «0 bundles» only on a
    healthy empty scan; «0 gates/loops waiting» only when at least one
    bundle exists, ALL bundles are ok AND there is no report-level
    diagnostic — anything less than a fully classified scan renders as
    incomplete, never as a confident global zero.
    """
    suppressed = sum(1 for b in report.bundles if b.state != "ok")
    certain = bool(report.bundles) and not suppressed and not report.diagnostics
    note = (
        [
            escape(
                f"⚠ {suppressed} bundle(s): classification suppressed — "
                "their waits are unknown, not zero"
            )
        ]
        if suppressed
        else []
    )

    def zero_state(label: str) -> list[str]:
        if certain:
            return [escape(label)]
        if not report.bundles and not report.diagnostics:
            return ["0 bundles"]
        return ["classification incomplete — waits unknown, not zero"]

    gate_lines = note + [
        escape(
            f"{w.artifact_ref} · {w.gate_label} ({w.gate_id}) · "
            f"{w.authority} · v{w.version} · updated {w.proposal_updated_at} "
            f"· {w.bundle_path}"
        )
        for w in report.waits
    ]
    loop_lines = note + [
        escape(
            f"{w.loop_id} · iteration {w.iteration} · {w.reason} · "
            f"stopped {w.stopped_at} · {w.bundle_path}"
        )
        for w in report.needs_human
    ]
    bundle_lines = [
        escape(
            f"{b.path} · {_pp_badge(b.state)}"
            + (f" · {b.status}" if b.status else "")
            + (f" · v{b.version}" if b.version is not None else "")
            + f" · loop: {b.loop_status}"
        )
        for b in report.bundles
    ]
    sections = []
    if report.diagnostics:
        sections.append(
            (
                "product proposals — diagnostics",
                [
                    escape(
                        f"⚠ {d.code}: {d.message}" + (f" · {d.path}" if d.path else "")
                    )
                    for d in report.diagnostics
                ],
            )
        )
    sections.extend(
        [
            (
                "product proposals — gate waits",
                gate_lines or zero_state("0 gates waiting"),
            ),
            (
                "product proposals — loop waits (needs_human)",
                loop_lines or zero_state("0 loops waiting"),
            ),
            ("product proposals — bundles", bundle_lines),
        ]
    )
    return sections


class ProjectDetailScreen(Screen[None]):
    """Read-only drill-down into one project's snapshot."""

    BINDINGS = [("escape,q", "app.pop_screen", "Back")]

    def __init__(
        self,
        snap: ProjectSnapshot,
        onboarding: OnboardingView | None = None,
        product_proposals: ProductProposalsReport | None = None,
        product_proposals_error: str | None = None,
    ) -> None:
        super().__init__()
        self._snap = snap
        self._onboarding = onboarding
        self._product_proposals = product_proposals
        self._product_proposals_error = product_proposals_error

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
            view = self._onboarding
            texts.extend(
                _section(title, lines) for title, lines in _onboarding_sections(view)
            )
            extra = [w for w in view.warnings if w not in set(s.warnings)]
            texts.append(_section("onboarding warnings", [escape(w) for w in extra]))
        # fail-loud: a broken collect renders as an error section — unknown
        # must never look like «no product-governance waits»
        if self._product_proposals_error is not None:
            texts.append(
                _section(
                    "product proposals — error",
                    [escape(self._product_proposals_error)],
                )
            )
        elif self._product_proposals is not None:
            texts.extend(
                _section(title, lines)
                for title, lines in _pp_sections(self._product_proposals)
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
