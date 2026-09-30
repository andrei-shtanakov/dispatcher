import { describe, expect, it } from "vitest";
import type { HumanQueueView, HumanWait, SourceStatus } from "../src/api";
import {
  ageLabel,
  emptyText,
  groupWaits,
  incompleteLines,
  isOverdue,
  myTurnStatus,
  prepareAct,
  waitDescription,
} from "../src/myTurn";

const NOW = new Date("2026-09-30T12:00:00Z");
const OK: SourceStatus = { state: "ok", detail: null };

function wait(
  key: string,
  extra: Partial<HumanWait> = {},
): HumanWait {
  return {
    key,
    reasons: ["proposal_gate"],
    source: "impresario",
    repo: "impresario",
    ref: key,
    title: `title ${key}`,
    since: null,
    since_basis: null,
    act: { kind: "open_artifact", path: `pilot/${key}` },
    ...extra,
  };
}

function aged(key: string, since: string, extra: Partial<HumanWait> = {}) {
  return wait(key, { since, since_basis: "test", ...extra });
}

function view(
  waits: HumanWait[],
  sources: Record<string, SourceStatus> = { impresario: OK },
): HumanQueueView {
  const complete = Object.values(sources).every(
    (s) => s.state === "ok" || s.state === "not_configured",
  );
  return { waits, sources, complete, generated_at: NOW.toISOString() };
}

const INCOMPLETE = {
  impresario: OK,
  maestro: { state: "not_configured", detail: null } as SourceStatus,
  forge_labelled_prs: {
    state: "not_connected",
    detail: "arrives in slice A2",
  } as SourceStatus,
  dispatcher_runs: {
    state: "partial",
    detail: "unreadable: bad.json",
  } as SourceStatus,
};

describe("status text", () => {
  it("counts waits", () => {
    const s = myTurnStatus(view([wait("a"), wait("b"), wait("c")]), NOW, 24);
    expect(s.text).toBe("⏳ 3");
  });

  it("marks an incomplete queue", () => {
    const s = myTurnStatus(view([wait("a")], INCOMPLETE), NOW, 24);
    expect(s.text).toBe("⏳ 1 · ?");
  });

  it("is unknown, never zero, when the queue was not read", () => {
    const s = myTurnStatus(null, NOW, 24);
    expect(s.text).toBe("⏳ ?");
    expect(s.overdue).toBe(false);
  });

  it("flags overdue independently of completeness", () => {
    const s = myTurnStatus(
      view([aged("old", "2026-09-28T12:00:00Z")], INCOMPLETE),
      NOW,
      24,
    );
    expect(s.text).toBe("⏳ 1 · ?");
    expect(s.overdue).toBe(true);
  });
});

describe("overdue", () => {
  it("is overdue only past the threshold", () => {
    expect(isOverdue(aged("a", "2026-09-29T11:59:59Z"), NOW, 24)).toBe(true);
    expect(isOverdue(aged("b", "2026-09-29T12:00:00Z"), NOW, 24)).toBe(false);
    expect(isOverdue(aged("c", "2026-09-30T11:00:00Z"), NOW, 24)).toBe(false);
  });

  it("never marks an unknown age overdue", () => {
    expect(isOverdue(wait("a"), NOW, 0)).toBe(false);
  });

  it("compares instants, not text", () => {
    // 13:00+02:00 is 11:00Z — 25h before NOW the next day.
    expect(
      isOverdue(aged("a", "2026-09-29T13:00:00+02:00"), NOW, 24),
    ).toBe(true);
  });
});

describe("age and description", () => {
  it("labels unknown ages explicitly", () => {
    expect(ageLabel(wait("a"), NOW)).toBe("age unknown");
  });

  it("labels known ages", () => {
    expect(ageLabel(aged("a", "2026-09-30T11:30:00Z"), NOW)).toBe("<1h");
    expect(ageLabel(aged("a", "2026-09-30T09:00:00Z"), NOW)).toBe("3h");
    expect(ageLabel(aged("a", "2026-09-28T12:00:00Z"), NOW)).toBe("2d");
  });

  it("lists every reason when there are several", () => {
    const w = wait("a", { reasons: ["backlog_gate", "proposal_gate"] });
    const d = waitDescription(w, NOW);
    expect(d).toContain("age unknown");
    expect(d).toContain("impresario");
    expect(d).toContain("backlog_gate + proposal_gate");
  });
});

