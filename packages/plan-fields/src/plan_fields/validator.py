"""Validate documents against the vendored schema and run fixture conformance.

Conformance compares the parser's output to each fixture's pinned ``expected.json``
after canonicalization, ignoring the two INFORMATIVE fields the contract marks
non-normative: ``message`` (human text) and ``raw`` (optional, illustrative in
fixtures). Everything else — codes, severities, URIs, provenance, structure — must
match exactly.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from plan_fields.canonical import canonicalize
from plan_fields.parser import parse_todo
from plan_fields.reuse import detect_reuse

CONTRACT_DIR = Path(__file__).parent / "contract"
FIXTURES_DIR = CONTRACT_DIR / "fixtures"
FIXTURE_REPO = "demo"
FIXTURE_STAMP = "2026-07-28T00:00:00Z"


def load_schema() -> dict[str, Any]:
    return json.loads((CONTRACT_DIR / "schema.json").read_text(encoding="utf-8"))


def validate_document(doc: dict[str, Any]) -> None:
    """Raise jsonschema.ValidationError if doc violates the contract schema."""
    Draft202012Validator(load_schema()).validate(doc)


def _strip(doc: dict[str, Any]) -> dict[str, Any]:
    """Drop non-normative fields (message, raw) for conformance comparison."""
    d = copy.deepcopy(doc)
    for n in d["nodes"]:
        n.pop("raw", None)
    for r in d["references"]:
        r.pop("raw", None)
    for g in d["diagnostics"]:
        g.pop("message", None)
    return d


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""


def _parse_fixture(md: Path) -> dict[str, Any]:
    return parse_todo(
        md.read_text(encoding="utf-8"),
        FIXTURE_REPO,
        path="TODO.md",
        generated_at=FIXTURE_STAMP,
    )


def _bundle_doc(dir_: Path) -> dict[str, Any]:
    ctx = json.loads((dir_ / "context.json").read_text(encoding="utf-8"))
    repo = ctx.get("repo", FIXTURE_REPO)
    stamp = ctx.get("generated_at", FIXTURE_STAMP)
    prev = parse_todo(
        (dir_ / "previous.md").read_text(encoding="utf-8"), repo, generated_at=stamp
    )
    curr = parse_todo(
        (dir_ / "current.md").read_text(encoding="utf-8"), repo, generated_at=stamp
    )
    curr["diagnostics"].extend(detect_reuse(curr, prev))
    return canonicalize(curr)


def run_conformance() -> list[Result]:
    """Parse every fixture and compare (schema-valid + equal to expected)."""
    results: list[Result] = []
    simple = sorted(
        p
        for p in FIXTURES_DIR.rglob("*.md")
        if p.parent.name != "reused-id" and p.stem != "README"
    )
    cases: list[tuple[str, dict[str, Any], Path]] = []
    for md in simple:
        cases.append(
            (
                str(md.relative_to(FIXTURES_DIR)),
                _parse_fixture(md),
                md.with_suffix(".expected.json"),
            )
        )
    bundles = sorted(d for d in FIXTURES_DIR.rglob("reused-id") if d.is_dir())
    for d in bundles:
        cases.append(
            (str(d.relative_to(FIXTURES_DIR)), _bundle_doc(d), d / "expected.json")
        )

    for name, got, exp_path in cases:
        try:
            validate_document(got)
        except ValidationError as err:
            results.append(Result(name, False, f"schema: {err}".splitlines()[0]))
            continue
        expected = canonicalize(json.loads(exp_path.read_text(encoding="utf-8")))
        if _strip(got) == _strip(expected):
            results.append(Result(name, True))
        else:
            results.append(Result(name, False, "parsed output != expected"))
    return results
