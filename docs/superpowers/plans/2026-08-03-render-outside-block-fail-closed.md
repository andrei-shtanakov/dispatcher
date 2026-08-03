# Fail-closed rendering of `project.yaml` — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `spec_runner:` config editor refuse to open a PR whenever
rendering would change any byte of a neighbour's `project.yaml` outside the
block it owns.

**Architecture:** Two independent layers. Style *preservation* makes the
renderer reproduce the source (BOM, line endings, `---`/`...`, indentation);
two *checks* then verify the result — Check A that an unmodified round-trip
equals the source byte for byte, Check B that an edited render differs only
inside the owned span. Neither check holds a list of allowed normalisations,
so every style heuristic is a guess made safe by Check A. The whole path
operates on `bytes` and is wrapped in one exception type whose message carries
no file content.

**Tech Stack:** Python 3.12/3.13, `ruamel.yaml` (round-trip mode), pytest,
FastAPI (the route under test), `uv` for all tooling.

**Spec:** `docs/superpowers/specs/2026-08-03-render-outside-block-fail-closed-design.md`
— read it first. It records *why* each piece exists, including the measured
inventory of what escapes the block today.

## Global Constraints

- All tooling through `uv`: `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format .`, `uv run pyrefly check`. Never `pip`.
- Line length 88 (ruff enforces). Type hints on every new function.
- **This repo forbids direct commits to `master`.** Work happens on
  `feat/render-outside-block-fail-closed`, which already exists and already
  holds the spec commits. Do not create another branch. Do not merge.
- **Never edit a neighbouring repo.** Everything here is inside `dispatcher/`.
- No diagnostic message, at any stage, may contain a source line, a scalar
  value, a candidate value, or a diff. Permitted: stage name, exception *class*
  name, byte/line coordinates, lengths, counts, and which side of the span
  diverged.
- Do not use `str(mark)` on a ruamel error mark, and never read a ruamel
  error's `.problem` attribute — both embed source text. Only `mark.line` and
  `mark.column` are safe.
- The full suite must stay green. Two live-smoke tests need real binaries:
  run `bash scripts/install_pinned_checker.sh` and
  `bash scripts/install_pinned_steward.sh` once, then prefix both printed `bin`
  directories onto `PATH`. Without them those two tests FAIL by design; that is
  not a regression you introduced.

---

## File Structure

| File | Responsibility |
|---|---|
| `dispatcher/core/spec_runner_config_actions.py` | All changes. Style inspection, rendering, both checks, the safe exception, and the runner. Already ~530 lines and cohesive — a split is not part of this slice. |
| `dispatcher/server/app.py` | One line: the route must not turn the new exception into a 500. |
| `tests/test_spec_runner_config_actions.py` | Unit tests for style, checks, span, and leak-safety. |
| `tests/test_api.py` | The HTTP-surface leak test. |
| `tests/fixtures/project_yaml_with_comments.yaml` | Existing; untouched. |
| `TODO.md` | Close `@id:render-outside-block-fail-closed`. |

---

## Task 1: Move the pipeline to bytes

**Files:**
- Modify: `dispatcher/core/spec_runner_config_actions.py` — rename
  `build_new_yaml_text` → `build_new_yaml_bytes`, change its signature, and fix
  `run()` at lines 445-455.
- Test: `tests/test_spec_runner_config_actions.py`

**Interfaces:**
- Produces: `build_new_yaml_bytes(base_bytes: bytes, candidate: ConfigCandidate) -> tuple[bytes, list[str], bool]`
- Produces: test helper `_render_text(text: str, candidate) -> tuple[str, list[str], bool]`
  so existing string-based tests stay readable.

**Why the rename:** the function no longer returns text. Every call site has to
change anyway because the parameter type changes, so keeping an inaccurate name
would buy nothing.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_spec_runner_config_actions.py`, after `fake_checker`:

```python
def test_the_bytes_that_were_rendered_are_the_bytes_propose_pr_receives(
    tmp_path: Path,
) -> None:
    """`Path.write_text` opens with newline=None and translates \\n to
    os.linesep, so a text-mode write can alter bytes after they were built.
    The temp file propose-pr reads must be byte-identical to what the
    renderer produced, on every platform."""
    from dispatcher.core.spec_runner_config_actions import build_new_yaml_bytes

    repo = make_project(tmp_path, "alpha")
    base_bytes = (repo / "project.yaml").read_bytes()
    candidate = _candidate(repo, max_retries=7)
    expected, _, _ = build_new_yaml_bytes(base_bytes, candidate)

    command, record = fake_checker(tmp_path, {"ok": True, "detail": "created"})
    runner = SpecRunnerConfigActionRunner(
        DispatcherConfig(roots=(tmp_path,)), command=command
    )
    runner.run("alpha", candidate)

    import json as _json

    written = bytes.fromhex(_json.loads(record.read_text())["edit_bytes_hex"])
    assert written == expected
