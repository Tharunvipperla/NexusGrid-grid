import * as fs from "fs";
import * as path from "path";
import { DispatchOpts } from "./client";

// `nexus.service.json` at the workspace root defines one long-running service
// (e.g. redis/postgres, or a custom image). It deploys through the same
// dispatch path as a one-step workflow whose step has `runtime: "service"`.

export interface ServiceDeploy {
  steps: unknown[];
  opts: DispatchOpts;
  ports: number[];
}

const DEFAULT_SERVICE = {
  image: "redis:7",
  entrypoint: "",
  expose_ports: [6379],
  service_kind: "tcp",
  environment: {},
  target: "auto",
};

export function serviceConfigPath(root: string): string {
  return path.join(root, "nexus.service.json");
}

/** Read `nexus.service.json`, creating the example if missing. Throws on a
 *  malformed config (the caller opens the file for review). */
export function readOrCreateService(root: string): { created: boolean; deploy?: ServiceDeploy } {
  const p = serviceConfigPath(root);
  if (!fs.existsSync(p)) {
    fs.writeFileSync(p, JSON.stringify(DEFAULT_SERVICE, null, 2) + "\n");
    return { created: true };
  }
  return { created: false, deploy: buildService(JSON.parse(fs.readFileSync(p, "utf8"))) };
}

/** Turn the service config into the step + workflow opts the dispatcher takes. */
export function buildService(raw: any): ServiceDeploy {
  const image = String(raw?.image || "").trim();
  if (!image) {
    throw new Error('nexus.service.json needs an "image"');
  }
  const ports = (Array.isArray(raw.expose_ports) ? raw.expose_ports : []).map(Number).filter((n: number) => n > 0);
  if (ports.length === 0) {
    throw new Error('nexus.service.json needs "expose_ports" (at least one port)');
  }
  const step: Record<string, unknown> = {
    id: "svc",
    runtime: "service",
    image,
    entrypoint: String(raw.entrypoint || ""),
    expose_ports: ports,
    service_kind: String(raw.service_kind || "tcp"),
    depends_on: [],
  };
  if (raw.environment && typeof raw.environment === "object") {
    step.environment = raw.environment;
  }
  const opts: DispatchOpts = {};
  const t = String(raw.target || "auto").trim();
  if (t && t !== "auto") {
    if (t.startsWith("group:")) {
      opts.targetGroups = [t.slice("group:".length)];
    } else {
      opts.preferredWorkers = [t];
    }
  }
  return { steps: [step], opts, ports };
}
