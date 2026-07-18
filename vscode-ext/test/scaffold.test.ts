import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

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
    ]);
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
});
