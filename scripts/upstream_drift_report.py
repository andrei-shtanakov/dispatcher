"""Advisory upstream-drift report (PF-6, guarantee B).

Answers one question — "has canon in prograph-vault moved away from the copy
vendored here?" — and answers it *about a named upstream*: the resolved
commit, the remote it came from, and the tree hash recomputed from the files
that were actually checked out.

This is an observation, not a gate. It runs on a schedule and on demand, never
on a dispatcher pull request: a commit in a neighbouring repository must not
be able to redden this repository's PR checks. Guarantee A — that the vendored
copy matches its own manifest — is the PR gate, and it needs no network.

Nothing here re-vendors anything. A red run means a human owes a deliberate
re-vendor PR, which is a review of what upstream changed and why. Editing an
expected hash to make this green would delete the only signal it produces.

Run:  python scripts/upstream_drift_report.py <canon-dir> [--vendored <dir>]
Exit: 0 no drift, 1 drift, 2 canon unavailable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

NO_DRIFT = "no_drift"
DRIFT = "drift"
UNAVAILABLE = "unavailable"

_EXIT = {NO_DRIFT: 0, DRIFT: 1, UNAVAILABLE: 2}
_META = {"manifest.json", "drift-control.md", "PINNED.txt"}

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VENDORED_REL = Path("packages/plan-fields/src/plan_fields/contract")


@dataclass(frozen=True)
class Report:
    outcome: str
    summary: str

    @property
    def exit_code(self) -> int:
        return _EXIT[self.outcome]


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _live_surface(root: Path) -> dict[str, str | None]:
    return {
        p.relative_to(root).as_posix(): _sha256(p)
        for p in root.rglob("*")
        if p.is_file() and p.relative_to(root).as_posix() not in _META
    }


def _tree_sha256(surface: dict[str, str | None]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(surface):
        digest.update(f"{rel}\0{surface[rel]}\n".encode())
    return digest.hexdigest()


def compare(canon_dir: Path, vendored_dir: Path, provenance: dict[str, str]) -> Report:
    """Compare the two surfaces as they exist on disk.

    Both sides are hashed from their files, never read out of a manifest. A
    manifest-to-manifest comparison would call an upstream that changed files
    without regenerating its fingerprint "no drift" — the one case where the
    fingerprint is the least trustworthy thing in the directory.
    """
    provenance_lines = [
        f"- upstream remote: `{provenance.get('remote', '?')}`",
        f"- upstream ref requested: `{provenance.get('ref', '?')}`",
        f"- upstream commit resolved: `{provenance.get('commit', '?')}`",
    ]

    def report(outcome: str, body: list[str]) -> Report:
        return Report(outcome, "\n".join([*body, "", *provenance_lines]))

    if not canon_dir.is_dir():
        return report(
            UNAVAILABLE,
            [
                "## Upstream unavailable",
                "",
                f"No canon directory at `{canon_dir}`. This is **unknown**, not "
                "“no drift” — nothing was compared.",
            ],
        )
    try:
        json.loads((canon_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return report(
            UNAVAILABLE,
            [
                "## Upstream unreadable",
                "",
                f"Canon manifest could not be read (`{type(exc).__name__}`). "
                "Nothing was compared.",
            ],
        )

    canon = _live_surface(canon_dir)
    vendored = _live_surface(vendored_dir)
    canon_tree = _tree_sha256(canon)
    vendored_tree = _tree_sha256(vendored)
    provenance_lines.append(f"- canon tree_sha256 (recomputed): `{canon_tree}`")
    provenance_lines.append(f"- vendored tree_sha256 (recomputed): `{vendored_tree}`")

    if canon_tree == vendored_tree:
        return report(
            NO_DRIFT,
            ["## No upstream drift", "", "Canon matches the vendored copy."],
        )

    added = sorted(set(canon) - set(vendored))
    removed = sorted(set(vendored) - set(canon))
    changed = sorted(
        rel for rel in set(canon) & set(vendored) if canon[rel] != vendored[rel]
    )
    body = ["## Upstream drift", ""]
    if added:
        body.append(f"- only in canon: {', '.join(f'`{r}`' for r in added)}")
    if removed:
        body.append(
            f"- only in the vendored copy: {', '.join(f'`{r}`' for r in removed)}"
        )
    if changed:
        body.append(f"- differing content: {', '.join(f'`{r}`' for r in changed)}")
    body += [
        "",
        "This is advisory. It does not block any pull request, and nothing was "
        "changed automatically.",
        "",
        "**Next step: a deliberate re-vendor PR** — read what upstream changed, "
        "decide whether to take it, and re-vendor at the new commit. Do not "
        "silence this by hand-editing a hash; the hash is the signal.",
    ]
    return report(DRIFT, body)


def _git(repo: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True
        )
    except OSError:
        return "?"
    return out.stdout.strip() if out.returncode == 0 else "?"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("canon_dir", type=Path, help="checked-out canon directory")
    parser.add_argument("--vendored", type=Path, default=_REPO_ROOT / _VENDORED_REL)
    parser.add_argument(
        "--upstream-root",
        type=Path,
        default=None,
        help="repo root of the canon checkout, for recording its provenance",
    )
    parser.add_argument("--ref", default="?", help="ref that was requested")
    args = parser.parse_args(argv)

    root = args.upstream_root or args.canon_dir
    provenance = {
        "commit": _git(root, "rev-parse", "HEAD"),
        "remote": _git(root, "remote", "get-url", "origin"),
        "ref": args.ref,
    }
    report = compare(args.canon_dir, args.vendored, provenance)
    print(report.summary)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as handle:
            handle.write(report.summary + "\n")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
