import { describe, expect, it } from "vitest";
import type { SpecRunnerConfigEntry } from "../src/api";
import {
  applyEdit,
  diffLines,
  fieldItems,
  newFlow,
  requestBody,
  validateField,
} from "../src/configFlow";

function entry(): SpecRunnerConfigEntry {
  return {
    project: "arbiter",
    project_yaml_path: "/repos/arbiter/project.yaml",
    base_mtime: 1_700_000_000,
    typed: {
      max_retries: { value: 3, explicit: false },
      task_timeout_minutes: { value: 30, explicit: false },
      claude_command: { value: "claude", explicit: true },
      auto_commit: { value: true, explicit: false },
      create_git_branch: { value: true, explicit: false },
      run_tests_on_done: { value: true, explicit: false },
      test_command: { value: "uv run pytest", explicit: false },
      run_lint_on_done: { value: true, explicit: false },
      lint_command: { value: "uv run ruff check .", explicit: false },
      claude_model: { value: "", explicit: false },
      review_command: { value: "", explicit: false },
      review_model: { value: "", explicit: false },
    },
    extra_executor_config: {},
    extra_explicit: false,
  };
}

describe("newFlow / fieldItems markers", () => {
  it("reflects explicit vs default from the entry", () => {
    const state = newFlow(entry());
    const items = fieldItems(state);
    const claudeCommand = items.find((f) => f.field === "claude_command");
    const maxRetries = items.find((f) => f.field === "max_retries");
    expect(claudeCommand?.marker).toBe("explicit");
    expect(claudeCommand?.value).toBe("claude");
    expect(maxRetries?.marker).toBe("default");
    expect(maxRetries?.value).toBe(3);
    expect(items).toHaveLength(12);
  });

  it("marks a field edited after applyEdit, without mutating the marker of others", () => {
    const state = newFlow(entry());
    const edited = applyEdit(state, "max_retries", "9");
    const items = fieldItems(edited);
    const maxRetries = items.find((f) => f.field === "max_retries");
    const claudeCommand = items.find((f) => f.field === "claude_command");
    expect(maxRetries?.marker).toBe("edited");
    expect(maxRetries?.value).toBe(9);
    expect(claudeCommand?.marker).toBe("explicit");
  });
});

describe("validateField", () => {
  it("rejects non true/false text for bool fields", () => {
    expect(validateField(entry(), "auto_commit", "yes")).toEqual(
      expect.any(String),
    );
  });

  it("accepts case-insensitive true/false for bool fields", () => {
    expect(validateField(entry(), "auto_commit", "TRUE")).toBeNull();
    expect(validateField(entry(), "auto_commit", "false")).toBeNull();
  });

  it("rejects non-integers for int fields", () => {
    expect(validateField(entry(), "max_retries", "3.5")).toEqual(
      expect.any(String),
    );
  });

  it("accepts whitespace-padded integers for int fields", () => {
    expect(validateField(entry(), "max_retries", " 7 ")).toBeNull();
  });

  it("accepts anything for string fields", () => {
    expect(validateField(entry(), "claude_command", "")).toBeNull();
    expect(validateField(entry(), "claude_command", "anything at all")).toBeNull();
  });
});

describe("applyEdit coercion", () => {
  it("coerces an edited int field to a number", () => {
    const state = applyEdit(newFlow(entry()), "max_retries", "9");
    expect(state.edits.max_retries).toBe(9);
    expect(typeof state.edits.max_retries).toBe("number");
  });

  it("coerces an edited bool field to a boolean", () => {
    const state = applyEdit(newFlow(entry()), "auto_commit", "true");
    expect(state.edits.auto_commit).toBe(true);
    expect(typeof state.edits.auto_commit).toBe("boolean");
  });

  it("returns a NEW state object, leaving the input state untouched", () => {
    const state = newFlow(entry());
    const edited = applyEdit(state, "max_retries", "9");
    expect(edited).not.toBe(state);
    expect(state.edits).toEqual({});
    expect(edited.edits.max_retries).toBe(9);
  });
});

describe("diffLines", () => {
  it("is only the honesty caption + (no changes) with no edits", () => {
    const lines = diffLines(newFlow(entry()));
    expect(lines[0]).toBe(
      "PR diff may include already-explicit keys unchanged",
    );
    expect(lines).toEqual([
      "PR diff may include already-explicit keys unchanged",
      "(no changes)",
    ]);
  });

  it("lists only edited fields as -/+ pairs, after the caption", () => {
    let state = newFlow(entry());
    state = applyEdit(state, "max_retries", "9");
    const lines = diffLines(state);
    expect(lines[0]).toBe(
      "PR diff may include already-explicit keys unchanged",
    );
    expect(lines).toContain("- max_retries: 3");
    expect(lines).toContain("+ max_retries: 9");
    // untouched fields must not appear in the diff
    expect(lines.join("\n")).not.toContain("claude_command");
    expect(lines).toHaveLength(3);
  });

  it("survives the diff-reopen scenario: previewing does not lose edits", () => {
    let state = newFlow(entry());
    state = applyEdit(state, "max_retries", "9");
    // simulate opening the diff doc, then re-entering the field loop with
    // the SAME state (the extension.ts driver's `continue` branch)
    const firstPreview = diffLines(state);
    const secondPreview = diffLines(state);
    expect(firstPreview).toEqual(secondPreview);
    const items = fieldItems(state);
    expect(items.find((f) => f.field === "max_retries")?.value).toBe(9);
    expect(items.find((f) => f.field === "max_retries")?.marker).toBe(
      "edited",
    );
  });
});

describe("requestBody", () => {
  it("carries all 12 typed keys, current-or-edited and coerced", () => {
    let state = newFlow(entry());
    state = applyEdit(state, "max_retries", "9");
    state = applyEdit(state, "auto_commit", "true");
    const body = requestBody(state);
    expect(Object.keys(body.typed)).toHaveLength(12);
    expect(body.typed.max_retries).toBe(9);
    expect(typeof body.typed.max_retries).toBe("number");
    expect(body.typed.auto_commit).toBe(true);
    // unedited fields pass through the entry's current value verbatim
    expect(body.typed.claude_command).toBe("claude");
    expect(body.typed.task_timeout_minutes).toBe(30);
    expect(body.typed.create_git_branch).toBe(true);
    expect(body.typed.run_tests_on_done).toBe(true);
    expect(body.typed.test_command).toBe("uv run pytest");
    expect(body.typed.run_lint_on_done).toBe(true);
    expect(body.typed.lint_command).toBe("uv run ruff check .");
    expect(body.typed.claude_model).toBe("");
    expect(body.typed.review_command).toBe("");
    expect(body.typed.review_model).toBe("");
  });

  it("carries extra_executor_config: null and base_mtime passthrough", () => {
    const state = newFlow(entry());
    const body = requestBody(state);
    expect(body.extra_executor_config).toBeNull();
    expect(body.base_mtime).toBe(1_700_000_000);
  });

  it("sets dir to the basename of the project directory (project.yaml's parent)", () => {
    const body = requestBody(newFlow(entry()));
    expect(body.dir).toBe("arbiter");
  });
});
