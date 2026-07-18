# DESIGN-307 Config Suggestions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** AI-подсказки значений default-полей в spec-runner конфиг-редакторе через захардённую делегацию локальному `claude` CLI (recommendation only, human accept).

**Architecture:** Чистый bundle-билдер (`core/suggest_bundle.py`) → изолированный CLI-адаптер (`core/suggest_cli.py`, весь конверт-специфичный код внутри) → два эндпоинта (suggest/cancel) с audit → web-панель. Спека: `docs/superpowers/specs/2026-07-18-config-suggestions-design.md` (DESIGN-901..906, H-1..H-7).

**Tech Stack:** Python 3.12, subprocess (`shell=False`), pydantic v2, FastAPI, vanilla JS SPA.

## Global Constraints

- Гейты после КАЖДОГО таска: `uv run pytest -q` (baseline: 288 passed + 1 skipped, warning-free), `uv run ruff format --check .`, `uv run ruff check .`, `uv run pyrefly check`.
- H-1: контекст ТОЛЬКО stdin; бинарь — `claude` с PATH или абсолютный путь из конфига с basename ровно `claude`; флаги фиксированы кодом (`-p --output-format json`); `shell=False`. Никакой интерполяции bundle в argv.
- H-2/H-3: stdout — конверт агента; полезная нагрузка — СТРОКА в `.result`, парсится вторым проходом; вся конверт-специфика не покидает `suggest_cli.py`.
- H-4: все строковые значения bundle проходят `mask_secrets` (`dispatcher/core/collectors/base.py:108`) до сериализации.
- H-6: cancel = `terminate()` собственного процесса; лок освобождается немедленно; per-project семантика (чужой in-flight → 409 с именем).
- Подсказки — только для default-provenance полей; принятые значения проходят `validate_typed_fields`; по-полевое частичное принятие (dropped-список), совпадающие с дефолтом — дропаются.
- Peers — распределения-СПИСКИ `[{value, count, explicit_count}]` (не объекты), топ-5 по частоте + `{"other": N}`; отбор: все при ≤15, иначе топ-15 по `base_mtime` (freshness конфига — наблюдаемый прокси из спеки).
- **Зафиксированная адаптация спеки**: `field_schema` несёт `{type, default}` БЕЗ min/max/enum — у typed-полей констрейнтов нет нигде (валидация только по типам, `_TYPED_TYPES` в `spec_runner_config_schema.py`); выдумывать их — расхождение с `validate_typed_fields`.
- Roadmap в bundle ОТСУТСТВУЕТ (негативный пин тестом).
- Ветка: `feat/config-suggestions` (этот план — её первый коммит; отдельного plan-PR нет). Прямые коммиты в master запрещены.

---

### Task 1: bundle-билдер (DESIGN-901)

**Files:**
- Create: `dispatcher/core/suggest_bundle.py`
- Test: `tests/test_suggest_bundle.py` (create)

**Interfaces:**
- Consumes: `ProjectSpecRunnerConfig`, `TYPED_DEFAULTS` (`core/spec_runner_config.py`), `mask_secrets` (`core/collectors/base.py`), `ProjectSnapshot.description/description_source`.
- Produces: `build_suggest_bundle(cfg: ProjectSpecRunnerConfig, peers: list[ProjectSpecRunnerConfig], snapshot: ProjectSnapshot | None) -> dict[str, Any]`; константы `INSTRUCTION: str`, `PROMPT_VERSION: str`, `PEERS_PROJECT_CAP = 15`, `PEERS_VALUE_TOP = 5`. Tasks 2–3 зависят от них.

- [ ] **Step 1: Write the failing tests**

`tests/test_suggest_bundle.py`:

```python
"""DESIGN-901: deterministic, redacted suggestion bundle."""

from typing import Any

from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.spec_runner_config import (
    TYPED_DEFAULTS,
    ProjectSpecRunnerConfig,
    TypedField,
)
from dispatcher.core.suggest_bundle import (
    INSTRUCTION,
    PROMPT_VERSION,
    build_suggest_bundle,
)


def _cfg(
    project: str, explicit: dict[str, Any] | None = None, mtime: float = 1000.0
) -> ProjectSpecRunnerConfig:
    explicit = explicit or {}
    typed = {
        name: TypedField(value=explicit.get(name, default), explicit=name in explicit)
        for name, default in TYPED_DEFAULTS.items()
    }
    return ProjectSpecRunnerConfig(
        project=project,
        project_yaml_path=f"/w/{project}/project.yaml",
        base_mtime=mtime,
        typed=typed,
        extra_executor_config={},
        extra_explicit=False,
    )


def test_bundle_shape_and_requested_fields() -> None:
    cfg = _cfg("steward", {"max_retries": 5})
    bundle = build_suggest_bundle(cfg, peers=[], snapshot=None)
    assert bundle["instruction"] == INSTRUCTION
    assert bundle["prompt_version"] == PROMPT_VERSION
    # explicit field is NOT requested; every default-provenance field is
    assert "max_retries" not in bundle["requested_fields"]
    assert "claude_model" in bundle["requested_fields"]
    assert bundle["field_schema"]["max_retries"] == {"type": "int", "default": 3}
    assert bundle["field_schema"]["auto_commit"] == {"type": "bool", "default": True}
    assert bundle["current_config"]["max_retries"] == {"value": 5, "explicit": True}
    assert bundle["project"] == {"name": "steward"}


def test_bundle_has_no_roadmap_or_extra() -> None:
    bundle = build_suggest_bundle(_cfg("steward"), peers=[], snapshot=None)
    assert "roadmap" not in bundle  # negative pin — cut by design (§3)
    assert "extra_executor_config" not in bundle
    for key in bundle:
        assert key in {
            "instruction",
            "prompt_version",
            "requested_fields",
            "field_schema",
            "current_config",
            "peers",
            "project",
        }


def test_bundle_description_from_snapshot() -> None:
    snap = ProjectSnapshot(
        name="Maestro",
        path="/w/steward",
        description="Steward governs specs.",
        description_source="readme",
    )
    bundle = build_suggest_bundle(_cfg("steward"), peers=[], snapshot=snap)
    assert bundle["project"] == {
        "name": "steward",
        "description": "Steward governs specs.",
        "description_source": "readme",
    }


def test_peers_distribution_list_top5_other_and_explicit_count() -> None:
    peers = (
        [_cfg(f"p{i}", {"max_retries": 5}) for i in range(3)]  # 3x explicit 5
        + [_cfg(f"q{i}") for i in range(2)]  # 2x default 3
        + [
            _cfg("r0", {"max_retries": 7}),
            _cfg("r1", {"max_retries": 8}),
            _cfg("r2", {"max_retries": 9}),
            _cfg("r3", {"max_retries": 10}),
            _cfg("r4", {"max_retries": 11}),
        ]
    )
    bundle = build_suggest_bundle(_cfg("steward"), peers=peers, snapshot=None)
    dist = bundle["peers"]["max_retries"]
    assert dist[0] == {"value": 5, "count": 3, "explicit_count": 3}
    assert dist[1] == {"value": 3, "count": 2, "explicit_count": 0}
    # list encoding (not an object): bool/int values stay typed
    assert isinstance(dist, list) and isinstance(dist[0]["value"], int)
    # top-5 entries + {"other": N} tail for the 5 distinct singletons - 3 kept
    values = [d["value"] for d in dist if "value" in d]
    assert len(values) == 5
    assert dist[-1] == {"other": 2}


def test_peers_cap_top15_by_base_mtime() -> None:
    # 5 oldest carry a marker value: cap must drop exactly them
    old = [_cfg(f"o{i}", {"max_retries": 99}, mtime=float(i)) for i in range(5)]
    fresh = [
        _cfg(f"f{i}", {"max_retries": 5}, mtime=float(100 + i)) for i in range(15)
    ]
    bundle = build_suggest_bundle(_cfg("steward"), peers=old + fresh, snapshot=None)
    (row,) = bundle["peers"]["max_retries"]
    assert row == {"value": 5, "count": 15, "explicit_count": 15}  # no 99 row


def test_bundle_masks_secrets_in_values_and_description() -> None:
    cfg = _cfg("steward", {"claude_command": "run --key sk-abcdef123456"})
    snap = ProjectSnapshot(
        name="Maestro",
        path="/w/steward",
        description="Uses token ghp_abcdef123456 internally.",
        description_source="readme",
    )
    peers = [_cfg("p0", {"test_command": "curl https://u:pass@host/x"})]
    bundle = build_suggest_bundle(cfg, peers=peers, snapshot=snap)
    assert "sk-abcdef123456" not in str(bundle)
    assert "ghp_abcdef123456" not in str(bundle)
    assert "u:pass@" not in str(bundle)


def test_bundle_deterministic() -> None:
    peers = [_cfg("b"), _cfg("a")]
    one = build_suggest_bundle(_cfg("s"), peers=peers, snapshot=None)
    two = build_suggest_bundle(_cfg("s"), peers=list(reversed(peers)), snapshot=None)
    assert one == two
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_suggest_bundle.py -q`
Expected: FAIL — `ModuleNotFoundError: dispatcher.core.suggest_bundle`.

- [ ] **Step 3: Implement `dispatcher/core/suggest_bundle.py`**

```python
"""Suggestion context bundle (DESIGN-901).

Deterministic and redacted: every string value passes mask_secrets before
serialization (H-4) — foreign config content flows into the model prompt
(H-5), but foreign SECRETS must not. Roadmap is deliberately absent: no
causal chain from roadmap signals to any typed-field value (spec §3).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from dispatcher.core.collectors.base import mask_secrets
from dispatcher.core.models import ProjectSnapshot
from dispatcher.core.spec_runner_config import (
    TYPED_DEFAULTS,
    ProjectSpecRunnerConfig,
)

PROMPT_VERSION = "v1"
PEERS_PROJECT_CAP = 15
PEERS_VALUE_TOP = 5

INSTRUCTION = (
    "You are suggesting values for a Maestro spec_runner config. Reply "
    "with EXACTLY one JSON object, no prose, no markdown fence: "
    '{"suggestions": {"<field>": {"value": <typed value>, "rationale": '
    '"<one sentence>"}}}. Suggest ONLY fields listed in requested_fields. '
    "field_schema gives each field's type and default. peers gives value "
    "distributions across neighbour projects as lists of "
    "{value, count, explicit_count}; explicit_count is how many are "
    "deliberate human choices — the rest are copied defaults, so majority "
    "alone is not correctness. Ground each rationale in the distribution "
    "(e.g. '3 of 8 peers set this explicitly') and the project "
    "description. Omit a field rather than guess."
)


def build_suggest_bundle(
    cfg: ProjectSpecRunnerConfig,
    peers: list[ProjectSpecRunnerConfig],
    snapshot: ProjectSnapshot | None,
) -> dict[str, Any]:
    """Assemble the stdin document for the suggest CLI call (spec §3)."""
    selected = _select_peers(peers)
    project: dict[str, Any] = {"name": cfg.project}
    if snapshot is not None and snapshot.description:
        project["description"] = mask_secrets(snapshot.description)
        project["description_source"] = snapshot.description_source
    return {
        "instruction": INSTRUCTION,
        "prompt_version": PROMPT_VERSION,
        "requested_fields": [
            name for name in TYPED_DEFAULTS if not cfg.typed[name].explicit
        ],
        "field_schema": {
            name: {"type": type(default).__name__, "default": default}
            for name, default in TYPED_DEFAULTS.items()
        },
        "current_config": {
            name: {
                "value": mask_secrets(field.value, key=name),
                "explicit": field.explicit,
            }
            for name, field in cfg.typed.items()
        },
        "peers": {
            name: _distribution(selected, name) for name in TYPED_DEFAULTS
        },
        "project": project,
    }


def _select_peers(
    peers: list[ProjectSpecRunnerConfig],
) -> list[ProjectSpecRunnerConfig]:
    """All when <= cap, else freshest by base_mtime (observable rule)."""
    if len(peers) <= PEERS_PROJECT_CAP:
        return peers
    return sorted(peers, key=lambda p: p.base_mtime, reverse=True)[
        :PEERS_PROJECT_CAP
    ]


def _distribution(
    peers: list[ProjectSpecRunnerConfig], name: str
) -> list[dict[str, Any]]:
    """Top-N value rows + {"other": N} tail; list-encoded so bool/int
    values stay typed (JSON object keys would stringify them)."""
    counts: Counter[Any] = Counter()
    explicit: Counter[Any] = Counter()
    for peer in peers:
        field = peer.typed[name]
        value = mask_secrets(field.value, key=name)
        counts[value] += 1
        if field.explicit:
            explicit[value] += 1
    # deterministic: count desc, then stable repr order
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], repr(kv[0])))
    rows: list[dict[str, Any]] = [
        {"value": value, "count": count, "explicit_count": explicit[value]}
        for value, count in ranked[:PEERS_VALUE_TOP]
    ]
    other = sum(count for _, count in ranked[PEERS_VALUE_TOP:])
    if other:
        rows.append({"other": other})
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_suggest_bundle.py -q`
Expected: PASS.