```

The fake checker must record raw bytes. In `fake_checker`, replace the
`edit_content` capture block with one that records both:

```python
    script.write_text(
        "import json, sys\n"
        "argv = sys.argv[1:]\n"
        "edit_content = None\n"
        "edit_bytes_hex = None\n"
        "for a in argv:\n"
        "    if a.startswith('project.yaml='):\n"
        "        p = a.split('=', 1)[1]\n"
        "        try:\n"
        "            raw = open(p, 'rb').read()\n"
        "            edit_bytes_hex = raw.hex()\n"
        "            edit_content = raw.decode('utf-8')\n"
        "        except OSError:\n"
        "            pass\n"
        f"json.dump({{'argv': argv, 'edit_content': edit_content, "
        f"'edit_bytes_hex': edit_bytes_hex}}, "
        f"open({str(record)!r}, 'w'))\n"
        f"json.dump({payload!r}, sys.stdout)\n"
        f"sys.exit({returncode})\n"
    )
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_spec_runner_config_actions.py -k bytes_propose_pr -v`
Expected: FAIL with `ImportError: cannot import name 'build_new_yaml_bytes'`.

- [ ] **Step 3: Change the signature**

In `dispatcher/core/spec_runner_config_actions.py`, rename the function and
convert at its edges. Replace the `def` line and the first two body lines:

```python
def build_new_yaml_bytes(
    base_bytes: bytes, candidate: ConfigCandidate
) -> tuple[bytes, list[str], bool]:
```

Immediately after the docstring, decode; at the end, encode. The body between
is unchanged for now:

```python
    base_text = base_bytes.decode("utf-8")
    yaml = YAML()
```

and the return becomes:

```python
    return buf.getvalue().encode("utf-8"), changed_keys, extra_changed
```

Update the docstring's first line to say bytes, and add:

```
    Takes and returns BYTES. A check that compares `str` and a write that
    re-encodes independently do not compose into "we verified these bytes and
    sent those bytes" — `Path.write_text` translates newlines on write.
```

- [ ] **Step 4: Fix the call site in `run()`**

Lines 445-455 become:

```python
            base_bytes = project_yaml.read_bytes()
            if_match_hex = hashlib.sha256(base_bytes).hexdigest()
            new_bytes, changed_keys, extra_changed = build_new_yaml_bytes(
                base_bytes, candidate
            )
            message = _commit_message(changed_keys, extra_changed)
            with tempfile.TemporaryDirectory(
                prefix="dispatcher-config-edit-"
            ) as tmp_dir:
                edit_file = Path(tmp_dir) / "project.yaml"
                edit_file.write_bytes(new_bytes)
```

- [ ] **Step 5: Add the test helper and migrate existing tests**

Add near `_cand` in the test file:

```python
def _render_text(text: str, candidate: ConfigCandidate) -> tuple[str, list[str], bool]:
    """String-in/string-out wrapper: the renderer's contract is bytes, but
    most tests are about YAML shape, not encoding."""
    from dispatcher.core.spec_runner_config_actions import build_new_yaml_bytes

    out, changed, extra = build_new_yaml_bytes(text.encode("utf-8"), candidate)
    return out.decode("utf-8"), changed, extra
```

Then in every existing test that calls `build_new_yaml_text(...)`, drop the
local import and call `_render_text(...)` instead. Two tests monkeypatch the
function by name — `test_write_failure_audits_and_frees_busy_slot` (~line 252)
and `test_the_catch_all_labels_itself_pre_launch` (~line 719); change both
`monkeypatch.setattr(mod, "build_new_yaml_text", boom)` to
`"build_new_yaml_bytes"`.

- [ ] **Step 6: Run the full file**

Run: `uv run pytest tests/test_spec_runner_config_actions.py -q`
Expected: all pass, including the new byte-identity test.

- [ ] **Step 7: Commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "refactor: render project.yaml as bytes end to end

Path.write_text opens with newline=None and translates \\n to os.linesep,
so on Windows a deliberately preserved CRLF would be translated a second
time into \\r\\r\\n — bytes no check would ever have seen. The renderer now
takes and returns bytes, encodes once, and the caller writes exactly those
bytes."
```

---

## Task 2: One safe exception over the whole render path

**Files:**
- Modify: `dispatcher/core/spec_runner_config_actions.py`
- Modify: `dispatcher/server/app.py:529-532`
- Test: `tests/test_spec_runner_config_actions.py`, `tests/test_api.py`

**Interfaces:**
- Consumes: `build_new_yaml_bytes` from Task 1.
- Produces: `UnsafeEditError(message, *, stage)` with attribute `.stage: str`.
  It retains NO reference to the original exception. Stages used across the
  plan: `"decode"`, `"parse"`, `"render"`, `"encode"`, `"check-a"`, `"check-b"`.
  `"parse"` covers style inspection as well as `yaml.load` — both are reading
  the source.
- Produces: `_stage(name)` context manager that converts any exception into
  `UnsafeEditError`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_spec_runner_config_actions.py`:

```python
_SECRET = "s3cr3t-telegram-token-ABC123"


def test_a_duplicate_key_refusal_does_not_echo_the_values(tmp_path: Path) -> None:
    """MEASURED leak, not hypothetical: ruamel's DuplicateKeyError renders as
    'found duplicate key "a" with value "2" (original value: "<secret>")'.
    A neighbour's project.yaml routinely holds tokens."""
    from dispatcher.core.spec_runner_config_actions import (
        UnsafeEditError,
        build_new_yaml_bytes,
    )

    base = f'a: "{_SECRET}"\na: 2\nspec_runner:\n  max_retries: 5\n'
    with pytest.raises(UnsafeEditError) as caught:
        build_new_yaml_bytes(base.encode("utf-8"), _cand())
    assert _SECRET not in str(caught.value)
    assert caught.value.stage == "parse"
    assert "DuplicateKeyError" in str(caught.value)


def test_invalid_utf8_refuses_without_echoing_bytes(tmp_path: Path) -> None:
    from dispatcher.core.spec_runner_config_actions import (
        UnsafeEditError,
        build_new_yaml_bytes,
    )

    with pytest.raises(UnsafeEditError) as caught:
        build_new_yaml_bytes(b"project: \xff\xfe bad\n", _cand())
    assert caught.value.stage == "decode"
    assert "UnicodeDecodeError" in str(caught.value)


