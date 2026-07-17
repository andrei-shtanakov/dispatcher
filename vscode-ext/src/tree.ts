/** TreeDataProviders: thin adapters over the pure model mappers. */

import * as vscode from "vscode";
import type {
  ApiClient,
  ErrorEvent,
  EvidenceResult,
  HostPanel,
  OverviewEntry,
  RepoVerdict,
  RoadmapItemView,
  RoadmapResponse,
  SyncStatusResponse,
} from "./api";
import {
  detailLines,
  errorLabel,
  evidenceLabel,
  projectView,
  roadmapEmptyText,
  roadmapItemChildren,
  roadmapItemDescription,
  roadmapItemLabel,
  roadmapStatusIcon,
  syncAgeLabel,
  syncItemContext,
  syncVerdictIcon,
} from "./model";

export type ProjectNode =
  | { kind: "project"; entry: OverviewEntry }
  | { kind: "line"; text: string }
  | { kind: "offline" };

function offlineItem(): vscode.TreeItem {
  const item = new vscode.TreeItem("server unreachable");
  item.iconPath = new vscode.ThemeIcon("debug-disconnected");
  item.command = {
    command: "dispatcher.startServer",
    title: "Start server",
  };
  return item;
}

export class ProjectsProvider
  implements vscode.TreeDataProvider<ProjectNode>
{
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private entries: OverviewEntry[] | null = null; // null = offline

  constructor(private readonly api: () => ApiClient) {}

  setData(entries: OverviewEntry[] | null): void {
    this.entries = entries;
    this.changed.fire();
  }

  getTreeItem(node: ProjectNode): vscode.TreeItem {
    if (node.kind === "offline") {
      return offlineItem();
    }
    if (node.kind === "line") {
      return new vscode.TreeItem(node.text);
    }
    const view = projectView(node.entry, new Date());
    const item = new vscode.TreeItem(
      view.name,
      view.detected
        ? vscode.TreeItemCollapsibleState.Collapsed
        : vscode.TreeItemCollapsibleState.None,
    );
    item.description = view.description;
    item.iconPath =
      view.health === "ok"
        ? new vscode.ThemeIcon(
            "circle-filled",
            new vscode.ThemeColor("testing.iconPassed"),
          )
        : view.health === "err"
          ? new vscode.ThemeIcon(
              "circle-filled",
              new vscode.ThemeColor("testing.iconFailed"),
            )
          : new vscode.ThemeIcon("circle-outline");
    return item;
  }

  async getChildren(node?: ProjectNode): Promise<ProjectNode[]> {
    if (node === undefined) {
      if (this.entries === null) {
        return [{ kind: "offline" }];
      }
      return this.entries.map((entry) => ({ kind: "project", entry }));
    }
    if (node.kind !== "project" || !node.entry.detected) {
      return [];
    }
    try {
      const detail = await this.api().project(node.entry.name);
      return detailLines(detail).map((text) => ({ kind: "line", text }));
    } catch {
      return [{ kind: "line", text: "detail unavailable" }];
    }
  }

  dispose(): void {
    this.changed.dispose();
  }
}

export type RoadmapNode =
  | { kind: "item"; item: RoadmapItemView }
  | { kind: "evidence"; evidence: EvidenceResult }
  | { kind: "line"; text: string }
  | { kind: "empty"; text: string }
  | { kind: "offline" };

export class RoadmapProvider implements vscode.TreeDataProvider<RoadmapNode> {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private roadmap: RoadmapResponse | null = null; // null = offline

  setData(roadmap: RoadmapResponse | null): void {
    this.roadmap = roadmap;
    this.changed.fire();
  }

  getTreeItem(node: RoadmapNode): vscode.TreeItem {
    if (node.kind === "offline") {
      return offlineItem();
    }
    if (node.kind === "empty") {
      const item = new vscode.TreeItem(node.text);
      item.iconPath = new vscode.ThemeIcon("info");
      return item;
    }
    if (node.kind === "line") {
      return new vscode.TreeItem(node.text);
    }
    if (node.kind === "evidence") {
      const item = new vscode.TreeItem(evidenceLabel(node.evidence));
      item.tooltip = node.evidence.detail;
      item.iconPath = node.evidence.passed
        ? new vscode.ThemeIcon(
            "check",
            new vscode.ThemeColor("testing.iconPassed"),
          )
        : new vscode.ThemeIcon(
            "close",
            new vscode.ThemeColor("testing.iconFailed"),
          );
      return item;
    }
    const item = new vscode.TreeItem(
      roadmapItemLabel(node.item),
      vscode.TreeItemCollapsibleState.Collapsed,
    );
    item.description = roadmapItemDescription(node.item);
    item.tooltip = [
      node.item.title,
      `phase: ${node.item.phase ?? "—"}`,
      `owner: ${node.item.owner_project ?? "—"}`,
      `status: ${node.item.computed_status}`,
      `source: ${node.item.source}`,
    ].join("\n");
    const icon = roadmapStatusIcon(node.item.computed_status);
    item.iconPath =
      icon.color === null
        ? new vscode.ThemeIcon(icon.icon)
        : new vscode.ThemeIcon(icon.icon, new vscode.ThemeColor(icon.color));
    return item;
  }

