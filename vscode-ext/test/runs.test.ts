import { describe, expect, it } from "vitest";
import { composeProjectDoc } from "../src/productProposals";
import {
  renderRunsMarkdown,
  runWarnings,
  type RunsSnapshot,
} from "../src/runs";

const RUN = {
  repo_key: "github.com/acme/app",
  run_id: "01NEW",
  status: "interrupted",
  started_at: "2026-08-12T00:00:00",
  ended_at: null,
  reason: null,
  source: "/x/state.db",
};

const LEGACY = {
  repo_key: "legacy",
  run_id: null,
  status: "legacy",
  started_at: null,
  ended_at: null,
  reason: null,
  source: "/x/maestro.db",
};

const snap = (extra: Partial<RunsSnapshot> = {}): RunsSnapshot => ({
  runs: [],
  warnings: [],
  ...extra,
});

describe("renderRunsMarkdown", () => {
  it("renders repo_key, run_id, badge and dates off one screen", () => {
    const out = renderRunsMarkdown(snap({ runs: [RUN, LEGACY] }));
    expect(out).not.toBeNull();
    expect(out).toContain("## Orchestration runs");
    expect(out).toContain("github.com/acme/app");
    expect(out).toContain("01NEW");
    // No terminal record renders as interrupted / unknown, never
    // in-progress (the load-bearing point of #147).
    expect(out).toContain("⚠ interrupted / unknown");
    expect(out).toContain("🗄 legacy (frozen pre-#147 file)");
  });

  it("returns null on zero runs with a clean enumeration", () => {
    expect(renderRunsMarkdown(snap())).toBeNull();
  });

  it("a degradation warning forces the section open as unknown", () => {
    const out = renderRunsMarkdown(
      snap({
        warnings: ["runs enumeration: cannot list /x/projects: denied"],
      }),
    );
    expect(out).not.toBeNull();
    expect(out).toContain("run enumeration degraded");
    expect(out).toContain("unknown, not zero");
    expect(out).toContain("cannot list /x/projects");
  });

  it("non-run warnings are not a degradation signal", () => {
    const s = snap({ warnings: ["maestro.db not found (~/.maestro/...)"] });
    expect(runWarnings(s)).toEqual([]);
    expect(renderRunsMarkdown(s)).toBeNull();
  });

  it("an unrecognized status gets the ✖ fallback, verbatim", () => {
    const out = renderRunsMarkdown(
      snap({ runs: [{ ...RUN, status: "weird-new-state" }] }),
    );
    expect(out).toContain("✖ weird-new-state");
  });

  it("hostile producer strings arrive markdown-escaped", () => {
    const out = renderRunsMarkdown(
      snap({ runs: [{ ...RUN, repo_key: "evil *bold* [link](x)" }] }),
    );
    expect(out).not.toContain("*bold*");
    expect(out).not.toContain("[link](x)");
  });
});

describe("composeProjectDoc runs section", () => {
  const onboarding = { error: "down" };
  const proposals = { report: null };

  it("renders the section from a snapshot with runs", () => {
    const doc = composeProjectDoc("Maestro", onboarding, proposals, {
      snap: snap({ runs: [RUN] }),
    });
    expect(doc).toContain("## Orchestration runs");
    expect(doc).toContain("01NEW");
  });

  it("omits the section on 404 (snap: null) and on clean zero", () => {
    const on404 = composeProjectDoc("widget", onboarding, proposals, {
      snap: null,
    });
    expect(on404).not.toContain("Orchestration runs");
    const onZero = composeProjectDoc("widget", onboarding, proposals, {
      snap: snap(),
    });
    expect(onZero).not.toContain("Orchestration runs");
  });

  it("a fetch failure is fail-loud, never «no runs»", () => {
    const doc = composeProjectDoc("Maestro", onboarding, proposals, {
      error: "ECONNREFUSED",
    });
    expect(doc).toContain("⚠ runs failed: ECONNREFUSED");
  });

  it("stays backward-compatible when the runs argument is omitted", () => {
    const doc = composeProjectDoc("Maestro", onboarding, proposals);
    expect(doc).not.toContain("Orchestration runs");
  });
});
