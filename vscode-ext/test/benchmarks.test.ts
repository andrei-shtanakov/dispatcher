import { describe, expect, it } from "vitest";
import type {
  BenchmarkInfo,
  BenchmarksStatusResponse,
  LeaderboardState,
} from "../src/api";
import {
  benchmarkChildNodes,
  benchmarkDescription,
  benchmarkRootNodes,
  leaderboardRowDescription,
} from "../src/model";

const BENCH: BenchmarkInfo = {
  id: 7,
  name: "atp-core",
  description: "core suite",
  tasks_count: 42,
  tags: ["core", "regression"],
  version: "1.2",
  family_tag: null,
  created_at: "2026-08-01T00:00:00Z",
};

const status = (
  report: Partial<BenchmarksStatusResponse["report"]>,
): BenchmarksStatusResponse => ({
  report: {
    status: "ok",
    url: "http://atp.test",
    fetched_at: "2026-08-16T10:00:00Z",
    error: null,
    benchmarks: [],
    leaderboards: {},
    ...report,
  },
  fetch_in_flight: false,
});

describe("benchmarkRootNodes (cross-surface zero-state rules)", () => {
  it("offline (null) is its own node, never an empty list", () => {
    expect(benchmarkRootNodes(null)).toEqual([{ kind: "offline" }]);
  });

  it("ok + empty is a CONFIDENT «0 benchmarks»", () => {
    expect(benchmarkRootNodes(status({}))).toEqual([
      { kind: "state", text: "0 benchmarks", warn: false },
    ]);
  });

  it("unavailable with an error is explicit unknown, warn-flagged", () => {
    const nodes = benchmarkRootNodes(
      status({
        status: "unavailable",
        error: "ConnectError: refused",
      }),
    );
    expect(nodes).toHaveLength(1);
    expect(nodes[0]).toMatchObject({ kind: "state", warn: true });
    const text = (nodes[0] as { text: string }).text;
    expect(text).toContain("benchmarks unknown: unavailable");
    expect(text).toContain("ConnectError");
  });

  it("the not-fetched-yet triple reads as such, not as a failure", () => {
    const nodes = benchmarkRootNodes(
      status({ status: "unavailable", fetched_at: null, error: null }),
    );
    expect(nodes).toEqual([
      { kind: "state", text: "not fetched yet", warn: false },
    ]);
  });

  it("ok with benchmarks yields one bench node each", () => {
    const nodes = benchmarkRootNodes(status({ benchmarks: [BENCH] }));
    expect(nodes).toEqual([{ kind: "bench", bench: BENCH }]);
  });
});

describe("benchmarkChildNodes", () => {
  const lb = (state: Partial<LeaderboardState>): LeaderboardState => ({
    status: "ok",
    rows: [],
    error: null,
    ...state,
  });

  it("ok + empty leaderboard is a CONFIDENT «0 entries»", () => {
    const s = status({ benchmarks: [BENCH], leaderboards: { "7": lb({}) } });
    expect(benchmarkChildNodes(s, BENCH)).toEqual([
      { kind: "state", text: "0 entries", warn: false },
    ]);
  });

  it("non-ok leaderboard is explicit unknown, never «0 entries»", () => {
    const s = status({
      benchmarks: [BENCH],
      leaderboards: {
        "7": lb({ status: "unavailable", error: "timeout" }),
      },
    });
    const nodes = benchmarkChildNodes(s, BENCH);
    expect(nodes[0]).toMatchObject({ kind: "state", warn: true });
    const text = (nodes[0] as { text: string }).text;
    expect(text).toContain("leaderboard unknown (unavailable)");
    expect(text).toContain("timeout");
    expect(text).not.toContain("0 entries");
  });

  it("a null error carries no trailing colon (Copilot review PR #153)", () => {
    const s = status({
      benchmarks: [BENCH],
      leaderboards: { "7": lb({ status: "unreadable", error: null }) },
    });
    const text = (benchmarkChildNodes(s, BENCH)[0] as { text: string }).text;
    expect(text).toBe("leaderboard unknown (unreadable)");
  });

  it("a missing leaderboard entry is unknown, not empty", () => {
    const s = status({ benchmarks: [BENCH] });
    expect(benchmarkChildNodes(s, BENCH)).toEqual([
      { kind: "state", text: "leaderboard unknown", warn: true },
    ]);
  });

  it("rows map to lbrow nodes", () => {
    const row = { user_id: 1, agent_name: "bot", best_score: 0.9, run_count: 3 };
    const s = status({
      benchmarks: [BENCH],
      leaderboards: { "7": lb({ rows: [row] }) },
    });
    expect(benchmarkChildNodes(s, BENCH)).toEqual([{ kind: "lbrow", row }]);
  });
});

describe("labels", () => {
  it("bench description carries tasks and tags", () => {
    expect(benchmarkDescription(BENCH)).toBe("42 tasks · core, regression");
  });

  it("leaderboard row description is one line", () => {
    expect(
      leaderboardRowDescription({
        user_id: 1,
        agent_name: "bot",
        best_score: 0.9,
        run_count: 3,
      }),
    ).toBe("best 0.9 · 3 runs · user 1");
  });
});
