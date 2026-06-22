# Security Center

**Sidebar → My node → Security Center.** Your node's protection posture,
verified-accounting status, and recent security events — in one place.

---

## Task sandbox posture

How your node runs other people's work:

| Control | Meaning |
|---|---|
| **Security profile** | The sandbox hardening profile applied to container tasks (capability drops, no-new-privileges, pid limits, tmpfs, etc.). |
| **Native host runtime** | Whether the unsandboxed `native` runtime is allowed. **Off by default** — only enable it if you fully trust the work, since it has no kernel-enforced isolation. |
| **Worker consent** | Whether running a peer's task requires your explicit consent. |
| **Task network access** | Whether tasks may reach the network. |
| **Task code scanning** | Pre-run inspection of task code. |
| **Idle auto-accept** | Whether to auto-accept work when the machine is idle. |
| **IP privacy** | Mask your IP in logs/telemetry shown to others. |

These map to the deeper settings on [Local Config](local-config.md); the Security
Center is the at-a-glance posture view.

---

## Verified accounting

Shows the status of **counterparty-signed usage receipts** — the cryptographic
record of who-did-work-for-whom. Because receipts are signed, contribution
figures can't be inflated, so this is your trustworthy accounting of give/receive.

---

## Relay privacy

Confirms that traffic routed through relays stays **end-to-end encrypted** — relays
forward frames but never see your plaintext.

---

## Storage tripwires

The status of the **unauthorized-access tripwire** on data you host. If a peer
tries to access hosted data it shouldn't, you get a bell alert + an audit entry,
shown here and in [Diagnostics](diagnostics.md).

---

## Security events

A recent feed of security-relevant events (consent decisions, tripwires, blocked
requests, …). The full, filterable, exportable log lives in the
[Diagnostics audit feed](diagnostics.md#audit-feed).