def test_no_reference_to_the_original_exception_survives(tmp_path: Path) -> None:
    """The original object carries the secret in its own message, so keeping
    it anywhere would make this boundary depend on some future logger's
    behaviour. Walk the whole chain — asserting on str() would not see it."""
    from dispatcher.core.spec_runner_config_actions import (
        UnsafeEditError,
        build_new_yaml_bytes,
    )

    base = f'a: "{_SECRET}"\na: 2\nspec_runner:\n  max_retries: 5\n'
    with pytest.raises(UnsafeEditError) as caught:
        build_new_yaml_bytes(base.encode("utf-8"), _cand())
    err = caught.value
    assert err.__cause__ is None
    assert err.__context__ is None
    assert not hasattr(err, "original")
    # nothing in the object's own attributes carries it either
    assert _SECRET not in repr(vars(err))
```

And the two surface tests — the exception being clean does not prove the
surfaces are:

```python
def test_the_outcome_and_audit_line_carry_no_file_content(
    tmp_path: Path, caplog
) -> None:
    """ActionOutcome.error and the audit line are the surfaces this design
    claims to protect. A clean exception does not prove a caller has not
    appended repr(original) on the way out."""
    import logging

    repo = make_project(tmp_path, "alpha")
    (repo / "project.yaml").write_text(
        f'a: "{_SECRET}"\na: 2\nspec_runner:\n  max_retries: 5\n'
    )
    runner = SpecRunnerConfigActionRunner(DispatcherConfig(roots=(tmp_path,)))
    candidate = _candidate(repo, max_retries=7)
    with caplog.at_level(
        logging.INFO, logger="dispatcher.actions.spec_runner_config"
    ):
        outcome = runner.run("alpha", candidate)

    assert outcome.ok is False
    assert outcome.phase == PHASE_PRE_LAUNCH
    assert _SECRET not in (outcome.error or "")
    assert _SECRET not in caplog.text
```

In `tests/test_api.py`, add the HTTP-surface test. Follow that file's existing
client/token fixtures; the shape is:

```python
def test_update_spec_runner_config_response_carries_no_file_content(
    tmp_path: Path,
) -> None:
    """The refusal reaches an HTTP body. Assert on the body, not the
    exception."""
    secret = "s3cr3t-telegram-token-ABC123"
    # build a repo whose project.yaml has a duplicate key holding `secret`,
    # POST /api/actions/update-spec-runner-config with a valid action token,
    # then:
    assert secret not in response.text
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_spec_runner_config_actions.py -k "duplicate_key or invalid_utf8 or no_reference or no_file_content" -v`
Expected: FAIL with `ImportError: cannot import name 'UnsafeEditError'`.

- [ ] **Step 3: Add the exception and the stage wrapper**

In `dispatcher/core/spec_runner_config_actions.py`, after
`SpecRunnerConfigConflictError`:

```python
class UnsafeEditError(Exception):
    """project.yaml cannot be edited safely; the message carries no content.

    One type for every refusal on the render path, not one per stage: two
    types would invite callers to treat them differently, and they are the
    same decision — this file cannot be edited safely.

    `project.yaml` belongs to a neighbour repo and routinely holds secrets
    (`telegram_bot_token` is in this repo's own fixtures), and this message
    reaches an HTTP response body and the audit log. So it carries the stage,
    the cause's CLASS name, coordinates and sizes — never a source line, a
    scalar value, or a diff.
    """

    def __init__(
        self, message: str, *, stage: str
    ) -> None:
        super().__init__(message)
        self.stage = stage
```

Then the cause formatter and the wrapper, placed just above
`build_new_yaml_bytes`:

```python
def _safe_cause(err: BaseException) -> str:
    """Name the failure without quoting the file.

    Deliberately NOT `str(err)`: ruamel's DuplicateKeyError prints both the
    new and the original value verbatim. Deliberately not `str(mark)` or
    `err.problem` either — both embed source text. Only the class name and
    the mark's integer coordinates are safe.
    """
    name = type(err).__name__
    mark = getattr(err, "problem_mark", None)
    if mark is not None:
        return f"{name} at line {mark.line + 1}, column {mark.column + 1}"
    start = getattr(err, "start", None)
    if isinstance(start, int):
        return f"{name} at byte {start}"
    return name


@contextmanager
def _stage(name: str) -> Iterator[None]:
    """Convert anything raised inside into a content-free UnsafeEditError."""
    try:
        yield
    except UnsafeEditError:
        raise
    except Exception as err:
        raise UnsafeEditError(
            f"cannot safely edit project.yaml: {name} failed ({_safe_cause(err)})",
            stage=name,
        )
```

Add to the imports at the top of the file:

```python
from collections.abc import Iterator
from contextlib import contextmanager
```

- [ ] **Step 4: Wrap the render path**

Restructure `build_new_yaml_bytes` so its body is staged. The decode and the
load move under `_stage`; the existing computation is unchanged:

```python
    with _stage("decode"):
        base_text = base_bytes.decode("utf-8")
    with _stage("parse"):
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.width = _NO_REWRAP
        offset = _sequence_offset(base_text)
        if offset is not None:
            yaml.indent(mapping=2, sequence=offset + 2, offset=offset)
        null_style = _null_style(base_text)
        if null_style is not None:
            yaml.Representer = _representer_spelling_null(null_style)
        doc = yaml.load(StringIO(base_text))
