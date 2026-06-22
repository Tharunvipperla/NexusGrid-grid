/* Security Center — one place that answers "how protected is this node right
 * now, and why can the numbers be trusted". Read-only posture view: every
 * card links to where the setting is changed rather than duplicating editors. */
import React from "react";
import { I } from "../icons.jsx";
import { api } from "../api.js";
import { Pill, CardHead, Verified } from "../components.jsx";
import { fmtAgo } from "../notify.js";

/* Section head for pad-lg cards — same pattern as Local Config, so the
 * headline, body text, and rows all share one left edge. */
const SecHead = ({ icon, tone, title, meta }) => (
  <div className="fsec-head" style={{ marginBottom: 10 }}>
    <span className={"ico-tile " + (tone || "emerald")} style={{ width: 28, height: 28 }}>{icon}</span>
    <h4>{title}</h4>
    {meta && <span style={{ marginLeft: "auto" }}>{meta}</span>}
  </div>
);

const fmtTs = (ts) => fmtAgo((Number(ts) || 0) * 1000);

/* A posture row: label, current state pill, one-line meaning. */
const Posture = ({ label, ok, value, note }) => (
  <div className="row" style={{ gap: 10, padding: "9px 0", borderBottom: "1px solid var(--br-mute)", alignItems: "baseline" }}>
    <span style={{ fontSize: 13, minWidth: 210 }}>{label}</span>
    <Pill tone={ok ? "emerald" : "amber"} dot>{value}</Pill>
    <span className="hint grow">{note}</span>
  </div>
);

const SEC_ACTIONS = /unauthorized|denied|reject|tripwire|blocked|forged|invalid_sig|mismatch|revoke/i;