  getChildren(node?: RoadmapNode): RoadmapNode[] {
    if (node === undefined) {
      if (this.roadmap === null) {
        return [{ kind: "offline" }];
      }
      const empty = roadmapEmptyText(this.roadmap);
      if (empty !== null) {
        return [{ kind: "empty", text: empty }];
      }
      return this.roadmap.items.map((item) => ({ kind: "item", item }));
    }
    if (node.kind !== "item") {
      return [];
    }
    return roadmapItemChildren(node.item).map((child) =>
      child.kind === "evidence"
        ? { kind: "evidence", evidence: child.evidence }
        : { kind: "line", text: child.text },
    );
  }

  dispose(): void {
    this.changed.dispose();
  }
}

export type ErrorNode =
  | { kind: "error"; event: ErrorEvent }
  | { kind: "empty" }
  | { kind: "offline" };

export class ErrorsProvider implements vscode.TreeDataProvider<ErrorNode> {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private events: ErrorEvent[] | null = null; // null = offline

  setData(events: ErrorEvent[] | null): void {
    this.events = events;
    this.changed.fire();
  }

  getTreeItem(node: ErrorNode): vscode.TreeItem {
    if (node.kind === "offline") {
      return offlineItem();
    }
    if (node.kind === "empty") {
      const item = new vscode.TreeItem("no errors 🎉");
      item.iconPath = new vscode.ThemeIcon(
        "check",
        new vscode.ThemeColor("testing.iconPassed"),
      );
      return item;
    }
    const item = new vscode.TreeItem(errorLabel(node.event));
    item.tooltip = node.event.body;
    item.iconPath = new vscode.ThemeIcon(
      "error",
      new vscode.ThemeColor("testing.iconFailed"),
    );
    item.command = {
      command: "dispatcher.showError",
      title: "Show error body",
      arguments: [node.event.body],
    };
    return item;
  }

  getChildren(node?: ErrorNode): ErrorNode[] {
    if (node !== undefined) {
      return [];
    }
    if (this.events === null) {
      return [{ kind: "offline" }];
    }
    if (this.events.length === 0) {
      return [{ kind: "empty" }];
    }
    return this.events.map((event) => ({ kind: "error", event }));
  }

  dispose(): void {
    this.changed.dispose();
  }
}

export type SyncNode =
  | { kind: "host"; panel: HostPanel }
  | { kind: "verdict"; v: RepoVerdict; live: boolean }
  | { kind: "proposal"; dir: string }
  | { kind: "offline" };

export class SyncProvider implements vscode.TreeDataProvider<SyncNode> {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private sync: SyncStatusResponse | null = null; // null = offline

  setData(sync: SyncStatusResponse | null): void {
    this.sync = sync;
    this.changed.fire();
  }

  getTreeItem(node: SyncNode): vscode.TreeItem {
    if (node.kind === "offline") {
      return offlineItem();
    }
    if (node.kind === "proposal") {
      const item = new vscode.TreeItem(node.dir);
      item.description = "proposal";
      item.contextValue = "dispatcherSyncProposal";
      item.iconPath = new vscode.ThemeIcon("question");
      return item;
    }
    if (node.kind === "host") {
      const panel = node.panel;
      const item = new vscode.TreeItem(
        `${panel.host} (${panel.source})`,
        panel.error === null && panel.verdicts.length > 0
          ? vscode.TreeItemCollapsibleState.Collapsed
          : vscode.TreeItemCollapsibleState.None,
      );
      if (panel.error !== null) {
        item.description = panel.error;
        item.iconPath = new vscode.ThemeIcon(
          "error",
          new vscode.ThemeColor("testing.iconFailed"),
        );
        return item;
      }
      item.description = syncAgeLabel(panel.age_seconds, panel.stale);
      item.iconPath = new vscode.ThemeIcon("server-environment");
      return item;
    }
    const v = node.v;
    const item = new vscode.TreeItem(v.is_kb ? `📌 ${v.repo}` : v.repo);
    item.description =
      `↑${v.ahead ?? "—"}/↓${v.behind ?? "—"}` + (v.dirty ? " ✎" : "");
    item.tooltip = v.reason ?? undefined;
    const icon = syncVerdictIcon(v.verdict);
    item.iconPath =
      icon.color === null
        ? new vscode.ThemeIcon(icon.icon)
        : new vscode.ThemeIcon(icon.icon, new vscode.ThemeColor(icon.color));
    item.contextValue = syncItemContext(v, node.live) ?? undefined;
    return item;
  }

  getChildren(node?: SyncNode): SyncNode[] {
    if (node === undefined) {
      if (this.sync === null) {
        return [{ kind: "offline" }];
      }
      const hosts: SyncNode[] = this.sync.report.hosts.map((panel) => ({
        kind: "host",
        panel,
      }));
      const proposals: SyncNode[] = this.sync.report.proposals.map((dir) => ({
        kind: "proposal",
        dir,
      }));
      return [...hosts, ...proposals];
    }
    if (node.kind !== "host" || node.panel.error !== null) {
      return [];
    }
    const live = node.panel.source === "live";
    return node.panel.verdicts.map((v) => ({ kind: "verdict", v, live }));
  }

  dispose(): void {
    this.changed.dispose();
  }
}