```

The `existing`/`emit` computation and `_apply_block` call go under
`with _stage("render"):`, and the dump plus encode under
`with _stage("encode"):`.

Keep the explicit `TypeError` for a non-mapping `spec_runner:` exactly as it
is — `_stage("render")` will convert it, and its message already names only a
type.

- [ ] **Step 5: Keep the route from turning a refusal into a 500**

`UnsafeEditError` is raised inside `build_new_yaml_bytes`, which `run()` calls
inside its catch-all, so it already becomes an `ActionOutcome`. Confirm no new
route arm is needed by reading `dispatcher/server/app.py:528-532`; leave the
file unchanged if the catch-all covers it. Add a comment there only if you
find it does not.

- [ ] **Step 6: Clear the implicit chain at the function boundary**

`raise ... from None` suppresses *display* only; the original stays reachable
on `__context__`, and its text holds the secret. Raising outside the handler
(the technique `core/contract.py:636-642` uses) does NOT help here — measured:
inside a `@contextmanager`, `contextlib` re-raises within the original
exception's propagation, so `__context__` is set anyway.

So enforce it at one choke point. Wrap the whole body of
`build_new_yaml_bytes`, so every path out of the function is covered —
including the two checks in Tasks 4 and 5, which raise directly and never go
through `_stage`:

```python
    try:
        return _build_new_yaml_bytes(base_bytes, candidate)
    except UnsafeEditError as err:
        # The original is proven to carry secrets in its own message, so no
        # reference to it may leave this function — not on an attribute, not
        # via __cause__, not via __context__. Retaining it would make this
        # boundary's safety depend on some future logger or error reporter.
        # Same rule as core/contract.py:636-642.
        err.__cause__ = None
        err.__context__ = None
        err.__suppress_context__ = True
        raise
```

Move the existing body into a module-level `_build_new_yaml_bytes` with the
same signature; `build_new_yaml_bytes` becomes this wrapper.

- [ ] **Step 7: Run and watch them pass**

Run: `uv run pytest tests/test_spec_runner_config_actions.py tests/test_api.py -q`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat: one content-free exception over the whole render path

Guarding only the checks would leave the boundary open one step earlier at
run()'s error=str(err). Measured: DuplicateKeyError prints both values
verbatim, so a duplicate key in a neighbour's project.yaml would carry its
secret into an HTTP body and the audit log. Asserted on those surfaces, not
on str(exception)."
```

---

## Task 3: Reproduce the source's style (rows 1-5)

**Files:**
- Modify: `dispatcher/core/spec_runner_config_actions.py`
- Test: `tests/test_spec_runner_config_actions.py`

**Interfaces:**
- Consumes: `_stage`, `UnsafeEditError` from Task 2.
- Produces: `_SourceStyle` frozen dataclass with fields `bom: bool`,
  `crlf: bool`, `final_newline: bool`, `explicit_start: bool`,
  `explicit_end: bool`, `mapping_indent: int`, `sequence_offset: int | None`,
  `null_style: str | None`.
- Produces: `_inspect_style(text: str) -> _SourceStyle`,
  `_configured_yaml(style: _SourceStyle) -> YAML`,
  `_render(doc: CommentedMap, style: _SourceStyle) -> bytes`.

- [ ] **Step 1: Write the failing tests**

```python
_STYLE_CASES = {
    "marker_start": "---\nproject: alpha\nspec_runner:\n  max_retries: 5\n",
    "marker_both": "---\nproject: alpha\nspec_runner:\n  max_retries: 5\n...\n",
    "no_final_newline": "project: alpha\nspec_runner:\n  max_retries: 5",
    "crlf": "project: alpha\r\nspec_runner:\r\n  max_retries: 5\r\n",
    "bom": "﻿project: alpha\nspec_runner:\n  max_retries: 5\n",
    "mapping_indent_3": "project: alpha\nspec_runner:\n   max_retries: 5\n",
    "mapping_indent_4": "project: alpha\nspec_runner:\n    max_retries: 5\n",
    "combined": (
        "﻿---\r\nproject: alpha\r\nspec_runner:\r\n   max_retries: 5\r\n..."
    ),
}


@pytest.mark.parametrize("name", sorted(_STYLE_CASES))
def test_a_noop_reproduces_each_source_style(name: str) -> None:
    """Rows 1-5 are fixed by making the RENDERING reproduce the source, never
    by teaching a comparator to forgive them."""
    from dispatcher.core.spec_runner_config_actions import build_new_yaml_bytes

    base = _STYLE_CASES[name].encode("utf-8")
    out, changed, _ = build_new_yaml_bytes(base, ConfigCandidate(typed={}, base_mtime=0.0))
    assert out == base
    assert changed == []


@pytest.mark.parametrize("name", sorted(_STYLE_CASES))
def test_a_real_edit_keeps_each_source_style(name: str) -> None:
    from dispatcher.core.spec_runner_config_actions import build_new_yaml_bytes

    base = _STYLE_CASES[name].encode("utf-8")
    out, changed, _ = build_new_yaml_bytes(
        base, ConfigCandidate(typed={"max_retries": 9}, base_mtime=0.0)
    )
    assert changed == ["max_retries"]
    text = out.decode("utf-8")
    assert "max_retries: 9" in text
    # everything the style test pinned still holds
    assert text.startswith("﻿") == _STYLE_CASES[name].startswith("﻿")
    assert text.endswith("\n") == _STYLE_CASES[name].endswith("\n")
    assert ("\r\n" in text) == ("\r\n" in _STYLE_CASES[name])
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_spec_runner_config_actions.py -k source_style -v`
Expected: FAIL — `marker_start`, `crlf`, `bom`, `no_final_newline`,
`mapping_indent_3`, `mapping_indent_4` and `combined` all differ.

