# extra_executor_config Editing UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Редактирование overlay `extra_executor_config` в web-конфиг-панели: три явных состояния (preserve/edit/clear) поверх готового серверного tri-state.

**Architecture:** Чисто клиентская фича — index.html only, серверных правок НОЛЬ (tri-state `null`/`{}`/dict, schema-валидация и propose-pr готовы с PR #40). Спека: `docs/superpowers/specs/2026-07-18-extra-config-editing-design.md` (DESIGN-1001..1003).

**Tech Stack:** vanilla JS single-file SPA, pytest static pins.

## Global Constraints

- Гейты после КАЖДОГО таска: `uv run pytest -q` (baseline: 317 passed + 1 skipped, warning-free), `uv run ruff format --check .`, `uv run ruff check .`, `uv run pyrefly check`.
- Tri-state строго: `null` (preserve) | `{}` (clear) | непустой dict (replace); ручной ввод `{}` в edit эквивалентен clear (не отдельное состояние).
- Plain-object-guard: `parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)`.
- Preserve-состояние НЕ рендерит содержимое overlay (визуальная экспозиция токенов) — только «no overlay» / «overlay present (N keys), preserved as-is»; содержимое появляется ТОЛЬКО по клику Edit.
- Каждый переход состояния и каждый input в textarea сбрасывает взведённый Preview (`resetSpecRunnerConfigPreview()`), как typed-поля и AI-подсказки.
- Пустая textarea = невалидный JSON = Preview/submit заблокированы.
- Web Preview остаётся ЛОКАЛЬНЫМ; schema-422 приходит только на Confirm (существующий result-div путь) — серверного preview-endpoint НЕ добавлять.
- Все строки в DOM — через textContent (никакого innerHTML для пользовательских/модельных данных).
- Ветка: `feat/extra-config-editing` (план — первый коммит, без plan-PR). Прямые коммиты в master запрещены.

---

### Task 1: overlay-секция в web-панели (DESIGN-1001 + 1002)

**Files:**
- Modify: `dispatcher/server/static/index.html` (markup ~строки 114–121; JS: `renderSpecRunnerConfigForm` ~470, `readSpecRunnerConfigTyped`-регион ~553, `renderSpecRunnerConfigDiff` ~575, submit-body ~613 — якоря по текущему master, найти по grep)
- Test: `tests/test_api.py` (static-пины в index-тесте)

**Interfaces:**
- Consumes: существующие `currentSpecRunnerConfig` (поля `extra_executor_config`, `extra_explicit`), `resetSpecRunnerConfigPreview()`, submit-flow с `dataset.armed`.
- Produces: `readSpecRunnerConfigOverlay() -> null | {} | dict` — подставляется в body POST вместо хардкода `extra_executor_config: null`.

- [ ] **Step 1: Write the failing static pins**

В существующий index-тест `tests/test_api.py` добавить:

```python
    assert "overlay-editor" in resp.text
    assert "overlay-edit" in resp.text
    assert "overlay-clear" in resp.text
    assert "overlay-cancel" in resp.text
    assert "overlay-warning" in resp.text
    assert "overlay-summary" in resp.text
    assert "readSpecRunnerConfigOverlay" in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_api.py -q`
Expected: FAIL на новых assert-ах.

- [ ] **Step 3: Markup**

В `#spec-runner-config`, между `</form>` и `<pre id="spec-runner-config-diff">`:

```html
    <div id="overlay-section">
      <div id="overlay-summary" class="fresh"></div>
      <button id="overlay-edit" type="button">Edit overlay</button>
      <button id="overlay-clear" type="button">Clear overlay</button>
      <button id="overlay-cancel" type="button" hidden>Cancel</button>
      <textarea id="overlay-editor" rows="10" spellcheck="false" hidden></textarea>
      <div id="overlay-error" class="fresh"></div>
      <div id="overlay-warning" hidden>⚠ the extra_executor_config block
        will be REMOVED from project.yaml</div>
    </div>
```

CSS (рядом с конфиг-панельными правилами):

```css
  #overlay-editor { width: 100%; font-family: ui-monospace, monospace;
    font-size: 12px; margin-top: 6px; }
  #overlay-warning { color: var(--bad); margin-top: 6px; }
```

- [ ] **Step 4: JS — состояния, guard, сборка**

Рядом с конфиг-JS (после `readSpecRunnerConfigTyped`):

```js
let overlayState = "preserve";  // preserve | edit | clear (spec §2)

function overlayParse() {
  const text = document.getElementById("overlay-editor").value;
  let parsed;
  try {
    parsed = JSON.parse(text);
  } catch (err) {
    return {ok: false, error: "invalid JSON: " + err.message};
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return {ok: false, error: "overlay must be a JSON object"};
  }
  return {ok: true, value: parsed};
}

function readSpecRunnerConfigOverlay() {
  if (overlayState === "clear") return {};
  if (overlayState === "edit") {
    const res = overlayParse();
    return res.ok ? res.value : null;  // unreachable armed: submit locked
  }
  return null;  // preserve
}

function renderOverlaySummary() {
  const out = document.getElementById("overlay-summary");
  if (overlayState === "edit") {
    out.textContent = "editing overlay — replaces the block on submit";
    return;
  }
  if (overlayState === "clear") {
    out.textContent = "";
    return;
  }
  const overlay = currentSpecRunnerConfig?.extra_executor_config ?? {};
  const n = Object.keys(overlay).length;
  // preserve NEVER renders the content (token exposure, spec §1.4)
  out.textContent = n
    ? `overlay present (${n} keys), preserved as-is`
    : "no overlay";
}

function overlayUpdateSubmitLock() {
  const submit = document.getElementById("spec-runner-config-submit");
  const error = document.getElementById("overlay-error");
  if (overlayState !== "edit") {
    error.textContent = "";
    submit.disabled = !currentSpecRunnerConfig;
    return;
  }
  const res = overlayParse();
  error.textContent = res.ok ? "" : "✗ " + res.error;
  submit.disabled = !res.ok;
}

function overlaySetState(state) {
  overlayState = state;
  document.getElementById("overlay-editor").hidden = state !== "edit";
  document.getElementById("overlay-warning").hidden = state !== "clear";
  document.getElementById("overlay-cancel").hidden = state === "preserve";
  document.getElementById("overlay-edit").hidden = state !== "preserve";
  document.getElementById("overlay-clear").hidden = state !== "preserve";
  renderOverlaySummary();
  if (document.getElementById("spec-runner-config-submit")
      .dataset.armed === "true") {
    resetSpecRunnerConfigPreview();
  }
  overlayUpdateSubmitLock();
}

document.getElementById("overlay-edit").addEventListener("click", () => {
  document.getElementById("overlay-editor").value = JSON.stringify(
    currentSpecRunnerConfig?.extra_executor_config ?? {}, null, 2);
  overlaySetState("edit");
});
document.getElementById("overlay-clear").addEventListener("click",
  () => overlaySetState("clear"));
document.getElementById("overlay-cancel").addEventListener("click",
  () => overlaySetState("preserve"));
document.getElementById("overlay-editor").addEventListener("input", () => {
  if (document.getElementById("spec-runner-config-submit")
      .dataset.armed === "true") {
    resetSpecRunnerConfigPreview();
  }
  overlayUpdateSubmitLock();
});
```

- [ ] **Step 5: интеграция с формой, диффом и сабмитом**

1. В `renderSpecRunnerConfigForm(cfg)` (после существующего
   `resetSpecRunnerConfigPreview()`): `overlaySetState("preserve");` —
   каждый рендер панели возвращает overlay в безопасный дефолт.
2. В `renderSpecRunnerConfigDiff(typed)` перед записью в `diff`:

```js
  const overlay = readSpecRunnerConfigOverlay();
  if (overlay === null) {
    lines.push("overlay: preserved");
  } else if (Object.keys(overlay).length === 0) {
    lines.push("overlay: will be cleared");
  } else {
    lines.push(`overlay: replaced (${Object.keys(overlay).length} top-level keys)`);
  }
```

   (intent-строка есть всегда, поэтому ветка `"(no changes)"` становится
   недостижимой — это ожидаемо: «overlay: preserved» и есть честный
   «no changes» для overlay-части; сохранить fallback-выражение как есть.)
3. В submit-body заменить строку
   `extra_executor_config: null,  // tri-state: null = preserve current`
   на
   `extra_executor_config: readSpecRunnerConfigOverlay(),  // tri-state: spec §2`.
4. После успешного submit панель перезагружается существующим путём
   (`detail()` → `renderSpecRunnerConfigForm`) — состояние вернётся в
   preserve само; проверить, что это так, и если панель НЕ
   перезагружается после success — вызвать `overlaySetState("preserve")`
   в success-ветке.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_api.py -q`
Expected: PASS (static-пины).

- [ ] **Step 7: Full gates, then commit**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git add dispatcher/server/static/index.html tests/test_api.py
git commit -m "feat: extra_executor_config overlay editing — explicit tri-state UI (DESIGN-1001/1002)"
```

---

### Task 2: документация

**Files:**
- Modify: `README.md` (секция config editor / AI suggestions — рядом)

**Interfaces:** нет (docs-only).

- [ ] **Step 1: README**

В секцию конфиг-редактора добавить абзац (голос секции соблюсти):
редактирование overlay `extra_executor_config` в web — три явных
состояния (preserve по умолчанию — содержимое скрыто, показывается
только «overlay present (N keys)»; Edit — JSON-textarea с локальной
синтакс-валидацией; Clear — с предупреждением об удалении блока);
schema-валидация выполняется сервером на «Confirm & open PR» (422 с
перечнем ошибок); TUI/VSCode остаются read-only.

- [ ] **Step 2: Full gates, then commit**

```bash
uv run pytest -q && uv run ruff format --check . && uv run ruff check . && uv run pyrefly check
git add README.md
git commit -m "docs: overlay editing states in the config-editor section"
```

---

## Final whole-branch review mandate

- Гейты прогнать самому (317 passed + 1 skipped, warning-free).
- Живой click-through (uvicorn + fixture workspace с project.yaml,
  содержащим overlay c 2 ключами): preserve показывает «overlay present
  (2 keys)» БЕЗ содержимого; Edit префиллит pretty-JSON; невалидный
  JSON / `[]` / `"x"` / пустая строка блокируют Preview с inline-ошибкой;
  Clear показывает warning; взведённый Preview сбрасывается на переходах
  и вводе; intent-строка в диффе для всех трёх состояний; Confirm с
  невалидной схемой (неизвестный ключ в overlay) → 422-перечень в
  result-div (fake github-checker не нужен — 422 происходит до него).
- Пин: body запроса реально несёт null/{}/dict по состояниям (проверить
  перехватом в live-прогоне или чтением кода `readSpecRunnerConfigOverlay`).
