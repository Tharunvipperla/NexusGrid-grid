/* @mentions for group chat — shared by the Groups chat tab and the Messages
 * screen. Mentions live inside the message text as plain @tokens (so they
 * survive the E2E frame untouched) and are resolved against the group's
 * roster at render time: @all, @<role>, @<member-name> (names with spaces
 * match greedily). */
import React from "react";

const norm = (s) => String(s || "").toLowerCase();

/* Build the mention dictionary for a group detail — the FULL sets, used for
 * rendering chips and the mentions-you check. */
export function mentionTargets(detail) {
  const members = (detail && detail.members) || [];
  const roles = (detail && detail.roles) || [];
  return {
    names: members.map(m => (m.display_name || "").trim()).filter(Boolean),
    roles: roles.map(r => r.name).filter(Boolean),
  };
}

/* The composer's suggestion sets: you can't mention yourself, and a role is
 * only offered when someone OTHER than you holds it (@founder when you're
 * the founder pings nobody; @admin shows while other admins exist). */
export function suggestTargets(detail) {
  const members = (detail && detail.members) || [];
  const my = detail && detail.my_pubkey;
  const others = members.filter(m => m.pubkey !== my);
  const heldByOthers = new Set();
  for (const m of others) for (const r of (m.roles || [])) heldByOthers.add(r);
  return {
    names: others.map(m => (m.display_name || "").trim()).filter(Boolean),
    roles: ((detail && detail.roles) || []).map(r => r.name).filter(n => heldByOthers.has(n)),
  };
}

/* Match an @token starting at text[i] (i points at "@"). Longest match wins
 * so "@team lead" beats "@team". Returns {label, kind, len} or null. */
function matchAt(text, i, targets) {
  const rest = text.slice(i + 1);
  if (/^all\b/i.test(rest)) return { label: "all", kind: "all", len: 4 };
  let best = null;
  for (const [list, kind] of [[targets.roles, "role"], [targets.names, "name"]]) {
    for (const t of list) {
      if (!t) continue;
      if (norm(rest).startsWith(norm(t))) {
        const after = rest[t.length];
        if (after === undefined || /[\s.,!?;:)]/.test(after)) {
          if (!best || t.length > best.label.length) best = { label: t, kind, len: t.length + 1 };
        }
      }
    }
  }
  return best;
}

/* Split message text into segments: strings and {mention} objects. */
export function parseMentions(text, targets) {
  const out = [];
  let buf = "";
  let i = 0;
  text = String(text || "");
  while (i < text.length) {
    if (text[i] === "@") {
      const m = matchAt(text, i, targets);
      if (m) {
        if (buf) { out.push(buf); buf = ""; }
        out.push({ mention: m.label, kind: m.kind });
        i += m.len;
        continue;
      }
    }
    buf += text[i];
    i += 1;
  }
  if (buf) out.push(buf);
  return out;
}

/* Does this message mention me (by name, one of my roles, or @all)? */
export function mentionsMe(text, targets, myName, myRoles) {
  const segs = parseMentions(text, targets);
  return segs.some(s => typeof s === "object" && (
    s.kind === "all"
    || (s.kind === "name" && norm(s.mention) === norm(myName))
    || (s.kind === "role" && (myRoles || []).some(r => norm(r) === norm(s.mention)))
  ));
}

/* Render message text with mention chips. */
export const MentionText = ({ text, targets }) => (
  <>
    {parseMentions(text, targets).map((seg, i) =>
      typeof seg === "string"
        ? <React.Fragment key={i}>{seg}</React.Fragment>
        : <span key={i} style={{
            background: seg.kind === "all" ? "rgba(245,158,11,0.18)" : "rgba(99,102,241,0.18)",
            color: seg.kind === "all" ? "#fbbf24" : "#a5b4fc",
            borderRadius: 4, padding: "0 4px", fontWeight: 600, fontSize: "0.95em",
          }}>@{seg.mention}</span>
    )}
  </>
);

