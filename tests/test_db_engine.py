"""C7 — one-click DB engine bring-up: pure spec/DSN builders."""

from __future__ import annotations

import pytest

from nexus.runtime import db_engine as E


def test_list_engines():
    assert set(E.list_engines()) == {"postgres", "mysql", "redis", "mongo"}


def test_build_admin_dsn_per_engine():
    assert E.build_admin_dsn("postgres", "127.0.0.1", 5440, "p") == \
        "postgresql://postgres:p@127.0.0.1:5440/postgres"
    assert E.build_admin_dsn("mysql", "127.0.0.1", 3310, "p") == \
        "mysql://root:p@127.0.0.1:3310"
    assert E.build_admin_dsn("redis", "127.0.0.1", 6390, "p") == \
        "redis://:p@127.0.0.1:6390"
    assert E.build_admin_dsn("mongo", "127.0.0.1", 27020, "p") == \
        "mongodb://root:p@127.0.0.1:27020"


def test_build_admin_dsn_unknown_raises():
    with pytest.raises(ValueError):
        E.build_admin_dsn("cassandra", "h", 1, "p")


def test_container_spec_postgres_publishes_loopback_only():
    spec = E.engine_container_spec("postgres", "secret", 5440, "nexus-db-postgres-x")
    assert spec["image"] == "postgres:16-alpine"
    assert spec["environment"]["POSTGRES_PASSWORD"] == "secret"
    # Published to loopback only, with the right host port.
    assert spec["ports"] == {"5432/tcp": ("127.0.0.1", 5440)}
    assert spec["detach"] is True
    assert spec["restart_policy"]["Name"] == "unless-stopped"
    assert spec["labels"]["nexus.dbaas.engine"] == "postgres"
    assert "command" not in spec  # postgres takes the password via env


def test_container_spec_redis_uses_command_password():
    spec = E.engine_container_spec("redis", "secret", 6390, "nexus-db-redis-x")
    # Redis has no password env — it must be passed on the command line.
    assert spec["command"] == ["redis-server", "--requirepass", "secret"]
    assert "REDIS_PASSWORD" not in spec["environment"]


def test_container_spec_mongo_sets_root_username():
    spec = E.engine_container_spec("mongo", "secret", 27020, "nexus-db-mongo-x")
    assert spec["environment"]["MONGO_INITDB_ROOT_USERNAME"] == "root"
    assert spec["environment"]["MONGO_INITDB_ROOT_PASSWORD"] == "secret"


def test_container_spec_unknown_raises():
    with pytest.raises(ValueError):
        E.engine_container_spec("cassandra", "p", 1, "n")
