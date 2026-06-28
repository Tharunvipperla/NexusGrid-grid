import * as vscode from "vscode";
import { getTasks, getDeposits, resolveBaseUrl, TaskInfo, DepositInfo, TERMINAL_STATUSES } from "./client";

// Tree of task/service rows. Empty + unreachable states return [] so the view's
// welcome content (package.json `viewsWelcome`) shows instead; status is reported
// to the extension via `onState` (sets the connected context + view message).

/** A task row carrying its id + status so commands can act on it. */
export class TaskItem extends vscode.TreeItem {
  constructor(public readonly taskId: string, public readonly status: string, displayId: string, worker: string) {
    super(displayId, vscode.TreeItemCollapsibleState.None);
    this.iconPath = new vscode.ThemeIcon(statusIcon(status));
    this.description = worker ? `${status} · ${worker}` : status;
    this.tooltip = `${taskId} (${status})`;
    this.contextValue = TERMINAL_STATUSES.has(status) ? "nexusTaskDone" : "nexusTaskActive";
    this.command = { command: "nexusgrid.viewLogs", title: "View logs", arguments: [this] };
  }
}

/** A service row (a serving task) with start/stop affordances. */
export class ServiceItem extends vscode.TreeItem {
  constructor(public readonly taskId: string, public readonly status: string, displayId: string, worker: string) {
    super(displayId, vscode.TreeItemCollapsibleState.None);
    const running = status === "processing";
    this.iconPath = new vscode.ThemeIcon("server-process");
    this.description = `${running ? "service · running" : "service · stopped"}${worker ? ` · ${worker}` : ""}`;
    this.tooltip = `${taskId} (service, ${status})`;
    this.contextValue = running ? "nexusServiceRunning" : "nexusServiceStopped";
    this.command = { command: "nexusgrid.viewLogs", title: "View logs", arguments: [this] };
  }
}

/** Collapsible parent for the deposits this node owns elsewhere. */
class DepositsRoot extends vscode.TreeItem {
  constructor(count: number) {
    super(`Deposits (${count})`, vscode.TreeItemCollapsibleState.Collapsed);
    this.iconPath = new vscode.ThemeIcon("database");
    this.contextValue = "nexusDepositsRoot";
  }
}

/** A deposit row with a retrieve affordance. */
export class DepositItem extends vscode.TreeItem {
  constructor(public readonly depositId: string, public readonly status: string, filename: string, host: string, bytes: number) {
    super(filename || depositId, vscode.TreeItemCollapsibleState.None);
    this.iconPath = new vscode.ThemeIcon("file-binary");
    this.description = [status, host, bytes ? fmtBytes(bytes) : ""].filter(Boolean).join(" · ");
    this.tooltip = `${depositId} (${status})`;
    this.contextValue = "nexusDeposit";
  }
}

export interface NodeState {
  connected: boolean;
  url: string;
  error?: string;
}

export class NodeTreeProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this._onDidChange.event;

  /** Set by the extension to reflect connection status in the UI. */
  onState?: (state: NodeState) => void;

  refresh(): void {
    this._onDidChange.fire();
  }

  /** Deposits cached from the last root fetch, handed to the Deposits parent. */
  private deposits: DepositInfo[] = [];

  getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
    return element;
  }

  async getChildren(element?: vscode.TreeItem): Promise<vscode.TreeItem[]> {
    if (element instanceof DepositsRoot) {
      return this.deposits.map((d) => new DepositItem(d.depositId, d.status, d.filename, d.host, d.bytes));
    }
    let tasks: TaskInfo[];
    try {
      tasks = await getTasks();
    } catch (err: any) {
      this.onState?.({ connected: false, url: resolveBaseUrl(), error: String(err?.message || err) });
      return [];
    }
    this.onState?.({ connected: true, url: resolveBaseUrl() });
    // Deposits are secondary — never let their absence/failure break the task list.
    this.deposits = await getDeposits().catch(() => []);
    // Most recent first by id (ids are time-ordered enough for a quick view).
    tasks.sort((a, b) => (a.id < b.id ? 1 : -1));
    const items: vscode.TreeItem[] = tasks
      .slice(0, 25)
      .map((t) =>
        t.coordination === "serving"
          ? new ServiceItem(t.id, t.status, t.displayId, t.worker)
          : new TaskItem(t.id, t.status, t.displayId, t.worker)
      );
    if (this.deposits.length) {
      items.push(new DepositsRoot(this.deposits.length));
    }
    return items;
  }
}

function fmtBytes(n: number): string {
  if (n < 1024) {
    return `${n} B`;
  }
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(1)} ${units[i]}`;
}

function statusIcon(status: string): string {
  switch (status) {
    case "completed":
      return "pass";
    case "failed":
      return "error";
    case "processing":
      return "sync";
    default:
      return "circle-outline";
  }
}
