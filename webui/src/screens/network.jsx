/* Network — trusted mesh management (port of the classic "Trusted Network
 * Mesh"): auto-discovery, manual pairing by address, the permanent pair-invite
 * link (mint / redeem / incoming requests / issued invites), the peer roster
 * with the full trust-state action matrix, and blocked peers.
 * Destructive actions (revoke / block) use two-click inline confirms. */
import React from "react";
import { I } from "../icons.jsx";
import { api } from "../api.js";
import { Pill, CardHead, Avatar } from "../components.jsx";

const Danger = ({ label, confirmLabel = "Sure?", onFire }) => {
  const [armed, setArmed] = React.useState(false);
  React.useEffect(() => {
    if (!armed) return;
    const id = setTimeout(() => setArmed(false), 3500);
    return () => clearTimeout(id);
  }, [armed]);
  return (
    <button className={"btn sm " + (armed ? "accent" : "ghost")}
            onClick={() => { if (armed) { setArmed(false); onFire(); } else setArmed(true); }}>
      {armed ? confirmLabel : label}
    </button>
  );
};

const STATUS_PILL = {
  trusted: ["emerald", "Trusted partner"],
  pending_in: ["amber", "Requests your trust"],
  pending_out: ["ghost", "Awaiting their approval"],
  trusted_pending_in: ["amber", "Requests dual connect"],
  trusted_pending_out: ["ghost", "Awaiting dual approval"],
};

const relationText = (p) => {
  if (["trusted", "trusted_pending_in", "trusted_pending_out"].includes(p.status)) {
    if (p.role === "worker") return "They compute for you";
    if (p.role === "master") return "You compute for them";
    if (p.role === "dual") return "Dual compute (both ways)";
  }
  if (p.status === "pending_out") return "Pending — awaiting their approval";
  if (p.status === "pending_in") return "Pending — they request your trust";
  return "—";
};

