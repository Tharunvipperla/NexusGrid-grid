# Updating

NexusGrid can update itself safely. Updates are **cryptographically verified** end
to end — your node only ever runs a release that proves it came from the project's
signing keys.

---

## How updates appear

When a new release is available you'll see:
- An **update banner** in [Interface settings](interface.md#interface-settings) and
  the profile menu, and
- An **update flag** in the [notification bell](interface.md#notification-bell).

The banner shows the new version, a **Patch notes** link, and **Update now**.

---

## Applying an update

Click **Update now**. The node:
1. Re-verifies the signed release manifest.
2. Downloads the new build and checks its hash against the signed manifest.
3. Swaps it in and restarts.

**It confirms first.** For an ordinary update you get a backup reminder; for a
release flagged **breaking** you get a stronger warning to **take a Full backup
and note your custom plugins** before proceeding. Always take that backup — see
[Backup & restore](backup-and-restore.md).

---

## Why it's safe (the chain of trust)

- The project holds an **offline root key** (baked into every node).
- The root key signs a short-lived **delegation certificate** authorizing a
  per-release signing key.
- That release key signs the manifest's facts — including the **exact hash** of the
  download.
- Your node verifies: root-signed cert → cert not expired/revoked → manifest signed
  by the certified key → downloaded binary matches the signed hash.

A tampered manifest, a wrong/expired key, or a modified binary all fail
verification, so a malicious or corrupted update can't be installed. A leaked
release key has a small blast radius (it expires and can be revoked).

---

## What's new

The **What's new** panel (profile menu, also flagged by the bell on a new release)
shows the in-app changelog. Old release notes are archived locally so you can read
past versions, and you can delete notes you don't want to keep.
