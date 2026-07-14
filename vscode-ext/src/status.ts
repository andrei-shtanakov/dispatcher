/** Status-bar item: thin adapter over model.statusText. */

import * as vscode from "vscode";
import type { OverviewResponse, SyncStatusResponse } from "./api";
import { statusText, verdictText } from "./model";

export function createStatusBar(): {
  item: vscode.StatusBarItem;
  update: (
    overview: OverviewResponse | null,
    sync?: SyncStatusResponse | null,
  ) => void;
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
    update: (overview, sync = null) => {
      item.text = statusText(overview) + verdictText(sync);
      const reason = sync?.report.top_reason;
      item.tooltip =
        overview === null
          ? "dispatcher: server unreachable"
          : "dispatcher: detected✓ with-errors✗" +
            (reason ? `\nsync: ${reason}` : "");
    },
  };
}
