"""Wave 15.6 — admin heartbeat + TTL pruning.

Time-pinned tests (no real sleeps): every function under test accepts
an injectable ``now_iso`` so we can simulate a clock that has advanced
arbitrarily.
"""

from __future__ import annotations

import asyncio
import base64
import json

import pytest

from nexus.api.group_peer import GRANT_TTL_SECONDS
from nexus.runtime import group_heartbeat
from nexus.security import group_grant, group_keys, tokens
from nexus.storage import database, get_session
from nexus.storage.models import GroupGrant


# ---- fixtures -----------------------------------------------------------


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()

    db_path = tmp_path / "heartbeat.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    asyncio.run(database.init_db(0, url=url))
    yield url

    async def _teardown():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""

    asyncio.run(_teardown())
    tokens._reset_for_testing()
    group_keys._reset_for_testing()


def _run(coro):
    return asyncio.run(coro)


def _seed_grant(
    *,
    group_id: str,
    member_pubkey: str,
    admin_privkey: str,
    issued_at: str,
    expires_at: str,
    roles: tuple[str, ...] = ("member",),
    nonce: str = "deadbeef",
) -> bytes:
    """Insert a grant row via the same path the handshake would use."""
    from nexus.security.group_keys import get_local_group_pubkey

    admin_pub = get_local_group_pubkey()

    blob = group_grant.sign_grant(
        group_id=group_id,
        member_pubkey=member_pubkey,
        roles=roles,
        admin_privkey=admin_privkey,
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
    )

    async def _go():
        async with get_session() as s:
            s.add(
                GroupGrant(
                    id="grant-1",
                    group_id=group_id,
                    member_pubkey=member_pubkey,
                    issued_by_pubkey=admin_pub,
                    issued_at=issued_at,
                    expires_at=expires_at,
                    nonce=nonce,
                    signature=blob,
                    roles_json=json.dumps(list(roles)),
                )
            )
            await s.commit()

    _run(_go())
    return blob


# ---- refresh -----------------------------------------------------------


def test_refresh_extends_expires_at(isolated_db):
    admin_priv = group_keys.get_local_group_privkey()
    _, member_pub = group_grant.generate_keypair()
    issued = "2026-05-19T00:00:00+00:00"
    old_expires = "2026-05-20T00:00:00+00:00"

    _seed_grant(
        group_id="g1",
        member_pubkey=member_pub,
        admin_privkey=admin_priv,
        issued_at=issued,
        expires_at=old_expires,
    )

    # Heartbeat tick at T = 12h after issuance, well before expiry.
    async def _go():
        async with get_session() as s:
            n = await group_heartbeat.refresh_my_issued_grants(
                s, now_iso="2026-05-19T12:00:00+00:00", ttl_s=GRANT_TTL_SECONDS
            )
            await s.commit()
            return n

    refreshed = _run(_go())
    assert refreshed == 1

    # The row's expires_at advanced past the original 2026-05-20.
    async def _read():
        from sqlalchemy import select

        async with get_session() as s:
            row = (await s.execute(select(GroupGrant))).scalar_one()
            return row.expires_at, bytes(row.signature)

    new_expires, new_blob = _run(_read())
    assert new_expires > old_expires
    # The fresh blob verifies under the admin pubkey. Pin ``now_iso`` to
    # the refresh time so the assertion isn't a function of the calendar
    # day the test runs on (otherwise verify_grant's real-wall-clock
    # default rejects the 2026-05-20T12 expiry once the suite is run any
    # time after that date).
    admin_pub = group_keys.get_local_group_pubkey()
    grant = group_grant.verify_grant(
        new_blob,
        group_admin_pubkeys=[admin_pub],
        now_iso="2026-05-19T12:00:00+00:00",
    )
    assert grant is not None
    assert grant.expires_at == new_expires


def test_refresh_skips_already_expired_grants(isolated_db):
    """Once a grant is past its expires_at it cannot be refreshed — only
    purged. This prevents zombie grants coming back from the dead."""
    admin_priv = group_keys.get_local_group_privkey()
    _, member_pub = group_grant.generate_keypair()
    _seed_grant(
        group_id="g1",
        member_pubkey=member_pub,
        admin_privkey=admin_priv,
        issued_at="2026-05-01T00:00:00+00:00",
        expires_at="2026-05-02T00:00:00+00:00",
    )

    async def _go():
        async with get_session() as s:
            n = await group_heartbeat.refresh_my_issued_grants(
                s, now_iso="2026-05-19T00:00:00+00:00"
            )
            await s.commit()
            return n

    assert _run(_go()) == 0


