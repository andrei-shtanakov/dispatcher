/** "My turn" view-model (spec 2026-09-30-my-turn-vscode-design). vscode-free.
 *
 * Everything here renders the human queue the server already assembled:
 * nothing is re-classified, merged or re-ordered inside a group. Unknown is
 * never shown as zero — an unread queue is `⏳ ?`, an incomplete one says so.
 */

import type {
  HumanQueueView,
  HumanWait,
  WaitAct,
  WaitReason,
} from "./api";

/** Group order of the tree — fixed, not derived from the data. */
export const REASON_ORDER: readonly WaitReason[] = [
  "launch_unknown",
  "run_needs_review",
  "run_awaiting_approval",
  "loop_needs_human",
  "proposal_gate",
  "backlog_gate",
];

export const REASON_LABEL: Record<GroupKey, string> = {
  launch_unknown: "Launch outcome unknown",
  run_needs_review: "Run task needs review",
  run_awaiting_approval: "Run task awaits approval",
  loop_needs_human: "Product loop needs a human",
  proposal_gate: "Proposal gate",
  backlog_gate: "Backlog gate (QG-4)",
  other: "Other reasons",
};

const HOUR_MS = 3_600_000;
// `not_configured` means the feature is off: nothing can wait there, so it
// is not a hole in the queue (same rule as the server's `complete`).
const SILENT_STATES = new Set(["ok", "not_configured"]);

export interface MyTurnStatus {
  text: string;
  overdue: boolean;
  tooltip: string;
}

/** A known reason, or "other" for reasons this build predates. */
export type GroupKey = WaitReason | "other";

export interface MyTurnGroup {
  reason: GroupKey;
  waits: HumanWait[];
}

export type PreparedAct =
  | { kind: "url"; url: string }
  | { kind: "terminal"; name: string; text: string; note: string }
  | { kind: "file"; path: string }
  | { kind: "clipboard"; text: string; note: string }
  | { kind: "refused"; note: string };

/** Milliseconds the wait has lasted, or null when its start is unknown. */
export function waitAgeMs(wait: HumanWait, now: Date): number | null {
  if (wait.since === null) {
    return null;
  }
  const start = Date.parse(wait.since);
  return Number.isNaN(start) ? null : now.getTime() - start;
}

export function ageLabel(wait: HumanWait, now: Date): string {
  const age = waitAgeMs(wait, now);
  if (age === null) {
    return "age unknown";
  }
  const hours = Math.floor(age / HOUR_MS);
  if (hours < 1) {
    return "<1h";
  }
  return hours < 48 ? `${hours}h` : `${Math.floor(hours / 24)}d`;
}

/** Strictly older than the threshold; an unknown age is never overdue. */
export function isOverdue(
  wait: HumanWait,
  now: Date,
  thresholdHours: number,
): boolean {
  const age = waitAgeMs(wait, now);
  return age !== null && age > thresholdHours * HOUR_MS;
}

export function waitDescription(wait: HumanWait, now: Date): string {
  const parts = [ageLabel(wait, now)];
  if (wait.repo) {
    parts.push(wait.repo);
  }
  if (wait.reasons.length > 1) {
    parts.push(wait.reasons.join(" + "));
  }
  return parts.join(" · ");
}

export function myTurnStatus(
  view: HumanQueueView | null,
  now: Date,
  thresholdHours: number,
): MyTurnStatus {
  if (view === null) {
    return {
      text: "⏳ ?",
      overdue: false,
      tooltip: "My turn: the human queue could not be read",
    };
  }
  const count = view.waits.length;
  const overdue = view.waits.some((w) => isOverdue(w, now, thresholdHours));
  const lines = [`My turn: ${count} wait(s) for you`];
  if (overdue) {
    lines.push(`some wait longer than ${thresholdHours}h`);
  }
  if (!view.complete) {
    lines.push("queue incomplete:", ...incompleteLines(view));
  }
  return {
    text: view.complete ? `⏳ ${count}` : `⏳ ${count} · ?`,
    overdue,
    tooltip: lines.join("\n"),
  };
}

/** One line per source that makes the queue incomplete, sorted by name. */
export function incompleteLines(view: HumanQueueView): string[] {
  return Object.entries(view.sources)
    .filter(([, status]) => !SILENT_STATES.has(status.state))
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, status]) =>
      status.detail
        ? `${name}: ${status.state} — ${status.detail}`
        : `${name}: ${status.state}`,
    );
}

/** Non-empty groups in REASON_ORDER, then "other"; server order kept
 * inside a group. A reason this build does not know (a newer server) lands
 * in "other" — a served wait is never dropped from the tree. */
export function groupWaits(view: HumanQueueView): MyTurnGroup[] {
  const known = new Set<string>(REASON_ORDER);
  const groups: MyTurnGroup[] = REASON_ORDER.map((reason) => ({
    reason,
    waits: view.waits.filter((w) => w.reasons[0] === reason),
  }));
  groups.push({
    reason: "other",
    waits: view.waits.filter((w) => !known.has(w.reasons[0])),
  });
  return groups.filter((group) => group.waits.length > 0);
}

/** The empty-list text, or null when there are waits to show. */
export function emptyText(view: HumanQueueView): string | null {
  if (view.waits.length > 0) {
    return null;
  }
  return view.complete
    ? "nothing waits for you"
    : "no known waits — queue incomplete";
}

// Ids pass through unquoted only when a shell cannot split or expand them.
const SHELL_SAFE = /^[A-Za-z0-9._:/@-]+$/;
// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u001f\u007f]/;

function shellWord(value: string): string {
  return SHELL_SAFE.test(value) ? value : `'${value.replace(/'/g, "'\\''")}'`;
}

function safeRelative(path: string): boolean {
  return (
    path.length > 0 &&
    !path.startsWith("/") &&
    !CONTROL.test(path) &&
    path.split("/").every((seg) => seg !== ".." && seg !== "")
  );
}

/** What clicking a wait does. Never executes anything (spec §3). */
export function prepareAct(
  act: WaitAct,
  opts: { baseUrl: string; impresarioPath: string | null },
): PreparedAct {
  if (act.kind === "run_view") {
    const base = opts.baseUrl.replace(/\/+$/, "");
    return {
      kind: "url",
      url: `${base}/#launchpad/${encodeURIComponent(act.request_id)}`,
    };
  }
  if (act.kind === "maestro_verb") {
    const ids = [act.task_id, act.run_id, act.repo_key];
    if (ids.some((v) => CONTROL.test(v))) {
      // A newline in a terminal-typed line would execute it on its own.
      return {
        kind: "refused",
        note: "the wait's ids contain control characters; not preparing a command",
      };
    }
    return {
      kind: "terminal",
      name: `maestro · ${act.repo_key}`,
      text: `maestro ${act.verb} ${shellWord(act.task_id)} --run ${shellWord(act.run_id)}`,
      note:
        `Typed in, not executed. Run it from a checkout of ${act.repo_key} ` +
        "(maestro resolves the run's repository from the current directory), " +
        "with the MAESTRO_HOME and ATP_CATALOG your dispatcher config uses — " +
        "otherwise maestro looks in a different home or cannot route a model.",
    };
  }
  if (opts.impresarioPath !== null && safeRelative(act.path)) {
    const root = opts.impresarioPath.replace(/\/+$/, "");
    return { kind: "file", path: `${root}/${act.path}` };
  }
  return {
    kind: "clipboard",
    text: act.path,
    note:
      opts.impresarioPath === null
        ? "impresario mirror path unknown — copied the mirror-relative path"
        : "path is not a plain relative path — copied it instead of opening",
  };
}
