# nexus/security

Authentication, cryptography, and input safety — the trust boundary of the node.
**Reuse these primitives; don't invent new crypto.** The load-bearing invariants
are documented in the [security model guide](../../docs/dev/security-model.md).

> Add a line below when you add a module.

## Auth & request gating
| Module | Purpose |
|---|---|
| `auth.py` | FastAPI deps: `verify_local_auth` (token + non-spoofable private-network check) and `verify_trusted_peer` (per-peer token). |
| `tokens.py` | Persistent secret files (`.nexus_secret` HMAC key, `.nexus_local_token`). |
| `limits.py` | Per-endpoint upload size guards (`enforce_content_length` / `enforce_actual_size`). |
| `body_limit.py` | Global request-body ceiling ASGI middleware (DoS guard). |
| `tls.py` | Self-signed TLS cert/key bootstrap + fingerprint helpers (cert pinning). |

## Identity & signing
| Module | Purpose |
|---|---|
| `group_keys.py` | Per-node Ed25519 group-identity keypair (the trust anchor). |
| `group_grant.py` | Group grant envelope crypto (Ed25519 sign/verify, canonical JSON). |
| `usage_receipt.py` | Counterparty-signed usage receipts + `sign_statement`/`verify_statement` (used by DMs, service requests). |
| `crypto.py` | HMAC signing primitives for peer payload integrity. |
| `grid_keys.py` | Derive per-context relay `grid_key` values. |
| `app_update.py` | Signed app-update verification — baked-in root key → delegated per-release keys. |

## Confidentiality (encryption)
| Module | Purpose |
|---|---|
| `group_ecies.py` | X25519 derivation + ECIES envelope (HKDF + ChaCha20Poly1305) for the group symkey / DMs. |
| `group_frame.py` | Opaque AEAD channel frames for group traffic (relay sees only ciphertext). |
| `deposit_crypto.py` | Per-deposit encryption primitives (foreign storage). |
| `cred_crypto.py` | Credential-blob crypto: AES-256-GCM at rest + fresh-key-per-message transit wrap. |

## Membership, invites & permissions
| Module | Purpose |
|---|---|
| `group_invite.py` | Group invite-link logic (bearer-token flow). |
| `group_invite_token.py` | Signed group-join-invite tokens (v2 envelopes). |
| `group_join_link.py` | Encode/parse the `nxg://join` link format. |
| `pair_invite.py` | Signed pair-invite tokens for 1:1 peer links. |
| `group_permissions.py` | Group permission constants + effective-permissions helper. |

## Execution safety
| Module | Purpose |
|---|---|
| `profiles.py` | Docker container security profiles (cap-drop, no-new-privileges, pid limits, tmpfs…). |
| `threat_scanner.py` | Pre-execution regex scan for known-bad patterns in user workspaces. |
| `entrypoint.py` | Validation of user-supplied entrypoint/setup commands. |

## Consent terms
| Module | Purpose |
|---|---|
| `foreign_storage_terms.py` | Foreign-storage T&Cs (depositor signature surface). |
| `task_data_terms.py` | Depositor IP/copyright consent for cloud task-data sources. |