const SecurityScreen = ({ settings = {}, setRoute }) => {
  const [audit, setAudit] = React.useState([]);
  const [shares, setShares] = React.useState([]);   // relays allowed to read content
  const [usage, setUsage] = React.useState(null);

  React.useEffect(() => {
    let dead = false;
    (async () => {
      try {
        const a = await api.get("/local/audit?limit=400");
        if (!dead) setAudit(((a && a.events) || []).filter(e =>
          SEC_ACTIONS.test(e.action || "") || e.severity === "warning" || e.severity === "error"));
      } catch (_) {}
      try {
        const p = await api.get("/local/profile");
        if (!dead) setUsage((p && p.global_usage) || null);
      } catch (_) {}
      try {
        const g = await api.get("/local/groups");
        const withRelays = ((g && g.groups) || []).filter(x => (x.relay_count || 0) > 0).slice(0, 15);
        const out = [];
        await Promise.all(withRelays.map(async (grp) => {
          try {
            const d = await api.get(`/local/groups/${encodeURIComponent(grp.id)}`);
            for (const r of (d.relays || [])) {
              if (r.content_share) out.push({ group: grp.name || grp.id, gid: grp.id, relay: r.relay_url || r.url || "" });
            }
          } catch (_) {}
        }));
        if (!dead) setShares(out);
      } catch (_) {}
    })();
    return () => { dead = true; };
  }, []);

  const s = settings;
  const tripwires = audit.filter(e => (e.action || "").includes("unauthorized_access_detected"));
  const profile = s.security_profile || "maximum";

  return (
    <>
      <div className="page-head">
        <div>
          <div className="page-title">Security Center</div>
          <div className="page-sub">This node's protection posture, verified-accounting status, and recent security events.</div>
        </div>
        <div className="page-tools">
          <button className="btn ghost" onClick={() => setRoute && setRoute("config")}><I.cog size={14}/> Edit settings</button>
        </div>
      </div>

      <div className="split-2" style={{ marginBottom: 16 }}>
        <div className="card pad-lg">
          <SecHead icon={<I.shield size={14}/>} tone={profile === "maximum" ? "emerald" : profile === "standard" ? "amber" : "rose"}
                   title="Task sandbox posture" meta={<Pill tone={profile === "maximum" ? "emerald" : "amber"} dot>{profile}</Pill>}/>
          <Posture label="Security profile" ok={profile === "maximum"} value={profile}
                   note={profile === "maximum" ? "full sandbox, network cut, code scan" : profile === "standard" ? "basic hardening, network cut" : "legacy behaviour — not recommended"}/>
          <Posture label="Native host runtime" ok={!s.native_runtime_enabled} value={s.native_runtime_enabled ? "enabled" : "disabled"}
                   note={s.native_runtime_enabled ? "tasks may run directly on this OS" : "tasks stay containerized"}/>
          <Posture label="Worker consent" ok={!!s.require_worker_consent || !s.native_runtime_enabled}
                   value={s.require_worker_consent ? "ask first" : "automatic"}
                   note={s.require_worker_consent ? "every incoming task needs your approval" : "tasks within limits run without asking"}/>
          <Posture label="Task network access" ok={!s.allow_network_tasks} value={s.allow_network_tasks ? "allowed" : "cut"}
                   note={s.allow_network_tasks ? "tasks can reach the network" : "tasks run with network disabled"}/>
          <Posture label="Task code scanning" ok={!!s.enable_task_scanning} value={s.enable_task_scanning ? "on" : "off"}
                   note="static scan of incoming task code before execution"/>
          <Posture label="Idle auto-accept" ok={!s.idle_auto_accept} value={s.idle_auto_accept ? "on" : "off"}
                   note={s.idle_auto_accept ? `consent skipped after ${s.idle_threshold_sec || 300}s idle` : "consent is never skipped"}/>
          <Posture label="IP privacy" ok={!!s.hide_profile} value={s.hide_profile ? "masked" : "visible"}
                   note="whether peers see this node's real address"/>
        </div>

        <div className="col" style={{ gap: 16 }}>
          <div className="card pad-lg">
            <SecHead icon={<I.check size={14}/>} tone="emerald" title="Verified accounting" meta={<Verified/>}/>
            <div className="dim" style={{ fontSize: 12.5, lineHeight: 1.6 }}>
              Every usage number on this grid is recomputed from <strong>counterparty-signed receipts</strong> —
              the consumer of a task or deposit signs what it used, so no node can inflate its contribution
              or hide its consumption by editing its own code or database.
            </div>
            {usage && (
              <div className="row" style={{ gap: 18, marginTop: 12, flexWrap: "wrap" }}>
                <div><div className="label">Compute contributed</div><div className="mono name" style={{ fontSize: 15 }}>{Math.round(usage.compute_secs_contributed || 0)}s</div></div>
                <div><div className="label">Compute consumed</div><div className="mono name" style={{ fontSize: 15 }}>{Math.round(usage.compute_secs_consumed || 0)}s</div></div>
                <div><div className="label">Storage hosted</div><div className="mono name" style={{ fontSize: 15 }}>{Math.round((usage.storage_bytes_hosted || 0) / 1048576)} MB</div></div>
                <div><div className="label">Peers helped</div><div className="mono name" style={{ fontSize: 15 }}>{usage.peers_helped || 0}</div></div>
              </div>
            )}
          </div>

          <div className="card pad-lg">
            <SecHead icon={<I.eye size={14}/>} tone={shares.length ? "amber" : "emerald"} title="Relay privacy"
                     meta={<Pill tone={shares.length ? "amber" : "emerald"} dot>{shares.length ? `${shares.length} can read content` : "E2E-blind"}</Pill>}/>
            <div className="dim" style={{ fontSize: 12.5, lineHeight: 1.6 }}>
              Relays forward encrypted frames and cannot read group content unless a founder or admin
              explicitly authorizes content-share for a specific relay.
            </div>
            {shares.map((x, i) => (
              <div key={i} className="row" style={{ gap: 8, marginTop: 8, cursor: "pointer" }}
                   onClick={() => setRoute && setRoute("groups", x.gid)}>
                <Pill tone="amber" dot>can read</Pill>
                <span style={{ fontSize: 12.5 }}>{x.group}</span>
                <span className="hint mono">{x.relay}</span>
              </div>
            ))}
          </div>

          <div className="card pad-lg">
            <SecHead icon={<I.alertT size={14}/>} tone={tripwires.length ? "rose" : "emerald"} title="Storage tripwires"
                     meta={<Pill tone={tripwires.length ? "rose" : "emerald"} dot>{tripwires.length ? `${tripwires.length} triggered` : "quiet"}</Pill>}/>
            <div className="dim" style={{ fontSize: 12.5 }}>
              Unauthorized attempts to open hosted deposits are detected and logged.
            </div>
          </div>
        </div>
      </div>

      <div className="card">
        <CardHead icon={<I.pulse size={14}/>} tone="cyan" title="Security events"
                  meta={<span>{audit.length} from the last 400 audit entries</span>}/>
        {audit.length === 0 && <div className="dim" style={{ padding: 16, fontSize: 12 }}>No security-relevant events — nothing denied, rejected, or tripped recently.</div>}
        {audit.slice(0, 30).map((e, i) => (
          <div key={i} className="row" style={{ gap: 10, padding: "8px 16px", borderTop: "1px solid var(--br-mute)", fontSize: 12 }}>
            <Pill tone={e.severity === "error" ? "rose" : "amber"} dot>{e.severity || "info"}</Pill>
            <span className="mono" style={{ minWidth: 230 }}>{e.action}</span>
            <span className="dim grow">{e.details || ""}</span>
            <span className="hint">{fmtTs(e.ts)}</span>
          </div>
        ))}
      </div>
    </>
  );
};

export { SecurityScreen };