- [ ] **Step 5: Full gates, then commit**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/suggest_bundle.py tests/test_suggest_bundle.py
git commit -m "feat: suggestion bundle builder — redacted, deterministic, roadmap-free (DESIGN-901)"
```

---

### Task 2: CLI-адаптер + config-ключ (DESIGN-902)

**Files:**
- Create: `dispatcher/core/suggest_cli.py`
- Modify: `dispatcher/core/discovery.py` (поле `suggest_claude_cli` в `DispatcherConfig` + чтение в `load_config`)
- Test: `tests/test_suggest_cli.py` (create), `tests/test_discovery.py` (добавить)

**Interfaces:**
- Consumes: `build_suggest_bundle`-словарь (Task 1); `validate_typed_fields`, `TYPED_DEFAULTS`.
- Produces:
  - `DispatcherConfig.suggest_claude_cli: Path | None = None`;
  - `class Suggestion(BaseModel): value: Any; rationale: str`;
  - `class SuggestOutcome(BaseModel): suggestions: dict[str, Suggestion]; dropped: list[str]; cli_version: str | None; duration_s: float; cost_usd: float | None`;
  - исключения `SuggestUnavailableError`, `SuggestRunnerBusyError` (несёт `.project`), `SuggestTimeoutError`, `SuggestCancelledError`, `SuggestInvalidError`;
  - `class SuggestRunner: __init__(config, *, command: tuple[str, ...] | None = None)`, `run(project: str, bundle: dict, requested: set[str]) -> SuggestOutcome`, `cancel(project: str) -> bool` (True = был in-flight этого проекта и убит; False = нечего отменять; чужой in-flight → `SuggestRunnerBusyError`);
  - константа `SUGGEST_TIMEOUT_S = 60.0`.

- [ ] **Step 1: Write the failing tests**

`tests/test_suggest_cli.py` — fake-CLI бинарь по прецеденту `fake_checker` из `tests/test_spec_runner_config_actions.py:49` (python-скрипт, печатающий заданный конверт; command-инъекция через параметр конструктора):

```python
"""DESIGN-902: CLI adapter — envelope parsing, partial acceptance, cancel."""

import json
import threading
import time
from pathlib import Path

import pytest

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.suggest_cli import (
    SuggestInvalidError,
    SuggestRunner,
    SuggestRunnerBusyError,
    SuggestTimeoutError,
    SuggestUnavailableError,
)

_BUNDLE = {"instruction": "x", "requested_fields": ["claude_model"]}


def _fake_cli(tmp_path: Path, envelope: dict, sleep_s: float = 0.0) -> tuple[str, ...]:
    """A stand-in claude binary: reads stdin, prints the given envelope."""
    script = tmp_path / "fake_claude.py"
    script.write_text(
        "import json, sys, time\n"
        "_ = sys.stdin.read()\n"
        f"time.sleep({sleep_s})\n"
        f"print(json.dumps({envelope!r}))\n"
    )
    return ("python3", str(script))


def _envelope(result_payload: dict, **extra: object) -> dict:
    return {"type": "result", "result": json.dumps(result_payload), **extra}


def _config(tmp_path: Path) -> DispatcherConfig:
    return DispatcherConfig(roots=(tmp_path,))


def test_happy_path_with_cost_and_partial_drop(tmp_path: Path) -> None:
    envelope = _envelope(
        {
            "suggestions": {
                "claude_model": {"value": "sonnet", "rationale": "peers use it"},
                "max_retries": {"value": 9, "rationale": "not requested"},
                "auto_commit": {"value": "yes", "rationale": "wrong type"},
                "review_model": {"value": "", "rationale": "equals default"},
            }
        },
        total_cost_usd=0.04,
    )
    runner = SuggestRunner(_config(tmp_path), command=_fake_cli(tmp_path, envelope))
    outcome = runner.run("steward", _BUNDLE, requested={"claude_model", "auto_commit", "review_model"})
    assert outcome.suggestions["claude_model"].value == "sonnet"
    assert sorted(outcome.dropped) == ["auto_commit", "max_retries", "review_model"]
    assert outcome.cost_usd == 0.04
    assert outcome.duration_s >= 0


