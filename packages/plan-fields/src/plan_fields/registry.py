"""Epics registry — the fleet-layer half of the stream axis (epics/v1).

`epic.py` proves a tag is well-formed from one repo. This module holds the other half:
membership. It loads the umbrella's ``epics.toml`` — read LIVE by path, never vendored,
because a weekly-changing registry is stale the moment it is pinned (ADR-ECO-010 D5) —
validates it, and downgrades nodes whose epic is unknown or retired.

Two verdicts that must not be confused:

* **no registry given** — nothing is downgraded, and that is not the same as "everything
  is fine". A caller that wants the typo guard must pass a registry; a caller that cannot
  reach one gets an honest absence rather than a silent pass.
* **registry given** — `EP-UNKNOWN` / `EP-MOVED` are emitted and the node's
  ``epic_classification`` becomes ``invalid``, because a value the registry does not know
  cannot be counted into any aggregate.

The schema this validates against is the vendored ``contract_epics/registry.schema.json``;
the VALUES it validates are the umbrella's. Shape is pinned, content is live.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CONTRACT_DIR = Path(__file__).parent / "contract_epics"
_REGISTRY_SCHEMA = _CONTRACT_DIR / "registry.schema.json"


@dataclass(frozen=True)
class EpicsRegistry:
    """Loaded registry values plus the diagnostics found while loading them."""

    programs: dict[str, dict[str, Any]] = field(default_factory=dict)
    epics: dict[str, dict[str, Any]] = field(default_factory=dict)
    defect_classes: dict[str, dict[str, Any]] = field(default_factory=dict)
    coverage_policy: dict[str, Any] = field(default_factory=dict)
    diagnostics: tuple[dict[str, Any], ...] = ()

    def kind_of(self, epic_id: str) -> str | None:
        """`ecosystem` / `external` for a known epic, else None."""
        program = self.programs.get(epic_id.split(".", 1)[0])
        return None if program is None else program.get("kind")

    def resolve(self, epic_id: str) -> tuple[str | None, str | None]:
        """Resolve one epic id to ``(final_id, diagnostic_code)``.

        A tombstoned id resolves to its ``moved_to`` target: a rename must not split one
        stream into two rows in every aggregate. Whether the retired spelling is ALLOWED
        on this artifact is a separate question — it depends on whether the artifact is
        historical, which this layer cannot see — so the caller decides what to do with
        ``EP-MOVED``.
        """
        entry = self.epics.get(epic_id)
        if entry is None:
            return None, "EP-UNKNOWN"
        moved_to = entry.get("moved_to")
        if moved_to is not None:
            return moved_to, "EP-MOVED"
        return epic_id, None


def _diag(
    code: str,
    severity: str,
    subject_key: str | None,
    message: str,
    raw: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "message": message,
        "subject_key": subject_key,
    }
    if raw is not None:
        out["raw"] = raw
    return out


def load_registry(path: Path) -> EpicsRegistry:
    """Load and validate ``epics.toml``, returning values plus EP-REG-* diagnostics.

    Loading never raises on a bad registry: the caller needs the findings, and a raise
    would turn "the registry has a defect" into "the tool is broken", which reads to an
    operator as the same thing as an outage.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return EpicsRegistry(
            diagnostics=(
                _diag(
                    "EP-REG-POLICY-INVALID",
                    "error",
                    None,
                    f"epics registry not found at {path}",
                ),
            )
        )
    except tomllib.TOMLDecodeError as exc:
        return EpicsRegistry(
            diagnostics=(
                _diag(
                    "EP-REG-POLICY-INVALID",
                    "error",
                    None,
                    f"epics registry is not valid TOML: {exc}",
                ),
            )
        )

    # Shape BEFORE content, and before anything else touches the sections. A section
    # that is not a table cannot be iterated at all, so a check running first would
    # raise on it — and "the tool crashed" reads to an operator exactly like "the
    # registry is fine and something else broke". Ordering is the whole guarantee here:
    # this pass previously sat after the structural one and protected nothing.
    diagnostics: list[dict[str, Any]] = []
    sections: dict[str, dict[str, Any]] = {}
    for key in ("programs", "epics", "defect_classes", "coverage_policy"):
        value = data.get(key, {})
        if isinstance(value, dict):
            sections[key] = {
                k: v
                for k, v in value.items()
                if key == "coverage_policy" or isinstance(v, dict)
            }
            for k, v in value.items():
                if key != "coverage_policy" and not isinstance(v, dict):
                    diagnostics.append(
                        _diag(
                            "EP-REG-MALFORMED",
                            "error",
                            str(k),
                            "registry entry is not the shape the schema declares",
                            type(v).__name__,
                        )
                    )
            continue
        sections[key] = {}
        diagnostics.append(
            _diag(
                "EP-REG-MALFORMED",
                "error",
                key,
                "registry section is not the shape the schema declares",
                type(value).__name__,
            )
        )
    diagnostics.extend(_structural_diagnostics({**data, **sections}))
    programs = sections["programs"]
    epics = sections["epics"]

    # Referential checks — these hold BETWEEN keys and no JSON Schema can express them
    # (epics/v1 fixtures/README.md names exactly these four).
    for key, entry in epics.items():
        program = key.split(".", 1)[0]
        if program not in programs:
            diagnostics.append(
                _diag(
                    "EP-REG-PROGRAM-UNKNOWN",
                    "error",
                    key,
                    "epic key's program prefix is not a declared program",
                    program,
                )
            )
        moved_to = entry.get("moved_to")
        if moved_to is None:
            continue
        target = epics.get(moved_to)
        if target is None:
            diagnostics.append(
                _diag(
                    "EP-REG-MOVED-DANGLING",
                    "error",
                    key,
                    "moved_to names an epic that does not exist",
                    moved_to,
                )
            )
        elif "moved_to" in target:
            chain = _walk(epics, key)
            code = "EP-REG-MOVED-CYCLE" if chain is None else "EP-REG-MOVED-CHAIN"
            message = (
                "moved_to relations form a cycle"
                if chain is None
                else "moved_to points at another tombstone; "
                "a move must name the final id"
            )
            diagnostics.append(_diag(code, "error", key, message, moved_to))

    diagnostics.extend(_schema_backstop(data, diagnostics))
    return EpicsRegistry(
        programs=programs,
        epics=epics,
        defect_classes=sections["defect_classes"],
        coverage_policy=sections["coverage_policy"],
        diagnostics=tuple(
            sorted(diagnostics, key=lambda d: (d["code"], d.get("subject_key") or ""))
        ),
    )