def test_refresh_ignores_grants_issued_by_others(isolated_db):
    """The heartbeat must only touch grants this node issued. A grant
    from another admin (e.g. on a peer node, replicated later) is read-
    only here."""
    admin_priv = group_keys.get_local_group_privkey()
    me_pub = group_keys.get_local_group_pubkey()
    other_priv, other_pub = group_grant.generate_keypair()
    _, member_pub = group_grant.generate_keypair()

    # Hand-craft a row whose issued_by_pubkey is some OTHER admin.
    blob = group_grant.sign_grant(
        group_id="g1",
        member_pubkey=member_pub,
        roles=("member",),
        admin_privkey=other_priv,
        issued_at="2026-05-19T00:00:00+00:00",
        expires_at="2026-05-20T00:00:00+00:00",
        nonce="abcd",
    )

    async def _seed():
        async with get_session() as s:
            s.add(
                GroupGrant(
                    id="grant-other",
                    group_id="g1",
                    member_pubkey=member_pub,
                    issued_by_pubkey=other_pub,
                    issued_at="2026-05-19T00:00:00+00:00",
                    expires_at="2026-05-20T00:00:00+00:00",
                    nonce="abcd",
                    signature=blob,
                    roles_json='["member"]',
                )
            )
            await s.commit()

    _run(_seed())

    async def _go():
        async with get_session() as s:
            n = await group_heartbeat.refresh_my_issued_grants(
                s, now_iso="2026-05-19T12:00:00+00:00"
            )
            await s.commit()
            return n

    # Zero refreshed because the row's issuer is not us.
    assert _run(_go()) == 0


# ---- purge -------------------------------------------------------------


def test_purge_drops_expired_grants(isolated_db):
    admin_priv = group_keys.get_local_group_privkey()
    _, member_pub = group_grant.generate_keypair()
    _seed_grant(
        group_id="g1",
        member_pubkey=member_pub,
        admin_privkey=admin_priv,
        issued_at="2026-05-01T00:00:00+00:00",
        expires_at="2026-05-02T00:00:00+00:00",
    )

    async def _purge_and_count():
        from sqlalchemy import select

        async with get_session() as s:
            n = await group_heartbeat.purge_expired_grants(
                s, now_iso="2026-05-19T00:00:00+00:00"
            )
            await s.commit()
            remaining = (await s.execute(select(GroupGrant))).scalars().all()
            return n, len(remaining)

    deleted, remaining = _run(_purge_and_count())
    assert deleted == 1
    assert remaining == 0


def test_purge_leaves_unexpired_grants(isolated_db):
    admin_priv = group_keys.get_local_group_privkey()
    _, member_pub = group_grant.generate_keypair()
    _seed_grant(
        group_id="g1",
        member_pubkey=member_pub,
        admin_privkey=admin_priv,
        issued_at="2026-05-19T00:00:00+00:00",
        expires_at="2099-01-01T00:00:00+00:00",
    )

    async def _purge_and_count():
        from sqlalchemy import select

        async with get_session() as s:
            n = await group_heartbeat.purge_expired_grants(
                s, now_iso="2026-05-19T12:00:00+00:00"
            )
            await s.commit()
            remaining = (await s.execute(select(GroupGrant))).scalars().all()
            return n, len(remaining)

    deleted, remaining = _run(_purge_and_count())
    assert deleted == 0
    assert remaining == 1


# ---- end-to-end: admin offline => grants lapse --------------------------


def test_admin_offline_grant_lapses_via_verify(isolated_db):
    """If admin stops heartbeating, the member's stored blob is no
    longer verifiable past its expires_at."""
    admin_priv = group_keys.get_local_group_privkey()
    admin_pub = group_keys.get_local_group_pubkey()
    _, member_pub = group_grant.generate_keypair()

    blob = _seed_grant(
        group_id="g1",
        member_pubkey=member_pub,
        admin_privkey=admin_priv,
        issued_at="2026-05-19T00:00:00+00:00",
        expires_at="2026-05-20T00:00:00+00:00",
    )

    # Pin "now" before expiry — verifies.
    assert (
        group_grant.verify_grant(
            blob,
            group_admin_pubkeys=[admin_pub],
            now_iso="2026-05-19T12:00:00+00:00",
        )
        is not None
    )

    # Pin "now" past expiry without any heartbeat in between — fails.
    assert (
        group_grant.verify_grant(
            blob,
            group_admin_pubkeys=[admin_pub],
            now_iso="2026-05-21T00:00:00+00:00",
        )
        is None
    )


