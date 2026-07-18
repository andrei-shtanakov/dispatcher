/** FR-04 third thin renderer (DESIGN-1101).
 *
 * FORMATTING ONLY: every verdict, aggregate and the next_items order come
 * from the server's build_onboarding (spec §1) — this module never
 * computes or re-sorts. All model strings pass mdEscape (two layers:
 * markdown structure + inline HTML; the preview renders both).
 */

export interface OnboardingProject {
  name: string;
  path?: string | null;
  description?: string | null;
  description_source?: string | null;
  freshness?: string | null;
}

export interface OnboardingSummary {
  readiness?: number | null;
  done?: number | null;
  total?: number | null;
  lagging?: boolean | null;
  contract_drift?: boolean | null;
}

export interface OnboardingPhase {
  phase?: string | null;
  counts?: Record<string, number> | null;
}

export interface OnboardingPosition {
  summary?: OnboardingSummary | null;
  median_readiness?: number | null;
  phases?: OnboardingPhase[] | null;
}

export interface OnboardingNextItem {
  id?: string | null;
  title?: string | null;
  computed_status?: string | null;
  actionable?: boolean | null;
  blocked_by?: string[] | null;
}

export interface OnboardingTask {
  task_id?: string | null;
  status?: string | null;
  title?: string | null;
}

export interface OnboardingView {
  project: OnboardingProject;
  roadmap_position?: OnboardingPosition | null;
  next_items?: OnboardingNextItem[] | null;
  live_tasks?: OnboardingTask[] | null;
  warnings?: string[] | null;
}

const DASH = "—";

/** Spec's exact 4-step algorithm; order is significant. */
export function mdEscape(value: string): string {
  return value
    .replace(/\\/g, "\\\\")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/[`*_[\]()#|]/g, (ch) => `\\${ch}`)
    .replace(/\r\n|\r|\n/g, " ");
}

function pct(x: number | null | undefined): string {
  return typeof x === "number" ? `${Math.round(x * 100)}%` : DASH;
}

function text(x: string | null | undefined): string {
  return typeof x === "string" && x !== "" ? mdEscape(x) : DASH;
}

export function renderOnboardingMarkdown(view: OnboardingView): string {
  const p = view.project;
  const lines: string[] = [`# ${mdEscape(p.name)}`];
  const meta = [
    p.path ? mdEscape(p.path) : null,
    p.freshness ? `freshness: ${mdEscape(p.freshness)}` : null,
  ].filter((x): x is string => x !== null);
  if (meta.length) {
    lines.push("", meta.join(" · "));
  }

  lines.push("", "## Description", "");
  lines.push(
    p.description
      ? mdEscape(p.description) +
          (p.description_source ? ` (${mdEscape(p.description_source)})` : "")
      : `${DASH} (no description)`,
  );

  lines.push("", "## Roadmap position", "");
  const pos = view.roadmap_position;
  const s = pos?.summary;
  if (s) {
    const flags = [
      s.lagging === true ? "**LAGGING**" : null,
      s.contract_drift === true ? "**CONTRACT DRIFT**" : null,
    ].filter((x): x is string => x !== null);
    lines.push(
      `readiness ${pct(s.readiness)} (${s.done ?? DASH}/${s.total ?? DASH})` +
        ` · median ${pct(pos?.median_readiness)}` +
        (flags.length ? ` · ${flags.join(" · ")}` : ""),
    );
    for (const ph of pos?.phases ?? []) {
      // alphabetical join is presentation, not aggregation (spec §1)
      const counts = Object.entries(ph.counts ?? {})
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([k, v]) => `${mdEscape(k)}=${v}`)
        .join(", ");
      lines.push(`- phase ${text(ph.phase)}: ${counts || DASH}`);
    }
  } else {
    lines.push(`${DASH} (no roadmap items for this project)`);
  }

  lines.push("", "## Next items", "");
  const items = view.next_items ?? [];
  if (items.length === 0) {
    lines.push(`${DASH} (none)`);
  }
  for (const n of items) {
    // missing actionable => pessimistic ⛔ (consistent with S-3 spirit)
    const head = n.actionable === true ? "▶" : "⛔";
    const body = [text(n.id), text(n.title), text(n.computed_status)].join(
      " · ",
    );
    const blocked = n.blocked_by?.length
      ? ` — blocked by: ${n.blocked_by.map(mdEscape).join(", ")}`
      : "";
    lines.push(`- ${head} ${body}${blocked}`);
  }

  const tasks = view.live_tasks ?? [];
  if (tasks.length) {
    lines.push("", "## Live tasks", "");
    for (const t of tasks) {
      lines.push(`- ${text(t.task_id)} · ${text(t.status)} · ${text(t.title)}`);
    }
  }

  const warnings = view.warnings ?? [];
  if (warnings.length) {
    lines.push("", "## Warnings", "");
    for (const w of warnings) {
      lines.push(`- ⚠ ${mdEscape(w)}`);
    }
  }

  return lines.join("\n") + "\n";
}
