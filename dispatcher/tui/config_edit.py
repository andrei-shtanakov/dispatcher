"""Config-editor screen: DESIGN-503, the TUI half of DESIGN-308.

Priority ctrl-chords, not printable keys: with 12 Inputs on screen the
focused Input consumes plain letters — `d`/`y` would type into the field.
Candidate always carries extra_executor_config=None (tri-state preserve,
shipped in dispatcher PR #40); the extra block is shown read-only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Input, Label, Static

from dispatcher.core.spec_runner_config import TYPED_DEFAULTS, ProjectSpecRunnerConfig
from dispatcher.core.spec_runner_config_actions import (
    SpecRunnerConfigBusyError,
    SpecRunnerConfigConflictError,
    SpecRunnerConfigRejectedError,
)
from dispatcher.tui.detail import ErrorMessageScreen


def coerce_typed(name: str, raw: str) -> Any:
    """Strict input coercion; raises ValueError with a user-facing message.

    bool BEFORE int (bool subclasses int): only literal true/false accepted —
    never a silent everything-else-is-False.
    """
    default = TYPED_DEFAULTS[name]
    text = raw.strip()
    if isinstance(default, bool):
        if text.lower() == "true":
            return True
        if text.lower() == "false":
            return False
        raise ValueError(f"{name}: enter true or false")
    if isinstance(default, int):
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{name}: enter an integer") from None
    return raw


class ConfigEditScreen(Screen[None]):
    """Edit one project.yaml's spec_runner: block; confirm opens a PR."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+d", "preview", "Diff", priority=True),
        Binding("ctrl+y", "confirm", "Confirm → PR", priority=True),
    ]

    def __init__(self, cfg: ProjectSpecRunnerConfig, runner: Any) -> None:
        super().__init__()
        self._cfg = cfg
        self._runner = runner

    def compose(self) -> ComposeResult:
        yield Static(
            f"spec_runner config — {self._cfg.project} "
            f"({Path(self._cfg.project_yaml_path).parent.name}/project.yaml)",
            id="config-edit-title",
        )
        with VerticalScroll():
            for name, field in self._cfg.typed.items():
                marker = "explicit" if field.explicit else "default"
                with Horizontal(classes="config-field"):
                    yield Label(f"{name} ({marker})", classes="config-label")
                    yield Input(value=str(field.value), id=f"field-{name}")
            if self._cfg.extra_executor_config:
                yield Static(
                    "extra_executor_config (read-only, preserved as-is):\n"
                    + str(self._cfg.extra_executor_config),
                    id="config-extra-preview",
                )
        yield Footer()

    def _collect_typed(self) -> dict[str, Any] | None:
        """All 12 fields coerced; first invalid input → toast + None."""
        typed: dict[str, Any] = {}
        for name in self._cfg.typed:
            raw = self.query_one(f"#field-{name}", Input).value
            try:
                typed[name] = coerce_typed(name, raw)
            except ValueError as err:
                self.app.notify(str(err), severity="warning")
                return None
        return typed

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def action_preview(self) -> None:
        typed = self._collect_typed()
        if typed is None:
            return
        lines: list[str] = []
        for name, field in self._cfg.typed.items():
            if typed[name] != field.value:
                lines.append(f"- {name}: {field.value}")
                lines.append(f"+ {name}: {typed[name]}")
        self.app.push_screen(ErrorMessageScreen("\n".join(lines) or "(no changes)"))

    def action_confirm(self) -> None:
        typed = self._collect_typed()
        if typed is None:
            return
        self._do_confirm(typed)

    @work(thread=True, group="actions")
    def _do_confirm(self, typed: dict[str, Any]) -> None:
        from dispatcher.core.spec_runner_config_actions import ConfigCandidate

        candidate = ConfigCandidate(
            typed=typed,
            extra_executor_config=None,  # tri-state: preserve current overlay
            base_mtime=self._cfg.base_mtime,
        )
        repo_dir = Path(self._cfg.project_yaml_path).parent.name
        try:
            outcome = self._runner.run(repo_dir, candidate)
        except SpecRunnerConfigConflictError:
            self.app.call_from_thread(
                self.app.notify,
                "project.yaml changed — reload required",
                severity="warning",
            )
            return
        except (SpecRunnerConfigRejectedError, SpecRunnerConfigBusyError) as err:
            self.app.call_from_thread(self.app.notify, str(err), severity="warning")
            return
        if outcome.ok:
            self.app.call_from_thread(
                self._finish, f"✓ PR: {outcome.pr_url or 'opened'}"
            )
        elif outcome.detail == "no-op":
            self.app.call_from_thread(
                self._finish, "config already in this state — no PR needed"
            )
        else:
            self.app.call_from_thread(
                self.app.notify, f"✗ {outcome.error or 'failed'}", severity="error"
            )

    def _finish(self, message: str) -> None:
        self.app.notify(message)
        self.app.pop_screen()
