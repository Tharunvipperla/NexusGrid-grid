import * as vscode from "vscode";
import * as path from "path";
import * as fs from "fs";
import { NodeTreeProvider, TaskItem, ServiceItem, DepositItem } from "./tree";
import {
  dispatchTask,
  runOnGrid,
  runDag,
  getWorkers,
  getGroups,
  getNodeSettings,
  updateSettings,
  subscribeEvents,
  resolveBaseUrl,
  resolveToken,
  getLogTail,
  getTaskStatus,
  cancelTask,
  disruptTask,
  requeueTask,
  getResultFiles,
  getResultFile,
  startService,
  stopService,
  getStoragePeers,
  depositFile,
  retrieveDeposit,
  TERMINAL_STATUSES,
} from "./client";
import { getWorkspaceRoot, readOrCreateConfig, configPath, resolveDispatch, setConfigTarget } from "./dispatchConfig";
import { readOrCreateDag, dagConfigPath, setDagTarget } from "./dagConfig";
import { readOrCreateService, serviceConfigPath } from "./serviceConfig";
import { parseDirectives, ParsedDirectives } from "./directives";
import { registerLens } from "./lens";

export function activate(context: vscode.ExtensionContext) {
  const tree = new NodeTreeProvider();
  const treeView = vscode.window.createTreeView("nexusgrid.nodeView", { treeDataProvider: tree });
  context.subscriptions.push(treeView);
  tree.onState = (s) => {
    treeView.message = s.connected ? undefined : `Can't reach ${s.url}`;
    vscode.commands.executeCommand("setContext", "nexusgrid.connected", s.connected);
  };

  // Live two-way sync: refresh whenever anything changes on the grid (including
  // from the web UI). The extension only drives real endpoints, so its own
  // actions show up in the web UI through the same event stream.
  const unsubscribe = subscribeEvents(() => tree.refresh());
  context.subscriptions.push({ dispose: unsubscribe });

  // Inline "Dispatch to <target>" CodeLens + "Nexus Lens" hover on @nexus: lines.
  registerLens(context);

  const tailer = new LogTailer();
  context.subscriptions.push(tailer);

  context.subscriptions.push(
    vscode.commands.registerCommand("nexusgrid.refresh", () => tree.refresh()),

    vscode.commands.registerCommand("nexusgrid.configure", () =>
      vscode.commands.executeCommand("workbench.action.openSettings", "nexusgrid")
    ),

    vscode.commands.registerCommand("nexusgrid.editConfig", async () => {
      const root = getWorkspaceRoot();
      if (!root) {
        vscode.window.showWarningMessage("NexusGrid: open a folder first.");
        return;
      }
      readOrCreateConfig(root); // create with defaults if missing
      await openConfig(root);
    }),

    vscode.commands.registerCommand("nexusgrid.editDagConfig", async () => {
      const root = getWorkspaceRoot();
      if (!root) {
        vscode.window.showWarningMessage("NexusGrid: open a folder first.");
        return;
      }
      readOrCreateDag(root); // create the example if missing
      await openFile(dagConfigPath(root));
    }),

    // Run the workspace's nexus.dag.json as a multi-step pipeline.
    vscode.commands.registerCommand("nexusgrid.runDag", async (resource?: vscode.Uri) => {
      const root = getWorkspaceRoot(resource);
      if (!root) {
        vscode.window.showWarningMessage("NexusGrid: open a folder first.");
        return;
      }
      let result;
      try {
        result = readOrCreateDag(root);
      } catch (err: any) {
        vscode.window.showErrorMessage(`NexusGrid: nexus.dag.json — ${err?.message || err}`);
        await openFile(dagConfigPath(root));
        return;
      }
      if (result.created) {
        vscode.window.showInformationMessage("NexusGrid: created nexus.dag.json — review it, then run again to dispatch.");
        await openFile(dagConfigPath(root));
        return;
      }
      const { steps, opts } = result.config!;
      await dispatch(() => runDag([root], steps, opts), tree, `pipeline (${steps.length} steps)`);
    }),

    vscode.commands.registerCommand("nexusgrid.editServiceConfig", async () => {
      const root = getWorkspaceRoot();
      if (!root) {
        vscode.window.showWarningMessage("NexusGrid: open a folder first.");
        return;
      }
      readOrCreateService(root); // create the example if missing
      await openFile(serviceConfigPath(root));
    }),

    // Deploy the workspace's nexus.service.json as a long-running service.
    vscode.commands.registerCommand("nexusgrid.deployService", async (resource?: vscode.Uri) => {
      const root = getWorkspaceRoot(resource);
      if (!root) {
        vscode.window.showWarningMessage("NexusGrid: open a folder first.");
        return;
      }
      let result;
      try {
        result = readOrCreateService(root);
      } catch (err: any) {
        vscode.window.showErrorMessage(`NexusGrid: nexus.service.json — ${err?.message || err}`);
        await openFile(serviceConfigPath(root));
        return;
      }
      if (result.created) {
        vscode.window.showInformationMessage("NexusGrid: created nexus.service.json — review it, then run again to deploy.");
        await openFile(serviceConfigPath(root));
        return;
      }
      const { steps, opts, ports } = result.deploy!;
      await dispatch(() => runDag([root], steps, opts), tree, `service (ports ${ports.join(", ")})`);
    }),

    vscode.commands.registerCommand("nexusgrid.openControlPanel", () => {
      const token = resolveToken();
      const url = `${resolveBaseUrl()}/app${token ? `?local_token=${encodeURIComponent(token)}` : ""}`;
      vscode.env.openExternal(vscode.Uri.parse(url));
    }),

    // Dispatch with no workspace files, using nexus.json (+ active file directives).
    vscode.commands.registerCommand("nexusgrid.dispatchTask", async () => {
      const ed = vscode.window.activeTextEditor;
      const parsed = ed ? parseDirectives(ed.document.getText()) : undefined;
      await withConfig(undefined, async (config) => {
        const r = resolveDispatch(config, parsed);
        warnUnknown(parsed);
        await dispatch(() => dispatchTask(r.image, r.command, r.runtime, r.opts), tree);
      });
    }),

    // Explorer right-click: run the selected file(s)/folder(s) on the grid.
    vscode.commands.registerCommand(
      "nexusgrid.runOnGrid",
      async (clicked?: vscode.Uri, selected?: vscode.Uri[]) => {
        const uris = (selected && selected.length ? selected : clicked ? [clicked] : []).filter(
          (u) => u.scheme === "file"
        );
        if (uris.length === 0) {
          vscode.window.showWarningMessage("NexusGrid: select a file or folder in the Explorer first.");
          return;
        }
        const label = uris.length === 1 ? path.basename(uris[0].fsPath) : `${uris.length} items`;
        const single = uris.length === 1 ? uris[0] : undefined;
        const parsed = single ? directivesFromFile(single) : undefined;
        await withConfig(uris[0], async (config) => {
          const r = resolveDispatch(config, parsed, single);
          warnUnknown(parsed);
          await dispatch(
            () => runOnGrid(uris.map((u) => u.fsPath), r.image, r.command, r.runtime, r.opts),
            tree,
            label
          );
        });
      }
    ),

    // Pick a real worker/group to target, written into the active config file
    // (nexus.dag.json if it's the active editor, else nexus.json).
    vscode.commands.registerCommand("nexusgrid.setTarget", async () => {
      const root = getWorkspaceRoot();
      if (!root) {
        vscode.window.showWarningMessage("NexusGrid: open a folder first.");
        return;
      }
      let workers, groups;
      try {
        [workers, groups] = await Promise.all([getWorkers(), getGroups()]);
      } catch (err: any) {
        vscode.window.showErrorMessage(`NexusGrid: ${err?.message || err}`);
        return;
      }
      type TargetPick = vscode.QuickPickItem & { sel: "auto" | "worker" | "group"; value?: string };
      const items: TargetPick[] = [
        { label: "$(broadcast) Auto", description: "best fit — scheduler decides", sel: "auto" },
        ...workers.map((w): TargetPick => ({
          label: `$(server) ${w.label}`,
          description: w.id + (w.gpu ? "  • GPU" : ""),
          sel: "worker",
          value: w.id,
        })),
        ...groups.map((g): TargetPick => ({ label: `$(organization) ${g.name}`, description: "group", sel: "group", value: g.id })),
      ];
      const pick = await vscode.window.showQuickPick<TargetPick>(items, {
        placeHolder: "Dispatch target for this workspace",
        ignoreFocusOut: true,
      });
      if (!pick) {
        return;
      }
      const active = vscode.window.activeTextEditor?.document.uri.fsPath;
      const isDag = !!active && path.basename(active) === "nexus.dag.json";
      try {
        if (isDag) {
          setDagTarget(root, pick.sel === "worker" ? [pick.value!] : [], pick.sel === "group" ? [pick.value!] : []);
        } else {
          const target = pick.sel === "auto" ? "auto" : pick.sel === "group" ? `group:${pick.value}` : pick.value!;
          setConfigTarget(root, target);
        }
        vscode.window.showInformationMessage(`NexusGrid: ${isDag ? "pipeline" : "dispatch"} target → ${pick.label.replace(/\$\([^)]*\)\s*/, "")}`);
      } catch (err: any) {
        vscode.window.showErrorMessage(`NexusGrid: ${err?.message || err}`);
      }
    }),

    // Toggle a node setting; writes via settings_partial so the web UI updates live.
    vscode.commands.registerCommand("nexusgrid.toggleSetting", async () => {
      const toggles = [
        { field: "node_online", name: "Node online", desc: "participate in the grid" },
        { field: "node_gpu", name: "Offer GPU", desc: "share this node's GPU with the grid" },
        { field: "cache_venvs", name: "Cache venvs", desc: "reuse virtualenvs across runs" },
        { field: "foreign_storage_accept_offers", name: "Accept deposits", desc: "host others' encrypted files" },
      ];
      let settings;
      try {
        settings = await getNodeSettings();
      } catch (err: any) {
        vscode.window.showErrorMessage(`NexusGrid: ${err?.message || err}`);
        return;
      }
      type SettingPick = vscode.QuickPickItem & { field: string; name: string; current: boolean };
      const items: SettingPick[] = toggles.map((t): SettingPick => {
        const on = !!settings[t.field];
        return {
          label: `${on ? "$(check)" : "$(circle-slash)"} ${t.name}`,
          description: `${on ? "on" : "off"} — ${t.desc}`,
          field: t.field,
          name: t.name,
          current: on,
        };
      });
      const pick = await vscode.window.showQuickPick<SettingPick>(items, {
        placeHolder: "Toggle a node setting (reflects in the web UI)",
        ignoreFocusOut: true,
      });
      if (!pick) {
        return;
      }
      try {
        await updateSettings({ [pick.field]: !pick.current });
        vscode.window.showInformationMessage(`NexusGrid: ${pick.name} → ${!pick.current ? "on" : "off"} — check the web UI.`);
        tree.refresh();
      } catch (err: any) {
        vscode.window.showErrorMessage(`NexusGrid: ${err?.message || err}`);
      }
    }),

    vscode.commands.registerCommand("nexusgrid.setDisplayName", async () => {
      const name = await vscode.window.showInputBox({
        prompt: "Display name for this node (shows in the web UI)",
        ignoreFocusOut: true,
      });
      if (name === undefined) {
        return;
      }
      try {
        await updateSettings({ user_display_name: name });
        vscode.window.showInformationMessage(`NexusGrid: display name set to "${name}" — check the web UI.`);
        tree.refresh();
      } catch (err: any) {
        vscode.window.showErrorMessage(`NexusGrid: ${err?.message || err}`);
      }
    }),

    // Tail a task's logs into an output channel (live until the task ends).
    vscode.commands.registerCommand("nexusgrid.viewLogs", (item?: TaskItem) => {
      if (item?.taskId) {
        tailer.start(item.taskId);
      }
    }),

    // Stop a running/queued task (disrupt if processing, else cancel).
    vscode.commands.registerCommand("nexusgrid.stopTask", async (item?: TaskItem) => {
      if (!item?.taskId) {
        return;
      }
      try {
        await (item.status === "processing" ? disruptTask(item.taskId) : cancelTask(item.taskId));
        vscode.window.showInformationMessage(`NexusGrid: stopped ${item.taskId}`);
        tree.refresh();
      } catch (err: any) {
        vscode.window.showErrorMessage(`NexusGrid: ${err?.message || err}`);
      }
    }),

    vscode.commands.registerCommand("nexusgrid.requeueTask", async (item?: TaskItem) => {
      if (!item?.taskId) {
        return;
      }
      try {
        await requeueTask(item.taskId);
        vscode.window.showInformationMessage(`NexusGrid: requeued ${item.taskId}`);
        tree.refresh();
      } catch (err: any) {
        vscode.window.showErrorMessage(`NexusGrid: ${err?.message || err}`);
      }
    }),

    // List a finished task's artifacts and open the chosen one in the editor.
    vscode.commands.registerCommand("nexusgrid.openResults", async (item?: TaskItem) => {
      if (!item?.taskId) {
        return;
      }
      try {
        const files = await getResultFiles(item.taskId);
        if (files.length === 0) {
          vscode.window.showInformationMessage(`NexusGrid: no artifacts for ${item.taskId}`);
          return;
        }
        const pick = await vscode.window.showQuickPick(
          files.map((f) => ({ label: f.path, description: `${f.bytes} B` })),
          { placeHolder: "Open artifact", ignoreFocusOut: true }
        );
        if (!pick) {
          return;
        }
        const content = await getResultFile(item.taskId, pick.label);
        const doc = await vscode.workspace.openTextDocument({ content });
        await vscode.window.showTextDocument(doc);
      } catch (err: any) {
        vscode.window.showErrorMessage(`NexusGrid: ${err?.message || err}`);
      }
    }),

    // Open a local tunnel to a service and surface its connection string.
    vscode.commands.registerCommand("nexusgrid.startService", async (item?: ServiceItem) => {
      if (!item?.taskId) {
        return;
      }
      try {
        const { connectionString, port } = await startService(item.taskId);
        const msg = connectionString ? `Service started: ${connectionString}` : `Service started on port ${port}`;
        const copy = "Copy";
        const choice = await vscode.window.showInformationMessage(`NexusGrid: ${msg}`, ...(connectionString ? [copy] : []));
        if (choice === copy) {
          await vscode.env.clipboard.writeText(connectionString);
        }
        tree.refresh();
      } catch (err: any) {
        vscode.window.showErrorMessage(`NexusGrid: ${err?.message || err}`);
      }
    }),

    vscode.commands.registerCommand("nexusgrid.stopService", async (item?: ServiceItem) => {
      if (!item?.taskId) {
        return;
      }
      try {
        await stopService(item.taskId);
        vscode.window.showInformationMessage(`NexusGrid: stopped service ${item.taskId}`);
        tree.refresh();
      } catch (err: any) {
        vscode.window.showErrorMessage(`NexusGrid: ${err?.message || err}`);
      }
    }),

    // Explorer right-click: deposit one file (encrypted) onto a peer's storage.
    vscode.commands.registerCommand("nexusgrid.depositFile", async (clicked?: vscode.Uri) => {
      if (!clicked || clicked.scheme !== "file") {
        vscode.window.showWarningMessage("NexusGrid: right-click a single file to deposit.");
        return;
      }
      if (!fs.statSync(clicked.fsPath).isFile()) {
        vscode.window.showWarningMessage("NexusGrid: deposit takes a single file, not a folder.");
        return;
      }
      let peers;
      try {
        peers = await getStoragePeers();
      } catch (err: any) {
        vscode.window.showErrorMessage(`NexusGrid: ${err?.message || err}`);
        return;
      }
      type PeerPick = vscode.QuickPickItem & { value: string };
      const items: PeerPick[] = [
        { label: "$(broadcast) Auto", description: "best fit — fan out to peers with free space", value: "auto" },
        ...peers.map((p): PeerPick => ({ label: `$(server) ${p.label}`, description: `${p.freeGb.toFixed(1)} GB free`, value: p.uuid })),
      ];
      const pick = await vscode.window.showQuickPick<PeerPick>(items, {
        placeHolder: `Deposit ${path.basename(clicked.fsPath)} to…`,
        ignoreFocusOut: true,
      });
      if (!pick) {
        return;
      }
      const password = await vscode.window.showInputBox({
        prompt: "Encryption password (you'll need it to retrieve — the host never sees it)",
        password: true,
        ignoreFocusOut: true,
      });
      if (!password) {
        return;
      }
      await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: `NexusGrid: depositing ${path.basename(clicked.fsPath)}…` },
        async () => {
          try {
            await depositFile({ targetPeer: pick.value, filePath: clicked.fsPath, password });
            vscode.window.showInformationMessage(`NexusGrid: deposit started for ${path.basename(clicked.fsPath)}.`);
            tree.refresh();
          } catch (err: any) {
            vscode.window.showErrorMessage(`NexusGrid: ${err?.message || err}`);
          }
        }
      );
    }),

    // Tree action: pull a deposit's bytes back and decrypt to a chosen folder.
    vscode.commands.registerCommand("nexusgrid.retrieveDeposit", async (item?: DepositItem) => {
      if (!item?.depositId) {
        return;
      }
      const folder = await vscode.window.showOpenDialog({
        canSelectFiles: false,
        canSelectFolders: true,
        canSelectMany: false,
        openLabel: "Retrieve here",
      });
      if (!folder || folder.length === 0) {
        return;
      }
      const password = await vscode.window.showInputBox({
        prompt: "Password used when this file was deposited",
        password: true,
        ignoreFocusOut: true,
      });
      if (!password) {
        return;
      }
      try {
        await retrieveDeposit(item.depositId, { password, saveToPath: folder[0].fsPath });
        vscode.window.showInformationMessage(`NexusGrid: retrieving into ${folder[0].fsPath} — it'll appear when the transfer finishes.`);
        tree.refresh();
      } catch (err: any) {
        vscode.window.showErrorMessage(`NexusGrid: ${err?.message || err}`);
      }
    })
  );
}

