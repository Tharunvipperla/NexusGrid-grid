# Groups

**Sidebar → My people → Groups.** A group is a set of nodes that trust each other
and can share services, tasks, and chat. This screen creates groups, joins them,
and manages members and invites.

---

## Creating a group (Create a group)

| Field | Meaning |
|---|---|
| **Group name** | A display name for the group. |
| **Open / Private** | **Open** lets anyone with a join link in; **Private** queues join requests for an admin to approve (with an optional message). |
| **Relay to attach (optional)** | A relay (`wss://relay.example.com`) the group uses so members behind NAT can reach each other. |

The node that creates a group is its **founder** and first admin. The founder's
key anchors trust for the group (members are admitted via admin-signed grants).

---

## Joining a group (Join a group)

1. Get an `nxg://join#…` link from a member.
2. Paste it and **Check link** to preview the group.
3. Add an optional **Message to the admin** (for private groups) and join.

For an **open** group you're admitted immediately; for a **private** group your
request waits for an admin's approval (they see it in their bell).

---

## Inviting people

- **Mint invite link** — generate an `nxg://join#…` link with:
  - **Expires in (days)** — how long the link is valid.
  - **Max uses** — how many times it can be redeemed.
- **Invite friends** — invite trusted peers directly.

Invite links are **signed** and pinned to the issuing group, and expiry/usage are
enforced on the founder's side — a stale or tampered link is refused.

---

## Roles & members

- **Role name** — define roles to organize members and what they can do.
- The member list shows who's in the group; admins can manage membership.

---

## What groups unlock

Once you share a group with someone:
- They can use your group-advertised **[Services](services.md)** (subject to each
  service's access setting).
- You can target **[Dispatcher](dispatcher.md)** tasks at the group.
- The group gets its **own chat** (on this page; see also [Messages](messages.md)).

> A group also has a lightweight **chat** of its own, separate from 1:1 direct
> messages. Group/DM content is end-to-end encrypted — a relay forwarding it sees
> only ciphertext.
