import { describe, expect, it } from "vitest";
import type { HaltView } from "../src/api";
import { haltLines, haltSummary, reasonProblem } from "../src/halt";

const ok = { state: "ok" as const, detail: null };

function view(extra: Partial<HaltView> = {}): HaltView {
  return {
    fleet: [
      { repo: "alpha", state: "off", ruleset_id: 1, detail: null },
      { repo: "beta", state: "off", ruleset_id: 2, detail: null },
    ],
    applying: null,
    halted: 0,
    unhealthy: [],
    deviations: [],
    last_request: null,
    sources: { forge_halt: ok, halt_requests: ok },
    complete: true,
    generated_at: "2026-09-30T12:00:00Z",
    ...extra,
  };
}

describe("halt summary", () => {
  it("unknown is never off", () => {
    expect(haltSummary(null).tone).toBe("unknown");
    expect(haltSummary(view({ fleet: [] })).label).toBe("Halt: reading…");
  });
  it("off only when every repo is confirmed off", () => {
    expect(haltSummary(view())).toEqual({
      label: "Halt: off (2 repos confirmed)",
      tone: "clear",
    });
    const shaky = view({
      fleet: [
        { repo: "alpha", state: "off", ruleset_id: 1, detail: null },
        { repo: "beta", state: "missing", ruleset_id: null, detail: "never armed" },
      ],
      unhealthy: ["beta"],
    });
    expect(haltSummary(shaky)).toEqual({
      label: "Halt: off, 1/2 not confirmed",
      tone: "unhealthy",
    });
  });
  it("on wins, and applying is said", () => {
    expect(haltSummary(view({ halted: 1, applying: "r1" })).label).toBe(
      "Halt: ON for 1/2 · applying…",
    );
  });
  it("an empty fleet says it is not configured", () => {
    const off = view({
      fleet: [],
      sources: { forge_halt: { state: "not_configured", detail: "x" } },
    });
    expect(haltSummary(off).tone).toBe("off-config");
  });
});

describe("halt lines", () => {
  it("lists repos, deviations and the last request's outcome", () => {
    const lines = haltLines(
      view({
        deviations: ["gamma: joined the fleet after the last fleet request"],
        last_request: {
          request_id: "r",
          at: "t",
          target: "on",
          scope: "fleet",
          repos: ["alpha", "beta"],
          fleet_at_request: ["alpha", "beta"],
          reason: "incident",
          principal: "p",
          results: [
            { repo: "alpha", ok: true, changed: true, state: "on", error: null },
            { repo: "beta", ok: false, changed: false, state: "off", error: "403" },
          ],
        },
      }),
    );
    expect(lines).toEqual([
      "alpha: off",
      "beta: off",
      "deviation: gamma: joined the fleet after the last fleet request",
      "last request: on (fleet) — incident — not confirmed: beta",
    ]);
  });
});

describe("reason", () => {
  it("is required, one line, bounded", () => {
    expect(reasonProblem("incident: bad merges")).toBeNull();
    expect(reasonProblem("  ")).not.toBeNull();
    expect(reasonProblem("a\nb")).not.toBeNull();
    expect(reasonProblem("x".repeat(501))).not.toBeNull();
  });
});