def test_heartbeat_renewal_extends_verifiable_window(isolated_db):
    """If the heartbeat fires before expiry, the renewed blob keeps
    verifying past the original expires_at."""
    admin_priv = group_keys.get_local_group_privkey()
    admin_pub = group_keys.get_local_group_pubkey()
    _, member_pub = group_grant.generate_keypair()

    _seed_grant(
        group_id="g1",
        member_pubkey=member_pub,
        admin_privkey=admin_priv,
        issued_at="2026-05-19T00:00:00+00:00",
        expires_at="2026-05-20T00:00:00+00:00",
    )

    # Heartbeat at T=12h — re-signs with a fresh 24h TTL relative to now.
    async def _tick():
        async with get_session() as s:
            n = await group_heartbeat.refresh_my_issued_grants(
                s,
                now_iso="2026-05-19T12:00:00+00:00",
                ttl_s=GRANT_TTL_SECONDS,
            )
            await s.commit()
            return n

    refreshed = _run(_tick())
    assert refreshed == 1

    # Pull the renewed blob.
    async def _read():
        from sqlalchemy import select

        async with get_session() as s:
            row = (await s.execute(select(GroupGrant))).scalar_one()
            return bytes(row.signature)

    renewed_blob = _run(_read())

    # The renewed blob verifies past 2026-05-20 (the original expires_at)
    # because heartbeat advanced expires_at to ~24h after the refresh wall
    # clock (not the pinned now_iso, since _expires_iso uses real time).
    grant = group_grant.verify_grant(
        renewed_blob,
        group_admin_pubkeys=[admin_pub],
        now_iso="2026-05-20T06:00:00+00:00",
    )
    assert grant is not None
    assert grant.expires_at > "2026-05-20T00:00:00+00:00"


def test_purge_runs_after_refresh_in_loop_iteration(isolated_db):
    """Single-tick sanity: refresh + purge in the same DB session both
    succeed without conflict on the grants table."""
    admin_priv = group_keys.get_local_group_privkey()
    _, member_pub_a = group_grant.generate_keypair()
    _, member_pub_b = group_grant.generate_keypair()

    # One fresh grant + one already-expired.
    _seed_grant(
        group_id="g1",
        member_pubkey=member_pub_a,
        admin_privkey=admin_priv,
        issued_at="2026-05-19T00:00:00+00:00",
        expires_at="2099-01-01T00:00:00+00:00",
        nonce="aa",
    )

    # Insert a second expired one manually.
    from nexus.security.group_keys import get_local_group_pubkey

    me_pub = get_local_group_pubkey()
    expired_blob = group_grant.sign_grant(
        group_id="g1",
        member_pubkey=member_pub_b,
        roles=("member",),
        admin_privkey=admin_priv,
        issued_at="2026-05-01T00:00:00+00:00",
        expires_at="2026-05-02T00:00:00+00:00",
        nonce="bb",
    )

    async def _add_expired():
        async with get_session() as s:
            s.add(
                GroupGrant(
                    id="grant-expired",
                    group_id="g1",
                    member_pubkey=member_pub_b,
                    issued_by_pubkey=me_pub,
                    issued_at="2026-05-01T00:00:00+00:00",
                    expires_at="2026-05-02T00:00:00+00:00",
                    nonce="bb",
                    signature=expired_blob,
                    roles_json='["member"]',
                )
            )
            await s.commit()

    _run(_add_expired())

    async def _tick():
        from sqlalchemy import select

        async with get_session() as s:
            ref = await group_heartbeat.refresh_my_issued_grants(
                s, now_iso="2026-05-19T12:00:00+00:00"
            )
            pur = await group_heartbeat.purge_expired_grants(
                s, now_iso="2026-05-19T12:00:00+00:00"
            )
            await s.commit()
            remaining = (await s.execute(select(GroupGrant))).scalars().all()
            return ref, pur, [r.member_pubkey for r in remaining]

    refreshed, purged, surviving = _run(_tick())
    assert refreshed == 1  # only the unexpired one
    assert purged == 1  # the expired one
    assert surviving == [member_pub_a]
