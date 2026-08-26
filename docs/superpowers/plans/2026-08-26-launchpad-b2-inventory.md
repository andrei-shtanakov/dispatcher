# Launchpad PR-B2: Parser Re-vendor + Inventory Reader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** dispatcher learns to read `@dag` from the fleet's plans and to derive
the launchpad inventory — Ready / blocked / unregistered_items / orphan_dags —
from captured inputs, per spec §3, §5.1, §6.

**Architecture:** three layers, dependency-ordered. (1) The vendored
`plan-fields` package re-vendors contract v3 r2 and implements the `@dag`
grammar in its parser — the 5 canon fixtures that fail under the pre-B2 parser
go green. (2) A pure DAG-subset discriminator and a pure item classifier extend
`dispatcher/core/admission.py`'s no-IO discipline. (3) One capture module does
all the IO (ledger scrape, `dags/` listing, one-generation file reads via
`O_NOFOLLOW` fd, HEAD-blob facts, identity resolution) and hands frozen
inputs to the classifier. The
`/api/launchpad` endpoint stays PR-C; B2 ends at a tested data layer.

**Tech Stack:** Python 3.13, uv, pytest, ruamel-free (yaml.safe_load for the
subset check), git plumbing via subprocess (existing `_GIT_TIMEOUT` pattern).

**Spec:** `docs/superpowers/specs/2026-08-26-launchpad-design.md` (§3 the
`@dag` tag; §5 one classifier, two adapters; §5.1 readiness conditions; §6 DAG
subset + content pinning). Canon contract: prograph-vault
`authored/contracts/plan-fields/v3` at `dc12b0e` (r2, tree
`6f8b23066fd0bbad4e5c6b5cd3cce3ab8b3a3f81302f3e83adcaf565e609ea2c`).

## Global Constraints

- **CI reads local fixtures only, never sibling repositories' real files**
  (spec §6.1). Fleet-derived fixtures carry a provenance comment naming what
  they were modeled on.
- **dispatcher does not vendor maestro's schema** (spec §6.1): the subset
  check is a named *supported subset*, not "Mode-1 validation"; authoritative
  validation stays with maestro at launch.
- **The classifier never touches a store or a disk** (spec §5): pure functions
  over captured values only. All IO lives in the capture module.
- **Fail-closed by subtraction** (B1 discipline): anything unreadable or
  unparseable is captured as a named unreadable fact and classifies as
  blocked/invalid — never silently skipped.
- The admission-code vocabulary for items is fixed by spec §4.2:
  `item_closed`, `item_unregistered`, `dag_invalid`, `dag_duplicate`,
  `dag_dirty`. Do not invent new codes.
- Fixture `expected.json` files in the vendored contract are canon: parser
  output must match them **byte-for-byte after canonicalization**; messages are
  pinned there, not chosen by the implementer.
- Package hygiene: `uv` only; `ruff format` + `ruff check` + `pyrefly check`
  clean after every task; line length 88.
- The re-vendored surface must verify against `manifest.json`
  (`tree_sha256 = 6f8b2306…`) — copy-integrity is a test, not a hope.

---

### Task 1: Re-vendor contract r2 and make the parser conform (7/7 @dag fixtures)

**Files:**
- Replace: `packages/plan-fields/src/plan_fields/contract/` (all 44 surface
  files from canon r2 + regenerated `PINNED.txt`)
- Modify: `packages/plan-fields/src/plan_fields/parser.py`
- Modify: `packages/plan-fields/src/plan_fields/scrape.py` (quoted-tag
  detection helper only — the tokenizer regex itself does not change)
- Modify: `packages/plan-fields/tests/test_conformance.py` (case count 11 → 18)
- Create: `packages/plan-fields/tests/test_dag_tag.py`
- Modify: `packages/plan-fields/CONSUMING.md` (line 4 still says "plan-fields
  v2" — stale since the v3 vendor; recorded in vault#103)
- Modify: `packages/plan-fields/CHANGELOG.md`, `packages/plan-fields/pyproject.toml`
  (0.9.0 → 0.10.0)

**Interfaces:**
- Produces: `parse_dag(item: ScrapedItem, item_id: str | None) ->
  tuple[str | None, tuple[tuple[str, str], ...]]` — `(dag_value,
  diagnostics)` where diagnostics are `(code, message)` pairs; public for the
  same reason `parse_owner` is (operational reporters classify without copying
  the grammar).
- Produces: node dict gains key `"dag"` **only when valid** (absent, not
  null, otherwise — the canon fixtures pin this).
- Produces: `scrape.py` gains `last_tag_is_quoted(raw_text: str, key: str)
  -> bool` — the tokenizer unquotes into `tags`, so the grammar re-asks the
  SAME tokenizer (`_TAG_RE.finditer`, quoted group vs bare group) whether the
  LAST occurrence of `key` was quoted. Last-occurrence, because `tags` is
  last-wins: an earlier quoted `@dag` superseded by a valid bare one is the
  bare one. No second regex exists.

- [ ] **Step 1: Re-vendor the surface**

Copy every file of canon `authored/contracts/plan-fields/v3/` **except**
`PINNED.txt` (vendor-local) into `src/plan_fields/contract/`, preserving the
exclusions the surface itself defines (`manifest.json` and `drift-control.md`
ARE copied — they travel with the vendored copy; they are just excluded from
the hash surface). Write `PINNED.txt`:

```
source: prograph-vault authored/contracts/plan-fields/v3
commit: dc12b0e660f4e17558d6a747aef7b09d55f97b0e
vendored: 2026-08-26
note: pinned copy (repo-boundaries vendoring). Do not edit here; re-vendor from
canon and update this header. Revision: v3 r2 (adds optional @dag).
```

(Use the full 40-hex of `dc12b0e` from `git -C ../prograph-vault rev-parse`.)

- [ ] **Step 2: Prove copy-integrity**

Run the package's manifest-verification test (it recomputes vendored hashes
against `manifest.json`). Expected: pass, `tree_sha256` ends `…e609ea2c`.

