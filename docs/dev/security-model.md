# Security Model & Invariants

NexusGrid has been through a multi-pass security review. The shape of the risk is
**P2P trust**, not classic web bugs — there's no `eval`/`exec` of data, no
`pickle`/`yaml.load`, no `shell=True`, no SQL injection. Keep it that way. The
invariants below are load-bearing; don't regress them.

---

## 1. Authorize by cryptographic pubkey, never the gossiped UUID
Node UUIDs are broadcast in beacons/profiles — **not secret**. Every authorization
decision must bind to the **group pubkey** a signature proves, not a claimed UUID
or IP.
- Service requests check `GroupMember.pubkey == consumer_pubkey` (and the trusted
  fallback binds `Peer.peer_group_pubkey`).
- DMs are **signed** (`sign_statement`/`verify_statement` with kind `dm`) and
  verified against the pubkey bound to `from_uuid`.
- Group invites pin the envelope's `founder_pubkey` to the locally-recorded one;
  pair invites pin `expected_issuer_pubkey`.
- Foreign-storage ops (retrieve/delete/view-grant) check `peer_uuid == depositor_uuid`.

When you add a peer-facing handler, **bind trust to the key**, and prefer pinning
the expected pubkey at the call site (the `verify_*_invite` functions take an
`expected_*_pubkey` for exactly this reason).

## 2. Crypto: use the existing primitives, with domain separation
- Signing: Ed25519 via `cryptography`; sign `domain || canonical_json(sort_keys=True)`
  with a **distinct domain per context** (statement/receipt/worker-proof/grant/DM).
- Confidentiality: ECIES = X25519 + HKDF-SHA256 + ChaCha20Poly1305, **fresh
  ephemeral key + random nonce per seal**. At-rest/transit wraps derive a **fresh
  per-message key** (HKDF salted by a random nonce) so a fixed AES-GCM nonce is
  safe.
- Randomness: **always `secrets`/`os.urandom`**, never the `random` module.
- Compares: `hmac.compare_digest` for tokens.
Don't invent new crypto; reuse `security/group_grant.py`, `group_frame.py`,
`group_ecies.py`, `usage_receipt.py`, `cred_crypto.py`.

## 3. Code execution is opt-in, consent-gated, sandboxed, allowlisted
- Container images must be on the receiving node's `allowed_images` allowlist;
  Dockerfile `FROM` bases are allowlist-checked (tokenize on **any** whitespace).
- `native` runtime is **off by default** (`native_runtime_enabled`); `wasm` is
  wasmtime-sandboxed; `raw` is service-only + local consent.
- Running a peer's work requires consent.

## 4. Treat all untrusted input defensively
- **Path sanitizers** must reject `..` **and** drive letters / anchors (`:`); join
  with `resolve()` + `relative_to`/`parents`. Use `utils.text.safe_extractall` for
  any network-sourced archive (rejects traversal members).
- **Size caps** everywhere: the global `BodySizeLimitMiddleware` bounds request
  bodies; per-op limits (e.g. `receive_chunk` bounds `chunk_idx` to the agreed
  `chunk_count`) prevent resource exhaustion.
- **Outbound fetches** (cloud connector) block private/loopback/link-local/metadata
  IPs and re-check every redirect hop (SSRF guard).
- Parsers must stay **total** on garbage (return/raise cleanly, never crash the
  receive loop). The fuzz suite (`tests/test_fuzz_security.py`) enforces this.

## 5. Anti-replay & freshness
Signed state-changes that can be replayed must be monotonic — e.g.
`apply_grant_update` rejects an update whose `decided_at` is older than the stored
one.

## 6. Backups & migrations stay forward-compatible
DB migrations are **additive + idempotent** (`ADD COLUMN`/new tables), bump
`SCHEMA_VERSION`, and old backups must restore on newer nodes (settings merge over
defaults). Restore refuses a backup from a **newer** schema. A genuinely
destructive release sets `breaking: true` in the release manifest.

## 7. Updates are signed
Auto-update verifies a root→delegation-cert→release-key chain and the binary hash
(`security/app_update.py`). Never add an unsigned code-fetch path.

---

## Auth dependencies cheat-sheet
- `verify_local_auth` — management API: token + non-spoofable private-network check.
- `verify_trusted_peer` — peer protocol: per-peer secret `my_auth_token`.
- In-body signed statements — for unauthenticated `/peer/*` routes (service/DM/join),
  where the signature *is* the auth.
