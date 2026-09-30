/** Extension entry point: config, poller, commands, wiring. */

import * as path from "node:path";
import * as vscode from "vscode";
import { ApiClient, ApiError } from "./api";
import { ServerManager } from "./server";
import type {
  ActionOutcome,
  HumanWait,
  InFlightRun,
  OverviewResponse,
  SpecRunnerConfigEntry,
  SyncStatusResponse,
  HaltView,
} from "./api";
import { prepareAct } from "./myTurn";
import { MyTurnProvider, createMyTurnStatus } from "./myTurnView";
import { FloorProvider } from "./floorView";
import { prepareRunEnd } from "./floor";
import { haltSummary, reasonProblem } from "./halt";
import type { RunEndOutcome } from "./floor";
import { createStatusBar } from "./status";
import type { OnboardingView } from "./onboarding";
import { composeProjectDoc } from "./productProposals";
import type { ProductProposalsReport } from "./productProposals";
import type { RunsSnapshot } from "./runs";
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
  BenchmarksProvider,
  ErrorsProvider,
  ProjectsProvider,
  RoadmapProvider,
  SyncProvider,
} from "./tree";
import type { ProjectNode, SyncNode } from "./tree";

interface Config {
  url: string;
  projectDir: string;
  autoStart: boolean;
  pollSeconds: number;
  myTurnOverdueHours: number;
}

