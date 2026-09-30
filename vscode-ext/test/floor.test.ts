import { describe, expect, it } from "vitest";
import type { FactoryFloorView, InFlightRun } from "../src/api";
import {
  floorEmptyText,
  floorGroups,
  launchLine,
  prepareRunEnd,
  runDescription,
  runAge,
  runLabel,
} from "../src/floor";

const NOW = new Date("2026-09-30T12:00:00Z");

function run(runId: string, extra: Partial<InFlightRun> = {}): InFlightRun {
  return {
    repo_key: "github.com/andrei-shtanakov/deployer",
    run_id: runId,
    status: "interrupted",
    started_at: "2026-08-24T07:29:18+00:00",
    last_activity_at: "2026-08-24T08:00:00+00:00",
    request_id: null,
    work_id: null,
    stale: false,
    act: null,
    ...extra,
  };
}

function view(runs: InFlightRun[], complete = true): FactoryFloorView {
  return { in_flight: runs, sources: {}, complete, generated_at: NOW.toISOString() };
}

const ACT = {
  kind: "maestro_run_end" as const,
  run_id: "01M0SARX",
  repo_key: "github.com/andrei-shtanakov/deployer",
  maestro_home: "/Users/me/.maestro",
  maestro_cli: "/ws/maestro/.venv/bin/maestro",
};

describe("factory floor", () => {
  it("puts stale runs first and keeps server order inside", () => {
    const groups = floorGroups(
      view([run("a", { stale: true }), run("b"), run("c", { stale: true })]),
    );
    expect(groups.map((g) => g.runs.map((r) => r.run_id))).toEqual([
      ["a", "c"],
      ["b"],
    ]);
  });

  it("never says nothing runs while a source is unread", () => {
    expect(floorEmptyText(view([], false))).toBe("no known runs — sources incomplete");
    expect(floorEmptyText(view([]))).toBe("nothing is running");
    expect(floorEmptyText(view([run("a")]))).toBeNull();
  });

  it("labels by repo and work item, ages honestly", () => {
    expect(runLabel(run("01X", { work_id: "todo://deployer/x" }))).toBe(
      "deployer · todo://deployer/x",
    );
    expect(runLabel(run("01X"))).toBe("deployer · 01X");
    expect(runAge(run("a"), NOW)).toBe("37d");
    expect(runAge(run("a", { started_at: null }), NOW)).toBe("age unknown");
  });

  it("prepares run-end with the outcome the human chose, never executed", () => {
    const p = prepareRunEnd(ACT, "superseded", "done elsewhere");
    expect(p.kind === "terminal" && p.text).toBe(
      "MAESTRO_HOME=/Users/me/.maestro /ws/maestro/.venv/bin/maestro run-end " +
        "01M0SARX --outcome superseded --reason 'done elsewhere'",
    );
    expect(p.kind === "terminal" && p.note).toContain("deployer");
  });

  it("quotes a reason the shell would otherwise split or glob", () => {
    // The exact reason that broke live on 2026-09-30: zsh read `(...)` as a
    // glob qualifier and `;` as a second command.
    const reason = "died at start (tasks never ran); work completed by 01M0SE57";
    const p = prepareRunEnd(ACT, "superseded", reason);
    expect(p.kind === "terminal" && p.text).toContain(
      "--reason 'died at start (tasks never ran); work completed by 01M0SE57'",
    );
  });

  it("escapes a single quote inside the reason", () => {
    const p = prepareRunEnd(ACT, "cancelled", "wasn't needed");
    expect(p.kind === "terminal" && p.text).toContain(
      "--reason 'wasn'\\''t needed'",
    );
  });

  it("refuses an empty reason and control characters in it", () => {
    expect(prepareRunEnd(ACT, "cancelled", "   ").kind).toBe("refused");
    expect(prepareRunEnd(ACT, "cancelled", "a\nrm -rf /").kind).toBe("refused");
  });

  it("refuses control characters", () => {
    expect(prepareRunEnd({ ...ACT, run_id: "x\nrm" }, "cancelled", "r").kind).toBe(
      "refused",
    );
  });
});

describe("factory floor wording (review on #282)", () => {
  it("shows idle time, which is what decides stale", () => {
    expect(runDescription(run("a"), NOW)).toBe("interrupted · started 37d · idle 37d");
    expect(runDescription(run("a", { last_activity_at: null }), NOW)).toContain(
      "idle unknown",
    );
  });

  it("never claims 'not launched by dispatcher' when its records were not read", () => {
    expect(launchLine(run("a"), true)).toBe(
      "no dispatcher launch record carries this run id",
    );
    expect(launchLine(run("a"), false)).toContain("unknown");
    expect(launchLine(run("a", { request_id: "rc-1" }), false)).toBe(
      "launched by dispatcher: rc-1",
    );
  });
});
