/** Orchestration-runs section of the project markdown doc (TODO
 * maestro-runs-panel-parity, #147).
 *
 * FORMATTING ONLY: statuses come from the server's Maestro collector,
 * which classified fail-closed — this module never re-classifies. Badge
 * words are identical across web/TUI/VSCode. Zero-state rule: no runs on
 * a CLEAN enumeration hides the section (most projects have none); any
 * `run `/`runs `-prefixed warning (the collector's degradation signal,
 * prefix pinned in dispatcher tests/test_maestro.py) forces it open and
 * reads as unknown, never as zero.
 */

import { mdEscape } from "./onboarding";

export interface OrchestrationRunInfo {
  repo_key?: string | null;
  run_id?: string | null;
  status?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  reason?: string | null;
  source?: string | null;
}

/** The subset of `GET /api/projects/{name}` the runs section reads. */
export interface RunsSnapshot {
  runs?: OrchestrationRunInfo[] | null;
  warnings?: string[] | null;
}

const DASH = "—";

const BADGES: Record<string, string> = {
  running: "▶ running",
  suspended: "⏸ suspended (waiting on a human)",
  interrupted: "⚠ interrupted / unknown",
  completed: "✅ completed",
  failed: "⛔ failed",
  cancelled: "∅ cancelled",
  superseded: "↻ superseded",
  legacy: "🗄 legacy (frozen pre-#147 file)",
  unreadable: "✖ unreadable",
};

function badge(status: string | null | undefined): string {
  const key = status ?? "";
  return BADGES[key] ?? `✖ ${mdEscape(key)}`;
}

export function runWarnings(snap: RunsSnapshot): string[] {
  return (snap.warnings ?? []).filter(
    (w) => w.startsWith("run ") || w.startsWith("runs "),
  );
}

/** `null` — nothing to render: the section is omitted entirely. */
export function renderRunsMarkdown(snap: RunsSnapshot): string | null {
  const runs = snap.runs ?? [];
  const degraded = runWarnings(snap);
  if (!runs.length && !degraded.length) {
    return null;
  }
  const lines: string[] = ["## Orchestration runs"];
  if (degraded.length) {
    lines.push(
      "",
      "**⚠ run enumeration degraded** — the list may be incomplete, " +
        "missing runs are unknown, not zero:",
      ...degraded.map((w) => `- ${mdEscape(w)}`),
    );
  }
  if (runs.length) {
    lines.push("");
    for (const r of runs) {
      const text = (x: string | null | undefined): string =>
        typeof x === "string" && x !== "" ? mdEscape(x) : DASH;
      lines.push(
        `- ${text(r.repo_key)} · ${text(r.run_id)} · ${badge(r.status)} ` +
          `(${text(r.started_at)} → ${r.ended_at ? mdEscape(r.ended_at) : "…"})` +
          (r.reason ? ` · ${mdEscape(r.reason)}` : ""),
      );
    }
  }
  return lines.join("\n");
}