- [ ] **Step 3: Add style inspection**

```python
_NULL_ONLY_KEY_RE = re.compile(r"^(\s*)(?:[^\s#-][^:]*|\"[^\"]*\"|'[^']*'):\s*$")


def _mapping_indent(text: str) -> int | None:
    """The file's own nesting step, or None if it is not consistent.

    A guess, and allowed to be one: Check A verifies the result, so a wrong
    measurement produces a refusal rather than a bad PR.
    """
    steps: set[int] = set()
    parent: int | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped.startswith("-"):
            parent = None
            continue
        if parent is not None and indent > parent:
            steps.add(indent - parent)
        parent = indent if _NULL_ONLY_KEY_RE.match(line) else None
    if len(steps) != 1:
        return None
    step = steps.pop()
    return step if 1 <= step <= 8 else None


@dataclass(frozen=True)
class _SourceStyle:
    """Everything about the source's layout that ruamel would otherwise
    replace with its own defaults."""

    bom: bool
    crlf: bool
    final_newline: bool
    explicit_start: bool
    explicit_end: bool
    mapping_indent: int
    sequence_offset: int | None
    null_style: str | None


def _inspect_style(text: str) -> _SourceStyle:
    body = text.lstrip("﻿")
    stripped_lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    newlines = body.count("\n")
    return _SourceStyle(
        bom=text.startswith("﻿"),
        crlf=newlines > 0 and body.count("\r\n") == newlines,
        final_newline=text.endswith("\n"),
        explicit_start=bool(stripped_lines) and stripped_lines[0] == "---",
        explicit_end=bool(stripped_lines) and stripped_lines[-1] == "...",
        mapping_indent=_mapping_indent(body) or 2,
        sequence_offset=_sequence_offset(body),
        null_style=_null_style(body),
    )
```

Add `from dataclasses import dataclass` to the imports.

- [ ] **Step 4: Add the configured renderer**

```python
def _configured_yaml(style: _SourceStyle) -> YAML:
    """A fresh YAML per use.

    Precaution, not a fix for an observed bug: dumping one document twice was
    measured stable, anchors included, both across instances and on a reused
    one. A fresh instance keeps the fidelity render from sharing any emitter
    state with the real one, so that stability does not become load-bearing.
    """
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = _NO_REWRAP
    yaml.explicit_start = style.explicit_start
    yaml.explicit_end = style.explicit_end
    offset = style.sequence_offset if style.sequence_offset is not None else 0
    yaml.indent(
        mapping=style.mapping_indent, sequence=offset + 2, offset=offset
    )
    if style.null_style is not None:
        yaml.Representer = _representer_spelling_null(style.null_style)
    return yaml


def _render(doc: CommentedMap, style: _SourceStyle) -> bytes:
    """Emit `doc` as the source's own bytes: ruamel first, then the three
    things its emitter has no setting for."""
    buf = StringIO()
    _configured_yaml(style).dump(doc, buf)
    text = buf.getvalue()
    if not style.final_newline:
        text = text.rstrip("\n")
    if style.crlf:
        text = text.replace("\n", "\r\n")
    if style.bom:
        text = "﻿" + text
    return text.encode("utf-8")
```

- [ ] **Step 5: Use it in `build_new_yaml_bytes`**

Replace the ad-hoc YAML construction in the `"parse"` stage with:

```python
    with _stage("parse"):
        style = _inspect_style(base_text)
        doc = _configured_yaml(style).load(StringIO(base_text))
```

and replace the final dump/encode in the `"encode"` stage with:

```python
    with _stage("encode"):
        new_bytes = _render(doc, style)
    return new_bytes, changed_keys, extra_changed
```

- [ ] **Step 6: Run and watch them pass**

Run: `uv run pytest tests/test_spec_runner_config_actions.py -q`
Expected: all pass, including both real-file tests from #113.

- [ ] **Step 7: Commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat: reproduce the source's BOM, line endings, markers and indent

Rows 1-5 of the escape inventory, fixed in the rendering rather than in a
comparator. Every heuristic here is a guess; Task 4's Check A is what makes
guessing safe."
```

---

## Task 4: Check A — fidelity

**Files:**
- Modify: `dispatcher/core/spec_runner_config_actions.py`
- Test: `tests/test_spec_runner_config_actions.py`

**Interfaces:**
- Consumes: `_render`, `_inspect_style`, `UnsafeEditError` from Tasks 2-3.
- Produces: `_first_differing_byte(a: bytes, b: bytes) -> int`.

- [ ] **Step 1: Write the failing tests**

```python
def test_row_6_aligned_values_are_refused_not_reflowed() -> None:
    """ruamel cannot reproduce alignment padding, so an aligned file becomes
    honestly unsupported rather than silently reflowed."""
    from dispatcher.core.spec_runner_config_actions import (
        UnsafeEditError,
        build_new_yaml_bytes,
    )

    base = b"project:      alpha\nspec_runner:\n  max_retries: 5\n"
    with pytest.raises(UnsafeEditError) as caught:
        build_new_yaml_bytes(base, _cand(max_retries=7))
    assert caught.value.stage == "check-a"
    assert "cannot reproduce the source" in str(caught.value)


