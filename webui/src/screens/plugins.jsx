/* Plugins — A8: edit the drop-in plugin modules (relays / pumps / runners /
 * db-providers) from the UI instead of hand-editing files on disk.
 *
 * Two views: a gallery of kind cards (click a module to open it), and a
 * full-page editor that takes over the screen. Save/Delete fire global toasts
 * so they also land in the notification bell. Saving only writes + syntax-
 * checks; running stays each subsystem's explicit sandboxed action. */
import React from "react";
import { I } from "../icons.jsx";
import { api } from "../api.js";
import { CardHead, Field, Pill, Chk } from "../components.jsx";
import { notify } from "../toast.jsx";

/* Trigger a browser download of `obj` as a pretty-printed JSON file. */
const downloadJson = (obj, filename) => {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
};

const installSummary = (r) =>
  `${r.installed} installed · ${r.skipped} skipped${r.errors ? ` · ${r.errors} error${r.errors > 1 ? "s" : ""}` : ""}`;

const KIND_META = {
  relays:      { icon: "broadcast", tone: "purple", run: "Run + bind from a group's Relays tab (fingerprint-validated)." },
  pumps:       { icon: "share",     tone: "cyan",   run: "Reference it on a hosted service via its pump field." },
  runners:     { icon: "zap",       tone: "amber",  run: "Used as a sandbox backend when running services / replicas." },
  dbproviders: { icon: "box",       tone: "emerald", run: "Reference it as a service's db_provider engine (DBaaS)." },
};

const TEMPLATES = {
  relays:
    'from fastapi import FastAPI\n\nGRID_KEY = ""\napp = FastAPI()\n\n\n@app.get("/")\n' +
    'def root():\n    return {"relay": "my-relay"}\n',
  pumps:
    'from nexus.runtime.service_tunnel import register_pump\n\n\ndef _make():\n' +
    '    def transform(direction, chunk):\n        # direction: "to_consumer" | "to_provider"; return None to drop\n' +
    '        return chunk\n    return transform\n\n\nregister_pump("my-pump", _make)\n',
  runners:
    'from nexus.runtime.replica_runner import register_runner\n\n\ndef _build(spec):\n' +
    '    # return the argv list to launch the run-spec in your sandbox\n    return ["echo", "hello"]\n\n\n' +
    'register_runner("my-runner", _build, sandboxed=True, available=lambda: True)\n',
  dbproviders:
    'KIND = "postgres"\n\n\ndef create(admin_dsn, database, user, password):\n    ...\n\n\n' +
    'def drop(admin_dsn, database, user):\n    ...\n',
};

/* Fetch a module's source — `builtin` reference implementations come from the
 * read-only built-in endpoint; everything else from the editable plugin store. */
const fetchSource = async (kind, name, builtin) => {
  const path = builtin
    ? `/local/plugins/${kind}/builtin/${encodeURIComponent(name)}`
    : `/local/plugins/${kind}/${encodeURIComponent(name)}`;
  const r = await api.get(path);
  return r.source || "";
};

/* ── full-page editor ─────────────────────────────────────────────── */
/* mode: "edit" (existing module) · "new" (blank) · "copy" (new, pre-filled
 * from `name`) · "view" (read-only built-in reference). */
