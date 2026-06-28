import * as vscode from "vscode";
import * as path from "path";
import { parseDirectives } from "./directives";
import { getWorkspaceRoot, readConfigOrDefault, resolveDispatch } from "./dispatchConfig";
import { getWorkers, DispatchOpts } from "./client";

// CodeLens + hover for `@nexus:` directive lines:
//   - CodeLens: a clickable "Dispatch to <target>" above the directive.
//   - Hover: a "Nexus Lens" with the resolved requirements and which connected
//     workers fit. No time estimate (we have no honest data for one).

function directiveLineNumbers(document: vscode.TextDocument): number[] {
  const lines: number[] = [];
  for (let i = 0; i < document.lineCount; i++) {
    if (/@nexus:/i.test(document.lineAt(i).text)) {
      lines.push(i);
    }
  }
  return lines;
}

function targetLabel(opts: DispatchOpts): string {
  if (opts.preferredWorkers?.length) {
    return opts.preferredWorkers[0];
  }
  if (opts.targetGroups?.length) {
    return `group ${opts.targetGroups[0]}`;
  }
  return "best fit";
}

function codeLensEnabled(): boolean {
  return vscode.workspace.getConfiguration("nexusgrid").get<boolean>("codeLens", true);
}

class NexusCodeLensProvider implements vscode.CodeLensProvider {
  private emitter = new vscode.EventEmitter<void>();
  readonly onDidChangeCodeLenses = this.emitter.event;

  refresh(): void {
    this.emitter.fire();
  }

  provideCodeLenses(document: vscode.TextDocument): vscode.CodeLens[] {
    if (!codeLensEnabled() || document.uri.scheme !== "file") {
      return [];
    }
    const fname = path.basename(document.uri.fsPath);
    if (fname === "nexus.dag.json") {
      return dagLens(document);
    }
    if (fname === "nexus.service.json") {
      return serviceLens(document);
    }
    const lines = directiveLineNumbers(document);
    if (lines.length === 0) {
      return [];
    }
    const config = readConfigOrDefault(getWorkspaceRoot(document.uri));
    const parsed = parseDirectives(document.getText());
    const r = resolveDispatch(config, parsed, document.uri);
    const title = `$(rocket) Dispatch to ${targetLabel(r.opts)}`;
    return lines.map(
      (line) =>
        new vscode.CodeLens(new vscode.Range(line, 0, line, 0), {
          title,
          command: "nexusgrid.runOnGrid",
          arguments: [document.uri],
        })
    );
  }
}

/** A single "Run pipeline (N steps)" lens at the top of nexus.dag.json. */
function dagLens(document: vscode.TextDocument): vscode.CodeLens[] {
  let count = 0;
  try {
    const raw = JSON.parse(document.getText());
    const steps = Array.isArray(raw) ? raw : raw?.steps;
    count = Array.isArray(steps) ? steps.length : 0;
  } catch {
    return []; // mid-edit / invalid JSON: no lens
  }
  if (count === 0) {
    return [];
  }
  return [
    new vscode.CodeLens(new vscode.Range(0, 0, 0, 0), {
      title: `$(rocket) Run pipeline (${count} step${count === 1 ? "" : "s"})`,
      command: "nexusgrid.runDag",
      arguments: [document.uri],
    }),
  ];
}

/** A single "Deploy service" lens at the top of nexus.service.json. */
function serviceLens(document: vscode.TextDocument): vscode.CodeLens[] {
  let title = "$(rocket) Deploy service";
  try {
    const raw = JSON.parse(document.getText());
    const image = String(raw?.image || "").trim();
    if (!image) {
      return []; // incomplete config
    }
    const ports = Array.isArray(raw.expose_ports) ? raw.expose_ports.join(", ") : "";
    title = `$(rocket) Deploy ${image}${ports ? ` (ports ${ports})` : ""}`;
  } catch {
    return []; // mid-edit / invalid JSON
  }
  return [
    new vscode.CodeLens(new vscode.Range(0, 0, 0, 0), {
      title,
      command: "nexusgrid.deployService",
      arguments: [document.uri],
    }),
  ];
}

class NexusHoverProvider implements vscode.HoverProvider {
  async provideHover(document: vscode.TextDocument, position: vscode.Position): Promise<vscode.Hover | undefined> {
    if (document.uri.scheme !== "file" || !/@nexus:/i.test(document.lineAt(position.line).text)) {
      return undefined;
    }
    const config = readConfigOrDefault(getWorkspaceRoot(document.uri));
    const parsed = parseDirectives(document.getText());
    const r = resolveDispatch(config, parsed, document.uri);

    const md = new vscode.MarkdownString(undefined, true);
    md.appendMarkdown("**Nexus Lens**\n\n");
    md.appendMarkdown(`requirements\n`);
    md.appendMarkdown(`- image &nbsp; \`${r.image}\`\n`);
    md.appendMarkdown(`- runtime &nbsp; \`${r.runtime}\`\n`);
    md.appendMarkdown(`- command &nbsp; \`${r.command}\`\n`);
    md.appendMarkdown(`- target &nbsp; ${targetLabel(r.opts)}\n`);
    if (r.opts.requireGpu) {
      md.appendMarkdown(`- gpu &nbsp; required (\`${r.opts.gpu}\`)\n`);
    }
    if (r.opts.ramLimitMb) {
      md.appendMarkdown(`- ram limit &nbsp; ${r.opts.ramLimitMb / 1024} GB\n`);
    }
    if (r.opts.cpuLimitPct) {
      md.appendMarkdown(`- cpu limit &nbsp; ${r.opts.cpuLimitPct}%\n`);
    }

    try {
      const workers = await getWorkers();
      if (workers.length === 0) {
        md.appendMarkdown(`\n_No connected workers — runs per this node's policy._\n`);
      } else {
        const eligible = r.opts.requireGpu ? workers.filter((w) => w.gpu) : workers;
        const names = eligible.slice(0, 5).map((w) => w.label).join(", ") || "none";
        md.appendMarkdown(`\n**fits ${eligible.length}/${workers.length} workers** — ${names}\n`);
      }
    } catch {
      md.appendMarkdown(`\n_Node unreachable — can't compute fits._\n`);
    }

    if (parsed.unknown.length) {
      md.appendMarkdown(`\n⚠️ ignored: ${parsed.unknown.join(", ")}\n`);
    }
    return new vscode.Hover(md, document.lineAt(position.line).range);
  }
}

export function registerLens(context: vscode.ExtensionContext): void {
  const selector: vscode.DocumentSelector = { scheme: "file" };
  const codeLens = new NexusCodeLensProvider();
  context.subscriptions.push(
    vscode.languages.registerCodeLensProvider(selector, codeLens),
    vscode.languages.registerHoverProvider(selector, new NexusHoverProvider()),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("nexusgrid.codeLens")) {
        codeLens.refresh();
      }
    })
  );
}
