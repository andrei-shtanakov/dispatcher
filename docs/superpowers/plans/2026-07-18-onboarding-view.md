# FR-04 Onboarding View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** «Выбор проекта → описание, позиция в roadmap и предстоящие задачи одним экраном» (FR-04) на web + TUI + MCP через один канонический билдер.

**Architecture:** Post-collect обогащение снапшота описанием (`core/descriptions.py` + `SnapshotService`), чистый билдер `build_onboarding` (`core/onboarding.py`), фасадная функция `read_api.onboarding` → тонкие HTTP-роут, MCP-тул №15, web-секции и TUI-детализация. Спека: `docs/superpowers/specs/2026-07-18-onboarding-view-design.md` (DESIGN-801..808, семантика S-1..S-4).

**Tech Stack:** Python 3.12, pydantic v2, FastAPI, FastMCP (in-memory `Client`), Textual, vanilla JS SPA.

## Global Constraints

- Гейты после КАЖДОГО таска: `uv run pytest -q` (baseline: 264 passed + 1 skipped, warning-free), `uv run ruff format --check .`, `uv run ruff check .`, `uv run pyrefly check`.
- Семантика §2 спеки — закон: dep закрыт ⇔ `computed_status ∈ DONE_STATUSES` (реиспользуемая константа из `roadmap.py`, НЕ перепечатанный литерал); dep в `drift` → `blocked_by`; неизвестный dep → `blocked_by` + warning; только прямые deps; флаг называется `actionable` (никогда `ready`).
- Порядок `next_items`: `(not actionable, phase or "", id)` — пинуется тестом.
- Обрезка описания: 360 символов, по границе слова, суффикс `…` — пинуется тестом.
- MCP-тул возвращает `model_dump(mode="json")`; лукап-ошибки → `ToolError` с HTTP-текстом `unknown project: {name}`.
- Ветки: Tasks 1–4 → `feat/onboarding-data` (PR A, от master); Tasks 5–7 → `feat/onboarding-ui` (PR B, stacked на A). Прямые коммиты в master запрещены.

---

### Task 1: `core/descriptions.py` + snapshot fields + enrichment (DESIGN-801)

**Files:**
- Create: `dispatcher/core/descriptions.py`
- Modify: `dispatcher/core/models.py` (поля `ProjectSnapshot`, ~строка 99)
- Modify: `dispatcher/core/service.py` (`SnapshotService._collect`, перед `return` на строке 95)
- Test: `tests/test_descriptions.py` (create), `tests/test_service.py` (добавить)

**Interfaces:**
- Produces: `DescriptionSource = Literal["readme", "pyproject", "package.json"]` (в `models.py`); `extract_project_description(path: Path) -> tuple[str | None, DescriptionSource | None]`; поля `ProjectSnapshot.description: str | None` и `ProjectSnapshot.description_source: DescriptionSource | None` (Tasks 2–6 читают их).

- [ ] **Step 1: Write the failing tests**

`tests/test_descriptions.py`:

```python
"""DESIGN-801: README-first description extraction with metadata fallback."""

from pathlib import Path

from dispatcher.core.descriptions import extract_project_description


def test_readme_first_meaningful_paragraph(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        "# Title\n\n"
        "[![build](https://x/badge.svg)](https://x)\n\n"
        "<p align=\"center\"><img src=\"logo.png\"></p>\n\n"
        "Dispatcher is an ecosystem dashboard.\n"
        "It watches every project.\n\n"
        "Second paragraph must not leak.\n"
    )
    text, source = extract_project_description(tmp_path)
    assert text == "Dispatcher is an ecosystem dashboard. It watches every project."
    assert source == "readme"


def test_readme_all_noise_falls_back_to_pyproject(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Only a title\n\n![badge](x.svg)\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndescription = "Terse authoritative line."\n'
    )
    text, source = extract_project_description(tmp_path)
    assert text == "Terse authoritative line."
    assert source == "pyproject"


def test_package_json_is_last_fallback(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"description": "From npm metadata."}')
    text, source = extract_project_description(tmp_path)
    assert text == "From npm metadata."
    assert source == "package.json"


def test_no_sources_returns_none(tmp_path: Path) -> None:
    assert extract_project_description(tmp_path) == (None, None)


def test_rst_readme_skips_title_underline(tmp_path: Path) -> None:
    (tmp_path / "README.rst").write_text(
        "My Project\n==========\n\nAn rst-described project.\n"
    )
    text, source = extract_project_description(tmp_path)
    assert text == "An rst-described project."
    assert source == "readme"


def test_extensionless_readme_case_insensitive(tmp_path: Path) -> None:
    (tmp_path / "readme").write_text("Plain readme text.\n")
    assert extract_project_description(tmp_path) == ("Plain readme text.", "readme")


def test_non_utf8_readme_degrades_to_next_source(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_bytes(b"\xff\xfe broken")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndescription = "Fallback wins."\n'
    )
    assert extract_project_description(tmp_path) == ("Fallback wins.", "pyproject")


def test_oversized_readme_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("x" * (256 * 1024 + 1))
    assert extract_project_description(tmp_path) == (None, None)


def test_trim_is_360_word_boundary_with_ellipsis(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(("word " * 100).strip() + "\n")
    text, _ = extract_project_description(tmp_path)
    assert text is not None
    assert len(text) <= 361  # 360 + "…"
    assert text.endswith("…")
    assert not text[:-1].endswith(" ")  # cut on a word boundary, then rstrip
```

Добавить в `tests/test_service.py` (import `SnapshotService`, `DispatcherConfig`, конфтестовые builders уже есть в файле — следовать её существующим импортам):

