/* Chat attachments shared by group chat + DMs (Wave 46/47).
 * ≤5 MB rides the message inline (base64); bigger files are sender-hosted
 * and pulled on open — the server decides, the client just sends base64. */
import React from "react";
import { I } from "./icons.jsx";
import { api } from "./api.js";

export const MAX_ATTACH_BYTES = 100 * 1024 * 1024;

export const attSize = (n) => {
  n = Number(n) || 0;
  if (n >= 1 << 20) return (n / (1 << 20)).toFixed(1) + " MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
  return n + " B";
};

export const readFileB64 = (file) => new Promise((resolve, reject) => {
  const r = new FileReader();
  r.onload = () => resolve(String(r.result).split(",", 2)[1] || "");
  r.onerror = () => reject(new Error("could not read file"));
  r.readAsDataURL(file);
});

/* Paperclip button + staged-file chip. `att` = {name, mime, size, data} | null. */
export const AttachControl = ({ att, setAtt, onError }) => {
  const ref = React.useRef(null);
  const pick = async (file) => {
    if (!file) return;
    if (file.size > MAX_ATTACH_BYTES) { onError && onError("Attachment too large — max 100 MB."); return; }
    try {
      const data = await readFileB64(file);
      setAtt({ name: file.name, mime: file.type || "application/octet-stream", size: file.size, data });
    } catch (e) { onError && onError("Could not read the file: " + (e.message || e)); }
  };
  return (
    <>
      <input ref={ref} type="file" style={{ display: "none" }}
             onChange={e => { pick(e.target.files && e.target.files[0]); e.target.value = ""; }}/>
      {att ? (
        <span className="row" style={{ gap: 6, alignItems: "center", fontSize: 11, background: "rgba(255,255,255,0.04)", border: "1px solid var(--br)", borderRadius: 6, padding: "4px 8px", maxWidth: 220 }}>
          <I.paperclip size={11}/>
          <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{att.name}</span>
          <span className="dim">{attSize(att.size)}{att.size > 5 * 1024 * 1024 ? " · sender-hosted" : ""}</span>
          <span style={{ cursor: "pointer" }} onClick={() => setAtt(null)}><I.x size={12}/></span>
        </span>
      ) : (
        <button className="btn ghost sm" title="Attach a file (up to 100 MB)" onClick={() => ref.current && ref.current.click()}>
          <I.upload size={13}/>
        </button>
      )}
    </>
  );
};

/* Render a received attachment: inline image, or a download link.
 * `url` = the attachment endpoint path (token appended here). */
export const AttachmentView = ({ url, m }) => {
  const href = `${url}?local_token=${encodeURIComponent(api.token)}`;
  if ((m.attach_mime || "").startsWith("image/")) {
    return (
      <a href={href} target="_blank" rel="noreferrer" title={m.attach_name || "image"}>
        <img src={href} alt={m.attach_name || ""} style={{ maxWidth: 240, maxHeight: 240, borderRadius: 8, border: "1px solid var(--br)", display: "block", marginTop: 4 }}/>
      </a>
    );
  }
  return (
    <a className="btn ghost sm" style={{ marginTop: 4, display: "inline-flex" }} href={href} download={m.attach_name || "file"}>
      <I.paperclip size={12}/> {m.attach_name || "file"} <span className="dim">({attSize(m.attach_size)})</span>
    </a>
  );
};
