"""plan-fields CLI: parse a TODO.md, validate a document, or run conformance."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jsonschema.exceptions import ValidationError

from plan_fields.canonical import canonical_dumps
from plan_fields.parser import parse_todo
from plan_fields.validator import run_conformance, validate_document


def _cmd_parse(args: argparse.Namespace) -> int:
    text = Path(args.file).read_text(encoding="utf-8")
    doc = parse_todo(text, args.repo, path=args.path, generated_at=args.generated_at)
    sys.stdout.write(canonical_dumps(doc))
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    doc = json.loads(Path(args.doc).read_text(encoding="utf-8"))
    try:
        validate_document(doc)
    except ValidationError as err:
        print(f"INVALID: {str(err).splitlines()[0]}", file=sys.stderr)
        return 1
    print("valid")
    return 0


def _cmd_conformance(_: argparse.Namespace) -> int:
    results = run_conformance()
    for r in results:
        mark = "ok  " if r.ok else "FAIL"
        print(f"{mark} {r.name}{('  — ' + r.detail) if r.detail else ''}")
    failed = [r for r in results if not r.ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} fixtures conform")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="plan-fields", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="parse a TODO.md into canonical JSON")
    p.add_argument("file")
    p.add_argument("--repo", required=True)
    p.add_argument("--path", default="TODO.md")
    p.add_argument("--generated-at", default="1970-01-01T00:00:00Z")
    p.set_defaults(fn=_cmd_parse)

    v = sub.add_parser("validate", help="validate a document against the schema")
    v.add_argument("doc")
    v.set_defaults(fn=_cmd_validate)

    c = sub.add_parser("conformance", help="run the vendored fixture suite")
    c.set_defaults(fn=_cmd_conformance)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