const Editor = ({ kind, name, mode, doc, builtin, onClose, onChanged, onCopy }) => {
  const isNew = mode === "new" || mode === "copy";
  const readOnly = mode === "view";
  const copyable = kind === "relays";   // only the relay default is a complete, forkable module
  const [source, setSource] = React.useState("");
  const [newName, setNewName] = React.useState(mode === "copy" ? `${name}-copy` : "");
  const [dirty, setDirty] = React.useState(isNew);
  const [check, setCheck] = React.useState(null);   // {ok, error, line}
  const [err, setErr] = React.useState("");
  const [loading, setLoading] = React.useState(mode !== "new");

  React.useEffect(() => {
    if (mode === "new") { setSource(TEMPLATES[kind] || ""); return; }
    fetchSource(kind, name, builtin).then(s => setSource(s))
      .catch(e => setErr("Open failed: " + (e.detail || e.message || "")))
      .finally(() => setLoading(false));
  }, [kind, name, mode, builtin]);

  const validate = async () => {
    try { const r = await api.post("/local/plugins/validate", { source }); setCheck(r); return r.ok; }
    catch (e) { setErr("Validate failed: " + (e.detail || e.message || "")); return false; }
  };
  const save = async () => {
    setErr("");
    const nm = isNew ? newName.trim() : name;
    if (!nm) { setErr("Give the module a name."); return; }
    if (!(await validate())) return;   // the syntax pill + line below show why
    try {
      await api.put(`/local/plugins/${kind}/${encodeURIComponent(nm)}`, { source });
      notify(`Saved ${kind}/${nm}`);
      onChanged(); onClose();
    } catch (e) { setErr("Save failed: " + (e.detail || e.message || "")); }
  };
  const del = async () => {
    try {
      await api.del(`/local/plugins/${kind}/${encodeURIComponent(name)}`);
      notify(`Deleted ${kind}/${name}`); onChanged(); onClose();
    } catch (e) { setErr("Delete failed: " + (e.detail || e.message || "")); }
  };

  const title = mode === "new" ? `New ${kind} module`
              : mode === "copy" ? `Copy of ${name}`
              : `${kind} / ${name}`;

  return (
    <>
      <div className="page-head">
        <div className="row" style={{ gap: 10, alignItems: "center" }}>
          <button className="icon-btn" onClick={onClose} title="Back"><I.chevronLeft size={18}/></button>
          <div>
            <div className="page-title" style={{ fontSize: 18 }}>{title}</div>
            <div className="page-sub">{readOnly ? (copyable ? "Built-in — read-only. Make a copy to customise it." : "Built-in reference — read-only. Use “New” to create your own.") : doc}</div>
          </div>
        </div>
        <div className="page-tools">
          {check && (check.ok ? <Pill tone="emerald">✓ valid</Pill> : <Pill tone="rose">error · line {check.line || "?"}</Pill>)}
        </div>
      </div>

      <div className="card pad-lg">
        {readOnly && (
          <div className="row" style={{ gap: 8, alignItems: "center", marginBottom: 10, fontSize: 12.5, color: "var(--amber)" }}>
            <I.lock size={14}/> Read-only — this is what the app ships by default.
            {copyable ? " Make a copy to customise it." : " Use “New” to start your own from a template."}
          </div>
        )}
        {isNew && (
          <div style={{ marginBottom: 12 }}>
            <Field label="Module name" hint="letters, digits, - and _ (becomes <name>.py)">
              <input className="input mono" value={newName} maxLength={40} autoFocus
                     onChange={e => setNewName(e.target.value)} placeholder="my_module"/>
            </Field>
          </div>
        )}
        {loading ? <div className="dim" style={{ padding: 20 }}>Loading…</div> : (
          <textarea className="input mono" spellCheck={false} readOnly={readOnly}
                    style={{ width: "100%", minHeight: "58vh", resize: "vertical", fontSize: 12.5, lineHeight: 1.55, whiteSpace: "pre", overflowWrap: "normal", tabSize: 4, opacity: readOnly ? 0.85 : 1 }}
                    value={source} onChange={e => { if (readOnly) return; setSource(e.target.value); setDirty(true); setCheck(null); setErr(""); }}/>
        )}
        {check && !check.ok && <div className="hint" style={{ color: "var(--rose, #fb7185)", marginTop: 8 }}>✗ {check.error}{check.line ? ` (line ${check.line})` : ""}</div>}
        {err && <div className="hint" style={{ color: "var(--rose, #fb7185)", marginTop: 8 }}>✗ {err}</div>}
        <div className="row" style={{ gap: 8, marginTop: 14 }}>
          {readOnly
            ? (copyable
                ? <button className="btn accent" onClick={onCopy}><I.copy size={14}/> Make a copy</button>
                : <button className="btn ghost" onClick={onClose}><I.chevronLeft size={14}/> Back to list</button>)
            : <>
                <button className="btn accent" disabled={!dirty} onClick={save}><I.check size={14}/> Save</button>
                {mode === "edit" && <button className="btn ghost" onClick={onCopy}><I.copy size={14}/> Make a copy</button>}
                <button className="btn ghost u-danger" style={{ marginLeft: "auto" }} onClick={mode === "edit" ? del : onClose}>
                  <I.x size={14}/> {mode === "edit" ? "Delete" : "Discard"}
                </button>
              </>}
        </div>
        <div className="hint" style={{ fontSize: 11, marginTop: 12 }}>
          Host-trusted Python this node loads — edit with care. {KIND_META[kind] && KIND_META[kind].run}
        </div>
      </div>
    </>
  );
};

