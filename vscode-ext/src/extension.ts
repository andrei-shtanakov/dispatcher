/** Extension entry point: config, poller, commands, wiring. */

import * as vscode from "vscode";
import { ApiClient } from "./api";
import { ServerManager } from "./server";
import type { SyncStatusResponse } from "./api";
import { createStatusBar } from "./status";
import { ErrorsProvider, ProjectsProvider, RoadmapProvider } from "./tree";

interface Config {
  url: string;
  projectDir: string;
  autoStart: boolean;
  pollSeconds: number;
}

function readConfig(): Config {
  const cfg = vscode.workspace.getConfiguration("dispatcher");
  return {
    url: cfg.get<string>("url", "http://127.0.0.1:8787"),
    projectDir: cfg.get<string>("projectDir", ""),
    autoStart: cfg.get<boolean>("autoStart", true),
    pollSeconds: Math.max(5, cfg.get<number>("pollSeconds", 10)),
  };
}

export function activate(context: vscode.ExtensionContext): void {
  const client = (): ApiClient => new ApiClient(readConfig().url);

  const server = new ServerManager({
    get url() {
      return readConfig().url;
    },
    get projectDir() {
      return readConfig().projectDir;
    },
    get autoStart() {
      return readConfig().autoStart;
    },
    probe: async () => {
      try {
        await client().overview();
        return true;
      } catch {
        return false;
      }
    },
    notify: (message) => {
      void vscode.window.showErrorMessage(message);
    },
  });

  const projects = new ProjectsProvider(client);
  const errors = new ErrorsProvider();
  const roadmap = new RoadmapProvider();
  const status = createStatusBar();

  let polling = false;
  let lastSync: SyncStatusResponse | null = null;

  async function poll(): Promise<void> {
    if (polling) {
      return;
    }
    polling = true;
    try {
      const api = client();
      // overview() is the health signal (same call server.probe uses).
      // Only its failure means the server is offline; errors/roadmap
      // degrade independently so one broken endpoint (e.g. an older
      // server without /api/roadmap) doesn't blank the other views.
      const overview = await api.overview().catch(() => null);
      if (overview === null) {
        projects.setData(null);
        errors.setData(null);
        roadmap.setData(null);
        status.update(null);
        await server.ensureRunning();
        return;
      }
      projects.setData(overview.projects);
      // мгновенный базовый статус с ПОСЛЕДНИМ известным вердиктом:
      // медленный /api/sync не задерживает статус-бар и не мигает им
      status.update(overview, lastSync);
      server.markOnline();
      const [events, roadmapData, syncData] = await Promise.allSettled([
        api.errors(),
        api.roadmap(),
        api.sync(),
      ]);
      errors.setData(events.status === "fulfilled" ? events.value : null);
      roadmap.setData(
        roadmapData.status === "fulfilled" ? roadmapData.value : null,
      );
      // вердикт деградирует независимо: старый сервер без /api/sync
      // не гасит остальные вьюхи (тот же принцип, что errors/roadmap)
      lastSync = syncData.status === "fulfilled" ? syncData.value : null;
      status.update(overview, lastSync);
    } finally {
      polling = false;
    }
  }

  const timer = setInterval(() => void poll(), readConfig().pollSeconds * 1000);

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("dispatcherProjects", projects),
    vscode.window.registerTreeDataProvider("dispatcherErrors", errors),
    vscode.window.registerTreeDataProvider("dispatcherRoadmap", roadmap),
    status.item,
    vscode.commands.registerCommand("dispatcher.refresh", () => void poll()),
    vscode.commands.registerCommand("dispatcher.startServer", () => {
      if (readConfig().projectDir.trim() === "") {
        void vscode.window.showWarningMessage(
          "Set dispatcher.projectDir to the dispatcher repo path to start the server.",
        );
        return;
      }
      server.start();
      void poll();
    }),
    vscode.commands.registerCommand(
      "dispatcher.showError",
      async (body: string) => {
        const doc = await vscode.workspace.openTextDocument({
          content: body,
          language: "log",
        });
        await vscode.window.showTextDocument(doc, { preview: true });
      },
    ),
    { dispose: () => clearInterval(timer) },
    { dispose: () => server.dispose() },
    { dispose: () => projects.dispose() },
    { dispose: () => errors.dispose() },
    { dispose: () => roadmap.dispose() },
  );

  void poll();
}

export function deactivate(): void {}