def test_missing_cost_is_none(tmp_path: Path) -> None:
    envelope = _envelope({"suggestions": {}})
    runner = SuggestRunner(_config(tmp_path), command=_fake_cli(tmp_path, envelope))
    outcome = runner.run("steward", _BUNDLE, requested=set())
    assert outcome.cost_usd is None and outcome.suggestions == {}


def test_result_not_json_is_invalid(tmp_path: Path) -> None:
    envelope = {"type": "result", "result": "I think you should…"}
    runner = SuggestRunner(_config(tmp_path), command=_fake_cli(tmp_path, envelope))
    with pytest.raises(SuggestInvalidError):
        runner.run("steward", _BUNDLE, requested=set())


def test_stdout_not_json_is_invalid(tmp_path: Path) -> None:
    script = tmp_path / "fake_claude.py"
    script.write_text("import sys\n_ = sys.stdin.read()\nprint('plain text')\n")
    runner = SuggestRunner(_config(tmp_path), command=("python3", str(script)))
    with pytest.raises(SuggestInvalidError):
        runner.run("steward", _BUNDLE, requested=set())


def test_binary_missing_is_unavailable(tmp_path: Path) -> None:
    runner = SuggestRunner(_config(tmp_path), command=(str(tmp_path / "nope"),))
    with pytest.raises(SuggestUnavailableError):
        runner.run("steward", _BUNDLE, requested=set())


