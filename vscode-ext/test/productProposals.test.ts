import { describe, expect, it } from "vitest";
import {
  composeProjectDoc,
  renderProductProposalsMarkdown,
  type ProductProposalsReport,
} from "../src/productProposals";

const WAIT = {
  proposal_id: "PP-101",
  gate_id: "qg5_business",
  gate_label: "Gate A",
  authority: "business_owner",
  artifact_ref: "proposal://PP-101",
  bundle_path: "pilot/pp-101",
  version: 6,
  proposal_updated_at: "2026-08-12T04:12:30Z",
};

const LOOP = {
  loop_id: "LOOP-101",
  iteration: 2,
  proposal_id: "PP-101",
  reason: "needs a human decision",
  stopped_at: "2026-08-12T05:00:00Z",
  bundle_path: "pilot/pp-101",
};

const OK_BUNDLE = {
  path: "pilot/pp-101",
  state: "ok",
  status: "ready_for_business",
  version: 6,
  loop_status: "absent",
};

const BAD_BUNDLE = {
  path: "pilot/pp-999",
  state: "unreadable",
  diagnostics: [{ code: "proposal-unreadable", message: "boom" }],
  loop_status: "unknown",
};

function report(extra: Partial<ProductProposalsReport>): ProductProposalsReport {
  return { mirror_path: "/w/impresario", ...extra };
}

describe("renderProductProposalsMarkdown", () => {
  it("renders waits, needs_human and the suppression note together", () => {
    const md = renderProductProposalsMarkdown(
      report({
        bundles: [OK_BUNDLE, BAD_BUNDLE],
        waits: [WAIT],
        needs_human: [LOOP],
        attention: true,
      }),
    );
    expect(md).toContain("Gate A");
    expect(md).toContain("business\\_owner"); // mdEscape applied
    expect(md).toContain("LOOP-101");
    expect(md).toContain("classification suppressed");
    expect(md).not.toContain("0 gates waiting");
    expect(md).not.toContain("0 loops waiting");
  });

  it("shows a confident zero only on a fully classified scan", () => {
    const md = renderProductProposalsMarkdown(report({ bundles: [OK_BUNDLE] }));
    expect(md).toContain("0 gates waiting");
    expect(md).toContain("0 loops waiting");
  });

  it("a non-ok bundle forbids the confident zero", () => {
    const md = renderProductProposalsMarkdown(
      report({ bundles: [BAD_BUNDLE], attention: true }),
    );
    expect(md).not.toContain("0 gates waiting");
    expect(md).not.toContain("0 loops waiting");
    expect(md).toContain("classification suppressed");
  });

  it("a report-level diagnostic forbids the confident zero", () => {
    const md = renderProductProposalsMarkdown(
      report({
        bundles: [OK_BUNDLE],
        diagnostics: [{ code: "scan-degraded", message: "one dir lost" }],
        attention: true,
      }),
    );
    expect(md).toContain("scan-degraded");
    expect(md).not.toContain("0 gates waiting");
    expect(md).not.toContain("0 loops waiting");
  });

  it("a healthy empty scan is exactly 0 bundles", () => {
    const md = renderProductProposalsMarkdown(report({}));
    expect(md).toContain("0 bundles");
    expect(md).not.toContain("0 gates waiting");
  });

  it("renders a 200 report WITH diagnostics fail-loud", () => {
    const md = renderProductProposalsMarkdown(
      report({
        diagnostics: [
          { code: "mirror-not-detected", message: "no impresario mirror" },
        ],
        attention: true,
      }),
    );
    expect(md).toContain("mirror-not-detected");
    expect(md).not.toContain("0 bundles");
  });
});

describe("composeProjectDoc", () => {
  const view = { project: { name: "impresario" } };

  it("keeps the proposals section when onboarding fails", () => {
    const doc = composeProjectDoc(
      "impresario",
      { error: "onboarding exploded" },
      { report: report({ bundles: [OK_BUNDLE] }) },
    );
    expect(doc).toContain("onboarding exploded");
    expect(doc).toContain("0 gates waiting");
  });

  it("keeps onboarding when the proposals request fails, fail-loud", () => {
    const doc = composeProjectDoc(
      "impresario",
      { view },
      { error: "connection refused" },
    );
    expect(doc).toContain("# impresario");
    expect(doc).toContain("Product proposals");
    expect(doc).toContain("connection refused");
  });

  it("hides the section entirely on 404 (not this kind of project)", () => {
    const doc = composeProjectDoc("arbiter", { view: { project: { name: "arbiter" } } }, { report: null });
    expect(doc).not.toContain("Product proposals");
  });
});
