/* App root — fetches live node data, routes screens, applies theme.
 *
 * Phase 1 of the v3 UI rebuild: the shell + Overview are wired to real
 * endpoints; the remaining screens render a Placeholder until later waves
 * port them. The classic UI stays the default at "/". */
import React from "react";
import { createRoot } from "react-dom/client";
import { api, subscribeEvents } from "./api.js";
import { Sidebar, Topbar, uiSettings } from "./shell.jsx";
import { lastRead, msgTs } from "./notify.js";
import { mentionTargets, mentionsMe } from "./mentions.jsx";
import { I } from "./icons.jsx";
import { Toasts, toast, notify } from "./toast.jsx";
import { OverviewScreen } from "./screens/overview.jsx";
import { SecurityScreen } from "./screens/security.jsx";
import { ConfigScreen } from "./screens/config.jsx";
import { ServicesScreen } from "./screens/services.jsx";
import { MessagesScreen } from "./screens/messages.jsx";
import { GroupsScreen } from "./screens/groups.jsx";
import { StorageScreen } from "./screens/storage.jsx";
import { TelemetryScreen } from "./screens/telemetry.jsx";
import { DispatcherScreen } from "./screens/dispatcher.jsx";
import { TopologyScreen } from "./screens/topology.jsx";
import { NetworkScreen } from "./screens/network.jsx";
import { DiagnosticsScreen } from "./screens/diagnostics.jsx";
import { ApiScreen } from "./screens/api.jsx";
import { PluginsScreen } from "./screens/plugins.jsx";

const PEER_COLORS = ["#60a5fa", "#a78bfa", "#22d3ee", "#f472b6", "#fbbf24", "#34d399", "#c084fc"];

function gpuPct(stats) {
  if (!stats || typeof stats !== "object") return null;
  const v = stats.utilization ?? stats.util ?? stats.gpu_util ?? stats.load;
  return typeof v === "number" ? v : null;
}

function deriveModel(net, relay) {
  net = net || {};
  const lw = net.local_worker || {};
  const names = net.peer_names || {};
  const node = {
    name: (lw.user_display_name || "").trim() || lw.node_identity || "this node",
    addr: lw.node_identity || "",
    online: (net.settings || {}).node_online !== false,
    cpu: lw.cpu, ram: lw.ram, gpu: gpuPct(lw.gpu_stats),
    alertCount: (net.alerts || []).length,
  };
  const workers = net.workers || {};
  const peers = Object.keys(workers).map((ip, i) => {
    const w = workers[ip] || {};
    return {
      name: (names[ip] || w.user_display_name || ip),
      addr: w.display_ip || ip,
      online: !!w.online,
      cpu: w.cpu, ram: w.ram, gpu: gpuPct(w.gpu_stats),
      color: PEER_COLORS[i % PEER_COLORS.length],
    };
  }).filter(p => p.addr !== node.addr);
  const gdrive = (net.settings || {}).gdrive_key === "***";
  return { node, peers, metrics: net.metrics || {}, alerts: net.alerts || [], gdrive, relay: relay || {} };
}

const Placeholder = ({ route }) => (
  <div className="page-head" style={{ flexDirection: "column", alignItems: "flex-start" }}>
    <div className="page-title" style={{ textTransform: "capitalize" }}>{route}</div>
    <div className="card" style={{ marginTop: 16, maxWidth: 560 }}>
      <div className="row" style={{ gap: 12, padding: 4 }}>
        <div className="ico-tile cyan" style={{ width: 36, height: 36 }}><I.layers size={18}/></div>
        <div>
          <div style={{ fontWeight: 600 }}>This screen is being rebuilt</div>
          <div className="dim" style={{ fontSize: 12, marginTop: 4 }}>
            The new <span className="mono">{route}</span> screen ships in an upcoming wave. For now,
            use the classic interface for this feature.
          </div>
          <a className="btn ghost" href="/classic" style={{ marginTop: 12, display: "inline-flex" }}>
            <I.arrUR size={14}/> Open classic UI
          </a>
        </div>
      </div>
    </div>
  </div>
);

/* Hash routing: every screen is addressable (#/groups/<gid> deep-links a
 * group), refresh keeps your place, and bell/badge click-throughs link in. */
const ROUTE_IDS = ["overview", "dispatcher", "telemetry", "groups", "messages", "services",
                   "storage", "topology", "network", "security", "diagnostics", "config", "plugins", "api"];