const NetworkScreen = ({ setRoute }) => {
  const [peers, setPeers] = React.useState([]);
  const [discovered, setDiscovered] = React.useState([]);
  const [identity, setIdentity] = React.useState(null);
  const [blocked, setBlocked] = React.useState([]);
  const [pairLink, setPairLink] = React.useState("");
  const [needsRelay, setNeedsRelay] = React.useState(false);
  const [incoming, setIncoming] = React.useState([]);
  const [issued, setIssued] = React.useState([]);
  const [target, setTarget] = React.useState("");
  const [redeem, setRedeem] = React.useState("");
  const [redeemStatus, setRedeemStatus] = React.useState("");
  const [discFilter, setDiscFilter] = React.useState("all");
  const [msg, setMsg] = React.useState("");

  const note = (text) => { setMsg(text); setTimeout(() => setMsg(""), 5000); };

  const load = React.useCallback(async () => {
    try {
      const d = await api.get("/local/peers");
      setPeers(d.peers || []);
      setDiscovered(d.discovered_lan || []);
      setIdentity(d.my_identity || null);
    } catch (_) {}
    api.get("/local/peers/blocked").then(d => setBlocked(d.blocked || [])).catch(() => {});
    api.get("/local/pair/incoming").then(d => setIncoming(d.incoming || [])).catch(() => {});
    api.get("/local/pair/invites").then(d => setIssued(d.invites || [])).catch(() => {});
    try {
      const d = await api.get("/local/pair/permanent_link");
      setPairLink(d.link || ""); setNeedsRelay(false);
    } catch (e) {
      if (e.status === 409) { setPairLink(""); setNeedsRelay(true); }
    }
  }, []);
  React.useEffect(() => {
    load();
    const id = setInterval(load, 6000);
    return () => clearInterval(id);
  }, [load]);

  const act = async (label, fn) => {
    try { await fn(); note(label + " ✓"); } catch (e) { note(label + " failed: " + (e.detail || e.message)); }
    finally { load(); }
  };
  const managePeer = (ip, action, label) => act(label, () => {
    const fd = new FormData(); fd.append("ip", ip); fd.append("action", action);
    return api.post("/local/manage_peer", fd);
  });
  const connect = (ip) => act("Pairing request sent", () => {
    const fd = new FormData(); fd.append("target_ip", ip);
    return api.post("/local/request_peer", fd);
  });

  const doRedeem = async () => {
    if (!redeem.trim()) { setRedeemStatus("Paste a nxg://pair#… link first."); return; }
    setRedeemStatus("Waiting for the issuer to accept (can take up to 60 s)…");
    try {
      const r = await api.post("/local/pair/redeem", { link: redeem.trim() });
      if (r.status === "accepted") { setRedeemStatus("✓ Accepted — paired with " + (r.issuer_pubkey || "").slice(0, 12) + "…"); setRedeem(""); load(); }
      else setRedeemStatus(r.status + ": " + (r.reason || "rejected by issuer"));
    } catch (e) { setRedeemStatus("Error: " + (e.detail || e.message)); }
  };

  // Connection state for discovery rows (UUID-first, IP fallback — keeps the
  // button stable when the beacon alternates between LAN and relay form).
  const statusByUuid = new Map(); const statusByIp = new Map();
  for (const p of peers) {
    if (p.peer_uuid) statusByUuid.set(p.peer_uuid, p.status);
    statusByIp.set(p.internal_ip || p.ip, p.status);
    if (p.resolved_ip) statusByIp.set(p.resolved_ip, p.status);
  }
  const discStatus = (d) => (d.peer_uuid && statusByUuid.get(d.peer_uuid)) || statusByIp.get(d.internal_ip || d.ip);
  const shownDisc = [...discovered].sort((a, b) => (b.score || 0) - (a.score || 0)).filter(d => {
    const connected = discStatus(d) !== undefined;
    if (discFilter === "available") return !connected;
    if (discFilter === "connected") return connected;
    return true;
  });

  const myAddr = identity ? `${identity.ip}:${identity.port}` : "";

  const hw = (d) => {
    const s = d.stats || {}; const parts = [];
    if (s.cpu_cores) parts.push(`${s.cpu_cores} cores`);
    if (s.cpu_pct !== undefined) parts.push(`CPU ${s.cpu_pct}%`);
    if (s.ram_free_mb && s.ram_total_mb) parts.push(`RAM ${(s.ram_free_mb / 1024).toFixed(1)}/${(s.ram_total_mb / 1024).toFixed(1)} GB`);
    if (s.gpu && s.gpu_name) parts.push(s.gpu_name);
    return parts.length ? parts.join(" · ") : "awaiting data…";
  };

  const rosterActions = (p) => {
    const ip = p.internal_ip || p.ip;
    const out = [];
    if (p.status === "pending_in") {
      out.push(<button key="a" className="btn accent sm" onClick={() => managePeer(ip, "accept", "Accepted")}>Accept</button>);
      out.push(<button key="ad" className="btn ghost sm" onClick={() => managePeer(ip, "accept_dual", "Accepted (dual)")}>Accept &amp; dual</button>);
      out.push(<Danger key="r" label="Reject" onFire={() => managePeer(ip, "reject", "Rejected")}/>);
    } else if (p.status === "pending_out") {
      out.push(<Danger key="c" label="Cancel request" onFire={() => managePeer(ip, "cancel", "Cancelled")}/>);
    } else if (p.status === "trusted_pending_in") {
      out.push(<button key="ad" className="btn accent sm" onClick={() => managePeer(ip, "accept_dual", "Dual approved")}>Approve dual</button>);
      out.push(<Danger key="rd" label="Reject dual" onFire={() => managePeer(ip, "reject_dual", "Dual rejected")}/>);
    } else if (p.status === "trusted_pending_out") {
      out.push(<Danger key="cd" label="Cancel dual request" onFire={() => managePeer(ip, "reject_dual", "Dual request cancelled")}/>);
    } else if (p.status === "trusted") {
      if (p.role !== "dual")
        out.push(<button key="rq" className="btn ghost sm" onClick={() => managePeer(ip, "request_dual", "Dual connect requested")}>Request dual</button>);
      out.push(p.paused
        ? <button key="ps" className="btn ghost sm" onClick={() => managePeer(ip, "resume", "Resumed")}>Resume</button>
        : <button key="ps" className="btn ghost sm" title="Stop heartbeats/RPC to this peer — they see you as offline" onClick={() => managePeer(ip, "pause", "Paused")}>Pause</button>);
      out.push(<Danger key="bl" label="Block" confirmLabel="Block?" onFire={() => act("Blocked", () => api.post(`/local/peers/block/${encodeURIComponent(ip)}`))}/>);
      out.push(<Danger key="rv" label="Revoke trust" confirmLabel="Revoke?" onFire={() => managePeer(ip, "revoke", "Trust revoked")}/>);
    }
    return out;
  };

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">Network</div>
          <div className="page-sub">Discover nearby nodes, pair securely, and manage who you trust.</div>
        </div>
        <div className="page-tools">
          <button className="btn ghost" onClick={load}><I.refresh size={14}/> Refresh</button>
        </div>
      </div>

      {msg && <div className="banner info" style={{ marginBottom: 14 }}><I.info size={14}/><span>{msg}</span></div>}

      <div className="col" style={{ gap: 14, marginBottom: 24 }}>

        {/* Identity + manual pairing */}
        <div className="split-2">
          <div className="card pad-lg">
            <div className="fsec-head"><span className="ico-tile emerald" style={{ width: 28, height: 28 }}><I.user size={14}/></span><h4>My node identity</h4></div>
            <div className="hint" style={{ marginBottom: 8 }}>Share your host address so others can pair with you.</div>
            <div className="row" style={{ gap: 10, alignItems: "center" }}>
              <span className="mono" style={{ fontSize: 15, fontWeight: 600 }}>{myAddr || "—"}</span>
              {myAddr && <button className="btn ghost sm" onClick={() => { navigator.clipboard.writeText(myAddr); note("Address copied"); }}><I.copy size={13}/> Copy</button>}
            </div>
          </div>
          <div className="card pad-lg">
            <div className="fsec-head"><span className="ico-tile cyan" style={{ width: 28, height: 28 }}><I.link size={14}/></span><h4>Pair by address</h4></div>
            <div className="hint" style={{ marginBottom: 8 }}>Type a friend's host address to start a secure pairing handshake.</div>
            <div className="row" style={{ gap: 10 }}>
              <input className="input mono" placeholder="ip:port" value={target} onChange={e => setTarget(e.target.value)} style={{ flex: 1 }}/>
              <button className="btn accent" disabled={!target.trim()} onClick={() => { connect(target.trim()); setTarget(""); }}>
                <I.zap size={14}/> Pair
              </button>
            </div>
          </div>
        </div>

        {/* Pair invite link */}
        <div className="card pad-lg">
          <div className="fsec-head">
            <span className="ico-tile purple" style={{ width: 28, height: 28 }}><I.share size={14}/></span>
            <h4>Pair invite link</h4>
            <span className="fsec-sub">safe to post publicly — each requester needs your approval, no grid key inside</span>
          </div>
          {needsRelay ? (
            <div className="banner info">
              <I.info size={14}/>
              <span>Pair links need a relay to route requests. <a style={{ color: "var(--accent)", cursor: "pointer" }} onClick={() => setRoute && setRoute("config")}>Set one up in Config</a> (start a local relay or paste an existing URL), then come back.</span>
            </div>
          ) : pairLink ? (
            <div className="row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
              <span className="mono" title={pairLink} style={{ fontSize: 11, background: "rgba(255,255,255,0.04)", border: "1px solid var(--br)", borderRadius: 6, padding: "6px 10px", whiteSpace: "nowrap" }}>{pairLink.slice(0, 32)}…</span>
              <button className="btn ghost sm" onClick={() => { navigator.clipboard.writeText(pairLink); note("Pair link copied"); }}><I.copy size={13}/> Copy full link</button>
            </div>
          ) : <div className="hint">Loading link…</div>}

          <hr className="divider" style={{ margin: "14px 0" }}/>
          <div className="row" style={{ gap: 10, alignItems: "center", flexWrap: "wrap" }}>
            <span style={{ fontSize: 13, fontWeight: 500 }}>Connect with their link</span>
            <input className="input mono" placeholder="nxg://pair#…" value={redeem} onChange={e => setRedeem(e.target.value)} style={{ flex: 1, minWidth: 220 }}/>
            <button className="btn ghost" onClick={doRedeem}>Connect</button>
          </div>
          {redeemStatus && <div className="hint" style={{ marginTop: 8 }}>{redeemStatus}</div>}

          {incoming.length > 0 && (
            <>
              <hr className="divider" style={{ margin: "14px 0" }}/>
              <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 8 }}>Incoming pair requests <Pill tone="amber">{incoming.length}</Pill></div>
              {incoming.map(r => (
                <div key={r.transient_id} className="row" style={{ gap: 10, alignItems: "center", padding: "8px 0", flexWrap: "wrap" }}>
                  <Avatar name={r.bob_display_name || r.bob_pubkey} seed={r.bob_pubkey} color="#fbbf24"/>
                  <div>
                    <div style={{ fontSize: 13, fontWeight: 600 }}>{(r.bob_display_name || "").trim() || (r.bob_pubkey || "").slice(0, 12) + "…"}</div>
                    <div className="hint mono" style={{ fontSize: 11 }}>pubkey {(r.bob_pubkey || "").slice(0, 10)}… · {(r.bob_relay_urls || []).length} relay{(r.bob_relay_urls || []).length === 1 ? "" : "s"}</div>
                  </div>
                  <div className="row" style={{ gap: 6, marginLeft: "auto" }}>
                    <button className="btn accent sm" onClick={() => act("Accepted", () => api.post(`/local/pair/incoming/${encodeURIComponent(r.transient_id)}/accept`))}>Accept</button>
                    <Danger label="Reject" onFire={() => act("Rejected", () => api.post(`/local/pair/incoming/${encodeURIComponent(r.transient_id)}/reject`))}/>
                  </div>
                </div>
              ))}
            </>
          )}

          {issued.length > 0 && (
            <>
              <hr className="divider" style={{ margin: "14px 0" }}/>
              <div style={{ fontWeight: 600, fontSize: 13, marginBottom: 6 }}>Issued invites</div>
              <table className="t">
                <thead><tr><th>Issued</th><th>Expires</th><th>Status</th><th>Used</th><th style={{ textAlign: "right" }}></th></tr></thead>
                <tbody>
                  {issued.map(inv => (
                    <tr key={inv.invite_id}>
                      <td className="mono" style={{ fontSize: 11 }}>{(inv.issued_at || "").slice(0, 19)}</td>
                      <td className="mono" style={{ fontSize: 11 }}>{(inv.expires_at || "").slice(0, 19)}</td>
                      <td><Pill tone={inv.status === "active" ? "emerald" : inv.status === "redeemed" ? "cyan" : "ghost"}>{inv.status}</Pill></td>
                      <td className="mono" style={{ fontSize: 11 }}>{inv.used_count}/{inv.max_uses}</td>
                      <td style={{ textAlign: "right" }}>
                        {inv.status === "active" && <Danger label="Revoke" onFire={() => act("Invite revoked", () => api.del(`/local/pair/invites/${encodeURIComponent(inv.invite_id)}`))}/>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </div>

        {/* Auto-discovery */}
        <div className="card">
          <CardHead icon={<I.broadcast size={14}/>} tone="cyan" title="Auto-discovery"
                    meta={<span>LAN + relay beacons</span>}>
            <div className="seg" style={{ marginLeft: "auto" }}>
              {["all", "available", "connected"].map(f => (
                <button key={f} className={discFilter === f ? "on" : ""} onClick={() => setDiscFilter(f)}>{f[0].toUpperCase() + f.slice(1)}</button>
              ))}
            </div>
          </CardHead>
          {shownDisc.length === 0 && <div className="dim" style={{ padding: 18, fontSize: 12 }}>
            {discFilter === "available" ? "No available (unconnected) nodes found." : discFilter === "connected" ? "No connected nodes in discovery." : "Listening for broadcasts…"}
          </div>}
          {shownDisc.length > 0 && (
            <table className="t">
              <thead><tr><th>Node</th><th>Fitness</th><th>Hardware</th><th>Last beacon</th><th style={{ textAlign: "right" }}></th></tr></thead>
              <tbody>
                {shownDisc.map((d, i) => {
                  const st = discStatus(d);
                  const sc = d.score || 0;
                  const scTone = sc >= 70 ? "emerald" : sc >= 40 ? "amber" : "rose";
                  return (
                    <tr key={(d.peer_uuid || d.ip) + i}>
                      <td>
                        <div style={{ fontWeight: 600, fontSize: 13 }}>{d.display_name || d.ip} <Pill tone={d.source === "relay" ? "cyan" : "emerald"}>{d.source === "relay" ? "relay" : "LAN"}</Pill></div>
                        <div className="hint mono" style={{ fontSize: 11 }}>{d.ip}</div>
                      </td>
                      <td><Pill tone={scTone}>{sc}</Pill></td>
                      <td className="dim" style={{ fontSize: 12 }}>{hw(d)}</td>
                      <td className="mono" style={{ fontSize: 11 }}>{d.last_seen}s ago</td>
                      <td style={{ textAlign: "right" }}>
                        {st === undefined
                          ? <button className="btn accent sm" onClick={() => connect(d.internal_ip || d.ip)}>Connect</button>
                          : st === "pending_out" ? <Pill tone="ghost">pending</Pill>
                          : st === "pending_in" ? <Pill tone="amber">awaiting you</Pill>
                          : <Pill tone="emerald">connected</Pill>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        {/* Roster */}
        <div className="card">
          <CardHead icon={<I.users size={14}/>} tone="emerald" title="Peer roster" meta={<span>{peers.length} peer{peers.length === 1 ? "" : "s"}</span>}/>
          {peers.length === 0 && <div className="dim" style={{ padding: 18, fontSize: 12 }}>No peers recorded — connect to a discovered node or pair by address above.</div>}
          {peers.length > 0 && (
            <table className="t">
              <thead><tr><th>Node</th><th>Relationship</th><th>Status</th><th style={{ textAlign: "right" }}>Actions</th></tr></thead>
              <tbody>
                {peers.map((p, i) => {
                  const [tonePill, label] = STATUS_PILL[p.status] || ["ghost", p.status];
                  return (
                    <tr key={(p.peer_uuid || p.ip) + i}>
                      <td>
                        <div className="row" style={{ gap: 9 }}>
                          <Avatar name={p.display_name || p.ip} seed={p.peer_uuid || p.ip} size={26}/>
                          <div>
                            <div style={{ fontWeight: 600, fontSize: 13 }}>{p.display_name || p.ip}</div>
                            <div className="hint mono" style={{ fontSize: 11 }}>{p.ip}</div>
                          </div>
                        </div>
                      </td>
                      <td className="dim" style={{ fontSize: 12 }}>{relationText(p)} {p.paused && <Pill tone="amber">paused</Pill>}</td>
                      <td><Pill tone={tonePill} dot>{label}</Pill></td>
                      <td>
                        <div className="row" style={{ gap: 6, justifyContent: "flex-end", flexWrap: "wrap" }}>{rosterActions(p)}</div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>

        {/* Blocked peers */}
        {blocked.length > 0 && (
          <div className="card">
            <CardHead icon={<I.shield size={14}/>} tone="rose" title="Blocked peers" meta={<span>{blocked.length}</span>}/>
            <div className="hint" style={{ padding: "0 16px 8px" }}>Blocked peers are hidden from the roster and rejected for all task / deposit traffic in both directions.</div>
            <table className="t">
              <tbody>
                {blocked.map(id => (
                  <tr key={id}>
                    <td className="mono" style={{ fontSize: 12 }}>{id}</td>
                    <td style={{ textAlign: "right" }}>
                      <button className="btn ghost sm" onClick={() => act("Unblocked", () => api.post(`/local/peers/unblock/${encodeURIComponent(id)}`))}>Unblock</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </>
  );
};

export { NetworkScreen };
