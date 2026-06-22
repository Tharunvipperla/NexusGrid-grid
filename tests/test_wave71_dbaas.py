"""Wave 71 — Database-as-a-Service: provider adapters + grant provisioning."""

from __future__ import annotations

import asyncio

import pytest

from nexus.core.config import (
    LOCAL_SETTINGS,
    normalize_hosted_services,
    public_services,
)
from nexus.runtime import db_provider
from nexus.runtime import service_grants as sg
from nexus.runtime.service_kinds import connection_string
from nexus.security import group_keys, tokens
from nexus.security.group_grant import generate_keypair
from nexus.security.usage_receipt import sign_statement
from nexus.storage import database, get_session
from nexus.storage.models import ServiceDbProvision, ServiceGrant
from nexus.utils.time import iso_now


# --- fake adapter -----------------------------------------------------------


class _FakeAdapter:
    KIND = "postgres"

    def __init__(self):
        self.created = []
        self.dropped = []

    def create(self, admin_dsn, database, user, password):
        self.created.append((admin_dsn, database, user, password))

    def drop(self, admin_dsn, database, user):
        self.dropped.append((admin_dsn, database, user))


@pytest.fixture
def fake_adapter(monkeypatch):
    fa = _FakeAdapter()
    monkeypatch.setitem(db_provider._PLUGINS, "fake", fa)
    return fa


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr("nexus.security.tokens.BASE_DIR", tmp_path)
    monkeypatch.setattr("nexus.security.group_keys.BASE_DIR", tmp_path)
    monkeypatch.delenv("NEXUS_SIGNING_SECRET", raising=False)
    tokens._reset_for_testing()
    group_keys._reset_for_testing()
    db_path = tmp_path / "groups.db"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    asyncio.run(database.init_db(0, url=url))
    LOCAL_SETTINGS["hosted_services"] = normalize_hosted_services([{
        "name": "PG", "access": "permission", "service_kind": "postgres",
        "local_host": "127.0.0.1", "local_port": 5432,
        "db_provider": {"engine": "fake", "admin_dsn": "postgresql://admin@localhost/postgres"},
    }])
    yield url

    async def _teardown():
        if database._engine is not None:
            await database._engine.dispose()
        database._engine = None
        database._session_factory = None
        database._current_db_url = ""

    asyncio.run(_teardown())
    LOCAL_SETTINGS["hosted_services"] = []
    tokens._reset_for_testing()
    group_keys._reset_for_testing()


# --- db_provider framework --------------------------------------------------


def test_names_are_deterministic_and_safe():
    a = db_provider._names("svc", "consumerpub")
    b = db_provider._names("svc", "consumerpub")
    assert a == b
    db, user = a
    assert db.startswith("nx_") and db == user
    assert all(c.isalnum() or c == "_" for c in db)  # injection-safe identifier


def test_unknown_engine_raises():
    with pytest.raises(ValueError):
        db_provider.get_adapter("nope-engine")


def test_provision_deprovision_via_adapter(fake_adapter):
    creds = db_provider.provision("fake", "dsn://x", "PG", "consumer1")
    assert creds["engine"] == "fake" and creds["kind"] == "postgres"
    assert creds["database"] == creds["user"] and creds["password"]
    assert fake_adapter.created and fake_adapter.created[0][1] == creds["database"]
    db_provider.deprovision("fake", "dsn://x", "PG", "consumer1")
    assert fake_adapter.dropped and fake_adapter.dropped[0][1] == creds["database"]


def test_available_adapters_lists_builtin_postgres():
    assert "postgres" in db_provider.available_adapters()


def test_available_adapters_lists_all_builtins():
    a = db_provider.available_adapters()
    for engine in ("postgres", "mysql", "redis", "mongo"):
        assert engine in a


# --- reference adapters: command construction (mocked connections) ----------


class _FakeCursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.log.append(sql)


class _FakeSQLConn:
    def __init__(self):
        self.log = []
        self.closed = False

    def cursor(self):
        return _FakeCursor(self.log)

    def close(self):
        self.closed = True


def test_mysql_adapter_create_and_drop(monkeypatch):
    a = db_provider.get_adapter("mysql")
    conn = _FakeSQLConn()
    monkeypatch.setattr(a, "_connect", lambda dsn: conn)
    a.create("mysql://root:x@h:3306", "nx_db", "nx_db", "secret")
    joined = " | ".join(conn.log)
    assert "CREATE DATABASE IF NOT EXISTS `nx_db`" in joined
    assert "CREATE USER IF NOT EXISTS 'nx_db'@'%'" in joined
    assert "GRANT ALL PRIVILEGES ON `nx_db`.* TO 'nx_db'@'%'" in joined
    assert "'secret'" in joined  # quoted password literal
    assert conn.closed

    conn2 = _FakeSQLConn()
    monkeypatch.setattr(a, "_connect", lambda dsn: conn2)
    a.drop("mysql://root:x@h:3306", "nx_db", "nx_db")
    j2 = " | ".join(conn2.log)
    assert "DROP DATABASE IF EXISTS `nx_db`" in j2
    assert "DROP USER IF EXISTS 'nx_db'@'%'" in j2