- [ ] **Step 3: Update the conformance count and run to RED**

In `test_conformance.py::test_all_fixtures_conform` change the count assert to
`assert len(results) == 18` and its comment to "17 simple pairs + 1 history
bundle". Run: `uv run pytest tests/test_conformance.py -x`
Expected: FAIL — exactly these five diff (the two no-new-behaviour cases,
`dag-without-id` and `dag-continuation-invisible`, already pass):
`valid/dag-registered`, `valid/dag-id-stem-agreement`,
`invalid/dag-name-mismatch`, `invalid/dag-traversal`, `invalid/dag-quoted`.

- [ ] **Step 4: Write the unit tests**

`tests/test_dag_tag.py` — beyond the fixtures, pin the API and the structural
rule:

```python
"""@dag grammar unit surface (canon fixtures pin the document level)."""

from plan_fields.parser import parse_dag, parse_todo
from plan_fields.scrape import last_tag_is_quoted, scrape_items


def _item(line: str):
    return scrape_items(f"# demo\n\n{line}\n")[0]


def test_valid_dag_matches_id():
    item = _item("- [ ] T @id:alpha @owner:github:u @dag:dags/alpha.yaml")
    value, diags = parse_dag(item, "alpha")
    assert value == "dags/alpha.yaml"
    assert diags == ()


def test_structural_codes_fire_on_closed_items_too():
    # the @epic precedent: a malformed tag on a closed item is still malformed
    doc = parse_todo(
        "# demo\n\n- [x] Done @id:z @owner:github:u @dag:dags/other.yaml\n",
        "demo",
        generated_at="2026-07-28T00:00:00Z",
    )
    codes = [d["code"] for d in doc["diagnostics"]]
    assert "PF-DAG-MISMATCH" in codes
    assert "dag" not in doc["nodes"][0]


def test_quoted_detection_follows_last_wins():
    assert last_tag_is_quoted('T @dag:"dags/x.yaml"', "dag")
    assert not last_tag_is_quoted("T @dag:dags/x.yaml", "dag")
    # last-wins: the surviving occurrence decides
    assert not last_tag_is_quoted('T @dag:"dags/a.yaml" @dag:dags/x.yaml', "dag")
    assert last_tag_is_quoted('T @dag:dags/a.yaml @dag:"dags/x.yaml"', "dag")
    # prose about a tag inside backticks is not a tag (tokenizer boundary)
    assert not last_tag_is_quoted('see `@dag:"x"` in docs', "dag")


def test_last_wins_on_repeated_dag_tags():
    # single-valued key convention (like @owner): the tags map is last-wins;
    # no DAG-MULTIPLE code exists in the r2 registry, so none is emitted
    item = _item(
        "- [ ] T @id:a @owner:github:u @dag:dags/b.yaml @dag:dags/a.yaml"
    )
    value, diags = parse_dag(item, "a")
    assert value == "dags/a.yaml"
    assert diags == ()
```

- [ ] **Step 5: Implement**

`scrape.py` (below `_display_text`) — reuse `_TAG_RE`, never a second regex:

```python
def last_tag_is_quoted(raw_text: str, key: str) -> bool:
    """Whether the LAST `@{key}:` occurrence on this line used a quoted value.

    The tokenizer unquotes values into `tags`, deliberately erasing the
    spelling; grammars that reject quoting (r2's @dag) re-ask the SAME
    tokenizer here. Last occurrence, because `tags` is last-wins."""
    quoted = None
    for m in _TAG_RE.finditer(raw_text):
        if m.group(1) == key:
            quoted = m.group(2) is not None  # group 2 = the quoted alternative
    return bool(quoted)
```

`parser.py` — grammar constants beside the owner regexes:

```python
# r2 @dag: bare token, relative, normalized; traversal dies in the grammar.
DAG_RE = re.compile(r"^dags/[a-z0-9][a-z0-9._-]*\.yaml$")
```

`parse_dag` beside `parse_owner`:

