import * as https from "https";
import * as http from "http";
import * as fs from "fs";
import * as path from "path";
import { URL } from "url";
import * as vscode from "vscode";

// Minimal client for a local NexusGrid node's management API (`/local/*`).
// The node serves self-signed TLS on localhost, so cert verification is off
// for the loopback node — see resolveBaseUrl()'s default.

export interface TaskInfo {
  id: string;
  displayId: string;
  status: string;
  worker: string;
  coordination: string; // "serving" marks a service
}

function config() {
  return vscode.workspace.getConfiguration("nexusgrid");
}

export function resolveBaseUrl(): string {
  return (config().get<string>("baseUrl") || "https://127.0.0.1:8000").replace(/\/+$/, "");
}

/** Read `.nexus_local_token` from a dir, walking up to the filesystem root. */
function tokenFromDirUpwards(start: string): string {
  let dir = start;
  while (dir) {
    try {
      return fs.readFileSync(path.join(dir, ".nexus_local_token"), "utf8").trim();
    } catch {
      const parent = path.dirname(dir);
      if (parent === dir) {
        break; // reached root
      }
      dir = parent;
    }
  }
  return "";
}

/**
 * Token resolution, easiest-first so it usually needs no setup:
 *   1. the `nexusgrid.token` setting, if set;
 *   2. the `nexusgrid.nodeDir` setting (and its parents), if set;
 *   3. each workspace folder (and its parents);
 *   4. the node's working directory (and its parents).
 */
export function resolveToken(): string {
  const explicit = (config().get<string>("token") || "").trim();
  if (explicit) {
    return explicit;
  }
  const nodeDir = (config().get<string>("nodeDir") || "").trim();
  if (nodeDir) {
    const t = tokenFromDirUpwards(nodeDir);
    if (t) {
      return t;
    }
  }
  for (const f of vscode.workspace.workspaceFolders || []) {
    const t = tokenFromDirUpwards(f.uri.fsPath);
    if (t) {
      return t;
    }
  }
  return tokenFromDirUpwards(process.cwd());
}

function request(method: string, urlStr: string, body?: Buffer, contentType?: string): Promise<{ status: number; text: string }> {
  const url = new URL(urlStr);
  const isHttps = url.protocol === "https:";
  const lib = isHttps ? https : http;
  const headers: Record<string, string> = { "X-Local-Token": resolveToken() };
  if (body) {
    headers["Content-Type"] = contentType || "application/json";
    headers["Content-Length"] = String(body.length);
  }
  const opts: https.RequestOptions = {
    method,
    hostname: url.hostname,
    port: url.port,
    path: url.pathname + url.search,
    headers,
    // The loopback node uses a self-signed cert; trust it for localhost only.
    rejectUnauthorized: false,
  };
  return new Promise((resolve, reject) => {
    const req = lib.request(opts, (res) => {
      const chunks: Buffer[] = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve({ status: res.statusCode || 0, text: Buffer.concat(chunks).toString("utf8") }));
    });
    req.on("error", reject);
    if (body) {
      req.write(body);
    }
    req.end();
  });
}

async function getJson(pathPart: string): Promise<any> {
  const res = await request("GET", resolveBaseUrl() + pathPart);
  if (res.status !== 200) {
    throw new Error(`${pathPart} → HTTP ${res.status}: ${res.text.slice(0, 200)}`);
  }
  return JSON.parse(res.text);
}

/** Pull the node's network snapshot and flatten its tasks map. */
export async function getTasks(): Promise<TaskInfo[]> {
  const data = await getJson("/local/network");
  const tasks = data.tasks || {};
  return Object.keys(tasks).map((id) => ({
    id,
    displayId: tasks[id].display_id || id,
    status: tasks[id].status || "?",
    worker: tasks[id].worker || "",
    coordination: tasks[id].coordination || "",
  }));
}

// 22-byte empty ZIP (EOCD only) for no-file tasks.
const EMPTY_ZIP = Buffer.concat([Buffer.from([0x50, 0x4b, 0x05, 0x06]), Buffer.alloc(18)]);

/** Where + how to run: scheduler target and GPU. All optional (auto otherwise). */
export interface DispatchOpts {
  preferredWorkers?: string[];
  targetGroups?: string[];
  requireGpu?: boolean;
  gpu?: string; // per-step passthrough request, e.g. "all"
  ramLimitMb?: number; // container memory cap (enforced)
  cpuLimitPct?: number; // container CPU cap
  priority?: number; // dispatch priority 0-100
  isolation?: boolean; // require_venv_isolation
  noCache?: boolean; // no_venv_cache
  scan?: boolean; // enable_task_scanning
}

