# Security Policy

NexusGrid moves code, files, and services between peers, so we take security
seriously. Thank you for helping keep users safe.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately through GitHub's **Private Vulnerability Reporting**:
the repository's **Security** tab → **Report a vulnerability**. (Maintainers:
enable this under *Settings → Code security and analysis* if it isn't already.)

Include what you found, how to reproduce it, and the impact. We aim to acknowledge
reports within a few days and will keep you updated on the fix.

## Supported versions

Security fixes target the **latest** release. Installed nodes receive them through
the signed auto-update channel; please keep your node current.

| Version | Supported |
|---------|-----------|
| latest  | ✅        |
| older   | ❌ (please update) |

## How updates are trusted

Updates are delivered over a signed chain (offline root key → per-release
delegated key → manifest → binary `sha256`); a node installs nothing it can't
verify against the baked-in root key. See [`release/RELEASING.md`](release/RELEASING.md)
and [`docs/dev/security-model.md`](docs/dev/security-model.md) for the details.