```python
def parse_dag(
    item: ScrapedItem, item_id: str | None
) -> tuple[str | None, tuple[tuple[str, str], ...]]:
    """Parse the r2 @dag launch-registration tag for one item.

    Returns ``(dag, diagnostics)``: ``dag`` is the value only when it passed
    the grammar AND equals ``dags/<id>.yaml``; diagnostics are
    ``(code, message)`` pairs whose texts are pinned by the canon fixtures.
    With no ``@id`` the item yields no node, so the caller never gets here —
    the signature keeps ``item_id`` optional for operational reporters."""
    value = item.tags.get("dag")
    if value is None or item_id is None:
        return None, ()
    node_id_stub = item_id  # messages name todo://<repo>/<id>; repo added by caller
    if last_tag_is_quoted(item.raw_text, "dag"):
        return None, (
            (
                "PF-DAG-GRAMMAR",
                "@dag on item todo://{repo}/%s uses a quoted value — the grammar "
                "takes a bare dags/<name>.yaml token" % node_id_stub,
            ),
        )
    if not DAG_RE.fullmatch(value):
        return None, (
            (
                "PF-DAG-GRAMMAR",
                "@dag value %s on item todo://{repo}/%s fails the grammar"
                % (value, node_id_stub),
            ),
        )
    if value != f"dags/{item_id}.yaml":
        return None, (
            (
                "PF-DAG-MISMATCH",
                "@dag %s on item todo://{repo}/%s does not equal dags/%s.yaml"
                % (value, node_id_stub, item_id),
            ),
        )
    return value, ()
```

(Exact message strings: copy from the canon `expected.json` files — they are
the authority; the `{repo}` placeholder is formatted by the caller in
`parse_todo`, which knows the repo. If the two-stage formatting reads worse
than passing `node_id` in, pass `node_id: str` instead — the fixtures decide
correctness, not this sketch.)

In `parse_todo`'s node assembly (beside the epic block):

```python
        dag, dag_diags = parse_dag(item, item_id)
        if dag is not None:
            node["dag"] = dag
        for code, message in dag_diags:
            diagnostics.append(
                _diag(code, "warning", node_id, None, None,
                      message.format(repo=repo), prov)
            )
```

Severity is `"warning"` for both codes (registry: `default_severity: warning`).
Structural codes fire regardless of open/closed (the epic precedent already in
this loop). The `raw` map needs no change — the tokenizer already keeps `dag`.

- [ ] **Step 6: Run to GREEN**

`uv run pytest` (whole package). Expected: 18/18 conformance + new unit tests
pass.

- [ ] **Step 7: Docs and version**

- `CONSUMING.md:4`: "plan-fields v2" → "plan-fields v3 (r2)".
- `CHANGELOG.md`: `## 0.10.0 — 2026-08-26` — r2 re-vendor, `@dag` grammar,
  `parse_dag`/`last_tag_is_quoted` public API, node key `dag`.
- `pyproject.toml` version 0.10.0.

- [ ] **Step 8: Commit**

```bash
git add packages/plan-fields
git commit -m "feat(plan-fields): re-vendor contract v3 r2 and implement @dag (0.10.0)"
```

---

### Task 2: Pure DAG-subset discriminator with fleet-derived fixtures

**Files:**
- Create: `dispatcher/core/dag_subset.py`
- Create: `tests/test_dag_subset.py`
- Create: `tests/fixtures/dag_subset/` (YAML fixtures, each with a provenance
  header comment)

**Interfaces:**
- Produces: `classify_dag_text(text: str) -> DagSubsetVerdict` where
  `DagSubsetVerdict = Accepted(repo: str) | Rejected(reason: str)` —
  frozen dataclasses. Pure: consumes the file's TEXT, never a path.
- Consumed by: Task 3's capture (reads the file, hands text in) and Task 4's
  classifier (maps `Rejected` → `dag_invalid`).

- [ ] **Step 1: Write the fixtures**

`tests/fixtures/dag_subset/mode1-minimal.yaml` (provenance: modeled on the
maestro Mode-1 pilot configs, e.g. dispatcher's own slice-0 pilots — NOT read
from a sibling checkout):

```yaml
# Fleet-derived fixture: minimal Mode-1 ProjectConfig shape, modeled on the
# slice-0 pilot runs (spec §6.1). CI reads THIS file, never a sibling repo.
repo: git@github.com:andrei-shtanakov/demo.git
tasks:
  - id: t1
    prompt: do the thing
```

`mode2-orchestrator.yaml` (provenance: modeled on `proctor-a-*.yaml`
OrchestratorConfig — the two Mode-2 markers present):

```yaml
# Fleet-derived fixture: Mode-2 OrchestratorConfig shape modeled on
# proctor-a-*.yaml (spec §6.1). workstreams/repo_url are the discriminator.
repo_url: git@github.com:andrei-shtanakov/demo.git
workstreams:
  - name: ws1
tasks: []
repo: demo
```

