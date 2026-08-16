/** Product-proposals section of the project markdown doc (TODO
 * product-proposal-parity).
 *
 * FORMATTING ONLY: states, waits and diagnostics come from the server's
 * collector (inbox #129) — this module never re-classifies. Zero-state
 * rule (refined for parity): «0 bundles» only on a healthy empty scan;
 * «0 gates/loops waiting» only when at least one bundle exists, ALL
 * bundles are ok AND there is no report-level diagnostic — anything
 * less renders as incomplete, never as a confident global zero.
 */

import { mdEscape, renderOnboardingMarkdown } from "./onboarding";
import type { OnboardingView } from "./onboarding";
import { renderRunsMarkdown } from "./runs";
import type { RunsSnapshot } from "./runs";

export interface PpDiagnostic {
  code?: string | null;
  message?: string | null;
  path?: string | null;
}

export interface PpGateWait {
  proposal_id?: string | null;
  gate_id?: string | null;
  gate_label?: string | null;
  authority?: string | null;
  artifact_ref?: string | null;
  bundle_path?: string | null;
  version?: number | null;
  proposal_updated_at?: string | null;
}

export interface PpLoopWait {
  loop_id?: string | null;
  iteration?: number | null;
  proposal_id?: string | null;
  reason?: string | null;
  stopped_at?: string | null;
  bundle_path?: string | null;
}

export interface PpBundle {
  path?: string | null;
  state?: string | null;
  diagnostics?: PpDiagnostic[] | null;
  status?: string | null;
  version?: number | null;
  loop_status?: string | null;
}

export interface ProductProposalsReport {
  mirror_path?: string | null;
  bundles?: PpBundle[] | null;
  waits?: PpGateWait[] | null;
  needs_human?: PpLoopWait[] | null;
  diagnostics?: PpDiagnostic[] | null;
  attention?: boolean | null;
}

const DASH = "—";

function text(x: string | null | undefined): string {
  return typeof x === "string" && x !== "" ? mdEscape(x) : DASH;
}

/** '✅ ok' is the only green word — any other state reads as
 * «classification suppressed», never as zero waits. */
function badge(state: string | null | undefined): string {
  if (state === "ok") {
    return "✅ ok";
  }
  const marks: Record<string, string> = {
    unreadable: "✖",
    unknown: "❓",
    conflict: "⛔",
  };
  const mark = marks[state ?? ""] ?? "✖";
  return `${mark} classification suppressed — ${text(state)}`;
}

export function renderProductProposalsMarkdown(
  report: ProductProposalsReport,
): string {
  const bundles = report.bundles ?? [];
  const diagnostics = report.diagnostics ?? [];
  const waits = report.waits ?? [];
  const loops = report.needs_human ?? [];
  const suppressed = bundles.filter((b) => b.state !== "ok").length;
  const certain = bundles.length > 0 && suppressed === 0 && !diagnostics.length;
  const note =
    suppressed > 0
      ? [
          `⚠ ${suppressed} bundle(s): classification suppressed — ` +
            "their waits are unknown, not zero",
        ]
      : [];
  const zeroState = (label: string): string[] => {
    if (certain) {
      return [label];
    }
    if (bundles.length === 0 && diagnostics.length === 0) {
      return ["0 bundles"];
    }
    return ["classification incomplete — waits unknown, not zero"];
  };

  const lines: string[] = ["## Product proposals"];
  if (diagnostics.length) {
    lines.push("");
    for (const d of diagnostics) {
      lines.push(
        `- ⚠ **${text(d.code)}**: ${text(d.message)}` +
          (d.path ? ` · ${mdEscape(d.path)}` : ""),
      );
    }
  }

  lines.push("", "### Gate waits", "");
  const gateLines = note.concat(
    waits.map(
      (w) =>
        `- ${text(w.artifact_ref)} · ${text(w.gate_label)} ` +
        `(${text(w.gate_id)}) · **${text(w.authority)}** · ` +
        `v${w.version ?? DASH} · updated ${text(w.proposal_updated_at)} · ` +
        `${text(w.bundle_path)}`,
    ),
  );
  lines.push(...(gateLines.length ? gateLines : zeroState("0 gates waiting")));

  lines.push("", "### Loop waits (needs_human)", "");
  const loopLines = note.concat(
    loops.map(
      (l) =>
        `- ${text(l.loop_id)} · iteration ${l.iteration ?? DASH} · ` +
        `${text(l.reason)} · stopped ${text(l.stopped_at)} · ` +
        `${text(l.bundle_path)}`,
    ),
  );
  lines.push(...(loopLines.length ? loopLines : zeroState("0 loops waiting")));

  if (bundles.length) {
    lines.push("", "### Bundles", "");
    for (const b of bundles) {
      lines.push(
        `- ${text(b.path)} · ${badge(b.state)}` +
          (b.status ? ` · ${mdEscape(b.status)}` : "") +
          (b.version != null ? ` · v${b.version}` : "") +
          ` · loop: ${text(b.loop_status)}`,
      );
      for (const d of b.diagnostics ?? []) {
        lines.push(
          `  - ${text(d.code)}: ${text(d.message)}` +
            (d.path ? ` · ${mdEscape(d.path)}` : ""),
        );
      }
    }
  }
  return lines.join("\n");
}

/** The whole project doc from INDEPENDENT fetches: one failing must
 * not take the others down. `report`/`snap: null` means 404 — «not this
 * kind of project / unknown project» — and hides the section; an error
 * string is rendered fail-loud instead of silently dropping the section
 * (for runs: unknown must not look like «no runs»). */
export function composeProjectDoc(
  name: string,
  onboarding: { view?: OnboardingView; error?: string },
  proposals: { report?: ProductProposalsReport | null; error?: string },
  runs?: { snap?: RunsSnapshot | null; error?: string },
): string {
  const parts: string[] = [];
  if (onboarding.view !== undefined) {
    parts.push(renderOnboardingMarkdown(onboarding.view));
  } else {
    parts.push(
      `# ${mdEscape(name)}`,
      `⚠ onboarding failed: ${text(onboarding.error)}`,
    );
  }
  if (proposals.error !== undefined) {
    parts.push(
      "## Product proposals",
      `⚠ product-proposals failed: ${text(proposals.error)}`,
    );
  } else if (proposals.report != null) {
    parts.push(renderProductProposalsMarkdown(proposals.report));
  }
  if (runs?.error !== undefined) {
    parts.push("## Orchestration runs", `⚠ runs failed: ${text(runs.error)}`);
  } else if (runs?.snap != null) {
    const section = renderRunsMarkdown(runs.snap);
    if (section !== null) {
      parts.push(section);
    }
  }
  return parts.join("\n\n");
}
