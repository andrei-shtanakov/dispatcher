/** "Factory floor" tree: a thin adapter over floor.ts. */

import * as vscode from "vscode";
import type { AgentMerge, FactoryFloorView, InFlightRun } from "./api";
import {
  floorEmptyText,
  floorGroups,
  launchLine,
  mergeDescription,
  mergeLabel,
  mergesSection,
  runDescription,
  runLabel,
} from "./floor";
import { incompleteLines } from "./myTurn";
import { offlineItem } from "./tree";

export type FloorState =
  | { kind: "view"; view: FactoryFloorView }
  | { kind: "offline" }
  | { kind: "unavailable"; detail: string };

type FloorNode =
  | { kind: "banner"; lines: string[] }
  | { kind: "line"; text: string }
  | { kind: "group"; label: string; stale: boolean; runs: InFlightRun[] }
  | { kind: "run"; run: InFlightRun }
  | { kind: "merges"; label: string; merges: AgentMerge[] }
  | { kind: "merge"; merge: AgentMerge }
  | { kind: "text"; text: string; icon: string }
  | { kind: "offline" };

export class FloorProvider implements vscode.TreeDataProvider<FloorNode> {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private state: FloorState = { kind: "offline" };

  setState(state: FloorState): void {
    this.state = state;
    this.changed.fire();
  }

  getTreeItem(node: FloorNode): vscode.TreeItem {
    switch (node.kind) {
      case "offline":
        return offlineItem();
      case "banner": {
        const item = new vscode.TreeItem(
          "sources incomplete",
          vscode.TreeItemCollapsibleState.Collapsed,
        );
        item.iconPath = new vscode.ThemeIcon(
          "warning",
          new vscode.ThemeColor("list.warningForeground"),
        );
        return item;
      }
      case "line":
        return new vscode.TreeItem(node.text);
      case "group": {
        const item = new vscode.TreeItem(
          `${node.label} (${node.runs.length})`,
          vscode.TreeItemCollapsibleState.Expanded,
        );
        item.iconPath = new vscode.ThemeIcon(node.stale ? "warning" : "sync");
        return item;
      }
      case "run":
        return this.runItem(node.run, this.recordsRead());
      case "merges": {
        const item = new vscode.TreeItem(
          node.label,
          node.merges.length > 0
            ? vscode.TreeItemCollapsibleState.Collapsed
            : vscode.TreeItemCollapsibleState.None,
        );
        item.iconPath = new vscode.ThemeIcon("git-merge");
        return item;
      }
      case "merge":
        return this.mergeItem(node.merge);
      case "text": {
        const item = new vscode.TreeItem(node.text);
        item.iconPath = new vscode.ThemeIcon(node.icon);
        return item;
      }
    }
  }

  private recordsRead(): boolean {
    if (this.state.kind !== "view") {
      return false;
    }
    const s = this.state.view.sources["dispatcher_runs"]?.state;
    return s === "ok" || s === "not_configured";
  }

  private runItem(run: InFlightRun, recordsRead: boolean): vscode.TreeItem {
    const item = new vscode.TreeItem(runLabel(run));
    item.description = runDescription(run, new Date());
    item.tooltip = [
      `repo: ${run.repo_key}`,
      `run: ${run.run_id}`,
      launchLine(run, recordsRead),
      run.started_at ? `started: ${run.started_at}` : "start unknown",
      run.last_activity_at
        ? `last activity: ${run.last_activity_at}`
        : "last activity unknown",
    ].join("\n");
    item.iconPath = run.stale
      ? new vscode.ThemeIcon("warning", new vscode.ThemeColor("list.warningForeground"))
      : new vscode.ThemeIcon(run.status === "running" ? "play" : "debug-pause");
    if (run.act !== null) {
      item.command = {
        command: "dispatcher.floorRunEnd",
        title: "Prepare run-end",
        arguments: [run],
      };
    }
    return item;
  }

  private mergeItem(merge: AgentMerge): vscode.TreeItem {
    const item = new vscode.TreeItem(mergeLabel(merge));
    item.description = mergeDescription(merge, new Date());
    item.tooltip = `${merge.repo}#${merge.number}\nmerged: ${merge.merged_at}\n${merge.url}`;
    item.iconPath = new vscode.ThemeIcon("git-pull-request");
    item.command = {
      command: "vscode.open",
      title: "Open PR",
      arguments: [vscode.Uri.parse(merge.url)],
    };
    return item;
  }

  getChildren(node?: FloorNode): FloorNode[] {
    if (node !== undefined) {
      if (node.kind === "banner") {
        return node.lines.map((text) => ({ kind: "line", text }));
      }
      if (node.kind === "group") {
        return node.runs.map((run) => ({ kind: "run", run }));
      }
      if (node.kind === "merges") {
        return node.merges.map((merge) => ({ kind: "merge", merge }));
      }
      return [];
    }
    if (this.state.kind === "offline") {
      return [{ kind: "offline" }];
    }
    if (this.state.kind === "unavailable") {
      return [{ kind: "text", text: `factory floor unavailable: ${this.state.detail}`, icon: "error" }];
    }
    const view = this.state.view;
    const roots: FloorNode[] = [];
    if (!view.complete) {
      roots.push({
        kind: "banner",
        lines: incompleteLines({
          waits: [],
          sources: view.sources,
          complete: view.complete,
          generated_at: view.generated_at,
        }),
      });
    }
    const empty = floorEmptyText(view);
    if (empty !== null) {
      roots.push({
        kind: "text",
        text: empty,
        icon: empty === "nothing is running" ? "check" : "question",
      });
    }
    roots.push(
      ...floorGroups(view).map(
        (g): FloorNode => ({
          kind: "group",
          label: g.label,
          stale: g.runs[0]?.stale ?? false,
          runs: g.runs,
        }),
      ),
    );
    // After the runs: what the agent did is context, the runs are the work.
    const merges = mergesSection(view);
    if (merges !== null) {
      roots.push({ kind: "merges", label: merges.label, merges: merges.merges });
    }
    return roots;
  }

  dispose(): void {
    this.changed.dispose();
  }
}