def test_unconfigured_and_bad_basename(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(SuggestUnavailableError):
        SuggestRunner(_config(tmp_path)).run("steward", _BUNDLE, requested=set())
    bad = DispatcherConfig(
        roots=(tmp_path,), suggest_claude_cli=tmp_path / "not-claude"
    )
    with pytest.raises(SuggestUnavailableError):
        SuggestRunner(bad).run("steward", _BUNDLE, requested=set())


def test_timeout_terminates_and_frees_lock(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("dispatcher.core.suggest_cli.SUGGEST_TIMEOUT_S", 0.2)
    slow = _fake_cli(tmp_path, _envelope({"suggestions": {}}), sleep_s=30)
    runner = SuggestRunner(_config(tmp_path), command=slow)
    with pytest.raises(SuggestTimeoutError):
        runner.run("steward", _BUNDLE, requested=set())
    assert runner.current_project is None  # SAME runner's lock freed


def test_cancel_frees_lock_immediately(tmp_path: Path) -> None:
    slow = _fake_cli(tmp_path, _envelope({"suggestions": {}}), sleep_s=30)
    runner = SuggestRunner(_config(tmp_path), command=slow)
    errors: list[Exception] = []

    def _run() -> None:
        try:
            runner.run("steward", _BUNDLE, requested=set())
        except Exception as err:  # noqa: BLE001 — collected for assertion
            errors.append(err)

    thread = threading.Thread(target=_run)
    thread.start()
    for _ in range(100):  # wait until in-flight
        if runner.current_project == "steward":
            break
        time.sleep(0.05)
    assert runner.cancel("steward") is True
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors and type(errors[0]).__name__ == "SuggestCancelledError"
    assert runner.current_project is None  # lock freed immediately


def test_cancel_other_project_raises_busy_with_name(tmp_path: Path) -> None:
    slow = _fake_cli(tmp_path, _envelope({"suggestions": {}}), sleep_s=30)
    runner = SuggestRunner(_config(tmp_path), command=slow)
    thread = threading.Thread(
        target=lambda: pytest.raises(Exception, runner.run, "steward", _BUNDLE, set())
    )
    thread.start()
    for _ in range(100):
        if runner.current_project == "steward":
            break
        time.sleep(0.05)
    with pytest.raises(SuggestRunnerBusyError) as exc:
        runner.cancel("other")
    assert exc.value.project == "steward"
    runner.cancel("steward")
    thread.join(timeout=10)


def test_cancel_idle_returns_false(tmp_path: Path) -> None:
    runner = SuggestRunner(_config(tmp_path), command=("python3", "-c", "pass"))
    assert runner.cancel("steward") is False


def test_busy_second_run_raises(tmp_path: Path) -> None:
    slow = _fake_cli(tmp_path, _envelope({"suggestions": {}}), sleep_s=30)
    runner = SuggestRunner(_config(tmp_path), command=slow)
    thread = threading.Thread(
        target=lambda: pytest.raises(Exception, runner.run, "steward", _BUNDLE, set())
    )
    thread.start()
    for _ in range(100):
        if runner.current_project == "steward":
            break
        time.sleep(0.05)
    with pytest.raises(SuggestRunnerBusyError):
        runner.run("other", _BUNDLE, requested=set())
    runner.cancel("steward")
    thread.join(timeout=10)
```

В `tests/test_discovery.py` добавить (следуя стилю существующих load_config-тестов):

```python
def test_load_config_suggest_claude_cli(tmp_path: Path) -> None:
    cfg_file = tmp_path / "dispatcher.toml"
    cfg_file.write_text('suggest_claude_cli = "/opt/bin/claude"\n')
    cfg = load_config(cfg_file)
    assert cfg.suggest_claude_cli == Path("/opt/bin/claude")
    cfg_file.write_text("")
    assert load_config(cfg_file).suggest_claude_cli is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_suggest_cli.py tests/test_discovery.py -q`
Expected: FAIL — `ModuleNotFoundError` / `TypeError: unexpected keyword 'suggest_claude_cli'`.

- [ ] **Step 3: Implement**

`dispatcher/core/discovery.py`: в `DispatcherConfig` после `tracking_file`:

```python
    # Optional ABSOLUTE path to the claude binary for config suggestions
    # (DESIGN-902). Distinct from spec_runner.claude_command in project.yaml
    # (that configures spec-runner; this configures dispatcher itself).
    suggest_claude_cli: Path | None = None
```

В `load_config`, перед `return`:

```python
    raw_suggest = data.get("suggest_claude_cli")
    suggest_claude_cli = Path(raw_suggest).expanduser() if raw_suggest else None
```

и `suggest_claude_cli=suggest_claude_cli,` в конструкторе.

`dispatcher/core/suggest_cli.py`:

```python
"""CLI adapter for config suggestions (DESIGN-902).

ALL claude-CLI specifics (envelope shape, `.result` extraction, version/
cost keys) live here and never leak upward (H-3) — swapping spawn for a
sidecar HTTP call replaces this module's internals only. Hardening: argv
is built from an allowlisted binary plus code-fixed flags, the bundle
travels via stdin only, shell=False (H-1). Cancel terminates OUR child
process and frees the lock immediately (H-6).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from typing import Any

from pydantic import BaseModel

from dispatcher.core.discovery import DispatcherConfig
from dispatcher.core.spec_runner_config import TYPED_DEFAULTS
from dispatcher.core.spec_runner_config_schema import validate_typed_fields

SUGGEST_TIMEOUT_S = 60.0
_FIXED_FLAGS = ("-p", "--output-format", "json")


class SuggestUnavailableError(Exception):
    """CLI not configured / not found — the feature degrades honestly."""


class SuggestRunnerBusyError(Exception):
    """One in-flight suggest per process; carries the busy project."""

    def __init__(self, project: str) -> None:
        self.project = project
        super().__init__(f"suggest in flight for {project}")


class SuggestTimeoutError(Exception):
    """CLI exceeded SUGGEST_TIMEOUT_S; the process was terminated."""


class SuggestCancelledError(Exception):
    """A cancel endpoint terminated this run."""


class SuggestInvalidError(Exception):
    """Envelope or `.result` payload unparseable — loud, not silent."""


class Suggestion(BaseModel):
    """One accepted suggestion for one typed field."""

    value: Any
    rationale: str


class SuggestOutcome(BaseModel):
    """Adapter output; no envelope details cross this boundary (H-3)."""

    suggestions: dict[str, Suggestion]
    dropped: list[str]
    cli_version: str | None = None
    duration_s: float
    cost_usd: float | None = None


class SuggestRunner:
    """Serialized executor of suggest CLI calls (pattern: ActionRunner)."""

    def __init__(
        self,
        config: DispatcherConfig,
        *,
        command: tuple[str, ...] | None = None,
    ) -> None:
        self._config = config
        self._command = command
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._current: str | None = None
        self._cancelled = False

    @property
    def current_project(self) -> str | None:
        return self._current

    def _argv(self) -> tuple[str, ...]:
        if self._command is not None:  # test injection, mirrors fake_checker
            return self._command
        configured = self._config.suggest_claude_cli
        if configured is not None:
            # allowlist: absolute path whose basename MUST be `claude` (H-1)
            if not configured.is_absolute() or configured.name != "claude":
                raise SuggestUnavailableError(
                    f"suggest_claude_cli must be an absolute path to a "
                    f"'claude' binary, got: {configured}"
                )
            if not configured.is_file():
                raise SuggestUnavailableError(f"not found: {configured}")
            return (str(configured),)
        found = shutil.which("claude")
        if found is None:
            raise SuggestUnavailableError("claude CLI not found on PATH")
        return (found,)

    def run(
        self, project: str, bundle: dict[str, Any], requested: set[str]
    ) -> SuggestOutcome:
        """One CLI call: spawn, parse envelope, filter suggestions."""
        argv = (*self._argv(), *_FIXED_FLAGS)
        with self._lock:
            if self._current is not None:
                raise SuggestRunnerBusyError(self._current)
            self._current = project
            self._cancelled = False
            started = time.monotonic()
            try:
                self._proc = subprocess.Popen(  # noqa: S603 — allowlisted argv
                    argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            except OSError as err:
                self._current = None
                raise SuggestUnavailableError(str(err)) from err
        # communicate OUTSIDE the lock: cancel() needs the lock to terminate
        proc = self._proc
        try:
            stdout, _ = proc.communicate(
                input=json.dumps(bundle, sort_keys=True),
                timeout=SUGGEST_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired as err:
            proc.terminate()
            proc.wait(timeout=5)
            raise SuggestTimeoutError("suggest timed out") from err
        finally:
            with self._lock:
                cancelled = self._cancelled
                self._proc = None
                self._current = None
        if cancelled:
            raise SuggestCancelledError("cancelled")
        duration = time.monotonic() - started
        return self._parse(stdout, requested, duration)

    def cancel(self, project: str) -> bool:
        """Terminate THIS project's in-flight run; True if one was killed."""
        with self._lock:
            if self._current is None:
                return False
            if self._current != project:
                raise SuggestRunnerBusyError(self._current)
            self._cancelled = True
            if self._proc is not None:
                self._proc.terminate()
            return True

    def _parse(
        self, stdout: str, requested: set[str], duration: float
    ) -> SuggestOutcome:
        try:
            envelope = json.loads(stdout)
            payload = json.loads(envelope["result"])
            raw = payload["suggestions"]
            if not isinstance(raw, dict):
                raise TypeError("suggestions is not an object")
        except (json.JSONDecodeError, KeyError, TypeError) as err:
            raise SuggestInvalidError(f"suggestion invalid: {err}") from err
        suggestions: dict[str, Suggestion] = {}
        dropped: list[str] = []
        for name, entry in raw.items():
            value = entry.get("value") if isinstance(entry, dict) else None
            if (
                name not in requested
                or not isinstance(entry, dict)
                or validate_typed_fields({name: value})
                or value == TYPED_DEFAULTS.get(name)
            ):
                dropped.append(name)
                continue
            suggestions[name] = Suggestion(
                value=value, rationale=str(entry.get("rationale", ""))
            )
        cost = envelope.get("total_cost_usd", envelope.get("cost_usd"))
        return SuggestOutcome(
            suggestions=suggestions,
            dropped=sorted(dropped),
            cli_version=envelope.get("version"),
            duration_s=round(duration, 3),
            cost_usd=cost if isinstance(cost, (int, float)) else None,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_suggest_cli.py tests/test_discovery.py -q`
Expected: PASS.

- [ ] **Step 5: Full gates, then commit**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git add dispatcher/core/suggest_cli.py dispatcher/core/discovery.py tests/test_suggest_cli.py tests/test_discovery.py
git commit -m "feat: suggest CLI adapter — envelope isolation, cancel, partial acceptance (DESIGN-902)"
```

---

### Task 3: endpoints + audit (DESIGN-903)

**Files:**
- Modify: `dispatcher/server/app.py` (два роута рядом с `spec_runner_config_view`, ~строка 272; модели рядом с `UpdateSpecRunnerConfigRequest`)
- Test: `tests/test_api.py` (добавить)

**Interfaces:**
- Consumes: `SuggestRunner` (Task 2), `build_suggest_bundle` (Task 1), `discover_project_configs`, `SnapshotService` (`cache`), логгер `dispatcher.actions.spec_runner_config`.
- Produces: `POST /api/projects/{name}/spec-runner-config/suggest` (body `{"base_mtime": float}`, ответ — `SuggestOutcome` с `response_model_exclude={"cli_version"}`: `{suggestions, dropped, duration_s, cost_usd|null}`); `POST /api/projects/{name}/spec-runner-config/suggest/cancel` (ответ `{"cancelled": bool}`). Task 4 зовёт оба.

- [ ] **Step 1: Write the failing tests**

В `tests/test_api.py` (стиль файла; fake-CLI как в Task 2 — фабрику `_fake_cli`/`_envelope` продублировать локально, тест-модули независимы):

```python
def _suggest_workspace(tmp_path: Path) -> None:
    steward = tmp_path / "steward"
    steward.mkdir()
    (steward / "project.yaml").write_text(
        "project: steward\nspec_runner:\n  max_retries: 5\nworkstreams: []\n"
    )


async def _token(client) -> str:
    return (await client.get("/api/actions/session")).json()["token"]


async def test_suggest_endpoint_happy_and_errors(tmp_path: Path) -> None:
    _suggest_workspace(tmp_path)
    envelope = _envelope(
        {"suggestions": {"claude_model": {"value": "sonnet", "rationale": "r"}}},
        total_cost_usd=0.02,
    )
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(
        config, suggest_runner=SuggestRunner(config, command=_fake_cli(tmp_path, envelope))
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        token = await _token(client)
        mtime = (tmp_path / "steward" / "project.yaml").stat().st_mtime

        # 403 without token
        resp = await client.post(
            "/api/projects/steward/spec-runner-config/suggest",
            json={"base_mtime": mtime},
        )
        assert resp.status_code == 403

        # 200 happy path
        resp = await client.post(
            "/api/projects/steward/spec-runner-config/suggest",
            json={"base_mtime": mtime},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["suggestions"]["claude_model"]["value"] == "sonnet"
        assert body["cost_usd"] == 0.02

        # 409 stale base_mtime
        resp = await client.post(
            "/api/projects/steward/spec-runner-config/suggest",
            json={"base_mtime": mtime - 10},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 409
        assert "config changed" in resp.json()["detail"]

        # 404 unknown project
        resp = await client.post(
            "/api/projects/nope/spec-runner-config/suggest",
            json={"base_mtime": 1.0},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 404

        # cancel with nothing in flight: idempotent 200 false
        resp = await client.post(
            "/api/projects/steward/spec-runner-config/suggest/cancel",
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 200 and resp.json() == {"cancelled": False}


async def test_suggest_unavailable_is_503(tmp_path: Path) -> None:
    _suggest_workspace(tmp_path)
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(
        config,
        suggest_runner=SuggestRunner(config, command=(str(tmp_path / "missing"),)),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        token = await _token(client)
        mtime = (tmp_path / "steward" / "project.yaml").stat().st_mtime
        resp = await client.post(
            "/api/projects/steward/spec-runner-config/suggest",
            json={"base_mtime": mtime},
            headers={"X-Action-Token": token},
        )
        assert resp.status_code == 503


async def test_suggest_invalid_is_422_and_audited(tmp_path: Path, caplog) -> None:
    _suggest_workspace(tmp_path)
    envelope = {"type": "result", "result": "not json"}
    config = DispatcherConfig(roots=(tmp_path,))
    app = create_app(
        config, suggest_runner=SuggestRunner(config, command=_fake_cli(tmp_path, envelope))
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        token = await _token(client)
        mtime = (tmp_path / "steward" / "project.yaml").stat().st_mtime
        with caplog.at_level("INFO", logger="dispatcher.actions.spec_runner_config"):
            resp = await client.post(
                "/api/projects/steward/spec-runner-config/suggest",
                json={"base_mtime": mtime},
                headers={"X-Action-Token": token},
            )
        assert resp.status_code == 422
        assert any(
            "action=suggest" in r.message and "outcome=invalid" in r.message
            for r in caplog.records
        )
```

Плюс happy-path audit-пин в первом тесте (caplog): строка содержит `action=suggest project=steward outcome=ok` и `cost=0.02`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api.py -q`
Expected: FAIL — `create_app` не принимает `suggest_runner` / 404 на новых роутах.

- [ ] **Step 3: Implement in `dispatcher/server/app.py`**

Импорты:

```python
from dispatcher.core.suggest_bundle import build_suggest_bundle
from dispatcher.core.suggest_cli import (
    SuggestCancelledError,
    SuggestInvalidError,
    SuggestOutcome,
    SuggestRunner,
    SuggestRunnerBusyError,
    SuggestTimeoutError,
    SuggestUnavailableError,
)
```

Модели (рядом с `UpdateSpecRunnerConfigRequest`):

```python
class SuggestRequest(BaseModel):
    """POST .../suggest body: mtime of the config the form was built from."""

    base_mtime: float


class CancelResponse(BaseModel):
    """POST .../suggest/cancel result."""

    cancelled: bool
```

`create_app` получает keyword-only `suggest_runner: SuggestRunner | None = None` (is-None паттерн как у сервисов):

```python
    suggest = suggest_runner if suggest_runner is not None else SuggestRunner(config)
    _suggest_audit = logging.getLogger("dispatcher.actions.spec_runner_config")
```

(`import logging` уже есть или добавить). Роуты после `spec_runner_config_view`:

```python
    @app.post(
        "/api/projects/{name}/spec-runner-config/suggest",
        response_model=SuggestOutcome,
        response_model_exclude={"cli_version"},
    )
    def spec_runner_config_suggest(
        name: str,
        request: SuggestRequest,
        x_action_token: str | None = Header(default=None),
    ) -> SuggestOutcome:
        """Явный клик человека: CLI-вызов ТРАТИТ ДЕНЬГИ — токен обязателен."""
        if x_action_token != action_token:
            raise HTTPException(status_code=403, detail="bad or missing action token")
        configs, _ = discover_project_configs(config.roots)
        target = next(
            (c for c in configs if Path(c.project_yaml_path).parent.name == name),
            None,
        )
        if target is None:
            raise HTTPException(status_code=404, detail=f"no project.yaml for: {name}")
        if target.base_mtime != request.base_mtime:
            raise HTTPException(
                status_code=409, detail="config changed — reload the form"
            )
        peers = [c for c in configs if c is not target]
        snapshots, _w = cache.get()
        target_dir = str(Path(target.project_yaml_path).parent)
        snap = next((s for s in snapshots if s.path == target_dir), None)
        bundle = build_suggest_bundle(target, peers, snap)
        requested = set(bundle["requested_fields"])
        try:
            outcome = suggest.run(name, bundle, requested)
        except SuggestUnavailableError as err:
            _suggest_audit.info("action=suggest project=%s outcome=unavailable", name)
            raise HTTPException(status_code=503, detail=str(err)) from err
        except SuggestRunnerBusyError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        except SuggestTimeoutError as err:
            _suggest_audit.info("action=suggest project=%s outcome=timeout", name)
            raise HTTPException(status_code=409, detail=str(err)) from err
        except SuggestCancelledError as err:
            _suggest_audit.info("action=suggest project=%s outcome=cancelled", name)
            raise HTTPException(status_code=409, detail="cancelled") from err
        except SuggestInvalidError as err:
            _suggest_audit.info("action=suggest project=%s outcome=invalid", name)
            raise HTTPException(status_code=422, detail=str(err)) from err
        _suggest_audit.info(
            "action=suggest project=%s outcome=ok duration=%.1fs fields=%s "
            "dropped=%s cost=%s cli=%s",
            name,
            outcome.duration_s,
            sorted(outcome.suggestions),
            outcome.dropped,
            outcome.cost_usd,
            outcome.cli_version,
        )
        return outcome

    @app.post(
        "/api/projects/{name}/spec-runner-config/suggest/cancel",
        response_model=CancelResponse,
    )
    def spec_runner_config_suggest_cancel(
        name: str,
        x_action_token: str | None = Header(default=None),
    ) -> CancelResponse:
        if x_action_token != action_token:
            raise HTTPException(status_code=403, detail="bad or missing action token")
        try:
            return CancelResponse(cancelled=suggest.cancel(name))
        except SuggestRunnerBusyError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api.py -q`
Expected: PASS.

- [ ] **Step 5: Full gates, then commit**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git add dispatcher/server/app.py tests/test_api.py
git commit -m "feat: suggest + cancel endpoints with audit (DESIGN-903)"
```

---

### Task 4: web UX (DESIGN-904)

**Files:**
- Modify: `dispatcher/server/static/index.html` (панель `spec-runner-config`; `renderSpecRunnerConfigForm` ~строка 440)
- Test: `tests/test_api.py` (static-пины в index-тесте)

**Interfaces:**
- Consumes: оба эндпоинта Task 3; существующие `esc()`, токен-механику сабмита конфига (найти в файле, как update-flow получает `X-Action-Token`, и использовать тот же путь); `currentSpecRunnerConfig.base_mtime` (уже в объекте конфига).

- [ ] **Step 1: Write the failing test**

В индекс-тест `tests/test_api.py` добавить:

```python
    assert "spec-runner-config-suggest" in resp.text
    assert "spec-runner-config-suggest-cancel" in resp.text
    assert "suggest-marker" in resp.text
    assert "suggest-dropped" in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api.py -q` → FAIL на новых пинах.

- [ ] **Step 3: Implement**

HTML в панели `spec-runner-config` (рядом с submit-кнопкой):

```html
<button id="spec-runner-config-suggest" type="button" disabled>Suggest values</button>
<button id="spec-runner-config-suggest-cancel" type="button" hidden>Cancel</button>
<div id="suggest-dropped" class="fresh"></div>
```

CSS:

```css
  .suggest-marker { color: var(--accent); font-size: 11px; margin-left: 4px; }
  .suggest-rationale { color: var(--dim); font-size: 11px; display: block; }
```

JS (рядом с конфиг-обвязкой; `renderSpecRunnerConfigForm` дополнительно включает suggest-кнопку — `document.getElementById("spec-runner-config-suggest").disabled = false;`):

```js
let suggestTimer = null;

function suggestSetBusy(busy, elapsed) {
  const btn = document.getElementById("spec-runner-config-suggest");
  const cancel = document.getElementById("spec-runner-config-suggest-cancel");
  btn.disabled = busy;
  btn.textContent = busy ? `Suggesting… ${elapsed}s` : "Suggest values";
  cancel.hidden = !busy;
}

async function suggestValues() {
  const cfg = currentSpecRunnerConfig;
  if (!cfg) return;
  const out = document.getElementById("suggest-dropped");
  out.textContent = "";
  let elapsed = 0;
  suggestSetBusy(true, 0);
  suggestTimer = setInterval(() => suggestSetBusy(true, ++elapsed), 1000);
  try {
    const resp = await fetch(
      "/api/projects/" + encodeURIComponent(cfg.repoDir) +
        "/spec-runner-config/suggest",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Action-Token": await actionToken(),
        },
        body: JSON.stringify({base_mtime: cfg.base_mtime}),
      });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail ?? `HTTP ${resp.status}`);
    applySuggestions(data);
  } catch (err) {
    out.textContent = "✗ " + err.message;
  } finally {
    clearInterval(suggestTimer);
    suggestSetBusy(false, 0);
  }
}

function applySuggestions(data) {
  const form = document.getElementById("spec-runner-config-form");
  for (const [field, s] of Object.entries(data.suggestions)) {
    const input = form.querySelector(`input[data-typed="${CSS.escape(field)}"]`);
    if (!input) continue;
    input.value = String(s.value);
    const label = input.closest("label");
    if (label && !label.querySelector(".suggest-marker")) {
      const marker = document.createElement("span");
      marker.className = "suggest-marker";
      marker.textContent = "(suggested)";
      const rationale = document.createElement("span");
      rationale.className = "suggest-rationale";
      rationale.textContent = s.rationale;
      label.append(marker, rationale);
    }
  }
  const out = document.getElementById("suggest-dropped");
  out.textContent = data.dropped.length
    ? `${data.dropped.length} dropped: ${data.dropped.join(", ")}` : "";
}

async function cancelSuggest() {
  const cfg = currentSpecRunnerConfig;
  if (!cfg) return;
  await fetch(
    "/api/projects/" + encodeURIComponent(cfg.repoDir) +
      "/spec-runner-config/suggest/cancel",
    {method: "POST", headers: {"X-Action-Token": await actionToken()}});
}

document.getElementById("spec-runner-config-suggest")
  .addEventListener("click", suggestValues);
document.getElementById("spec-runner-config-suggest-cancel")
  .addEventListener("click", cancelSuggest);
```

ВАЖНО: `actionToken()` — если в файле токен добывается иначе (переменная, другая функция) — использовать РОВНО существующий механизм сабмита update-конфига, не изобретать второй. `renderSpecRunnerConfigForm` при перерисовке формы сбрасывает и `suggest-dropped`, и маркеры (innerHTML формы перерисовывается — маркеры внутри label уйдут сами; проверить).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api.py -q` → PASS.

- [ ] **Step 5: Full gates, then commit**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git add dispatcher/server/static/index.html tests/test_api.py
git commit -m "feat: web suggest UX — elapsed, cancel, suggested markers, dropped note (DESIGN-904)"
```

---

### Task 5: документация (DESIGN-906)

**Files:**
- Modify: `README.md`, `COWORK_CONTEXT.md`

**Interfaces:** нет (docs-only).

- [ ] **Step 1: README**

Секция «AI suggestions» (после секции конфиг-редактора): требования (claude CLI на PATH или `suggest_claude_cli = "/abs/path/claude"` в dispatcher.toml — абсолютный путь, basename ровно `claude`; НЕ путать с typed-полем `spec_runner.claude_command`); честная фраза «секрет живёт в конфиге CLI на том же хосте — релоцирован, не устранён»; стоимость — на аккаунте пользователя; кнопка недоступна без CLI. API-список: два новых POST-эндпоинта одной строкой каждый.

- [ ] **Step 2: COWORK_CONTEXT.md**

Interfaces line: добавить `/api/projects/{name}/spec-runner-config/suggest[/cancel]`.

- [ ] **Step 3: Full gates, then commit**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git add README.md COWORK_CONTEXT.md
git commit -m "docs: AI suggestions section + endpoints (DESIGN-906)"
```

---

## Final whole-branch review mandate

- Гейты прогнать самому (ожидание: 288+N passed, warning-free).
- Живой прогон: scratch-workspace + fake-claude на PATH → uvicorn → suggest (200 с маркерами полей), stale-mtime 409, cancel-освобождение лока, 503 без CLI.
- H-4 проверить эмпирически: токен в фикстурном конфиге соседа → в stdin fake-CLI (записать его) токена НЕТ.
- Семантика частичного принятия — по тестам адаптера; roadmap-отсутствие — негативный пин.
- Web: маркеры/`dropped`/elapsed — grep + по возможности живой click-through.