def test_the_fidelity_coordinate_is_a_byte_offset_not_a_string_index() -> None:
    """They diverge after the first non-ASCII character, and the number has
    to name the thing that was compared."""
    from dispatcher.core.spec_runner_config_actions import (
        UnsafeEditError,
        build_new_yaml_bytes,
    )

    # 'путь' is 4 chars but 8 bytes; the alignment damage is after it.
    base = "note: путь\nproject:      alpha\nspec_runner:\n  max_retries: 5\n"
    with pytest.raises(UnsafeEditError) as caught:
        build_new_yaml_bytes(base.encode("utf-8"), _cand(max_retries=7))
    message = str(caught.value)
    expected = base.encode("utf-8").index(b"project:      alpha") + len(
        b"project:"
    )
    assert f"first mismatch at byte {expected}" in message
    assert "путь" not in message


def test_a_refusal_names_both_lengths() -> None:
    from dispatcher.core.spec_runner_config_actions import (
        UnsafeEditError,
        build_new_yaml_bytes,
    )

    base = b"project:      alpha\nspec_runner:\n  max_retries: 5\n"
    with pytest.raises(UnsafeEditError) as caught:
        build_new_yaml_bytes(base, _cand(max_retries=7))
    assert f"lengths {len(base)}/" in str(caught.value)
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_spec_runner_config_actions.py -k "row_6 or fidelity_coordinate or both_lengths" -v`
Expected: FAIL — no exception raised; the aligned file renders happily today.

- [ ] **Step 3: Implement**

```python
def _first_differing_byte(left: bytes, right: bytes) -> int:
    """Index of the first differing byte; the shorter length if one is a
    prefix of the other."""
    limit = min(len(left), len(right))
    for index in range(limit):
        if left[index] != right[index]:
            return index
    return limit
```

In `build_new_yaml_bytes`, immediately after the `"parse"` stage:

```python
    # Check A — fidelity. A pure load -> dump, with NO candidate applied:
    # this asks only whether the renderer can reproduce this file at all.
    # It covers every construct, including ones nobody enumerated, anywhere
    # in the file, and needs no notion of a block boundary. It is also what
    # makes the style heuristics above safe to guess at.
    with _stage("check-a"):
        fidelity = _render(doc, style)
    if fidelity != base_bytes:
        raise UnsafeEditError(
            "cannot safely edit project.yaml: YAML renderer cannot reproduce "
            "the source byte-for-byte (first mismatch at byte "
            f"{_first_differing_byte(base_bytes, fidelity)}; "
            f"source/output lengths {len(base_bytes)}/{len(fidelity)})",
            stage="check-a",
        )
```

- [ ] **Step 4: Run and watch them pass**

Run: `uv run pytest tests/test_spec_runner_config_actions.py -q`
Expected: all pass. If a pre-existing test now refuses, its fixture has a style
this renderer cannot reproduce — fix the fixture, never the check.

- [ ] **Step 5: Confirm the real files still edit**

Run:

```bash
uv run python - <<'PY'
from pathlib import Path
from dispatcher.core.spec_runner_config_actions import (
    ConfigCandidate, build_new_yaml_bytes,
)
root = Path("/Users/Andrei_Shtanakov/labs/all_ai_orchestrators")
for real in sorted(root.glob("*/project.yaml")):
    base = real.read_bytes()
    out, changed, _ = build_new_yaml_bytes(
        base, ConfigCandidate(typed={}, base_mtime=0.0)
    )
    print(f"{'OK ' if out == base else 'FAIL'} {real.parent.name} changed={changed}")
PY
```

Expected: `OK` for every file. A `FAIL` means Check A rejects a real neighbour
file — stop and report it rather than loosening the check.

- [ ] **Step 6: Commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat: Check A — refuse when the renderer cannot reproduce the source

An unmodified round-trip must equal the source byte for byte. Covers rows
1-6 and anything unenumerated, needs no block boundary, and cannot be
taught to forgive: the whole check is render(load(x)) == x."
```

---

## Task 5: Check B — containment

**Files:**
- Modify: `dispatcher/core/spec_runner_config_actions.py`
- Test: `tests/test_spec_runner_config_actions.py`

**Interfaces:**
- Consumes: everything from Tasks 2-4.
- Produces: `_owned_span(doc: CommentedMap, lines: list[str]) -> tuple[int, int] | None`
  returning 0-based inclusive line indices, and
  `_outside(lines: list[str], span: tuple[int, int] | None) -> tuple[str, str]`
  returning `(prefix, suffix)`.

- [ ] **Step 1: Write the failing tests**

