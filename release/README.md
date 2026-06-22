# release/

Everything for **cutting a signed release** of NexusGrid lives here.

The auto-updater (`nexus/security/app_update.py`) trusts a baked-in **root**
public key, which delegates to a per-release **signing key**, which signs the
release **manifest** (including the binary's SHA-256). See
[`RELEASING.md`](RELEASING.md) for the full process.

## Contents
- **[`RELEASING.md`](RELEASING.md)** — the developer guide: how to build, sign,
  publish, and how auto-update verifies the chain.
- **`manifest.example.json`** — the shape of a release manifest (copy + fill in,
  then sign).
- **`keys/`** — where signing keys live locally. **Never committed** (gitignored).

## ⚠️ Key handling (read this)
- The **root private key** is the crown jewel — keep it **offline** (hardware
  token / an air-gapped machine). It signs only the short-lived delegation certs,
  not releases.
- The **release signing key** is per-release and shorter-lived; a leak has a small
  blast radius (it expires and can be revoked).
- **No private key may ever be committed.** `release/keys/`, `*.key`, and
  `*.pem` under `release/` are gitignored. If a key ever lands in git history,
  treat it as compromised and rotate.
- The signing tool is [`tools/sign_release.py`](../tools/sign_release.py) (holds
  the offline keys at sign time; not shipped in the app).

## Quick shape of a manifest
A node only acts on a manifest whose `cert` is root-signed (`cert_sig`) and whose
facts are signed by the certified key (`sig`). The signed facts include the
download `url` and its `sha256`, so a tampered binary fails verification. Mark a
genuinely breaking release with `breaking: true` (+ optional `breaking_note`) so
the in-app updater warns users to back up first.