/** Post a DAG (array of step objects) with a workspace zip; returns workflow id. */
async function postSteps(steps: unknown[], zip: Buffer, opts: DispatchOpts = {}): Promise<string> {
  const wfId = "vscode-" + Date.now();
  const dag = JSON.stringify(steps);
  const boundary = "----nexusgrid" + Date.now();
  const field = (name: string, value: string) =>
    Buffer.from(`--${boundary}\r\nContent-Disposition: form-data; name="${name}"\r\n\r\n${value}\r\n`);
  const parts: Buffer[] = [
    field("workflow_id", wfId),
    field("workflow_json", dag),
    field("preferred_workers", JSON.stringify(opts.preferredWorkers || [])),
    field("target_groups", JSON.stringify(opts.targetGroups || [])),
    field("require_gpu", opts.requireGpu ? "true" : "false"),
  ];
  if (opts.priority !== undefined) {
    parts.push(field("priority", String(opts.priority)));
  }
  parts.push(
    Buffer.from(
      `--${boundary}\r\nContent-Disposition: form-data; name="file"; filename="ws.zip"\r\nContent-Type: application/zip\r\n\r\n`
    ),
    zip,
    Buffer.from(`\r\n--${boundary}--\r\n`)
  );
  const payload = Buffer.concat(parts);
  const res = await request("POST", resolveBaseUrl() + "/local/add_workflow", payload, `multipart/form-data; boundary=${boundary}`);
  if (res.status !== 200) {
    throw new Error(`dispatch failed → HTTP ${res.status}: ${res.text.slice(0, 300)}`);
  }
  return wfId;
}

/** Post a one-step DAG (image + entrypoint on a runtime), building the step from opts. */
function postWorkflow(
  image: string,
  entrypoint: string,
  runtime: string,
  zip: Buffer,
  opts: DispatchOpts = {}
): Promise<string> {
  const step: Record<string, unknown> = { id: "s", runtime, image, entrypoint, depends_on: [] };
  if (opts.gpu) {
    step.gpu = opts.gpu;
  }
  if (opts.ramLimitMb) {
    step.ram_limit = opts.ramLimitMb;
  }
  if (opts.cpuLimitPct) {
    step.cpu_limit = opts.cpuLimitPct;
  }
  if (opts.isolation) {
    step.require_venv_isolation = true;
  }
  if (opts.noCache) {
    step.no_venv_cache = true;
  }
  if (opts.scan) {
    step.enable_task_scanning = true;
  }
  return postSteps([step], zip, opts);
}

/** Dispatch a multi-step pipeline from nexus.dag.json, zipping the workspace. */
export async function runDag(roots: string[], steps: unknown[], opts?: DispatchOpts): Promise<string> {
  const zip = roots.length ? await zipPaths(roots) : EMPTY_ZIP;
  return postSteps(steps, zip, opts);
}

/** Dispatch with no workspace files (image already has everything it needs). */
export function dispatchTask(image: string, entrypoint: string, runtime: string, opts?: DispatchOpts): Promise<string> {
  return postWorkflow(image, entrypoint, runtime, EMPTY_ZIP, opts);
}

/** Dispatch the given files/folders as the task's workspace. */
export async function runOnGrid(
  roots: string[],
  image: string,
  entrypoint: string,
  runtime: string,
  opts?: DispatchOpts
): Promise<string> {
  const zip = await zipPaths(roots);
  return postWorkflow(image, entrypoint, runtime, zip, opts);
}

export interface WorkerInfo {
  id: string;
  label: string;
  gpu: boolean;
}

export interface GroupInfo {
  id: string;
  name: string;
}

/** Trusted compute workers (for targeting), from the network snapshot. */
export async function getWorkers(): Promise<WorkerInfo[]> {
  const data = await getJson("/local/network");
  const w = data.workers || {};
  return Object.keys(w).map((ip) => ({
    id: ip,
    label: w[ip].user_display_name || w[ip].node_identity || ip,
    gpu: !!w[ip].gpu,
  }));
}

/** Groups this node belongs to (for targeting). */
export async function getGroups(): Promise<GroupInfo[]> {
  const data = await getJson("/local/groups");
  return (data.groups || []).map((g: any) => ({ id: g.id, name: g.name }));
}

