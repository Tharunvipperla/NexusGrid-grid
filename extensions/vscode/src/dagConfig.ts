import * as fs from "fs";
import * as path from "path";
import { DispatchOpts } from "./client";

// `nexus.dag.json` at the workspace root defines a multi-step pipeline (DAG).
// It carries the same step array the node's dispatcher consumes, optionally
// wrapped in an object that also holds workflow-level targeting. Plain JSON so
// it's editable by hand or from a terminal.

export interface DagStep {
  id: string;
  image?: string;
  entrypoint?: string;
  runtime?: string;
  depends_on?: string[];
  [key: string]: unknown;
}

export interface DagConfig {
  steps: DagStep[];
  opts: DispatchOpts;
}

// A 2-step example written on first use so the shape is obvious.
const DEFAULT_DAG = {
  preferred_workers: [],
  target_groups: [],
  require_gpu: false,
  steps: [
    { id: "prep", image: "python:3.11-slim", runtime: "docker", entrypoint: "python prep.py", depends_on: [] },
    { id: "train", image: "python:3.11-slim", runtime: "docker", entrypoint: "python train.py", depends_on: ["prep"] },
  ],
};

export function dagConfigPath(root: string): string {
  return path.join(root, "nexus.dag.json");
}

/**
 * Read `nexus.dag.json`, creating it with the example if missing. Throws on
 * invalid JSON or a malformed pipeline (the caller surfaces it and opens the
 * file). `created` is true the first time so the caller can open it for review
 * instead of dispatching blind.
 */
export function readOrCreateDag(root: string): { created: boolean; config?: DagConfig } {
  const p = dagConfigPath(root);
  if (!fs.existsSync(p)) {
    fs.writeFileSync(p, JSON.stringify(DEFAULT_DAG, null, 2) + "\n");
    return { created: true };
  }
  return { created: false, config: parseDag(JSON.parse(fs.readFileSync(p, "utf8"))) };
}

/** Persist pipeline targeting into an existing nexus.dag.json. */
export function setDagTarget(root: string, workers: string[], groups: string[]): void {
  const p = dagConfigPath(root);
  const raw = JSON.parse(fs.readFileSync(p, "utf8"));
  const obj = Array.isArray(raw) ? { steps: raw } : raw;
  obj.preferred_workers = workers;
  obj.target_groups = groups;
  fs.writeFileSync(p, JSON.stringify(obj, null, 2) + "\n");
}

/** Accept either a bare step array or a `{ steps, ...targeting }` wrapper. */
export function parseDag(raw: any): DagConfig {
  const steps: DagStep[] = Array.isArray(raw) ? raw : raw?.steps;
  if (!Array.isArray(steps) || steps.length === 0) {
    throw new Error('nexus.dag.json needs a non-empty "steps" array');
  }
  for (const s of steps) {
    if (!s || typeof s !== "object" || !s.id) {
      throw new Error('each pipeline step needs an "id"');
    }
  }
  const wrap = Array.isArray(raw) ? {} : raw || {};
  const opts: DispatchOpts = {};
  if (Array.isArray(wrap.preferred_workers) && wrap.preferred_workers.length) {
    opts.preferredWorkers = wrap.preferred_workers;
  }
  if (Array.isArray(wrap.target_groups) && wrap.target_groups.length) {
    opts.targetGroups = wrap.target_groups;
  }
  if (wrap.require_gpu) {
    opts.requireGpu = true;
  }
  if (typeof wrap.priority === "number") {
    opts.priority = wrap.priority;
  }
  return { steps, opts };
}