function readConfig(): Config {
  const cfg = vscode.workspace.getConfiguration("dispatcher");
  return {
    url: cfg.get<string>("url", "http://127.0.0.1:8787"),
    projectDir: cfg.get<string>("projectDir", ""),
    autoStart: cfg.get<boolean>("autoStart", true),
    pollSeconds: Math.max(5, cfg.get<number>("pollSeconds", 10)),
    myTurnOverdueHours: Math.max(0, cfg.get<number>("myTurnOverdueHours", 24)),
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
  const benchmarks = new BenchmarksProvider();
  // Visibility starts TRUE: unknown ≠ «feature off». If the very first
  // poll fails, the view stays visible with the offline node; only an
  // explicit `unconfigured` response hides it (Copilot review PR #153).
  void vscode.commands.executeCommand(
    "setContext",
    "dispatcher.benchmarksConfigured",
    true,
  );
  const status = createStatusBar();
  const overdueHours = (): number => readConfig().myTurnOverdueHours;
  const myTurn = new MyTurnProvider(overdueHours);
  const myTurnStatus = createMyTurnStatus(overdueHours);
  const floor = new FloorProvider();
  // The overview carries the impresario mirror path an `open_artifact` act
  // resolves against; the last good one is enough (spec §3).
  let lastOverview: OverviewResponse | null = null;

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
        benchmarks.setData(null);
        status.update(null);
        myTurn.setState({ kind: "offline" });
        myTurnStatus.update(null);
        floor.setState({ kind: "offline" });
        await server.ensureRunning();
        return;
      }
      projects.setData(overview.projects);
      lastOverview = overview;
      // мгновенный базовый статус с ПОСЛЕДНИМ известным вердиктом:
      // медленный /api/sync не задерживает статус-бар и не мигает им
      status.update(overview, lastSync);
      server.markOnline();
      const [events, roadmapData, syncData, benchData, queueData, floorData] =
        await Promise.allSettled([
          api.errors(),
          api.roadmap(),
          api.sync(),
          api.benchmarks(),
          api.humanQueue(),
          api.factoryFloor(),
        ]);
      // The halt is polled beside the floor; an older server (404) shows
      // no halt node rather than a false "off".
      try {
        floor.setHalt(await api.halt());
      } catch (err) {
        floor.setHalt(err instanceof ApiError && err.status === 404 ? undefined : null);
      }
      floor.setState(
        floorData.status === "fulfilled"
          ? { kind: "view", view: floorData.value }
          : { kind: "unavailable", detail: errorText(floorData.reason) },
      );
      // An older server without /api/human-queue, or a failing read, is
      // «unavailable» — never an empty queue (spec §2).
      if (queueData.status === "fulfilled") {
        myTurn.setState({ kind: "view", view: queueData.value });
        myTurnStatus.update(queueData.value);
      } else {
        myTurn.setState({
          kind: "unavailable",
          detail: errorText(queueData.reason),
        });
        myTurnStatus.update(null);
      }
      errors.setData(events.status === "fulfilled" ? events.value : null);
      roadmap.setData(
        roadmapData.status === "fulfilled" ? roadmapData.value : null,
      );
      // вердикт деградирует независимо: старый сервер без /api/sync
      // не гасит остальные вьюхи (тот же принцип, что errors/roadmap)
      lastSync = syncData.status === "fulfilled" ? syncData.value : null;
      sync.setData(lastSync);
      // Cross-surface rule (web hides the section, TUI hides the tab):
      // `unconfigured` hides the whole view via the context key. A failed
      // fetch keeps the last-known visibility and shows the offline node —
      // unknown must not read as «feature off».
      if (benchData.status === "fulfilled") {
        benchmarks.setData(benchData.value);
        void vscode.commands.executeCommand(
          "setContext",
          "dispatcher.benchmarksConfigured",
          benchData.value.report.status !== "unconfigured",
        );
      } else {
        benchmarks.setData(null);
      }
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
        // full path disambiguates duplicate basenames across roots
        // (the server documents that case; both resolve to the first
        // root at action time, so at least make the ambiguity visible)
        detail: entry.project_yaml_path,
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

  const onboardingDocs = new Map<string, string>();
  const onboardingChanged = new vscode.EventEmitter<vscode.Uri>();
  const onboardingProvider: vscode.TextDocumentContentProvider = {
    onDidChange: onboardingChanged.event,
    provideTextDocumentContent: (uri) =>
      onboardingDocs.get(uri.path) ??
      "onboarding not loaded — run “Dispatcher: Project Onboarding”",
  };

  // Late-response guard: a re-run of the command for the SAME project
  // must own the document — a slower older run resuming after it would
  // otherwise overwrite the fresher content.
  const onboardingGen = new Map<string, number>();

  function errorText(reason: unknown): string {
    return reason instanceof ApiError ? reason.detail : String(reason);
  }

  async function showOnboarding(name: string): Promise<void> {
    const uri = vscode.Uri.parse(
      `dispatcher-onboarding:/${encodeURIComponent(name)}.md`,
    );
    const myGen = (onboardingGen.get(uri.path) ?? 0) + 1;
    onboardingGen.set(uri.path, myGen);
    // The two requests are INDEPENDENT: one failing must not take the
    // other down — each side lands in the doc as content or as its own
    // fail-loud error line (composeProjectDoc).
    const api = client();
    const [ob, pp, rn] = await Promise.allSettled([
      api.getOnboarding(name),
      api.getProductProposals(name),
      api.getProjectRuns(name),
    ]);
    if (onboardingGen.get(uri.path) !== myGen) {
      return; // a newer run already owns this document
    }
    let onboarding: { view?: OnboardingView; error?: string };
    if (ob.status !== "fulfilled") {
      onboarding = { error: errorText(ob.reason) };
    } else if (typeof ob.value?.project?.name !== "string") {
      onboarding = { error: "malformed onboarding response" };
    } else {
      onboarding = { view: ob.value };
    }
    const proposals: {
      report?: ProductProposalsReport | null;
      error?: string;
    } =
      pp.status === "fulfilled"
        ? { report: pp.value }
        : { error: errorText(pp.reason) };
    const runs: { snap?: RunsSnapshot | null; error?: string } =
      rn.status === "fulfilled"
        ? { snap: rn.value }
        : { error: errorText(rn.reason) };
    onboardingDocs.set(
      uri.path,
      composeProjectDoc(name, onboarding, proposals, runs),
    );
    onboardingChanged.fire(uri); // re-run refreshes the SAME document
    const doc = await vscode.workspace.openTextDocument(uri);
    await vscode.commands.executeCommand("markdown.showPreview", doc.uri);
  }

  async function onboardingCommand(node?: ProjectNode): Promise<void> {
    if (node !== undefined && node.kind === "project") {
      await showOnboarding(node.entry.name);
      return;
    }
    // palette path: FRESH overview, never the provider's poll state
    let names: string[];
    try {
      const overview = await client().overview();
      names = overview.projects.filter((p) => p.detected).map((p) => p.name);
    } catch (err) {
      void vscode.window.showErrorMessage(
        err instanceof ApiError ? err.detail : String(err),
      );
      return;
    }
    const pick = await vscode.window.showQuickPick(names, {
      title: "Project onboarding",
    });
    if (pick !== undefined) {
      await showOnboarding(pick);
    }
  }

  function impresarioPath(): string | null {
    const entry = lastOverview?.projects.find(
      (p) => p.name === "impresario" && p.detected,
    );
    return entry?.path ?? null;
  }

  /** Prepare a wait's act (spec §3) — opens or types, never executes.
   * Every outcome is visible: a failure becomes a message, never silence. */
  async function myTurnAct(wait: HumanWait): Promise<void> {
    try {
      await prepareAndOpen(wait);
    } catch (err) {
      void vscode.window.showErrorMessage(
        `My turn: could not open "${wait.title}": ${errorText(err)}`,
      );
    }
  }

  async function prepareAndOpen(wait: HumanWait): Promise<void> {
    const prepared = prepareAct(wait.act, {
      baseUrl: readConfig().url,
      impresarioPath: impresarioPath(),
    });
    switch (prepared.kind) {
      case "url":
        if (!(await vscode.env.openExternal(vscode.Uri.parse(prepared.url)))) {
          await vscode.env.clipboard.writeText(prepared.url);
          void vscode.window.showWarningMessage(
            `could not open the browser — copied ${prepared.url}`,
          );
        }
        return;
      case "terminal": {
        const terminal = vscode.window.createTerminal({ name: prepared.name });
        terminal.show();
        terminal.sendText(prepared.text, false); // typed in, NOT executed
        void vscode.window.showInformationMessage(prepared.note);
        return;
      }
      case "file": {
        const uri = vscode.Uri.file(prepared.path);
        const stat = await vscode.workspace.fs.stat(uri).then(
          (s) => s,
          () => null,
        );
        if (stat === null) {
          void vscode.window.showWarningMessage(
            `not found under the impresario mirror: ${prepared.path}`,
          );
        } else if (stat.type === vscode.FileType.Directory) {
          // revealInExplorer is a silent no-op outside the workspace; the
          // OS file manager works for any path.
          await vscode.commands.executeCommand("revealFileInOS", uri);
        } else {
          await vscode.window.showTextDocument(uri);
        }
        return;
      }
      case "clipboard":
        await vscode.env.clipboard.writeText(prepared.text);
        void vscode.window.showInformationMessage(prepared.note);
        return;
      case "refused":
        void vscode.window.showWarningMessage(prepared.note);
        return;
      case "human_merge": {
        // Review first, merge second: both are offered, neither is done.
        const open = "Open PR in browser";
        const type = "Type human-merge command (not executed)";
        const pick = await vscode.window.showQuickPick([open, type], {
          title: wait.title,
        });
        if (pick === open) {
          await vscode.env.openExternal(vscode.Uri.parse(prepared.url));
        } else if (pick === type) {
          const terminal = vscode.window.createTerminal({
            name: "human-merge",
          });
          terminal.show();
          terminal.sendText(prepared.command, false); // typed in, NOT executed
          void vscode.window.showInformationMessage(prepared.note);
        }
        return;
      }
    }
  }

  /** Prepare `run-end` for a stale run — the outcome is the human's call. */
  /** Halt or lift, by explicit human act (spec D1): action, scope, reason,
   * confirmation — then the server records it and applies it in the
   * background; the floor's halt node shows the read-back. */
  async function haltCommand(): Promise<void> {
    const api = client();
    let view: HaltView;
    try {
      view = await api.halt();
    } catch (err) {
      void vscode.window.showErrorMessage(`Halt: cannot read the halt — ${errorText(err)}`);
      return;
    }
    if (view.sources["forge_halt"]?.state === "not_configured") {
      void vscode.window.showWarningMessage(
        "Halt: halt_fleet is empty in dispatcher.toml — nothing can be halted.",
      );
      return;
    }
    if (view.fleet.length === 0) {
      // Never act blind, and never ask "halt 0 repos?" (review #287).
      void vscode.window.showWarningMessage(
        "Halt: the fleet's halt is still being read — try again in a moment.",
      );
      return;
    }
    const action = await vscode.window.showQuickPick(
      [
        { label: "$(debug-stop) Halt", description: "nothing lands on the default branch except by an admin", target: "on" as const },
        { label: "$(debug-start) Lift", description: "merges land again", target: "off" as const },
      ],
      { title: `Halt · ${haltSummary(view).label}` },
    );
    if (!action) {
      return;
    }
    const scope = await vscode.window.showQuickPick(
      [
        { label: `Whole fleet (${view.fleet.length} repos)`, repos: null as string[] | null },
        ...view.fleet.map((r) => ({ label: r.repo, description: r.state, repos: [r.repo] })),
      ],
      { title: `${action.target === "on" ? "Halt" : "Lift"} — which repos?` },
    );
    if (!scope) {
      return;
    }
    const reason = await vscode.window.showInputBox({
      title: "Reason (recorded with the request)",
      prompt: "one line — why the factory stops, or why it may resume",
      validateInput: (v) => reasonProblem(v),
    });
    if (reason === undefined || reasonProblem(reason) !== null) {
      return;
    }
    const count = scope.repos === null ? view.fleet.length : scope.repos.length;
    const verb = action.target === "on" ? "Halt" : "Lift the halt on";
    const confirm = await vscode.window.showWarningMessage(
      `${verb} ${count} repo(s)? Runs already admitted keep working on their branches.`,
      { modal: true },
      verb,
    );
    if (confirm !== verb) {
      return;
    }
    try {
      const request = await api.setHalt(action.target, scope.repos, reason.trim());
      void vscode.window.showInformationMessage(
        `Halt request ${request.request_id.slice(0, 8)} recorded; applying repo by repo. ` +
          "The Factory floor shows what GitHub reads back.",
      );
    } catch (err) {
      void vscode.window.showErrorMessage(`Halt: ${errorText(err)}`);
    }
    void poll();
  }

  async function floorRunEnd(run: InFlightRun): Promise<void> {
    if (run.act === null) {
      return;
    }
    const outcome = await vscode.window.showQuickPick(
      [
        { label: "superseded", description: "another run did this work" },
        { label: "cancelled", description: "this work was abandoned" },
      ],
      { title: `End run ${run.run_id}? Pick the outcome (typed, not executed)` },
    );
    if (outcome === undefined) {
      return;
    }
    // Asked here, not typed after the fact: free text pasted after a bare
    // `--reason` reaches the shell unquoted (observed live 2026-09-30).
    const reason = await vscode.window.showInputBox({
      title: `Why is run ${run.run_id} ${outcome.label}?`,
      prompt: "Stored with the outcome. One line; it will be quoted for you.",
      validateInput: (value) =>
        value.trim() === "" ? "a reason is required" : undefined,
    });
    if (reason === undefined) {
      return;
    }
    const prepared = prepareRunEnd(
      run.act,
      outcome.label as RunEndOutcome,
      reason,
    );
    if (prepared.kind === "refused") {
      void vscode.window.showWarningMessage(prepared.note);
      return;
    }
    const terminal = vscode.window.createTerminal({ name: prepared.name });
    terminal.show();
    terminal.sendText(prepared.text, false); // typed in, NOT executed
    void vscode.window.showInformationMessage(
      run.request_id
        ? `${prepared.note} dispatcher's launch record ${run.request_id} stays ` +
            "open after a CLI run-end (known gap, TODO launchpad-active-stale-records)."
        : prepared.note,
    );
  }

  const timer = setInterval(() => void poll(), readConfig().pollSeconds * 1000);

  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("dispatcherProjects", projects),
    vscode.window.registerTreeDataProvider("dispatcherErrors", errors),
    vscode.window.registerTreeDataProvider("dispatcherRoadmap", roadmap),
    vscode.window.registerTreeDataProvider("dispatcherSync", sync),
    vscode.window.registerTreeDataProvider("dispatcherBenchmarks", benchmarks),
    vscode.window.registerTreeDataProvider("dispatcherMyTurn", myTurn),
    vscode.window.registerTreeDataProvider("dispatcherFloor", floor),
    vscode.commands.registerCommand("dispatcher.halt", () => void haltCommand()),
    vscode.commands.registerCommand(
      "dispatcher.floorRunEnd",
      (run: InFlightRun) => void floorRunEnd(run),
    ),
    status.item,
    myTurnStatus.item,
    vscode.commands.registerCommand(
      "dispatcher.myTurnAct",
      (wait: HumanWait) => void myTurnAct(wait),
    ),
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
    vscode.workspace.registerTextDocumentContentProvider(
      "dispatcher-onboarding",
      onboardingProvider,
    ),
    vscode.commands.registerCommand(
      "dispatcher.projectOnboarding",
      (node?: ProjectNode) => void onboardingCommand(node),
    ),
    onboardingChanged,
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
    { dispose: () => myTurn.dispose() },
    { dispose: () => floor.dispose() },
  );

  void poll();
}

export function deactivate(): void {}