export const TERMINAL_STATUSES = new Set(["completed", "failed", "disrupted", "cancelled", "lease_expired"]);

function httpErr(r: { status: number; text: string }): string {
  try {
    const j = JSON.parse(r.text);
    if (j.detail) {
      return `HTTP ${r.status}: ${j.detail}`;
    }
  } catch {
    // not JSON
  }
  return `HTTP ${r.status}: ${r.text.slice(0, 200)}`;
}

async function post(pathPart: string): Promise<void> {
  const r = await request("POST", resolveBaseUrl() + pathPart);
  if (r.status !== 200) {
    throw new Error(httpErr(r));
  }
}

async function postJson(pathPart: string): Promise<any> {
  const r = await request("POST", resolveBaseUrl() + pathPart);
  if (r.status !== 200) {
    throw new Error(httpErr(r));
  }
  return JSON.parse(r.text);
}

/** Open a local tunnel to a service; returns its connection string + port. */
export async function startService(taskId: string): Promise<{ connectionString: string; port: number }> {
  const d = await postJson(`/local/services/${encodeURIComponent(taskId)}/start`);
  return { connectionString: d.connection_string || "", port: d.port || 0 };
}

export const stopService = (taskId: string) => post(`/local/services/${encodeURIComponent(taskId)}/stop`);

/** Incremental log tail; pass the previous cursor to get only new lines. */
export async function getLogTail(taskId: string, since: number): Promise<{ lines: string[]; cursor: number }> {
  const data = await getJson(`/local/task_log_tail/${encodeURIComponent(taskId)}?since=${since}`);
  return { lines: data.lines || [], cursor: data.cursor || 0 };
}

/** Current status of one task (from the network snapshot), or undefined. */
export async function getTaskStatus(taskId: string): Promise<string | undefined> {
  const data = await getJson("/local/network");
  return (data.tasks || {})[taskId]?.status;
}

export const cancelTask = (taskId: string) => post(`/local/cancel_task/${encodeURIComponent(taskId)}`);
export const disruptTask = (taskId: string) => post(`/local/disrupt_task/${encodeURIComponent(taskId)}`);
export const requeueTask = (taskId: string) => post(`/local/requeue_task/${encodeURIComponent(taskId)}`);

/** Files inside a completed task's result bundle ([] if there's no bundle yet). */
export async function getResultFiles(taskId: string): Promise<{ path: string; bytes: number }[]> {
  const r = await request("GET", resolveBaseUrl() + `/local/results/${encodeURIComponent(taskId)}/files`);
  if (r.status === 404) {
    return [];
  }
  if (r.status !== 200) {
    throw new Error(httpErr(r));
  }
  return JSON.parse(r.text).files || [];
}

/** Fetch one artifact's contents as text. */
export async function getResultFile(taskId: string, filePath: string): Promise<string> {
  const r = await request(
    "GET",
    resolveBaseUrl() + `/local/results/${encodeURIComponent(taskId)}/file?path=${encodeURIComponent(filePath)}`
  );
  if (r.status !== 200) {
    throw new Error(httpErr(r));
  }
  return r.text;
}

/** Recursively list files under a path (or just the file itself). */
function listFiles(p: string): string[] {
  const st = fs.statSync(p);
  if (st.isFile()) {
    return [p];
  }
  if (st.isDirectory()) {
    return fs.readdirSync(p).flatMap((name) => listFiles(path.join(p, name)));
  }
  return [];
}

/**
 * Zip the selected roots into a workspace bundle, lazily requiring jszip.
 * Base dir: a single folder zips its contents at the root; otherwise paths are
 * relative to the common parent of the selection (mirrors a UI build context).
 */
async function zipPaths(roots: string[]): Promise<Buffer> {
  const JSZip = require("jszip");
  const zip = new JSZip();
  const base =
    roots.length === 1 && fs.statSync(roots[0]).isDirectory()
      ? roots[0]
      : commonDir(roots);
  for (const root of roots) {
    for (const file of listFiles(root)) {
      const rel = path.relative(base, file).split(path.sep).join("/");
      zip.file(rel, fs.readFileSync(file));
    }
  }
  return zip.generateAsync({ type: "nodebuffer" });
}

