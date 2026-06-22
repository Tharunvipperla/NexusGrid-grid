/* Unified toast stack — one bottom-right notification channel for the whole
 * app instead of per-screen inline flash banners. Fire-and-forget from any
 * module: toast("Saved"), toast("Save failed: …", "danger"). */
import React from "react";
import { I } from "./icons.jsx";

let seq = 0;

/* Recent toasts are also kept in a small log so the notification bell can
 * replay them — the bell subscribes to "nexus-toast-log". */
let toastLog = [];
export const getToastLog = () => toastLog;
export const clearToastLog = () => { toastLog = []; window.dispatchEvent(new Event("nexus-toast-log")); };

const notifOff = () => {
  try { return JSON.parse(localStorage.getItem("nexus-ui-settings") || "{}").notifications === false; }
  catch (_) { return false; }
};
const snoozed = () => {
  try { return Number(localStorage.getItem("nexus-bell-snooze") || 0) > Date.now(); }
  catch (_) { return false; }
};

/* Optional `action` = {label, hash}: renders a button on the toast that
 * jumps via hash routing (e.g. {label:"View", hash:"#/telemetry"}) — the
 * confirmation doubles as the shortcut to the thing it confirmed. */
export function toast(text, tone, action, silent) {
  if (!text) return;
  if (notifOff()) return;                       // notifications turned off entirely
  if (!tone) tone = /fail|error|denied|invalid|unable/i.test(String(text)) ? "danger" : "info";
  const item = { id: ++seq, text: String(text), tone, action, ts: Date.now() };
  toastLog = [item, ...toastLog].slice(0, 30);
  window.dispatchEvent(new Event("nexus-toast-log"));
  if (!snoozed() && !silent) window.dispatchEvent(new CustomEvent("nexus-toast", { detail: item }));   // popup muted while snoozed/silent
}

/* Record in the notification bell without the on-screen popup — for
 * confirmations the bell should log but that shouldn't interrupt. */
export const notify = (text, tone, action) => toast(text, tone, action, true);

export const Toasts = () => {
  const [items, setItems] = React.useState([]);
  React.useEffect(() => {
    const on = (e) => {
      const t = e.detail;
      setItems(prev => [...prev.slice(-4), t]);
      setTimeout(() => setItems(prev => prev.filter(x => x.id !== t.id)), t.tone === "danger" ? 8000 : 4500);
    };
    window.addEventListener("nexus-toast", on);
    return () => window.removeEventListener("nexus-toast", on);
  }, []);
  if (!items.length) return null;
  return (
    <div className="toast-stack">
      {items.map(t => (
        <div key={t.id} className={"toast " + t.tone}>
          {t.tone === "danger" ? <I.alertT size={14}/> : <I.check size={14}/>}
          <span>{t.text}</span>
          {t.action && (
            <button className="btn ghost sm" style={{ flexShrink: 0 }}
                    onClick={() => { location.hash = t.action.hash; setItems(prev => prev.filter(x => x.id !== t.id)); }}>
              {t.action.label}
            </button>
          )}
          <button className="icon-btn" onClick={() => setItems(prev => prev.filter(x => x.id !== t.id))}>
            <I.x size={12}/>
          </button>
        </div>
      ))}
    </div>
  );
};
