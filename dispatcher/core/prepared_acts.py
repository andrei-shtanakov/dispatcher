"""What a human does about one wait, in words shared by every surface.

Spec: docs/superpowers/specs/2026-09-30-my-turn-b2-design.md. The web tab and
the TUI render this; the VSCode extension builds the same words in TypeScript
(`vscode-ext/src/myTurn.ts`), and one fixture
(`vscode-ext/test/fixtures/prepared-acts.json`) is checked by both test
suites, so the command a human sees does not depend on the surface (FR-06).

Nothing here executes anything. A command is built from the act's typed
fields — never from PR titles or bodies — and a field carrying a control
character refuses the whole command: a newline in a pasted line would run it
on its own.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal
from urllib.parse import quote

from pydantic import BaseModel

if TYPE_CHECKING:
    from dispatcher.core.human_queue import (
        Act,
        HumanMergeAct,
        MaestroVerbAct,
    )

# Ids pass through unquoted only when a shell cannot split or expand them.
_SHELL_SAFE = re.compile(r"^[A-Za-z0-9._:/@-]+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class PreparedAct(BaseModel):
    """One wait's act, ready to show.

    `command`: a line to paste (`text`); `link`: a place to open (`url`, a
    page-relative fragment for dispatcher's own views); `path`: a file to
    open (`text`, relative to the impresario mirror); `refused`: nothing
    safe could be prepared — `note` says why. `url` also carries the PR of
    a human merge, beside its command.
    """

    kind: Literal["command", "link", "path", "refused"]
    text: str | None = None
    url: str | None = None
    note: str = ""


def shell_word(value: str) -> str:
    """*value* as one shell word: bare when safe, else single-quoted."""
    if _SHELL_SAFE.match(value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def prepare_act(act: Act) -> PreparedAct:
    """What clicking (or selecting) a wait offers. Executes nothing."""
    if act.kind == "run_view":
        return PreparedAct(
            kind="link",
            url=f"#launchpad/{quote(act.request_id, safe='')}",
            note="dispatcher's run view — resolve or act on the run there",
        )
    if act.kind == "maestro_verb":
        return _maestro_verb(act)
    if act.kind == "human_merge":
        return _human_merge(act)
    return PreparedAct(
        kind="path",
        text=act.path,
        note="recorded at the source: open it in the impresario mirror",
    )


def _maestro_verb(act: MaestroVerbAct) -> PreparedAct:
    env = [act.maestro_home, act.atp_catalog or "", act.maestro_cli or ""]
    if any(_CONTROL.search(v) for v in [act.task_id, act.run_id, act.repo_key, *env]):
        return PreparedAct(
            kind="refused",
            note="the wait's ids contain control characters; not preparing a command",
        )
    parts: list[str] = []
    if act.maestro_home:
        parts.append(f"MAESTRO_HOME={shell_word(act.maestro_home)}")
    if act.atp_catalog:
        parts.append(f"ATP_CATALOG={shell_word(act.atp_catalog)}")
    parts.append(shell_word(act.maestro_cli) if act.maestro_cli else "maestro")
    parts += [act.verb, shell_word(act.task_id), "--run", shell_word(act.run_id)]
    note = (
        f"Typed in, not executed. Run it from a checkout of {act.repo_key} "
        "(maestro resolves the run's repository from the current directory)."
        + (
            " MAESTRO_HOME is the one dispatcher reads."
            if act.maestro_home
            else " This server did not send its MAESTRO_HOME — set it as your "
            "dispatcher config does, or maestro looks elsewhere."
        )
        + (
            " ATP_CATALOG is the one dispatcher uses."
            if act.atp_catalog
            else " No ATP_CATALOG came from the server; retry needs one."
        )
    )
    return PreparedAct(kind="command", text=" ".join(parts), note=note)


def _human_merge(act: HumanMergeAct) -> PreparedAct:
    name = next((p for p in reversed(act.repo.split("/")) if p), "")
    values = [act.repo, act.url, act.head_sha or ""]
    if name == "" or any(_CONTROL.search(v) for v in values):
        return PreparedAct(
            kind="refused",
            note="the PR's identifiers are unusable; not preparing a command",
        )
    pin = f" --expect-head {shell_word(act.head_sha)}" if act.head_sha else ""
    note = (
        "Typed in, not executed. Run it from the workspace root under YOUR gh "
        "profile — the merge is the human act that signs."
        + (
            " The PR head could not be read, so no --expect-head pin was added."
            if not act.head_sha
            else ""
        )
    )
    return PreparedAct(
        kind="command",
        text=f"sh devtools/human-merge.sh {shell_word(name)} {act.number}{pin}",
        url=act.url,
        note=note,
    )
