"""Pluggable database-provider adapters (Database-as-a-Service).

DBaaS = the existing service marketplace + a thin provider-adapter layer. A host
already runs a database engine; on an *approved* :class:`ServiceGrant` we
provision a dedicated per-consumer database + login and hand back a connection.
The bytes ride the unchanged service tunnel — this module only mints
and drops credentials.

An adapter is the same drop-in model as pumps / runners: a
builtin reference adapter (``postgres``) plus any host-supplied
``nexus_dbproviders/<engine>.py`` next to the node. An adapter exposes::

    create(admin_dsn, database, user, password) -> None   # idempotent
    drop(admin_dsn, database, user) -> None                # idempotent
    KIND = "postgres"   # optional; the service_kind for the connection string

Adapters run the HOST'S OWN code against the HOST'S OWN engine, using the admin
DSN the host configured on the service — never anything a consumer supplies.
Provisioning derives a deterministic, injection-safe name from
``(service_name, consumer_pubkey)`` so re-provisioning is idempotent and a
later deprovision targets exactly the same objects.
"""

from __future__ import annotations

import hashlib
import importlib.util
import logging
import secrets

_log = logging.getLogger("nexus.runtime.db_provider")


def _names(service_name: str, consumer_pubkey: str) -> tuple[str, str]:
    """Deterministic ``(database, user)`` for one (service, consumer) pair.

    ``nx_<16 hex>`` — alphanumeric+underscore only, so it is a safe SQL
    identifier by construction regardless of the adapter."""
    h = hashlib.sha256(
        f"{service_name}|{consumer_pubkey}".encode("utf-8")
    ).hexdigest()[:16]
    return f"nx_{h}", f"nx_{h}"


# --- reference adapter: PostgreSQL ------------------------------------------


class _PostgresAdapter:
    """Provision a per-consumer database + login role on a host-run Postgres.

    The driver (``psycopg`` v3, falling back to ``psycopg2``) is imported
    lazily so the node runs fine without it installed — it's only needed when a
    host actually provisions a Postgres DBaaS grant."""

    KIND = "postgres"

    @staticmethod
    def _connect(admin_dsn: str):
        try:
            import psycopg  # type: ignore
            return psycopg.connect(admin_dsn, autocommit=True), "psycopg"
        except ImportError:
            pass
        try:
            import psycopg2  # type: ignore
            conn = psycopg2.connect(admin_dsn)
            conn.autocommit = True
            return conn, "psycopg2"
        except ImportError as exc:
            raise RuntimeError(
                "postgres DBaaS adapter needs the 'psycopg' (v3) or 'psycopg2' "
                "driver installed on the host"
            ) from exc

    def create(self, admin_dsn: str, database: str, user: str, password: str) -> None:
        conn, flavor = self._connect(admin_dsn)
        try:
            if flavor == "psycopg":
                from psycopg import sql  # type: ignore
            else:
                from psycopg2 import sql  # type: ignore
            ident = sql.Identifier
            lit = sql.Literal
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (user,))
                if cur.fetchone():
                    cur.execute(sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                        ident(user), lit(password)))
                else:
                    cur.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        ident(user), lit(password)))
                cur.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,))
                if not cur.fetchone():
                    cur.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        ident(database), ident(user)))
        finally:
            conn.close()

    def drop(self, admin_dsn: str, database: str, user: str) -> None:
        conn, flavor = self._connect(admin_dsn)
        try:
            if flavor == "psycopg":
                from psycopg import sql  # type: ignore
            else:
                from psycopg2 import sql  # type: ignore
            ident = sql.Identifier
            with conn.cursor() as cur:
                # Terminate live sessions so DROP DATABASE isn't blocked.
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname=%s AND pid<>pg_backend_pid()", (database,))
                cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(ident(database)))
                cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(ident(user)))
        finally:
            conn.close()


def _sql_str(value: str) -> str:
    """Single-quoted SQL string literal (used for passwords in MySQL DDL, which
    can't be parameterised). Identifiers are ``nx_<hex>`` so only the password —
    a controlled token — needs quoting; we still escape defensively."""
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


# --- reference adapter: MySQL / MariaDB -------------------------------------