```python
def test_an_alias_expanded_outside_the_block_is_refused() -> None:
    """Row 7: destroying an anchor INSIDE the block makes ruamel expand every
    alias to it elsewhere, so `elsewhere: *over` becomes an inlined copy. No
    style matching prevents this; only the containment check catches it."""
    from dispatcher.core.spec_runner_config_actions import (
        UnsafeEditError,
        build_new_yaml_bytes,
    )

    base = (
        "spec_runner:\n"
        "  max_retries: 5\n"
        "  extra_executor_config: &over\n"
        "    a: 1\n"
        "elsewhere: *over\n"
    ).encode("utf-8")
    cand = ConfigCandidate(typed={}, extra_executor_config={}, base_mtime=0.0)
    with pytest.raises(UnsafeEditError) as caught:
        build_new_yaml_bytes(base, cand)
    assert caught.value.stage == "check-b"
    assert "outside spec_runner" in str(caught.value)


_SPAN_POSITIONS = {
    "first": "spec_runner:\n  max_retries: 5\nz_after: 1\n",
    "middle": "a_before: 1\nspec_runner:\n  max_retries: 5\nz_after: 1\n",
    "last": "a_before: 1\nspec_runner:\n  max_retries: 5\n",
    "many_following": (
        "a: 1\nspec_runner:\n  max_retries: 5\nb: 2\nc: 3\nd: 4\n"
    ),
    "block_scalar_at_the_boundary": (
        "spec_runner:\n"
        "  max_retries: 5\n"
        "  extra_executor_config:\n"
        "    note: |\n"
        "      first\n"
        "      second\n"
        "\n"
        "# after the block\n"
        "after: 1\n"
    ),
    "absent_with_tail_and_end_marker": (
        "project: alpha\n\n# trailing note\n...\n"
    ),
}


@pytest.mark.parametrize("name", sorted(_SPAN_POSITIONS))
def test_an_edit_is_contained_wherever_the_block_sits(name: str) -> None:
    from dispatcher.core.spec_runner_config_actions import build_new_yaml_bytes

    base = _SPAN_POSITIONS[name].encode("utf-8")
    out, _, _ = build_new_yaml_bytes(base, _cand(max_retries=9))
    assert b"max_retries: 9" in out


def test_the_tail_comment_after_the_block_is_outside_the_span() -> None:
    """It is the thing #113 was about, so it is protected by Check B rather
    than merely produced correctly."""
    from dispatcher.core.spec_runner_config_actions import build_new_yaml_bytes

    base = (
        "spec_runner:\n"
        "  max_retries: 5\n"
        "\n"
        "# col-0 standalone\n"
        "after: 1\n"
    ).encode("utf-8")
    out, _, _ = build_new_yaml_bytes(base, _cand(max_retries=9, review_model="rm"))
    text = out.decode("utf-8")
    assert "review_model: rm" in text
    assert text.index("review_model: rm") < text.index("# col-0 standalone")
    assert text.index("# col-0 standalone") < text.index("after: 1")
```

- [ ] **Step 2: Run and watch them fail**

Run: `uv run pytest tests/test_spec_runner_config_actions.py -k "alias_expanded or contained_wherever or tail_comment_after" -v`
Expected: `alias_expanded` FAILS (no exception). The others may already pass —
that is fine, they are the regression net for the span logic you are about to
add, and Step 4 must not break them.

- [ ] **Step 3: Implement the span**

```python
def _owned_span(doc: CommentedMap, lines: list[str]) -> tuple[int, int] | None:
    """Inclusive 0-based line range of the `spec_runner:` block's DATA.

    Trailing blank and comment lines are deliberately left OUT, which puts
    the text following the block into the compared region — that text is
    exactly what #113 was about. The end is derived from the next top-level
    key rather than by walking the block's own subtree, because ruamel's line
    info for a block scalar points at the `|`, not at the scalar's last line,
    and walking would truncate the span.
    """
    if "spec_runner" not in doc:
        return None
    start = doc.lc.data["spec_runner"][0]
    following = [pos[0] for pos in doc.lc.data.values() if pos[0] > start]
    end = (min(following) if following else len(lines)) - 1
    while end > start and (
        not lines[end].strip() or lines[end].lstrip().startswith("#")
    ):
        end -= 1
    return start, end


def _outside(lines: list[str], span: tuple[int, int] | None) -> tuple[str, str]:
    """(before, after) the owned span. No span -> the whole text is outside."""
    if span is None:
        return "".join(lines), ""
    start, end = span
    return "".join(lines[:start]), "".join(lines[end + 1 :])
```

- [ ] **Step 4: Wire Check B in**

Capture the source span **before** `_apply_block` mutates `doc` — after
mutation the source's own line numbers are gone. Just after Check A:

```python
    source_lines = base_text.splitlines(keepends=True)
    source_span = _owned_span(doc, source_lines)
```

and after the `"encode"` stage, before returning:

```python
    # Check B — containment. Check A already proved the renderer reproduces
    # this file, so any difference now is attributable to the edit; this asks
    # only whether the edit stayed inside the block we own. Spans are located
    # independently in source and output, so a tail that shifts down a line
    # when a key is appended is not a difference — only content is compared.
    with _stage("check-b"):
        new_text = new_bytes.decode("utf-8")
        new_lines = new_text.splitlines(keepends=True)
        new_doc = _configured_yaml(style).load(StringIO(new_text))
        new_span = _owned_span(new_doc, new_lines)
    source_before, source_after = _outside(source_lines, source_span)
    new_before, new_after = _outside(new_lines, new_span)
    for side, was, now, offset in (
        ("before the block", source_before, new_before, 0),
        ("after the block", source_after, new_after, (new_span or (0, -1))[1] + 1),
    ):
        if was == now:
            continue
        differing = _first_differing_line(was, now)
        raise UnsafeEditError(
            "cannot safely edit project.yaml: rendering changes bytes outside "
            f"spec_runner ({side}; first mismatch at output line "
            f"{offset + differing + 1}; source/output lengths "
            f"{len(base_bytes)}/{len(new_bytes)})",
            stage="check-b",
        )
```

with:

```python
def _first_differing_line(left: str, right: str) -> int:
    """0-based index of the first line that differs, within the region."""
    left_lines = left.splitlines()
    right_lines = right.splitlines()
    for index in range(min(len(left_lines), len(right_lines))):
        if left_lines[index] != right_lines[index]:
            return index
    return min(len(left_lines), len(right_lines))
```

- [ ] **Step 5: Run and watch them pass**

Run: `uv run pytest tests/test_spec_runner_config_actions.py -q`
Expected: all pass.

- [ ] **Step 6: Re-run the real-file check from Task 4 Step 5**

Expected: `OK` for every file. Then run the full suite with both pinned
binaries on `PATH` (see Global Constraints).

- [ ] **Step 7: Commit**

