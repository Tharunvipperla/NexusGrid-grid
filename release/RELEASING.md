# Releasing & Auto-Update — Developer Guide

How NexusGrid ships new versions to running nodes, and exactly what you do to
cut a release. The update path is **central** (you host a signed release) and
**user-approved** (a node never installs silently — the user clicks *Update
now*). Every node checks independently; there is no central coordinator.

---

## 1. How it works (trust model)

There are **two** Ed25519 keys, in a chain of trust, so a leaked *release* key
has a tiny blast radius:

```
ROOT key      (offline, used rarely — the one thing you guard absolutely)
  └─ signs a delegation CERT: "release key K is valid until <not_after>"
       └─ release key K   (FRESH per release, short-lived)
            └─ signs the manifest facts (version / url / sha256 / …)
                 └─ the downloaded binary's sha256 is one of those facts
```

- The **root public** key is baked into the app:
  `nexus/security/app_update.py` (`ROOT_PUBKEY_B64`). It ships in every build and
  is the single trust anchor.
- The **root private** key is **NOT in the repo** — keep it offline (hardware /
  password manager / air-gapped). It signs only the per-release delegation cert,
  rarely. `tools/sign_release.py` reads it from `NEXUS_ROOT_PRIVKEY` (base64 raw
  Ed25519) or `--root-key-file <path>`.
- Each **release** gets its **own fresh keypair**, generated on the spot by
  `sign_release.py` and certified by the root with an expiry (`--cert-days`,
  default 90). The release private key is ephemeral — used once to sign that
  manifest, then thrown away. Nothing long-lived signs releases.

A node trusts only the baked **root** key. It verifies the full chain on every
manifest: root signed the cert → cert not revoked → cert not expired → the
certified release key signed the facts → the downloaded binary matches the
signed `sha256`. A hacked download host, a man-in-the-middle, or a malicious
peer **cannot** push code to a node — they can't forge the root's signature on a
cert, and they can't sign a manifest with a key the root certified.

**Why the hierarchy:** if a release key ever leaks, the damage is bounded — it
stops working when its cert expires, and you can revoke it immediately
(`REVOKED_KEY_IDS`, §9.5) without ever touching the root. The root, which you
guard absolutely, stays offline and is only ever used to mint the next cert.

Privacy: a node's only outbound contact for updates is a single `GET` to the
release URL you configure. No telemetry, no phone-home.

---

## 2. Versioning

The version string lives in **one place**: `nexus/__init__.py` →
`__version__ = "X.Y.Z"`. It is returned by `GET /health` and compared
numerically (dot-separated integers; higher wins). Bump it for every release.

---

## 3. One-time setup: the root key

> ✅ **Already done for this project.** The root keypair has been generated: the
> **public** key is baked into `nexus/security/app_update.py` (`ROOT_PUBKEY_B64`),
> and the **private** key is in an offline file **outside the repo** —
> `NexusGrid-RELEASE-KEYS.txt` (kept in the folder *above* the git repo so it can
> never be committed). Keep that file safe and offline. You only redo the steps
> below if you deliberately rotate the root (rare — §10.6).

Do this **once**, ever (not per release):

```
python tools/sign_release.py --gen-root
```

It prints a ROOT PRIVATE and a ROOT PUBLIC key. Then:

1. Paste the **PUBLIC** key into `nexus/security/app_update.py` →
   `ROOT_PUBKEY_B64`, and ship a build with it. Every node now trusts this root.