/* A suggestion matches when the query prefixes the whole label OR any word
 * inside it ("@lead" finds "team lead"). */
const suggestionMatches = (label, q) => {
  if (!q) return true;
  const l = norm(label);
  if (l.startsWith(q)) return true;
  return l.split(/\s+/).some(w => w.startsWith(q));
};

/* Composer with @ autocomplete: live per-letter filtering, ↑/↓ to move,
 * Enter/Tab to pick, Escape to dismiss. */
export const MentionComposer = ({ value, onChange, onSend, targets, placeholder }) => {
  const [sug, setSug] = React.useState(null); // {items, query, at}
  const [hi, setHi] = React.useState(0);      // highlighted suggestion index
  const inputRef = React.useRef(null);

  const refreshSuggestions = (text, caret) => {
    const upto = text.slice(0, caret);
    const at = upto.lastIndexOf("@");
    if (at < 0 || (at > 0 && !/[\s]/.test(upto[at - 1]))) { setSug(null); return; }
    const query = upto.slice(at + 1);
    if (/[\n]/.test(query) || query.length > 24) { setSug(null); return; }
    const q = norm(query);
    const items = [
      // @all only makes sense when someone besides you is in the room
      ...(targets.names.length ? [{ label: "all", kind: "all" }] : []),
      ...targets.roles.map(r => ({ label: r, kind: "role" })),
      ...targets.names.map(n => ({ label: n, kind: "name" })),
    ].filter(it => suggestionMatches(it.label, q)).slice(0, 8);
    setSug(items.length ? { items, at, query } : null);
    setHi(0);
  };

  const pick = (item) => {
    const el = inputRef.current;
    const caret = el ? el.selectionStart : value.length;
    const before = value.slice(0, sug.at);
    const after = value.slice(caret);
    const next = `${before}@${item.label} ${after}`;
    onChange(next);
    setSug(null);
    setTimeout(() => el && el.focus(), 0);
  };

  const onKeyDown = (e) => {
    if (sug) {
      if (e.key === "ArrowDown") { e.preventDefault(); setHi((hi + 1) % sug.items.length); return; }
      if (e.key === "ArrowUp") { e.preventDefault(); setHi((hi - 1 + sug.items.length) % sug.items.length); return; }
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); pick(sug.items[hi]); return; }
      if (e.key === "Escape") { setSug(null); return; }
    } else if (e.key === "Enter") {
      onSend && onSend();
    }
  };

  return (
    <div style={{ position: "relative", flex: 1 }}>
      {sug && (
        <div className="card" style={{ position: "absolute", bottom: "calc(100% + 6px)", left: 0, minWidth: 230, padding: 6, zIndex: 20 }}>
          {sug.items.map((it, i) => (
            <div key={i} className="row"
                 style={{
                   gap: 8, padding: "5px 8px", borderRadius: 6, cursor: "pointer", alignItems: "center",
                   background: i === hi ? "color-mix(in oklab, var(--accent) 18%, transparent)" : "transparent",
                 }}
                 onMouseEnter={() => setHi(i)}
                 onMouseDown={e => { e.preventDefault(); pick(it); }}>
              <span className="pill ghost" style={{ fontSize: 9 }}>{it.kind}</span>
              <span style={{ fontSize: 13 }}>@{it.label}</span>
            </div>
          ))}
          <div className="hint" style={{ padding: "4px 8px 2px", fontSize: 10 }}>↑↓ choose · Enter to insert</div>
        </div>
      )}
      <input ref={inputRef} className="input" style={{ width: "100%" }}
             placeholder={placeholder || "Message…  (@ to mention)"}
             value={value}
             onChange={e => { onChange(e.target.value); refreshSuggestions(e.target.value, e.target.selectionStart); }}
             onKeyDown={onKeyDown}/>
    </div>
  );
};
