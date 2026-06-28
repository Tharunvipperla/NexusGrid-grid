import * as vscode from "vscode";
import * as fs from "fs";
import * as path from "path";
import { DispatchOpts } from "./client";
import { ParsedDirectives } from "./directives";

// `nexus.json` at the workspace root holds the dispatch defaults. It's plain JSON
// so it's editable by hand or from a terminal. Per-file `@nexus:` comment
// directives (added in a later step) override these.

export interface NexusConfig {
  image: string;
  runtime: string;
  command: string;
  /** "auto" | "<worker-ip>" | "group:<id>" */
  target: string;
  /** false | true | "all" | a GPU count */
  gpu: boolean | number | string;
}

export const DEFAULT_CONFIG: NexusConfig = {
  image: "python:3.11-slim",
  runtime: "docker",
  command: "python main.py",
  target: "auto",
  gpu: false,
};

export function configPath(root: string): string {
  return path.join(root, "nexus.json");
}

/** Workspace root for a resource, else the first workspace folder. */
export function getWorkspaceRoot(resource?: vscode.Uri): string | undefined {
  if (resource) {
    const wf = vscode.workspace.getWorkspaceFolder(resource);
    if (wf) {
      return wf.uri.fsPath;
    }
  }
  return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
}

/**
 * Read `nexus.json`, creating it with defaults if missing. Throws on invalid
 * JSON (the caller surfaces it and opens the file). `created` is true the first
 * time so the caller can open it for review instead of dispatching blind.
 */
export function readOrCreateConfig(root: string): { created: boolean; config: NexusConfig } {
  const p = configPath(root);
  if (!fs.existsSync(p)) {
    fs.writeFileSync(p, JSON.stringify(DEFAULT_CONFIG, null, 2) + "\n");
    return { created: true, config: DEFAULT_CONFIG };
  }
  const parsed = JSON.parse(fs.readFileSync(p, "utf8"));
  return { created: false, config: { ...DEFAULT_CONFIG, ...parsed } };
}

/** Read nexus.json if present, else defaults. Never creates the file (for
 *  read-only consumers like CodeLens/hover). */
export function readConfigOrDefault(root: string | undefined): NexusConfig {
  if (!root) {
    return DEFAULT_CONFIG;
  }
  try {
    const p = configPath(root);
    if (!fs.existsSync(p)) {
      return DEFAULT_CONFIG;
    }
    return { ...DEFAULT_CONFIG, ...JSON.parse(fs.readFileSync(p, "utf8")) };
  } catch {
    return DEFAULT_CONFIG;
  }
}

/** Persist the dispatch target into nexus.json (creating it if missing). */
export function setConfigTarget(root: string, target: string): void {
  const config = readConfigOrDefault(root);
  fs.writeFileSync(configPath(root), JSON.stringify({ ...config, target }, null, 2) + "\n");
}

/** The command to run: derived from a single clicked file, else the config. */
export function commandFor(config: NexusConfig, file?: vscode.Uri): string {
  if (file) {
    const base = path.basename(file.fsPath);
    const ext = path.extname(base).toLowerCase();
    if (ext === ".py") {
      return `python ${base}`;
    }
    if (ext === ".js") {
      return `node ${base}`;
    }
    if (ext === ".sh") {
      return `sh ${base}`;
    }
  }
  return config.command || DEFAULT_CONFIG.command;
}

export interface ResolvedDispatch {
  image: string;
  runtime: string;
  command: string;
  opts: DispatchOpts;
}

/** Merge nexus.json with this file's `@nexus:` directives into a dispatch. */
export function resolveDispatch(config: NexusConfig, parsed?: ParsedDirectives, file?: vscode.Uri): ResolvedDispatch {
  const merged: NexusConfig = {
    ...config,
    target: parsed?.target ?? config.target,
    gpu: parsed?.gpu ?? config.gpu,
  };
  const opts = configToOpts(merged);
  if (parsed?.ramLimitMb) {
    opts.ramLimitMb = parsed.ramLimitMb;
  }
  if (parsed?.cpuLimitPct) {
    opts.cpuLimitPct = parsed.cpuLimitPct;
  }
  if (parsed?.priority !== undefined) {
    opts.priority = parsed.priority;
  }
  if (parsed?.isolation) {
    opts.isolation = true;
  }
  if (parsed?.noCache) {
    opts.noCache = true;
  }
  if (parsed?.scan) {
    opts.scan = true;
  }
  return {
    image: parsed?.image ?? config.image,
    runtime: parsed?.runtime ?? config.runtime,
    command: commandFor(config, file),
    opts,
  };
}

/** Translate the config's target + gpu into scheduler dispatch options. */
export function configToOpts(config: NexusConfig): DispatchOpts {
  const opts: DispatchOpts = {};
  const t = String(config.target || "auto").trim();
  if (t && t !== "auto") {
    if (t.startsWith("group:")) {
      opts.targetGroups = [t.slice("group:".length)];
    } else {
      opts.preferredWorkers = [t];
    }
  }
  const g = config.gpu;
  if (g === true || g === "all") {
    opts.requireGpu = true;
    opts.gpu = "all";
  } else if (typeof g === "number" && g >= 1) {
    opts.requireGpu = true;
    opts.gpu = String(g);
  } else if (typeof g === "string" && /^\d+$/.test(g) && Number(g) >= 1) {
    opts.requireGpu = true;
    opts.gpu = g;
  }
  return opts;
}