describe("grouping", () => {
  it("follows the fixed reason order and keeps server order inside", () => {
    const waits = [
      wait("g1", { reasons: ["proposal_gate"] }),
      wait("m1", { reasons: ["run_needs_review"] }),
      wait("g2", { reasons: ["proposal_gate"] }),
      wait("l1", { reasons: ["launch_unknown"] }),
    ];
    const groups = groupWaits(view(waits));
    expect(groups.map((g) => g.reason)).toEqual([
      "launch_unknown",
      "run_needs_review",
      "proposal_gate",
    ]);
    expect(groups[2].waits.map((w) => w.key)).toEqual(["g1", "g2"]);
  });

  it("places a multi-reason wait by its first reason", () => {
    const w = wait("x", { reasons: ["backlog_gate", "proposal_gate"] });
    expect(groupWaits(view([w])).map((g) => g.reason)).toEqual([
      "backlog_gate",
    ]);
  });
});

describe("completeness banner and empty states", () => {
  it("lists exactly the sources that are not ok and not off", () => {
    expect(incompleteLines(view([], INCOMPLETE))).toEqual([
      "dispatcher_runs: partial — unreadable: bad.json",
      "forge_labelled_prs: not_connected — arrives in slice A2",
    ]);
    expect(incompleteLines(view([]))).toEqual([]);
  });

  it("never says nothing waits while a source is unread", () => {
    expect(emptyText(view([], INCOMPLETE))).toBe(
      "no known waits — queue incomplete",
    );
    expect(emptyText(view([]))).toBe("nothing waits for you");
    expect(emptyText(view([wait("a")]))).toBeNull();
  });
});

describe("acts are prepared, never executed", () => {
  const opts = { baseUrl: "http://127.0.0.1:8787/", impresarioPath: null };

  it("maps run_view to the launchpad run view", () => {
    expect(
      prepareAct({ kind: "run_view", request_id: "1111-aa" }, opts),
    ).toEqual({
      kind: "url",
      url: "http://127.0.0.1:8787/#launchpad/1111-aa",
    });
  });

  it("maps maestro_verb to a single command line", () => {
    const p = prepareAct(
      {
        kind: "maestro_verb",
        verb: "retry",
        task_id: "T-1",
        run_id: "01RUN",
        repo_key: "github.com/acme/app",
      },
      opts,
    );
    expect(p.kind).toBe("terminal");
    if (p.kind !== "terminal") return;
    expect(p.text).toBe("maestro retry T-1 --run 01RUN");
    expect(p.name).toBe("maestro · github.com/acme/app");
    expect(p.note).toContain("github.com/acme/app");
  });

  it("quotes ids a shell would split, and refuses control characters", () => {
    const quoted = prepareAct(
      {
        kind: "maestro_verb",
        verb: "approve",
        task_id: "T 1;rm",
        run_id: "01RUN",
        repo_key: "r",
      },
      opts,
    );
    expect(quoted.kind === "terminal" && quoted.text).toBe(
      "maestro approve 'T 1;rm' --run 01RUN",
    );
    const refused = prepareAct(
      {
        kind: "maestro_verb",
        verb: "retry",
        task_id: "T-1\nrm -rf /",
        run_id: "01RUN",
        repo_key: "r",
      },
      opts,
    );
    expect(refused.kind).toBe("refused");
  });

  it("opens an artifact under the mirror when it is known", () => {
    const p = prepareAct(
      { kind: "open_artifact", path: "pilot/pp-101" },
      { ...opts, impresarioPath: "/ws/impresario" },
    );
    expect(p).toEqual({ kind: "file", path: "/ws/impresario/pilot/pp-101" });
  });

  it("falls back to the clipboard when the mirror is unknown or the path escapes", () => {
    const unknown = prepareAct(
      { kind: "open_artifact", path: "pilot/pp-101" },
      opts,
    );
    expect(unknown.kind).toBe("clipboard");
    const escaping = prepareAct(
      { kind: "open_artifact", path: "../secrets" },
      { ...opts, impresarioPath: "/ws/impresario" },
    );
    expect(escaping.kind).toBe("clipboard");
    const absolute = prepareAct(
      { kind: "open_artifact", path: "/etc/passwd" },
      { ...opts, impresarioPath: "/ws/impresario" },
    );
    expect(absolute.kind).toBe("clipboard");
  });
});
