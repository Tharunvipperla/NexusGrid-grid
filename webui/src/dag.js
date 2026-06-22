/* Pure DAG-blueprint helpers shared by the dispatcher's Builder / Graph / JSON
 * views. No React here on purpose: keeping the parse / validate / layout / cycle
 * logic framework-free makes it unit-testable under `node --test`
 * (see webui/test/dag.test.mjs). The JSON string stays the source of truth. */

export const parseDag = (s) => {
  try { const p = JSON.parse(s); return Array.isArray(p) ? p : null; } catch (_) { return null; }
};

/* CSV -> array, dropping the key entirely when empty so the step inherits the
 * dispatch-level default instead of being forced grid-wide. */
export const csvArr = (s) => {
  const a = s.split(",").map(x => x.trim()).filter(Boolean);
  return a.length ? a : undefined;
};

/* Semantic validation beyond "is it JSON" — the messages the user actually needs. */
export const dagIssues = (nodes) => {
  if (!Array.isArray(nodes)) return ["Blueprint must be a JSON array of steps."];
  if (!nodes.length) return ["Add at least one step."];
  const issues = [];
  const ids = nodes.map(n => (n && n.id) || "");
  ids.forEach((id, i) => { if (!String(id).trim()) issues.push(`Step ${i + 1}: missing id.`); });
  const seen = {}; ids.forEach(id => { if (id) seen[id] = (seen[id] || 0) + 1; });
  Object.keys(seen).filter(k => seen[k] > 1).forEach(k => issues.push(`Duplicate id "${k}".`));
  const idSet = new Set(ids.filter(Boolean));
  nodes.forEach(n => {
    if (!n || typeof n !== "object") { issues.push("A step is not an object."); return; }
    if (!n.entrypoint || !String(n.entrypoint).trim()) issues.push(`${n.id || "?"}: missing run command.`);
    (n.depends_on || []).forEach(d => { if (!idSet.has(d)) issues.push(`${n.id || "?"}: depends_on "${d}" not found.`); });
    if (n.slice_count != null && (!Number.isInteger(n.slice_count) || n.slice_count < 1))
      issues.push(`${n.id || "?"}: slice_count must be a positive integer.`);
  });
  // Cycle detection (DFS three-colour).
  const adj = {}; nodes.forEach(n => { if (n && n.id) adj[n.id] = (n.depends_on || []).filter(Boolean); });
  const color = {};
  const visit = (u) => {
    color[u] = 1;
    for (const v of (adj[u] || [])) {
      if (color[v] === 1) return true;
      if (color[v] === undefined && visit(v)) return true;
    }
    color[u] = 2; return false;
  };
  for (const id of Object.keys(adj)) { if (color[id] === undefined && visit(id)) { issues.push("Dependency cycle detected."); break; } }
  return issues;
};

/* Depth-column layout used by both the read-only DagGraph and the interactive
 * DagCanvas: nodes are placed in dependency-depth columns, spread vertically. */
export const layoutDag = (nodes) => {
  const byId = {}; for (const n of nodes) if (n && n.id) byId[n.id] = n;
  const depth = {};
  const depthOf = (id, seen) => {
    if (depth[id] != null) return depth[id];
    seen = seen || new Set();
    if (seen.has(id)) return 0; // cycle guard
    seen.add(id);
    const deps = ((byId[id] && byId[id].depends_on) || []).filter(d => byId[d]);
    const d = deps.length ? 1 + Math.max(...deps.map(x => depthOf(x, seen))) : 0;
    depth[id] = d; return d;
  };
  nodes.forEach(n => { if (n && n.id) depthOf(n.id); });
  const cols = [];
  for (const n of nodes) if (n && n.id) (cols[depth[n.id]] = cols[depth[n.id]] || []).push(n);
  const maxCol = Math.max(1, ...cols.map(c => (c || []).length));
  const W = Math.max(440, cols.length * 175);
  const H = Math.max(190, maxCol * 64);
  const pos = {};
  cols.forEach((col, ci) => col.forEach((n, ri) => {
    pos[n.id] = { x: 72 + ci * ((W - 144) / Math.max(1, cols.length - 1) || 0), y: (H / (col.length + 1)) * (ri + 1) };
  }));
  return { pos, W, H };
};

/* Would adding "nodeId depends_on depId" close a loop? Simulate the edge, then
 * run the same three-colour DFS dagIssues uses. */
export const wouldCycle = (nodes, nodeId, depId) => {
  const adj = {};
  nodes.forEach(n => { if (n && n.id) adj[n.id] = [...(n.depends_on || [])]; });
  (adj[nodeId] = adj[nodeId] || []).push(depId);
  const color = {};
  const visit = (u) => {
    color[u] = 1;
    for (const v of (adj[u] || [])) {
      if (color[v] === 1) return true;
      if (color[v] === undefined && visit(v)) return true;
    }
    color[u] = 2; return false;
  };
  for (const id of Object.keys(adj)) if (color[id] === undefined && visit(id)) return true;
  return false;
};
