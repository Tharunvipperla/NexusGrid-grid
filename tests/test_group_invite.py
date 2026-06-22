"""Wave 15.3 — group invite-link state machine.

Tests for :mod:`nexus.security.group_invite`. Touches the DB via the
real async engine to exercise the full path (token generation, row
upsert, atomic capacity tracking, rotation, re-open).
"""

from __future__ import annotations

import asyncio

import pytest

from nexus.security import group_invite
from nexus.storage import database, get_session


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "invite.db"
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


def _run(coro):
    return asyncio.run(coro)


async def _mint(group_id: str = "g1", slot_cap: int = 0, by: str = "admin"):
    async with get_session() as s:
        invite = await group_invite.mint_invite(
            session=s,
            group_id=group_id,
            slot_cap=slot_cap,
            created_by_pubkey=by,
        )
        await s.commit()
    return invite


# ---- mint ---------------------------------------------------------------


def test_mint_invite_returns_unique_tokens(db):
    a = _run(_mint())
    b = _run(_mint())
    assert a.token != b.token
    assert a.active is True
    assert a.slots_filled == 0
    assert a.rotated_at == ""


def test_mint_invite_rejects_negative_cap(db):
    async def _go():
        async with get_session() as s:
            with pytest.raises(ValueError):
                await group_invite.mint_invite(
                    session=s,
                    group_id="g1",
                    slot_cap=-1,
                    created_by_pubkey="admin",
                )

    _run(_go())


def test_mint_invite_with_zero_cap_means_unlimited(db):
    invite = _run(_mint(slot_cap=0))
    assert invite.slot_cap == 0


# ---- validate -----------------------------------------------------------


def test_validate_invite_ok_for_fresh_token(db):
    invite = _run(_mint(slot_cap=5))

    async def _go():
        async with get_session() as s:
            return await group_invite.validate_invite(
                session=s, token=invite.token
            )

    res = _run(_go())
    assert res.ok is True
    assert res.reason == group_invite.REASON_OK


def test_validate_invite_not_found(db):
    async def _go():
        async with get_session() as s:
            return await group_invite.validate_invite(
                session=s, token="bogus"
            )

    res = _run(_go())
    assert res.ok is False
    assert res.reason == group_invite.REASON_NOT_FOUND
    assert res.invite is None


def test_validate_invite_rejects_wrong_group_id(db):
    invite = _run(_mint(group_id="g1"))

    async def _go():
        async with get_session() as s:
            return await group_invite.validate_invite(
                session=s, token=invite.token, group_id="g2"
            )

    res = _run(_go())
    assert res.ok is False
    assert res.reason == group_invite.REASON_WRONG_GROUP


# ---- capacity -----------------------------------------------------------


def test_consume_increments_slots_filled(db):
    invite = _run(_mint(slot_cap=3))

    async def _go():
        async with get_session() as s:
            r = await group_invite.consume_invite(session=s, token=invite.token)
            await s.commit()
            return r

    r = _run(_go())
    assert r.ok is True
    assert r.invite.slots_filled == 1
    assert r.auto_deactivated is False
    assert r.invite.active is True


def test_consume_auto_deactivates_when_cap_reached(db):
    invite = _run(_mint(slot_cap=2))

    async def _consume_once():
        async with get_session() as s:
            r = await group_invite.consume_invite(session=s, token=invite.token)
            await s.commit()
            return r

    first = _run(_consume_once())
    assert first.ok is True
    assert first.auto_deactivated is False

    second = _run(_consume_once())
    assert second.ok is True
    assert second.auto_deactivated is True
    assert second.invite.slots_filled == 2
    assert second.invite.active is False

    # A third attempt is rejected — capacity reached AND active flipped off.
    third = _run(_consume_once())
    assert third.ok is False
    # Could be either inactive or cap_reached depending on which check fires
    # first; both indicate the invite is closed.
    assert third.reason in (
        group_invite.REASON_INACTIVE,
        group_invite.REASON_CAP_REACHED,
    )


def test_pending_validations_do_not_consume_slots(db):
    """The point of the admitted-only counter: validating an invite many
    times must NOT eat into ``slot_cap``."""
    invite = _run(_mint(slot_cap=2))

    async def _check_n_times():
        async with get_session() as s:
            for _ in range(20):
                r = await group_invite.validate_invite(
                    session=s, token=invite.token
                )
                assert r.ok is True

    _run(_check_n_times())

    # After 20 read-only validations, both real slots are still free.
    async def _final():
        async with get_session() as s:
            return await group_invite.validate_invite(
                session=s, token=invite.token
            )

    final = _run(_final())
    assert final.invite.slots_filled == 0


def test_uncapped_invite_never_auto_deactivates(db):
    invite = _run(_mint(slot_cap=0))

    async def _consume():
        async with get_session() as s:
            r = await group_invite.consume_invite(session=s, token=invite.token)
            await s.commit()
            return r

    for _ in range(50):
        r = _run(_consume())
        assert r.ok is True
        assert r.auto_deactivated is False
        assert r.invite.active is True


