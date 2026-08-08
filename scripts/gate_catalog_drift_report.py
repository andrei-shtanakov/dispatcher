"""Advisory upstream-drift report for steward-gate-catalog/v1 (guarantee B).

Answers one question — "has `profiles/gate-catalog.yaml` in steward moved
away from the copy vendored here?" — and answers it *about a named upstream*:
the resolved commit, the remote it came from, and the hash recomputed from
the file actually checked out.

This is an observation, not a gate. It runs on a schedule and on demand,
never on a dispatcher pull request: a commit in a neighbouring repository
must not be able to redden this repository's PR checks. Guarantee A — that
the vendored copy matches its own manifest — is
`tests/test_gate_catalog_vendor.py`, and it needs no network.

The structural difference from the directory-shaped siblings
(`upstream_drift_report.py`, `actions_drift_report.py`): this contract's
surface is a single file, so the comparison is one sha256 against the entry
the vendored `manifest.json` records for `gate-catalog.yaml`. The probe that
proves the upstream path really is the catalog (and not some other YAML a
moved checkout happened to land on) is the file's own shape: a mapping
carrying `version` and `obligation_vocabulary`.

Nothing here re-vendors anything, and nothing here rewrites an expected
hash. A red run means a human owes a deliberate re-vendor PR via
`scripts/revendor_steward_gate_catalog.sh` — reading what upstream changed
and why. Editing `manifest.json` to make this green would delete the only
signal it produces.

Run:  python scripts/gate_catalog_drift_report.py <upstream-catalog-file> \
          [--vendored <dir>] [--upstream-root <repo>] [--ref <ref>]
Exit: 0 no drift, 1 drift, 2 upstream unavailable.
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

import yaml

NO_DRIFT = "no_drift"
DRIFT = "drift"
UNAVAILABLE = "unavailable"

_EXIT = {NO_DRIFT: 0, DRIFT: 1, UNAVAILABLE: 2}

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VENDORED_REL = Path("contracts/steward-gate-catalog/v1")
_SURFACE_FILE = "gate-catalog.yaml"
_UPSTREAM_PATH = "profiles/gate-catalog.yaml"


@dataclass(frozen=True)
class Report:
    outcome: str
    summary: str
    # The pinned `producer_commit`, when the vendored manifest could be read.
    # Carried out of `compare()` so `main()` can reuse it for the commit-log
    # section without reading the manifest a second time.
    vendored_pin: str | None = None

    @property
    def exit_code(self) -> int:
        return _EXIT[self.outcome]


def compare(
    upstream_file: Path,
    vendored_dir: Path,
    provenance: dict[str, str],
) -> Report:
    """Compare upstream's recomputed file hash against the vendored manifest.

    Upstream is hashed fresh from the file on disk (never from a manifest it
    does not publish). The vendored side is read from `manifest.json` as it
    stands — that manifest's own internal consistency is guarantee A's job,
    checked elsewhere and not repeated here.
    """
    provenance_lines = [
        f"- upstream remote: `{provenance.get('remote', '?')}`",
        f"- upstream ref requested: `{provenance.get('ref', '?')}`",
        f"- upstream commit resolved: `{provenance.get('commit', '?')}`",
    ]

    def report(outcome: str, body: list[str], *, pin: str | None = None) -> Report:
        return Report(outcome, "\n".join([*body, "", *provenance_lines]), pin)

    try:
        upstream_bytes = upstream_file.read_bytes()
    except OSError as exc:
        return report(
            UNAVAILABLE,
            [
                "## Upstream unavailable",
                "",
                f"No readable upstream catalog at `{upstream_file}` "
                f"(`{type(exc).__name__}`). This is **unknown**, not "
                "“no drift” — nothing was compared.",
            ],
        )

    # The probe: the bytes must parse as the catalog's own shape. A checkout
    # whose layout moved would otherwise hash whatever file the stale path
    # now points at and report drift instead of unavailable.
    try:
        parsed = yaml.safe_load(upstream_bytes)
    except yaml.YAMLError as exc:
        return report(
            UNAVAILABLE,
            [
                "## Upstream unreadable",
                "",
                f"`{upstream_file}` is not parseable YAML "
                f"(`{type(exc).__name__}`). Nothing was compared.",
            ],
        )
    if not isinstance(parsed, dict) or not {
        "version",
        "obligation_vocabulary",
    } <= set(parsed):
        return report(
            UNAVAILABLE,
            [
                "## Upstream unrecognized",
                "",
                f"`{upstream_file}` parses but does not look like the gate "
                "catalog (no `version` + `obligation_vocabulary` mapping). "
                "Nothing was compared — the path may no longer point at the "
                "contract.",
            ],
        )

    manifest_path = vendored_dir / "manifest.json"
    try:
        vendored_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return report(
            UNAVAILABLE,
            [
                "## Vendored manifest unreadable",
                "",
                f"`{manifest_path}` could not be read as JSON "
                f"(`{type(exc).__name__}`). A broken local checkout is not "
                "upstream moving — nothing was compared.",
            ],
        )

    pinned_commit = vendored_manifest.get("producer_commit")
    surface = vendored_manifest.get("surface")
    vendored_sha: str | None = None
    if isinstance(surface, list):
        vendored_sha = next(
            (
                str(e["sha256"])
                for e in surface
                if isinstance(e, dict) and e.get("path") == _SURFACE_FILE
            ),
            None,
        )
    if not isinstance(pinned_commit, str) or vendored_sha is None:
        return report(
            UNAVAILABLE,
            [
                "## Vendored manifest malformed",
                "",
                f"`{manifest_path}` is missing `producer_commit` or a "
                f"`surface` entry for `{_SURFACE_FILE}`. Nothing was "
                "compared.",
            ],
        )

    upstream_sha = hashlib.sha256(upstream_bytes).hexdigest()
    provenance_lines.append(f"- pinned producer_commit: `{pinned_commit}`")
    provenance_lines.append(f"- vendored sha256: `{vendored_sha}`")
    provenance_lines.append(f"- upstream sha256 (recomputed): `{upstream_sha}`")

    if upstream_sha == vendored_sha:
        return report(
            NO_DRIFT,
            ["## No upstream drift", "", "Upstream matches the vendored copy."],
            pin=pinned_commit,
        )
    return report(
        DRIFT,
        [
            "## Upstream drift",
            "",
            f"- differing content: `{_SURFACE_FILE}`",
            "",
            "This is advisory. It does not block any pull request, and "
            "nothing was changed automatically.",
            "",
            "**Next step: a deliberate re-vendor PR** — read what upstream "
            "changed and why, and re-vendor at the new commit with "
            "`scripts/revendor_steward_gate_catalog.sh`. Do not silence "
            "this by hand-editing a hash; the hash is the signal.",
        ],
        pin=pinned_commit,
    )


def _git(repo: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True
        )
    except OSError:
        return "?"
    return out.stdout.strip() if out.returncode == 0 else "?"


def _commits_since_pin(
    upstream_root: Path, pin: str | None, resolved: str, path: str
) -> str:
    """Who touched `path` since the pin, not just whether it drifted.

    A reverted change produces commits here with no drift in `compare()`,
    and the two questions need different answers. A shallow clone that lacks
    the pinned commit says so plainly rather than guessing or going quiet.
    """
    header = "## Commits since the pin"
    if not pin or pin == "?" or not resolved or resolved == "?":
        return f"{header}\n\nPin or resolved commit unknown; nothing was queried."

    has_pin = subprocess.run(
        ["git", "-C", str(upstream_root), "cat-file", "-e", f"{pin}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    if has_pin.returncode != 0:
        return (
            f"{header}\n\nPinned commit `{pin}` is not present in this "
            "checkout (likely a shallow clone) — cannot list commits since "
            "it."
        )

    log = subprocess.run(
        [
            "git",
            "-C",
            str(upstream_root),
            "log",
            "--oneline",
            f"{pin}..{resolved}",
            "--",
            path,
        ],
        capture_output=True,
        text=True,
    )
    if log.returncode != 0:
        return f"{header}\n\n`git log` failed; nothing was queried."

    lines = [ln for ln in log.stdout.strip().splitlines() if ln]
    if not lines:
        return f"{header}\n\nNo commits touched `{path}` since the pin."
    return f"{header}\n\n" + "\n".join(f"- `{ln}`" for ln in lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "upstream_file",
        type=Path,
        help="checked-out upstream profiles/gate-catalog.yaml",
    )
    parser.add_argument("--vendored", type=Path, default=_REPO_ROOT / _VENDORED_REL)
    parser.add_argument(
        "--upstream-root",
        type=Path,
        default=None,
        help="repo root of the upstream checkout, for provenance and the commit log",
    )
    parser.add_argument("--ref", default="?", help="ref that was requested")
    args = parser.parse_args(argv)

    root = args.upstream_root or args.upstream_file.parent
    provenance = {
        "commit": _git(root, "rev-parse", "HEAD"),
        "remote": _git(root, "remote", "get-url", "origin"),
        "ref": args.ref,
    }
    try:
        report = compare(args.upstream_file, args.vendored, provenance)
    except Exception as exc:  # noqa: BLE001 — last-resort net, see below
        # `compare()` guards every OSError it can anticipate. This is the
        # backstop for whatever it did not anticipate: an uncaught exception
        # exits the interpreter with 1, the same code as DRIFT, which would
        # dress an unhandled failure as a positive finding about upstream.
        # Anything reaching here is UNAVAILABLE, never a finding.
        report = Report(
            UNAVAILABLE,
            "## Reporter failed\n\n"
            f"`{type(exc).__name__}: {exc}`. Nothing was compared — this is "
            "unknown, not drift.",
        )
    summary = report.summary
    if args.upstream_root is not None:
        summary += "\n\n" + _commits_since_pin(
            args.upstream_root,
            report.vendored_pin,
            provenance["commit"],
            _UPSTREAM_PATH,
        )
    print(summary)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as handle:
            handle.write(summary + "\n")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