const parseHash = () => {
  const h = (location.hash || "").replace(/^#\/?/, "");
  const [r, ...rest] = h.split("/");
  return ROUTE_IDS.includes(r) ? { route: r, arg: rest.length ? decodeURIComponent(rest.join("/")) : null }
                               : { route: "overview", arg: null };
};

const App = () => {
  const [nav, setNav] = React.useState(parseHash);
  const route = nav.route, routeArg = nav.arg;
  const setRoute = React.useCallback((r, arg) => {
    const next = { route: r, arg: arg || null };
    setNav(next);
    const h = "#/" + r + (arg ? "/" + encodeURIComponent(arg) : "");
    if (location.hash !== h) history.pushState(null, "", h);
  }, []);
  React.useEffect(() => {
    const onHash = () => setNav(parseHash());
    window.addEventListener("hashchange", onHash);
    window.addEventListener("popstate", onHash);
    return () => { window.removeEventListener("hashchange", onHash); window.removeEventListener("popstate", onHash); };
  }, []);

  const [theme, setTheme] = React.useState(() => localStorage.getItem("nexus-theme") || "dark");
  const [collapsed, setCollapsed] = React.useState(false);
  const [net, setNet] = React.useState(null);
  const [relay, setRelay] = React.useState({});
  const [prefill, setPrefill] = React.useState(null); // clone-task → dispatcher
  const [dmTarget, setDmTarget] = React.useState(null); // groups "Message" → messages DM
  const [badges, setBadges] = React.useState({ byGroup: {}, groups: { n: 0, mention: false }, messages: { n: 0, mention: false } });
  const [storageWarn, setStorageWarn] = React.useState(null);   // {n} when deposits need attention
  const [unreachable, setUnreachable] = React.useState(false);
  const [density, setDensity] = React.useState(() => uiSettings().density);
  React.useEffect(() => {
    const on = () => setDensity(uiSettings().density);
    window.addEventListener("nexus-ui-settings-changed", on);
    return () => window.removeEventListener("nexus-ui-settings-changed", on);
  }, []);

  React.useEffect(() => {
    document.documentElement.className = theme === "light" ? "theme-light" : "";
    localStorage.setItem("nexus-theme", theme);
  }, [theme]);

  /* /local/network supports ?since=<revision>: when nothing changed the
   * server answers {unchanged:true} instead of the full snapshot. Failures
   * are counted so a restarting node shows a reconnect bar, not frozen data. */
  const netRev = React.useRef(0);
  const netFails = React.useRef(0);
  const refresh = React.useCallback(async () => {
    try {
      const d = await api.get(`/local/network?since=${netRev.current}`);
      netFails.current = 0;
      setUnreachable(false);
      if (d && d.unchanged) return;
      netRev.current = (d && d.revision) || 0;
      setNet(d);
    } catch (_) {
      netFails.current += 1;
      if (netFails.current >= 3) setUnreachable(true);
    }
  }, []);
  const [latency, setLatency] = React.useState([]);
  const refreshRelay = React.useCallback(async () => {
    try { setRelay(await api.get("/local/relay/status")); } catch (_) {}
    try {
      const snap = await api.get("/local/relay/latency");
      // Scope by reachability: LAN = private network only, everything else is
      // reachable from the public internet. Loopback relays are skipped — they
      // only matter to this machine, not worth a row in the panel.
      const scope = (h) => {
        if (/^(127\.|localhost$)/.test(h)) return null;
        if (/^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)/.test(h)) return "lan relay";
        return "public relay";
      };
      // One row per scope — collapse multiple LAN (or PUBLIC) relays into a
      // single row, keeping the most-reachable representative (lowest rtt).
      const byScope = {};
      for (const [url, info] of Object.entries((snap && snap.relays) || {})) {
        let host = url;
        try { host = new URL(url.replace(/^ws/, "http")).hostname; } catch (_) {}
        const label = scope(host);
        if (!label) continue;
        const rtt = info && info.rtt_ms;
        const cur = byScope[label];
        if (!cur || (rtt != null && (cur.rtt == null || rtt < cur.rtt))) byScope[label] = { label, url, rtt };
      }
      setLatency(["lan relay", "public relay"].filter(l => byScope[l]).map(l => byScope[l]));
    } catch (_) {}
  }, []);

  /* Sidebar power button: offline = stop serving + close tunnel + stop the
   * bundled relay; online = resume serving. */
  const power = React.useCallback(async (goOnline) => {
    try {
      await api.post("/local/settings_partial", { node_online: goOnline });
      if (!goOnline) {
        await api.post("/local/relay/tunnel/stop").catch(() => {});
        await api.post("/local/relay/stop").catch(() => {});
      }
      notify(goOnline ? "Node is back online" : "Node shut down — not serving the grid");
    } catch (e) { toast("Power toggle failed: " + (e.detail || e.message), "danger"); }
    netRev.current = 0; // force a full snapshot so the state flips instantly
    refresh(); refreshRelay();
  }, [refresh, refreshRelay]);

  /* Unread / mention badges: for every group, count messages newer than the
   * conversation's last-read stamp that aren't ours; flag a mention when any
   * of them @-mentions us (by name, a role we hold, or @all).
   * Scale guard: the groups list carries message_count, so a group is only
   * refetched when its count or last-read stamp moved since the last poll —
   * a quiet group costs nothing beyond the single list call. */
  /* Sidebar warning for Foreign Storage: any of my deposits the host is
   * evicting, or that's within ~2 days of its TTL, needs me to download or act.
   * This is live state (not a "seen" badge) — it stays until the deposit is
   * resolved, so the operator can spot it and jump straight there. */
  const refreshStorageWarn = React.useCallback(async () => {
    const TERMINAL = ["withdrawn", "deleted", "expired", "failed", "evicted", "completed"];
    const atRisk = (d) => {
      if (TERMINAL.includes(d.status)) return false;
      if (d.status === "eviction_requested" || d.status === "in_db_grace") return true;
      let exp = 0;
      if (d.ttl_at) exp = Date.parse(d.ttl_at);
      else if (d.created_at && d.ttl_days) exp = Date.parse(d.created_at) + d.ttl_days * 86400000;
      if (!exp) return false;
      return (exp - Date.now()) <= 2 * 86400000;   // near or past TTL
    };
    try {
      const r = await api.get("/local/foreign_storage/my_deposits");
      const n = (r.deposits || []).filter(atRisk).length;
      setStorageWarn(n > 0 ? { n } : null);
    } catch (_) {}
  }, []);

  const badgeCache = React.useRef({});
  const refreshBadges = React.useCallback(async () => {
    try {
      const g = await api.get("/local/groups");
      const groups = (g && g.groups) || [];
      const byGroup = {};
      const totals = { full: { n: 0, mention: false }, chat: { n: 0, mention: false } };
      const cache = badgeCache.current;
      const liveIds = new Set(groups.map(grp => grp.id));
      for (const gid of Object.keys(cache)) {
        if (!gid.startsWith("dm:") && !liveIds.has(gid)) delete cache[gid];
      }
      await Promise.all(groups.map(async (grp) => {
        try {
          const isChat = (grp.kind || "full") === "chat";
          const since = lastRead(grp.id);
          const prev = cache[grp.id];
          let badge;
          if (prev && prev.count === grp.message_count && prev.since === since) {
            badge = prev.badge;
          } else {
            const [msgRes, detail] = await Promise.all([
              api.get(`/local/groups/${encodeURIComponent(grp.id)}/messages?limit=60`),
              api.get(`/local/groups/${encodeURIComponent(grp.id)}`),
            ]);
            const msgs = (msgRes && msgRes.messages) || [];
            const fresh = msgs.filter(m => msgTs(m) > since && !(m.mine || m.is_self || m.self));
            if (fresh.length) {
              const full = mentionTargets(detail);
              const targets = isChat ? { names: full.names, roles: [] } : full;
              const me = (detail.members || []).find(m => m.pubkey === detail.my_pubkey);
              const myName = (me && me.display_name) || "";
              const myRoles = (me && me.roles) || [];
              const mention = fresh.some(m => mentionsMe(m.body ?? m.text ?? "", targets, myName, myRoles));
              badge = { n: fresh.length, mention };
            } else {
              badge = null;
            }
            cache[grp.id] = { count: grp.message_count, since, badge };
          }
          if (!badge) return;
          byGroup[grp.id] = badge;
          const bucket = totals[isChat ? "chat" : "full"];
          bucket.n += badge.n;
          bucket.mention = bucket.mention || badge.mention;
        } catch (_) {}
      }));
      // 1:1 DMs: same delta pattern via /local/dm/summary — one cheap call
      // per tick; a thread is only refetched when its counters moved.
      try {
        const sum = await api.get("/local/dm/summary");
        const dmPeers = (sum && sum.peers) || [];
        const liveDm = new Set(dmPeers.map(p => "dm:" + p.peer_uuid));
        for (const k of Object.keys(cache)) if (k.startsWith("dm:") && !liveDm.has(k)) delete cache[k];
        await Promise.all(dmPeers.map(async (p) => {
          try {
            const key = "dm:" + p.peer_uuid;
            const since = lastRead(key);
            const prev = cache[key];
            let badge;
            if (prev && prev.count === p.in_count && prev.since === since && prev.lastIn === p.last_in_at) {
              badge = prev.badge;
            } else {
              const res = await api.get(`/local/peers/${encodeURIComponent(p.peer_uuid)}/dm?limit=60`);
              const msgs = (res && res.messages) || [];
              const fresh = msgs.filter(m => m.direction !== "out" && !m.deleted && msgTs(m) > since);
              badge = fresh.length ? { n: fresh.length, mention: false } : null;
              cache[key] = { count: p.in_count, since, lastIn: p.last_in_at, badge };
            }
            if (!badge) return;
            byGroup[p.peer_uuid] = badge;
            totals.chat.n += badge.n;
          } catch (_) {}
        }));
      } catch (_) {}
      setBadges({ byGroup, groups: totals.full, messages: totals.chat });
    } catch (_) {}
  }, []);

  React.useEffect(() => {
    refresh(); refreshRelay(); refreshBadges(); refreshStorageWarn();
    const a = setInterval(refresh, 5000);
    const b = setInterval(refreshRelay, 12000);
    const c = setInterval(refreshBadges, 12000);
    const cs = setInterval(refreshStorageWarn, 12000);
    const unsub = subscribeEvents((ev) => {
      refresh();
      if (ev && String(ev.type || "").startsWith("group.")) refreshBadges();
    });
    const onRead = () => refreshBadges();
    window.addEventListener("nexus-read-changed", onRead);
    return () => {
      clearInterval(a); clearInterval(b); clearInterval(c); clearInterval(cs);
      unsub(); window.removeEventListener("nexus-read-changed", onRead);
    };
  }, [refresh, refreshRelay, refreshBadges, refreshStorageWarn]);

  const m = deriveModel(net, relay);

  let Screen;
  if (route === "overview") {
    Screen = <OverviewScreen node={m.node} peers={m.peers} metrics={m.metrics} loading={net === null}
                             tasks={(net || {}).tasks || {}} lw={(net || {}).local_worker || {}}
                             peerNames={(net || {}).peer_names || {}}
                             alerts={m.alerts} relay={m.relay} gdrive={m.gdrive} setRoute={setRoute}/>;
  } else if (route === "security") {
    Screen = <SecurityScreen settings={(net || {}).settings || {}} setRoute={setRoute}/>;
  } else if (route === "config") {
    Screen = <ConfigScreen online={m.node.online} onPower={power}/>;
  } else if (route === "api") {
    Screen = <ApiScreen/>;
  } else if (route === "plugins") {
    Screen = <PluginsScreen/>;
  } else if (route === "services") {
    Screen = <ServicesScreen/>;
  } else if (route === "messages") {
    Screen = <MessagesScreen badges={badges.byGroup} dmTarget={dmTarget} clearDmTarget={() => setDmTarget(null)}
                             initialGid={routeArg}/>;
  } else if (route === "groups") {
    Screen = <GroupsScreen badges={badges.byGroup} initialGid={routeArg}
                           onMessage={(uuid) => { setDmTarget(uuid); setRoute("messages"); }}/>;
  } else if (route === "storage") {
    Screen = <StorageScreen initialArg={routeArg}
                            onMessage={(uuid) => { setDmTarget(uuid); setRoute("messages"); }}/>;
  } else if (route === "dispatcher") {
    Screen = <DispatcherScreen setRoute={setRoute} prefill={prefill} clearPrefill={() => setPrefill(null)}/>;
  } else if (route === "telemetry") {
    Screen = <TelemetryScreen initialTask={routeArg}
                              onClone={(manifest, suggestedId) => { setPrefill({ manifest, suggestedId }); setRoute("dispatcher"); }}/>;
  } else if (route === "diagnostics") {
    Screen = <DiagnosticsScreen/>;
  } else if (route === "topology") {
    Screen = <TopologyScreen/>;
  } else if (route === "network") {
    Screen = <NetworkScreen setRoute={setRoute}/>;
  } else {
    Screen = <Placeholder route={route}/>;
  }

  return (
    <div className={"app" + (collapsed ? " collapsed" : "") + (m.node.online ? " node-online" : " node-offline") + (density === "compact" ? " density-compact" : "")}>
      <Topbar theme={theme} setTheme={setTheme} collapsed={collapsed} setCollapsed={setCollapsed}
              node={m.node} setRoute={setRoute}/>
      <Sidebar route={route} setRoute={setRoute} collapsed={collapsed} node={m.node} onPower={power}
               latency={latency} navBadges={{ groups: badges.groups, messages: badges.messages, storage: storageWarn ? { warn: true, n: storageWarn.n } : null }}/>
      {unreachable && (
        <div className="reconnect-bar">
          <I.broadcast size={13}/> Node unreachable — reconnecting…
        </div>
      )}
      <main className="main" key={route + (routeArg || "")}>
        {Screen}
      </main>
      <Toasts/>
    </div>
  );
};

createRoot(document.getElementById("root")).render(<App/>);