def test_mysql_password_literal_is_escaped(monkeypatch):
    a = db_provider.get_adapter("mysql")
    conn = _FakeSQLConn()
    monkeypatch.setattr(a, "_connect", lambda dsn: conn)
    a.create("dsn", "nx_db", "nx_db", "a'b")
    assert "'a''b'" in " | ".join(conn.log)  # embedded single quote doubled


class _FakeRedis:
    def __init__(self):
        self.calls = []

    def execute_command(self, *a):
        self.calls.append(tuple(a))


def test_redis_adapter_create_and_drop(monkeypatch):
    a = db_provider.get_adapter("redis")
    r = _FakeRedis()
    monkeypatch.setattr(a, "_connect", lambda dsn: r)
    a.create("redis://:x@h:6379", "nx_k", "nx_k", "secret")
    call = r.calls[0]
    assert call[:3] == ("ACL", "SETUSER", "nx_k")
    for tok in ("reset", "on", ">secret", "~nx_k:*", "+@all"):
        assert tok in call

    r2 = _FakeRedis()
    monkeypatch.setattr(a, "_connect", lambda dsn: r2)
    a.drop("redis://:x@h:6379", "nx_k", "nx_k")
    assert r2.calls[0] == ("ACL", "DELUSER", "nx_k")


class _FakeMongoDB:
    def __init__(self, log, users):
        self.log = log
        self._users = users

    def command(self, *a, **kw):
        self.log.append((a, kw))
        if a and a[0] == "usersInfo":
            return {"users": list(self._users)}
        return {}


class _FakeMongoClient:
    def __init__(self, users=()):
        self.log = []
        self.dropped = []
        self._users = users

    def __getitem__(self, name):
        return _FakeMongoDB(self.log, self._users)

    def drop_database(self, name):
        self.dropped.append(name)

    def close(self):
        pass


def test_mongo_adapter_creates_user_when_absent(monkeypatch):
    a = db_provider.get_adapter("mongo")
    client = _FakeMongoClient(users=())
    monkeypatch.setattr(a, "_connect", lambda dsn: client)
    a.create("mongodb://root:x@h:27017", "nx_db", "nx_db", "secret")
    cmds = [c[0][0] for c in client.log]
    assert "usersInfo" in cmds and "createUser" in cmds and "updateUser" not in cmds
    create = [c for c in client.log if c[0][0] == "createUser"][0]
    assert create[1]["pwd"] == "secret"
    assert create[1]["roles"] == [{"role": "readWrite", "db": "nx_db"}]


def test_mongo_adapter_updates_user_when_present(monkeypatch):
    a = db_provider.get_adapter("mongo")
    client = _FakeMongoClient(users=({"user": "nx_db"},))
    monkeypatch.setattr(a, "_connect", lambda dsn: client)
    a.create("dsn", "nx_db", "nx_db", "secret")
    cmds = [c[0][0] for c in client.log]
    assert "updateUser" in cmds and "createUser" not in cmds


def test_mongo_adapter_drop(monkeypatch):
    a = db_provider.get_adapter("mongo")
    client = _FakeMongoClient()
    monkeypatch.setattr(a, "_connect", lambda dsn: client)
    a.drop("dsn", "nx_db", "nx_db")
    cmds = [c[0][0] for c in client.log]
    assert "dropUser" in cmds and client.dropped == ["nx_db"]


def test_plugin_adapter_loaded_from_disk(tmp_path, monkeypatch):
    """A host can drop a nexus_dbproviders/<engine>.py file and it's picked up."""
    monkeypatch.setattr("nexus.core.paths.BASE_DIR", tmp_path)
    d = tmp_path / "nexus_dbproviders"
    d.mkdir()
    (d / "memdb.py").write_text(
        "KIND = 'memdb'\n"
        "CALLS = []\n"
        "def create(admin_dsn, database, user, password): CALLS.append(('c', database))\n"
        "def drop(admin_dsn, database, user): CALLS.append(('d', database))\n",
        encoding="utf-8",
    )
    # Force a fresh load against the temp BASE_DIR.
    monkeypatch.setattr(db_provider, "_plugins_loaded", False)
    monkeypatch.setattr(db_provider, "_PLUGINS", {})
    assert "memdb" in db_provider.available_adapters()
    creds = db_provider.provision("memdb", "dsn", "svc", "consumer")
    assert creds["kind"] == "memdb" and creds["database"].startswith("nx_")


# --- config: schema + public stripping --------------------------------------


def test_db_provider_is_host_only_stripped_from_public():
    svcs = normalize_hosted_services([{
        "name": "PG", "service_kind": "postgres",
        "db_provider": {"engine": "postgres", "admin_dsn": "postgresql://secret@h/db"},
    }])
    assert svcs[0]["service_kind"] == "postgres"
    assert svcs[0]["db_provider"]["admin_dsn"].startswith("postgresql://secret")
    pub = public_services(svcs)
    assert "db_provider" not in pub[0]          # secret stripped
    assert "local_port" not in pub[0]
    assert pub[0]["service_kind"] == "postgres"  # kind is public