```python
def test_collect_enriches_descriptions(tmp_path: Path) -> None:
    p = make_arbiter(tmp_path)
    (p / "README.md").write_text("Arbiter routes agents.\n")
    svc = SnapshotService(DispatcherConfig(roots=(tmp_path,)))
    snapshots, _ = svc.get()
    arb = next(s for s in snapshots if s.name == "arbiter")
    assert arb.description == "Arbiter routes agents."
    assert arb.description_source == "readme"
    undetected = [s for s in snapshots if not s.detected]
    assert all(s.description is None for s in undetected)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_descriptions.py tests/test_service.py -q`
Expected: FAIL — `ModuleNotFoundError: dispatcher.core.descriptions` и AssertionError/AttributeError по description.

- [ ] **Step 3: Implement**

`dispatcher/core/models.py` — добавить рядом с верхними определениями (до `ProjectSnapshot`):

```python
DescriptionSource = Literal["readme", "pyproject", "package.json"]
```

(`from typing import Any, Literal` — расширить существующий импорт). В `ProjectSnapshot` после `freshness`:

```python
    # DESIGN-801: onboarding description, filled post-collect by
    # SnapshotService — collectors never set it.
    description: str | None = None
    description_source: DescriptionSource | None = None
```

`dispatcher/core/descriptions.py` (новый):

```python
"""Project description extraction (DESIGN-801).

README-first (richer for onboarding), metadata fallback (terse but
authoritative) — the trade-off is named in the design spec. Reads ONLY
under the given project path; every failure degrades to the next source,
never raises.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from dispatcher.core.models import DescriptionSource

_MAX_SOURCE_BYTES = 256 * 1024
_TRIM_LIMIT = 360
_README_NAMES = ("readme.md", "readme.rst", "readme")  # priority order
_NOISE_PREFIXES = ("#", "[![", "![", "<")  # headings, badges, HTML/comments
_UNDERLINE_CHARS = set("=-~^\"'`*+")


def extract_project_description(
    path: Path,
) -> tuple[str | None, DescriptionSource | None]:
    """First meaningful README paragraph, else pyproject/package.json."""
    for text, source in (
        (_from_readme(path), "readme"),
        (_from_pyproject(path), "pyproject"),
        (_from_package_json(path), "package.json"),
    ):
        if text:
            return text, source
    return None, None


def _from_readme(path: Path) -> str | None:
    file = _find_readme(path)
    if file is None:
        return None
    text = _read_limited(file)
    return _first_paragraph(text) if text else None


def _find_readme(path: Path) -> Path | None:
    try:
        entries = {p.name.lower(): p for p in path.iterdir() if p.is_file()}
    except OSError:
        return None
    for name in _README_NAMES:
        if name in entries:
            return entries[name]
    return None


def _read_limited(file: Path) -> str | None:
    try:
        if file.stat().st_size > _MAX_SOURCE_BYTES:
            return None
        return file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _first_paragraph(text: str) -> str | None:
    para: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if para:
                break
            continue
        if _is_underline(stripped):
            if len(para) == 1:
                # setext/rst heading: the underline retroactively marks the
                # single collected line as a TITLE, not a paragraph
                para = []
                continue
            if para:
                break
            continue
        if stripped.startswith(_NOISE_PREFIXES):
            if para:
                break
            continue
        para.append(stripped)
    joined = " ".join(para).strip()
    return _trim(joined) if joined else None


def _is_underline(stripped: str) -> bool:
    """rst/setext title underlines and md rules: one repeated punct char."""
    return len(set(stripped)) == 1 and stripped[0] in _UNDERLINE_CHARS


def _trim(text: str) -> str:
    if len(text) <= _TRIM_LIMIT:
        return text
    head, _, _ = text[:_TRIM_LIMIT].rpartition(" ")
    return (head or text[:_TRIM_LIMIT]).rstrip() + "…"


def _from_pyproject(path: Path) -> str | None:
    text = _read_limited(path / "pyproject.toml")
    if text is None:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    desc = data.get("project", {}).get("description")
    return _clean_meta(desc)


def _from_package_json(path: Path) -> str | None:
    text = _read_limited(path / "package.json")
    if text is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    desc = data.get("description") if isinstance(data, dict) else None
    return _clean_meta(desc)


def _clean_meta(desc: object) -> str | None:
    if not isinstance(desc, str) or not desc.strip():
        return None
    return _trim(desc.strip())
```

`dispatcher/core/service.py` — импорт + обогащение в конце `_collect` (перед `return snapshots, warnings`):

```python
from dispatcher.core.descriptions import extract_project_description
```

```python
        # DESIGN-801: one post-collect enrichment instead of five
        # per-collector implementations; undetected rows (path="") stay None.
        for snap in snapshots:
            if snap.detected and snap.path:
                desc, source = extract_project_description(Path(snap.path))
                snap.description = desc
                snap.description_source = source
        return snapshots, warnings
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_descriptions.py tests/test_service.py tests/test_models.py -q`
Expected: PASS.

- [ ] **Step 5: Full gates, then commit**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/descriptions.py dispatcher/core/models.py dispatcher/core/service.py tests/test_descriptions.py tests/test_service.py
git commit -m "feat: project description extraction + snapshot enrichment (DESIGN-801)"
```

---

### Task 2: `core/onboarding.py` — models + `build_onboarding` (DESIGN-802, S-1..S-4)

**Files:**
- Create: `dispatcher/core/onboarding.py`
- Modify: `dispatcher/core/roadmap.py` (переименовать `_DONE` → `DONE_STATUSES`; 5 использований: строки 25, 249, 262, 321 (`build_blockers`) и в `_apply_blocked` ~478 — после правки `grep -n "_DONE" dispatcher/core/roadmap.py` обязан вернуть пусто)
- Test: `tests/test_onboarding.py` (create)

