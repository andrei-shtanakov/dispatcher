"""Content-PR action: update-spec-runner-config (DESIGN-304, resolves X-02).

Deliberately NOT `core/actions.py`'s `ActionRunner` — this runner produces
file *content* (a diff limited to one project.yaml's `spec_runner:` block)
before delegating branch/commit/push/PR to github-checker, a different
mutation shape than the pure git-plumbing sync actions (pull/create-pr).
Own lock, own audit logger, so the two action classes stay independently
testable and reasoned about (explicit stakeholder requirement, spec §1).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
from io import StringIO
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from ruamel.yaml import YAML

from dispatcher.core.actions import ActionOutcome
from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.spec_runner_config import TYPED_FIELDS
from dispatcher.core.spec_runner_config_schema import (
    ConfigValidationError,
    validate_candidate,
)

_ACTION_TIMEOUT = 120
_SAFE_DIR_RE = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]*")
_audit = logging.getLogger("dispatcher.actions.spec_runner_config")


class ConfigCandidate(BaseModel):
    """A proposed spec_runner: block, as submitted by the editor UI."""

    typed: dict[str, Any]
    extra_executor_config: dict[str, Any] = Field(default_factory=dict)
    base_mtime: float  # project.yaml's mtime when the form was rendered


class SpecRunnerConfigRejectedError(Exception):
    """Bad target or invalid candidate (API turns this into 422)."""


class SpecRunnerConfigBusyError(Exception):
    """This repo's project.yaml already has an update in flight (-> 409)."""


class SpecRunnerConfigConflictError(Exception):
    """project.yaml changed on disk since the form was rendered (-> 409)."""


def build_new_yaml_text(project_yaml: Path, candidate: ConfigCandidate) -> str:
    """Render `project.yaml` with only its `spec_runner:` key replaced.

    Uses ruamel.yaml's round-trip mode so comments, key order, and block
    literals elsewhere in the file (e.g. `workstreams:`) are preserved —
    plain PyYAML load+dump would rewrite the whole file and violate the
    "only the spec_runner: block changes" constraint.

    `YAML()` defaults to `typ="rt"` (round-trip) — as safe as
    `yaml.safe_load()`, no arbitrary object construction. Never pass
    `typ="unsafe"` here.
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    with project_yaml.open() as fh:
        doc = yaml.load(fh)
    new_block: dict[str, Any] = dict(candidate.typed)
    if candidate.extra_executor_config:
        new_block["extra_executor_config"] = candidate.extra_executor_config
    doc["spec_runner"] = new_block
    buf = StringIO()
    yaml.dump(doc, buf)
    return buf.getvalue()


class SpecRunnerConfigActionRunner:
    """Serialized executor of the update-spec-runner-config action."""

    def __init__(
        self,
        config: DispatcherConfig,
        *,
        command: tuple[str, ...] = ("github-checker",),
    ) -> None:
        self._config = config
        self._command = command
        self._lock = threading.Lock()
        self._busy: set[str] = set()

    def _target(self, repo_dir: str) -> Path:
        if not _SAFE_DIR_RE.fullmatch(repo_dir) or repo_dir in (".", ".."):
            raise SpecRunnerConfigRejectedError(f"unsafe repo dir: {repo_dir!r}")
        workspace = next((r for r in self._config.roots if r.is_dir()), None)
        if workspace is None:
            raise SpecRunnerConfigRejectedError("no existing workspace root configured")
        project_yaml = workspace / repo_dir / "project.yaml"
        if not project_yaml.is_file():
            raise SpecRunnerConfigRejectedError(f"no project.yaml in: {repo_dir}")
        return project_yaml

    def run(self, repo_dir: str, candidate: ConfigCandidate) -> ActionOutcome:
        """Validate, diff, write, and hand off to github-checker. Always audits."""
        try:
            unknown = set(candidate.typed) - set(TYPED_FIELDS)
            if unknown:
                raise SpecRunnerConfigRejectedError(
                    f"unknown typed field(s): {unknown}"
                )
            try:
                validate_candidate(candidate.typed, candidate.extra_executor_config)
            except ConfigValidationError as verr:
                raise SpecRunnerConfigRejectedError(str(verr)) from verr
            project_yaml = self._target(repo_dir)
            # Claim the busy slot before checking mtime: a concurrent run()
            # may already be writing project.yaml (which changes its mtime)
            # while blocked on _invoke below. Checking busy first means a
            # second caller sees SpecRunnerConfigBusyError, not a spurious
            # SpecRunnerConfigConflictError caused by that in-flight write.
            with self._lock:
                if repo_dir in self._busy:
                    raise SpecRunnerConfigBusyError(
                        f"{repo_dir}: update already in flight"
                    )
                self._busy.add(repo_dir)
            try:
                stale = project_yaml.stat().st_mtime != candidate.base_mtime
            except OSError as err:
                # project.yaml vanished between _target()'s is_file() and
                # here — release the just-claimed busy slot, don't leak it.
                with self._lock:
                    self._busy.discard(repo_dir)
                raise SpecRunnerConfigRejectedError(
                    f"{repo_dir}: project.yaml unreadable: {err}"
                ) from err
            if stale:
                with self._lock:
                    self._busy.discard(repo_dir)
                raise SpecRunnerConfigConflictError(
                    f"{repo_dir}: project.yaml changed since the form was loaded"
                )
        except (
            SpecRunnerConfigRejectedError,
            SpecRunnerConfigConflictError,
            SpecRunnerConfigBusyError,
        ) as err:
            _audit.info(
                "action=update-spec-runner-config repo=%s ok=False rejected=%s",
                repo_dir,
                err,
            )
            raise
        try:
            new_text = build_new_yaml_text(project_yaml, candidate)
            project_yaml.write_text(new_text)
            outcome = self._invoke(repo_dir)
        except Exception as err:
            # "Always audits" includes unexpected build/write failures —
            # without this, such an attempt would leave no audit line.
            _audit.info(
                "action=update-spec-runner-config repo=%s ok=False error=%s",
                repo_dir,
                err,
            )
            raise
        finally:
            with self._lock:
                self._busy.discard(repo_dir)
        _audit.info(
            "action=update-spec-runner-config repo=%s ok=%s detail=%s error=%s",
            repo_dir,
            outcome.ok,
            outcome.detail,
            outcome.error,
        )
        return outcome

    def _invoke(self, repo_dir: str) -> ActionOutcome:
        workspace = next(r for r in self._config.roots if r.is_dir())
        target = workspace / repo_dir
        argv = [*self._command, "open-pr", str(target)]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=_ACTION_TIMEOUT
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as err:
            return ActionOutcome(
                action="update-spec-runner-config",
                dir=target.name,
                ok=False,
                error=str(err),
            )
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return ActionOutcome(
                action="update-spec-runner-config",
                dir=target.name,
                ok=False,
                error=proc.stderr.strip() or "github-checker returned no JSON",
            )
        return ActionOutcome(
            action="update-spec-runner-config",
            dir=target.name,
            ok=bool(data.get("ok")),
            detail=data.get("detail"),
            error=data.get("error"),
            pr_url=data.get("pr_url"),
        )
