import { EventEmitter } from "node:events";
import { describe, expect, it, vi } from "vitest";
import { ServerManager, type ServerManagerOptions } from "../src/server";

class FakeChild extends EventEmitter {
  stderr = new EventEmitter();
  killed = false;
  kill(): boolean {
    this.killed = true;
    return true;
  }
}

function manager(overrides: Partial<ServerManagerOptions> = {}) {
  const child = new FakeChild();
  const spawnFn = vi.fn().mockReturnValue(child);
  const notify = vi.fn();
  const opts: ServerManagerOptions = {
    url: "http://127.0.0.1:8787",
    projectDir: "/repo",
    autoStart: true,
    probe: vi.fn().mockResolvedValue(false),
    notify,
    sleep: () => Promise.resolve(),
    spawnFn: spawnFn as unknown as ServerManagerOptions["spawnFn"],
    ...overrides,
  };
  return { mgr: new ServerManager(opts), spawnFn, notify, child, opts };
}

describe("ServerManager", () => {
  it("does not spawn when the server is reachable", async () => {
    const { mgr, spawnFn } = manager({ probe: vi.fn().mockResolvedValue(true) });
    await mgr.ensureRunning();
    expect(spawnFn).not.toHaveBeenCalled();
  });

  it("spawns uv run dispatcher serve with the URL port", async () => {
    const { mgr, spawnFn } = manager();
    await mgr.ensureRunning();
    expect(spawnFn).toHaveBeenCalledWith(
      "uv",
      ["run", "dispatcher", "serve", "--port", "8787"],
      expect.objectContaining({ cwd: "/repo" }),
    );
  });

  it("spawns at most once per offline episode", async () => {
    const { mgr, spawnFn } = manager();
    await mgr.ensureRunning();
    await mgr.ensureRunning();
    expect(spawnFn).toHaveBeenCalledTimes(1);
    mgr.markOnline(); // a successful poll ends the episode
    await mgr.ensureRunning();
    expect(spawnFn).toHaveBeenCalledTimes(2);
  });

  it("does not spawn when autoStart is off or projectDir empty", async () => {
    const a = manager({ autoStart: false });
    await a.mgr.ensureRunning();
    expect(a.spawnFn).not.toHaveBeenCalled();
    const b = manager({ projectDir: "" });
    await b.mgr.ensureRunning();
    expect(b.spawnFn).not.toHaveBeenCalled();
  });

  it("notifies with stderr tail on nonzero exit", async () => {
    const { mgr, notify, child } = manager();
    await mgr.ensureRunning();
    child.stderr.emit("data", Buffer.from("uvicorn exploded"));
    child.emit("exit", 1);
    expect(notify).toHaveBeenCalledWith(
      expect.stringContaining("uvicorn exploded"),
    );
  });

  it("notifies when spawn itself fails", async () => {
    const { mgr, notify, child } = manager();
    await mgr.ensureRunning();
    child.emit("error", new Error("uv not found"));
    expect(notify).toHaveBeenCalledWith(expect.stringContaining("uv not found"));
  });

  it("dispose kills only its own child", async () => {
    const { mgr, child } = manager();
    await mgr.ensureRunning();
    mgr.dispose();
    expect(child.killed).toBe(true);
    const fresh = manager();
    fresh.mgr.dispose(); // never spawned — nothing to kill, no throw
  });
});
