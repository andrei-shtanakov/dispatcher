import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

describe("manifest", () => {
  const manifest = JSON.parse(
    readFileSync(new URL("../package.json", import.meta.url), "utf-8"),
  );

  it("declares both views and all commands", () => {
    const views = manifest.contributes.views.dispatcher.map(
      (v: { id: string }) => v.id,
    );
    expect(views).toEqual(["dispatcherProjects", "dispatcherErrors"]);
    const commands = manifest.contributes.commands.map(
      (c: { command: string }) => c.command,
    );
    expect(commands).toContain("dispatcher.refresh");
    expect(commands).toContain("dispatcher.startServer");
  });

  it("ships spec §5 defaults", () => {
    const props = manifest.contributes.configuration.properties;
    expect(props["dispatcher.url"].default).toBe("http://127.0.0.1:8787");
    expect(props["dispatcher.projectDir"].default).toBe("");
    expect(props["dispatcher.autoStart"].default).toBe(true);
    expect(props["dispatcher.pollSeconds"].default).toBe(10);
    expect(props["dispatcher.pollSeconds"].minimum).toBe(5);
  });
});
