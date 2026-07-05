/** Dispatcher server lifecycle: probe, spawn, kill. Must stay vscode-free. */

import { spawn, type ChildProcess } from "node:child_process";
import { portFromUrl, shouldSpawn } from "./model";

export interface ServerManagerOptions {
  url: string;
  projectDir: string;
  autoStart: boolean;
  probe: () => Promise<boolean>;
  notify: (message: string) => void;
  sleep?: (ms: number) => Promise<void>;
  spawnFn?: typeof spawn;
}

const READY_TRIES = 20;
const READY_DELAY_MS = 500;
const STDERR_TAIL = 500;

export class ServerManager {
  private child: ChildProcess | null = null;
  private triedThisEpisode = false;

  constructor(private readonly opts: ServerManagerOptions) {}

  /** A successful poll ends the offline episode (spec §4). */
  markOnline(): void {
    this.triedThisEpisode = false;
  }

  async ensureRunning(): Promise<void> {
    const reachable = await this.opts.probe();
    if (reachable) {
      this.triedThisEpisode = false;
      return;
    }
    const spawnIt = shouldSpawn({
      reachable,
      autoStart: this.opts.autoStart,
      projectDir: this.opts.projectDir,
      alreadyTried: this.triedThisEpisode,
    });
    if (!spawnIt) {
      return;
    }
    this.triedThisEpisode = true;
    this.start();
    await this.waitUntilReady();
  }

  /** Also invoked directly by the "Start Server" command. */
  start(): void {
    const spawnFn = this.opts.spawnFn ?? spawn;
    const port = portFromUrl(this.opts.url);
    const stderr: string[] = [];
    this.child = spawnFn(
      "uv",
      ["run", "dispatcher", "serve", "--port", String(port)],
      { cwd: this.opts.projectDir, stdio: ["ignore", "ignore", "pipe"] },
    );
    this.child.stderr?.on("data", (chunk: Buffer) => {
      stderr.push(chunk.toString());
      if (stderr.length > 20) {
        stderr.shift();
      }
    });
    this.child.on("exit", (code) => {
      if (code !== 0 && code !== null) {
        this.opts.notify(
          `dispatcher serve exited (${code}): ` +
            stderr.join("").slice(-STDERR_TAIL),
        );
      }
      this.child = null;
    });
    this.child.on("error", (err: Error) => {
      this.opts.notify(`failed to spawn dispatcher: ${err.message}`);
      this.child = null;
    });
  }

  private async waitUntilReady(): Promise<void> {
    const sleep =
      this.opts.sleep ??
      ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
    for (let i = 0; i < READY_TRIES; i++) {
      await sleep(READY_DELAY_MS);
      if (await this.opts.probe()) {
        return;
      }
    }
  }

  dispose(): void {
    this.child?.kill();
    this.child = null;
  }
}
