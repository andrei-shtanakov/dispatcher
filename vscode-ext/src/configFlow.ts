/**
 * Pure QuickPick config-editor state machine (DESIGN-604). Must stay
 * vscode-free — the extension.ts driver is the only vscode-aware layer;
 * this module is exercised directly by vitest without a vscode host.
 *
 * Coercion mirrors `dispatcher/tui/config_edit.py::coerce_typed` verbatim,
 * but the coercion AUTHORITY here is the field's CURRENT value in
 * `entry.typed` (loaded from JSON) rather than a TYPED_DEFAULTS table:
 * JSON already carries native boolean/number/string types, so
 * `typeof current` tells us which rule applies. Python has to check bool
 * before int because `bool` subclasses `int` there (`isinstance(True, int)`
 * is `True`); in JS/TS `typeof` already distinguishes "boolean" from
 * "number", so the two branches below can never collide the way the
 * Python bool-before-int ordering guards against — the comment on each
 * branch keeps that rationale explicit rather than assumed.
 */

import * as path from "node:path";
import type { SpecRunnerConfigEntry } from "./api";

/** In-flight edit state for one project's spec_runner: block. */
export interface FlowState {
  entry: SpecRunnerConfigEntry;
  edits: Record<string, unknown>;
}

export interface FieldItem {
  field: string;
  value: unknown;
  marker: "explicit" | "default" | "edited";
}

const DIFF_CAPTION = "PR diff may include already-explicit keys unchanged";

/** Start a flow for the given entry with no edits yet. */
export function newFlow(entry: SpecRunnerConfigEntry): FlowState {
  return { entry, edits: {} };
}

/**
 * Validate a raw InputBox string for `field`. Returns `null` when valid
 * (the exact vscode InputBox `validateInput` contract), a user-facing
 * message otherwise. Never mutates `entry`.
 */
export function validateField(
  entry: SpecRunnerConfigEntry,
  field: string,
  raw: string,
): string | null {
  const current = entry.typed[field]?.value;
  const text = raw.trim();
  // bool: current value's typeof is "boolean" — only literal true/false
  // accepted, never a silent everything-else-is-false.
  if (typeof current === "boolean") {
    const lower = text.toLowerCase();
    if (lower === "true" || lower === "false") {
      return null;
    }
    return `${field}: enter true or false`;
  }
  // int: current value's typeof is "number" — reject non-integers
  // (e.g. "3.5") the same way Python's `int(text)` would.
  if (typeof current === "number") {
    if (/^-?\d+$/.test(text)) {
      return null;
    }
    return `${field}: enter an integer`;
  }
  // str: always valid.
  return null;
}

function coerce(current: unknown, raw: string): unknown {
  const text = raw.trim();
  if (typeof current === "boolean") {
    return text.toLowerCase() === "true";
  }
  if (typeof current === "number") {
    return parseInt(text, 10);
  }
  return raw;
}

/**
 * Apply a validated edit, returning a NEW FlowState (the input state is
 * never mutated). Caller must have already checked `validateField`.
 */
export function applyEdit(
  state: FlowState,
  field: string,
  raw: string,
): FlowState {
  const current = state.entry.typed[field]?.value;
  return {
    entry: state.entry,
    edits: { ...state.edits, [field]: coerce(current, raw) },
  };
}

/** All typed fields for the QuickPick list, with their edit provenance. */
export function fieldItems(state: FlowState): FieldItem[] {
  return Object.entries(state.entry.typed).map(([field, typedField]) => {
    if (field in state.edits) {
      return { field, value: state.edits[field], marker: "edited" as const };
    }
    return {
      field,
      value: typedField.value,
      marker: typedField.explicit
        ? ("explicit" as const)
        : ("default" as const),
    };
  });
}

/**
 * Diff preview lines: honesty caption first (a full PR diff may touch
 * already-explicit keys that this preview shows as unchanged), then
 * `- field: old` / `+ field: new` for every EDITED field (present in
 * `state.edits`) — not just fields whose coerced value differs from the
 * original, so re-editing a field back to its original text still shows
 * up, matching `fieldItems`' "edited" marker.
 */
export function diffLines(state: FlowState): string[] {
  const editedFields = Object.keys(state.edits);
  if (editedFields.length === 0) {
    return [DIFF_CAPTION, "(no changes)"];
  }
  const lines: string[] = [DIFF_CAPTION];
  for (const field of editedFields) {
    const oldValue = state.entry.typed[field]?.value;
    lines.push(`- ${field}: ${String(oldValue)}`);
    lines.push(`+ ${field}: ${String(state.edits[field])}`);
  }
  return lines;
}

/** POST body for `ApiClient.updateSpecRunnerConfig`. */
export function requestBody(state: FlowState): {
  dir: string;
  typed: Record<string, unknown>;
  extra_executor_config: null;
  base_mtime: number;
} {
  const typed: Record<string, unknown> = {};
  for (const [field, typedField] of Object.entries(state.entry.typed)) {
    typed[field] =
      field in state.edits ? state.edits[field] : typedField.value;
  }
  // Basename-keyed contract (matches dispatcher/tui/config_edit.py and the
  // server's ActionRunner): the repo dir is project_yaml_path's PARENT
  // directory's basename, not the yaml file's own basename.
  return {
    dir: path.basename(path.dirname(state.entry.project_yaml_path)),
    typed,
    extra_executor_config: null,
    base_mtime: state.entry.base_mtime,
  };
}