/* ── level 2: every module inside one kind ────────────────────────── */
const KindView = ({ kind, onBack, onOpen, onNew }) => {
  const meta = KIND_META[kind.kind] || {};
  const Icon = I[meta.icon] || I.terminal;
  const [q, setQ] = React.useState("");

  // Built-in reference implementations (read-only) come first, then your own
  // editable modules — so you can always see what the app ships by default.
  const rows = [...(kind.builtins || []).map(b => ({ ...b, builtin: true })), ...(kind.modules || [])];
  const shown = q.trim() ? rows.filter(m => m.name.toLowerCase().includes(q.trim().toLowerCase())) : rows;

  return (
    <>
      <div className="page-head">
        <div className="row" style={{ gap: 12, alignItems: "center" }}>
          <button className="icon-btn" onClick={onBack} title="Back to Plugins"><I.chevronLeft size={18}/></button>
          <div>
            <div className="page-title" style={{ fontSize: 18 }}>{kind.label}</div>
            <div className="page-sub">{kind.doc}</div>
          </div>
        </div>
        <div className="page-tools">
          <button className="btn accent" onClick={onNew}><I.plus size={14}/> New {kind.label.replace(/s$/, "").toLowerCase()}</button>
        </div>
      </div>

      {rows.length > 6 && (
        <div className="row" style={{ marginBottom: 12, alignItems: "center", gap: 8, maxWidth: 360 }}>
          <I.search size={14} style={{ color: "var(--t-mute)" }}/>
          <input className="input" placeholder={`Search ${kind.label.toLowerCase()}…`} value={q} onChange={e => setQ(e.target.value)}/>
        </div>
      )}

      {shown.length === 0
        ? <div className="card pad-lg dim" style={{ textAlign: "center" }}>{q.trim() ? "No matches." : `No ${kind.label.toLowerCase()} yet — create one to get started.`}</div>
        : (
          <div className="col" style={{ gap: 10 }}>
            {shown.map(m => (
              <div key={m.name} className="card"
                   onClick={() => onOpen(m.name, m.builtin)}
                   style={{ cursor: "pointer", display: "flex", alignItems: "center", gap: 14, padding: "16px 20px" }}>
                <Icon size={20} style={{ color: "var(--t-mute)", flexShrink: 0 }}/>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <code className="mono" style={{ fontSize: 14 }}>{m.name}</code>
                  {m.builtin && <span className="dim" style={{ fontSize: 11, marginLeft: 8 }}>built-in · read-only</span>}
                </div>
                <I.chevronRight size={15} style={{ color: "var(--t-mute)", flexShrink: 0 }}/>
              </div>
            ))}
          </div>
        )}
    </>
  );
};

/* ── D1: plugin packages — share & install ────────────────────────── */
/* Bundle your modules into one portable file, install one someone shared, and
 * keep a local library of packages. Install never executes code — it only
 * syntax-checks + writes the module files (running stays a separate action). */
