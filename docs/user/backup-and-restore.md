# Backup & Restore

Your node's **identity files are your node.** If you lose them, you lose your
cryptographic identity and peers would have to re-trust you. Back up regularly —
especially before an update flagged as breaking.

Find this on **[Local Config → Backup & restore](screens/local-config.md#backup--restore)**.

---

## Two kinds of backup

| Kind | Contains | Use when |
|---|---|---|
| **Backup** | Your **identity** (`.nexus_*` keys/token/secret) + the **database** — settings, dispatch/DAG templates, the secrets vault, deposit records, DBaaS definitions. Everything that's DB- or settings-backed. | Routine protection of your node's identity and configuration. |
| **Full backup** | Everything in *Backup*, **plus** on-disk data the database only references: your **plugin folders**, **completed-task artifacts**, and **hosted deposit bytes**. | Before a risky update, migrating to a new machine, or when you want a complete snapshot. |

(Full backup skips regenerable caches and `__pycache__`.)

Both download as a single zip with a manifest describing what's inside.

---

## Restoring

- **One upload restores either kind** — the node auto-detects whether the file is a
  normal or full backup and extracts accordingly.
- A restore is **staged and applied on the next start** — your running node is
  never overwritten in place. The previous database is kept aside as
  `.pre_restore` so you can recover if something's wrong.
- Extraction is **path-traversal safe**, so a malformed/hostile archive can't write
  outside the node directory.

### Version safety
A backup taken on a **newer** node version than the one you're restoring onto is
**refused** ("data format vX > this node's vY — update first"). Restoring *older →
newer* always works: NexusGrid's database migrations are **additive**, so an old
backup loads on a new node with new settings arriving at their defaults and your
saved data preserved.

---

## What to do before an update

If an update is flagged **breaking** (the update banner warns you):
1. Take a **Full backup** (so plugins and hosted bytes are captured too).
2. Note any custom plugins you rely on.
3. Then apply the update.

See [Updating](updating.md).

---

## Tips
- Store backups somewhere off the machine (another disk, a trusted peer via
  foreign storage, or cloud) — a backup on the same disk that dies isn't a backup.
- Treat a backup like a secret: it contains your identity keys and your secrets
  vault. Anyone with it can impersonate your node.
