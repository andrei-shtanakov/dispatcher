/** Halt view-model (spec 2026-09-30-halt-d1-design). vscode-free.
 *
 * The state shown is always what GitHub read back, never what was asked.
 * Unknown is never "off": a repo that could not be read, or a fleet read
 * still in progress, says so.
 */

import type { HaltView, RepoHalt } from "./api";

export type HaltTone = "halted" | "clear" | "unhealthy" | "unknown" | "off-config";

export interface HaltSummary {
  label: string;
  tone: HaltTone;
}

const REASON_MAX = 500;
// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u001f\u007f]/;

export function haltSummary(view: HaltView | null): HaltSummary {
  if (view === null) {
    return { label: "Halt: unknown — not read", tone: "unknown" };
  }
  if (view.sources["forge_halt"]?.state === "not_configured") {
    return { label: "Halt: not configured (halt_fleet is empty)", tone: "off-config" };
  }
  const n = view.fleet.length;
  if (n === 0) {
    return { label: "Halt: reading…", tone: "unknown" };
  }
  const applying = view.applying ? " · applying…" : "";
  if (view.halted > 0) {
    return { label: `Halt: ON for ${view.halted}/${n}${applying}`, tone: "halted" };
  }
  if (view.unhealthy.length > 0) {
    return {
      label: `Halt: off, ${view.unhealthy.length}/${n} not confirmed${applying}`,
      tone: "unhealthy",
    };
  }
  return { label: `Halt: off (${n} repos confirmed)${applying}`, tone: "clear" };
}

export function repoLine(repo: RepoHalt): string {
  return repo.detail ? `${repo.repo}: ${repo.state} — ${repo.detail}` : `${repo.repo}: ${repo.state}`;
}

/** "read 7m ago" from the oldest per-repo read; null when unknown. */
export function readAge(view: HaltView, now: Date): string | null {
  const stamps = view.fleet
    .map((r) => (r.read_at ? Date.parse(r.read_at) : Number.NaN))
    .filter((t) => !Number.isNaN(t));
  if (stamps.length === 0) {
    return null;
  }
  const minutes = Math.floor((now.getTime() - Math.min(...stamps)) / 60_000);
  return minutes < 1 ? "read <1m ago" : `read ${minutes}m ago`;
}

/** The lines under the halt node: repos, deviations, the last request. */
export function haltLines(view: HaltView): string[] {
  const lines = view.fleet.map(repoLine);
  lines.push(...view.deviations.map((d) => `deviation: ${d}`));
  const last = view.last_request;
  if (last) {
    const failed = last.results.filter((r) => !r.ok).map((r) => r.repo);
    const outcome =
      last.results.length === 0
        ? "no results yet"
        : failed.length === 0
          ? "all confirmed"
          : `not confirmed: ${failed.join(", ")}`;
    lines.push(`last request: ${last.target} (${last.scope}) — ${last.reason} — ${outcome}`);
  }
  return lines;
}

/** null when the reason is acceptable, else what is wrong with it. */
export function reasonProblem(reason: string): string | null {
  const why = reason.trim();
  if (why === "" || CONTROL.test(why) || why.length > REASON_MAX) {
    return `a reason is required: one line, ≤${REASON_MAX} characters`;
  }
  return null;
}
