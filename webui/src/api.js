/* Data layer for the v3 UI.
 *
 * Auth model matches the legacy index.html: the server injects the local API
 * token into a <meta name="nexus-token"> tag; every /local call carries it as
 * an X-Local-Token header. SSE can't set headers, so the event stream takes
 * the token as a query param (same as the legacy UI). */

const TOKEN = (() => {
  const m = document.querySelector('meta[name="nexus-token"]');
  return (m && m.getAttribute("content")) || "";
})();

async function call(method, path, body) {
  const opts = {
    method,
    headers: { "X-Local-Token": TOKEN },
  };
  if (body instanceof FormData) {
    opts.body = body; // browser sets the multipart Content-Type + boundary
  } else if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch (_) { data = text; }
  }
  if (!res.ok) {
    const detail = (data && data.detail) || res.statusText || "request failed";
    const err = new Error(detail);
    err.status = res.status;
    err.detail = detail;
    throw err;
  }
  return data;
}

export const api = {
  token: TOKEN,
  get: (path) => call("GET", path),
  post: (path, body) => call("POST", path, body ?? {}),
  put: (path, body) => call("PUT", path, body ?? {}),
  del: (path) => call("DELETE", path),
};

/* Subscribe to the in-process event bus (SSE). Returns an unsubscribe fn.
 * onEvent receives the parsed event payload ({type, ...}). */
export function subscribeEvents(onEvent) {
  if (!window.EventSource) return () => {};
  const src = new EventSource(`/local/events/stream?local_token=${encodeURIComponent(TOKEN)}`);
  src.onmessage = (e) => {
    if (!e.data) return;
    try { onEvent(JSON.parse(e.data)); } catch (_) { /* heartbeat / non-JSON */ }
  };
  src.onerror = () => { /* EventSource auto-reconnects */ };
  return () => src.close();
}