/** Tails one task's logs into a shared output channel; stops when it ends. */
class LogTailer {
  private channel = vscode.window.createOutputChannel("NexusGrid Logs");
  private timer?: ReturnType<typeof setInterval>;
  private taskId?: string;

  async start(taskId: string): Promise<void> {
    this.stop();
    this.taskId = taskId;
    this.channel.clear();
    this.channel.show(true);
    this.channel.appendLine(`# tailing ${taskId}`);
    let cursor = 0;
    const poll = async () => {
      if (this.taskId !== taskId) {
        return; // superseded by a newer tail
      }
      try {
        const { lines, cursor: next } = await getLogTail(taskId, cursor);
        for (const ln of lines) {
          this.channel.appendLine(ln);
        }
        cursor = next;
        const status = await getTaskStatus(taskId);
        if (status && TERMINAL_STATUSES.has(status)) {
          this.channel.appendLine(`# task ${status}`);
          this.stop();
        }
      } catch (err: any) {
        this.channel.appendLine(`# tail stopped: ${err?.message || err}`);
        this.stop();
      }
    };
    await poll();
    this.timer = setInterval(poll, 1500);
  }

  private stop(): void {
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
  }

  dispose(): void {
    this.stop();
    this.channel.dispose();
  }
}

/**
 * Load nexus.json (creating + opening it on first use) and run `action`.
 * On first creation we open the file for review instead of dispatching blind.
 */
