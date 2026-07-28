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


def manifest_repos(manifest_path: Path) -> set[str]:
    """The canonical repo names the frozen workspace manifest declares."""
    data = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for section in data.values():
        if isinstance(section, dict):
            for entry in section.values():
                if isinstance(entry, dict) and isinstance(
                    entry.get("package_name"), str
                ):
                    names.add(entry["package_name"].lower())
    return names


def scan_workspace(root: Path) -> dict[str, list[ScrapedItem]]:
    """Every cloned repo with a TODO.md, by canonical name -> scraped items."""
    fleet: dict[str, list[ScrapedItem]] = {}
    for child in sorted(root.iterdir()):
        if not child.is_dir() or not (child / ".git").exists():
            continue
        todo = child / "TODO.md"
        if todo.is_file():
            fleet[canonical_name(child).lower()] = scrape_items(
                todo.read_text(encoding="utf-8", errors="ignore")
            )
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
    fleet: dict[str, list[ScrapedItem]], manifest: set[str]
) -> list[LegacyRef]:
    out: list[LegacyRef] = []
    for repo, items in sorted(fleet.items()):
        for item in items:
            for raw in item.values("blocked_by"):
                m = _LEGACY_RE.match(raw)
                if not m:
                    continue  # todo:// or malformed — not this report's job
                trepo, slug = m.group(1).lower(), m.group(2)
                out.append(_resolve(repo, item.line, raw, trepo, slug, fleet, manifest))
    return out


def _resolve(repo, line, raw, trepo, slug, fleet, manifest) -> LegacyRef:
    def ref(res, tline=None):
        return LegacyRef(repo, line, raw, trepo, slug, res, tline)

    if trepo not in manifest:
        return ref("not-in-manifest")  # plan defect
    if trepo not in fleet:
        return ref("absent")  # in manifest, not scanned here — environmental
    hits = [i for i in fleet[trepo] if slug in i.raw_text]
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