# ---- rotate -------------------------------------------------------------


def test_rotate_invite_kills_old_token_and_returns_new(db):
    invite = _run(_mint(slot_cap=5))

    async def _rotate():
        async with get_session() as s:
            new = await group_invite.rotate_invite(
                session=s,
                token=invite.token,
                group_id="g1",
                created_by_pubkey="admin",
            )
            await s.commit()
            return new

    new = _run(_rotate())
    assert new is not None
    assert new.token != invite.token
    assert new.slot_cap == invite.slot_cap
    assert new.slots_filled == 0

    # The old token now fails validation as rotated.
    async def _check_old():
        async with get_session() as s:
            return await group_invite.validate_invite(
                session=s, token=invite.token
            )

    old_res = _run(_check_old())
    assert old_res.ok is False
    assert old_res.reason == group_invite.REASON_ROTATED


def test_rotate_invite_returns_none_for_unknown_token(db):
    async def _go():
        async with get_session() as s:
            return await group_invite.rotate_invite(
                session=s,
                token="nope",
                group_id="g1",
                created_by_pubkey="admin",
            )

    assert _run(_go()) is None


def test_rotate_invite_returns_none_for_wrong_group(db):
    invite = _run(_mint(group_id="g1"))

    async def _go():
        async with get_session() as s:
            return await group_invite.rotate_invite(
                session=s,
                token=invite.token,
                group_id="g2",
                created_by_pubkey="admin",
            )

    assert _run(_go()) is None


def test_consume_after_rotate_rejects(db):
    """Stolen-link defense — after rotation, the original token cannot
    consume a slot even if the attacker is racing the legitimate
    rotation."""
    invite = _run(_mint(slot_cap=5))

    async def _rotate_and_consume_old():
        async with get_session() as s:
            await group_invite.rotate_invite(
                session=s,
                token=invite.token,
                group_id="g1",
                created_by_pubkey="admin",
            )
            await s.commit()
            return await group_invite.consume_invite(
                session=s, token=invite.token
            )

    res = _run(_rotate_and_consume_old())
    assert res.ok is False
    assert res.reason == group_invite.REASON_ROTATED


# ---- reopen -------------------------------------------------------------


def test_reopen_flips_active_back_on(db):
    invite = _run(_mint(slot_cap=1))

    async def _consume_to_close():
        async with get_session() as s:
            r = await group_invite.consume_invite(session=s, token=invite.token)
            await s.commit()
            return r

    consumed = _run(_consume_to_close())
    assert consumed.invite.active is False

    async def _reopen_same_cap():
        async with get_session() as s:
            r = await group_invite.reopen_invite(
                session=s, token=invite.token, group_id="g1"
            )
            await s.commit()
            return r

    reopened = _run(_reopen_same_cap())
    assert reopened is not None
    assert reopened.active is True
    # But cap is still 1 and slots_filled is still 1 — so validation
    # will still reject as cap_reached.
    assert reopened.slot_cap == 1
    assert reopened.slots_filled == 1


def test_reopen_can_raise_cap_and_reactivate(db):
    invite = _run(_mint(slot_cap=1))

    async def _exhaust():
        async with get_session() as s:
            await group_invite.consume_invite(session=s, token=invite.token)
            await s.commit()

    _run(_exhaust())

    async def _reopen_with_more_cap():
        async with get_session() as s:
            r = await group_invite.reopen_invite(
                session=s,
                token=invite.token,
                group_id="g1",
                new_slot_cap=5,
            )
            await s.commit()
            return r

    reopened = _run(_reopen_with_more_cap())
    assert reopened.slot_cap == 5
    assert reopened.active is True

    # Subsequent consume succeeds because there's now headroom.
    async def _consume_again():
        async with get_session() as s:
            r = await group_invite.consume_invite(session=s, token=invite.token)
            await s.commit()
            return r

    next_consume = _run(_consume_again())
    assert next_consume.ok is True
    assert next_consume.invite.slots_filled == 2


def test_reopen_returns_none_for_rotated_invite(db):
    """Rotation is the kill switch — re-open cannot resurrect a rotated
    token. Admins must mint a fresh invite instead."""
    invite = _run(_mint(slot_cap=5))

    async def _rotate():
        async with get_session() as s:
            await group_invite.rotate_invite(
                session=s,
                token=invite.token,
                group_id="g1",
                created_by_pubkey="admin",
            )
            await s.commit()

    _run(_rotate())

    async def _try_reopen():
        async with get_session() as s:
            return await group_invite.reopen_invite(
                session=s, token=invite.token, group_id="g1"
            )

    assert _run(_try_reopen()) is None


def test_reopen_rejects_negative_cap(db):
    invite = _run(_mint(slot_cap=1))

    async def _go():
        async with get_session() as s:
            with pytest.raises(ValueError):
                await group_invite.reopen_invite(
                    session=s,
                    token=invite.token,
                    group_id="g1",
                    new_slot_cap=-3,
                )

    _run(_go())
