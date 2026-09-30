"""Prepared acts (spec 2026-09-30-my-turn-b2-design §1)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from dispatcher.core.human_queue import Act, HumanWait, OpenArtifactAct, RunViewAct
from dispatcher.core.prepared_acts import prepare_act, shell_word

_FIXTURE = (
    Path(__file__).parent.parent
    / "vscode-ext"
    / "test"
    / "fixtures"
    / "prepared-acts.json"
)
_CASES = json.loads(_FIXTURE.read_text())
_ACT = TypeAdapter(Act)


@pytest.mark.parametrize("case", _CASES, ids=[c["name"] for c in _CASES])
def test_the_same_words_as_the_extension(case: dict) -> None:
    """FR-06 parity: vscode-ext/test/preparedParity.test.ts checks the
    extension's prepareAct against this same fixture."""
    got = prepare_act(_ACT.validate_python(case["act"]))
    assert got.model_dump(exclude_none=True) == case["expected"]


def test_the_fixture_covers_both_command_kinds_and_refusals() -> None:
    kinds = {(c["act"]["kind"], c["expected"]["kind"]) for c in _CASES}
    assert kinds == {
        ("maestro_verb", "command"),
        ("maestro_verb", "refused"),
        ("human_merge", "command"),
        ("human_merge", "refused"),
    }


def test_a_run_view_is_a_page_relative_link() -> None:
    got = prepare_act(RunViewAct(request_id="a/b c"))
    assert (got.kind, got.url) == ("link", "#launchpad/a%2Fb%20c")


def test_an_artifact_is_a_mirror_relative_path() -> None:
    got = prepare_act(OpenArtifactAct(path="decisions/QG-4.md"))
    assert (got.kind, got.text) == ("path", "decisions/QG-4.md")


def test_every_wait_carries_its_prepared_act_on_the_wire() -> None:
    wait = HumanWait(
        key="run:x",
        reasons=["launch_unknown"],
        source="dispatcher_runs",
        repo=None,
        ref="x",
        title="t",
        since=None,
        since_basis=None,
        act=RunViewAct(request_id="r1"),
    )
    assert wait.model_dump(mode="json")["prepared"]["url"] == "#launchpad/r1"


def test_shell_word_quotes_only_what_a_shell_would_touch() -> None:
    assert shell_word("/ws/a-b_c.d:e@f") == "/ws/a-b_c.d:e@f"
    assert shell_word("$HOME") == "'$HOME'"
    assert shell_word("it's") == "'it'\\''s'"
