/* Client-side unread / mention tracking. Last-read timestamps live in
 * localStorage per conversation; the app shell polls group messages and
 * turns anything newer into sidebar + rail badges. Screens call markRead()
 * when a thread is on screen; a window event nudges the shell to recount. */

const KEY = (id) => "nexus-read:" + id;

export const lastRead = (id) => Number(localStorage.getItem(KEY(id)) || 0);

export const markRead = (id) => {
  try { localStorage.setItem(KEY(id), String(Date.now())); } catch (_) {}
  window.dispatchEvent(new Event("nexus-read-changed"));
};

/* Message timestamp → epoch ms (handles ISO strings and s/ms epochs). */
export const msgTs = (m) => {
  const t = m.ts || m.created_at || m.sent_at || m.timestamp;
  if (!t) return 0;
  if (typeof t === "number") return t > 1e12 ? t : t * 1000;
  const d = new Date(t);
  return isNaN(d) ? 0 : d.getTime();
};

export const fmtBadge = (n) => (n > 999 ? "999+" : String(n));

/* One relative-time format for the whole app (feeds, timelines, audit):
 * recent events read as "ago", older ones fall back to a short date.
 * Accepts epoch ms; use msgTs()/x*1000 to normalize first. */
export const fmtAgo = (ms) => {
  ms = Number(ms) || 0;
  if (!ms) return "—";
  const s = Math.floor((Date.now() - ms) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  if (s < 7 * 86400) return Math.floor(s / 86400) + "d ago";
  return new Date(ms).toLocaleDateString([], { month: "short", day: "numeric" });
};
