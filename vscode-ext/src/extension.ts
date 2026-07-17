/** Extension entry point: config, poller, commands, wiring. */

import * as path from "node:path";
import * as vscode from "vscode";
import { ApiClient, ApiError } from "./api";
import { ServerManager } from "./server";
import type { ActionOutcome, SpecRunnerConfigEntry, SyncStatusResponse } from "./api";
import { createStatusBar } from "./status";
import {
  applyEdit,
  diffLines,
  fieldItems,
  newFlow,
  requestBody,
  validateField,
} from "./configFlow";
import type { FlowState } from "./configFlow";
import {
  ErrorsProvider,
  ProjectsProvider,
  RoadmapProvider,
  SyncProvider,
} from "./tree";
import type { SyncNode } from "./tree";

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
  const sync = new SyncProvider();
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
        sync.setData(null);
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
      sync.setData(lastSync);
      status.update(overview, lastSync);
    } finally {
      polling = false;
    }
  }

  async function runAction(
    action: "pull" | "create-pr",
    node: SyncNode,
  ): Promise<void> {
    if (node.kind !== "verdict") {
      return;
    }
    const dir = node.v.repo;
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: `dispatcher: ${action} ${dir}`,
      },
      async () => {
        try {
          const api = client();
          const outcome =
            action === "pull" ? await api.pull(dir) : await api.createPr(dir);
          if (outcome.ok) {
            const message = outcome.pr_url ?? outcome.detail ?? "done";
            const choice = outcome.pr_url
              ? await vscode.window.showInformationMessage(message, "Open PR")
              : await vscode.window.showInformationMessage(message);
            if (choice === "Open PR" && outcome.pr_url) {
              void vscode.env.openExternal(vscode.Uri.parse(outcome.pr_url));
            }
          } else {
            void vscode.window.showErrorMessage(
              outcome.error ?? "dispatcher action failed",
            );
          }
        } catch (e) {
          void vscode.window.showErrorMessage(
            e instanceof ApiError ? e.detail : String(e),
          );
        }
      },
    );
    void poll();
  }

  async function decideProposal(
    action: "track" | "ignore",
    node: SyncNode,
  ): Promise<void> {
    if (node.kind !== "proposal") {
      return;
    }
    try {
      await client().track(node.dir, action);
    } catch (e) {
      void vscode.window.showErrorMessage(
        e instanceof ApiError ? e.detail : String(e),
      );
    }
    void poll();
  }

  async function confirmConfig(state: FlowState): Promise<void> {
    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: "dispatcher: update spec-runner config",
      },
      async () => {
        let outcome: ActionOutcome;
        try {
          outcome = await client().updateSpecRunnerConfig(requestBody(state));
        } catch (e) {
          void vscode.window.showErrorMessage(
            e instanceof ApiError ? e.detail : String(e),
          );
          return;
        }
        // outcome order: no-op (benign info) first, then ok (info + Open
        // PR), then error toast — a no-op still has ok=false/true
        // depending on the server, so detail is checked before ok.
        if (outcome.detail === "no-op") {
          void vscode.window.showInformationMessage(
            "config already in this state — no PR needed",
          );
        } else if (outcome.ok) {
          const message = outcome.pr_url ?? outcome.detail ?? "done";
          const choice = outcome.pr_url
            ? await vscode.window.showInformationMessage(message, "Open PR")
            : await vscode.window.showInformationMessage(message);
          if (choice === "Open PR" && outcome.pr_url) {
            void vscode.env.openExternal(vscode.Uri.parse(outcome.pr_url));
          }
        } else {
          void vscode.window.showErrorMessage(
            outcome.error ?? "dispatcher action failed",
          );
        }
      },
    );
    void poll();
  }

  async function editConfigCommand(): Promise<void> {
    let entries: SpecRunnerConfigEntry[];
    try {
      entries = await client().specRunnerConfigs();
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        void vscode.window.showWarningMessage(
          "server does not support the config editor (upgrade dispatcher)",
        );
        return;
      }
      // parity with runAction/confirmConfig: never a silent unhandled
      // rejection — offline/500/409 all surface as a toast
      void vscode.window.showErrorMessage(
        e instanceof ApiError ? e.detail : String(e),
      );
      return;
    }
    const picked = await vscode.window.showQuickPick(
      entries.map((entry) => ({
        label: path.basename(path.dirname(entry.project_yaml_path)),
        description: entry.project,
        entry,
      })),
      { title: "spec-runner config: choose a project" },
    );
    if (!picked) return;
    let state = newFlow(picked.entry);
    // field loop: lives until confirm/cancel; diff preview re-enters with
    // the SAME state (the flow's bug magnet — state is in configFlow, not
    // in this closure's locals beyond `state` itself)
    for (;;) {
      const choice = await vscode.window.showQuickPick(
        [
          ...fieldItems(state).map((f) => ({
            label: f.field,
            description: `${String(f.value)} (${f.marker})`,
          })),
          { label: "$(diff) Preview diff", description: "" },
          { label: "$(git-pull-request) Confirm → PR", description: "" },
        ],
        { title: "spec-runner config: edit fields" },
      );
      if (!choice) return; // cancelled
      if (choice.label.endsWith("Preview diff")) {
        const doc = await vscode.workspace.openTextDocument({
          content: diffLines(state).join("\n"),
          language: "diff",
        });
        await vscode.window.showTextDocument(doc, { preview: true });
        continue; // re-enter the loop with the same state
      }
      if (choice.label.endsWith("Confirm → PR")) {
        await confirmConfig(state);
        return;
      }
      const field = choice.label;
      const current = fieldItems(state).find((f) => f.field === field);
      const raw = await vscode.window.showInputBox({
        title: field,
        value: String(current?.value ?? ""),
        validateInput: (input) => validateField(state.entry, field, input),
      });
      if (raw !== undefined) {
        state = applyEdit(state, field, raw);
      }
    }
  }

  const timer = setInterval(() => void poll(), readConfig().pollSeconds * 1000);

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("dispatcherProjects", projects),
    vscode.window.registerTreeDataProvider("dispatcherErrors", errors),
    vscode.window.registerTreeDataProvider("dispatcherRoadmap", roadmap),
    vscode.window.registerTreeDataProvider("dispatcherSync", sync),
    status.item,
    vscode.commands.registerCommand("dispatcher.refresh", () => void poll()),
    vscode.commands.registerCommand(
      "dispatcher.pull",
      (node: SyncNode) => void runAction("pull", node),
    ),
    vscode.commands.registerCommand(
      "dispatcher.openPr",
      (node: SyncNode) => void runAction("create-pr", node),
    ),
    vscode.commands.registerCommand(
      "dispatcher.track",
      (node: SyncNode) => void decideProposal("track", node),
    ),
    vscode.commands.registerCommand(
      "dispatcher.ignore",
      (node: SyncNode) => void decideProposal("ignore", node),
    ),
    vscode.commands.registerCommand(
      "dispatcher.editSpecRunnerConfig",
      () => void editConfigCommand(),
    ),
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
    { dispose: () => sync.dispose() },
  );

  void poll();
}

export function deactivate(): void {}
