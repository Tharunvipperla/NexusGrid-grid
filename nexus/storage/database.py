"""Async SQLAlchemy engine, session factory, and migrations.

Extracted from node_modified.py:

* engine creation — lines 258-262
* table + column migration — lines 6009-6024

Design notes
------------
The engine is **lazily bound** to a port via :func:`init_db`. Importing the
module does no I/O and does not create a database file. The app's lifespan
handler calls ``await init_db(port)`` exactly once at startup; thereafter any
subpackage can call :func:`get_session` without caring about setup order.

Why lazy? Tests need to point the engine at an in-memory database, and the
PyInstaller bundler imports modules at build time where no port is available.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from nexus.core.paths import BASE_DIR
from nexus.storage.models import Base

_log = logging.getLogger("nexus.storage")


_engine: Optional[AsyncEngine] = None
_session_factory: Optional[sessionmaker] = None
_current_db_url: str = ""


def _default_url_for_port(port: int) -> str:
    """Return the default SQLite URL used by the original implementation for a given port."""
    return f"sqlite+aiosqlite:///{(BASE_DIR / f'nexus_mod_{int(port)}.db').as_posix()}"


async def init_db(port: int, *, url: str | None = None) -> None:
    """Create (or open) the database, run migrations, cache the engine.

    Safe to call multiple times: if the engine is already bound to the same
    URL, the second call is a no-op. Calling with a different URL disposes
    the existing engine first — useful for tests that swap in an
    in-memory database.
    """
    global _engine, _session_factory, _current_db_url

    db_url = url or _default_url_for_port(port)
    if _engine is not None and db_url == _current_db_url:
        return

    if _engine is not None:
        await _engine.dispose()

    _engine = create_async_engine(db_url, echo=False)
    _session_factory = sessionmaker(
        _engine, class_=AsyncSession, expire_on_commit=False
    )
    _current_db_url = db_url

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate_schema(conn)
        # SQLite serializes writers by default, which deadlocks
        # nested sessions (e.g. write_audit_event opened from inside
        # relay_state.transition while the caller's transaction is still
        # holding the binding row). WAL relaxes that to "one writer at a
        # time" without locking out concurrent readers — fine for our
        # single-process node. Skipped for non-SQLite backends.
        if db_url.startswith("sqlite"):
            try:
                from sqlalchemy import text
                await conn.execute(text("PRAGMA journal_mode=WAL"))
                # 5 s of patience before SQLITE_BUSY — covers the brief
                # window where one session holds the binding row while
                # write_audit_event opens a sibling session for the
                # transition log.
                await conn.execute(text("PRAGMA busy_timeout=5000"))
            except Exception:
                _log.debug("WAL/busy_timeout not available", exc_info=True)

    _log.debug("database initialised at %s", db_url)


async def _migrate_schema(conn) -> None:
    """Idempotent, best-effort forward migrations.

    Each statement is wrapped in its own try/except because SQLite lacks
    ``IF NOT EXISTS`` for ``ADD COLUMN``. Re-running a migration on a
    schema that already contains the target column raises — we swallow the
    error and move on.
    """
    statements = (
        "ALTER TABLE peers ADD COLUMN display_name TEXT DEFAULT ''",
        "ALTER TABLE peers ADD COLUMN resolved_ip TEXT DEFAULT ''",
        "ALTER TABLE peers ADD COLUMN cert_fingerprint TEXT",
        "ALTER TABLE peers ADD COLUMN benchmark_score REAL",
        "ALTER TABLE peers ADD COLUMN benchmark_at TEXT",
        # External-cloud eviction tier columns.
        "ALTER TABLE foreign_storage_deposits ADD COLUMN cloud_provider TEXT DEFAULT ''",
        "ALTER TABLE foreign_storage_deposits ADD COLUMN cloud_dest TEXT DEFAULT ''",
        "ALTER TABLE foreign_storage_deposits ADD COLUMN cloud_object_id TEXT DEFAULT ''",
        "ALTER TABLE foreign_storage_deposits ADD COLUMN cloud_uploaded_at INTEGER DEFAULT 0",
        # Per-deposit host-view grant timestamp.
        "ALTER TABLE foreign_storage_deposits ADD COLUMN host_view_granted_at INTEGER DEFAULT 0",
        # Follow-up: host-side path of the decrypted plaintext
        # directory (populated when the host first clicks Open on a
        # granted deposit). Survives depositor revoke; only the host
        # can delete via /foreign_storage/delete_view_decrypted.
        "ALTER TABLE foreign_storage_deposits ADD COLUMN host_view_decrypted_dir TEXT DEFAULT ''",
        # P8: pause/resume transit bookkeeping.
        "ALTER TABLE foreign_storage_deposits ADD COLUMN transferred_chunks INTEGER DEFAULT 0",
        "ALTER TABLE foreign_storage_deposits ADD COLUMN retry_count INTEGER DEFAULT 0",
        "ALTER TABLE foreign_storage_deposits ADD COLUMN last_progress_at TEXT DEFAULT ''",
        "ALTER TABLE foreign_storage_deposits ADD COLUMN pause_reason TEXT DEFAULT ''",
        # Schema 9: filename surfaces in My Deposits / Hosted / Histories.
        # The id stays the unique key, but humans recognise file names.
        "ALTER TABLE foreign_storage_deposits ADD COLUMN filename TEXT DEFAULT ''",
        # Schema 9: host-configured total countdown (in days) between
        # Evict click and disk purge. Stamped at Evict-click time and
        # mirrored onto the depositor row from the eviction frame so
        # both sides render the same countdown.
        "ALTER TABLE foreign_storage_deposits ADD COLUMN eviction_total_days INTEGER DEFAULT 0",
        # Per-deposit sender window override (0 = node default setting).
        "ALTER TABLE foreign_storage_deposits ADD COLUMN window_chunks INTEGER DEFAULT 0",
        # Per-deposit transit-tuning overrides (0 = node default setting).
        "ALTER TABLE foreign_storage_deposits ADD COLUMN ack_timeout_sec INTEGER DEFAULT 0",
        "ALTER TABLE foreign_storage_deposits ADD COLUMN transit_retries INTEGER DEFAULT 0",
        "ALTER TABLE foreign_storage_deposits ADD COLUMN offer_timeout_sec INTEGER DEFAULT 0",
        # Schema 11 : per-group join policy. Default 'open'
        # preserves the Wave-15 behavior for already-existing groups.
        "ALTER TABLE groups ADD COLUMN privacy_mode TEXT DEFAULT 'open'",
        # Admin-side delivery marker for the
        # /peer/group/join_decision push. Empty string = not yet
        # delivered; the scheduler retry loop watches for these.
        "ALTER TABLE group_pending_join_requests ADD COLUMN delivered_at TEXT DEFAULT ''",
        # Schema 12 (post-ship): self-declared joiner display name
        # mirrored onto the admin's GroupMember row so the Members tab can
        # render names instead of bare pubkeys.
        "ALTER TABLE group_members ADD COLUMN display_name TEXT DEFAULT ''",
        "ALTER TABLE group_pending_join_requests ADD COLUMN display_name TEXT DEFAULT ''",
        # Schema 13 (post-ship): roster-sync address columns. The
        # founder_address sits on the joiner's local Group row so the
        # joiner can call /peer/group/roster on the admin; peer_address
        # sits on every GroupMember row so any member can reach peers
        # directly without going through the admin.
        "ALTER TABLE groups ADD COLUMN founder_address TEXT DEFAULT ''",
        "ALTER TABLE group_members ADD COLUMN peer_address TEXT DEFAULT ''",
        # Schema 14 : ECIES envelope columns for the lazily-
        # minted group symmetric key. ``group_symkey_enc`` is each
        # node's own self-sealed (founder) or recipient-sealed (joiner)
        # copy of the symkey. ``member_x25519_pub`` is the X25519
        # pubkey each member advertises so others can target them with
        # ECIES envelopes.
        "ALTER TABLE groups ADD COLUMN group_symkey_enc BLOB",
        "ALTER TABLE group_members ADD COLUMN member_x25519_pub TEXT DEFAULT ''",
        # Capture the joiner's X25519 pubkey at pending-request time so
        # the private-mode approve flow can ECIES-seal the symkey
        # without re-prompting the joiner.
        "ALTER TABLE group_pending_join_requests ADD COLUMN joiner_x25519_pub TEXT DEFAULT ''",
        # Schema 15 : the node UUID each member advertises so
        # group fan-out can route a frame over the generic WS relay
        # (relay_http_request keys on node_id) when direct HTTP to the
        # member's peer_address fails — i.e. cross-region / behind NAT.
        "ALTER TABLE group_members ADD COLUMN node_id TEXT DEFAULT ''",
        "ALTER TABLE group_pending_join_requests ADD COLUMN joiner_node_id TEXT DEFAULT ''",
        # Per-binding last-observed HTTP probe RTT in milliseconds.
        # Populated by nexus.runtime.relay_latency's periodic probe loop.
        "ALTER TABLE group_relay_bindings ADD COLUMN last_rtt_ms INTEGER",
        # Peer's advertised relay-pool URL set (JSON list).
        # Exchanged at pair-handshake; used by peer_http_post's relay
        # fallback to restrict candidates to a shared relay.
        "ALTER TABLE peers ADD COLUMN peer_relay_urls TEXT DEFAULT '[]'",
        # Per-connection local pause flags. Backfill 0 so
        # existing rows are not paused on first boot after upgrade.
        "ALTER TABLE groups ADD COLUMN paused INTEGER DEFAULT 0",
        "ALTER TABLE peers ADD COLUMN paused INTEGER DEFAULT 0",
        # Peer's grid_key for the transient-WS last-resort
        # path when our subscribed relay pool has no overlap with the
        # peer's. NULL on legacy rows -> fallback skipped.
        "ALTER TABLE peers ADD COLUMN peer_grid_key TEXT DEFAULT ''",
        # High-watermark for catch-up. Empty string on legacy
        # rows -> first catchup gets the full retained window.
        "ALTER TABLE groups ADD COLUMN last_catchup_at TEXT DEFAULT ''",
        # Hard cap on group membership. 0 = unlimited.
        "ALTER TABLE groups ADD COLUMN max_members INTEGER DEFAULT 0",
        # Permanent "follow-link" marker on PairInvite.
        "ALTER TABLE pair_invites ADD COLUMN is_permanent INTEGER DEFAULT 0",
        # Relay-governance columns on Group + GroupRelayBinding.
        "ALTER TABLE groups ADD COLUMN relay_code_fingerprint TEXT DEFAULT ''",
        "ALTER TABLE group_relay_bindings ADD COLUMN state TEXT DEFAULT 'online'",
        "ALTER TABLE group_relay_bindings ADD COLUMN last_state_change_at TEXT DEFAULT ''",
        "ALTER TABLE group_relay_bindings ADD COLUMN consecutive_probe_failures INTEGER DEFAULT 0",
        "ALTER TABLE group_relay_bindings ADD COLUMN host_node_id TEXT DEFAULT ''",
        "ALTER TABLE group_relay_bindings ADD COLUMN frame_counts_24h TEXT DEFAULT '[]'",
        "ALTER TABLE group_relay_bindings ADD COLUMN frame_counts_24h_updated_at TEXT DEFAULT ''",
        # Operator-adjustable relay metadata.
        "ALTER TABLE group_relay_bindings ADD COLUMN label TEXT DEFAULT ''",
        "ALTER TABLE group_relay_bindings ADD COLUMN region TEXT DEFAULT ''",
        "ALTER TABLE group_relay_bindings ADD COLUMN priority INTEGER DEFAULT 0",
        # Consensual relay content-share authorization.
        "ALTER TABLE group_relay_bindings ADD COLUMN content_share INTEGER DEFAULT 0",
        "ALTER TABLE group_relay_bindings ADD COLUMN content_share_by TEXT DEFAULT ''",
        "ALTER TABLE group_relay_bindings ADD COLUMN content_share_at TEXT DEFAULT ''",
        # Chat moderation flag on membership.
        "ALTER TABLE group_members ADD COLUMN muted INTEGER DEFAULT 0",
        # Peer X25519 pubkey for E2E-encrypted DMs.
        "ALTER TABLE peers ADD COLUMN peer_enc_pub TEXT DEFAULT ''",
        # Reply/quote columns on messages.
        "ALTER TABLE group_messages ADD COLUMN reply_to TEXT DEFAULT ''",
        "ALTER TABLE group_messages ADD COLUMN reply_snippet TEXT DEFAULT ''",
        "ALTER TABLE group_messages ADD COLUMN reply_sender TEXT DEFAULT ''",
        "ALTER TABLE direct_messages ADD COLUMN reply_to TEXT DEFAULT ''",
        "ALTER TABLE direct_messages ADD COLUMN reply_snippet TEXT DEFAULT ''",
        "ALTER TABLE direct_messages ADD COLUMN reply_sender TEXT DEFAULT ''",
        # Attachment columns.
        "ALTER TABLE group_messages ADD COLUMN attach_kind TEXT DEFAULT ''",
        "ALTER TABLE group_messages ADD COLUMN attach_name TEXT DEFAULT ''",
        "ALTER TABLE group_messages ADD COLUMN attach_mime TEXT DEFAULT ''",
        "ALTER TABLE group_messages ADD COLUMN attach_size INTEGER DEFAULT 0",
        "ALTER TABLE group_messages ADD COLUMN attach_data TEXT DEFAULT ''",
        "ALTER TABLE group_messages ADD COLUMN attach_ref TEXT DEFAULT ''",
        "ALTER TABLE direct_messages ADD COLUMN attach_kind TEXT DEFAULT ''",
        "ALTER TABLE direct_messages ADD COLUMN attach_name TEXT DEFAULT ''",
        "ALTER TABLE direct_messages ADD COLUMN attach_mime TEXT DEFAULT ''",
        "ALTER TABLE direct_messages ADD COLUMN attach_size INTEGER DEFAULT 0",
        "ALTER TABLE direct_messages ADD COLUMN attach_data TEXT DEFAULT ''",
        "ALTER TABLE direct_messages ADD COLUMN attach_ref TEXT DEFAULT ''",
        # Member liveness presence.
        "ALTER TABLE group_members ADD COLUMN last_seen_at TEXT DEFAULT ''",
        # Offline DM outbox delivery flag.
        "ALTER TABLE direct_messages ADD COLUMN delivered INTEGER DEFAULT 0",
        # Group profile picture (small data URL).
        "ALTER TABLE groups ADD COLUMN avatar TEXT DEFAULT ''",
        # Group kind — "full" (Groups screen) vs "chat"
        # (lightweight message group surfaced in Messages).
        "ALTER TABLE groups ADD COLUMN kind TEXT DEFAULT 'full'",
        # Security (F-005/F-007, schema 14): peer's Ed25519 group pubkey, so
        # trust + signed-message verification bind to the crypto identity, not
        # the gossiped UUID.
        "ALTER TABLE peers ADD COLUMN peer_group_pubkey TEXT DEFAULT ''",
    )
    for stmt in statements:
        try:
            await conn.execute(text(stmt))
        except Exception:
            pass


def get_engine() -> AsyncEngine:
    """Return the live async engine. Raises if :func:`init_db` has not run."""
    if _engine is None:
        raise RuntimeError(
            "storage.init_db(port) must be called before get_engine()"
        )
    return _engine


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Async context manager yielding a session bound to the live engine.

    ::

        async with get_session() as db:
            peers = (await db.execute(select(Peer))).scalars().all()

    Commits are explicit — nothing auto-flushes on exit.
    """
    if _session_factory is None:
        raise RuntimeError(
            "storage.init_db(port) must be called before get_session()"
        )
    async with _session_factory() as session:
        yield session


async def dispose() -> None:
    """Dispose the engine. Tests call this; production does not."""
    global _engine, _session_factory, _current_db_url
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    _current_db_url = ""