function commonDir(paths: string[]): string {
  const dirs = paths.map((p) => path.dirname(p));
  let base = dirs[0];
  for (const d of dirs.slice(1)) {
    while (!d.startsWith(base)) {
      base = path.dirname(base);
    }
  }
  return base;
}

async function postJsonBody(pathPart: string, obj: unknown): Promise<any> {
  const body = Buffer.from(JSON.stringify(obj));
  const r = await request("POST", resolveBaseUrl() + pathPart, body, "application/json");
  if (r.status !== 200) {
    throw new Error(httpErr(r));
  }
  return JSON.parse(r.text);
}

// --- Foreign storage (deposit + retrieve only; lifecycle lives in the web UI) ---

export interface DepositInfo {
  depositId: string;
  status: string;
  filename: string;
  host: string;
  bytes: number;
}

export interface StoragePeer {
  uuid: string;
  label: string;
  freeGb: number;
}

/** Deposits this node owns on other nodes. */
export async function getDeposits(): Promise<DepositInfo[]> {
  const data = await getJson("/local/foreign_storage/my_deposits");
  return (data.deposits || []).map((d: any) => ({
    depositId: d.deposit_id,
    status: d.status || "?",
    filename: d.filename || "",
    host: d.host_display_name || d.host_uuid || "",
    bytes: d.total_bytes || 0,
  }));
}

/** Trusted peers that are online and accepting deposits, most free space first. */
export async function getStoragePeers(): Promise<StoragePeer[]> {
  const data = await getJson("/local/foreign_storage/peer_capacities");
  return (data.peers || [])
    .filter((p: any) => p.available && p.accepting)
    .map((p: any) => ({ uuid: p.peer_uuid, label: p.display_name || p.peer_uuid, freeGb: p.free_gb || 0 }));
}

/** Start an encrypted deposit of one file toward a peer (or "auto"). */
export function depositFile(opts: { targetPeer: string; filePath: string; password: string; ttlDays?: number }): Promise<any> {
  return postJsonBody("/local/foreign_storage/deposit", {
    target_peer: opts.targetPeer,
    file_path: opts.filePath,
    password: opts.password,
    ttl_days: opts.ttlDays ?? 30,
    queue_if_offline: opts.targetPeer !== "auto",
  });
}

/** Pull a deposit's bytes back and decrypt to a local path. */
export function retrieveDeposit(depositId: string, opts: { password: string; saveToPath: string }): Promise<any> {
  return postJsonBody(`/local/foreign_storage/retrieve/${encodeURIComponent(depositId)}`, {
    password: opts.password,
    save_to_path: opts.saveToPath,
  });
}

/** The node's current settings block (from the network snapshot). */
export async function getNodeSettings(): Promise<Record<string, any>> {
  const data = await getJson("/local/network");
  return data.settings || {};
}

/** Change a node setting via the same endpoint the web UI uses (broadcasts live). */
export async function updateSettings(partial: Record<string, unknown>): Promise<void> {
  const body = Buffer.from(JSON.stringify(partial));
  const res = await request("POST", resolveBaseUrl() + "/local/settings_partial", body, "application/json");
  if (res.status !== 200) {
    throw new Error(`settings update failed → HTTP ${res.status}: ${res.text.slice(0, 200)}`);
  }
}

/**
 * Subscribe to the node's SSE stream so the extension refreshes when anything
 * changes on the grid (including changes made from the web UI). Returns a
 * dispose function. Reconnects on drop.
 */
export function subscribeEvents(onEvent: () => void): () => void {
  let closed = false;
  let req: http.ClientRequest | undefined;
  const connect = () => {
    if (closed) {
      return;
    }
    const base = resolveBaseUrl();
    const url = new URL(base + "/local/events/stream?local_token=" + encodeURIComponent(resolveToken()));
    const lib = url.protocol === "https:" ? https : http;
    req = lib.request(
      {
        method: "GET",
        hostname: url.hostname,
        port: url.port,
        path: url.pathname + url.search,
        headers: { Accept: "text/event-stream" },
        rejectUnauthorized: false,
      },
      (res) => {
        res.on("data", (chunk: Buffer) => {
          if (chunk.includes("data:")) {
            onEvent();
          }
        });
        res.on("end", () => !closed && setTimeout(connect, 2000));
      }
    );
    req.on("error", () => !closed && setTimeout(connect, 2000));
    req.end();
  };
  connect();
  return () => {
    closed = true;
    req?.destroy();
  };
}