const PackagesPanel = ({ kinds, onBack, onChanged }) => {
  const [sel, setSel] = React.useState({});   // "kind/name" -> true
  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [library, setLibrary] = React.useState([]);
  const [overwrite, setOverwrite] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const fileRef = React.useRef(null);

  const loadLib = React.useCallback(async () => {
    try { setLibrary((await api.get("/local/plugins/packages")).packages || []); } catch (_) {}
  }, []);
  React.useEffect(() => { loadLib(); }, [loadLib]);

  // Only your own editable modules are exportable (built-ins ship with the app).
  const exportable = [];
  for (const k of kinds) for (const m of (k.modules || [])) exportable.push({ kind: k.kind, name: m.name });
  const chosen = exportable.filter(m => sel[`${m.kind}/${m.name}`]);

  const toggle = (key) => setSel(s => ({ ...s, [key]: !s[key] }));

  const buildPayload = () => ({ items: chosen, name: name.trim(), description: description.trim() });

  const doDownload = async () => {
    setBusy(true);
    try {
      const r = await api.post("/local/plugins/export", buildPayload());
      downloadJson(r.package, (name.trim() || "plugins").replace(/[^A-Za-z0-9._-]/g, "_") + ".json");
      notify(`Exported ${chosen.length} module${chosen.length > 1 ? "s" : ""}`);
    } catch (e) { notify("Export failed: " + (e.detail || e.message || "")); }
    finally { setBusy(false); }
  };
  const doSave = async () => {
    setBusy(true);
    try {
      await api.post("/local/plugins/export", { ...buildPayload(), save: true });
      notify("Saved package to library"); setSel({}); setName(""); setDescription(""); loadLib();
    } catch (e) { notify("Save failed: " + (e.detail || e.message || "")); }
    finally { setBusy(false); }
  };

  const onFile = async (e) => {
    const file = e.target.files && e.target.files[0];
    if (file) { fileRef.current.value = ""; }
    if (!file) return;
    try {
      const pkg = JSON.parse(await file.text());
      const r = await api.post("/local/plugins/install", { package: pkg, overwrite });
      notify("Installed: " + installSummary(r)); onChanged();
    } catch (e) { notify("Install failed: " + (e.detail || e.message || "invalid file")); }
  };

  const installSaved = async (fn) => {
    try {
      const r = await api.post(`/local/plugins/packages/${encodeURIComponent(fn)}/install`, { overwrite });
      notify("Installed: " + installSummary(r)); onChanged();
    } catch (e) { notify("Install failed: " + (e.detail || e.message || "")); }
  };
  const downloadSaved = async (fn) => {
    try { downloadJson(await api.get(`/local/plugins/packages/${encodeURIComponent(fn)}`), fn); }
    catch (e) { notify("Download failed: " + (e.detail || e.message || "")); }
  };
  const deleteSaved = async (fn) => {
    try { await api.del(`/local/plugins/packages/${encodeURIComponent(fn)}`); notify("Package deleted"); loadLib(); }
    catch (e) { notify("Delete failed: " + (e.detail || e.message || "")); }
  };

  return (
    <>
      <div className="page-head">
        <div className="row" style={{ gap: 12, alignItems: "center" }}>
          <button className="icon-btn" onClick={onBack} title="Back to Plugins"><I.chevronLeft size={18}/></button>
          <div>
            <div className="page-title" style={{ fontSize: 18 }}>Plugin packages</div>
            <div className="page-sub">Bundle your modules into one file to share, install one someone gave you, or keep a local library. Installing only writes the files — it never runs them.</div>
          </div>
        </div>
        <div className="page-tools">
          <label className="row" style={{ gap: 6, fontSize: 12, cursor: "pointer" }} title="Overwrite a module that already exists with the same name">
            <Chk on={overwrite} onChange={() => setOverwrite(!overwrite)}/> Overwrite existing
          </label>
          <button className="btn accent" onClick={() => fileRef.current && fileRef.current.click()}><I.upload size={14}/> Install from file</button>
          <input ref={fileRef} type="file" accept="application/json,.json" style={{ display: "none" }} onChange={onFile}/>
        </div>
      </div>

      {/* Export builder */}
      <div className="card pad-lg" style={{ marginBottom: 14 }}>
        <CardHead icon={<I.share size={14}/>} tone="cyan" title="Export a package" meta={<span>{chosen.length} selected</span>}/>
        <div style={{ padding: "12px 4px 2px" }}>
          {exportable.length === 0
            ? <div className="dim" style={{ fontSize: 12, padding: "8px 0" }}>You have no editable modules yet. Create some from the Plugins gallery first.</div>
            : (
              <div className="col" style={{ gap: 6, marginBottom: 12 }}>
                {exportable.map(m => {
                  const key = `${m.kind}/${m.name}`;
                  return (
                    <label key={key} className="row" style={{ gap: 8, cursor: "pointer", fontSize: 13, alignItems: "center" }}>
                      <Chk on={!!sel[key]} onChange={() => toggle(key)}/>
                      <Pill tone="ghost">{m.kind}</Pill>
                      <code className="mono">{m.name}</code>
                    </label>
                  );
                })}
              </div>
            )}
          <div className="field-row" style={{ marginBottom: 10 }}>
            <Field label="Package name (optional)"><input className="input" value={name} maxLength={60} placeholder="my-plugin-kit" onChange={e => setName(e.target.value)}/></Field>
            <Field label="Description (optional)"><input className="input" value={description} maxLength={300} onChange={e => setDescription(e.target.value)}/></Field>
          </div>
          <div className="row" style={{ gap: 8 }}>
            <button className="btn accent" disabled={busy || chosen.length === 0} onClick={doDownload}><I.download size={14}/> Download package</button>
            <button className="btn ghost" disabled={busy || chosen.length === 0} onClick={doSave}><I.box size={14}/> Save to library</button>
          </div>
        </div>
      </div>

      {/* Saved library */}
      <div className="card pad-lg">
        <CardHead icon={<I.box size={14}/>} tone="emerald" title="Library" meta={<span>{library.length}</span>}/>
        <div style={{ padding: "12px 4px 2px" }}>
          {library.length === 0
            ? <div className="dim" style={{ fontSize: 12, padding: "8px 0" }}>No saved packages. Build one above or install one to keep here.</div>
            : (
              <div className="col" style={{ gap: 10 }}>
                {library.map(p => (
                  <div key={p.filename} className="row" style={{ alignItems: "center", gap: 12, padding: "10px 12px", border: "1px solid var(--line)", borderRadius: 10 }}>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <code className="mono" style={{ fontSize: 13 }}>{p.name || p.filename}</code>
                      <div className="dim" style={{ fontSize: 11, marginTop: 2 }}>
                        {(p.modules || []).map(m => `${m.kind}/${m.name}`).join(", ") || "—"}
                      </div>
                      {p.description && <div className="hint" style={{ fontSize: 11, marginTop: 2 }}>{p.description}</div>}
                    </div>
                    <button className="btn ghost sm" onClick={() => installSaved(p.filename)}><I.upload size={13}/> Install</button>
                    <button className="icon-btn" title="Download" onClick={() => downloadSaved(p.filename)}><I.download size={14}/></button>
                    <button className="icon-btn" title="Delete" onClick={() => deleteSaved(p.filename)}><I.trash size={14}/></button>
                  </div>
                ))}
              </div>
            )}
        </div>
      </div>
    </>
  );
};

