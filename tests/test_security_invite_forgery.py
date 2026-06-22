"""Security F-013 — signed group-join invite must be pinned to the founder key
the node recorded at issue time, so a holder of a valid invite_id can't mint a
self-signed envelope (their own key) with tampered fields (e.g. a revived expiry).
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from nexus.api.group_peer import JoinRequestBody, peer_group_join_request
from nexus.security.group_grant import generate_keypair
from nexus.security.group_invite_token import sign_group_join_invite
from nexus.storage import database, get_session
from nexus.storage.models import Group, GroupJoinInviteV2
from nexus.utils.time import iso_now


@pytest.fixture
def db(tmp_path):
    url = f"sqlite+aiosqlite:///{(tmp_path / 'g.db').as_posix()}"
    asyncio.run(database.init_db(0, url=url))
    yield url

    async def _td():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""
    asyncio.run(_td())


def _seed_group_and_invite(founder_pub, invite_id, expires_at):
    async def _go():
        async with get_session() as s:
            s.add(Group(id="g1", name="Team", founder_pubkey=founder_pub))
            s.add(GroupJoinInviteV2(
                invite_id=invite_id, group_id="g1", founder_pubkey=founder_pub,
                issued_at=iso_now(), expires_at=expires_at, max_uses=5,
                used_count=0, status="active"))
            await s.commit()
    asyncio.run(_go())


def test_forged_invite_with_attacker_key_rejected(db):
    founder_priv, founder_pub = generate_keypair()
    _seed_group_and_invite(founder_pub, "inv-1", "2099-01-01T00:00:00+00:00")

    # Attacker knows the invite_id but not the founder key. They self-sign an
    # envelope with their OWN key (claiming it as founder) + a future expiry.
    atk_priv, atk_pub = generate_keypair()
    forged = sign_group_join_invite(
        invite_id="inv-1", group_id="g1", founder_pubkey=atk_pub,
        issued_at=iso_now(), expires_at="2099-01-01T00:00:00+00:00",
        max_uses=999, founder_privkey=atk_priv,
    )
    _joiner_priv, joiner_pub = generate_keypair()
    body = JoinRequestBody(signed_invite_hex=forged, joiner_pubkey=joiner_pub)

    with pytest.raises(HTTPException) as ei:
        asyncio.run(peer_group_join_request(body))
    assert ei.value.status_code == 403
    assert "founder" in ei.value.detail.lower()


def test_expired_invite_cannot_be_revived_by_forgery(db):
    founder_priv, founder_pub = generate_keypair()
    # Local row says the invite already expired.
    _seed_group_and_invite(founder_pub, "inv-2", "2000-01-01T00:00:00+00:00")

    # Attacker re-mints with a far-future expiry, self-signed.
    atk_priv, atk_pub = generate_keypair()
    forged = sign_group_join_invite(
        invite_id="inv-2", group_id="g1", founder_pubkey=atk_pub,
        issued_at=iso_now(), expires_at="2099-01-01T00:00:00+00:00",
        max_uses=1, founder_privkey=atk_priv,
    )
    _jp, joiner_pub = generate_keypair()
    body = JoinRequestBody(signed_invite_hex=forged, joiner_pubkey=joiner_pub)
    with pytest.raises(HTTPException) as ei:
        asyncio.run(peer_group_join_request(body))
    # Rejected at the founder pin (403), never reaching membership.
    assert ei.value.status_code == 403
