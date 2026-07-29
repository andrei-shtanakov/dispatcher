"""plan-fields CLI: parse a TODO.md, validate a document, or run conformance."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from jsonschema.exceptions import ValidationError

from plan_fields import fleet as _fleet
from plan_fields.canonical import canonical_dumps, canonicalize
from plan_fields.fleet_api import RepoInput, check_fleet, parse_fleet
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


def _load_fleet(args: argparse.Namespace):
    """Scan the workspace and the identity index the manifest declares.

    Without ``--manifest`` there is nothing to declare identity, so the scan
    keys itself (origin-derived) and the index is exactly those names with no
    aliases — the pre-index behaviour, expressed in the new type.
    """
    index = _fleet.manifest_index(Path(args.manifest)) if args.manifest else None
    fleet = _fleet.scan_workspace(Path(args.root), index)
    if index is None:
        index = _fleet.ManifestIndex(frozenset(fleet), {})
    return fleet, index


def _cmd_fleet_suggest(args: argparse.Namespace) -> int:
    fleet, index = _load_fleet(args)
    # only UNAMBIGUOUS references contribute a reusable slug, keyed by the exact
    # target line — so an ambiguous slug never gets assigned to several items
    clean: dict[str, dict[int, str]] = {}
    for r in _fleet.classify_legacy(fleet, index):
        if r.resolution == "clean-open" and r.target_line is not None:
            clean.setdefault(r.target_repo, {})[r.target_line] = r.slug
    rows = []
    for repo in sorted(fleet):
        for s in _fleet.suggest_ids(repo, fleet[repo], clean.get(repo, {})):
            rows.append(
                {
                    "repo": s.repo,
                    "line": s.line,
                    "suggested_id": s.suggested_id,
                    "source": s.source,
                    "text": s.text,
                }
            )
    print(json.dumps({"suggestions": rows}, indent=2, ensure_ascii=False))
    return 0


def _cmd_fleet_legacy(args: argparse.Namespace) -> int:
    fleet, index = _load_fleet(args)
    refs = _fleet.classify_legacy(fleet, index)
    rows = [
        {
            "source": f"{r.source_repo}/TODO.md:{r.source_line}",
            "raw": r.raw,
            "target_repo": r.target_repo,
            "slug": r.slug,
            "resolution": r.resolution,
            "target_line": r.target_line,
        }
        for r in refs
    ]
    counts: dict[str, int] = {}
    for r in refs:
        counts[r.resolution] = counts.get(r.resolution, 0) + 1
    print(json.dumps({"counts": counts, "refs": rows}, indent=2, ensure_ascii=False))
    return 0


def _head_commit(repo_dir: Path) -> str | None:
    """Resolve a checkout's HEAD commit from files only (no git subprocess)."""
    try:
        head = (repo_dir / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not head.startswith("ref: "):
        return head or None  # detached HEAD is a raw sha
    ref = head[5:].strip()
    try:
        return (repo_dir / ".git" / ref).read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    try:
        for line in (
            (repo_dir / ".git" / "packed-refs").read_text(encoding="utf-8").splitlines()
        ):
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1].strip() == ref:
                return parts[0]
    except OSError:
        return None
    return None


def _fleet_inputs(root: Path, index: _fleet.ManifestIndex) -> list[RepoInput]:
    """Freeze one RepoInput per manifest repo from disk — the sensor's job.

    The manifest is the authority for which repos exist; a declared repo with no
    checkout here is ``available=False``, one with a checkout but no TODO.md has
    ``todo_text=None``. This disk read is deliberately OUTSIDE ``parse_fleet``.

    Checkouts resolve through the index, so a repo cloned into its declared
    ``git_dir`` is found under its key rather than reported as not checked out.
    """
    on_disk = _fleet.checkout_map(root, index)
    inputs: list[RepoInput] = []
    for repo in sorted(index.canonical_keys):
        d = on_disk.get(repo)
        if d is None:
            inputs.append(RepoInput(repo, available=False))
            continue
        todo = d / "TODO.md"
        text = (
            todo.read_text(encoding="utf-8", errors="ignore")
            if todo.is_file()
            else None
        )
        inputs.append(
            RepoInput(repo, todo_text=text, commit=_head_commit(d), available=True)
        )
    return inputs


def _cmd_fleet_graph(args: argparse.Namespace) -> int:
    index = _fleet.manifest_index(Path(args.manifest))
    snap = parse_fleet(_fleet_inputs(Path(args.root), index), index)
    snap["diagnostics"].extend(check_fleet(snap))
    sys.stdout.write(canonical_dumps(canonicalize(snap)))
    return 0


def _cmd_fleet_snapshot(args: argparse.Namespace) -> int:
    fleet, _ = _load_fleet(args)
    print(json.dumps(_fleet.snapshot(fleet), indent=2, ensure_ascii=False))
    return 0


def _cmd_fleet_drift(args: argparse.Namespace) -> int:
    before = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after = json.loads(Path(args.after).read_text(encoding="utf-8"))
    d = _fleet.drift(before, after)
    if d.clean:
        print("clean: only identity/ref fields changed (status/text/count identical)")
        return 0
    print("DRIFT — a backfill moved more than identity:", file=sys.stderr)
    for line in d.detail:
        print(f"  {line}", file=sys.stderr)
    return 1


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

    # PF-2B0 read-only fleet tooling
    def _fleet_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--root", required=True, help="workspace root (holds the repos)")
        p.add_argument("--manifest", default=None, help="workspace-manifest.toml")

    fs = sub.add_parser("fleet-suggest", help="suggest an @id per un-@id'd open item")
    _fleet_args(fs)
    fs.set_defaults(fn=_cmd_fleet_suggest)

    fl = sub.add_parser("fleet-legacy", help="classify every legacy @blocked_by")
    _fleet_args(fl)
    fl.set_defaults(fn=_cmd_fleet_legacy)

    fn = sub.add_parser("fleet-snapshot", help="per-item zero-drift baseline (JSON)")
    _fleet_args(fn)
    fn.set_defaults(fn=_cmd_fleet_snapshot)

    fg = sub.add_parser(
        "fleet-graph",
        help="canonical cross-repo graph (parse_fleet + check_fleet) as contract JSON",
    )
    fg.add_argument("--root", required=True, help="workspace root (holds the repos)")
    fg.add_argument(
        "--manifest", required=True, help="workspace-manifest.toml (fleet authority)"
    )
    fg.set_defaults(fn=_cmd_fleet_graph)

    fd = sub.add_parser("fleet-drift", help="compare two fleet snapshots")
    fd.add_argument("--before", required=True)
    fd.add_argument("--after", required=True)
    fd.set_defaults(fn=_cmd_fleet_drift)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except ValueError as err:
        # The workspace/manifest refusals (an ambiguous git_dir, two plan-bearing
        # checkouts of one repo, two spellings of one repo among the inputs) are
        # the operator's to fix, not stack traces to read. Exit 2 keeps them
        # distinct from a clean "invalid document" / "drift found" verdict (1).
        print(f"plan-fields: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
