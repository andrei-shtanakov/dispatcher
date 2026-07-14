import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { ErrorEvent, OverviewResponse, ProjectDetail } from "../src/api";
import {
  detailLines,
  errorLabel,
  humanizeAgo,
  portFromUrl,
  projectView,
  shouldSpawn,
  statusText,
  verdictText,
  truncate,
} from "../src/model";

function fixture<T>(name: string): T {
  return JSON.parse(
    readFileSync(new URL(`./fixtures/${name}`, import.meta.url), "utf-8"),
  ) as T;
}

const overview = fixture<OverviewResponse>("overview.json");
const NOW = new Date("2026-07-05T12:00:00Z");

describe("humanizeAgo", () => {
  it("formats minutes, hours, days", () => {
    expect(humanizeAgo("2026-07-05T11:57:00Z", NOW)).toBe("3m ago");
    expect(humanizeAgo("2026-07-05T09:00:00Z", NOW)).toBe("3h ago");
    expect(humanizeAgo("2026-07-01T12:00:00Z", NOW)).toBe("4d ago");
    expect(humanizeAgo(null, NOW)).toBe("fresh?");
  });
});

describe("projectView", () => {
  it("maps a detected project with errors to health=err", () => {
    const view = projectView(overview.projects[0], NOW);
    expect(view.health).toBe("err");
    expect(view.description).toContain("7t");
    expect(view.description).toContain("2e");
    expect(view.detected).toBe(true);
  });

  it("maps an undetected project to health=off", () => {
    const view = projectView(overview.projects[1], NOW);
    expect(view).toEqual({
      name: "Maestro",
      description: "not detected",
      health: "off",
      detected: false,
    });
  });
});

describe("detailLines", () => {
  it("summarizes counts, schema checks, warnings", () => {
    const detail = fixture<ProjectDetail>("project.json");
    const lines = detailLines(detail);
    expect(lines[0]).toBe("tasks: 1 · tests: 1 · models: 1 · configs: 1");
    expect(lines).toContain("schema arbiter.db: ok");
  });

  it("marks drift and unknown schema states", () => {
    const detail = fixture<ProjectDetail>("project.json");
    detail.schema_versions = [
      { database: "a.db", found: "2", expected: "1", ok: false },
      { database: "b.db", found: null, expected: "1", ok: null },
    ];
    detail.warnings = ["boom"];
    const lines = detailLines(detail);
    expect(lines).toContain("schema a.db: DRIFT");
    expect(lines).toContain("schema b.db: unknown");
    expect(lines).toContain("⚠ boom");
  });
});

describe("errors", () => {
  it("truncates at the web-parity limit", () => {
    expect(truncate("x".repeat(160))).toBe("x".repeat(160));
    expect(truncate("x".repeat(161))).toBe("x".repeat(160) + "…");
  });

  it("labels dated and undated events", () => {
    const [dated, undated] = fixture<ErrorEvent[]>("errors.json");
    expect(errorLabel(dated)).toBe("12:01 maestro — timeout in pipeline #42");
    expect(errorLabel(undated)).toBe(
      "— — — undated failure with [markup-looking] text",
    );
  });
});

describe("statusText", () => {
  it("counts detected projects and projects with errors", () => {
    expect(statusText(overview)).toBe("$(pulse) disp: 1✓ 1✗");
  });

  it("shows offline when there is no data", () => {
    expect(statusText(null)).toBe("$(debug-disconnected) disp: offline");
  });
});

describe("server decisions", () => {
  it("extracts the port", () => {
    expect(portFromUrl("http://127.0.0.1:8787")).toBe(8787);
    expect(portFromUrl("http://localhost")).toBe(8787);
    expect(portFromUrl("")).toBe(8787);
    expect(portFromUrl("not-a-url")).toBe(8787);
  });

  it("spawns only when unreachable+autoStart+projectDir+first try", () => {
    const base = {
      reachable: false,
      autoStart: true,
      projectDir: "/x",
      alreadyTried: false,
    };
    expect(shouldSpawn(base)).toBe(true);
    expect(shouldSpawn({ ...base, reachable: true })).toBe(false);
    expect(shouldSpawn({ ...base, autoStart: false })).toBe(false);
    expect(shouldSpawn({ ...base, projectDir: "  " })).toBe(false);
    expect(shouldSpawn({ ...base, alreadyTried: true })).toBe(false);
  });
});

describe("verdictText", () => {
  const sync = (top_line: string, fetching = false) => ({
    report: { current_host: "mac-a", top_line, top_reason: null },
    fetch_in_flight: fetching,
    last_fetch_at: null,
    last_fetch_error: null,
  });

  it("is empty when sync is unavailable (old server)", () => {
    expect(verdictText(null)).toBe("");
  });

  it("renders ok with a check icon", () => {
    expect(verdictText(sync("ok"))).toBe(" · $(check) ok");
  });

  it("renders pull-first with a warning icon", () => {
    expect(verdictText(sync("pull-first"))).toBe(" · $(warning) pull-first");
  });

  it("renders unknown with a question icon", () => {
    expect(verdictText(sync("unknown"))).toBe(" · $(question) unknown");
  });

  it("appends a spinner while the background fetch runs", () => {
    expect(verdictText(sync("ok", true))).toBe(" · $(check) ok $(sync~spin)");
  });
});