**Interfaces:**
- Consumes: `ProjectSnapshot` (с полями Task 1), `RoadmapResponse`, `ProjectSummary`, `PhaseSummary`, `build_summary`, `build_phases`, `ContractStatus`, `TaskInfo`.
- Produces: `DONE_STATUSES: tuple[str, str]` (публично из `roadmap.py`); модели `OnboardingProject`, `OnboardingRoadmapPosition`, `OnboardingNextItem`, `OnboardingView`; `build_onboarding(snapshot: ProjectSnapshot, roadmap: RoadmapResponse, contracts: list[ContractStatus]) -> OnboardingView`. Tasks 3–6 зависят от этих имён.

- [ ] **Step 1: Rename `_DONE` → `DONE_STATUSES` in roadmap.py**

Механическая замена всех 5 вхождений `_DONE` на `DONE_STATUSES` (semantics S-1: константа реиспользуется, не перепечатывается). Проверка полноты: `grep -n "_DONE" dispatcher/core/roadmap.py` → пусто. `uv run pytest tests/test_roadmap.py -q` — PASS (чистое переименование).

- [ ] **Step 2: Write the failing tests**

`tests/test_onboarding.py` — билдер тестируется на синтетических `RoadmapResponse`/`ProjectSnapshot`, без файловой обвязки:

```python
"""DESIGN-802: build_onboarding — the canonical 'what next' join (S-1..S-4)."""

from dispatcher.core.models import ContractStatus, ProjectSnapshot, TaskInfo
from dispatcher.core.onboarding import build_onboarding
from dispatcher.core.roadmap import RoadmapItemView, RoadmapResponse


def _item(
    item_id: str,
    status: str,
    *,
    owner: str | None = "proj",
    phase: str | None = "1",
    deps: list[str] | None = None,
) -> RoadmapItemView:
    return RoadmapItemView(
        id=item_id,
        title=f"title {item_id}",
        phase=phase,
        owner_project=owner,
        depends_on=deps or [],
        computed_status=status,
        source="fixture.yaml",
    )


def _snap(**kwargs: object) -> ProjectSnapshot:
    return ProjectSnapshot(name="proj", path="/w/proj", **kwargs)  # type: ignore[arg-type]


def _view(items: list[RoadmapItemView], snap: ProjectSnapshot | None = None):
    roadmap = RoadmapResponse(roadmaps=["r"], items=items, warnings=[])
    return build_onboarding(snap or _snap(), roadmap, contracts=[])


def test_s1_verified_dep_makes_item_actionable() -> None:
    view = _view([_item("A", "planned", deps=["B"]), _item("B", "verified")])
    (next_a,) = [n for n in view.next_items if n.id == "A"]
    assert next_a.actionable is True
    assert next_a.blocked_by == []


def test_s1_planned_dep_blocks() -> None:
    view = _view([_item("A", "planned", deps=["B"]), _item("B", "planned")])
    (next_a,) = [n for n in view.next_items if n.id == "A"]
    assert next_a.actionable is False
    assert next_a.blocked_by == ["B"]


def test_s2_drift_dep_blocks() -> None:
    view = _view([_item("A", "planned", deps=["B"]), _item("B", "drift")])
    (next_a,) = [n for n in view.next_items if n.id == "A"]
    assert next_a.actionable is False
    assert next_a.blocked_by == ["B"]


def test_s3_unknown_dep_blocks_and_warns() -> None:
    view = _view([_item("A", "planned", deps=["GHOST"])])
    (next_a,) = view.next_items
    assert next_a.actionable is False
    assert next_a.blocked_by == ["GHOST"]
    assert "unknown dependency id: GHOST (item A)" in view.warnings


def test_non_planned_item_still_reports_blockers() -> None:
    # _apply_blocked only paints planned items; onboarding must see through
    view = _view([_item("A", "unknown", deps=["B"]), _item("B", "planned")])
    (next_a,) = [n for n in view.next_items if n.id == "A"]
    assert next_a.blocked_by == ["B"]


def test_done_items_are_not_next() -> None:
    view = _view([_item("A", "implemented"), _item("B", "verified")])
    assert view.next_items == []


def test_order_actionable_first_then_phase_then_id() -> None:
    view = _view(
        [
            _item("Z1", "planned", phase="1", deps=["GHOST"]),  # blocked ph1
            _item("A2", "planned", phase="2"),  # actionable ph2
            _item("A1", "planned", phase="1"),  # actionable ph1
            _item("A1B", "planned", phase="1"),  # actionable ph1, id after A1
        ]
    )
    assert [n.id for n in view.next_items] == ["A1", "A1B", "A2", "Z1"]


def test_foreign_items_are_excluded() -> None:
    view = _view([_item("X", "planned", owner="other")])
    assert view.next_items == []
    assert view.roadmap_position is None


def test_no_items_position_is_none_but_view_lives() -> None:
    snap = _snap(
        description="Desc.",
        description_source="readme",
        tasks=[TaskInfo(task_id="T-1", status="in_progress", source="db")],
    )
    view = _view([], snap)
    assert view.roadmap_position is None
    assert view.project.description == "Desc."
    assert [t.task_id for t in view.live_tasks] == ["T-1"]


def test_position_reuses_build_summary_row() -> None:
    view = _view([_item("A", "verified"), _item("B", "planned")])
    pos = view.roadmap_position
    assert pos is not None
    assert pos.summary.project == "proj"
    assert pos.summary.total == 2 and pos.summary.done == 1
    assert pos.median_readiness == 0.5
    assert [p.phase for p in pos.phases] == ["1"]


def test_live_tasks_filter_and_warning_merge_dedup() -> None:
    snap = _snap(
        tasks=[
            TaskInfo(task_id="T-1", status="pending", source="db"),
            TaskInfo(task_id="T-2", status="completed", source="db"),
            TaskInfo(task_id="T-3", status="in_progress", source="db"),
        ],
        warnings=["dup", "snap-only"],
    )
    roadmap = RoadmapResponse(roadmaps=["r"], items=[], warnings=["dup", "rm-only"])
    view = build_onboarding(snap, roadmap, contracts=[])
    assert [t.task_id for t in view.live_tasks] == ["T-1", "T-3"]
    assert view.warnings == ["dup", "snap-only", "rm-only"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_onboarding.py -q`
