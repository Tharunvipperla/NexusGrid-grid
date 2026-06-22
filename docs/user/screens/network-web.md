# Network Web

**Sidebar → My people → Network Web.** Discover nearby nodes, **pair** securely to
establish 1:1 trust, and manage everyone you trust. This is where trusted-peer
relationships are made and maintained.

---

## Discovering & connecting

- **Auto-discovery** — nodes on the same LAN announce themselves via a broadcast
  beacon and appear here automatically. Discovery shows them; you still **pair** to
  establish trust.
- **Connect** — reach a node directly by `ip:port`, or paste a pairing link
  (`nxg://pair#…`).

### Pairing (establishing trust)
1. One node creates a **pair invite link** (`nxg://pair#…`) and shares it.
2. The other node redeems it via **Connect**.
3. The issuing node gets a request (in its bell) and **Accept**s it.
4. Both nodes are now **trusted peers** — they've exchanged per-peer secret tokens
   and recorded each other's cryptographic identity.

Pair invites are **signed and pinned to the issuer**, so a tampered or
wrong-issuer link is refused.

---

## Managing trust

For each connection you'll have controls depending on its state:

| Action | What it does |
|---|---|
| **Accept** / **Reject** | Respond to an incoming pair request. |
| **Cancel request** | Withdraw a pair request you sent. |
| **Request dual / Approve dual / Reject dual** | The two-way ("dual") pairing handshake that makes the trust mutual. |
| **Pause** | Stop heartbeats/RPC to this peer — they see you as offline (without un-trusting). |
| **Resume** | Re-enable a paused peer. |
| **Block** | Refuse all peer-protocol requests from this peer (even though they once held a valid token). |
| **Revoke trust** / **Revoke** | End the trusted relationship entirely. |

---

## What trusted peers can do

A trusted peer can (subject to your consent on each action):
- Exchange **[tasks](dispatcher.md)** with you.
- Be a **host** for your [foreign storage](foreign-storage.md) (and you for theirs).
- Exchange **[direct messages](messages.md)**.

> Trust is mutual and explicit. You control who's trusted, and **Block**/**Revoke**
> let you cut a peer off at any time. For many-to-many trust across a team, use
> [Groups](groups.md) instead of pairing everyone individually.
