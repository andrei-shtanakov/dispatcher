/** Factory floor view-model (specs 2026-09-30-factory-floor-c1/c2-design). vscode-free. */

import type { AgentMerge, FactoryFloorView, InFlightRun, RunEndAct } from "./api";
import { shortRepo } from "./myTurn";

export type RunEndOutcome = "superseded" | "cancelled";

export interface FloorGroup {
  label: string;
  runs: InFlightRun[];
}

const HOUR_MS = 3_600_000;
const SHELL_SAFE = /^[A-Za-z0-9._:/@-]+$/;
// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u001f\u007f]/;

/** Always single-quoted: free text must never reach the shell as syntax
 * (a `(…)` is a zsh glob qualifier, a `;` a second command). */
function quoted(value: string): string {
  return `'${value.replace(/'/g, "'\\''")}'`;
}

function shellWord(value: string): string {
  return SHELL_SAFE.test(value) ? value : `'${value.replace(/'/g, "'\\''")}'`;
}

/** Stale first, then the rest — the server's order is kept inside each. */
export function floorGroups(view: FactoryFloorView): FloorGroup[] {
  const groups: FloorGroup[] = [
    { label: "Stale — likely abandoned", runs: view.in_flight.filter((r) => r.stale) },
    { label: "In flight", runs: view.in_flight.filter((r) => !r.stale) },
  ];
  return groups.filter((g) => g.runs.length > 0);
}

const RUN_SOURCES = ["maestro", "dispatcher_runs"];

/** Whether the runs list is complete — judged by the run sources only: an
 * unread merges section must not make "nothing is running" uncertain. */
function runsComplete(view: FactoryFloorView): boolean {
  const known = RUN_SOURCES.map((k) => view.sources[k]).filter((s) => s !== undefined);
  if (known.length === 0) {
    return view.complete;
  }
  return known.every((s) => s.state === "ok" || s.state === "not_configured");
}

export function floorEmptyText(view: FactoryFloorView): string | null {
  if (view.in_flight.length > 0) {
    return null;
  }
  return runsComplete(view) ? "nothing is running" : "no known runs — sources incomplete";
}

export interface MergesSection {
  label: string;
  merges: AgentMerge[];
}

/** The agent's merges, or null when that section was not read — its
 * absence is then explained by the "sources incomplete" banner, never
 * shown as "merged nothing". */
export function mergesSection(view: FactoryFloorView): MergesSection | null {
  const login = view.agent_merge_login;
  if (!login || view.sources["agent_merges"]?.state !== "ok") {
    return null;
  }
  const merges = view.agent_merges ?? [];
  const hours = view.merges_window_hours ?? 24;
  return { label: `Merged by ${login} · ${hours}h (${merges.length})`, merges };
}

export function mergeLabel(merge: AgentMerge): string {
  const repo = merge.repo.split("/").pop() ?? merge.repo;
  return `${repo}#${merge.number} · ${merge.title}`;
}

export function mergeDescription(merge: AgentMerge, now: Date): string {
  const when = ago(merge.merged_at, now);
  return when === "unknown" ? "merged, time unknown" : `${when} ago`;
}

function ago(iso: string | null, now: Date): string {
  if (iso === null) {
    return "unknown";
  }
  const hours = Math.floor((now.getTime() - Date.parse(iso)) / HOUR_MS);
  if (Number.isNaN(hours)) {
    return "unknown";
  }
  if (hours < 1) {
    return "<1h";
  }
  return hours < 48 ? `${hours}h` : `${Math.floor(hours / 24)}d`;
}

export function runAge(run: InFlightRun, now: Date): string {
  if (run.started_at === null) {
    return "age unknown";
  }
  const hours = Math.floor((now.getTime() - Date.parse(run.started_at)) / HOUR_MS);
  if (Number.isNaN(hours)) {
    return "age unknown";
  }
  if (hours < 1) {
    return "<1h";
  }
  return hours < 48 ? `${hours}h` : `${Math.floor(hours / 24)}d`;
}

export function runLabel(run: InFlightRun): string {
  const repo = shortRepo(run.repo_key) ?? run.repo_key;
  return `${repo} · ${run.work_id ?? run.run_id}`;
}

/** `interrupted · started 37d · idle 37d` — idle is what decides "stale". */
export function runDescription(run: InFlightRun, now: Date): string {
  return [
    run.status,
    `started ${ago(run.started_at, now)}`,
    `idle ${ago(run.last_activity_at, now)}`,
  ].join(" · ");
}

/** What the tooltip may claim about the launch record. */
export function launchLine(run: InFlightRun, recordsRead: boolean): string {
  if (run.request_id) {
    return `launched by dispatcher: ${run.request_id}`;
  }
  // A launch still in `launch_unknown` has no run_id yet, so "no record
  // joined" is all that can be said — never "not launched by dispatcher".
  return recordsRead
    ? "no dispatcher launch record carries this run id"
    : "launch record unknown — dispatcher's records were not fully read";
}

export type PreparedRunEnd =
  | { kind: "terminal"; name: string; text: string; note: string }
  | { kind: "refused"; note: string };

/** `MAESTRO_HOME=… <cli> run-end <id> --outcome <o>` — typed, never run. */
export function prepareRunEnd(
  act: RunEndAct,
  outcome: RunEndOutcome,
  reason: string,
): PreparedRunEnd {
  const values = [act.run_id, act.repo_key, act.maestro_home, act.maestro_cli ?? ""];
  if (values.some((v) => CONTROL.test(v))) {
    return { kind: "refused", note: "the run's identifiers contain control characters" };
  }
  const why = reason.trim();
  if (why === "" || CONTROL.test(why)) {
    return {
      kind: "refused",
      note: "a reason is required, on one line — it is stored with the outcome",
    };
  }
  const cli = act.maestro_cli ? shellWord(act.maestro_cli) : "maestro";
  return {
    kind: "terminal",
    name: `maestro · ${act.repo_key}`,
    text:
      `MAESTRO_HOME=${shellWord(act.maestro_home)} ${cli} run-end ` +
      `${shellWord(act.run_id)} --outcome ${outcome} --reason ${quoted(why)}`,
    note:
      `Typed in, not executed — run it from a checkout of ${act.repo_key}. ` +
      "Ending a run is a decision; nothing infers it.",
  };
}