Expected: FAIL — `ModuleNotFoundError: dispatcher.core.onboarding`.

- [ ] **Step 4: Implement `dispatcher/core/onboarding.py`**

```python
"""FR-04 onboarding read-model (DESIGN-802): one canonical 'what next' join.

Dependency semantics S-1..S-4 (design spec §2): a dep is satisfied iff
its computed_status is in DONE_STATUSES; drift and unknown ids block
(pessimism under uncertainty, unknown ids also warn); only DIRECT deps —
an unmet transitive dep keeps the direct dep out of DONE_STATUSES, so
the effect cascades without recursion.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from dispatcher.core.models import (
    ContractStatus,
    DescriptionSource,
    ProjectSnapshot,
    TaskInfo,
)
from dispatcher.core.roadmap import (
    DONE_STATUSES,
    PhaseSummary,
    ProjectSummary,
    RoadmapResponse,
    build_phases,
    build_summary,
)

_LIVE_STATUSES = ("pending", "in_progress")


class OnboardingProject(BaseModel):
    """Identity block of the onboarding screen."""

    name: str
    path: str
    description: str | None = None
    description_source: DescriptionSource | None = None
    freshness: str | None = None


class OnboardingRoadmapPosition(BaseModel):
    """Where the project stands: its build_summary row + own-phase cuts."""

    summary: ProjectSummary
    median_readiness: float | None = None
    phases: list[PhaseSummary] = Field(default_factory=list)


class OnboardingNextItem(BaseModel):
    """One not-done roadmap item with the actionable/blocked verdict."""

    id: str
    title: str
    phase: str | None = None
    computed_status: str
    actionable: bool  # named actionable, NOT ready — see spec §2 naming
    blocked_by: list[str] = Field(default_factory=list)


class OnboardingView(BaseModel):
    """Response of GET /api/projects/{name}/onboarding (FR-04)."""

    project: OnboardingProject
    roadmap_position: OnboardingRoadmapPosition | None = None
    next_items: list[OnboardingNextItem] = Field(default_factory=list)
    live_tasks: list[TaskInfo] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def build_onboarding(
    snapshot: ProjectSnapshot,
    roadmap: RoadmapResponse,
    contracts: list[ContractStatus],
) -> OnboardingView:
    """Pure join of one snapshot with the roadmap (style of build_summary)."""
    own = [i for i in roadmap.items if i.owner_project == snapshot.name]
    by_id = {i.id: i for i in roadmap.items}
    warnings = [*snapshot.warnings, *roadmap.warnings]

    next_items: list[OnboardingNextItem] = []
    for item in own:
        if item.computed_status in DONE_STATUSES:
            continue
        blocked_by = [
            dep
            for dep in item.depends_on
            if dep not in by_id or by_id[dep].computed_status not in DONE_STATUSES
        ]
        warnings.extend(
            f"unknown dependency id: {dep} (item {item.id})"
            for dep in item.depends_on
            if dep not in by_id
        )
        next_items.append(
            OnboardingNextItem(
                id=item.id,
                title=item.title,
                phase=item.phase,
                computed_status=item.computed_status,
                actionable=not blocked_by,
                blocked_by=blocked_by,
            )
        )
    next_items.sort(key=lambda n: (not n.actionable, n.phase or "", n.id))

    position: OnboardingRoadmapPosition | None = None
    if own:
        summary = build_summary(roadmap, contracts)
        row = next(p for p in summary.projects if p.project == snapshot.name)
        own_view = RoadmapResponse(roadmaps=roadmap.roadmaps, items=own, warnings=[])
        position = OnboardingRoadmapPosition(
            summary=row,
            median_readiness=summary.median_readiness,
            phases=build_phases(own_view).phases,
        )

    return OnboardingView(
        project=OnboardingProject(
            name=snapshot.name,
            path=snapshot.path,
            description=snapshot.description,
            description_source=snapshot.description_source,
            freshness=snapshot.freshness,
        ),
        roadmap_position=position,
        next_items=next_items,
        live_tasks=[t for t in snapshot.tasks if t.status in _LIVE_STATUSES],
        warnings=list(dict.fromkeys(warnings)),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_onboarding.py tests/test_roadmap.py -q`
Expected: PASS.

