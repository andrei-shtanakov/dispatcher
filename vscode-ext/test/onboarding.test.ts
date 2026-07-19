import { describe, expect, it } from "vitest";
import {
  mdEscape,
  renderOnboardingMarkdown,
  type OnboardingView,
} from "../src/onboarding";

function base(): OnboardingView {
  return {
    project: {
      name: "arbiter",
      path: "/w/arbiter",
      description: "Routes agents.",
      description_source: "readme",
      freshness: "2026-07-18T10:00:00",
    },
    roadmap_position: {
      summary: {
        readiness: 0.5,
        done: 1,
        total: 2,
        lagging: true,
        contract_drift: false,
      },
      median_readiness: 0.75,
      phases: [{ phase: "1", counts: { planned: 1, verified: 1 } }],
    },
    next_items: [
      {
        id: "RD-2",
        title: "Blocked one",
        computed_status: "blocked",
        actionable: false,
        blocked_by: ["RD-9"],
      },
      {
        id: "RD-1",
        title: "Do it",
        computed_status: "planned",
        actionable: true,
        blocked_by: [],
      },
    ],
    live_tasks: [{ task_id: "T-1", status: "pending", title: "Live" }],
    warnings: ["unknown dependency id: RD-9 (item RD-2)"],
  };
}

describe("renderOnboardingMarkdown", () => {
  it("renders every section with both verdicts", () => {
    const md = renderOnboardingMarkdown(base());
    expect(md).toContain("# arbiter");
    expect(md).toContain("Routes agents.");
    expect(md).toContain("readiness 50% (1/2) · median 75% · **LAGGING**");
    expect(md).toContain("phase 1: planned=1, verified=1");
    expect(md).toContain("⛔ RD-2 · Blocked one · blocked — blocked by: RD-9");
    expect(md).toContain("▶ RD-1 · Do it · planned");
    expect(md).toContain("T-1 · pending · Live");
    expect(md).toContain("unknown dependency id");
  });

  it("REPRODUCES server order — never re-sorts (spec §1)", () => {
    const md = renderOnboardingMarkdown(base());
    // fixture deliberately puts the blocked item FIRST
    expect(md.indexOf("RD-2")).toBeLessThan(md.indexOf("RD-1"));
  });

  it("degrades: position null, empty lists, no description", () => {
    const md = renderOnboardingMarkdown({
      project: { name: "bare" },
      roadmap_position: null,
      next_items: [],
      live_tasks: [],
      warnings: [],
    });
    expect(md).toContain("# bare");
    expect(md).toContain("no roadmap items");
    expect(md).toContain("(none)");
    expect(md).not.toContain("undefined");
  });

  describe("missing-field cross matrix (mapper is total)", () => {
    const cases: Array<[string, OnboardingView]> = [
      [
        "item without computed_status but with blocked_by",
        {
          project: { name: "p" },
          next_items: [{ id: "X", blocked_by: ["Y"] }],
        },
      ],
      ["item without title or id", { project: { name: "p" }, next_items: [{}] }],
      [
        "position without median or phases",
        {
          project: { name: "p" },
          roadmap_position: { summary: { readiness: 0.1 } },
        },
      ],
      [
        "position summary missing entirely",
        { project: { name: "p" }, roadmap_position: {} },
      ],
      [
        "live task without title",
        {
          project: { name: "p" },
          live_tasks: [{ task_id: "T", status: "pending" }],
        },
      ],
      ["all optionals absent", { project: { name: "p" } }],
    ];
    for (const [label, view] of cases) {
      it(label, () => {
        const md = renderOnboardingMarkdown(view);
        expect(md).toContain("# p");
        expect(md).not.toContain("undefined");
        expect(md).not.toContain("null");
      });
    }
  });

  describe("escape matrix (markdown structure AND inline HTML)", () => {
    it("neutralizes an HTML injection vector", () => {
      const md = renderOnboardingMarkdown({
        project: { name: "p", description: '<img src=x onerror="alert(1)">' },
      });
      expect(md).not.toContain("<img");
      expect(md).toContain("&lt;img");
    });
    it("escapes markdown controls", () => {
      expect(mdEscape("a*b_c`d#e")).toBe("a\\*b\\_c\\`d\\#e");
    });
    it("escapes pipes (table safety)", () => {
      expect(mdEscape("a|b")).toBe("a\\|b");
    });
    it("neutralizes a link-breaker", () => {
      expect(mdEscape("x](http://evil)")).toBe("x\\]\\(http://evil\\)");
    });
    it("collapses newlines (list-item safety)", () => {
      expect(mdEscape("line1\nline2\r\nline3")).toBe("line1 line2 line3");
    });
    it("doubles backslashes BEFORE adding escapes", () => {
      expect(mdEscape("a\\*")).toBe("a\\\\\\*");
    });
    it("html-encodes ampersand and angle brackets", () => {
      expect(mdEscape("a&b<c>d")).toBe("a&amp;b&lt;c&gt;d");
    });
  });
});
