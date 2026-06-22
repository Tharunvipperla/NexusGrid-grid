# Foreign Storage

**Sidebar → Use the grid → Foreign Storage.** Store your data on peers' disks
(encrypted, only you hold the key), and host data for peers. This screen has two
sides: **My deposits** (data you've placed elsewhere) and **Hosted for others**
(data you're storing for peers).

> **Privacy guarantee:** the host only ever stores **ciphertext**. Your encryption
> password never leaves your machine, so a host — or anyone who intercepts the
> transfer — can't read your data.

---

## Making a deposit (New deposit)

Click **New deposit** and fill in:

| Field | Meaning |
|---|---|
| **Host** | Which trusted peer / group member will store it. |
| **File on this machine** | The file to deposit, e.g. `C:\data\backup.zip`. |
| **Encryption password** | The key your data is encrypted with. **Only you have it** — keep it safe; without it the data is unrecoverable. |
| **Password hint (optional)** | A reminder for yourself (stored locally). |
| **TTL (days)** | How long the deposit should live before it's eligible for eviction. |

### Advanced — transport & transfer tuning
Optional knobs (each defaults to your node's settings):
- **Transport** — how chunks move (direct stream vs relay).
- **Transfer window (chunks)** — how many chunks are in flight at once.
- **Chunk ack timeout (s)**, **Transit retries**, **Offer timeout (s)** — tune
  reliability over flaky links.

The host receives an **offer** (in their notification bell) and must **accept**
before any bytes move — hosting is consent-gated, and the host controls how much
space it gives.

---

## Retrieving & recovering your data

On a **My deposits** row:
- **Retrieve** — pull your encrypted chunks back from the host and decrypt them
  locally (you supply the **Password** and a **Save to** path).
- **Auto-rescue** — configure automatic recovery so your data survives a host
  going away:
  - **When to act** — on eviction, or a number of **Days before TTL**.
  - **Recovery destination & order** — a local **Rescue folder** and/or a **Cloud
    credential** (an rclone remote) for overflow. Data is rescued **encrypted**
    and decrypted later, on your terms.

You can set node-wide auto-rescue defaults in [Local Config](local-config.md) and
override them per deposit here.

---

## Sharing (view grants)

You can grant a specific peer **view access** to a deposit. NexusGrid transit-wraps
the decryption key to that peer so the host can serve them the decrypted content —
without the host ever holding your key in the clear. The recipient sees a "shared"
notification; you can revoke later.

---

## Hosting for others

The **Hosted for others** side lists deposits you're storing for peers:
- Accept/decline incoming **offers** (also surfaced in the bell).
- See each deposit's size and status.
- **Delete** a deposit you host (only the depositor can delete *their* data;
  unauthorized delete attempts from other peers are refused and audited).

A **tripwire** alerts you (bell + audit) if an unauthorized access to hosted data
is detected.

---

## Where your storage footprint shows up

The on-disk size of hosted deposits and rescued data appears in
**[Diagnostics → Storage usage](diagnostics.md)**, where you can see the
breakdown and clean up.
