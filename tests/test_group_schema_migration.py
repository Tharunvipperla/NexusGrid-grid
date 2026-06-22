"""Wave 15 step 15.1 — group/IAM schema bump.

Verifies that ``init_db`` creates the six new Wave 15 tables on both a
fresh database and on one that already contains the prior schema.
SCHEMA_VERSION advances to 10 and the migration is a pure
``create_all`` (no ALTER TABLE) because all six tables are net-new.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text

from nexus.storage import database, models


WAVE_15_TABLES = {
    "groups",
    "group_members",
    "group_roles",
    "group_member_roles",
    "group_grants",
    "group_invite_links",
}


@pytest.fixture
def fresh_db(tmp_path):
    db_path = tmp_path / "fresh.db"
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


async def _list_tables(url: str) -> set[str]:
    async with database._engine.connect() as conn:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
        return {row[0] for row in result.fetchall()}


def test_schema_version_bumped_for_wave_16():
    # 12 = Wave 16 post-ship (member.display_name + pending.display_name).
    # 13 = C4 secrets vault (new `secrets` table).
    # 14 = F-005/F-007 security: peers.peer_group_pubkey.
    assert models.SCHEMA_VERSION == 14


def test_all_six_wave15_tables_exist_on_fresh_db(fresh_db):
    tables = asyncio.run(_list_tables(fresh_db))
    missing = WAVE_15_TABLES - tables
    assert not missing, f"missing Wave 15 tables on fresh init: {missing}"


def test_pre_wave15_db_picks_up_new_tables_after_init(tmp_path):
    """Simulate a Wave-14-shaped DB (the six new tables don't exist yet)
    and verify a re-init creates them."""

    db_path = tmp_path / "legacy.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"

    async def _seed_legacy_then_reinit():
        # Step 1: init creates the full Wave 15 schema.
        await database.init_db(0, url=url)
        # Step 2: drop the Wave 15 tables to simulate a pre-Wave-15 DB.
        async with database._engine.begin() as conn:
            for name in WAVE_15_TABLES:
                await conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
        # Step 3: dispose and re-init — migration must re-create them.
        await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""
        await database.init_db(0, url=url)

    asyncio.run(_seed_legacy_then_reinit())

    tables = asyncio.run(_list_tables(url))
    missing = WAVE_15_TABLES - tables
    assert not missing, f"migration did not create Wave 15 tables: {missing}"

    async def _teardown():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""

    asyncio.run(_teardown())


def test_groups_columns(fresh_db):
    async def _columns(table: str) -> set[str]:
        async with database._engine.connect() as conn:
            result = await conn.execute(text(f"PRAGMA table_info({table})"))
            return {row[1] for row in result.fetchall()}

    cols = asyncio.run(_columns("groups"))
    assert {"id", "name", "founder_pubkey", "created_at", "deleted_at"} <= cols


def test_invite_links_columns(fresh_db):
    async def _columns(table: str) -> set[str]:
        async with database._engine.connect() as conn:
            result = await conn.execute(text(f"PRAGMA table_info({table})"))
            return {row[1] for row in result.fetchall()}

    cols = asyncio.run(_columns("group_invite_links"))
    assert {
        "token",
        "group_id",
        "slot_cap",
        "slots_filled",
        "active",
        "created_by_pubkey",
        "created_at",
        "rotated_at",
    } <= cols


def test_grants_has_signature_blob(fresh_db):
    async def _columns(table: str) -> set[str]:
        async with database._engine.connect() as conn:
            result = await conn.execute(text(f"PRAGMA table_info({table})"))
            return {(row[1], row[2]) for row in result.fetchall()}

    cols = asyncio.run(_columns("group_grants"))
    names = {n for n, _ in cols}
    assert {"id", "group_id", "member_pubkey", "nonce", "signature", "roles_json"} <= names
    # signature is the only BLOB-typed column we care about.
    blob_cols = {n for n, ty in cols if ty.upper() == "BLOB"}
    assert "signature" in blob_cols


def test_wave16_tables_exist_on_fresh_db(fresh_db):
    """Wave 16.1 adds two net-new tables for privacy + invitations."""
    tables = asyncio.run(_list_tables(fresh_db))
    assert "group_invitation_offers" in tables
    assert "group_pending_join_requests" in tables


def test_groups_has_privacy_mode_column(fresh_db):
    async def _columns(table: str) -> set[str]:
        async with database._engine.connect() as conn:
            result = await conn.execute(text(f"PRAGMA table_info({table})"))
            return {row[1] for row in result.fetchall()}

    cols = asyncio.run(_columns("groups"))
    assert "privacy_mode" in cols


def test_groups_privacy_mode_defaults_to_open(fresh_db):
    """Inserting a row without privacy_mode picks up the 'open' default."""

    async def _exercise():
        async with database._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO groups (id, name, founder_pubkey, created_at, deleted_at) "
                    "VALUES ('gX', 'Test', 'pk', '2026-05-19', '')"
                )
            )
            result = await conn.execute(
                text("SELECT privacy_mode FROM groups WHERE id='gX'")
            )
            return result.scalar_one()

    assert asyncio.run(_exercise()) == "open"


def test_invitation_offers_columns(fresh_db):
    async def _columns(table: str) -> set[str]:
        async with database._engine.connect() as conn:
            result = await conn.execute(text(f"PRAGMA table_info({table})"))
            return {row[1] for row in result.fetchall()}

    cols = asyncio.run(_columns("group_invitation_offers"))
    expected = {
        "token", "role", "group_id", "group_name",
        "founder_pubkey", "founder_address", "target_peer_label",
        "status", "created_at", "responded_at",
    }
    assert expected <= cols, f"missing: {expected - cols}"


def test_invitation_offers_composite_pk_allows_same_token_both_roles(fresh_db):
    """Same token can live as sender + recipient in one DB (self-test edge)."""

    async def _exercise():
        async with database._engine.begin() as conn:
            for role in ("sender", "recipient"):
                await conn.execute(
                    text(
                        "INSERT INTO group_invitation_offers "
                        "(token, role, group_id, status, created_at) "
                        "VALUES ('tok1', :r, 'g1', 'pending', '2026-05-19')"
                    ),
                    {"r": role},
                )
            result = await conn.execute(
                text("SELECT role FROM group_invitation_offers WHERE token='tok1'")
            )
            return {r[0] for r in result.fetchall()}

    assert asyncio.run(_exercise()) == {"sender", "recipient"}


def test_pending_join_requests_columns(fresh_db):
    async def _columns(table: str) -> set[str]:
        async with database._engine.connect() as conn:
            result = await conn.execute(text(f"PRAGMA table_info({table})"))
            return {row[1] for row in result.fetchall()}

    cols = asyncio.run(_columns("group_pending_join_requests"))
    expected = {
        "id", "group_id", "joiner_pubkey", "joiner_address",
        "invite_token", "message", "status",
        "created_at", "decided_at", "decided_by_pubkey", "decision_reason",
    }
    assert expected <= cols, f"missing: {expected - cols}"


def test_legacy_db_picks_up_privacy_mode_via_migration(tmp_path):
    """A Wave-15-shaped 'groups' table (no privacy_mode column) gains the
    column on next init_db without dropping existing rows."""

    db_path = tmp_path / "wave15.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"

    async def _seed_legacy_then_reinit():
        await database.init_db(0, url=url)
        # Simulate the Wave-15 shape by dropping the new column.
        async with database._engine.begin() as conn:
            await conn.execute(text("ALTER TABLE groups DROP COLUMN privacy_mode"))
            # Insert a row in the old shape.
            await conn.execute(
                text(
                    "INSERT INTO groups (id, name, founder_pubkey, created_at, deleted_at) "
                    "VALUES ('legacy1', 'Old', 'pk', '2026-05-18', '')"
                )
            )
        await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""
        # Re-init triggers the ALTER ADD COLUMN migration.
        await database.init_db(0, url=url)
        async with database._engine.connect() as conn:
            result = await conn.execute(
                text("SELECT privacy_mode FROM groups WHERE id='legacy1'")
            )
            return result.scalar_one()

    # Existing row gets the default 'open'.
    assert asyncio.run(_seed_legacy_then_reinit()) == "open"

    async def _teardown():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""

    asyncio.run(_teardown())


def test_composite_pk_member_roles_allows_multiple_roles_per_member(fresh_db):
    """A single (group_id, member_pubkey) pair must be able to hold many
    role rows — one per role_name."""

    async def _exercise():
        async with database._engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO group_member_roles "
                    "(group_id, member_pubkey, role_name, assigned_by_pubkey, assigned_at) "
                    "VALUES ('g1', 'pk1', 'member', 'admin1', '2026-05-19T00:00:00Z')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO group_member_roles "
                    "(group_id, member_pubkey, role_name, assigned_by_pubkey, assigned_at) "
                    "VALUES ('g1', 'pk1', 'db-readers', 'admin1', '2026-05-19T00:00:00Z')"
                )
            )
            result = await conn.execute(
                text(
                    "SELECT role_name FROM group_member_roles "
                    "WHERE group_id='g1' AND member_pubkey='pk1'"
                )
            )
            return {r[0] for r in result.fetchall()}

    roles = asyncio.run(_exercise())
    assert roles == {"member", "db-readers"}
