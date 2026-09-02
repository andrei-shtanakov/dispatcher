"""BEH-13 (WS-dispatcher-229): self-owner warning наблюдаема без неявного
governance gate.

Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-13

Given отдельное продуктовое решение о включении ``PF-OWNER-REPO-SELF`` в
обязательный gate отсутствует, fleet-анализ self-owner node должен выдавать
finding базовой severity ``warning`` в fleet output и reporters, но само по
себе не включать новый обязательный governance gate (FR-10) — включение
требует отдельного явного решения и оценки существующих self-owner записей.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from plan_fields import ManifestIndex, RepoInput, parse_fleet, validate_document
from plan_fields.validator import CONTRACT_DIR
from plan_fields.views import REPO_OWNER_SELF, repo_owner_verdicts

from dispatcher.core.gate_catalog import load_catalog

_SELF_CODE = "PF-OWNER-REPO-SELF"


def _index() -> ManifestIndex:
    return ManifestIndex(frozenset({"dispatcher", "maestro"}), {})


def _self_owner_snapshot() -> dict:
    text = "- [ ] self owner @id:a @owner:repo:dispatcher\n"
    doc = parse_fleet([RepoInput("dispatcher", text)], _index())
    validate_document(doc)
    return doc


def test_finding_present_in_fleet_output_at_warning_severity() -> None:
    doc = _self_owner_snapshot()
    self_diags = [d for d in doc["diagnostics"] if d["code"] == _SELF_CODE]

    assert len(self_diags) == 1
    assert self_diags[0]["severity"] == "warning"
    assert self_diags[0]["subject_uri"] == "todo://dispatcher/a"


def test_finding_observable_through_the_shared_reporter_view() -> None:
    # FR-10's "не скрывает finding из fleet output и reporters" — the same
    # verdict every reporter (web/TUI/VSCode/MCP) reads must surface the
    # self-owner state, not just the raw diagnostics list.
    doc = _self_owner_snapshot()
    verdicts = repo_owner_verdicts(doc)

    assert verdicts["todo://dispatcher/a"] == REPO_OWNER_SELF


def test_diagnostic_registry_declares_warning_and_never_escalation() -> None:
    # FR-10: "базовая severity равна warning" and no hardening path — a
    # migration-period diagnostic escalating on its own would silently turn
    # into a de facto mandatory gate without the separate decision FR-10
    # requires.
    codes = yaml.safe_load(
        (CONTRACT_DIR / "diagnostics.yaml").read_text(encoding="utf-8")
    )["codes"]

    entry = codes[_SELF_CODE]
    assert entry["default_severity"] == "warning"
    assert entry["escalation"] == "never"


def test_gate_catalog_carries_no_entry_for_self_owner_diagnostic() -> None:
    # FR-10: enabling PF-OWNER-REPO-SELF as an obligatory gate is a separate,
    # explicit future decision — the vendored gate-catalog (SSOT for gate
    # identity, GC-* namespace) must carry no entry for it, implicitly or
    # otherwise.
    catalog = load_catalog()

    assert _SELF_CODE not in catalog.gates
    for slug, entry in catalog.gates.items():
        assert _SELF_CODE not in entry.title, slug


def test_fleet_emission_is_unconditional_no_gate_coupling_in_source() -> None:
    # FR-10: "отсутствие gate enforcement не скрывает finding" — checked at
    # the source level too: the emitting module must not IMPORT any
    # gate-catalog/governance/dispatcher module before emitting
    # PF-OWNER-REPO-SELF. A future accidental coupling (e.g. "only warn if
    # not gated") would silently turn the migration-period diagnostic into a
    # conditional one. ``plan-fields`` is a standalone package with no
    # dependency on ``dispatcher`` at all, so any such import is itself the
    # coupling this scenario forbids.
    repo_root = Path(__file__).resolve().parents[3]
    fleet_api_path = repo_root / "packages/plan-fields/src/plan_fields/fleet_api.py"
    import_lines = [
        line
        for line in fleet_api_path.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith(("import ", "from "))
    ]

    offenders = [
        line
        for line in import_lines
        if "dispatcher" in line or "gate_catalog" in line or "steward" in line
    ]
    assert offenders == [], (
        "PF-OWNER-REPO-SELF emission in fleet_api.py must stay unconditional, "
        f"uncoupled from any gate/governance import: {offenders}"
    )
