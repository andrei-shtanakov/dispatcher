import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { WaitAct } from "../src/api";
import { prepareAct } from "../src/myTurn";

/** FR-06 parity (spec B2 §1): the same fixture is checked by pytest against
 * dispatcher/core/prepared_acts.py — one act, one command, on every surface. */
interface Case {
  name: string;
  act: WaitAct;
  expected: { kind: "command" | "refused"; text?: string; url?: string; note: string };
}

const CASES: Case[] = JSON.parse(
  readFileSync(new URL("./fixtures/prepared-acts.json", import.meta.url), "utf-8"),
);

describe("prepared acts match the server's words", () => {
  it("has cases", () => {
    expect(CASES.length).toBeGreaterThan(5);
  });
  for (const c of CASES) {
    it(c.name, () => {
      const got = prepareAct(c.act, { baseUrl: "http://x", impresarioPath: null });
      if (c.expected.kind === "refused") {
        expect(got).toEqual({ kind: "refused", note: c.expected.note });
      } else if (got.kind === "human_merge") {
        expect({ text: got.command, url: got.url, note: got.note }).toEqual({
          text: c.expected.text,
          url: c.expected.url,
          note: c.expected.note,
        });
      } else {
        expect(got.kind).toBe("terminal");
        const t = got as { text: string; note: string };
        expect({ text: t.text, note: t.note }).toEqual({
          text: c.expected.text,
          note: c.expected.note,
        });
      }
    });
  }
});
