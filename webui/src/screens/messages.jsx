/* Messages — group chat + 1:1 DMs + lightweight chat groups (Wave 70).
 * Two-pane: conversation list on the left (groups, chat-groups, peers), the
 * selected thread + composer on the right. Group threads support @mentions
 * (@member, @role, @all) with autocomplete; messages that mention you get an
 * accent edge. "New chat group" creates a kind=chat group and pushes invites
 * to the picked trusted peers; incoming invitations surface at the top. */
import React from "react";
import { I } from "../icons.jsx";
import { api } from "../api.js";
import { Avatar, Pill, CardHead, Chk, Modal, Field } from "../components.jsx";
import { mentionTargets, suggestTargets, MentionText, MentionComposer, mentionsMe } from "../mentions.jsx";
import { AttachControl, AttachmentView } from "../attachments.jsx";
import { PeerProfileModal } from "./groups.jsx";
import { markRead, fmtBadge } from "../notify.js";

const COLORS = ["#60a5fa", "#a78bfa", "#22d3ee", "#f472b6", "#fbbf24", "#34d399"];
const mBody = (m) => m.body ?? m.text ?? m.message ?? "";
const mSender = (m) => m.sender_name || m.sender_display || m.sender || m.from || "peer";
const mTime = (m) => {
  const t = m.ts || m.created_at || m.sent_at || m.timestamp;
  if (!t) return "";
  const d = new Date(typeof t === "number" ? (t > 1e12 ? t : t * 1000) : t);
  return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};

/* In-page "new chat group" panel: name + trusted-peer picker. */
const NewChatPanel = ({ onDone, onCancel }) => {
  const [name, setName] = React.useState("");
  const [peers, setPeers] = React.useState(null);
  const [picked, setPicked] = React.useState([]);
  const [status, setStatus] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  React.useEffect(() => {
    api.get("/local/peers").then(d => setPeers((d.peers || []).filter(p =>
      ["trusted", "trusted_pending_in", "trusted_pending_out"].includes(p.status)))).catch(() => setPeers([]));
  }, []);
  const create = async () => {
    setBusy(true); setStatus("Creating…");
    try {
      const g = await api.post("/local/groups", { name: name.trim(), kind: "chat" });
      if (picked.length) {
        setStatus("Inviting…");
        const r = await api.post(`/local/groups/${encodeURIComponent(g.id)}/invite_friends`, { peer_ips: picked });
        const ok = (r.results || []).filter(x => x.ok).length;
        setStatus(`Created — ${ok}/${picked.length} invite${picked.length === 1 ? "" : "s"} delivered. They join once they accept.`);
      } else {
        setStatus("Created.");
      }
      setTimeout(() => onDone(g.id), 1200);
    } catch (e) { setStatus("Failed: " + (e.detail || e.message)); setBusy(false); }
  };
  return (
    <Modal title="New chat group" icon={<I.users size={14}/>} tone="cyan" onClose={onCancel}
           foot={<>
             <button className="btn ghost" onClick={onCancel}>Cancel</button>
             <button className="btn accent" disabled={!name.trim() || busy} onClick={create}><I.check size={14}/> Create chat</button>
           </>}>
      <div className="hint" style={{ marginBottom: 10 }}>A lightweight group just for messaging — invited peers accept from their own Messages screen.</div>
      <Field label="Chat name">
        <input className="input" placeholder="e.g. weekend plans" maxLength={128} autoFocus
               value={name} onChange={e => setName(e.target.value)}/>
      </Field>
      <div className="label" style={{ margin: "14px 0 8px" }}>Invite trusted peers</div>
      {peers === null && <div className="hint">Loading peers…</div>}
      {peers && peers.length === 0 && <div className="hint">No trusted peers yet — pair in the Network screen first. You can still create the chat and invite later.</div>}
      {peers && peers.length > 0 && (
        <div className="col" style={{ gap: 8 }}>
          {peers.map(p => {
            const key = p.internal_ip || p.ip;
            return (
              <div key={key} className="row" style={{ gap: 8, alignItems: "center", cursor: "pointer" }}
                   onClick={() => setPicked(picked.includes(key) ? picked.filter(x => x !== key) : [...picked, key])}>
                <Chk on={picked.includes(key)}/>
                <Avatar name={p.display_name || p.ip} color="#22d3ee" size={22}/>
                <span style={{ fontSize: 13 }}>{p.display_name || p.ip}</span>
              </div>
            );
          })}
        </div>
      )}
      {status && <div className="hint" style={{ marginTop: 10 }}>{status}</div>}
    </Modal>
  );
};

