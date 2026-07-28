import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { RoadmapResponse } from "../src/api";
import {
  evidenceLabel,
  evidenceSummary,
  roadmapEmptyText,
  roadmapItemChildren,
  roadmapItemDescription,
  roadmapItemLabel,
  roadmapStatusIcon,
} from "../src/model";

function fixture<T>(name: string): T {
  return JSON.parse(
    readFileSync(new URL(`./fixtures/${name}`, import.meta.url), "utf-8"),
  ) as T;
}

const roadmap = fixture<RoadmapResponse>("roadmap.json");
const [verified, implemented, planned, blocked, unknown, attested] =
  roadmap.items;

describe("roadmapStatusIcon", () => {
  it("distinguishes all five statuses", () => {
    expect(roadmapStatusIcon("verified")).toEqual({
      icon: "pass-filled",
      color: "testing.iconPassed",
    });
    expect(roadmapStatusIcon("implemented")).toEqual({
      icon: "circle-filled",
      color: "testing.iconPassed",
    });
    expect(roadmapStatusIcon("blocked")).toEqual({
      icon: "error",
      color: "testing.iconFailed",
    });
    expect(roadmapStatusIcon("planned")).toEqual({
      icon: "circle-outline",
      color: null,
    });
    expect(roadmapStatusIcon("unknown")).toEqual({
      icon: "question",
      color: null,
    });
  });

  it("maps drift to the error icon (web/TUI parity)", () => {
    expect(roadmapStatusIcon("drift")).toEqual({
      icon: "error",
      color: "testing.iconFailed",
    });
  });

  it("falls back to the unknown icon for unexpected statuses", () => {
    expect(roadmapStatusIcon("nonsense")).toEqual(roadmapStatusIcon("unknown"));
  });

  it("marks attested-only implementations amber, not machine-green", () => {
    const machine = roadmapStatusIcon("implemented");
    const attestedIcon = roadmapStatusIcon("implemented", true);
    expect(attestedIcon).toEqual({
      icon: "circle-filled",
      color: "list.warningForeground",
    });
    expect(attestedIcon).not.toEqual(machine);
  });

  it("keeps the base icon shape for attested verified (only color amber)", () => {
    // backend allows verified over an attested-only impl (PF-4); the verified
    // icon shape must survive so it stays distinct from implemented.
    const machine = roadmapStatusIcon("verified");
    const attestedIcon = roadmapStatusIcon("verified", true);
    expect(attestedIcon).toEqual({
      icon: "pass-filled",
      color: "list.warningForeground",
    });
    expect(attestedIcon.icon).toBe(machine.icon); // shape preserved
    expect(attestedIcon.color).not.toBe(machine.color); // color distinct
    // and distinct from an attested implemented (icon shape differs)
    expect(attestedIcon.icon).not.toBe(roadmapStatusIcon("implemented", true).icon);
  });
});

describe("roadmapItemLabel", () => {
  it("joins id and title like web/TUI item cells", () => {
    expect(roadmapItemLabel(verified)).toBe("RD-001 Contract sync checker");
  });

  it("trims when the title is empty", () => {
    expect(roadmapItemLabel({ ...verified, title: "" })).toBe("RD-001");
  });
});

describe("roadmapItemDescription", () => {
  it("mirrors Phase | Owner | Status | Blockers | Evidence", () => {
    expect(roadmapItemDescription(verified)).toBe(
      "M1 · dispatcher · verified · — · 2/2 rules",
    );
  });

  it("lists blockers and dashes missing fields", () => {
    expect(roadmapItemDescription(blocked)).toBe(
      "M3 · dispatcher · blocked · RD-003, RD-999 · 0/1 rules",
    );
    expect(roadmapItemDescription(unknown)).toBe(
      "— · — · unknown · — · no rules",
    );
  });

  it("renders the attested marker in the status column (ADR-ECO-005 D3)", () => {
    expect(roadmapItemDescription(attested)).toBe(
      "M4 · atp-platform · implemented (attested) · — · 1/1 rules",
    );
  });
});

describe("evidenceSummary", () => {
  it("counts passed over total rules (web/TUI parity)", () => {
    expect(evidenceSummary(verified)).toBe("2/2 rules");
    expect(evidenceSummary(implemented)).toBe("1/2 rules");
    expect(evidenceSummary(unknown)).toBe("no rules");
  });
});

describe("evidenceLabel", () => {
  it("shows rule, kind, and detail", () => {
    expect(evidenceLabel(implemented.evidence[1])).toBe(
      "work_item_chain [verification]: chain RD-002: 0 link(s), need 1",
    );
  });
});

describe("roadmapItemChildren", () => {
  it("returns one row per evidence rule", () => {
    const children = roadmapItemChildren(verified);
    expect(children).toHaveLength(2);
    expect(children.every((c) => c.kind === "evidence")).toBe(true);
  });

  it("prefixes a blockers line when the item is blocked", () => {
    const children = roadmapItemChildren(blocked);
    expect(children[0]).toEqual({
      kind: "line",
      text: "⛔ blocked by: RD-003, RD-999",
    });
    expect(children[1].kind).toBe("evidence");
  });

  it("degrades to a placeholder when there are no rules", () => {
    expect(roadmapItemChildren(unknown)).toEqual([
      { kind: "line", text: "no evidence rules" },
    ]);
  });

  it("passes evidence through for drill-down (passed/detail)", () => {
    const children = roadmapItemChildren(planned);
    expect(children).toEqual([
      { kind: "evidence", evidence: planned.evidence[0] },
    ]);
    expect(children[0].kind === "evidence" && children[0].evidence.passed).toBe(
      false,
    );
  });
});

describe("roadmapEmptyText", () => {
  it("is null when there are items", () => {
    expect(roadmapEmptyText(roadmap)).toBeNull();
  });

  it("reports no roadmaps found", () => {
    expect(roadmapEmptyText({ roadmaps: [], items: [], warnings: [] })).toBe(
      "no roadmaps found",
    );
  });

  it("reports empty roadmaps", () => {
    expect(
      roadmapEmptyText({ roadmaps: ["eco"], items: [], warnings: [] }),
    ).toBe("no roadmap items");
  });
});
