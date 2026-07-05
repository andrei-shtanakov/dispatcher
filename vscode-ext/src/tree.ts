/** TreeDataProviders: thin adapters over the pure model mappers. */

import * as vscode from "vscode";
import type { ApiClient, ErrorEvent, OverviewEntry } from "./api";
import { detailLines, errorLabel, projectView } from "./model";

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
    const detail = await this.api().project(node.entry.name);
    return detailLines(detail).map((text) => ({ kind: "line", text }));
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