const MessagesScreen = ({ badges = {}, dmTarget = null, clearDmTarget, initialGid = null }) => {
  const [convos, setConvos] = React.useState([]);
  const [sel, setSel] = React.useState(null);
  const [thread, setThread] = React.useState([]);
  const [draft, setDraft] = React.useState("");
  const [sending, setSending] = React.useState(false);
  const [creating, setCreating] = React.useState(false);
  const [invitations, setInvitations] = React.useState([]);
  const [groupDetail, setGroupDetail] = React.useState(null); // roster for mentions
  const [msg, setMsg] = React.useState("");
  const [att, setAtt] = React.useState(null);
  const [viewProfile, setViewProfile] = React.useState(null); // {nodeId, name}
  const endRef = React.useRef(null);

  const flash = (t) => { setMsg(t); setTimeout(() => setMsg(""), 4000); };

  const loadConvos = React.useCallback(async () => {
    const [g, p, inv] = await Promise.all([
      api.get("/local/groups").catch(() => ({})),
      api.get("/local/peers").catch(() => ({})),
      api.get("/local/invitations/incoming").catch(() => ({})),
    ]);
    // Only lightweight chat groups live here — a full group's conversation
    // is its Chat tab on the Groups screen.
    const groups = (Array.isArray(g) ? g : (g.groups || []))
      .filter(x => (x.kind || "full") === "chat")
      .map((x, i) => ({
        kind: "group", id: x.id, name: x.name || x.id, color: COLORS[i % COLORS.length],
        chat: true, avatar: x.avatar || "",
      }));
    const peerArr = (p.peers || p.trusted || []).filter(x => x && x.peer_uuid && x.status === "trusted");
    const seen = new Set();
    const dms = [];
    peerArr.forEach((x, i) => {
      if (seen.has(x.peer_uuid)) return;
      seen.add(x.peer_uuid);
      dms.push({ kind: "dm", id: x.peer_uuid, name: x.display_name || x.peer_uuid, color: COLORS[(i + 2) % COLORS.length] });
    });
    setConvos([...groups, ...dms]);
    setInvitations((inv && inv.offers) || []);
    setSel(prev => prev || (groups[0] || dms[0] || null));
  }, []);

  const loadThread = React.useCallback(async (c) => {
    if (!c) { setThread([]); return; }
    try {
      let res;
      if (c.kind === "group") res = await api.get(`/local/groups/${encodeURIComponent(c.id)}/messages?limit=200`);
      else res = await api.get(`/local/peers/${encodeURIComponent(c.id)}/dm?limit=200`);
      const list = Array.isArray(res) ? res : (res.messages || res.dm || res.items || []);
      setThread(list);
      // Thread on screen — clear its badge (DM read-stamps key by "dm:").
      markRead(c.kind === "group" ? c.id : "dm:" + c.id);
    } catch (_) { setThread([]); }
  }, []);

  React.useEffect(() => {
    loadConvos();
    const id = setInterval(loadConvos, 15000);
    return () => clearInterval(id);
  }, [loadConvos]);
  /* Groups screen "Message" shortcut: jump straight to that peer's DM. */
  React.useEffect(() => {
    if (!dmTarget || !convos.length) return;
    const c = convos.find(x => x.kind === "dm" && x.id === dmTarget);
    if (c) setSel(c);
    if (clearDmTarget) clearDmTarget();
  }, [dmTarget, convos, clearDmTarget]);
  /* Deep link (#/messages/<gid>): select that chat group once convos load. */
  const linked = React.useRef(false);
  React.useEffect(() => {
    if (linked.current || !initialGid || !convos.length) return;
    const c = convos.find(x => x.kind === "group" && x.id === initialGid);
    if (c) { setSel(c); linked.current = true; }
  }, [initialGid, convos]);
  React.useEffect(() => {
    loadThread(sel);
    setGroupDetail(null);
    setAtt(null);
    if (sel && sel.kind === "group") {
      api.get(`/local/groups/${encodeURIComponent(sel.id)}`).then(setGroupDetail).catch(() => {});
    }
    if (!sel) return;
    const id = setInterval(() => loadThread(sel), 5000);
    return () => clearInterval(id);
  }, [sel, loadThread]);
  React.useEffect(() => { if (endRef.current) endRef.current.scrollIntoView({ block: "end" }); }, [thread]);

  const send = async () => {
    const body = draft.trim();
    if ((!body && !att) || !sel || sending) return;
    setSending(true);
    const extra = att ? { attach_data: att.data, attach_name: att.name, attach_mime: att.mime } : {};
    try {
      if (sel.kind === "group") await api.post(`/local/groups/${encodeURIComponent(sel.id)}/messages`, { body, ...extra });
      else await api.post(`/local/peers/${encodeURIComponent(sel.id)}/dm`, { body, ...extra });
      setDraft(""); setAtt(null);
      await loadThread(sel);
    } catch (e) { flash("Failed to send: " + (e.detail || e.message || "")); }
    setSending(false);
  };

  const delMsg = async (m) => {
    const mid = m.msg_id || m.id;
    if (!mid || !sel) return;
    try {
      if (sel.kind === "group") await api.del(`/local/groups/${encodeURIComponent(sel.id)}/messages/${encodeURIComponent(mid)}`);
      else await api.del(`/local/peers/${encodeURIComponent(sel.id)}/dm/${encodeURIComponent(mid)}`);
      await loadThread(sel);
    } catch (e) { flash("Failed to delete: " + (e.detail || e.message || "")); }
  };

  const respondInvite = async (token, action) => {
    try {
      await api.post(`/local/invitations/${encodeURIComponent(token)}/${action}`);
      flash(action === "accept" ? "Joined ✓" : "Declined ✓");
      await loadConvos();
    } catch (e) { flash("Failed: " + (e.detail || e.message)); }
  };

  // Chat groups are just people — mentions resolve against member names
  // and @all only (roles never apply here). The composer additionally
  // excludes yourself from the suggestions.
  const targets = groupDetail
    ? { names: mentionTargets(groupDetail).names, roles: [] }
    : { names: [], roles: [] };
  const composeTargets = groupDetail
    ? { names: suggestTargets(groupDetail).names, roles: [] }
    : { names: [], roles: [] };
  const me = groupDetail && (groupDetail.members || []).find(m => m.pubkey === groupDetail.my_pubkey);
  const myName = (me && me.display_name) || "";
  const myRoles = (me && me.roles) || [];

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">Messages</div>
          <div className="page-sub">Chat groups with friends and 1:1 direct messages. A group's own chat lives on its Groups page.</div>
        </div>
        <div className="page-tools">
          <button className="btn accent" onClick={() => setCreating(!creating)}><I.plus size={14}/> New chat group</button>
        </div>
      </div>

      {msg && <div className={"banner " + (msg.includes("Failed") ? "danger" : "info")} style={{ marginBottom: 14 }}>
        <I.info size={14}/><span>{msg}</span>
      </div>}

      {creating && <NewChatPanel onCancel={() => setCreating(false)}
                                 onDone={(gid) => { setCreating(false); loadConvos().then(() => setSel({ kind: "group", id: gid, name: "", color: COLORS[0], chat: true })); }}/>}

      {invitations.length > 0 && (
        <div className="card pad-lg" style={{ marginBottom: 14 }}>
          <CardHead icon={<I.bell size={14}/>} tone="amber" title="Chat invitations" meta={<span>{invitations.length}</span>}/>
          {invitations.map((o) => (
            <div key={o.token} className="row" style={{ gap: 10, alignItems: "center", padding: "6px 0", flexWrap: "wrap" }}>
              <Avatar name={o.group_name || "?"} color="#fbbf24" size={26}/>
              <div style={{ flex: 1, minWidth: 160 }}>
                <div style={{ fontSize: 13, fontWeight: 600 }}>{o.group_name || o.group_id}</div>
                <div className="hint" style={{ fontSize: 11 }}>invited {(o.created_at || "").slice(0, 19)}</div>
              </div>
              <div className="row" style={{ gap: 6 }}>
                <button className="btn accent sm" onClick={() => respondInvite(o.token, "accept")}>Accept</button>
                <button className="btn ghost sm" onClick={() => respondInvite(o.token, "reject")}>Decline</button>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="card" style={{ padding: 0, display: "grid", gridTemplateColumns: "260px 1fr", height: "calc(100vh - 168px)", overflow: "hidden" }}>
        {/* conversation list */}
        <div style={{ borderRight: "1px solid var(--br)", overflowY: "auto" }}>
          {convos.length === 0 && <div className="dim" style={{ padding: 16, fontSize: 12 }}>No groups or paired peers yet.</div>}
          {convos.map((c) => (
            <div key={c.kind + c.id}
                 onClick={() => setSel(c)}
                 className={"nav-item" + (sel && sel.kind === c.kind && sel.id === c.id ? " active" : "")}
                 style={{ margin: "4px 8px", gap: 10 }}>
              {c.avatar
                ? <img src={c.avatar} alt="" style={{ width: 26, height: 26, borderRadius: "50%", objectFit: "cover" }}/>
                : <Avatar name={c.name} seed={c.kind === "dm" ? c.id : undefined} color={c.color} size={26}/>}
              <div style={{ overflow: "hidden", flex: 1 }}>
                <div className="name" style={{ whiteSpace: "nowrap", textOverflow: "ellipsis", overflow: "hidden" }}>{c.name}</div>
                <div className="dim" style={{ fontSize: 10 }}>{c.kind === "dm" ? "Direct" : "Chat group"}</div>
              </div>
              {badges[c.id] && badges[c.id].n > 0 && (
                <span style={{
                  minWidth: 18, height: 18, padding: "0 4px", borderRadius: 9,
                  display: "grid", placeItems: "center", fontSize: 9.5, fontWeight: 700,
                  fontFamily: "var(--f-mono)",
                  background: badges[c.id].mention ? "rgba(245,158,11,0.95)" : "var(--accent)",
                  color: badges[c.id].mention ? "#1a1206" : "#fff",
                }}>{fmtBadge(badges[c.id].n)}</span>
              )}
            </div>
          ))}
        </div>

        {/* thread */}
        <div style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
          {!sel && <div className="dim" style={{ margin: "auto" }}>Select a conversation</div>}
          {sel && (
            <>
              <div className="row" style={{ gap: 10, padding: "12px 16px", borderBottom: "1px solid var(--br)" }}>
                <Avatar name={sel.name} seed={sel.kind === "dm" ? sel.id : undefined} color={sel.color} size={28}/>
                <div style={{ flex: 1 }}>
                  <div style={{ fontWeight: 600 }}>{sel.name || (groupDetail && groupDetail.name) || ""}</div>
                  <div className="dim" style={{ fontSize: 11 }}>
                    {sel.kind === "dm" ? "Direct message" : "Chat group"}
                    {sel.kind === "group" && groupDetail ? ` · ${(groupDetail.members || []).length} member${(groupDetail.members || []).length === 1 ? "" : "s"}` : ""}
                  </div>
                </div>
                {sel.kind === "dm" && (
                  <button className="btn ghost sm" onClick={() => setViewProfile({ nodeId: sel.id, name: sel.name })}>
                    <I.users size={13}/> Profile
                  </button>
                )}
              </div>
              <div style={{ flex: 1, overflowY: "auto", padding: 16, display: "flex", flexDirection: "column", gap: 8 }}>
                {thread.length === 0 && <div className="dim" style={{ margin: "auto", fontSize: 12 }}>No messages yet — say hello.</div>}
                {thread.map((m, i) => {
                  const mine = m.mine || m.is_self || m.self || m.direction === "out"
                    || (groupDetail && m.sender_pubkey === groupDetail.my_pubkey) || false;
                  const body = mBody(m);
                  const mid = m.msg_id || m.id;
                  if (m.sender_pubkey === "system") return <div key={mid || i} className="dim" style={{ alignSelf: "center", fontSize: 11 }}>{body}</div>;
                  if (m.deleted) return <div key={mid || i} className="dim" style={{ alignSelf: mine ? "flex-end" : "flex-start", fontSize: 11, fontStyle: "italic" }}>message deleted</div>;
                  const pingsMe = sel.kind === "group" && !mine && mentionsMe(body, targets, myName, myRoles);
                  const attUrl = sel.kind === "group"
                    ? `/local/groups/${encodeURIComponent(sel.id)}/messages/${encodeURIComponent(mid || "")}/attachment`
                    : `/local/peers/${encodeURIComponent(sel.id)}/dm/${encodeURIComponent(mid || "")}/attachment`;
                  return (
                    <div key={mid || i} style={{ alignSelf: mine ? "flex-end" : "flex-start", maxWidth: "76%" }}>
                      {!mine && sel.kind === "group" && <div className="dim mono" style={{ fontSize: 10, marginBottom: 2 }}>{mSender(m)}</div>}
                      <div style={{
                        background: mine ? "var(--accent-w)" : "var(--bg-card-2)",
                        border: "1px solid " + (pingsMe ? "var(--amber, #fbbf24)" : mine ? "var(--accent)" : "var(--br)"),
                        borderLeft: pingsMe ? "3px solid var(--amber, #fbbf24)" : undefined,
                        borderRadius: 10, padding: "7px 11px", fontSize: 13, wordBreak: "break-word",
                      }}>
                        {body ? (sel.kind === "group" ? <MentionText text={body} targets={targets}/> : body) : null}
                        {m.attach_kind && mid && <AttachmentView m={m} url={attUrl}/>}
                      </div>
                      <div className="dim" style={{ fontSize: 10, marginTop: 2, textAlign: mine ? "right" : "left" }}>
                        {pingsMe && <Pill tone="amber">mentions you</Pill>} {mTime(m)}
                        {mine && mid && (
                          <span style={{ cursor: "pointer", marginLeft: 6, display: "inline-flex", verticalAlign: "middle", opacity: 0.7 }}
                                title={sel.kind === "group" ? "Delete for everyone" : "Delete from your thread"}
                                onClick={() => delMsg(m)}><I.trash size={11}/></span>
                        )}
                      </div>
                    </div>
                  );
                })}
                <div ref={endRef}/>
              </div>
              <div className="row" style={{ gap: 8, padding: 12, borderTop: "1px solid var(--br)" }}>
                <AttachControl att={att} setAtt={setAtt} onError={flash}/>
                {sel.kind === "group"
                  ? <MentionComposer value={draft} onChange={setDraft} onSend={send} targets={composeTargets}/>
                  : <input className="input" style={{ flex: 1 }} placeholder="Message…" value={draft}
                           onChange={e => setDraft(e.target.value)}
                           onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}/>}
                <button className="btn accent" disabled={(!draft.trim() && !att) || sending} onClick={send}><I.send size={14}/> Send</button>
              </div>
            </>
          )}
        </div>
      </div>

      {viewProfile && (
        <PeerProfileModal nodeId={viewProfile.nodeId} name={viewProfile.name}
                          onClose={() => setViewProfile(null)}/>
      )}
    </>
  );
};

export { MessagesScreen };