def _schema_backstop(data: dict[str, Any], found: list[dict[str, Any]]):
    """The vendored schema, run AFTER the explicit checks, for what they do not name.

    The explicit checks own classification (see `_structural_diagnostics`), but they
    only cover the defects that have a code. Without this pass the module's promise to
    validate against `registry.schema.json` would be prose: a registry could violate
    the schema in an unclassified way and load clean. Anything the explicit pass
    already reported is skipped, so one defect never appears twice under two codes.

    jsonschema is an optional dependency of the parser path; when it is absent the
    structural half still runs, and the absence is reported rather than swallowed.
    """
    try:
        from jsonschema import Draft202012Validator
    except ImportError:  # pragma: no cover - exercised only without the extra
        return [
            _diag(
                "EP-REG-MALFORMED",
                "warning",
                None,
                "jsonschema unavailable; schema backstop did not run",
            )
        ]
    schema = json.loads(_REGISTRY_SCHEMA.read_text(encoding="utf-8"))
    # Dedup by the whole path, not by the leaf. `[coverage_policy]` with a bad ratio is
    # reported once as EP-REG-POLICY-INVALID against `coverage_policy`, while the schema
    # errors point at `coverage_policy.robin_cutover_todo`; matching only the leaf let
    # the same defect through a second time under a second code, which is worse than no
    # backstop — two codes for one defect makes a reader hunt for a defect that is not
    # there.
    covered = {d.get("subject_key") for d in found if d.get("subject_key")}
    out: list[dict[str, Any]] = []
    for err in Draft202012Validator(schema).iter_errors(data):
        path = [str(part) for part in err.path]
        if covered & set(path):
            continue
        subject = path[1] if len(path) > 1 else (path[0] if path else None)
        out.append(
            _diag(
                "EP-REG-MALFORMED",
                "error",
                subject,
                "registry does not satisfy the vendored schema",
                err.message,
            )
        )
        if subject:
            covered.add(subject)
    return out


def _walk(epics: dict[str, Any], start: str) -> list[str] | None:
    """Follow moved_to from `start`; None when the chain closes on itself."""
    seen = [start]
    cur = start
    while True:
        nxt = epics.get(cur, {}).get("moved_to")
        if nxt is None:
            return seen
        if nxt in seen:
            return None
        seen.append(nxt)
        cur = nxt