async function withConfig(resource: vscode.Uri | undefined, action: (config: ReturnType<typeof readOrCreateConfig>["config"]) => Promise<void>) {
  const root = getWorkspaceRoot(resource);
  if (!root) {
    vscode.window.showWarningMessage("NexusGrid: open a folder first.");
    return;
  }
  let result;
  try {
    result = readOrCreateConfig(root);
  } catch (err: any) {
    vscode.window.showErrorMessage(`NexusGrid: nexus.json is invalid JSON — ${err?.message || err}`);
    await openConfig(root);
    return;
  }
  if (result.created) {
    vscode.window.showInformationMessage("NexusGrid: created nexus.json — review it, then run again to dispatch.");
    await openConfig(root);
    return;
  }
  await action(result.config);
}

/** Parse `@nexus:` directives from a file on disk (best-effort). */
function directivesFromFile(file?: vscode.Uri): ParsedDirectives | undefined {
  try {
    if (file) {
      return parseDirectives(fs.readFileSync(file.fsPath, "utf8"));
    }
  } catch {
    // unreadable file → no directives
  }
  return undefined;
}

/** Tell the user about directives we recognized syntactically but can't apply. */
function warnUnknown(parsed?: ParsedDirectives) {
  if (parsed && parsed.unknown.length) {
    vscode.window.showWarningMessage(
      `NexusGrid: ignored unrecognized @nexus directive(s): ${parsed.unknown.join(", ")}`
    );
  }
}

async function openConfig(root: string) {
  await openFile(configPath(root));
}

async function openFile(p: string) {
  const doc = await vscode.workspace.openTextDocument(vscode.Uri.file(p));
  await vscode.window.showTextDocument(doc);
}

/** Run a dispatch action with progress + a success/error toast, then refresh. */
async function dispatch(action: () => Promise<string>, tree: NodeTreeProvider, what?: string) {
  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `NexusGrid: dispatching${what ? ` ${what}` : ""}…` },
    async () => {
      try {
        const wfId = await action();
        vscode.window.showInformationMessage(`NexusGrid: dispatched ${wfId}`);
        tree.refresh();
      } catch (err: any) {
        vscode.window.showErrorMessage(`NexusGrid: ${err?.message || err}`);
      }
    }
  );
}

export function deactivate() {}
