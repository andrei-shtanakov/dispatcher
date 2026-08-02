"""Advisory upstream-drift report for github-checker-actions/v1 (guarantee B).

Answers one question — "has `contracts/actions/v1` in github-checker moved
away from the copy vendored here?" — and answers it *about a named upstream*:
the resolved commit, the remote it came from, and a tree hash recomputed from
the files actually checked out.

This is an observation, not a gate. It runs on a schedule and on demand,
never on a dispatcher pull request: a commit in a neighbouring repository
must not be able to redden this repository's PR checks. Guarantee A — that
the vendored copy matches its own manifest — is `tests/test_contract_ingest.py`
plus the shape checked by `scripts/revendor_github_checker_actions.sh`, and it
needs no network.

The structural difference from the plan-fields sibling (`upstream_drift_report.py`):
upstream publishes no manifest of its own for this contract — only
`README.md`, `actions.schema.json` and `fixtures/`. So "what upstream's tree
hash is" has to be *recomputed* here, with the exact algorithm the vendored
copy was built with (`vendor_manifest.build_manifest`), and compared against
the `tree_sha256` already recorded in the vendored manifest. That makes "what
counts as the surface" a decision this reporter makes, not one upstream
publishes — `vendor_manifest.EXCLUDED_NAMES` decides it, by filename, at any
depth. If upstream ever ships a file called `PINNED.txt` or `manifest.json`,
a naive recompute would silently drop it from the comparison and could report
"no drift" about a tree that had, in fact, gained a file. That case is
detected explicitly below and always reported as drift, naming the file.

Nothing here re-vendors anything, and nothing here rewrites an expected hash.
A red run means a human owes a deliberate re-vendor PR via
`scripts/revendor_github_checker_actions.sh` — reading what upstream changed
and why. Editing `manifest.json`'s `tree_sha256` to make this green would
delete the only signal it produces.

Run:  python scripts/actions_drift_report.py <upstream-contract-dir> \
          [--vendored <dir>] [--upstream-root <repo>] [--ref <ref>]
Exit: 0 no drift, 1 drift, 2 upstream unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from vendor_manifest import EXCLUDED_NAMES, build_manifest

NO_DRIFT = "no_drift"
DRIFT = "drift"
UNAVAILABLE = "unavailable"

_EXIT = {NO_DRIFT: 0, DRIFT: 1, UNAVAILABLE: 2}

_REPO_ROOT = Path(__file__).resolve().parents[1]
_VENDORED_REL = Path("contracts/github-checker-actions/v1")
_UPSTREAM_SUBDIR = "contracts/actions/v1"
_PROBE = "actions.schema.json"


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


def _surface_dict(entries: list[dict[str, object]]) -> dict[str, str]:
    return {str(e["path"]): str(e["sha256"]) for e in entries}


def compare(
    upstream_dir: Path,
    vendored_dir: Path,
    provenance: dict[str, str],
) -> Report:
    """Compare upstream's recomputed tree hash against the vendored manifest.

    Upstream is hashed fresh from the files on disk (never from a manifest it
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

    if not upstream_dir.is_dir():
        return report(
            UNAVAILABLE,
            [
                "## Upstream unavailable",
                "",
                f"No upstream contract directory at `{upstream_dir}`. This is "
                "**unknown**, not “no drift” — nothing was compared.",
            ],
        )

    try:
        json.loads((upstream_dir / _PROBE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return report(
            UNAVAILABLE,
            [
                "## Upstream unreadable",
                "",
                f"Upstream probe `{_PROBE}` could not be read "
                f"(`{type(exc).__name__}`). Nothing was compared.",
            ],
        )

    if not vendored_dir.is_dir():
        return report(
            UNAVAILABLE,
            [
                "## Vendored copy unavailable",
                "",
                f"No vendored contract at `{vendored_dir}`. A broken local "
                "checkout is not upstream moving — nothing was compared.",
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
                f"(`{type(exc).__name__}`). Nothing was compared.",
            ],
        )

    vendored_tree = vendored_manifest.get("tree_sha256")
    vendored_surface = vendored_manifest.get("surface")
    pinned_commit = vendored_manifest.get("producer_commit")
    if (
        not isinstance(vendored_tree, str)
        or not isinstance(vendored_surface, list)
        or not isinstance(pinned_commit, str)
    ):
        return report(
            UNAVAILABLE,
            [
                "## Vendored manifest malformed",
                "",
                f"`{manifest_path}` is missing `tree_sha256`, `producer_commit`, "
                "or a `surface` list of `{path, sha256}` pairs. Nothing was "
                "compared.",
            ],
        )
    try:
        vendored_hashes = _surface_dict(vendored_surface)
    except (KeyError, TypeError, ValueError):
        return report(
            UNAVAILABLE,
            [
                "## Vendored manifest malformed",
                "",
                f"`{manifest_path}`'s `surface` entries are not `{{path, sha256}}` "
                "pairs. Nothing was compared.",
            ],
        )

    provenance_lines.append(f"- pinned producer_commit: `{pinned_commit}`")
    provenance_lines.append(f"- vendored tree_sha256: `{vendored_tree}`")

    # Upstream ships no meta files of its own; a file at any depth that
    # happens to share a name with ours would be silently dropped by the
    # recompute below (`build_manifest` filters by name, matching how the
    # vendored copy itself was built). That is drift in itself, and hiding it
    # behind a hash comparison that never saw the file would be exactly the
    # quiet fail-open this reporter exists to avoid.
    #
    # The walk itself can raise (a directory we cannot enter) — caught here so
    # an unreadable upstream is reported as UNAVAILABLE, never as DRIFT. An
    # uncaught exception here would otherwise escape to `main()`, and Python
    # exits 1 on an uncaught exception — the same code as DRIFT — which would
    # turn "we could not look" into a positive claim about upstream's content.
    try:
        collisions = sorted(
            p.relative_to(upstream_dir).as_posix()
            for p in upstream_dir.rglob("*")
            if p.is_file() and p.name in EXCLUDED_NAMES
        )
    except OSError as exc:
        return report(
            UNAVAILABLE,
            [
                "## Upstream unreadable",
                "",
                f"Could not walk `{upstream_dir}` looking for excluded-name "
                f"collisions (`{type(exc).__name__}`). Nothing was compared.",
            ],
        )
    if collisions:
        return report(
            DRIFT,
            [
                "## Upstream drift — surface comparison invalidated",
                "",
                "Upstream now ships a file whose name is excluded from this "
                "reporter's recompute (`vendor_manifest.EXCLUDED_NAMES` = "
                f"{sorted(EXCLUDED_NAMES)}), so the tree hash below would "
                f"silently ignore it: {', '.join(f'`{c}`' for c in collisions)}.",
                "",
                "This is advisory. It does not block any pull request, and "
                "nothing was changed automatically.",
                "",
                "**Next step: a deliberate re-vendor PR** — read what "
                "upstream added and why, decide whether the exclusion list "
                "still makes sense, and re-vendor at the new commit with "
                "`scripts/revendor_github_checker_actions.sh`. Do not "
                "silence this by hand-editing a hash; the hash is the "
                "signal.",
            ],
            pin=pinned_commit,
        )

    # `build_manifest` calls `read_bytes()` on every file under `upstream_dir`
    # with no guard of its own (it is a shared tool, also used by the
    # re-vendor script against a checkout it just verified). A file that
    # vanished or lost read permission between the collision walk and here
    # must not turn into an uncaught exception: Python exits 1 on those, the
    # same code as DRIFT, which would make "we could not read it" look like a
    # positive claim about upstream's content.
    try:
        upstream_manifest = build_manifest(
            upstream_dir,
            provenance.get("commit", "?"),
            contract="github-checker-actions",
            contract_version=1,
        )
    except OSError as exc:
        offending = getattr(exc, "filename", None)
        named = f" `{offending}`" if offending else ""
        return report(
            UNAVAILABLE,
            [
                "## Upstream unreadable",
                "",
                f"Could not read upstream file{named} while recomputing the "
                f"tree hash (`{type(exc).__name__}`). Nothing was compared.",
            ],
        )
    upstream_tree = str(upstream_manifest["tree_sha256"])
    upstream_hashes = _surface_dict(upstream_manifest["surface"])  # type: ignore[arg-type]
    provenance_lines.append(f"- upstream tree_sha256 (recomputed): `{upstream_tree}`")

    if upstream_tree == vendored_tree:
        return report(
            NO_DRIFT,
            ["## No upstream drift", "", "Upstream matches the vendored copy."],
            pin=pinned_commit,
        )

    added = sorted(set(upstream_hashes) - set(vendored_hashes))
    removed = sorted(set(vendored_hashes) - set(upstream_hashes))
    changed = sorted(
        rel
        for rel in set(upstream_hashes) & set(vendored_hashes)
        if upstream_hashes[rel] != vendored_hashes[rel]
    )

    if not added and not removed and not changed:
        # The per-file pairs agree on both sides, yet the tree hashes do not
        # — the vendored `tree_sha256` does not fingerprint its own `surface`
        # list. That is a guarantee-A break (checked by
        # tests/test_contract_ingest.py on every PR), not evidence that
        # upstream changed; reporting it as ordinary drift would name nothing
        # and still tell the operator to re-vendor an upstream that is, by
        # every file comparison available here, identical.
        return report(
            DRIFT,
            [
                "## Vendored manifest does not match its own fingerprint",
                "",
                "Every upstream file matches its vendored counterpart, but "
                "the recorded `tree_sha256` disagrees with the recomputed "
                "one anyway. This is not upstream drift — it means "
                f"`{manifest_path}`'s `tree_sha256` does not fingerprint the "
                "`surface` it ships with.",
                "",
                "**Next step:** fix guarantee A first — see "
                "`tests/test_contract_ingest.py` and "
                "`scripts/vendor_manifest.py` — then re-run this watcher.",
            ],
            pin=pinned_commit,
        )

    body = ["## Upstream drift", ""]
    if added:
        body.append(f"- only upstream: {', '.join(f'`{r}`' for r in added)}")
    if removed:
        body.append(
            f"- only in the vendored copy: {', '.join(f'`{r}`' for r in removed)}"
        )
    if changed:
        body.append(f"- differing content: {', '.join(f'`{r}`' for r in changed)}")
    body += [
        "",
        "This is advisory. It does not block any pull request, and nothing "
        "was changed automatically.",
        "",
        "**Next step: a deliberate re-vendor PR** — read what upstream "
        "changed and why, and re-vendor at the new commit with "
        "`scripts/revendor_github_checker_actions.sh`. Do not silence this "
        "by hand-editing a hash; the hash is the signal.",
    ]
    return report(DRIFT, body, pin=pinned_commit)


def _git(repo: Path, *args: str) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True
        )
    except OSError:
        return "?"
    return out.stdout.strip() if out.returncode == 0 else "?"


def _commits_since_pin(
    upstream_root: Path, pin: str | None, resolved: str, subdir: str
) -> str:
    """Who touched `subdir` since the pin, not just whether it drifted.

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
            subdir,
        ],
        capture_output=True,
        text=True,
    )
    if log.returncode != 0:
        return f"{header}\n\n`git log` failed; nothing was queried."

    lines = [ln for ln in log.stdout.strip().splitlines() if ln]
    if not lines:
        return f"{header}\n\nNo commits touched `{subdir}` since the pin."
    return f"{header}\n\n" + "\n".join(f"- `{ln}`" for ln in lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "upstream_dir", type=Path, help="checked-out upstream contracts/actions/v1"
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

    root = args.upstream_root or args.upstream_dir
    provenance = {
        "commit": _git(root, "rev-parse", "HEAD"),
        "remote": _git(root, "remote", "get-url", "origin"),
        "ref": args.ref,
    }
    try:
        report = compare(args.upstream_dir, args.vendored, provenance)
    except Exception as exc:  # noqa: BLE001 — last-resort net, see below
        # `compare()` guards every OSError it can anticipate (I-1). This is
        # the backstop for whatever it did not anticipate: an uncaught
        # exception exits the interpreter with 1, the same code as DRIFT,
        # which would dress an unhandled failure as a positive finding about
        # upstream. Anything reaching here is UNAVAILABLE, never a finding.
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
            _UPSTREAM_SUBDIR,
        )
    print(summary)
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a", encoding="utf-8") as handle:
            handle.write(summary + "\n")
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