/* ── level 1: gallery of kinds (categories) ───────────────────────── */
const PluginsScreen = () => {
  const [kinds, setKinds] = React.useState([]);
  const [openKindId, setOpenKindId] = React.useState(null);
  const [editing, setEditing] = React.useState(null);  // {kind, name, mode, doc}
  const [packages, setPackages] = React.useState(false);
  const [loading, setLoading] = React.useState(true);
  const [err, setErr] = React.useState("");

  const load = React.useCallback(async () => {
    try { const r = await api.get("/local/plugins"); setKinds(r.kinds || []); setErr(""); }
    catch (e) { setErr("Load failed: " + (e.detail || e.message || "")); }
    finally { setLoading(false); }
  }, []);
  React.useEffect(() => { load(); }, [load]);

  const openKind = kinds.find(k => k.kind === openKindId);

  if (editing) return (
    <Editor {...editing} onClose={() => setEditing(null)} onChanged={load}
            onCopy={() => setEditing({ ...editing, mode: "copy" })}/>
  );

  if (openKind) return (
    <KindView kind={openKind}
              onBack={() => setOpenKindId(null)}
              onOpen={(name, builtin) => setEditing({ kind: openKind.kind, name, mode: builtin ? "view" : "edit", builtin: !!builtin, doc: openKind.doc })}
              onNew={() => setEditing({ kind: openKind.kind, name: "", mode: "new", doc: openKind.doc })}/>
  );

  if (packages) return (
    <PackagesPanel kinds={kinds} onBack={() => setPackages(false)} onChanged={load}/>
  );

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">Plugins</div>
          <div className="page-sub">Your drop-in modules — relays, service pumps, sandbox runners, DB providers. Pick a category to see what you've built, then click a module to edit its code.</div>
        </div>
        <div className="page-tools">
          <button className="btn ghost" onClick={() => setPackages(true)}><I.box size={14}/> Packages</button>
          <button className="btn ghost" onClick={load}><I.refresh size={14}/> Refresh</button>
        </div>
      </div>

      {loading && <div className="dim" style={{ padding: 16 }}>Loading…</div>}
      {err && <div className="hint" style={{ color: "var(--rose, #fb7185)", padding: "0 0 12px" }}>✗ {err}</div>}

      <div className="plugins-grid" style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(330px, 1fr))", gap: 14 }}>
        {kinds.map(k => {
          const meta = KIND_META[k.kind] || {};
          const Icon = I[meta.icon] || I.terminal;
          const count = (k.modules || []).length;
          return (
            <div key={k.kind} className="card" onClick={() => setOpenKindId(k.kind)} style={{ cursor: "pointer" }}>
              <CardHead icon={<Icon size={14}/>} tone={meta.tone || "purple"} title={k.label}
                        meta={<span>{count}</span>}/>
              <div className="col" style={{ gap: 8, padding: "10px 14px 14px" }}>
                <div className="hint" style={{ fontSize: 11.5, minHeight: 32 }}>{k.doc}</div>
                <span className="dim" style={{ fontSize: 12 }}>{count === 0 ? "No modules yet" : `${count} module${count > 1 ? "s" : ""}`}</span>
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
};

export { PluginsScreen };
