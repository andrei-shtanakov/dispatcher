"""BEH-11 (WS-dispatcher-229): public contract distinguishes three
repository owner states.

Source: workstreams/WS-dispatcher-229/spec/15-behaviour-spec.md#BEH-11

Given the canonical ``plan-fields`` contract, applicable schema validation
must pass over a structured fleet-analysis result that carries a self-owner,
an unknown repo-owner and a valid external repo-owner node at once. The
contract must describe self-owner separately from unknown and external
repo-owner (FR-08), ``PF-OWNER-REPO-SELF`` — severity ``warning``, node URI
and provenance — must be exposed as stable machine-readable fields (FR-03,
NFR-04), and every fleet reporter must share that classification semantics
(FR-05) rather than re-deriving it.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from plan_fields import (
    ManifestIndex,
    RepoInput,
    load_schema,
    parse_fleet,
    validate_document,
)
from plan_fields.validator import CONTRACT_DIR
from plan_fields.views import (
    REPO_OWNER_EXTERNAL,
    REPO_OWNER_SELF,
    REPO_OWNER_UNKNOWN,
    repo_owner_verdicts,
)

_TODO_URI_RE = r"^todo://[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9._-]{0,63}$"


def _diagnostics_registry() -> dict:
    return yaml.safe_load(
        (CONTRACT_DIR / "diagnostics.yaml").read_text(encoding="utf-8")
    )


def _index() -> ManifestIndex:
    return ManifestIndex(
        frozenset({"dispatcher", "maestro"}), {"legacy-checkout-dir": "dispatcher"}
    )


def _mixed_snapshot() -> dict:
    text = (
        "- [ ] self canonical @id:a @owner:repo:dispatcher\n"
        "- [ ] self git_dir @id:b @owner:repo:legacy-checkout-dir\n"
        "- [ ] external @id:c @owner:repo:maestro\n"
        "- [ ] unknown @id:d @owner:repo:unheard-of\n"
    )
    return parse_fleet([RepoInput("dispatcher", text)], _index())


def test_registry_declares_self_separately_from_unknown_repo_owner() -> None:
    # FR-08: the contract's diagnostic registry is the canonical place that
    # states self-owner is its own code, distinct from unknown repo-owner —
    # not a shared/ambiguous code disambiguated only by message text.
    codes = _diagnostics_registry()["codes"]
    assert "PF-OWNER-REPO-SELF" in codes
    assert "PF-OWNER-REPO-UNKNOWN" in codes
    assert codes["PF-OWNER-REPO-SELF"] != codes["PF-OWNER-REPO-UNKNOWN"]
    assert codes["PF-OWNER-REPO-SELF"]["default_severity"] == "warning"
    assert codes["PF-OWNER-REPO-UNKNOWN"]["default_severity"] == "warning"
    # SELF never escalates (it is expected steady state); UNKNOWN does — the
    # registry must keep these divergent, not just same-severity siblings.
    assert codes["PF-OWNER-REPO-SELF"]["escalation"] == "never"
    assert codes["PF-OWNER-REPO-UNKNOWN"]["escalation"] != "never"


def test_mixed_snapshot_passes_applicable_schema_validation() -> None:
    # BEH-11 Given/When: the structured fleet-analysis result carrying all
    # three owner shapes at once must validate against the canonical schema.
    doc = _mixed_snapshot()
    validate_document(doc)  # raises jsonschema.ValidationError on drift

    schema = load_schema()
    assert schema["$id"] == "urn:ecosystem:plan-fields:v3:schema"
    code_pattern = schema["$defs"]["Diagnostic"]["properties"]["code"]["pattern"]
    assert re.match(code_pattern, "PF-OWNER-REPO-SELF")
    assert re.match(code_pattern, "PF-OWNER-REPO-UNKNOWN")


def test_self_owner_diagnostic_exposes_stable_machine_readable_fields() -> None:
    doc = _mixed_snapshot()
    self_diags = [d for d in doc["diagnostics"] if d["code"] == "PF-OWNER-REPO-SELF"]

    # one PF-OWNER-REPO-SELF per self-owner node (canonical key + git_dir alias)
    assert {d["subject_uri"] for d in self_diags} == {
        "todo://dispatcher/a",
        "todo://dispatcher/b",
    }
    for diag in self_diags:
        assert diag["severity"] == "warning"
        assert diag["subject_uri"] is not None
        assert re.match(_TODO_URI_RE, diag["subject_uri"])
        provenance = diag["provenance"]
        assert provenance is not None
        assert provenance["repo"] == "dispatcher"
        assert provenance["path"]
        assert provenance["line"] >= 1


def test_contract_distinguishes_three_states_without_renormalizing_owner() -> None:
    # FR-05/NFR-04: a consumer reads codes and their absence directly off the
    # schema-validated document — self (PF-OWNER-REPO-SELF present), unknown
    # (PF-OWNER-REPO-UNKNOWN present) and valid external (neither present) —
    # never by re-normalizing owner_ref.raw itself.
    doc = _mixed_snapshot()
    validate_document(doc)

    def codes_for(node_id: str) -> set[str]:
        return {
            d["code"]
            for d in doc["diagnostics"]
            if d["subject_uri"] == node_id and d["code"].startswith("PF-OWNER-REPO")
        }

    assert codes_for("todo://dispatcher/a") == {"PF-OWNER-REPO-SELF"}
    assert codes_for("todo://dispatcher/b") == {"PF-OWNER-REPO-SELF"}
    assert codes_for("todo://dispatcher/c") == set()
    assert codes_for("todo://dispatcher/d") == {"PF-OWNER-REPO-UNKNOWN"}


def test_all_fleet_reporters_share_one_classification_semantics() -> None:
    # FR-05: reporters read the verdict off the shared view helper rather
    # than re-deriving it — this is the ONE classification every fleet
    # reporter (web/TUI/VSCode/MCP) consumes.
    doc = _mixed_snapshot()
    validate_document(doc)
    verdicts = repo_owner_verdicts(doc)

    assert verdicts["todo://dispatcher/a"] == REPO_OWNER_SELF
    assert verdicts["todo://dispatcher/b"] == REPO_OWNER_SELF
    assert verdicts["todo://dispatcher/c"] == REPO_OWNER_EXTERNAL
    assert verdicts["todo://dispatcher/d"] == REPO_OWNER_UNKNOWN
    assert set(verdicts.values()) == {
        REPO_OWNER_SELF,
        REPO_OWNER_EXTERNAL,
        REPO_OWNER_UNKNOWN,
    }


def test_contract_dir_is_the_vendored_pin_this_test_reads() -> None:
    # sanity: the registry this test asserts against is the actual vendored
    # contract shipped with plan-fields, not an incidental fixture copy.
    assert (Path(CONTRACT_DIR) / "PINNED.txt").exists()


def test_reporters_do_not_rederive_repo_owner_classification() -> None:
    """BEH-11: все fleet-reporters разделяют ОДНУ классификацию (FR-05, NFR-02).

    Архитектурная проверка вместо перечисления живых reporters: чтение кодов
    `PF-OWNER-REPO-*` обратно в вердикт разрешено ровно одному модулю —
    `plan_fields/views.py`; эмитирует их ровно один — `plan_fields/fleet_api.py`.
    Reporter (web/TUI/VSCode/MCP или будущий), классифицирующий владельца
    самостоятельно — по кодам или повторной нормализацией `owner_ref.raw`, —
    обязан упомянуть код и покраснит этот тест, пока не перейдёт на общий
    helper `repo_owner_verdicts`.
    """
    repo_root = Path(__file__).resolve().parents[3]
    allowed = {
        repo_root / "packages/plan-fields/src/plan_fields/views.py",
        repo_root / "packages/plan-fields/src/plan_fields/fleet_api.py",
    }
    offenders: list[str] = []
    for scope in ("dispatcher", "packages/plan-fields/src"):
        for py in sorted((repo_root / scope).rglob("*.py")):
            if py in allowed or "__pycache__" in py.parts:
                continue
            text = py.read_text(encoding="utf-8")
            if "PF-OWNER-REPO-SELF" in text or "PF-OWNER-REPO-UNKNOWN" in text:
                offenders.append(str(py.relative_to(repo_root)))
    assert not offenders, (
        "классификация repo-owner перевыведена вне общего helper: "
        f"{offenders} — используйте plan_fields.repo_owner_verdicts"
    )