2. Store the **PRIVATE** key **offline** (password manager / hardware token /
   air-gapped file). You only ever bring it out to cut a release. **Never commit
   it. Never put it in CI.** If you lose it you can't issue new release certs
   (you'd have to re-bake a new root and ship a build); if it leaks, see §9.6.

You only redo this if you deliberately rotate the root (rare — §9.6).

---

## 4. Cut a release (step by step)

1. **Bump the version** in `nexus/__init__.py` (e.g. `0.2.0` → `0.3.0`) and
   commit.

2. **Build the binaries for every OS.** PyInstaller only builds for the host OS,
   so cross-building isn't possible — use the CI matrix:
   - **Push the tag** (`git tag v1.0.1 && git push origin v1.0.1`). The
     `release.yml` workflow builds on `windows-latest`, `macos-latest` and
     `ubuntu-latest` and uploads three artifacts: `NexusGrid-windows`
     (`NexusGrid.exe` + `NexusGrid-Setup-<ver>.exe`), `NexusGrid-macos`
     (`NexusGrid-macos`), and `NexusGrid-linux` (`NexusGrid-linux`).
   - **Download** all three artifacts from the run into `dist\`.

   To build a single OS locally instead: `build\build_installer.bat` (Windows,
   needs [Inno Setup](https://jrsoftware.org/isdl.php) for the installer) or
   `bash build/build.sh` (macOS/Linux, produces the bare `dist/NexusGrid`).

   The bare binary (`NexusGrid.exe` / `NexusGrid-macos` / `NexusGrid-linux`) is
   the **auto-update target** for that OS. The Windows `…-Setup-<ver>.exe` is the
   installer humans download for a first install; macOS/Linux ship the bare
   binary only (no installer).

3. **Sign one manifest** covering every platform you built (this mints the fresh
   release key, gets it certified by the root, and hashes each binary). Pass a
   `URL PATH` pair per platform — the URL is where that binary will live on the
   release, the path is the local file to hash. Provide the offline **root** key
   first:
   ```
   set NEXUS_ROOT_PRIVKEY=<base64 root private key>    # Windows (or --root-key-file PATH)
   python tools/sign_release.py 1.0.1 ^
       --win   "https://github.com/Tharunvipperla/NexusGrid-releases/releases/download/v1.0.1/NexusGrid.exe"   dist\NexusGrid.exe ^
       --mac   "https://github.com/Tharunvipperla/NexusGrid-releases/releases/download/v1.0.1/NexusGrid-macos" dist\NexusGrid-macos ^
       --linux "https://github.com/Tharunvipperla/NexusGrid-releases/releases/download/v1.0.1/NexusGrid-linux" dist\NexusGrid-linux ^
       --notes "https://github.com/Tharunvipperla/NexusGrid-releases/releases/tag/v1.0.1" ^
       --min-version 1.0.0 ^
       --out manifest.json
   ```
   > ⚠️ Each `url` must point at a **bare binary**, never the installer — the
   > auto-updater downloads that file and swaps the running binary in place. A
   > node downloads only the entry for its own OS. (Windows-only releases can pass
   > just `--win`; the manifest stays backward-compatible with older nodes.)

4. **Publish** a GitHub release in **`NexusGrid-releases`** with tag `v1.0.1` and
   attach the manifest, every bare binary, and the Windows installer:
   - `manifest.json`  — what nodes fetch (`…/releases/latest/download/manifest.json`)
   - `NexusGrid.exe`, `NexusGrid-macos`, `NexusGrid-linux` — the auto-update
     binaries (the `url`s from step 3)
   - `NexusGrid-Setup-1.0.1.exe` — the Windows installer for a fresh install

5. Done. Installed nodes detect the new version on their next check and offer the
   user **Update now** (which pulls the bare exe and swaps it).

---

## 5. Point nodes at the manifest

By default every node checks a **baked-in** release channel, so a plain install
auto-updates with no configuration:

```
DEFAULT_MANIFEST_URL  (nexus/runtime/updater.py)
  = https://github.com/Tharunvipperla/NexusGrid-releases/releases/latest/download/manifest.json
```

The `/releases/latest/download/` alias always serves the newest release, so this
URL is **stable across versions** — you never re-point nodes. It's the only
hosting fact tied to a build; you'd rebuild to change it (or just use the
override below). Everything else — the per-release download URL, the binary —
lives in the signed manifest and changes freely.

To override per node/deployment, set the environment variable:

```
NEXUS_UPDATE_MANIFEST_URL=https://your-host/manifest.json   # http(s), a path, or file://
NEXUS_UPDATE_MANIFEST_URL=                                   # empty string = updates disabled
```

- **Set (non-empty)** → use that location (handy for testing, forks, private hosts).
- **Set to empty** → updates disabled (`check` reports `available: false`).
- **Unset** → the baked `DEFAULT_MANIFEST_URL` above.

---

## 6. What the user sees

- **Profile menu** footer shows the current version (e.g. `NexusGrid v0.2.0`).
  When a signed newer version exists, it becomes
  **"Update available · vX.Y.Z → Update now · Patch notes"**.
- **Interface settings → About** shows the version and the same Update control.
- The **notification bell** surfaces "App update available — vX.Y.Z".
- Clicking **Update now** is fully automatic: download → verify signature +
  sha256 → swap the exe → relaunch into the new version. No manual steps.

There is intentionally **no "you're up to date"** message — silence means
up to date; you only ever hear from it when there's something to do.

---

## 7. How the swap works (and rollback)

You can't overwrite a running `.exe` on Windows, so `apply` (in
`nexus/runtime/updater.py`):

1. downloads the new exe next to the running one (`NexusGrid.new.exe`),
2. verifies `sha256` against the **signed** manifest,
3. writes a tiny detached helper (`_nexus_update.bat` on Windows) that waits for
   the node process to exit,
4. renames the old exe to **`NexusGrid.old.exe`** (the rollback backup),
5. moves the new exe into place and **relaunches** with the same arguments.

If the new build fails to start, the previous binary is still on disk as
`NexusGrid.old.exe` — rename it back to recover. (A future enhancement could
auto-revert if the new exe fails its `/health` check on boot.)

---

## 8. Endpoints (for custom UIs)

- `GET /local/update/check` → `{ current, latest, available, notes_url }`
- `POST /local/update/apply` → `{ status: "applying", version }` then the node
  restarts. Both are auth-gated by the local token.

---

## 9. Manifest format

```json
{
  "version":      "0.3.0",
  "url":          "https://downloads.example.com/NexusGrid-0.3.0.exe",
  "sha256":       "<hex sha256 of the Windows exe>",
  "min_version":  "0.1.0",
  "notes_url":    "https://example.com/releases/0.3.0",
  "platforms": {
    "windows": { "url": "https://…/NexusGrid.exe",   "sha256": "<hex>" },
    "macos":   { "url": "https://…/NexusGrid-macos", "sha256": "<hex>" },
    "linux":   { "url": "https://…/NexusGrid-linux", "sha256": "<hex>" }
  },
  "platforms_sig": "<base64 RELEASE-KEY signature over the platforms map>",
  "cert": {
    "signing_pubkey": "<base64 release public key>",
    "key_id":         "<sha256(release pubkey)[:16]>",
    "not_after":      "2026-09-12T00:00:00Z",
    "created":        "2026-06-14T00:00:00Z"
  },
  "cert_sig":     "<base64 ROOT signature over the cert fields>",
  "sig":          "<base64 RELEASE-KEY signature over the manifest facts>"
}
```

- `sig` covers exactly `version, url, sha256, min_version, notes_url` (canonical,
  sorted-key JSON), signed by the **release** key in `cert.signing_pubkey`.
- `platforms` maps each OS to its bare binary; `platforms_sig` is the **release**
  key's signature over that map. A node downloads the entry for its own OS. The
  top-level `url`/`sha256` are the **Windows** binary, kept so nodes that predate
  the platform map still update. `platforms` is optional — a Windows-only release
  omits it and stays valid.
- `cert_sig` covers exactly `signing_pubkey, key_id, not_after, created`, signed
  by the **root** key (the one baked in as `ROOT_PUBKEY_B64`).

`sign_release.py` produces all of this; the node verifies the whole chain with
`nexus.security.app_update.verify_release`.

---

## 10. Security checklist

- [ ] **Root** private key is **offline**, never in the shipped app, the repo, or CI.
- [ ] Manifest + binary served over **HTTPS**.
- [ ] Version bumped in `nexus/__init__.py` before building.
- [ ] `sha256` in the manifest matches the **exact** uploaded binary
      (`sign_release.py` computes it from the file you pass — sign the file you
      actually ship).
- [ ] Release-key lifetime (`--cert-days`) is as short as your cadence allows
      (default 90). A leaked release key is useless past its cert expiry.

### 10.5 If a *release* key leaks (the common case — easy)

A release key is ephemeral and short-lived, so the blast radius is already small.
To kill it immediately, without touching the root:

1. Add its `key_id` (printed when you signed, also in `cert.key_id`) to
   `REVOKED_KEY_IDS` in `nexus/security/app_update.py`.
2. Ship a build carrying that revocation, and re-sign the **legitimate** current
   release with a fresh release key (just run `sign_release.py` again — it always
   mints a new key).

Nodes that have the revocation build will refuse any manifest signed by the
revoked key even before its cert expires. Nodes that don't update are still
protected once the cert expires.

### 10.6 If the *root* key leaks (rare — serious)

This is the one you guard absolutely; a leak means an attacker can mint certs and
publish updates. Recovery requires shipping a build with a **new root**:

1. `python tools/sign_release.py --gen-root` → a new root keypair.
2. Bake the new PUBLIC into `ROOT_PUBKEY_B64`, build, and distribute that build
   through a **trusted channel** (your download page over HTTPS, with a published
   sha256 — see §10.7). Auto-update can't safely deliver this, since the old root
   is compromised.
3. From then on, sign with the new root. Old nodes must re-install once.

This is why the root stays offline and is touched as rarely as possible.

### 10.7 First-install trust (separate from auto-update)

Auto-update secures *updates* to an already-trusted install. The very first
download is trusted by **other** means:

- Publish the installer's **sha256 on your HTTPS download page** so testers can
  verify what they downloaded.
- For production, buy an **Authenticode code-signing certificate** and sign the
  `.exe` so Windows SmartScreen shows your verified publisher name instead of an
  "unknown publisher" warning. (Deferred for the tester release; the published
  hash is enough to start.)

---

## 11. CI / CD

Two GitHub Actions workflows (repo root `.github/workflows/`):

- **`ci.yml`** — on every push to `main` and every PR: runs the full backend
  test suite (`pytest`) and builds + tests the UI bundle. This is the gate that
  catches regressions. Holds no secrets.
- **`release.yml`** — on a version **tag** (`vX.Y.Z`): a build **matrix** runs on
  `windows-latest`, `macos-latest` and `ubuntu-latest`, producing one artifact per
  OS (`NexusGrid-windows` with the exe + installer, `NexusGrid-macos`,
  `NexusGrid-linux`). **It does not sign** — signing is offline by design (the root
  key never touches CI). Your loop is: tag → download the artifacts →
  `sign_release.py` locally (one manifest, all platforms) → publish the binaries +
  manifest to the `NexusGrid-releases` repo.

Run the suite locally before tagging: `pytest -q`.

## 12. Future: P2P distribution (not yet built)

Today nodes download from your central host. Because the **signature** — not the
transport — is what makes an update safe, the same signed package could later be
relayed node-to-node over the grid (reusing the W64 relay-code-distribution
pattern), so updates propagate without your server. That's an additive layer on
top of this central path, not a replacement.