Plus: `not-yaml.yaml` (binary junk), `no-tasks.yaml` (repo present, `tasks`
absent), `tasks-not-list.yaml` (`tasks: 3`), `repo-not-string.yaml`
(`repo: [a]`), `only-workstreams.yaml` (one Mode-2 marker alone — still
rejected: the predicate is "either marker present ⇒ not the supported
subset").

- [ ] **Step 2: Write the failing tests**

```python
from pathlib import Path

import pytest

from dispatcher.core.dag_subset import Accepted, Rejected, classify_dag_text

FIXTURES = Path(__file__).parent / "fixtures" / "dag_subset"


def load(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_minimal_mode1_is_accepted():
    verdict = classify_dag_text(load("mode1-minimal.yaml"))
    assert isinstance(verdict, Accepted)
    assert verdict.repo == "git@github.com:andrei-shtanakov/demo.git"


@pytest.mark.parametrize(
    "name",
    [
        "mode2-orchestrator.yaml",
        "only-workstreams.yaml",
        "not-yaml.yaml",
        "no-tasks.yaml",
        "tasks-not-list.yaml",
        "repo-not-string.yaml",
    ],
)
def test_rejections_are_named(name: str):
    verdict = classify_dag_text(load(name))
    assert isinstance(verdict, Rejected)
    assert verdict.reason  # non-empty, human-readable


def test_oversized_source_is_rejected_before_parsing():
    verdict = classify_dag_text("a: " + "b" * (2 * 1024 * 1024))
    assert isinstance(verdict, Rejected)


def test_alias_bomb_is_rejected_at_the_event_stream():
    # billion-laughs expands INSIDE the 1MiB source cap; safe_load in PyYAML
    # does expand aliases, so the event-scan phase must refuse them before
    # any construction happens
    bomb = "a: &a [x,x,x,x,x,x,x,x]\n" + "\n".join(
        f"{chr(98 + i)}: &{chr(98 + i)} [{','.join(['*' + chr(97 + i)] * 8)}]"
        for i in range(6)
    )
    verdict = classify_dag_text(bomb)
    assert isinstance(verdict, Rejected)
    assert "alias" in verdict.reason
```

Run: expected FAIL (module missing).

- [ ] **Step 3: Implement**

```python
"""Supported DAG subset (spec §6.1) — a structural predicate, not validation.

dispatcher does not vendor maestro's schema. Accepted means "shaped like a
Mode-1 ProjectConfig as far as launchpad needs"; authoritative validation
stays with maestro at launch. `workstreams:`/`repo_url:` are Mode-2 markers
(OrchestratorConfig requires them, ProjectConfig lacks them).
"""

from __future__ import annotations

from dataclasses import dataclass

import yaml

_MAX_DAG_BYTES = 1024 * 1024  # a plan DAG is text; anything bigger is refused
_MODE2_MARKERS = ("workstreams", "repo_url")


@dataclass(frozen=True)
class Accepted:
    repo: str


@dataclass(frozen=True)
class Rejected:
    reason: str


DagSubsetVerdict = Accepted | Rejected


def classify_dag_text(text: str) -> DagSubsetVerdict:
    """Classify one DAG file's text against the supported subset."""
    if len(text.encode("utf-8", errors="replace")) > _MAX_DAG_BYTES:
        return Rejected(f"file exceeds {_MAX_DAG_BYTES} bytes")
    try:
        # phase 1: event scan — no construction, aliases NOT expanded here.
        # PyYAML's safe_load DOES expand aliases (billion-laughs fits inside
        # the size cap), so anchors/aliases are refused before construction.
        for event in yaml.parse(text, Loader=yaml.SafeLoader):
            if isinstance(event, yaml.AliasEvent):
                return Rejected("YAML aliases are not in the supported subset")
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return Rejected(f"not parseable as YAML: {exc}")
    if not isinstance(doc, dict):
        return Rejected("top level is not a mapping")
    for marker in _MODE2_MARKERS:
        if marker in doc:
            return Rejected(f"'{marker}:' present — a Mode-2 marker, "
                            "not the supported Mode-1 subset")
    repo = doc.get("repo")
    if not isinstance(repo, str) or not repo:
        return Rejected("top-level 'repo:' string is required")
    if not isinstance(doc.get("tasks"), list):
        return Rejected("top-level 'tasks:' list is required")
    return Accepted(repo=repo)
```

- [ ] **Step 4: Run to GREEN, format, typecheck, commit**

```bash
uv run pytest tests/test_dag_subset.py -v
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/dag_subset.py tests/test_dag_subset.py tests/fixtures/dag_subset
git commit -m "feat(core): supported DAG subset discriminator (spec §6.1)"
```

---

### Task 3: Inventory capture — all the IO, one generation, frozen outputs

**Files:**
- Create: `dispatcher/core/inventory.py`
- Create: `tests/test_inventory_capture.py`

**Interfaces:**
- Consumes: `plan_fields.scrape.scrape_items`, `plan_fields.parser.parse_dag`
  (Task 1), `identity_from_checkout` (`run_identity.py`).
- Produces (frozen dataclasses, consumed by Task 4's classifier):

```python
@dataclass(frozen=True)
class PlanItem:
    item_id: str | None
    line: int
    open: bool            # `- [ ]` vs anything else
    shipped: bool         # under a `##`-level section whose title contains "Shipped"
    dag_raw: str | None   # the @dag tag as the tags map holds it (last-wins), or None
    dag_tag: str | None   # validated value (grammar + equality), else None
    dag_diag: str | None  # "PF-DAG-GRAMMAR" | "PF-DAG-MISMATCH" | None

@dataclass(frozen=True)
class DagFileInfo:
    rel_path: str         # "dags/<name>.yaml"
    is_regular: bool      # fstat on the opened fd: regular file
    text: str | None      # decoded from the SAME captured bytes; None if undecodable
    blob_sha: str | None  # git hash-object --stdin over the SAME bytes
    head_blob_sha: str | None  # blob at <head_revision>:<rel>; None = ABSENT
                               # from that tree (a fact, not a failure)
    error: str | None     # named IO/git FAILURE for THIS file (timeout,
                          # plumbing error, undecodable) — distinct from
                          # head_blob_sha=None, which is a clean absence

@dataclass(frozen=True)
class InventorySurface:
    plan_items: tuple[PlanItem, ...]      # FULL ledger, Shipped included
    dag_files: tuple[DagFileInfo, ...]
    head_revision: str | None             # full 40-hex; None on capture failure
    repo_key: RepoKey | None              # None on identity failure
    plan_error: str | None                # TODO.md unreadable
    dag_dir_error: str | None             # dags/ dir unreadable (absent dir is
                                          # NOT an error: empty tuple, no dags)
    capture_error: str | None             # HEAD/identity failure, named

def capture_inventory(checkout: Path) -> InventorySurface: ...
```

**Single-generation rule (the load-bearing design):**

- `dags/` itself is opened once with
  `os.open(dir_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)` — a
  symlinked or swapped `dags/` directory is refused at the root, not just
  at the leaves. Enumeration (`os.scandir(dir_fd)` via `os.open`'s fd) and
  every candidate open (`dir_fd=` keyword) go through that one fd.
- Every candidate file is opened ONCE with
  `os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)`.
  `O_NOFOLLOW` refuses symlinks (`ELOOP` ⇒ `is_regular=False`);
  `O_NONBLOCK` prevents a FIFO from hanging the open; `os.fstat` on the fd
  answers `is_regular`, and bytes are read ONLY after `S_ISREG` — a FIFO or
  device is never read. Regular-file reads are unaffected by `O_NONBLOCK`.
- BOTH the YAML text and `blob_sha` (`git hash-object --stdin` fed those
  bytes) derive from that one read. There is no second path-based access,
  so no swap between validate and pin is possible.
- `head_blob_sha` resolves against the CAPTURED `head_revision`, never the
  moving `HEAD` ref: `git ls-tree <head_revision> -- dags/<name>.yaml`.
  Empty output with exit 0 ⇒ absent from that tree (`head_blob_sha=None`,
  `error=None` — a fact); non-zero exit or unparseable output ⇒ `error`
  set (a failure). Equality `blob_sha == head_blob_sha` is computed by the
  CLASSIFIER, not here — capture reports facts, not verdicts.

- [ ] **Step 1: Write the failing tests**

Tests build a real throwaway git repo per test (`tmp_path`) — the HEAD-blob
pinning (spec §5.1 cond. 7) needs actual git:

```python
def make_repo(tmp_path, todo: str, dags: dict[str, str]) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "remote", "add", "origin",
         "git@github.com:andrei-shtanakov/demo.git")
    (root / "TODO.md").write_text(todo)
    for rel, text in dags.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(root, "add", "-A")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init")
    return root