```bash
uv run ruff format . && uv run ruff check . && uv run pyrefly check
git add -A
git commit -m "feat: Check B — refuse when an edit escapes the spec_runner block

Catches the anchor-inside/alias-outside case, where clearing the overlay
destroys an anchor and ruamel expands every alias to it elsewhere in the
file. Spans are located independently in source and output, so a shifted
tail is not a difference."
```

---

## Task 6: Pin the third guarantee, and close the item

**Files:**
- Test: `tests/test_spec_runner_config_actions.py`
- Modify: `TODO.md`

**Interfaces:**
- Consumes: everything above. No production code changes.

**Why this task exists:** Check A runs *before* the candidate is applied and
Check B excludes the block by construction, so **neither check** protects
unknown data inside the block. These tests are its only defence.

- [ ] **Step 1: Write the tests**

```python
_IN_BLOCK_NEIGHBOUR_DATA = """\
project: alpha
spec_runner:
  max_retries: 5
  # a standalone note the owner wrote
  zzz_unknown_first: "quoted value"  # inline note
  aaa_unknown_second: 'single quoted'
  claude_model: keep-me
workstreams: []
"""


def test_an_unknown_in_block_key_survives_the_candidate() -> None:
    text, _, _ = _render_text(_IN_BLOCK_NEIGHBOUR_DATA, _cand(max_retries=7))
    assert "zzz_unknown_first:" in text
    assert "aaa_unknown_second:" in text


def test_an_unknown_keys_quote_style_survives_the_candidate() -> None:
    text, _, _ = _render_text(_IN_BLOCK_NEIGHBOUR_DATA, _cand(max_retries=7))
    assert '"quoted value"' in text
    assert "'single quoted'" in text


def test_in_block_comments_survive_the_candidate() -> None:
    text, _, _ = _render_text(_IN_BLOCK_NEIGHBOUR_DATA, _cand(max_retries=7))
    assert "# a standalone note the owner wrote" in text
    assert "# inline note" in text


def test_the_order_of_unknown_keys_survives_the_candidate() -> None:
    """Alphabetically reversed on purpose: a re-sort would silently pass a
    test that used already-sorted names."""
    text, _, _ = _render_text(_IN_BLOCK_NEIGHBOUR_DATA, _cand(max_retries=7))
    assert text.index("zzz_unknown_first") < text.index("aaa_unknown_second")


def test_values_absent_from_the_candidate_survive() -> None:
    cand = ConfigCandidate(typed={"max_retries": 7}, base_mtime=0.0)
    text, changed, _ = _render_text(_IN_BLOCK_NEIGHBOUR_DATA, cand)
    assert "claude_model: keep-me" in text
    assert changed == ["max_retries"]
```

- [ ] **Step 2: Run them**

Run: `uv run pytest tests/test_spec_runner_config_actions.py -k "unknown_in_block or quote_style_survives or in_block_comments_survive or order_of_unknown or absent_from_the_candidate" -v`
Expected: PASS. These pin behaviour delivered by #113's in-place mutation; if
any FAILS, that is a real bug in `_apply_block` — fix it before continuing and
say so in the commit.

- [ ] **Step 3: Close the TODO item**

In `TODO.md`, change the `@id:render-outside-block-fail-closed` line from
`- [ ]` to `- [x]`, append ` — PR #<n>` once the PR exists, and record beneath
it: the two-check structure, that rows 1-5 are preserved while row 6 is now an
honest refusal, that diagnostics are content-free because `DuplicateKeyError`
was measured leaking values, and that in-block preservation rests on tests
rather than on either check.

- [ ] **Step 4: Full verification**

```bash
bash scripts/install_pinned_checker.sh
bash scripts/install_pinned_steward.sh
# prefix both printed bin directories onto PATH, then:
uv run pytest -q
uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
```

Expected: every test passes, 0 skipped in the live-smoke files.

- [ ] **Step 5: Commit and open the PR**

```bash
git add -A
git commit -m "test: pin unknown in-block data against candidate mutation

Neither check covers this: Check A runs before the candidate is applied and
Check B excludes the block by construction. These tests are the only
defence for a neighbour's unknown keys, quote styles, comments and ordering."
git push -u origin feat/render-outside-block-fail-closed
gh pr create --title "..." --body "..."
```

The PR body must state the capability removal plainly: an aligned-value
`project.yaml` is edited today, with churn, and will be refused after this.

Then follow this repo's review rule: read the GitHub Copilot review, fix valid
findings with new commits on the same branch, answer invalid ones with
reasoning, and **do not merge** — the user merges.

---

## Self-Review

**Spec coverage.** Byte pipeline → Task 1. Safe exception across the whole
render path, asserted on all three surfaces → Task 2. Style preservation rows
1-5 → Task 3. Check A, and row 6 as a deliberate refusal → Task 4. Check B, the
owned span, and row 7 → Task 5. The third guarantee's targeted tests → Task 6.
Unicode byte-offset test → Task 4 Step 1. Temp-file byte identity → Task 1.
Span placement cases → Task 5 Step 1. Known residuals need no task; they are
accepted limits.

**Placeholders.** The only deliberately unwritten content is the `tests/test_api.py`
body in Task 2 Step 1, which must follow that file's existing client and
action-token fixtures rather than invent new ones, and the PR title/body in
Task 6. Both are marked as such.

**Type consistency.** `build_new_yaml_bytes(bytes, ConfigCandidate) -> tuple[bytes, list[str], bool]`
is used identically in Tasks 1-6. `UnsafeEditError(message, *, stage)` with
`.stage` and no reference to the original exception is consistent across
Tasks 2, 4, 5. `_SourceStyle`
fields are referenced only as defined in Task 3. `_owned_span` returns
`tuple[int, int] | None` and `_outside` consumes exactly that.
