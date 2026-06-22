"""SQLAlchemy ORM models.

Extracted from Phase-1/node_modified.py (lines 204-256).

The schema intentionally stays flat and denormalized:

* ``TaskRecord`` carries the task payload and log blob as deferred columns so
  the common "list recent tasks" path does not drag multi-megabyte blobs
  through the session.
* ``Peer`` is keyed by either an IP:port string *or* a ``nexus_<uuid>``
  identifier depending on how the peer was discovered. ``resolved_ip`` stores
  the last seen real IP so that restarts can rebuild the UUID→IP mapping
  without waiting for a beacon. See ``nexus.core.identity``.
* ``AuditEvent`` and ``PresenceEvent`` are append-only; no row is ever updated
  after insert. Retention pruning happens in a background task
  (``nexus.telemetry.audit``).

When adding a new column, always:

1. Extend the model here.
2. Add an idempotent ``ALTER TABLE`` in ``database._migrate_schema`` so older
   deployments pick up the change on next startup.
3. Bump :data:`SCHEMA_VERSION`.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, Float, Integer, LargeBinary, String, Text
from sqlalchemy.orm import declarative_base, deferred

Base = declarative_base()

SCHEMA_VERSION = 14
"""Incremented whenever the canonical schema changes."""


class TaskRecord(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True)
    parent_id = Column(String, index=True)
    status = Column(String, default="queued")
    depends_on = Column(String, default="")
    env_vars = Column(String, default="{}")
    worker = Column(String, nullable=True)
    logs = Column(Text, default="")
    payload = deferred(Column(LargeBinary, nullable=False))
    checkpoint_payload = deferred(Column(LargeBinary, nullable=True))


class Peer(Base):
    __tablename__ = "peers"

    ip = Column(String, primary_key=True)
    status = Column(String, default="pending_out")
    role = Column(String, default="worker")
    my_auth_token = Column(String, nullable=True)
    their_auth_token = Column(String, nullable=True)
    signing_key = Column(String, nullable=True)
    display_name = Column(String, default="")
    resolved_ip = Column(String, default="")
    cert_fingerprint = Column(String, nullable=True)
    benchmark_score = Column(Float, nullable=True)
    benchmark_at = Column(String, nullable=True)
    # Peer's advertised relay-pool URL set (JSON list of
    # strings). Exchanged at pair-handshake time. Used by peer_http_post's
    # relay-WS fallback to restrict the candidate set to relays both
    # ends are subscribed to — eliminates the "lowest-RTT relay isn't
    # on the target's pool" silent-drop case for cross-group P2P RPC.
    peer_relay_urls = Column(Text, default="[]", server_default="[]")
    # Local pause state. When True, peer_http_post short-
    # circuits to 503 so we stop sending heartbeats / RPC to this peer.
    # Inbound from them is still accepted (deferred). Local-only.
    paused = Column(Integer, default=0, server_default="0")
    # Peer's X25519 public key (hex, derived from their
    # Ed25519 group identity) for sealing end-to-end-encrypted DMs. Empty
    # until fetched lazily on first DM. Legacy/old peers stay empty ->
    # plaintext fallback.
    peer_enc_pub = Column(String, default="", server_default="")
    # Peer's relay grid_key, shared privately at pair-accept
    # time. Used by the transient-WS last-resort path in peer_http_post
    # when our subscribed pool has zero overlap with peer_relay_urls.
    # NULL on pre-W36.G rows — the fallback is just skipped in that
    # case, preserving the existing 503 behavior.
    peer_grid_key = Column(String, default="", server_default="")
    # Security (F-005/F-007): the peer's Ed25519 group pubkey (hex), recorded
    # when we learn it (pairing / profile fetch). Lets us bind trust + verify
    # signed peer messages to the cryptographic identity instead of the gossiped
    # node UUID. Empty until learned.
    peer_group_pubkey = Column(String, default="", server_default="")


class LocalConfigRecord(Base):
    __tablename__ = "local_config"

    id = Column(String, primary_key=True)
    config_json = Column(Text, default="{}")


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id = Column(String, primary_key=True)
    ts = Column(String, index=True)
    action = Column(String, index=True)
    actor = Column(String, index=True)
    task_id = Column(String, index=True)
    severity = Column(String, default="info")
    details = Column(Text, default="")


class PresenceEvent(Base):
    __tablename__ = "presence_events"

    id = Column(String, primary_key=True)
    peer_ip = Column(String, index=True)
    status = Column(String)  # "online" or "offline"
    source = Column(String)  # "ws", "timeout", "relay", "udp"
    ts = Column(String, index=True)


class ForeignStorageDeposit(Base):
    """A single encrypted deposit a peer holds (or owns) here.

    Rows live on both sides:
      * On the host: ``role='host'``, the ciphertext lives on disk.
      * On the depositor: ``role='depositor'``, only metadata is kept.
    """

    __tablename__ = "foreign_storage_deposits"

    deposit_id = Column(String, primary_key=True)
    role = Column(String, default="host")  # 'host' | 'depositor'
    depositor_uuid = Column(String, index=True)
    host_uuid = Column(String, index=True)
    status = Column(String, default="offered")
    # Lifecycle states: offered | accepted | transferring | stored |
    # eviction_requested | in_db_grace | purged | withdrawn
    total_bytes = Column(Integer, default=0)
    chunk_count = Column(Integer, default=0)
    transport = Column(String, default="stream")  # 'stream' | 'cloud_url'
    cloud_url = Column(String, default="")
    salt = Column(LargeBinary, nullable=True)
    password_hint = Column(String, default="")
    ttl_days = Column(Integer, default=30)
    created_at = Column(String, default="")
    ttl_at = Column(String, default="")
    eviction_requested_at = Column(String, default="")
    evicted_at = Column(String, default="")
    db_grace_at = Column(String, default="")
    purged_at = Column(String, default="")
    depositor_signature = Column(String, default="")
    host_signature = Column(String, default="")
    encrypted_manifest = deferred(Column(LargeBinary, nullable=True))
    # External-cloud eviction tier.
    cloud_provider = Column(String, default="")
    cloud_dest = Column(String, default="")
    cloud_object_id = Column(String, default="")
    cloud_uploaded_at = Column(Integer, default=0)
    # Per-deposit host-view grant. Set on the depositor row when
    # the user shares viewing rights; set on the host row when the host
    # accepts and caches the AES key. Cleared on revoke.
    host_view_granted_at = Column(Integer, default=0)
    # Follow-up: once the host clicks Open on a granted deposit,
    # the chunks are decrypted to plaintext on disk under this directory.
    # Stored as an absolute path. Persistent — depositor "revoke" only
    # drops the host's RAM key, it does NOT remove this directory. The
    # host explicitly deletes via /foreign_storage/delete_view_decrypted.
    host_view_decrypted_dir = Column(String, default="")
    # P8: pause/resume bookkeeping. ``transferred_chunks`` is the highest
    # contiguous chunk_idx the host has acked (batched-persisted every 16
    # acks or 2 s to keep write-amp bounded). ``retry_count`` caps
    # automatic resumes at fs_transit_max_retries before flipping to
    # failed_in_transit. ``last_progress_at`` lets the lifecycle pass
    # decide a row is "stuck". ``pause_reason`` classifies the pause
    # (host_shutdown | depositor_shutdown | send_failed | silent) so the
    # retry policy can wait on the right signal.
    transferred_chunks = Column(Integer, default=0)
    retry_count = Column(Integer, default=0)
    last_progress_at = Column(String, default="")
    pause_reason = Column(String, default="")
    # Schema 9: human-readable filename + host-configured evict window.
    filename = Column(String, default="")
    eviction_total_days = Column(Integer, default=0)
    # Per-deposit sender window override (in-flight chunks, 2-128).
    # 0 = use the node's storage_window_chunks setting at pump time. Set
    # by the depositor on creation; the pump reads it on every (re)send,
    # so resumes keep the same window.
    window_chunks = Column(Integer, default=0)
    # Per-deposit transit-tuning overrides, same 0-means-node-default
    # convention. All depositor-side: chunk-ack wait, auto-resume cap,
    # and fan-out offer timeout. Host-side policies (abandoned-chunk TTL)
    # deliberately stay the host's own setting.
    ack_timeout_sec = Column(Integer, default=0)
    transit_retries = Column(Integer, default=0)
    offer_timeout_sec = Column(Integer, default=0)


class CloudCredential(Base):
    """Depositor-side encrypted cloud-provider credentials.

    The ``encrypted_blob`` is wrapped with
    :func:`nexus.security.cred_crypto.wrap_credential_blob` (AES-256-GCM
    keyed off this node's ``.nexus_secret``). Only the depositor ever
    persists rows here; the host stores nothing about the credential.
    """

    __tablename__ = "cloud_credentials"

    id = Column(String, primary_key=True)
    provider = Column(String, index=True)  # 'gdrive' | 's3' | 'r2' | 'b2'
    label = Column(String, default="")
    encrypted_blob = deferred(Column(LargeBinary, nullable=False))
    default_folder = Column(String, default="")
    created_at = Column(String, default="")
    last_used_at = Column(String, default="")


class Secret(Base):
    """C4: node-local secret vault entry.

    ``encrypted_blob`` is wrapped at rest with
    :func:`nexus.security.cred_crypto.wrap_credential_blob` (AES-256-GCM
    keyed off this node's ``.nexus_secret``). The plaintext value is never
    returned over the API; task/service specs reference a secret as
    ``secret://NAME`` and resolution to plaintext happens only at execution
    time on the node that runs the work.
    """

    __tablename__ = "secrets"

    name = Column(String, primary_key=True)  # reference key, e.g. OPENAI_API_KEY
    encrypted_blob = deferred(Column(LargeBinary, nullable=False))
    description = Column(String, default="")
    created_at = Column(String, default="")
    updated_at = Column(String, default="")
    last_used_at = Column(String, default="")


class ForeignStorageDBGrace(Base):
    """Encrypted bytes parked in DB during the 2-day grace window."""

    __tablename__ = "foreign_storage_db_grace"

    deposit_id = Column(String, primary_key=True)
    encrypted_blob = deferred(Column(LargeBinary, nullable=False))
    expires_at = Column(String, index=True)


# ---- Groups + IAM core -----------------------------------------
#
# A Group is the unit of organization (Discord-server-shaped). Members hold
# roles; roles bundle permissions; grants are signed envelopes proving a
# Member's role assignment at a point in time. lands the schema +
# Crypto + APIs; multi-node state replication is.


class Group(Base):
    """A WaaS group (Discord-server-shaped unit of organization)."""

    __tablename__ = "groups"

    id = Column(String, primary_key=True)
    name = Column(String, default="")
    founder_pubkey = Column(String, index=True)
    created_at = Column(String, default="")
    # Soft-delete timestamp. Empty string means active. Authorised by a
    # founder/admin op; preserved so the same field carries forward to
    # 's replicated op log.
    deleted_at = Column(String, default="")
    # (16.1): join policy. ``open`` (default) = token + IP is
    # sufficient and the joiner gets a grant immediately. ``private`` =
    # the join enters an admin pending queue and a slot is only
    # consumed on approval. Editable later by founder/admin.
    # ``server_default`` so raw SQL inserts (including the legacy-DB
    # ALTER TABLE migration) backfill existing rows correctly.
    privacy_mode = Column(String, default="open", server_default="open")
    # Group profile picture — a small ``data:image/...;base64,``
    # URL (≤64 KB), set by role:assign holders and synced to members via
    # the durable ``group.meta`` frame. Empty = letter avatar in the UI.
    avatar = Column(Text, default="", server_default="")
    # "full" = a regular WaaS group; "chat" = a lightweight
    # message group created from the Messages screen (same machinery —
    # E2E frames, membership, invites — different UI surface).
    kind = Column(String, default="full", server_default="full")
    # (post-ship): where to reach the founder/admin node so a
    # joiner can pull roster updates. Empty on the founder's own row
    # (they don't ping themselves). ``host:port`` shape.
    founder_address = Column(String, default="", server_default="")
    # This node's copy of the group symmetric key, ECIES-sealed
    # to its own X25519 pubkey. NULL = not yet minted (no members beyond
    # the founder, or this node hasn't received it yet). Decrypted on
    # demand via :func:`nexus.security.group_ecies.ecies_open`.
    group_symkey_enc = Column(LargeBinary, nullable=True)
    # Local pause state. When True, this node skips outbound
    # publish_frame to this group AND drops inbound frames for it (looks
    # offline to other members). Local-only; not replicated. Idempotent
    # ALTER TABLE migration backfills existing rows with 0.
    paused = Column(Integer, default=0, server_default="0")
    # High-watermark ISO timestamp of the last frame this node
    # caught up via /peer/group/catchup. Empty = never; catchup endpoint
    # treats that as "give me the full retained window".
    last_catchup_at = Column(String, default="", server_default="")
    # Hard cap on group membership. 0 / NULL = unlimited.
    # Checked at join time independent of which invite was used —
    # prevents accidentally over-growing a group when many invite
    # links are in circulation.
    max_members = Column(Integer, default=0, server_default="0")
    # Frozen relay code fingerprint. Empty = unset (any code
    # accepted, first relay bind doesn't auto-freeze). When set, every
    # registering relay's code_fingerprint must match or the bind is
    # rejected. Founder can change at will; admins can only propose
    # via GroupRelayCodeprintProposal (founder approves).
    relay_code_fingerprint = Column(
        String, default="", server_default=""
    )


class GroupFrameLog(Base):
    """Rolling per-group frame log kept on every node that
    sees frames pass through it (publishers + relay hosts + appliers).

    Used as the source for ``/peer/group/catchup``: a member who was
    offline when a frame went out asks an admin/peer for everything
    since their last ``Group.last_catchup_at``; the admin replays the
    sealed envelopes and the member re-runs ``dispatch_inbound_frame``
    on each. Idempotent — ``frame_id`` PK + the existing
    ``FrameDedupeCache`` skip-on-replay handles the reapply path.

    Retention: rolling 14-day window pruned by the background sweep.
    Storage cost is bounded; on a typical group ~10 frames/day × 100
    bytes ≈ 14 KB per group per fortnight.
    """

    __tablename__ = "group_frame_log"

    group_id = Column(String, primary_key=True, index=True)
    frame_id = Column(String, primary_key=True)
    envelope_json = Column(Text, default="")
    frame_type = Column(String, default="")
    captured_at = Column(String, default="", index=True)


class GroupMember(Base):
    """Membership row. PK = (group_id, pubkey)."""

    __tablename__ = "group_members"

    group_id = Column(String, primary_key=True, index=True)
    pubkey = Column(String, primary_key=True, index=True)
    joined_at = Column(String, default="")
    # Last time an admin re-signed this member's grant (heartbeat lifecycle
    # — see step 15.6). Empty string means never re-signed since
    # initial issuance.
    last_heartbeat_at = Column(String, default="")
    # (post-ship fix): the joiner's self-declared display name at
    # join time, mirrored onto the admin's row so the Members tab can
    # render names instead of bare pubkeys. ``server_default`` so the
    # ALTER TABLE migration backfills existing rows with the empty string.
    display_name = Column(String, default="", server_default="")
    # (post-ship): reachable ``host:port`` for this member so any
    # current member can pull the roster and reach peers directly. Empty
    # on the local node's own row (we don't ping ourselves).
    peer_address = Column(String, default="", server_default="")
    # This member's X25519 pubkey (hex). Used by other members
    # to ECIES-seal payloads (currently just the symkey envelope; future
    # waves will use it for per-member frames). Empty for legacy rows
    # Created before those members must re-handshake to get
    # a sealed copy of the symkey.
    member_x25519_pub = Column(String, default="", server_default="")
    # This member's node UUID. When direct HTTP to peer_address
    # fails, group fan-out re-routes the frame over the generic WS relay
    # (relay_http_request keys on node_id) — the cross-region path.
    # Empty for legacy rows + members reachable only by direct HTTP.
    node_id = Column(String, default="", server_default="")
    # Chat moderation. A muted member can't send group messages;
    # set by a holder of ``member:mute`` (founder/admin) and converged via
    # the ``chat.mute`` frame. 0 = can speak, 1 = muted.
    muted = Column(Integer, default=0, server_default="0")
    # Liveness presence. Updated to the sender's beacon timestamp
    # whenever a ``presence.beacon`` frame for this member arrives. The UI
    # renders an online dot if recent, else "offline N days" (capped at 30).
    # Distinct from ``last_heartbeat_at`` (grant-TTL lifecycle).
    last_seen_at = Column(String, default="", server_default="")


class GroupComputeStat(Base):
    """Per-member compute pool usage, shared group-wide.

    PK = (group_id, member_pubkey). Each node maintains its OWN row's counters
    (tasks it ran for the group vs. tasks it dispatched to the group) and
    broadcasts them via the ``compute.stats`` beacon; receivers upsert the
    sender's row (last-writer-wins) so every member sees the whole table.
    """

    __tablename__ = "group_compute_stats"

    group_id = Column(String, primary_key=True, index=True)
    member_pubkey = Column(String, primary_key=True, index=True)
    tasks_contributed = Column(Integer, default=0, server_default="0")
    tasks_consumed = Column(Integer, default=0, server_default="0")
    updated_at = Column(String, default="", server_default="")


class GroupComputeBucket(Base):
    """Time-bucketed pool-usage history for THIS node.

    Mirrors :class:`RelayTelemetryBucket`. PK =
    ``(group_id, member_pubkey, bucket_kind, bucket_start)``. ``group_id="*"``
    holds the node-global rollup across every group; ``member_pubkey`` is always
    the local node's group pubkey (this is our own history, not a shared table).

    Hour buckets roll up to days after 24h then weeks after 7d, and prune past
    ``LOCAL_SETTINGS["pool_telemetry_retention_days"]``.
    """

    __tablename__ = "group_compute_buckets"

    group_id = Column(String, primary_key=True, index=True)
    member_pubkey = Column(String, primary_key=True)
    bucket_kind = Column(String, primary_key=True)  # hour | day | week
    bucket_start = Column(String, primary_key=True)
    tasks_contributed = Column(Integer, default=0, server_default="0")
    tasks_consumed = Column(Integer, default=0, server_default="0")
    compute_secs_contributed = Column(Integer, default=0, server_default="0")
    compute_secs_consumed = Column(Integer, default=0, server_default="0")
    storage_bytes_hosted = Column(Integer, default=0, server_default="0")
    storage_bytes_used = Column(Integer, default=0, server_default="0")


class UsageReceipt(Base):
    """A counterparty-signed record of one resource exchange.

    PK = ``receipt_id``. The **consumer** signs the body (see
    :mod:`nexus.security.usage_receipt`), so a node cannot forge contribution to
    itself nor hide consumption it signed for. Pool-usage numbers are recomputed
    only from rows whose ``sig`` verifies against ``consumer_pubkey``. ``group_id``
    is empty for a 1:1 peer exchange. Auto-created by ``create_all``.
    """

    __tablename__ = "usage_receipts"

    receipt_id = Column(String, primary_key=True)
    group_id = Column(String, default="", server_default="", index=True)
    provider_pubkey = Column(String, default="", server_default="", index=True)
    consumer_pubkey = Column(String, default="", server_default="", index=True)
    kind = Column(String, default="", server_default="")  # compute | storage
    ref_id = Column(String, default="", server_default="")  # task/deposit id
    amount = Column(Integer, default=0, server_default="0")  # secs | bytes
    ts = Column(String, default="", server_default="")
    sig = Column(String, default="", server_default="")


class ServiceGrant(Base):
    """Access to a provider's advertised service for one consumer.

    PK = ``grant_id``. Held on both ends — the provider keeps the grants it
    issued (``provider_pubkey`` == self) and the consumer keeps the grants it
    holds (``consumer_pubkey`` == self). Status drives whether the data-plane
    tunnel (Phase B) is allowed to carry bytes. Auto-created by ``create_all``.
    """

    __tablename__ = "service_grants"

    grant_id = Column(String, primary_key=True)
    service_name = Column(String, default="", server_default="", index=True)
    provider_pubkey = Column(String, default="", server_default="", index=True)
    consumer_pubkey = Column(String, default="", server_default="", index=True)
    # node UUID of the counterparty (for addressing pushes / opening tunnels).
    provider_uuid = Column(String, default="", server_default="")
    consumer_uuid = Column(String, default="", server_default="")
    status = Column(String, default="pending", server_default="pending")  # pending|approved|denied|revoked
    access = Column(String, default="", server_default="")  # free|permission|paid (snapshot)
    created_at = Column(String, default="", server_default="")
    decided_at = Column(String, default="", server_default="")


class ServiceDbProvision(Base):
    """Per-grant DBaaS provisioning record (PROVIDER side only).

    When a consumer fetches credentials for an approved DB-kind service grant,
    the host's DB-provider adapter creates a dedicated per-consumer database +
    login and the result is recorded here keyed by ``grant_id`` — so the same
    connection is re-served idempotently and dropped again on revoke. Holds the
    generated password (host's own machine); never sent except to the
    grant's own authenticated consumer. Auto-created by ``create_all``.
    """

    __tablename__ = "service_db_provisions"

    grant_id = Column(String, primary_key=True)
    engine = Column(String, default="")
    kind = Column(String, default="")
    database = Column(String, default="")
    username = Column(String, default="")
    password = Column(String, default="")
    created_at = Column(String, default="")


class GroupMessage(Base):
    """A group chat message. PK = (group_id, msg_id).

    Bodies arrive sealed under the group symkey via ``publish_frame`` and
    are stored plaintext locally (the DB is the user's own device). Kept
    indefinitely; users delete individual messages (propagated by the
    ``chat.delete`` frame).
    """

    __tablename__ = "group_messages"

    group_id = Column(String, primary_key=True, index=True)
    msg_id = Column(String, primary_key=True)
    sender_pubkey = Column(String, default="", index=True)
    sender_name = Column(String, default="", server_default="")
    body = Column(Text, default="")
    sent_at = Column(String, default="", index=True)
    received_at = Column(String, default="")
    deleted = Column(Integer, default=0, server_default="0")
    # WhatsApp-style reply/quote. ``reply_to`` is the quoted
    # message's id; ``reply_snippet``/``reply_sender`` render the quote
    # even if the original isn't present locally.
    reply_to = Column(String, default="", server_default="")
    reply_snippet = Column(String, default="", server_default="")
    reply_sender = Column(String, default="", server_default="")
    # Attachments. ``attach_kind`` = "" | "inline" | "fs".
    # inline: base64 file rides ``attach_data`` (≤5 MB, sealed by the
    # frame). fs: ``attach_ref`` is a foreign-storage deposit id.
    attach_kind = Column(String, default="", server_default="")
    attach_name = Column(String, default="", server_default="")
    attach_mime = Column(String, default="", server_default="")
    attach_size = Column(Integer, default=0, server_default="0")
    attach_data = Column(Text, default="", server_default="")
    attach_ref = Column(String, default="", server_default="")


class DirectMessage(Base):
    """A 1:1 direct message with a paired peer.

    PK = ``msg_id`` (globally-unique uuid, so inbound dedupe is trivial).
    ``peer_uuid`` is the *other* party's node UUID — the conversation key,
    stable across the peer's IP changes. ``direction`` is ``out``/``in``.
    """

    __tablename__ = "direct_messages"

    msg_id = Column(String, primary_key=True)
    peer_uuid = Column(String, default="", index=True)
    direction = Column(String, default="out")
    sender_name = Column(String, default="")
    body = Column(Text, default="")
    sent_at = Column(String, default="", index=True)
    received_at = Column(String, default="")
    deleted = Column(Integer, default=0, server_default="0")
    # Outbound delivery state. 0 = not yet delivered (the recipient
    # was offline); a background outbox loop retries until it flips to 1.
    # Inbound rows leave it 0 — the loop only retries direction="out".
    delivered = Column(Integer, default=0, server_default="0")
    # Reply/quote.
    reply_to = Column(String, default="", server_default="")
    reply_snippet = Column(String, default="", server_default="")
    reply_sender = Column(String, default="", server_default="")
    # Attachments (see GroupMessage).
    attach_kind = Column(String, default="", server_default="")
    attach_name = Column(String, default="", server_default="")
    attach_mime = Column(String, default="", server_default="")
    attach_size = Column(Integer, default=0, server_default="0")
    attach_data = Column(Text, default="", server_default="")
    attach_ref = Column(String, default="", server_default="")


class GroupRole(Base):
    """Role definition inside a group. PK = (group_id, name).

    ``permissions_json`` is a JSON array of permission strings such as
    ``"group:read"`` or ``"service:use:postgres-prod"``. The set is open
    — admins can add user-defined ``service:use:<id>`` perms as new
    services are registered.
    """

    __tablename__ = "group_roles"

    group_id = Column(String, primary_key=True, index=True)
    name = Column(String, primary_key=True)
    permissions_json = Column(Text, default="[]")
    created_at = Column(String, default="")
    updated_at = Column(String, default="")


class GroupMemberRole(Base):
    """Role assignment. PK = (group_id, member_pubkey, role_name).

    Effective permissions for a member = union of permission sets of all
    roles they hold in the group.
    """

    __tablename__ = "group_member_roles"

    group_id = Column(String, primary_key=True, index=True)
    member_pubkey = Column(String, primary_key=True, index=True)
    role_name = Column(String, primary_key=True)
    assigned_by_pubkey = Column(String, default="")
    assigned_at = Column(String, default="")


class GroupGrant(Base):
    """Signed grant envelope.

    Issued by an admin's private key to a member, proving they hold the
    listed roles at issuance time. Verified via challenge-response (the
    member signs a fresh nonce with their private key when connecting).

    Short-TTL: ``expires_at`` is typically 24h after ``issued_at``; admin
    nodes re-sign on a 6h heartbeat to keep grants alive. Stop re-signing
    = effective revocation after TTL.
    """

    __tablename__ = "group_grants"

    id = Column(String, primary_key=True)
    group_id = Column(String, index=True)
    member_pubkey = Column(String, index=True)
    issued_by_pubkey = Column(String, default="")
    issued_at = Column(String, default="")
    expires_at = Column(String, index=True)
    nonce = Column(String, default="")
    signature = Column(LargeBinary, nullable=True)
    roles_json = Column(Text, default="[]")


class GroupInviteLink(Base):
    """Capacity-capped invite link.

    ``slots_filled`` increments only on admitted joins; pending requests
    do not count. ``active`` auto-flips to 0 when ``slots_filled ==
    slot_cap``. An admin can flip it back to 1 and/or raise the cap to
    re-open. Rotating the token kills the old one (``rotated_at`` is set
    on the dead row; a fresh row is inserted with a new token).
    """

    __tablename__ = "group_invite_links"

    token = Column(String, primary_key=True)
    group_id = Column(String, index=True)
    slot_cap = Column(Integer, default=0)
    slots_filled = Column(Integer, default=0)
    active = Column(Integer, default=1)
    created_by_pubkey = Column(String, default="")
    created_at = Column(String, default="")
    rotated_at = Column(String, default="")


class GroupInvitationOffer(Base):
    """(16.1): a targeted invitation pushed to a trusted peer.

    Same row is used by both sides — the founder stores it when minting
    + pushing, the recipient stores it when receiving. Distinguished by
    ``role`` (``sender`` / ``recipient``).

    Status lifecycle: ``pending`` → ``accepted`` (recipient joined) or
    ``rejected`` (recipient declined). A rejected row keeps the token
    alive on the founder's side so they can resend without re-minting.
    """

    __tablename__ = "group_invitation_offers"

    # Composite PK lets the same token coexist as sender + recipient
    # rows in one DB without collision.
    token = Column(String, primary_key=True)
    role = Column(String, primary_key=True)  # 'sender' | 'recipient'
    group_id = Column(String, index=True)
    group_name = Column(String, default="")
    founder_pubkey = Column(String, default="")
    founder_address = Column(String, default="")
    target_peer_label = Column(String, default="")
    status = Column(String, default="pending")  # pending | accepted | rejected
    created_at = Column(String, default="")
    responded_at = Column(String, default="")


class GroupPendingJoinRequest(Base):
    """(16.1): a pending join in a private-mode group.

    Created on the admin's node when a joiner submits an invite token
    against a private group. Slot is **not** consumed until an admin
    approves; on approve, the existing grant-issuance path runs and
    the row flips to ``approved``. Reject keeps the slot but flips
    the row to ``rejected``.
    """

    __tablename__ = "group_pending_join_requests"

    id = Column(String, primary_key=True)
    group_id = Column(String, index=True)
    joiner_pubkey = Column(String, index=True)
    joiner_address = Column(String, default="")
    invite_token = Column(String, index=True)
    message = Column(Text, default="")
    status = Column(String, default="pending")  # pending | approved | rejected
    created_at = Column(String, default="")
    decided_at = Column(String, default="")
    decided_by_pubkey = Column(String, default="")
    decision_reason = Column(Text, default="")
    # (16.4): empty = decision not yet delivered to the joiner.
    # The scheduler retry loop scans rows where status != 'pending' AND
    # delivered_at == '' AND created_at is recent (< 30 min) and tries
    # to push the decision to ``joiner_address``.
    delivered_at = Column(String, default="", server_default="")
    # (post-ship fix): joiner's self-declared display name,
    # stashed here so the approve path can pass it on to
    # GroupMember.display_name without the joiner having to resubmit.
    display_name = Column(String, default="", server_default="")
    # Joiner's X25519 pubkey, captured at request submission
    # so the founder can ECIES-seal the symkey at approval time even
    # though the original join_request body is long gone.
    joiner_x25519_pub = Column(String, default="", server_default="")
    # Joiner's node UUID, captured at request submission so the
    # approve flow can stamp it onto the new GroupMember row (used for
    # WS-relay fan-out routing).
    joiner_node_id = Column(String, default="", server_default="")


class GroupRelayBinding(Base):
    """Per-group relay binding (1..N relays per group).

    A group is reachable via every binding listed here. Members fan out
    publishes to all bindings; subscribers connect to all bindings and
    dedupe by ``frame_id`` (added in 's envelope).

    ``operator_pubkey`` is the member who registered the binding; once
    lands, they must hold ``relay:host`` in the group.

    Status:
      ``active``       — accepting traffic.
      ``unreachable``  — recent connect attempts failed; UI surfaces.
      ``retired``      — operator stepped down; not used for fan-out.
    """

    __tablename__ = "group_relay_bindings"

    group_id = Column(String, primary_key=True, index=True)
    relay_url = Column(String, primary_key=True)
    operator_pubkey = Column(String, default="")
    registered_at = Column(String, default="")
    last_seen_at = Column(String, default="")
    status = Column(String, default="active")
    # Last successfully-measured HTTP probe RTT in milliseconds.
    # NULL = never probed or last probe failed. Populated by the periodic
    # background probe in nexus.runtime.relay_latency.
    last_rtt_ms = Column(Integer, nullable=True)
    # Per-binding state machine for the relay UX.
    # ``starting`` → ``validating`` → ``syncing`` → ``online`` (steady)
    # On probe failure: ``online`` → ``offline`` → (auto-recovery)
    #                            → ``reconnecting`` → ``syncing`` → ``online``
    # ``retired`` is terminal — set when the operator's node is replaced
    # or by an explicit "stop hosting" call.
    state = Column(
        String, default="online", server_default="online"
    )
    last_state_change_at = Column(String, default="", server_default="")
    consecutive_probe_failures = Column(
        Integer, default=0, server_default="0"
    )
    # Node UUID of the member currently running this relay.
    # Empty for pasted external URLs that no group member operates.
    host_node_id = Column(String, default="", server_default="")
    # Hourly frame counters for the last 24 buckets.
    # JSON-encoded array of 24 ints (rotating ring keyed by hour-of-day).
    # ``frame_counts_24h_updated_at`` is the iso8601 of the last sample
    # write — used by the daily rollup sweeper to know which buckets
    # have rolled into the past.
    frame_counts_24h = Column(
        Text, default="[]", server_default="[]"
    )
    frame_counts_24h_updated_at = Column(
        String, default="", server_default=""
    )
    # Operator-adjustable relay metadata.
    #   ``label``    — human-friendly name shown instead of the raw URL.
    #   ``region``   — free-text locality hint (e.g. "us-east", "home-LAN").
    #   ``priority`` — fan-out preference; higher publishes first.
    label = Column(String, default="", server_default="")
    region = Column(String, default="", server_default="")
    priority = Column(Integer, default=0, server_default="0")
    # Consensual content-share. Relays are E2E-blind by default
    # (they only ever see AEAD-sealed frames). When the group authorizes a
    # specific relay to read content, the group symkey is released to it
    # only through the gated /relay_content_key path. ``content_share`` = 1
    # marks that authorization; ``content_share_by`` is the founder/admin
    # (relay:share_content holder) who made the call — a relay operator can
    # NOT self-authorize. Authorizing/revoking replicates to every member.
    content_share = Column(Integer, default=0, server_default="0")
    content_share_by = Column(String, default="", server_default="")
    content_share_at = Column(String, default="", server_default="")


class GroupRelayCodeprintProposal(Base):
    """Admin's proposed change to a group's relay code fingerprint.

    The founder can change ``Group.relay_code_fingerprint`` directly.
    Admins (non-founder) instead open a proposal row; the founder
    accepts via :http:post:`.../relays/code_fingerprint/accept/{id}`,
    at which point the new fingerprint takes effect and the row is
    deleted. Audit-logged on both ends.

    Status:
      ``pending``  — waiting on founder.
      ``accepted`` — applied (row pruned by the next sweep).
      ``rejected`` — founder declined.
    """

    __tablename__ = "group_relay_codeprint_proposals"

    id = Column(String, primary_key=True)
    group_id = Column(String, index=True)
    proposed_fingerprint = Column(String, default="")
    proposed_by_pubkey = Column(String, default="")
    proposed_at = Column(String, default="")
    status = Column(String, default="pending", server_default="pending")
    decided_at = Column(String, default="", server_default="")
    decided_by_pubkey = Column(String, default="", server_default="")


class GroupRelayCode(Base):
    """A group's canonical relay module source, sealed once into
    the channel by a founder/admin so members can *copy* the relay this
    group runs and host it themselves.

    A group whose relay runs custom code (a non-bundled ``nexus_relays/*``
    plugin) freezes its W41 ``Group.relay_code_fingerprint``. Publishing the
    matching source here gives every member a durable, offline-host-proof
    copy to obtain → import → run → bind (W63 validates the fingerprint at
    bind). One row per group; re-publishing overwrites it.

    Integrity: the apply handler recomputes ``fingerprint`` from ``source``
    and stores it ONLY if it equals the group's frozen fingerprint — a
    forged frame can't substitute different code under the group's nose.
    """

    __tablename__ = "group_relay_code"

    group_id = Column(String, primary_key=True, index=True)
    source = Column(Text, default="")
    fingerprint = Column(String, default="")
    published_by = Column(String, default="")
    published_at = Column(String, default="")


class RelayTelemetryBucket(Base):
    """On-disk archive for relay frame counters + state-change
    counts, retained per ``relay_telemetry_retention_days`` LOCAL_SETTING.

    Three resolutions stored side-by-side:

    * ``hour``: 24 rolling buckets, written by the 60s sampler.
    * ``day``:  rolled up from hour buckets after 24h have passed.
    * ``week``: rolled up from day buckets after 7d.

    The daily rollup sweeper collapses + prunes according to retention.
    Export endpoint streams ranges out as CSV / JSON for users who want
    a permanent archive before rolloff.
    """

    __tablename__ = "relay_telemetry_buckets"

    relay_url = Column(String, primary_key=True)
    bucket_kind = Column(String, primary_key=True)
    bucket_start = Column(String, primary_key=True)
    frame_count = Column(Integer, default=0, server_default="0")
    state_changes = Column(Integer, default=0, server_default="0")
    last_state = Column(String, default="", server_default="")


class GroupJoinInviteV2(Base):
    """Signed group-join invite tracking (per-link max_uses).

    Companion to ``PairInvite`` for peer pairs. Each row tracks an
    invite the founder has issued for *this* group, with the same
    signed-envelope security model: no grid_key in the link, single-
    or multi-use, time-bounded, revocable.

    ``status`` lifecycle:
      ``active``    — eligible.
      ``exhausted`` — ``used_count >= max_uses``.
      ``revoked``   — issuer canceled before any redemption.
      ``expired``   — past ``expires_at`` (status updated lazily).
    """

    __tablename__ = "group_join_invites_v2"

    invite_id = Column(String, primary_key=True)
    group_id = Column(String, default="", index=True)
    founder_pubkey = Column(String, default="")
    issued_at = Column(String, default="")
    expires_at = Column(String, default="")
    max_uses = Column(Integer, default=1)
    used_count = Column(Integer, default=0)
    status = Column(String, default="active")
    last_used_at = Column(String, default="")
    signed_blob = Column(Text, default="")


class PairInvite(Base):
    """Pair-invite token tracking.

    A row per invite this node has issued. The signed envelope is
    self-contained (verifiable from `signed_blob` alone), but this
    table lets the local UI list / revoke issued invites and lets the
    issuer's relay-server consult the local cache before forwarding
    a ``pair_invite_probe`` frame.

    ``status``:
      ``active``    — eligible for redemption.
      ``redeemed``  — accepted by issuer; pair established.
      ``rejected``  — issuer rejected the request; can't be replayed.
      ``revoked``   — issuer canceled before any redemption.
      ``expired``   — past ``expires_at`` (status updated lazily).
    """

    __tablename__ = "pair_invites"

    invite_id = Column(String, primary_key=True)
    issuer_pubkey = Column(String, default="")
    issued_at = Column(String, default="")
    expires_at = Column(String, default="")
    max_uses = Column(Integer, default=1)
    used_count = Column(Integer, default=0)
    status = Column(String, default="active")
    last_used_at = Column(String, default="")
    last_redeemer_pubkey = Column(String, default="")
    signed_blob = Column(Text, default="")
    # A single permanent "follow-link" row per node. Twitter-style
    # — anyone with the link can attempt to pair, accept/reject is the gate.
    # Per-redeemer rate-limit is enforced via PairAttempt (one attempt per
    # bob_pubkey across the link's lifetime).
    is_permanent = Column(Boolean, default=False)


class PairAttempt(Base):
    """Per-redeemer rate-limit ledger for permanent pair links.

    Each (invite_id, bob_pubkey) tuple may attempt redemption exactly once.
    Inserted when the issuer's node parks an inbound ``pair_invite_probe``
    request; the decision (accept/reject) is updated when the issuer
    clicks. Subsequent probes from the same ``bob_pubkey`` are
    auto-rejected without disturbing the issuer's UI.
    """

    __tablename__ = "pair_attempts"

    invite_id = Column(String, primary_key=True)
    bob_pubkey = Column(String, primary_key=True)
    first_seen_at = Column(String, default="")
    decided_at = Column(String, default="")
    decision = Column(String, default="pending")  # pending|accepted|rejected


__all__ = [
    "Base",
    "SCHEMA_VERSION",
    "TaskRecord",
    "Peer",
    "LocalConfigRecord",
    "AuditEvent",
    "PresenceEvent",
    "ForeignStorageDeposit",
    "ForeignStorageDBGrace",
    "CloudCredential",
    "Secret",
    "Group",
    "GroupMember",
    "GroupRole",
    "GroupMemberRole",
    "GroupGrant",
    "GroupInviteLink",
    "GroupInvitationOffer",
    "GroupPendingJoinRequest",
    "GroupRelayBinding",
    "GroupFrameLog",
    "GroupJoinInviteV2",
    "PairInvite",
    "PairAttempt",
]
