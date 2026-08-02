import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type {
  ErrorEvent,
  OverviewResponse,
  ProjectDetail,
  RepoVerdict,
  SyncStatusResponse,
} from "../src/api";
import {
  detailLines,
  errorLabel,
  humanizeAgo,
  portFromUrl,
  projectView,
  shouldSpawn,
  statusText,
  syncAgeLabel,
  syncItemContext,
  syncVerdictIcon,
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
    report: {
      current_host: "mac-a",
      top_line,
      top_reason: null,
      hosts: [],
      proposals: [],
      warnings: [],
    },
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

  it("renders sync-first with a warning icon", () => {
    expect(verdictText(sync("sync-first"))).toBe(" · $(warning) sync-first");
  });

  it("renders unknown with a question icon", () => {
    expect(verdictText(sync("unknown"))).toBe(" · $(question) unknown");
  });

  it("renders no-data with the same question icon as unknown", () => {
    expect(verdictText(sync("no-data"))).toBe(" · $(question) no-data");
  });

  it("appends a spinner while the background fetch runs", () => {
    expect(verdictText(sync("ok", true))).toBe(" · $(check) ok $(sync~spin)");
  });
});

describe("syncItemContext (web/TUI parity)", () => {
  // pull ⇔ live sync-first row with truthy `behind` (a dirty-only or
  // ahead-only row has nothing a fast-forward pull can fix); open PR ⇔ live
  // sync-first row with truthy `ahead`, independent of pull. Every
  // combination of (behind, ahead) is pinned below, plus the gates that stay
  // unconditional on either button: live and verdict.
  const v = (o: Partial<RepoVerdict>): RepoVerdict => ({
    repo: "a",
    verdict: "ok",
    reason: null,
    branch: null,
    ahead: null,
    behind: null,
    dirty: false,
    is_kb: false,
    ...o,
  });

  it("behind-only -> pull only", () => {
    expect(
      syncItemContext(
        v({ verdict: "sync-first", behind: 2, ahead: null }),
        true,
      ),
    ).toBe("dispatcherSyncVerdict.pull");
    expect(
      syncItemContext(v({ verdict: "sync-first", behind: 2, ahead: 0 }), true),
    ).toBe("dispatcherSyncVerdict.pull");
  });
  it("ahead-only -> PR only, no pull — the fourth combination", () => {
    expect(
      syncItemContext(
        v({ verdict: "sync-first", behind: null, ahead: 2 }),
        true,
      ),
    ).toBe("dispatcherSyncVerdict.pr");
    expect(
      syncItemContext(v({ verdict: "sync-first", behind: 0, ahead: 2 }), true),
    ).toBe("dispatcherSyncVerdict.pr");
  });
  it("both behind and ahead -> pullPr (both actions)", () => {
    expect(
      syncItemContext(v({ verdict: "sync-first", behind: 1, ahead: 2 }), true),
    ).toBe("dispatcherSyncVerdict.pullPr");
  });
  it("neither behind nor ahead (e.g. dirty-only) -> null", () => {
    expect(
      syncItemContext(v({ verdict: "sync-first", behind: 0, ahead: 0 }), true),
    ).toBe(null);
  });
  it("behind/ahead unknown (null) -> null — unknown is not \"behind\"", () => {
    expect(
      syncItemContext(
        v({ verdict: "sync-first", behind: null, ahead: null }),
        true,
      ),
    ).toBe(null);
  });
  it("non-live -> null even with both numbers truthy", () => {
    expect(
      syncItemContext(
        v({ verdict: "sync-first", behind: 2, ahead: 2 }),
        false,
      ),
    ).toBe(null);
  });
  it("non-sync-first verdict -> null even with both numbers truthy", () => {
    expect(
      syncItemContext(v({ verdict: "ok", behind: 2, ahead: 2 }), true),
    ).toBe(null);
  });
});

// DESIGN-202 wire contract, consumer side: the producer (dispatcher/core/
// sync.py) can emit exactly ok | sync-first | no-data | unknown. This repo
// has no shared enum to import, so the guard below is a fixture/icon-map
// parity check instead — it fails the way this rename would have: a fixture
// or map still saying the old string while the other side moved on, falling
// back to a default icon silently rather than loudly.
describe("sync verdict vocabulary (fixture / icon-map parity)", () => {
  const CANONICAL_VERDICTS = ["ok", "sync-first", "no-data", "unknown"];

  it("syncVerdictIcon has an explicit icon for ok and sync-first, and the "
    + "same default fallback for no-data/unknown/anything unrecognized", () => {
    const fallback = syncVerdictIcon("__no_such_verdict__");
    for (const verdict of CANONICAL_VERDICTS) {
      const icon = syncVerdictIcon(verdict);
      if (verdict === "ok" || verdict === "sync-first") {
        expect(icon).not.toEqual(fallback);
      } else {
        expect(icon).toEqual(fallback);
      }
    }
  });

  it("the sync_full fixture only uses verdicts from the canonical set", () => {
    const sync = fixture<SyncStatusResponse>("sync_full.json");
    const seen = new Set(
      sync.report.hosts.flatMap((h) => h.verdicts.map((v) => v.verdict)),
    );
    seen.add(sync.report.top_line);
    for (const verdict of seen) {
      expect(CANONICAL_VERDICTS).toContain(verdict);
    }
  });
});

describe("syncAgeLabel (TUI _age_cell parity)", () => {
  it("renders seconds under the 90s threshold", () => {
    expect(syncAgeLabel(45, false)).toBe("45s");
  });
  it("renders minutes under the 5400s threshold", () => {
    expect(syncAgeLabel(120, false)).toBe("2m");
  });
  it("renders hours with one decimal past the threshold", () => {
    expect(syncAgeLabel(7200, false)).toBe("2.0h");
  });
  it("renders a placeholder for missing age", () => {
    expect(syncAgeLabel(null, false)).toBe("—");
  });
  it("appends a stale suffix", () => {
    expect(syncAgeLabel(45, true)).toBe("45s stale");
    expect(syncAgeLabel(null, true)).toBe("—");
  });
});