```

Cases (each a test):
1. Registered open item + clean committed DAG → `PlanItem(dag_raw=…,
   dag_tag=…, dag_diag=None)`, `DagFileInfo(is_regular=True,
   blob_sha == head_blob_sha)`.
2. DAG edited after commit → `blob_sha != head_blob_sha`, both non-None.
3. DAG is a symlink (`os.symlink`) → `is_regular=False`, `text is None`,
   `error` names the refusal (`O_NOFOLLOW`); no content was ever read
   through the link. A FIFO (`os.mkfifo`) → `is_regular=False`, open does
   NOT hang (`O_NONBLOCK`), nothing is read. `dags/` itself a symlink to a
   directory elsewhere → `dag_dir_error` (the `O_DIRECTORY|O_NOFOLLOW`
   root refusal).
4. Item under `## Shipped` → `shipped=True`, still captured. Item under
   `### sub` nested below `## Shipped` → **still `shipped=True`** (the
   capture tracks `##`-level sections itself over the same text — a
   sub-heading does not end the Shipped region; `ScrapedItem.section`
   alone cannot answer this).
5. Open item with a malformed `@dag` → `dag_raw` set, `dag_tag=None`,
   `dag_diag="PF-DAG-GRAMMAR"` (the "written but broken" vs "not written"
   distinction Task 4's ordering depends on).
6. TODO.md unreadable (`chmod 0`) → `plan_error` set, `plan_items == ()`.
7. `dags/` unreadable (`chmod 0` on the dir) → `dag_dir_error` set. `dags/`
   absent entirely → `dag_dir_error is None`, `dag_files == ()` (a repo
   with no DAGs is normal, not broken).
8. Grammar-valid file referenced by nothing → still listed (orphan
   detection is the classifier's job). Non-`.yaml` and grammar-invalid
   names in `dags/` → not listed at all (invisible to launchpad).
9. Untracked DAG file (on disk, absent from the captured revision's
   tree) → `head_blob_sha=None`, `error=None` — clean absence, not a
   failure. HEAD moved mid-capture (commit between `rev-parse HEAD` and
   the per-file `ls-tree`) → still compared against the CAPTURED
   `head_revision` (test: commit a new revision after capture started —
   inject by monkeypatching the rev-parse step — and pin that `ls-tree`
   was called with the captured hex, not `HEAD`).
10. `git init` without a commit (unborn HEAD) → `capture_error` names it,
    `head_revision=None`; NEVER a silent empty inventory.
11. No `origin` remote → `repo_key=None`, `capture_error` names identity.

- [ ] **Step 2: Implement**

Implementation notes (the tests above are the contract):

- `head_revision`: `git -C <root> rev-parse HEAD` (existing `_GIT_TIMEOUT`
  subprocess pattern from `run_identity.py`); failure → `capture_error`.
  Every later blob lookup uses this captured hex, never the literal
  string `HEAD`.
- `repo_key`: `identity_from_checkout(checkout)`; `IdentityError` →
  `capture_error`.
- Per-file: exactly the single-generation rule above — `dir_fd` from the
  `O_DIRECTORY|O_NOFOLLOW` open of `dags/`, then
  `fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
  dir_fd=dir_fd)` inside try/except OSError (ELOOP ⇒ symlink refusal);
  `st = os.fstat(fd)`; bytes read ONLY after `stat.S_ISREG(st.st_mode)`;
  both fds closed in `finally`. `blob_sha` via `git hash-object --stdin`
  with `input=data` — git itself hashes, so normalization matches
  `head_blob_sha` exactly.
- `dags/` listing: `os.scandir` (B1 lesson: `Path.glob` suppresses scan
  errors) wrapped in try/except OSError → `dag_dir_error`;
  `FileNotFoundError` on the dir itself → no error, empty tuple. Candidate
  names: `<name>.yaml` where `dags/<name>.yaml` matches the r2 grammar.
- Plan scrape: `scrape_items` over the TODO text. Per item: `open` from
  `checked`; `shipped` from a local `##`-heading tracker over the same
  text (test 4 pins the nested case); `dag_raw = item.tags.get("dag")`;
  `dag_tag`/`dag_diag` via `parse_dag(item, item.item_id)`. `parse_todo`
  is NOT used: inventory needs Shipped and id-less lines, which the
  canonical projection deliberately drops.

- [ ] **Step 3: Run to GREEN, format, typecheck, commit**

```bash
uv run pytest tests/test_inventory_capture.py -v
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/inventory.py tests/test_inventory_capture.py
git commit -m "feat(core): inventory capture — one-generation ledger and dags/ facts"
```

---

### Task 4: Pure inventory classifier — Ready / blocked / unregistered / orphan

**Files:**
- Modify: `dispatcher/core/admission.py`
- Create: `tests/test_classify_inventory.py`

**Interfaces:**
- Consumes: `InventorySurface` (Task 3), `classify_dag_text` (Task 2 — pure,
  so the classifier may call it on captured text), `parse_remote_url`
  (`run_identity.py` — pure string parsing), existing `classify_repo`.
- Produces:

```python
ITEM_CLOSED = "item_closed"          # vocabulary for PR-C submit re-validation
ITEM_UNREGISTERED = "item_unregistered"
DAG_INVALID = "dag_invalid"
DAG_DUPLICATE = "dag_duplicate"
DAG_DIRTY = "dag_dirty"

@dataclass(frozen=True)
class CapturedInputs:
    """Spec §5's one input set: both surfaces, ONE capture generation.

    The adapter (PR-C's assembler; B2's seam test) fills every field from
    facts captured in one pass — mixing generations between the repo
    surface and the inventory surface is exactly what this type exists to
    make visible."""
    inventory: InventorySurface
    lock: LockInfo | Malformed | None
    lock_error: str | None
    runs: tuple[RunFact, ...]
    runs_unreadable: tuple[str, ...]

@dataclass(frozen=True)
class ItemDecision:
    work_id: str
    dag_path: str | None
    category: str        # "ready" | "blocked" | "unregistered"
    reason_code: str | None
    reason: str

@dataclass(frozen=True)
class InventoryDecision:
    repo: RepoAdmission
    ready: tuple[ItemDecision, ...]
    blocked: tuple[ItemDecision, ...]
    unregistered_items: tuple[ItemDecision, ...]
    orphan_dags: tuple[str, ...]   # rel paths, diagnostics only
    unreadable: str | None         # set ⇒ every list above is EMPTY

def classify_inventory(captured: CapturedInputs) -> InventoryDecision: ...
```

`classify_inventory` computes `RepoAdmission` itself by calling the existing
`classify_repo` on the captured repo surface — one entry point, one
generation, no separately precomputed decision to drift.

- [ ] **Step 1: Write the failing tests**

Pure-data tests — build `CapturedInputs` literals, no filesystem. One test
per §5.1 condition, in the spec's numbering:

1. All eight conditions hold → item in `ready`; `decision.repo.admission
   == "ready"`.
2. Closed item with a valid `@dag` → appears in NO list (`item_closed` is
   submit-vocabulary for PR-C's re-validation, not a launchpad list). An
   id-less OPEN item likewise appears in no list (no identity to launch;
   PF-ID-MISSING is the parser plane's finding, not admission's).
3. Open item, `@id`, `dag_raw is None` → `unregistered_items` with
   `reason_code="no_dag_tag"` (spec §4.1's literal field value).
4. Open item, `dag_raw` set but `dag_diag` set (`dag_tag=None`) →
   `blocked` with `dag_invalid`, the diag code in `reason` — **the
   ordering rule**: "written but broken" is `dag_invalid`, never
   `no_dag_tag`; only a truly absent tag is unregistered.
5. Duplication, ledger-wide (spec §3.2 — "including `## Shipped`"): two
   OPEN claimants of one DAG → BOTH in `blocked` with `dag_duplicate`,
   each naming the other's line. One OPEN + one Shipped claimant → the
   open one in `blocked` (`dag_duplicate`, reason names the Shipped line);
   the closed one appears in no list (rule 2).
   **Recorded ruling (spec §5.1 "on both items" vs §4.1's list shapes):**
   the launchpad lists carry launch candidates; a closed item is not one
   (§5.1 cond. 1), so "on both items" binds every item that can appear in
   a list — the diagnostic still names the closed co-claimant inside
   `reason`, so no information is lost. The §5.1 sentence should say "on
   both open items"; that touch-up joins the PR-C spec-rewording tail
   already recorded in TODO.md (B1 inherited tails).
6. `dag_tag` names a rel_path absent from `dag_files` → `blocked`,
   `dag_invalid` ("registered but absent").
7. `is_regular=False` → `blocked`, `dag_invalid`.
8. `DagFileInfo.error` set, or `text is None` → `blocked`, `dag_invalid`
   (unreadable facts NEVER reach Ready — fail-closed).
9. Subset-rejected text (Task 2's `Rejected`) → `blocked`, `dag_invalid`
   with the `Rejected.reason`.
10. DAG `repo:` parsing to a DIFFERENT `RepoKey` than
    `inventory.repo_key` (via `parse_remote_url`) → `blocked`,
    `dag_invalid` naming both keys — resolved identity, never a
    directory-name string compare (spec §5.1 cond. 6). Unparseable
    `repo:` → likewise `dag_invalid`.
11. `blob_sha != head_blob_sha` → `blocked`, `dag_dirty`.
    `head_blob_sha is None` with `error is None` (clean absence — file
    untracked at the captured revision) → `dag_dirty` too: content that
    is not the seen revision's content, by subtraction. `error` set
    (git failure, NOT absence) → `dag_invalid` via rule 8 — a failure to
    gather facts is never reported as a content verdict.
12. `plan_error` / `dag_dir_error` / `capture_error` set, or
    `repo_key`/`head_revision` `None` → `InventoryDecision(unreadable=…)`
    with EVERY list empty (empty `ready` IS the fail-closed posture; the
    classifier never raises on captured states — spec §5's "the classifier
    never touches a store" includes never throwing the adapter's problem
    back at it). `repo` is still computed and reported.
13. Grammar-valid DAG file claimed by NO open item → `orphan_dags`;
    claimed only by a Shipped item → still `orphan_dags` (spec §4.1:
    diagnostics, no actions).
14. Repo surface blocked (e.g. a live run) → items passing conditions 1–7
    land in `blocked` with `reason_code` = the repo's FIRST blocker code
    (deterministic: blockers keep `classify_repo`'s emission order) and
    they do NOT appear in `ready`.

- [ ] **Step 2: Implement**

Order inside `classify_inventory` (each step consumes only captured
values):

```python
def classify_inventory(captured: CapturedInputs) -> InventoryDecision:
    inv = captured.inventory
    repo = classify_repo(
        captured.lock, captured.lock_error,
        captured.runs, captured.runs_unreadable,
    )
    broken = (
        inv.plan_error or inv.dag_dir_error or inv.capture_error
        or (inv.repo_key is None and "repo identity unresolved")
        or (inv.head_revision is None and "HEAD unresolved")
    )
    if broken:
        return InventoryDecision(
            repo=repo, ready=(), blocked=(), unregistered_items=(),
            orphan_dags=(), unreadable=str(broken),
        )
    dag_by_path = {d.rel_path: d for d in inv.dag_files}
    claims: dict[str, list[PlanItem]] = {}
    for item in inv.plan_items:
        if item.dag_tag is not None:
            claims.setdefault(item.dag_tag, []).append(item)
    ...
```

then per OPEN item WITH an id, in this order (first match wins):
`dag_raw is None` → unregistered · `dag_diag` → `dag_invalid` · file
absent/irregular/errored/unreadable → `dag_invalid` · subset-rejected →
`dag_invalid` · foreign or unparseable `repo:` → `dag_invalid` · claimed
by another item (any status) → `dag_duplicate` · `blob_sha !=
head_blob_sha` or `head_blob_sha is None` → `dag_dirty` · repo blocked →
blocked with the repo's first blocker code · else → ready. Orphans:
grammar-valid files minus paths claimed by OPEN items.

The docstring carries spec §5's discipline sentence verbatim: "consumes
only captured values — the classifier never touches a store or a disk."

- [ ] **Step 3: Run to GREEN, format, typecheck, commit**

```bash
uv run pytest tests/test_classify_inventory.py -v
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/admission.py tests/test_classify_inventory.py
git commit -m "feat(core): inventory classifier over one-generation captured inputs"
```

---

### Task 5: End-to-end seam test + docs

**Files:**
- Create: `tests/test_inventory_end_to_end.py`
- Modify: `TODO.md` (Launchpad section)
- Modify: `CHANGELOG.md` (if the repo keeps one at root — follow existing
  practice; skip otherwise)

**Interfaces:**
- Consumes: everything above. No new surface.

- [ ] **Step 1: Write the seam test**

One test that goes disk → `capture_inventory` → `classify_inventory` over a
real tmp git repo built like Task 3's `make_repo`, containing simultaneously:
a ready item, an unregistered item, a dirty DAG, a duplicate pair
(open + Shipped), an orphan file, and a Mode-2 file. The repo surface of
`CapturedInputs` is filled with quiet literals (`lock=None`, no runs) — the
seam under test is inventory; B1's own tests already cover the repo surface.
Assert the full `InventoryDecision` shape in one
`assert decision == InventoryDecision(...)` — this is the fixture future PR-C
work regression-tests against.

Run: GREEN immediately (all parts exist) — its value is the seam, and it
becomes the first thing PR-C's assembler consumes.

- [ ] **Step 2: Drive-by docstring correction (one line of truth)**

`dispatcher/core/run_identity.py::safe_path_parts`: the docstring claims the
producer checks only `repo` against `{'.', '..'}` — `_segment_is_safe` has
checked all three segments since the maestro#211 mirror. Correct the sentence
(keep the traversal example and the inbox-issue pointer; they are still true).
No behaviour change, no new tests.

- [ ] **Step 3: TODO.md**

In the Launchpad section:

- close `@id:launchpad-b1` with "(PR #200)" (the recorded debt from the B1
  merge — batch it here, this is the next docs touch);
- reword `@id:launchpad-b2` to name this plan file and drop the `@trigger`
  (it fired: vault#103 merged 2026-08-26, canon at `dc12b0e`);
- in `@id:launchpad-c`'s inherited-tails prose: ADD the spec touch-up
  "§5.1 'on both items' → 'on both open items'" (Task 4's recorded
  ruling), and CORRECT the stale sentence "одно предложение в спеку §7 про
  linked-unreadable=in-flight" — that proposal was overridden by the owner
  at the B1 review (linked-unreadable is `run_state_unreadable` now); what
  PR-C owes the spec is a §7 pass under the NEW semantics.

- [ ] **Step 4: Full suite, commit**

```bash
uv run pytest
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add tests/test_inventory_end_to_end.py TODO.md dispatcher/core/run_identity.py
git commit -m "test(core): inventory seam test; docs: launchpad TODO refresh"
```

Known standing failures that are NOT this plan's regressions (baseline from
the B1 merge, 2026-08-26): `test_governance_live_smoke`,
`test_product_proposals_live_smoke`, `test_spec_runner_config_integration`
(live-smoke), plus the two recorded flakes (`flake-run-end-checkout`,
`flake-revendor-sigint`). Anything else red is yours.

---

## What this plan deliberately does NOT do

- No `/api/launchpad`, no snapshot assembler, no UI, no structured 409s —
  PR-C (spec §4, §9).
- No submit-path changes: `admit_submit`'s re-validation with the item codes
  (`item_closed`, `item_unregistered`, `revision_moved`…) is PR-C, where
  submit v2's body (`repo_key`/`work_id`) exists to validate against. B2
  defines the codes and the classifier they will reuse.
- No §5 adapter-equivalence property test yet: it compares the snapshot
  assembler against `admit_submit`, and the assembler does not exist until
  PR-C. B2 keeps the precondition it depends on — ONE classifier consumed by
  every adapter — and C owns the test.
- No maestro schema, no maestro-side TOCTOU closing (spec §6.2 names the
  residual window as a limit).
- No fleet-wide multi-repo capture loop: `capture_inventory` takes ONE
  checkout; iterating the manifest is the assembler's job (PR-C), where the
  per-repo error containment (B1's `_concerns_this_repo` lesson) already
  lives.
