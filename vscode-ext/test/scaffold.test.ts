import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { RepoVerdict } from "../src/api";
import { syncItemContext } from "../src/model";

describe("manifest", () => {
  const manifest = JSON.parse(
    readFileSync(new URL("../package.json", import.meta.url), "utf-8"),
  );

  it("declares all views and all commands", () => {
    const views = manifest.contributes.views.dispatcher.map(
      (v: { id: string }) => v.id,
    );
    expect(views).toEqual([
      "dispatcherProjects",
      "dispatcherErrors",
      "dispatcherRoadmap",
      "dispatcherSync",
      "dispatcherBenchmarks",
    ]);
    // Benchmarks is the one view with a visibility condition: web hides
    // the section on `unconfigured`, this view hides via the context key.
    const benchmarksView = manifest.contributes.views.dispatcher.find(
      (v: { id: string }) => v.id === "dispatcherBenchmarks",
    );
    expect(benchmarksView.when).toBe("dispatcher.benchmarksConfigured");
    const commands = manifest.contributes.commands.map(
      (c: { command: string }) => c.command,
    );
    expect(commands).toContain("dispatcher.refresh");
    expect(commands).toContain("dispatcher.startServer");
    expect(commands).toContain("dispatcher.pull");
    expect(commands).toContain("dispatcher.openPr");
    expect(commands).toContain("dispatcher.track");
    expect(commands).toContain("dispatcher.ignore");
  });

  it("ships spec §5 defaults", () => {
    const props = manifest.contributes.configuration.properties;
    expect(props["dispatcher.url"].default).toBe("http://127.0.0.1:8787");
    expect(props["dispatcher.projectDir"].default).toBe("");
    expect(props["dispatcher.autoStart"].default).toBe(true);
    expect(props["dispatcher.pollSeconds"].default).toBe(10);
    expect(props["dispatcher.pollSeconds"].minimum).toBe(5);
  });

  it("contributes the project onboarding command, palette-visible", () => {
    const commands = manifest.contributes.commands as Array<{
      command: string;
      title: string;
    }>;
    expect(
      commands.some(
        (c) =>
          c.command === "dispatcher.projectOnboarding" &&
          c.title === "Dispatcher: Project Onboarding",
      ),
    ).toBe(true);
    // must NOT be hidden from the palette
    const palette = (manifest.contributes.menus?.commandPalette ?? []) as Array<{
      command: string;
      when?: string;
    }>;
    expect(
      palette.some(
        (m) => m.command === "dispatcher.projectOnboarding" && m.when === "false",
      ),
    ).toBe(false);
  });

  it("contributes the project context-menu entry with the exact when-rule", () => {
    const ctx = manifest.contributes.menus["view/item/context"] as Array<{
      command: string;
      when: string;
    }>;
    expect(
      ctx.some(
        (m) =>
          m.command === "dispatcher.projectOnboarding" &&
          m.when === "view == dispatcherProjects && viewItem == dispatcherProject",
      ),
    ).toBe(true);
  });

  it("every contextValue syncItemContext can return is wired into a view/item/context entry", () => {
    // Sweep the (behind, ahead) input space and collect what the function
    // ACTUALLY returns, rather than hard-coding the expected contextValue
    // strings beside it — a hard-coded list is a second thing to forget,
    // which is the failure mode this whole fix was about.
    const base: RepoVerdict = {
      repo: "a",
      verdict: "sync-first",
      reason: null,
      branch: null,
      ahead: null,
      behind: null,
      dirty: false,
      is_kb: false,
    };
    const sample = [null, 0, 1, 2];
    const observed = new Set<string>();
    for (const behind of sample) {
      for (const ahead of sample) {
        const ctx = syncItemContext({ ...base, behind, ahead }, true);
        if (ctx !== null) observed.add(ctx);
      }
    }
    // The sweep itself must have found something, or the assertion below
    // would pass vacuously over an empty set.
    expect(observed.size).toBeGreaterThan(0);

    const syncCtxEntries = (
      manifest.contributes.menus["view/item/context"] as Array<{
        command: string;
        when: string;
      }>
    ).filter((m) => m.when.includes("view == dispatcherSync"));

    for (const value of observed) {
      expect(
        syncCtxEntries.some((m) => m.when.includes(`viewItem == ${value}`)),
        `no view/item/context entry for contextValue "${value}"`,
      ).toBe(true);
    }
  });
});
