"""PF-2B0: read-only fleet enablement — suggest @ids, classify legacy blockers,
and prove a backfill changed nothing but identity.

Everything here READS. It never writes to any repo. It powers three evidence
artifacts for the @id backfill (ADR-ECO-005 PF-2B):

  * `suggest_ids`   — an @id per un-@id'd open item (reusing an existing
    author-chosen blocker slug where one already names the item; grammar-checked
    and de-collided within the repo). The owner accepts or renames in their PR.
  * `classify_legacy` — every `@blocked_by:<repo>#<slug>` sorted into clean /
    missing / ambiguous / absent / not-in-manifest. "Not in the frozen manifest"
    is a PLAN defect; "in the manifest but not scanned here" is an ENVIRONMENT
    diagnostic — the two are never conflated (they were, by folder presence).
  * `snapshot` / `drift` — a per-item baseline (status + tag-stripped text) that
    an @id backfill must leave byte-identical: adding `@id` strips out of the
    display text, so the only legal change is identity/ref fields.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from plan_fields.scrape import ScrapedItem, scrape_items

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_LEGACY_RE = re.compile(r"^([a-z0-9][a-z0-9-]*)#(\S+)$", re.IGNORECASE)


def canonical_name(repo_dir: Path) -> str:
    """Repo name as a plain `git clone` makes it — from origin, not the folder."""
    try:
        text = (repo_dir / ".git" / "config").read_text(encoding="utf-8")
    except OSError:
        return repo_dir.name
    in_origin = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("["):
            in_origin = s == '[remote "origin"]'
        elif in_origin and s.startswith("url ="):
            url = s.split("=", 1)[1].strip().rstrip("/")
            return url.rsplit("/", 1)[-1].removesuffix(".git") or repo_dir.name
    return repo_dir.name


@dataclass(frozen=True)
class ManifestIndex:
    """Repo identity exactly as the workspace manifest declares it.

    `canonical_keys` are the only identities: the leaf key of every entry that
    is not a `member`. `git_dir_to_key` maps a declared checkout location to
    its key — a locator alias, never an identity of its own, so a repo renamed
    upstream keeps its identity while only the alias moves.
    """

    canonical_keys: frozenset[str]
    git_dir_to_key: Mapping[str, str]

    def resolve_ref(self, name: str) -> str:
        """Normalise a written repo name to its canonical key (case-insensitive).

        The input is lower-cased before matching, and the return value is
        always lower-cased too. A name that already matches a key returns
        that key; a declared locator translates to its key; anything else
        returns as a lower-cased name, so an unknown repo stays visible as
        itself rather than silently vanishing.
        """
        low = name.lower()
        if low in self.canonical_keys:
            return low
        return self.git_dir_to_key.get(low, low)


def manifest_index(manifest_path: Path) -> ManifestIndex:
    """Build the identity index from the frozen workspace manifest.

    Members are skipped: an entry with ``member = true`` describes a package
    inside another repo and is never cloned on its own, so it must not mint an
    identity — nor make its owner's ``git_dir`` look ambiguous.
    """
    data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    keys: set[str] = set()
    by_git_dir: dict[str, list[str]] = {}
    for section in data.values():
        if not isinstance(section, dict):
            continue
        for key, entry in section.items():
            if not isinstance(entry, dict) or entry.get("member") is True:
                continue
            k = key.lower()
            keys.add(k)
            git_dir = entry.get("git_dir")
            if isinstance(git_dir, str):
                by_git_dir.setdefault(git_dir.lower(), []).append(k)
    ambiguous = {d: sorted(ks) for d, ks in by_git_dir.items() if len(ks) > 1}
    if ambiguous:
        detail = "; ".join(f"{d} -> {ks}" for d, ks in sorted(ambiguous.items()))
        raise ValueError(
            f"workspace manifest declares an ambiguous git_dir alias: {detail}. "
            f"A checkout location must name at most one non-member repo."
        )
    return ManifestIndex(frozenset(keys), {d: ks[0] for d, ks in by_git_dir.items()})


def resolve_checkout(repo_dir: Path, index: ManifestIndex) -> str:
    """The canonical key for a checkout on disk — manifest first, origin last.

    Each candidate spelling goes through ONE predicate — normalise it, then
    accept it only if the result is a declared key. That covers a canonical key
    and a `git_dir` alias under the same rule, so a folder named exactly like
    its key still resolves when the entry declares a different `git_dir`.

    Order matters. The folder name is tried before the origin so a repo renamed
    upstream still resolves through its declared locator; the origin-derived
    name is tried next so an arbitrarily named folder still resolves; and only
    a checkout the manifest declares under neither spelling falls back to the
    origin-derived name, which is provenance, not identity. That fallback never
    lands on a key, so an undeclared checkout stays visibly undeclared instead
    of borrowing an identity it was not given.
    """
    origin = canonical_name(repo_dir).lower()
    for candidate in (repo_dir.name.lower(), origin):
        canonical = index.resolve_ref(candidate)
        if canonical in index.canonical_keys:
            return canonical
    return origin


def manifest_repos(manifest_path: Path) -> set[str]:
    """The canonical repo names the frozen manifest declares — its non-member keys.

    Two things changed with the index, and only one of them is inert. It reads
    the section **key**, not ``package_name``: those coincide in every entry
    today, so that part changes no current answer, but the contract names the
    key and a ``package_name`` edited without touching its key would otherwise
    move plan identity. It also skips ``member = true`` entries, and that DOES
    change the answer — ``atp-platform-sdk`` describes a package inside
    ``atp-platform``, is never cloned on its own, and so leaves this set (and
    with it the `fleet-graph` node list) as a repo that cannot exist.
    """
    return set(manifest_index(manifest_path).canonical_keys)


def checkout_map(root: Path, index: ManifestIndex | None = None) -> dict[str, Path]:
    """Every cloned repo under ``root``, by canonical name -> its directory.

    With an index, checkouts resolve through the manifest (the contract's rule).
    Without one, the old origin-derived behaviour is kept so existing callers
    are not broken by this change alone.

    Two checkouts resolving to the same name are a loud error, not a last-one-
    wins. Which of them supplies the repo's TODO.md, commit and items would
    otherwise depend on directory order — identity decided by ``sorted()`` is
    exactly the failure mode this contract exists to prevent, and it matches
    how ``manifest_index`` already refuses an ambiguous ``git_dir``.
    """
    out: dict[str, Path] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / ".git").exists():
            continue
        key = (
            resolve_checkout(child, index)
            if index is not None
            else canonical_name(child).lower()
        )
        first = out.get(key)
        if first is not None:
            raise ValueError(
                f"workspace holds two checkouts resolving to '{key}': "
                f"{first.name} and {child.name}. A repo's identity must not "
                f"depend on directory order — rename or remove one."
            )
        out[key] = child
    return out


def scan_workspace(
    root: Path, index: ManifestIndex | None = None
) -> dict[str, list[ScrapedItem]]:
    """Every cloned repo with a TODO.md, by canonical name -> scraped items.

    Resolution and the collision rule are ``checkout_map``'s; this only reads
    the TODO.md of each resolved checkout that keeps one.
    """
    fleet: dict[str, list[ScrapedItem]] = {}
    for key, child in checkout_map(root, index).items():
        todo = child / "TODO.md"
        if todo.is_file():
            fleet[key] = scrape_items(todo.read_text(encoding="utf-8", errors="ignore"))
    return fleet


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    slug = "-".join(slug.split("-")[:6])[:64].strip("-")
    return slug


# --- suggester ---------------------------------------------------------------
@dataclass(frozen=True)
class Suggestion:
    repo: str
    line: int
    text: str
    suggested_id: str  # "" when the tool cannot derive one — owner must name it
    source: str  # "reused-slug" | "derived" | "needs-owner"


def suggest_ids(
    repo: str, items: list[ScrapedItem], clean_slug_by_line: dict[int, str]
) -> list[Suggestion]:
    """One suggested @id per un-@id'd OPEN item, de-collided within the repo.

    `clean_slug_by_line` maps a line to the slug that UNAMBIGUOUSLY references
    the item there (from `classify_legacy` clean resolutions) — that slug is
    reused verbatim as the author's chosen name. Ambiguous or missing references
    are deliberately absent, so they never over-assign one slug to many items.
    Every other item gets a slug derived from its leading words; text with no
    usable ASCII (e.g. Cyrillic) yields `needs-owner`, never a fabricated hash."""
    taken = {i.item_id for i in items if i.item_id}
    out: list[Suggestion] = []
    for item in items:
        if item.checked or item.item_id:
            continue
        reused = clean_slug_by_line.get(item.line)
        if reused and _ID_RE.match(reused.lower()):
            base, source = reused.lower(), "reused-slug"
        else:
            base = _slugify(item.display_text)
            source = "derived" if base else "needs-owner"
        sid = _dedupe(base, taken) if base else ""
        if sid:
            taken.add(sid)
        out.append(Suggestion(repo, item.line, item.display_text, sid, source))
    return out


def _dedupe(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


# --- legacy blocker classification -------------------------------------------
@dataclass(frozen=True)
class LegacyRef:
    source_repo: str
    source_line: int
    raw: str
    target_repo: str
    slug: str
    resolution: str  # clean-open|clean-closed|missing|ambiguous|absent|not-in-manifest
    target_line: int | None = None


def classify_legacy(
    fleet: dict[str, list[ScrapedItem]], index: ManifestIndex
) -> list[LegacyRef]:
    """Sort every legacy ``<repo>#<slug>`` blocker into its resolution class.

    The manifest arrives as a ``ManifestIndex`` rather than a bare set of names:
    a set has nowhere to record that a declared locator and its key name one
    repo, so a reference written with the checkout spelling could never resolve.
    """
    out: list[LegacyRef] = []
    for repo, items in sorted(fleet.items()):
        for item in items:
            for raw in item.values("blocked_by"):
                m = _LEGACY_RE.match(raw)
                if not m:
                    continue  # todo:// or malformed — not this report's job
                trepo, slug = m.group(1).lower(), m.group(2)
                out.append(_resolve(repo, item.line, raw, trepo, slug, fleet, index))
    return out


def _matches(item: ScrapedItem, slug: str) -> bool:
    """Does `slug` name `item`? Once the item carries an `@id`, only an exact
    (case-insensitive) `@id` match counts — the canonical identity. Before the
    backfill, fall back to the slug appearing in the tag-stripped `display_text`
    (case-insensitive), so casing and tag values never mis-resolve."""
    low = slug.lower()
    if item.item_id:
        return item.item_id.lower() == low
    return low in item.display_text.lower()


def _resolve(repo, line, raw, trepo, slug, fleet, index) -> LegacyRef:
    # Normalise before deciding: a reference spelled with a declared locator
    # names the same repo as one spelled with the key, so both must reach the
    # same verdict. `target_repo` carries the key; `raw` keeps the spelling.
    trepo = index.resolve_ref(trepo)

    def ref(res, tline=None):
        return LegacyRef(repo, line, raw, trepo, slug, res, tline)

    if trepo not in index.canonical_keys:
        return ref("not-in-manifest")  # plan defect
    if trepo not in fleet:
        return ref("absent")  # in manifest, not scanned here — environmental
    hits = [i for i in fleet[trepo] if _matches(i, slug)]
    if not hits:
        return ref("missing")
    if len(hits) > 1:
        return ref("ambiguous")
    hit = hits[0]
    return ref("clean-open" if not hit.checked else "clean-closed", hit.line)


# --- zero-drift snapshot / comparator ----------------------------------------
def snapshot(fleet: dict[str, list[ScrapedItem]]) -> dict:
    """Per-item baseline an @id backfill must not move: status + tag-stripped
    text + blocker count. `@id` strips out of display_text, so a faithful
    backfill leaves this identical."""
    repos: dict[str, list] = {}
    for repo, items in sorted(fleet.items()):
        repos[repo] = [
            {
                "status": "closed" if i.checked else "open",
                "text": i.display_text,
                "blockers": len(i.values("blocked_by")),
            }
            for i in items
        ]
    return {"repos": repos}


@dataclass
class Drift:
    changed_repos: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.changed_repos


def drift(before: dict, after: dict) -> Drift:
    """What a backfill actually moved. Clean == only identity/ref changed."""
    d = Drift()
    rb, ra = before.get("repos", {}), after.get("repos", {})
    for repo in sorted(set(rb) | set(ra)):
        if rb.get(repo) != ra.get(repo):
            d.changed_repos.append(repo)
            b, a = rb.get(repo, []), ra.get(repo, [])
            if len(b) != len(a):
                d.detail.append(f"{repo}: item count {len(b)} -> {len(a)}")
            else:
                for i, (x, y) in enumerate(zip(b, a)):
                    if x != y:
                        d.detail.append(f"{repo}[{i}]: {x} -> {y}")
    return d
