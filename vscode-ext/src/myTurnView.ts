/** "My turn" tree and status item: thin adapters over myTurn.ts. */

import * as vscode from "vscode";
import type { HumanQueueView, HumanWait } from "./api";
import {
  type GroupKey,
  REASON_LABEL,
  emptyText,
  groupWaits,
  incompleteLines,
  isOverdue,
  myTurnStatus,
  waitDescription,
} from "./myTurn";

/** What the view knows: a queue, the server being down, or the queue failing. */
export type MyTurnState =
  | { kind: "view"; view: HumanQueueView }
  | { kind: "offline" }
  | { kind: "unavailable"; detail: string };

export type MyTurnNode =
  | { kind: "banner"; lines: string[] }
  | { kind: "bannerLine"; text: string }
  | { kind: "group"; reason: GroupKey; waits: HumanWait[] }
  | { kind: "wait"; wait: HumanWait }
  | { kind: "text"; text: string; icon: string };

export class MyTurnProvider implements vscode.TreeDataProvider<MyTurnNode> {
  private readonly changed = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this.changed.event;
  private state: MyTurnState = { kind: "offline" };

  constructor(private readonly overdueHours: () => number) {}

  setState(state: MyTurnState): void {
    this.state = state;
    this.changed.fire();
  }

  getTreeItem(node: MyTurnNode): vscode.TreeItem {
    switch (node.kind) {
      case "banner": {
        const item = new vscode.TreeItem(
          "queue incomplete",
          vscode.TreeItemCollapsibleState.Collapsed,
        );
        item.iconPath = new vscode.ThemeIcon(
          "warning",
          new vscode.ThemeColor("list.warningForeground"),
        );
        item.description = `${node.lines.length} source(s) not read`;
        return item;
      }
      case "bannerLine":
        return new vscode.TreeItem(node.text);
      case "group": {
        const item = new vscode.TreeItem(
          `${REASON_LABEL[node.reason]} (${node.waits.length})`,
          vscode.TreeItemCollapsibleState.Expanded,
        );
        item.iconPath = new vscode.ThemeIcon("person");
        return item;
      }
      case "wait":
        return this.waitItem(node.wait);
      case "text": {
        const item = new vscode.TreeItem(node.text);
        item.iconPath = new vscode.ThemeIcon(node.icon);
        return item;
      }
    }
  }

  private waitItem(wait: HumanWait): vscode.TreeItem {
    const now = new Date();
    const item = new vscode.TreeItem(wait.title);
    item.description = waitDescription(wait, now);
    item.tooltip = [
      wait.key,
      wait.since_basis ? `since: ${wait.since} (${wait.since_basis})` : "age unknown",
      `act: ${wait.act.kind}`,
    ].join("\n");
    item.iconPath = isOverdue(wait, now, this.overdueHours())
      ? new vscode.ThemeIcon(
          "warning",
          new vscode.ThemeColor("list.errorForeground"),
        )
      : new vscode.ThemeIcon("circle-outline");
    item.contextValue = "dispatcherMyTurnWait";
    item.command = {
      command: "dispatcher.myTurnAct",
      title: "Prepare act",
      arguments: [wait],
    };
    return item;
  }

  getChildren(node?: MyTurnNode): MyTurnNode[] {
    if (node !== undefined) {
      if (node.kind === "banner") {
        return node.lines.map((text) => ({ kind: "bannerLine", text }));
      }
      if (node.kind === "group") {
        return node.waits.map((wait) => ({ kind: "wait", wait }));
      }
      return [];
    }
    if (this.state.kind === "offline") {
      return [{ kind: "text", text: "server unreachable", icon: "debug-disconnected" }];
    }
    if (this.state.kind === "unavailable") {
      return [
        {
          kind: "text",
          text: `human queue unavailable: ${this.state.detail}`,
          icon: "error",
        },
      ];
    }
    const view = this.state.view;
    const roots: MyTurnNode[] = [];
    if (!view.complete) {
      roots.push({ kind: "banner", lines: incompleteLines(view) });
    }
    const empty = emptyText(view);
    if (empty !== null) {
      roots.push({ kind: "text", text: empty, icon: view.complete ? "check" : "question" });
      return roots;
    }
    roots.push(
      ...groupWaits(view).map(
        (g): MyTurnNode => ({ kind: "group", reason: g.reason, waits: g.waits }),
      ),
    );
    return roots;
  }

  dispose(): void {
    this.changed.dispose();
  }
}

export function createMyTurnStatus(overdueHours: () => number): {
  item: vscode.StatusBarItem;
  update: (view: HumanQueueView | null) => void;
} {
  const item = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left,
    99,
  );
  item.name = "Dispatcher: My turn";
  item.command = "dispatcherMyTurn.focus"; // auto-generated view command
  item.show();
  return {
    item,
    update: (view) => {
      const status = myTurnStatus(view, new Date(), overdueHours());
      item.text = status.text;
      item.tooltip = status.tooltip;
      item.backgroundColor = status.overdue
        ? new vscode.ThemeColor("statusBarItem.errorBackground")
        : undefined;
    },
  };
}
