// Per-file `@nexus:` comment directives, e.g.
//   # @nexus: gpu, ram>=16, cpu=50, runtime=docker, isolation
// These override nexus.json for the file being dispatched. Only directives that
// map to a real backend field are honored; anything else is reported as unknown
// (we never silently pretend to apply something the node doesn't support).

export interface ParsedDirectives {
  found: boolean;
  image?: string;
  runtime?: string;
  target?: string;
  gpu?: boolean | number;
  ramLimitMb?: number;
  cpuLimitPct?: number;
  priority?: number;
  isolation?: boolean;
  noCache?: boolean;
  scan?: boolean;
  unknown: string[];
}

const VALID_RUNTIMES = ["docker", "native", "wasm"];

function num(v: string, out: ParsedDirectives, tok: string): number | undefined {
  const n = parseFloat(v.replace(/[^0-9.]/g, ""));
  if (isNaN(n)) {
    out.unknown.push(tok);
    return undefined;
  }
  return n;
}

/** Parse every `@nexus:` directive line in a file's text. */
export function parseDirectives(text: string): ParsedDirectives {
  const out: ParsedDirectives = { found: false, unknown: [] };
  const re = /@nexus:([^\n\r]*)/gi;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    out.found = true;
    for (const rawTok of m[1].split(",")) {
      const tok = rawTok.trim();
      if (!tok) {
        continue;
      }
      const parts = tok.match(/^([a-zA-Z_-]+)\s*(?:(?:>=|<=|=|:)\s*(.+))?$/);
      if (!parts) {
        out.unknown.push(tok);
        continue;
      }
      const key = parts[1].toLowerCase();
      const val = (parts[2] || "").trim();
      switch (key) {
        case "gpu":
          out.gpu = val ? num(val, out, tok) : true;
          break;
        case "ram": {
          const g = num(val, out, tok);
          if (g !== undefined) {
            out.ramLimitMb = Math.round(g * 1024);
          }
          break;
        }
        case "cpu": {
          const c = num(val, out, tok);
          if (c !== undefined) {
            out.cpuLimitPct = c;
          }
          break;
        }
        case "priority": {
          const p = num(val, out, tok);
          if (p !== undefined) {
            out.priority = p;
          }
          break;
        }
        case "image":
          val ? (out.image = val) : out.unknown.push(tok);
          break;
        case "runtime":
          VALID_RUNTIMES.includes(val) ? (out.runtime = val) : out.unknown.push(tok);
          break;
        case "target":
          val ? (out.target = val) : out.unknown.push(tok);
          break;
        case "isolation":
        case "isolate":
          out.isolation = true;
          break;
        case "nocache":
        case "no-cache":
          out.noCache = true;
          break;
        case "scan":
          out.scan = true;
          break;
        default:
          out.unknown.push(tok);
      }
    }
  }
  return out;
}