def _structural_diagnostics(data: dict[str, Any]):
    """Structural defects, checked explicitly rather than read out of jsonschema text.

    The schema stays the contract — `test_registry_schema_agrees_with_the_explicit_checks`
    asserts it rejects exactly the fixtures this function classifies as structural, and
    `_schema_backstop` runs it for anything unclassified. But its ERROR MESSAGES are
    not a classification API: with the LiveEpic/TombstonedEpic ``oneOf``, every shape
    violation collapses into one "not valid under any of the given schemas", so mapping
    codes from message text would report four different defects as the same one.
    """
    policy = data.get("coverage_policy")
    if not isinstance(policy, dict):
        yield _diag(
            "EP-REG-POLICY-INVALID",
            "error",
            "coverage_policy",
            "[coverage_policy] is absent",
        )
    else:
        missing = [
            k for k in _POLICY_RATIOS + ("missing_error_after",) if k not in policy
        ]
        bad = [
            k
            for k in _POLICY_RATIOS
            if isinstance(policy.get(k), (int, float)) and not 0 <= policy[k] <= 1
        ]
        date = policy.get("missing_error_after")
        if missing or bad or (date is not None and not _DATE_RE.fullmatch(str(date))):
            detail = "; ".join(
                filter(
                    None,
                    [
                        f"missing: {', '.join(missing)}" if missing else "",
                        f"out of range: {', '.join(bad)}" if bad else "",
                        f"missing_error_after={date}"
                        if date is not None and not _DATE_RE.fullmatch(str(date))
                        else "",
                    ],
                )
            )
            yield _diag(
                "EP-REG-POLICY-INVALID",
                "error",
                "coverage_policy",
                "[coverage_policy] is incomplete or malformed",
                detail,
            )

    for name, program in data.get("programs", {}).items():
        kind = program.get("kind")
        if kind not in ("ecosystem", "external"):
            yield _diag(
                "EP-REG-KIND-UNKNOWN",
                "error",
                name,
                "program declares a kind outside {ecosystem, external}",
                str(kind),
            )

    for key, entry in data.get("epics", {}).items():
        status = entry.get("status")
        if "moved_to" in entry:
            if status is not None:
                yield _diag(
                    "EP-REG-MOVED-STATUS",
                    "error",
                    key,
                    "entry carries moved_to together with a live status",
                    status,
                )
            continue
        if (
            status in ("active", "paused", "done", "abandoned")
            and "opened" not in entry
        ):
            yield _diag(
                "EP-REG-OPENED-MISSING",
                "error",
                key,
                "epic with status active/paused/done/abandoned carries no opened date",
            )
        if status in ("done", "abandoned") and "closed" not in entry:
            yield _diag(
                "EP-REG-CLOSED-MISSING",
                "error",
                key,
                "epic with status done/abandoned carries no closed date",
            )
        if status == "standing":
            extra = [f for f in ("goal", "closed") if f in entry]
            if extra:
                yield _diag(
                    "EP-REG-STANDING-FIELDS",
                    "error",
                    key,
                    "standing epic carries goal or closed",
                    ", ".join(extra),
                )


_POLICY_RATIOS = ("robin_cutover_todo", "robin_cutover_issues", "robin_cutover_prs")
_DATE_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")


def apply_registry(
    snapshot: dict[str, Any], registry: EpicsRegistry
) -> list[dict[str, Any]]:
    """Downgrade nodes whose epic the registry does not know, and report why.

    Mutates ``epic_classification`` in place and returns the new diagnostics, mirroring
    how ``check_fleet`` layers graph-semantic findings onto a built snapshot.
    """
    out: list[dict[str, Any]] = []
    for node in snapshot["nodes"]:
        epic = node.get("epic")
        if epic is not None:
            final, code = registry.resolve(epic)
            if code is not None:
                node["epic_classification"] = "invalid"
                message = (
                    "epic is absent from the registry"
                    if code == "EP-UNKNOWN"
                    else f"epic is retired; the registry moves it to {final}"
                )
                out.append(_node_diag(code, node, message, epic))
        # The defect axis is checked against the SAME registry but never touches
        # `epic_classification`: an unknown defect class says nothing about which
        # stream the item belongs to, and folding them would drop in-epic defects
        # out of the defect-class counts (ADR-ECO-010 D2).
        defect = node.get("defect")
        if defect is not None and defect not in registry.defect_classes:
            out.append(
                _node_diag(
                    "EP-DEFECT-UNKNOWN",
                    node,
                    "defect class is absent from the registry",
                    defect,
                )
            )
    return sorted(out, key=lambda d: (d["code"], d["subject_uri"]))


def _node_diag(
    code: str, node: dict[str, Any], message: str, raw: str
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": "error",
        "message": message,
        "subject_uri": node["node_id"],
        "related_uri": None,
        "rule_id": None,
        "raw": raw,
        "provenance": node["provenance"],
    }