- [ ] **Step 6: Full gates, then commit**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/onboarding.py dispatcher/core/roadmap.py tests/test_onboarding.py
git commit -m "feat: build_onboarding — canonical next-items join (DESIGN-802)"
```

---

### Task 3: facade + HTTP route (DESIGN-803)

**Files:**
- Modify: `dispatcher/core/read_api.py` (импорты + функция после `roadmap_item`)
- Modify: `dispatcher/server/app.py` (роут ПОСЛЕ строки `roadmap_dirs = ...` — она на 163; логично после `roadmap_summary`, до `roadmap_item`; + импорт `OnboardingView`)
- Test: `tests/test_api.py` (добавить)

**Interfaces:**
- Consumes: `build_onboarding`, `OnboardingView` (Task 2); паттерн `roadmap_summary` (один прогон `check_contracts`).
- Produces: `read_api.onboarding(cache: SnapshotService, roadmap_dirs: tuple[Path, ...], name: str) -> OnboardingView` (Tasks 4–6 зовут её); `GET /api/projects/{name}/onboarding` (404 detail: `unknown project: {name}`).

- [ ] **Step 1: Write the failing tests**

В `tests/test_api.py` (использовать существующий в файле паттерн `httpx.ASGITransport` + конфтестовые builders; роадмап-фикстуру писать в `tmp_path / "prograph-vault" / "authored" / "roadmaps" / "fixture.yaml"` — как в test_mcp_server.py):

```python
_ONBOARDING_ROADMAP = """
version: 1
roadmap: onboarding-api-fixture
title: Fixture
items:
  - id: RD-OB-DONE
    title: Done dep
    phase: "1"
    owner_project: arbiter
    evidence_rules:
      - rule: project_detected
        kind: implementation
        project: arbiter
      - rule: work_item_chain
        kind: verification
        work_item_id: T-9
        min_links: 2
  - id: RD-OB-NEXT
    title: Actionable next
    phase: "2"
    owner_project: arbiter
    depends_on: [RD-OB-DONE]
    evidence_rules:
      - rule: file_exists
        kind: implementation
        project: arbiter
        path: contracts/nope.json
  - id: RD-OB-BLOCKED
    title: Blocked by ghost
    phase: "2"
    owner_project: arbiter
    depends_on: [RD-OB-GHOST]
    evidence_rules:
      - rule: file_exists
        kind: implementation
        project: arbiter
        path: contracts/also-nope.json
"""


async def test_onboarding_endpoint(tmp_path: Path) -> None:
    make_arbiter(tmp_path)
    (tmp_path / "arbiter" / "README.md").write_text("Arbiter routes agents.\n")
    vault = tmp_path / "prograph-vault" / "authored" / "roadmaps"
    vault.mkdir(parents=True)
    (vault / "fixture.yaml").write_text(_ONBOARDING_ROADMAP)
    app = create_app(DispatcherConfig(roots=(tmp_path,)))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/api/projects/arbiter/onboarding")
        assert resp.status_code == 200
        body = resp.json()
        assert body["project"]["description"] == "Arbiter routes agents."
        assert body["project"]["description_source"] == "readme"
        pos = body["roadmap_position"]
        assert pos["summary"]["project"] == "arbiter"
        ids = [n["id"] for n in body["next_items"]]
        assert ids == ["RD-OB-NEXT", "RD-OB-BLOCKED"]  # actionable first
        by_id = {n["id"]: n for n in body["next_items"]}
        assert by_id["RD-OB-NEXT"]["actionable"] is True
        assert by_id["RD-OB-BLOCKED"]["blocked_by"] == ["RD-OB-GHOST"]
        assert any("unknown dependency id" in w for w in body["warnings"])

        missing = await client.get("/api/projects/no-such/onboarding")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "unknown project: no-such"
```

Прекондишен-ловушка (фикстура не должна тихо развакууситься): в том же тесте до onboarding-запроса —

```python
        roadmap = (await client.get("/api/roadmap")).json()
        statuses = {i["id"]: i["computed_status"] for i in roadmap["items"]}
        assert statuses["RD-OB-DONE"] == "verified"  # fixture precondition
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api.py -q`
Expected: FAIL — 404 на `/api/projects/arbiter/onboarding` (роут не существует).

- [ ] **Step 3: Implement**

`read_api.py` — в импорт из `dispatcher.core.onboarding`:

```python
from dispatcher.core.onboarding import OnboardingView, build_onboarding
```

Функция после `roadmap_item`:

```python
def onboarding(
    cache: SnapshotService, roadmap_dirs: tuple[Path, ...], name: str
) -> OnboardingView:
    """FR-04 one-screen join: description, roadmap position, next items."""
    snapshots, _ = cache.get()
    snap = next((s for s in snapshots if s.name == name), None)
    if snap is None:
        raise ReadLookupError(f"unknown project: {name}")
    projects = {s.name: Path(s.path) for s in snapshots if s.detected and s.path}
    # One checker run feeds the roadmap projection AND the summary join,
    # same as roadmap_summary (ADR-R5).
    contracts_state = check_contracts(projects)
    roadmap_state = build_roadmap(roadmap_dirs, snapshots, contracts=contracts_state)
    return build_onboarding(snap, roadmap_state, contracts_state)
```

`server/app.py` — импорт `from dispatcher.core.onboarding import OnboardingView`; роут после `roadmap_summary` (за строкой 187, до `/api/roadmap/{item_id}` можно и после — конфликтов путей нет, но он ОБЯЗАН стоять после присваивания `roadmap_dirs`):

```python
    @app.get("/api/projects/{name}/onboarding", response_model=OnboardingView)
    def project_onboarding(name: str) -> OnboardingView:
        """FR-04: описание + позиция в roadmap + предстоящие задачи."""
        try:
            return read_api.onboarding(cache, roadmap_dirs, name)
        except read_api.ReadLookupError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api.py -q`
Expected: PASS.

- [ ] **Step 5: Full gates, then commit**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/read_api.py dispatcher/server/app.py tests/test_api.py
git commit -m "feat: read_api.onboarding + GET /api/projects/{name}/onboarding (DESIGN-803)"
```

---

### Task 4: MCP-тул №15 + parity (DESIGN-806) — завершает PR A