def test_db_provider_requires_engine_and_dsn():
    svcs = normalize_hosted_services([{"name": "X", "db_provider": {"engine": "postgres"}}])
    assert svcs[0]["db_provider"] == {}  # incomplete config dropped


# --- connection_string ------------------------------------------------------


def test_connection_string_with_provisioned_login():
    assert connection_string("postgres", 15432, user="nx_abc", database="nx_abc") == (
        "psql -h localhost -p 15432 -U nx_abc nx_abc")
    # Unchanged when no login supplied (back-compat).
    assert connection_string("postgres", 15432) == "psql -h localhost -p 15432 -U postgres"


# --- grant provisioning flow ------------------------------------------------


def _approved_grant(consumer_pub, consumer_uuid="nexus_consumer") -> str:
    gid = "grant-" + consumer_pub[:8]

    async def _go():
        async with get_session() as s:
            s.add(ServiceGrant(
                grant_id=gid, service_name="PG",
                provider_pubkey=group_keys.get_local_group_pubkey(),
                consumer_pubkey=consumer_pub, consumer_uuid=consumer_uuid,
                status="approved", access="permission", created_at=iso_now(),
            ))
            await s.commit()
    asyncio.run(_go())
    return gid


def _creds_request(consumer_priv, consumer_pub, consumer_uuid="nexus_consumer"):
    provider = group_keys.get_local_group_pubkey()
    ts = "2026-06-14T00:00:00Z"
    payload = sg._req_payload(provider, "PG", consumer_pub, consumer_uuid, ts)
    sig = sign_statement(sg.STMT_DB_CREDS, payload, consumer_priv)
    return {"service": "PG", "consumer_pubkey": consumer_pub,
            "consumer_uuid": consumer_uuid, "ts": ts, "sig": sig}


def _get_provision(grant_id):
    async def _go():
        async with get_session() as s:
            return await s.get(ServiceDbProvision, grant_id)
    return asyncio.run(_go())


def test_handle_db_credentials_happy_path(isolated_db, fake_adapter):
    cpriv, cpub = generate_keypair()
    gid = _approved_grant(cpub)
    res = asyncio.run(sg.handle_db_credentials(_creds_request(cpriv, cpub)))
    assert res["ok"] is True
    conn = res["conn"]
    assert conn["engine"] == "fake" and conn["kind"] == "postgres"
    assert conn["database"] == conn["user"] and conn["password"]
    # Provisioned + recorded; the adapter created the db/login once.
    assert _get_provision(gid) is not None
    assert len(fake_adapter.created) == 1


def test_db_credentials_idempotent_same_password(isolated_db, fake_adapter):
    cpriv, cpub = generate_keypair()
    _approved_grant(cpub)
    r1 = asyncio.run(sg.handle_db_credentials(_creds_request(cpriv, cpub)))
    r2 = asyncio.run(sg.handle_db_credentials(_creds_request(cpriv, cpub)))
    assert r1["conn"]["password"] == r2["conn"]["password"]
    assert len(fake_adapter.created) == 1  # not re-provisioned


def test_db_credentials_bad_signature(isolated_db, fake_adapter):
    cpriv, cpub = generate_keypair()
    _approved_grant(cpub)
    body = _creds_request(cpriv, cpub)
    body["sig"] = "00" * 32  # wrong
    res = asyncio.run(sg.handle_db_credentials(body))
    assert res == {"ok": False, "error": "bad_signature"}


def test_db_credentials_requires_approved_grant(isolated_db, fake_adapter):
    cpriv, cpub = generate_keypair()  # no grant created
    res = asyncio.run(sg.handle_db_credentials(_creds_request(cpriv, cpub)))
    assert res["ok"] is False and res["error"] == "no_grant"


def test_revoke_deprovisions(isolated_db, fake_adapter):
    cpriv, cpub = generate_keypair()
    gid = _approved_grant(cpub)
    asyncio.run(sg.handle_db_credentials(_creds_request(cpriv, cpub)))
    assert _get_provision(gid) is not None
    asyncio.run(sg.revoke_grant(gid))
    assert _get_provision(gid) is None          # record gone
    assert len(fake_adapter.dropped) == 1        # db/login dropped


def test_non_db_service_returns_none(isolated_db, fake_adapter):
    # A service with no db_provider block isn't a DBaaS service.
    LOCAL_SETTINGS["hosted_services"] = normalize_hosted_services([
        {"name": "PG", "access": "permission", "local_port": 5432}])
    cpriv, cpub = generate_keypair()
    _approved_grant(cpub)
    res = asyncio.run(sg.handle_db_credentials(_creds_request(cpriv, cpub)))
    assert res["ok"] is False and res["error"] == "not_a_db_service"