class _MySQLAdapter:
    """Per-consumer database + ``user@'%'`` with privileges on it only."""

    KIND = "mysql"

    @staticmethod
    def _connect(admin_dsn: str):
        from urllib.parse import unquote, urlparse

        try:
            import pymysql  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "mysql DBaaS adapter needs the 'pymysql' driver installed on the host"
            ) from exc
        u = urlparse(admin_dsn)
        return pymysql.connect(
            host=u.hostname or "127.0.0.1", port=u.port or 3306,
            user=unquote(u.username or "root"),
            password=unquote(u.password or ""), autocommit=True,
        )

    def create(self, admin_dsn: str, database: str, user: str, password: str) -> None:
        conn = self._connect(admin_dsn)
        try:
            pw = _sql_str(password)
            with conn.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{database}`")
                cur.execute(f"CREATE USER IF NOT EXISTS '{user}'@'%' IDENTIFIED BY {pw}")
                cur.execute(f"ALTER USER '{user}'@'%' IDENTIFIED BY {pw}")
                cur.execute(f"GRANT ALL PRIVILEGES ON `{database}`.* TO '{user}'@'%'")
                cur.execute("FLUSH PRIVILEGES")
        finally:
            conn.close()

    def drop(self, admin_dsn: str, database: str, user: str) -> None:
        conn = self._connect(admin_dsn)
        try:
            with conn.cursor() as cur:
                cur.execute(f"DROP DATABASE IF EXISTS `{database}`")
                cur.execute(f"DROP USER IF EXISTS '{user}'@'%'")
                cur.execute("FLUSH PRIVILEGES")
        finally:
            conn.close()


# --- reference adapter: Redis (ACL user + key namespace) --------------------


class _RedisAdapter:
    """Redis has no named databases, so we mint an ACL user restricted to keys
    under ``<database>:*`` (the ``database`` here is the consumer's key prefix).
    ``ACL SETUSER ... reset`` makes re-provision idempotent."""

    KIND = "redis"

    @staticmethod
    def _connect(admin_dsn: str):
        try:
            import redis  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "redis DBaaS adapter needs the 'redis' driver installed on the host"
            ) from exc
        return redis.Redis.from_url(admin_dsn)

    def create(self, admin_dsn: str, database: str, user: str, password: str) -> None:
        r = self._connect(admin_dsn)
        r.execute_command(
            "ACL", "SETUSER", user, "reset", "on",
            f">{password}", f"~{database}:*", "+@all",
        )

    def drop(self, admin_dsn: str, database: str, user: str) -> None:
        r = self._connect(admin_dsn)
        try:
            r.execute_command("ACL", "DELUSER", user)
        except Exception:
            pass


# --- reference adapter: MongoDB ---------------------------------------------


class _MongoAdapter:
    """Per-consumer database + a ``readWrite`` user scoped to it."""

    KIND = "mongo"

    @staticmethod
    def _connect(admin_dsn: str):
        try:
            from pymongo import MongoClient  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "mongo DBaaS adapter needs the 'pymongo' driver installed on the host"
            ) from exc
        return MongoClient(admin_dsn)

    def create(self, admin_dsn: str, database: str, user: str, password: str) -> None:
        client = self._connect(admin_dsn)
        try:
            db = client[database]
            roles = [{"role": "readWrite", "db": database}]
            info = db.command("usersInfo", user)
            if info.get("users"):
                db.command("updateUser", user, pwd=password, roles=roles)
            else:
                db.command("createUser", user, pwd=password, roles=roles)
        finally:
            client.close()

    def drop(self, admin_dsn: str, database: str, user: str) -> None:
        client = self._connect(admin_dsn)
        try:
            try:
                client[database].command("dropUser", user)
            except Exception:
                pass
            client.drop_database(database)
        finally:
            client.close()


# --- adapter registry -------------------------------------------------------

_BUILTIN: dict[str, object] = {
    "postgres": _PostgresAdapter(),
    "mysql": _MySQLAdapter(),
    "redis": _RedisAdapter(),
    "mongo": _MongoAdapter(),
}
_PLUGINS: dict[str, object] = {}
_plugins_loaded = False


def register_adapter(name: str, adapter: object) -> None:
    """Register a DB-provider adapter. Call from a ``nexus_dbproviders/*.py``
    file (the host's own code) to add an engine."""
    _PLUGINS[str(name)] = adapter


def _load_plugins() -> None:
    """Import every ``nexus_dbproviders/*.py`` next to the node so a host can
    drop in their own engine adapters. Host-trusted code on the host's box."""
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True
    from nexus.core.paths import BASE_DIR
    d = BASE_DIR / "nexus_dbproviders"
    if not d.is_dir():
        return
    for f in sorted(d.glob("*.py")):
        try:
            spec = importlib.util.spec_from_file_location(
                f"nexus_dbproviders.{f.stem}", f)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            # A plugin either calls register_adapter() at import, or exposes a
            # module-level create/drop — in which case we register it by stem.
            if f.stem not in _PLUGINS and hasattr(mod, "create") and hasattr(mod, "drop"):
                _PLUGINS[f.stem] = mod
        except Exception:
            _log.warning("failed to load db provider %s", f, exc_info=True)


def available_adapters() -> list[str]:
    _load_plugins()
    return sorted(set(_BUILTIN) | set(_PLUGINS))


def get_adapter(engine: str):
    """Return the adapter for *engine* (plugins override builtins). Raises
    ``ValueError`` for an unknown engine."""
    _load_plugins()
    name = (engine or "").strip().lower()
    a = _PLUGINS.get(name) or _BUILTIN.get(name)
    if a is None:
        raise ValueError(f"unknown db engine '{engine}'")
    return a


# --- provision / deprovision ------------------------------------------------


def provision(engine: str, admin_dsn: str, service_name: str,
              consumer_pubkey: str) -> dict:
    """Create (idempotently) a per-consumer database + login. Returns
    ``{engine, kind, database, user, password}``."""
    adapter = get_adapter(engine)
    database, user = _names(service_name, consumer_pubkey)
    password = secrets.token_urlsafe(18)
    adapter.create(admin_dsn, database, user, password)
    return {
        "engine": engine,
        "kind": getattr(adapter, "KIND", engine),
        "database": database,
        "user": user,
        "password": password,
    }


def deprovision(engine: str, admin_dsn: str, service_name: str,
                consumer_pubkey: str) -> None:
    """Drop the per-consumer database + login (idempotent)."""
    adapter = get_adapter(engine)
    database, user = _names(service_name, consumer_pubkey)
    adapter.drop(admin_dsn, database, user)


__all__ = [
    "register_adapter", "available_adapters", "get_adapter",
    "provision", "deprovision",
]