**Files:**
- Modify: `dispatcher/mcp_server.py` (тул после `spec_runner_configs`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `read_api.onboarding` (Task 3).
- Produces: MCP-тул `onboarding(project: str)`; whitelist 15 имён.

- [ ] **Step 1: Write the failing tests**

`tests/test_mcp_server.py`:

1. В `EXPECTED_TOOLS` добавить `"onboarding"`.
2. Расширить `_ROADMAP_FIXTURE` items-ами S-1/S-3 (verified dep через `project_detected`+`work_item_chain` на arbiter/T-9 — как в `_ONBOARDING_ROADMAP` Task 3; blocked-by-ghost item). **Осознанная адаптация спеки**: DESIGN-807 просит S-1..S-3 в фикстуре, но S-2 (drift-dep) требует контрактной пары с известным именем в фикстурном workspace — хрупко; S-2 уже двусторонне пинован юнитами Task 2, а MCP-parity сравнивает JSON, не семантику, поэтому в MCP-фикстуре S-2 не дублируется.
3. В PARITY-таблицу добавить строку:

```python
    ("onboarding", {"project": "arbiter"}, "/api/projects/arbiter/onboarding"),
```

4. В `test_lookup_errors_carry_http_detail_text` добавить кейс:

```python
        with pytest.raises(ToolError, match="unknown project: no-such"):
            await client.call_tool("onboarding", {"project": "no-such"})
```

5. В `test_serializers_agree_for_every_read_model` добавить в список populated-моделей:

```python
        read_api.onboarding(cache, roadmap_dirs, "arbiter"),
```

(`roadmap_dirs` в тесте взять как в самом сервере: `config.roadmap_dirs or default_roadmap_dirs(config.roots)` — импорт уже есть в модуле сервера, в тест добавить `from dispatcher.core.roadmap import default_roadmap_dirs`.)

6. Прекондишен-ловушка в parity-тесте фикстуры: onboarding-ответ должен содержать И actionable, И blocked item — иначе parity сравнивает пустышки:

```python
        # fixture must exercise both verdicts, not compare empty lists
        flags = {n["actionable"] for n in tool_json["next_items"]}
        assert flags == {True, False}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_server.py -q`
Expected: FAIL — whitelist equality (нет тула `onboarding`).

- [ ] **Step 3: Implement the tool**

В `build_server` после `spec_runner_configs`:

```python
    @mcp.tool
    def onboarding(
        project: Annotated[
            str,
            Field(description="Collector name, e.g. 'Maestro' or 'arbiter'"),
        ],
    ) -> dict[str, Any]:
        """One-screen onboarding join for ONE project: description,
        roadmap position (readiness vs median, own-phase cuts), next_items
        with actionable/blocked_by verdicts, and live pending/in_progress
        tasks. For 'what should I do next in project X' prefer THIS over
        combining project() + roadmap_summary(). Errors with
        'unknown project: <name>' if the name is not monitored."""
        try:
            return read_api.onboarding(cache, roadmap_dirs, project).model_dump(
                mode="json"
            )
        except read_api.ReadLookupError as err:
            raise ToolError(str(err)) from err
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_server.py -q`
Expected: PASS (все parity/whitelist/serializer/lookup).

- [ ] **Step 5: Full gates, commit, push PR A**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git add dispatcher/mcp_server.py tests/test_mcp_server.py
git commit -m "feat: MCP onboarding tool — 15th read-only tool (DESIGN-806)"
```

PR A (`feat/onboarding-data` → master): «feat: onboarding data plane (DESIGN-801..803, 806)».

---

### Task 5: web-секции (DESIGN-804)

**Files:**
- Modify: `dispatcher/server/static/index.html` (функция `detail()` ~строка 453; CSS `#detail` ~строка 57)
- Test: `tests/test_api.py` (static-пин)

**Interfaces:**
- Consumes: `GET /api/projects/{name}/onboarding` (Task 3), существующие JS-хелперы `get(path)` и `esc(s)`.

- [ ] **Step 1: Write the failing test**

В `tests/test_api.py` найти существующий тест, отдающий `index.html` (например `test_index_served`), и добавить пины:

```python
    assert "/onboarding" in html  # detail() fetches the onboarding view
    assert "onboarding-next" in html  # structured sections replaced raw JSON
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api.py -q`
Expected: FAIL на новых assert-ах.

- [ ] **Step 3: Implement**

CSS: строку `#detail { white-space: pre-wrap; font-family: ui-monospace, monospace; ... }` заменить на обычный текст (моноширинность больше не нужна — raw JSON уходит):

```css
  #detail { font-size: 13px; }
  #detail h3 { font-size: 14px; margin: 12px 0 6px; }
  #detail .dim { color: var(--dim); }
  #detail li.blocked { color: var(--dim); }
  #detail ul { margin: 0; padding-left: 18px; }
```

JS: перед `detail()` добавить рендерер; в `detail()` заменить две строки (`const snap = await get(...)`; `document.getElementById("detail").textContent = JSON.stringify(...)`) на вызов onboarding:

```js
function renderOnboarding(ob) {
  const p = ob.project;
  const desc = p.description
    ? `<p>${esc(p.description)} <span class="dim">[${esc(p.description_source)}]</span></p>`
    : `<p class="dim">no description (README/pyproject/package.json)</p>`;
  const pos = ob.roadmap_position;
  const position = pos ? `
    <h3>Roadmap position</h3>
    <p>readiness ${(pos.summary.readiness * 100).toFixed(0)}%
      (${pos.summary.done}/${pos.summary.total})
      · median ${pos.median_readiness == null ? "—"
        : (pos.median_readiness * 100).toFixed(0) + "%"}
      ${pos.summary.lagging ? " · <b>lagging</b>" : ""}
      ${pos.summary.contract_drift ? " · <b>contract drift</b>" : ""}</p>
    <ul>${pos.phases.map(ph => `<li>phase ${esc(ph.phase ?? "—")}: ${
      esc(Object.entries(ph.counts).map(([k, v]) => `${k}=${v}`).join(", "))
    }</li>`).join("")}</ul>`
    : `<p class="dim">no roadmap items for this project</p>`;
  const next = ob.next_items.length ? `
    <h3>Next items</h3>
    <ul id="onboarding-next">${ob.next_items.map(n => `
      <li class="${n.actionable ? "actionable" : "blocked"}">
        ${n.actionable ? "▶" : "⛔"} ${esc(n.id)} · ${esc(n.title)}
        · ${esc(n.computed_status)}${n.blocked_by.length
          ? ` · blocked by: ${esc(n.blocked_by.join(", "))}` : ""}
      </li>`).join("")}</ul>`
    : `<ul id="onboarding-next" hidden></ul>`;
  const live = ob.live_tasks.length ? `
    <h3>Live tasks</h3>
    <ul>${ob.live_tasks.map(t => `
      <li>${esc(t.task_id)} · ${esc(t.status)} · ${esc(t.title ?? "")}</li>`
    ).join("")}</ul>` : "";
  const warn = ob.warnings.length
    ? `<p class="dim">⚠ ${esc(ob.warnings.join(" · "))}</p>` : "";
  return desc + position + next + live + warn;
}
```

В `detail()`:

```js
  try {
    const ob = await get(
      "/api/projects/" + encodeURIComponent(name) + "/onboarding"
    );
    document.getElementById("detail").innerHTML = renderOnboarding(ob);
  } catch (err) {
    document.getElementById("detail").textContent = String(err);
  }
```

(Остальное тело `detail()` — spec-runner-панель и scrollIntoView — без изменений.)

- [ ] **Step 4: Run tests, manual smoke**

Run: `uv run pytest tests/test_api.py -q` — PASS.
Smoke: `uv run dispatcher serve --config <любой валидный конфиг> &`, открыть `http://127.0.0.1:8000`, кликнуть карточку проекта — секции вместо raw JSON; убить сервер.

- [ ] **Step 5: Full gates, then commit**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git add dispatcher/server/static/index.html tests/test_api.py
git commit -m "feat: web onboarding sections replace raw snapshot JSON (DESIGN-804)"
```

---

### Task 6: TUI-детализация (DESIGN-805)

**Files:**
- Modify: `dispatcher/tui/detail.py` (`ProjectDetailScreen.__init__`/`compose` + новый `_onboarding_sections`)
- Modify: `dispatcher/tui/app.py` (сохранить contracts в `_apply`; передать onboarding при push, ~строка 546)
- Test: `tests/test_tui.py`

**Interfaces:**
- Consumes: `OnboardingView`, `build_onboarding` (Task 2); в `app.py` уже есть `self._roadmap` и переменная `contracts` в refresh-цикле.
- Produces: `ProjectDetailScreen(snap: ProjectSnapshot, onboarding: OnboardingView | None = None)` — второй параметр опционален, `None` деградирует к сегодняшнему экрану.

- [ ] **Step 1: Write the failing test**

В `tests/test_tui.py` (по образцу существующих detail-тестов файла — они гоняют `run_test()` и ищут текст на экране):

```python
async def test_detail_renders_onboarding_sections(tmp_path: Path) -> None:
    # fixture: снапшот + синтетический OnboardingView (билдер уже пинован
    # юнитами — здесь пинуется только рендер секций)
    from dispatcher.core.onboarding import (
        OnboardingNextItem,
        OnboardingProject,
        OnboardingView,
    )
    from dispatcher.tui.detail import ProjectDetailScreen

    snap = ProjectSnapshot(name="arbiter", path="/w/arbiter")
    view = OnboardingView(
        project=OnboardingProject(
            name="arbiter",
            path="/w/arbiter",
            description="Arbiter routes agents.",
            description_source="readme",
        ),
        next_items=[
            OnboardingNextItem(
                id="RD-1",
                title="Do the thing",
                phase="1",
                computed_status="planned",
                actionable=False,
                blocked_by=["RD-0"],
            )
        ],
        live_tasks=[TaskInfo(task_id="T-7", status="pending", source="db")],
    )
    screen = ProjectDetailScreen(snap, view)
    rendered = "\n".join(
        str(part) for part in screen._render_texts()  # см. Step 3
    )
    assert "Arbiter routes agents." in rendered
    assert "RD-1" in rendered and "blocked by: RD-0" in rendered
    assert "T-7" in rendered
    assert "collected:" in rendered  # старые snapshot-секции живы


async def test_detail_without_onboarding_degrades(tmp_path: Path) -> None:
    from dispatcher.tui.detail import ProjectDetailScreen

    snap = ProjectSnapshot(name="arbiter", path="/w/arbiter")
    rendered = "\n".join(str(p) for p in ProjectDetailScreen(snap)._render_texts())
    assert "collected:" in rendered
    assert "next items" not in rendered
```

(Если в test_tui.py существующие detail-тесты идут через `run_test()`/pilot — следовать их стилю вместо `_render_texts`; ключевые assert-ы те же. `_render_texts` вводится в Step 3 именно чтобы рендер был тестируем без пилота.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tui.py -q`
Expected: FAIL — `TypeError` (второй аргумент) / нет `_render_texts`.

- [ ] **Step 3: Implement**

`dispatcher/tui/detail.py`:

```python
from dispatcher.core.onboarding import OnboardingView
```

```python
def _onboarding_sections(view: OnboardingView) -> list[tuple[str, list[str]]]:
    """Onboarding blocks rendered ABOVE the raw snapshot sections."""
    pos = view.roadmap_position
    position = (
        []
        if pos is None
        else [
            escape(
                f"readiness {pos.summary.readiness:.0%} "
                f"({pos.summary.done}/{pos.summary.total})"
                + (" · LAGGING" if pos.summary.lagging else "")
                + (" · CONTRACT DRIFT" if pos.summary.contract_drift else "")
            ),
            *[
                escape(
                    f"phase {p.phase or '—'}: "
                    + ", ".join(f"{k}={v}" for k, v in sorted(p.counts.items()))
                )
                for p in pos.phases
            ],
        ]
    )
    return [
        (
            "description",
            [escape(f"{view.project.description} [{view.project.description_source}]")]
            if view.project.description
            else [],
        ),
        ("roadmap position", position),
        (
            "next items",
            [
                escape(
                    f"{'▶' if n.actionable else '⛔'} {n.id} · {n.title} "
                    f"· {n.computed_status}"
                    + (
                        f" · blocked by: {', '.join(n.blocked_by)}"
                        if n.blocked_by
                        else ""
                    )
                )
                for n in view.next_items
            ],
        ),
        (
            "live tasks",
            [
                escape(f"{t.task_id} · {t.status} · {t.title or ''}")
                for t in view.live_tasks
            ],
        ),
    ]
```

`ProjectDetailScreen`:

```python
    def __init__(
        self, snap: ProjectSnapshot, onboarding: OnboardingView | None = None
    ) -> None:
        super().__init__()
        self._snap = snap
        self._onboarding = onboarding

    def _render_texts(self) -> list[str]:
        """Static bodies in render order (plain list — unit-testable)."""
        s = self._snap
        texts = [
            f"[bold]{escape(s.name)}[/bold] — {escape(s.path)}\n"
            f"freshness: {escape(s.freshness or 'unknown')}\n"
            f"collected: {escape(s.collected_at.isoformat())} · "
            f"detected: {s.detected}"
        ]
        if self._onboarding is not None:
            texts.extend(
                _section(title, lines)
                for title, lines in _onboarding_sections(self._onboarding)
            )
        texts.extend(_section(title, lines) for title, lines in _sections(s))
        return texts

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll():
            for text in self._render_texts():
                yield Static(text, classes="detail-section")
        yield Footer()
```

(Первый Static теряет отсутствие класса `detail-section` — это безвредно; существующие тесты ищут текст, не классы. Если какой-то тест пинует классы — сохранить первый Static без класса, отделив `texts[0]`.)

`dispatcher/tui/app.py`:
1. Импорт: `from dispatcher.core.onboarding import build_onboarding` (+ `ContractStatus` если нужен тип поля).
2. В `__init__` рядом с `self._roadmap: RoadmapResponse | None = None` добавить `self._contracts: list[ContractStatus] = []`.
3. В `_apply(...)` рядом с `self._roadmap = roadmap` добавить `self._contracts = contracts`.
4. Место push (~546):

```python
                onboarding = (
                    build_onboarding(snap, self._roadmap, self._contracts)
                    if self._roadmap is not None
                    else None
                )
                self.push_screen(ProjectDetailScreen(snap, onboarding))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tui.py -q`
Expected: PASS (новые + все существующие detail-assertions).

- [ ] **Step 5: Full gates, then commit**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git add dispatcher/tui/detail.py dispatcher/tui/app.py tests/test_tui.py
git commit -m "feat: TUI project detail gains onboarding sections (DESIGN-805)"
```

---

### Task 7: документация (DESIGN-808) — завершает PR B

**Files:**
- Modify: `README.md` (секция API — новый эндпоинт; секция MCP — «15 read-only tools» + строка тула)
- Modify: `COWORK_CONTEXT.md` (interfaces line)
- Modify: `spec/discovery-brief-customer.md` (resolution-pointer FR-04)

**Interfaces:** нет (docs-only).

- [ ] **Step 1: README**

В списке API-эндпоинтов добавить:

```markdown
- `GET /api/projects/{name}/onboarding` — FR-04: описание проекта, позиция в roadmap (readiness vs median, phase-разрез) и предстоящие задачи (`actionable`/`blocked_by`) одним ответом.
```

В секции MCP: заменить «14 read-only tools» на «15 read-only tools», в перечень тулов добавить `onboarding` с одной строкой описания («что делать следующим в проекте X»).

- [ ] **Step 2: COWORK_CONTEXT.md**

В строку interfaces добавить `/api/projects/{name}/onboarding` (по образцу существующего перечисления).

- [ ] **Step 3: discovery-brief pointer**

К FR-04 добавить строку-пойнтер (стиль соседнего FR-05/FR-06):

```markdown
> Resolution: закрыто — design `docs/superpowers/specs/2026-07-18-onboarding-view-design.md`, web+TUI+MCP.
```

- [ ] **Step 4: Full gates, then commit**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git add README.md COWORK_CONTEXT.md spec/discovery-brief-customer.md
git commit -m "docs: onboarding endpoint/tool + FR-04 resolution pointer (DESIGN-808)"
```

PR B (`feat/onboarding-ui` → PR A / master): «feat: onboarding view surfaces (DESIGN-804..805, 808)».

---

## Final whole-branch review mandate

Финальное ревью гоняет РЕАЛЬНЫЕ поверхности, не пересказ диффов (anti-stub mandate):
- поднять `uvicorn` на fixture-workspace и руками дернуть `/api/projects/{name}/onboarding` (200 + 404);
- запустить MCP-сервер как stdio-подпроцесс и вызвать `onboarding` живым клиентом (включая ToolError-текст);
- проверить, что web `detail()` рендерит секции (хотя бы grep отсутствия `JSON.stringify(snap`);
- подтвердить семантику S-1..S-4 по тестам, а не по коду.
