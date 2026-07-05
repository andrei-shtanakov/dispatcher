/** Status-bar item: thin adapter over model.statusText. */

import * as vscode from "vscode";
import type { OverviewResponse } from "./api";
import { statusText } from "./model";

export function createStatusBar(): {
  item: vscode.StatusBarItem;
  update: (overview: OverviewResponse | null) => void;
} {
  const item = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left,
    100,
  );
  item.name = "Dispatcher";
  item.command = "dispatcherProjects.focus"; // auto-generated view command
  item.show();
  return {
    item,
    update: (overview) => {
      item.text = statusText(overview);
      item.tooltip =
        overview === null
          ? "dispatcher: server unreachable"
          : "dispatcher: detected✓ with-errors✗";
    },
  };
}
