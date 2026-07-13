/** Pure view-model mappers and decisions. Must stay vscode-free. */

import type {
  ErrorEvent,
  EvidenceResult,
  OverviewEntry,
  OverviewResponse,
  ProjectDetail,
  RoadmapItemView,
  RoadmapResponse,
} from "./api";

export const MSG_LIMIT = 160; // same truncation as web and TUI

export type Health = "ok" | "err" | "off";

export interface ProjectView {
  name: string;
  description: string;
  health: Health;
  detected: boolean;
}

export function humanizeAgo(iso: string | null, now: Date): string {
  if (iso === null) {
    return "fresh?";
  }
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) {
    return "fresh?";
  }
  const minutes = Math.max(0, Math.floor((now.getTime() - then) / 60_000));
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  if (minutes < 60 * 24) {
    return `${Math.floor(minutes / 60)}h ago`;
  }
  return `${Math.floor(minutes / (60 * 24))}d ago`;
}

export function projectView(entry: OverviewEntry, now: Date): ProjectView {
  if (!entry.detected) {
    return {
      name: entry.name,
      description: "not detected",
      health: "off",
      detected: false,
    };
  }
  const tasks = entry.counts.tasks ?? 0;
  const errors = entry.counts.errors ?? 0;
  return {
    name: entry.name,
    description: `${tasks}t · ${errors}e · ${humanizeAgo(entry.freshness, now)}`,
    health: errors > 0 ? "err" : "ok",
    detected: true,
  };
}

export function detailLines(detail: ProjectDetail): string[] {
  const lines = [
    `tasks: ${detail.tasks.length} · tests: ${detail.test_results.length}` +
      ` · models: ${detail.models.length} · configs: ${detail.configs.length}`,
  ];
  for (const check of detail.schema_versions) {
    const state =
      check.ok === true ? "ok" : check.ok === false ? "DRIFT" : "unknown";
    lines.push(`schema ${check.database}: ${state}`);
  }
  for (const warning of detail.warnings) {
    lines.push(`⚠ ${warning}`);
  }
  return lines;
}

export function truncate(body: string, limit: number = MSG_LIMIT): string {
  return body.length <= limit ? body : body.slice(0, limit) + "…";
}

export function errorLabel(event: ErrorEvent): string {
  const time = event.timestamp === null ? "—" : event.timestamp.slice(11, 16);
  return `${time} ${event.service ?? "—"} — ${truncate(event.body, 80)}`;
}

export function statusText(overview: OverviewResponse | null): string {
  if (overview === null) {
    return "$(debug-disconnected) disp: offline";
  }
  const detected = overview.projects.filter((p) => p.detected);
  const withErrors = detected.filter((p) => (p.counts.errors ?? 0) > 0);
  return `$(pulse) disp: ${detected.length}✓ ${withErrors.length}✗`;
}

export interface StatusIcon {
  icon: string;
  color: string | null;
}

const ROADMAP_ICONS: Record<string, StatusIcon> = {
  verified: { icon: "pass-filled", color: "testing.iconPassed" },
  implemented: { icon: "circle-filled", color: "testing.iconPassed" },
  blocked: { icon: "error", color: "testing.iconFailed" },
  // drift is an error state on web/TUI too (rendered red) — keep parity.
  drift: { icon: "error", color: "testing.iconFailed" },
  planned: { icon: "circle-outline", color: null },
  unknown: { icon: "question", color: null },
};

export function roadmapStatusIcon(status: string): StatusIcon {
  return ROADMAP_ICONS[status] ?? ROADMAP_ICONS.unknown;
}

export function evidenceSummary(item: RoadmapItemView): string {
  const total = item.evidence.length;
  if (total === 0) {
    return "no rules";
  }
  const passed = item.evidence.filter((e) => e.passed).length;
  return `${passed}/${total} rules`;
}

export function roadmapItemLabel(item: RoadmapItemView): string {
  return `${item.id} ${item.title}`.trim();
}

/** Phase | Owner | Status | Blockers | Evidence — web/TUI column parity. */
export function roadmapItemDescription(item: RoadmapItemView): string {
  return [
    item.phase ?? "—",
    item.owner_project ?? "—",
    item.computed_status,
    item.blockers.length > 0 ? item.blockers.join(", ") : "—",
    evidenceSummary(item),
  ].join(" · ");
}

export function evidenceLabel(evidence: EvidenceResult): string {
  return `${evidence.rule} [${evidence.kind}]: ${evidence.detail}`;
}

export type RoadmapChild =
  | { kind: "evidence"; evidence: EvidenceResult }
  | { kind: "line"; text: string };

/** Drill-down rows for one item: blockers, then per-rule results. */
export function roadmapItemChildren(item: RoadmapItemView): RoadmapChild[] {
  const children: RoadmapChild[] = [];
  if (item.blockers.length > 0) {
    children.push({
      kind: "line",
      text: `⛔ blocked by: ${item.blockers.join(", ")}`,
    });
  }
  if (item.evidence.length === 0) {
    children.push({ kind: "line", text: "no evidence rules" });
  } else {
    for (const evidence of item.evidence) {
      children.push({ kind: "evidence", evidence });
    }
  }
  return children;
}

/** Placeholder text when there is nothing to render; null = has items. */
export function roadmapEmptyText(roadmap: RoadmapResponse): string | null {
  if (roadmap.items.length > 0) {
    return null;
  }
  return roadmap.roadmaps.length === 0
    ? "no roadmaps found"
    : "no roadmap items";
}

export function portFromUrl(url: string): number {
  try {
    const parsed = new URL(url);
    return parsed.port === "" ? 8787 : Number(parsed.port);
  } catch {
    return 8787;
  }
}

export function shouldSpawn(opts: {
  reachable: boolean;
  autoStart: boolean;
  projectDir: string;
  alreadyTried: boolean;
}): boolean {
  return (
    !opts.reachable &&
    opts.autoStart &&
    opts.projectDir.trim() !== "" &&
    !opts.alreadyTried
  );
}
